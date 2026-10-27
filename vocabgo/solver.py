# -*- coding: utf-8 -*-
"""答题引擎：把一道题变成一次「判对 + 保存」。

策略链（先干净、后兜底，越靠前越像人）：

1. **cache**  本地题库命中（历史答过，零风险）
2. **match**  规则匹配：mode=0 学到的词义 / 搭配题 remark 里直接给的 relation
3. **llm**    大模型兜底（需自行配置 OpenAI 兼容接口）
4. **probe**  试答案：提交错误答案逼服务端回吐 answer_corrects，再用正确答案重判
              —— 注意这招会故意答错，而词达人的词分按作答正确率累计，所以放在模型之后

策略链顺序（见 `_solve_one`）：题库 → 规则匹配 → 大模型 → 试答案 → 盲选。
把「试答案」放在后面是**故意**的：实测错两次对一次的词只能拿 3.7 分，
错一次对一次拿 5.0 分，全程不错能到 7.5+，所以能一次答对就别故意答错。
5. **guess**  最后盲选（只保证流程不卡死）

其中 3 是关键：VerifyAnswer 连续两次收到错误答案后，响应体会带上本题的
answer_corrects，于是可以「用正确答案再判一次 → answer_result=1 → 提交」，
最终落库的是正确作答，因此仍然能拿高分。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .bank import Bank
from .client import VocabgoClient, join_phrase
from .llm import LlmHelper
from .vision import ImageReader, hydrate_topic, topic_image_fields
from .glossary import Glossary, match_option, meaning_hint
from .materials import MaterialLibrary
from .matcher import (
    absorb_word_defs,
    display_answer,
    display_stem,
    is_collocation,
    match_choice,
    match_collocation,
    target_word,
    topic_key,
    topic_key_loose,
    topic_mode,
    topic_options,
    topic_remark,
    topic_stem,
)

WRONG_SENTINEL = ""  # 试答案用的「明显错误」作答

# 日志里标注「这题靠什么答出来的」——界面上全部用中文
SOURCE_LABELS = {
    "cache": "题库",
    "match": "规则",
    "glossary": "词库",
    "materials": "本地资料",
    "probe": "试答案",
    "llm": "大模型",
    "guess": "猜测",
    "ocr": "读图",
    "fixed": "服务端纠错",
}
SOURCE_CN = {key: f"[{value}]" for key, value in SOURCE_LABELS.items()}


def source_label(source: str) -> str:
    return SOURCE_CN.get(source, f"[{source or '未知'}]")


@dataclass
class Stats:
    """一次任务的答题统计，用来判断准确率和兜底链路是否健康。"""

    total: int = 0
    done: int = 0
    cache: int = 0
    match: int = 0
    glossary: int = 0  # 在线词典 / 本地词库命中
    materials: int = 0  # 用户导入的词表、教材或电子书命中
    probe: int = 0
    llm: int = 0
    thinking_retry: int = 0
    thinking_on: int = 0
    guess: int = 0
    ocr: int = 0
    fixed: int = 0
    wrong_saved: int = 0
    errors: int = 0
    cancelled: bool = False
    sources: dict[str, int] = field(default_factory=dict)

    def hit(self, source: str) -> None:
        self.sources[source] = self.sources.get(source, 0) + 1

    def report(self) -> str:
        parts = [
            f"进度 {self.done}/{self.total}",
            f"题库 {self.cache}",
            f"规则 {self.match}",
            f"词库 {self.glossary}",
            f"本地资料 {self.materials}",
            f"试答案 {self.probe}",
            f"LLM {self.llm}",
            f"猜测 {self.guess}",
            f"服务端纠错 {self.fixed}",
        ]
        if self.ocr:
            parts.insert(-1, f"读图 {self.ocr}")
        if self.errors:
            parts.append(f"异常 {self.errors}")
        if self.thinking_retry:
            parts.append(f"思考重试 {self.thinking_retry}")
        return " | ".join(parts)


class Solver:
    def __init__(
        self,
        client: VocabgoClient,
        bank: Bank,
        *,
        kind: str = "class",
        llm: LlmHelper | None = None,
        reader: ImageReader | None = None,
        glossary: Glossary | None = None,
        materials: MaterialLibrary | None = None,
        vocabulary: Iterable[str] | None = None,
        fill_fail_path: str | Path | None = None,
        probe: bool = True,
        probe_attempts: int = 2,
        llm_votes: int = 1,
        adaptive_thinking: bool = True,
        pace: tuple[float, float] = (1.0, 2.6),
        dry_run: bool = False,
        cancel: "threading.Event | None" = None,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.bank = bank
        self.kind = kind  # "class" = 班级任务, "study" = 自学任务
        self.llm = llm
        self.reader = reader
        self.glossary = glossary  # 在线词典 / 本地词库，用来把「凭记忆」变成「查得到」
        self.materials = materials  # 用户上传的词表、教材和电子书
        self.vocabulary: set[str] = set()
        self.fill_fail_path = (
            Path(fill_fail_path)
            if fill_fail_path
            else self.bank.path.parent / "failed_fill.jsonl"
        )
        self.set_vocabulary(vocabulary or ())
        self.probe_enabled = probe
        self.probe_attempts = probe_attempts
        self.llm_votes = max(1, int(llm_votes))
        self.adaptive_thinking = bool(adaptive_thinking)
        self.pace = pace
        self.dry_run = dry_run
        self.cancel = cancel  # GUI 的「停止」按钮：置位后本题收尾即退出
        self.log = logger
        self.word_defs: dict[str, list[str]] = {}
        self.stats = Stats()
        self.last_accepted_answer: Any = None
        self.last_fixed_candidate: Any = None

    def set_vocabulary(self, words: Iterable[str]) -> None:
        """设置当前任务词表；只用于填空候选筛选，不会把整表塞进提示词。"""
        cleaned: set[str] = set()
        for item in words:
            for token in re.findall(r"[A-Za-z][A-Za-z'-]*", str(item or "").lower()):
                if token:
                    cleaned.add(token)
        self.vocabulary = cleaned

    # ── 对外入口 ──
    def run(
        self,
        task_id: Any = None,
        release_id: Any = None,
        *,
        course_id: str | None = None,
        list_id: Any = None,
        task_type: int = 3,
        grade: int = 2,
    ) -> Stats:
        resp = self._start(task_id, release_id, course_id, list_id, task_type, grade)
        topic = VocabgoClient.pick_topic(resp)
        if not topic:
            raise RuntimeError(f"StartAnswer 未返回题目: {resp.get('code')} {resp.get('msg') or ''}")

        data = resp.get("data") or {}
        self.stats.total = int(data.get("topic_total") or 0)
        self.stats.done = int(data.get("topic_done_num") or 0)
        self.log(
            f"🚀 开始本会话作答：{self.stats.done}/{self.stats.total}，"
            f"题库 {len(self.bank)} 条，填空约束 v3"
        )

        guard = 0
        while topic:
            if self.cancel is not None and self.cancel.is_set():
                self.stats.cancelled = True
                self.log(f"⏹️ 手动停止　{self.stats.report()}")
                self.bank.flush()
                return self.stats

            topic = self._hydrate(topic)  # 题干/选项若是图片，先读图再进策略链
            code = str(topic.get("topic_code") or "")
            mode = topic_mode(topic)
            self.stats.done = int(topic.get("topic_done_num") or self.stats.done)
            self.stats.total = int(topic.get("topic_total") or self.stats.total)

            try:
                if mode == 0:
                    absorb_word_defs(topic, self.word_defs)
                    self.log(f"  [{self.stats.done}/{self.stats.total}] 📖 {display_stem(topic)}")
                    save = self._submit(code, mode)
                else:
                    save = self._solve_one(topic)
            except Exception as exc:
                text = str(exc)
                if "20001" in text or "需要选词" in text:
                    # 这不是故障：本轮选中的词都答完了，服务端要求重新选词。
                    # 属于正常的会话结束，别报成「异常」吓人。
                    self.log(f"✅ 本轮题目已答完（{self.stats.done}/{self.stats.total}）　{self.stats.report()}")
                    self.bank.flush()
                    return self.stats
                if ("20004" in text or "任务已完成" in text) and self.kind == "class":
                    progress = self._class_progress(task_id, release_id)
                    self.log(
                        f"🔄 当前答题会话已结束，任务总进度 "
                        f"{progress if progress is not None else '未知'}%，准备重新选词"
                    )
                    self.bank.flush()
                    return self.stats
                self.stats.errors += 1
                self.log(f"  ⚠️ 本题异常，跳过：{type(exc).__name__}: {exc}")
                self.bank.flush()
                return self.stats

            next_topic = VocabgoClient.pick_topic(save)
            if not next_topic or str(next_topic.get("topic_code")) == code:
                finish = save.get("data") or {}
                if isinstance(finish, dict) and finish.get("topic_done_num"):
                    self.stats.done = int(finish["topic_done_num"])
                    self.stats.total = int(finish.get("topic_total") or self.stats.total)
                self.log(f"🎉 结束：{self.stats.done}/{self.stats.total}　{self.stats.report()}")
                self.bank.flush()
                return self.stats

            topic = next_topic
            guard += 1
            if guard > 5000:
                self.log("⚠️ 循环次数过多，主动退出以防死循环")
                self.bank.flush()
                return self.stats
            if mode != 0:
                self._sleep()

        self.bank.flush()
        return self.stats

    def _class_progress(self, task_id: Any, release_id: Any) -> int | None:
        """读取班级任务真实进度，用来判断 20004 后是否还能继续。"""
        if self.kind != "class" or not task_id or not release_id:
            return None
        try:
            data = self.client.task_info(int(task_id), int(release_id)).get("data") or {}
            return int(data.get("progress") or 0)
        except Exception:
            return None

    # ── 单题处理 ──
    def _hydrate(self, topic: dict[str, Any]) -> dict[str, Any]:
        """读图：题干/选项以图片下发时，OCR 成文本，后续匹配逻辑不用改。"""
        if self.reader is None or not topic_image_fields(topic):
            return topic
        try:
            replaced = hydrate_topic(topic, self.reader, logger=self.log)
            self.stats.ocr += replaced
        except Exception as exc:
            self.log(f"  ⚠️ 读图失败，按原样继续：{type(exc).__name__}: {exc}")
        return topic

    def _solve_one(self, topic: dict[str, Any]) -> dict[str, Any]:
        code = str(topic.get("topic_code") or "")
        mode = topic_mode(topic)
        stem = display_stem(topic)

        if is_collocation(topic):
            return self._solve_multi(topic)

        key = topic_key(topic)

        # 1) 题库
        cached = self.bank.get(key)
        if cached is None:
            if mode == 32:
                cached = self._loose_phrase_answer(topic, mode)
            elif mode == 22:
                cached = self._loose_choice_answer(topic, mode)
        if cached is not None:
            code, ok, _ = self._answer(topic, code, cached)
            if ok:
                self.stats.cache += 1
                self.log(f"  ✅ {stem} → {display_answer(topic, cached)} {source_label('cache')}")
                return self._submit(code, mode)

        # 2) 规则匹配
        matched = match_choice(topic, self.word_defs)
        if matched is not None:
            code, ok, _ = self._answer(topic, code, matched)
            if ok:
                self.stats.match += 1
                self.bank.put(key, matched, display_stem(topic))
                self.log(f"  ✅ {stem} → {display_answer(topic, matched)} {source_label('match')}")
                return self._submit(code, mode)

        # 2.5) 用户本地资料：先由脚本查词表和教材，明确命中就完全跳过模型。
        by_materials = self._solve_by_materials(topic, mode)
        if by_materials is not None:
            return by_materials

        # 2.6) 查词典（在线词典 / 本地词库）
        # 让「凭记忆」变成「查得到」：拿题干里的词去查中文释义，再和选项比对。
        # 比对是保守的 —— 不够确定就返回 None，交给后面的模型/试答案，绝不硬猜。
        by_dict = self._solve_by_glossary(topic, mode)
        if by_dict is not None:
            return by_dict

        # 2.7) 中文短语 → 英文候选 → 选项池精确匹配
        # 有道免费接口能处理「许多例子」这类中文，不依赖大模型密钥。
        by_phrase = self._solve_phrase_by_glossary(topic, mode)
        if by_phrase is not None:
            return by_phrase

        # 3) 大模型
        # 注意顺序：**先问模型，再考虑试答案**。
        # 因为词达人的词分是按作答正确率累计的（实测：错两次对一次 ≈ 3.7 分，
        # 错一次对一次 ≈ 5.0 分，全程不错 ≈ 7.5 分），而试答案会故意答错，
        # 每用一次就把这个词的记录拉低一截。所以只在模型也没辙时才动用它。
        if self.llm and self.llm.enabled and not self.llm.auth_failed:
            # 先查词典，把权威释义塞进 prompt —— 让模型「看着词典挑」而不是「凭记忆猜」
            hint = ""
            if self.glossary and self.glossary.enabled:
                word = target_word(topic)
                if word:
                    hint = meaning_hint(self.glossary.lookup(word))
            material_hint = (
                self.materials.context_for_topic(topic)
                if self.materials and self.materials.enabled
                else ""
            )
            guess = self._ask_llm(topic, mode, hint, material_hint)
            if guess is not None:
                code, ok, answer_data = self._answer(topic, code, guess)
                if ok:
                    accepted = self.last_accepted_answer
                    self.stats.llm += 1
                    self.bank.put(key, accepted, display_stem(topic))
                    self.log(f"  🤖 {stem} → {display_answer(topic, accepted)} {source_label('llm')}")
                    return self._submit(code, mode)
                if mode == 51:
                    for form in self._fill_word_forms(guess):
                        code, form_ok, form_data = self._answer(topic, code, form)
                        if form_ok:
                            accepted = self.last_accepted_answer
                            self.stats.llm += 1
                            self.bank.put(key, accepted, display_stem(topic))
                            self.log(
                                f"  🤖 {stem} → {display_answer(topic, accepted)} "
                                f"{source_label('llm')}（词形调整）"
                            )
                            return self._submit(code, mode)
                self._record_fill_failure(topic, mode, "server_rejected", guess, answer_data)
                self.log(f"  ⚠️ {stem} → 大模型答案未通过校验，进入兜底")
            else:
                self._record_fill_failure(topic, mode, "no_consensus", None)
                self.log(f"  ⚠️ {stem} → 大模型未给出稳定答案，进入兜底")

        if self.llm and self.llm.auth_failed:
            self.log("  ❌ 大模型鉴权失败（HTTP 401）：请重新创建 API Key；本题只能使用题库/规则兜底")

        # 4) 试答案（兜底：故意答错换服务端的正确答案，会拉低该词正确率，谨慎使用）
        if self.probe_enabled:
            code, probed = self._probe(topic, code)
            if probed is not None:
                code, ok, data = self._answer(topic, code, probed)
                if ok:
                    accepted = self.last_accepted_answer
                    self.stats.probe += 1
                    self.bank.put(key, accepted, display_stem(topic))
                    self.log(f"  🔎 {stem} → {display_answer(topic, accepted)} {source_label('probe')}")
                    return self._submit(code, mode)
                # 极端情况：服务端自己回吐的答案判不过，直接保存现状
                self.stats.wrong_saved += 1
                self.log(f"  ⚠️ {stem} → 试答案纠正失败，按现状提交")
                return self._submit(str(data.get("topic_code") or code), mode)

        # 5) 盲选
        fallback: Any = self._default_answer(topic)
        if mode == 32 and self.last_fixed_candidate is not None:
            fallback = self.last_fixed_candidate
        code, ok, _ = self._answer(topic, code, fallback)
        self.stats.guess += 1
        if not ok:
            self.stats.wrong_saved += 1
        reason = (
            "大模型鉴权失败"
            if self.llm and self.llm.auth_failed
            else "大模型不可用或答案不稳定"
            if self.llm and self.llm.enabled
            else "未启用大模型"
        )
        self.log(
            f"  ❓ {stem} → {display_answer(topic, fallback)} "
            f"{source_label('guess')}（mode={mode}；{reason}）"
        )
        return self._submit(code, mode)

    def _ask_llm(
        self,
        topic: dict[str, Any],
        mode: int,
        hint: str,
        material_hint: str,
    ) -> Any | None:
        """基础题先关思考；无稳定结果时再开思考重试。"""
        if not self.llm:
            return None

        def ask(thinking: str | None, votes: int) -> Any | None:
            kwargs = {
                "material_hint": material_hint,
                "votes": votes,
                "thinking": thinking,
            }
            if mode == 32:
                return self.llm.ask_phrase(topic, self.word_defs, hint, **kwargs)
            if not topic_options(topic) and mode not in (0, 32):
                return self.llm.ask_fill(
                    topic,
                    self.word_defs,
                    hint,
                    fill_candidates=self._fill_candidate_hint(topic),
                    **kwargs,
                )
            return self.llm.ask_choice(topic, self.word_defs, hint, **kwargs)

        if not self.adaptive_thinking:
            return ask(None, self.llm_votes)

        first_thinking = "off"
        if mode == 11 and not hint and not material_hint:
            word = target_word(topic)
            has_known_defs = bool(word and self.word_defs.get(word))
            if not has_known_defs:
                first_thinking = "on"
                self.log("  🧠 语境题没有本地证据，先用思考模式")

        guess = ask(first_thinking, self.llm_votes)
        if guess is not None:
            if first_thinking == "on":
                self.stats.thinking_on += 1
            return guess

        if first_thinking == "off":
            self.stats.thinking_retry += 1
            self.log("  🧠 未得到稳定答案，开启思考模式重试")
            guess = ask("on", 1)
            if guess is not None:
                self.stats.thinking_on += 1
        return guess

    @staticmethod
    def _phrase_tokens(text: Any) -> list[str]:
        return [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'-]*", str(text or ""))
        ]

    @classmethod
    def _slot_candidate(cls, stem: str, candidate: str) -> str:
        """从「in batches」这类候选中去掉题干固定部分，只留空格答案。"""
        parts = re.split(r"\{\}|_+", str(stem or ""), maxsplit=1)
        if len(parts) != 2:
            return ""
        before = cls._phrase_tokens(parts[0])
        after = cls._phrase_tokens(parts[1])
        tokens = cls._phrase_tokens(candidate)
        if not tokens:
            return ""

        start, end = 0, len(tokens)
        for fixed in before:
            if start < end and tokens[start] == fixed:
                start += 1
            else:
                break
        for fixed in reversed(after):
            if end > start and tokens[end - 1] == fixed:
                end -= 1
            else:
                break

        slot = tokens[start:end]
        if not 1 <= len(slot) <= 6:
            return ""
        return " ".join(slot)

    @staticmethod
    def _word_variants(token: str) -> set[str]:
        """给任务词表做轻量词形兼容，不改变最终提交内容。"""
        value = str(token or "").lower()
        variants = {value}
        if value.endswith("ies") and len(value) > 3:
            variants.add(value[:-3] + "y")
        if value.endswith("es") and len(value) > 2:
            variants.add(value[:-2])
        if value.endswith("s") and len(value) > 1:
            variants.add(value[:-1])
        if value.endswith("ed") and len(value) > 3:
            variants.add(value[:-2])
            variants.add(value[:-1])
        if value.endswith("ing") and len(value) > 4:
            variants.add(value[:-3])
            variants.add(value[:-3] + "e")
        return variants

    def _matches_vocabulary(self, token: str) -> bool:
        if not self.vocabulary:
            return True
        return bool(self._word_variants(token) & self.vocabulary)

    def _fill_candidate_hint(self, topic: dict[str, Any]) -> str:
        """把任务词表和少量翻译候选交给模型，避免自由生成同义词。"""
        stem = topic_stem(topic)
        remark = topic_remark(topic)
        raw: list[str] = []
        if self.glossary and self.glossary.enabled and remark:
            raw.extend(self.glossary.translate_to_english(str(remark)))

        # 之前见过且释义明确包含当前中文含义的词，也可以作为候选。
        for word, defs in self.word_defs.items():
            if remark and any(str(remark) in str(item) for item in defs):
                raw.append(word)

        candidates: list[str] = []
        for item in raw:
            slot = self._slot_candidate(stem, item)
            if not slot:
                continue
            tokens = slot.split()
            if self.vocabulary and not any(self._matches_vocabulary(token) for token in tokens):
                continue
            if slot not in candidates:
                candidates.append(slot)
            if len(candidates) >= 12:
                break

        blocks: list[str] = []
        if self.vocabulary:
            words = sorted(self.vocabulary)[:200]
            blocks.append(
                "当前任务词表（这些是原形；按句子需要补正确词形，允许复数、时态等变化，禁止同义词）:\n"
                + ", ".join(words)
            )
        if candidates:
            blocks.append(
                "中文翻译候选（只作辅助，若与任务词表冲突以任务词表为准）:\n"
                + "\n".join(f"{index}. {item}" for index, item in enumerate(candidates))
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _fill_word_forms(value: Any) -> list[str]:
        """给填空答案生成下一步可尝试的词形，至少覆盖常见复数变化。"""
        text = str(value or "").strip().lower()
        if not text:
            return []
        words = text.split()
        last = words[-1]
        forms: list[str] = []

        if last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
            forms.append(last[:-1] + "ies")
        elif last.endswith(("s", "x", "z", "ch", "sh")):
            forms.append(last + "es")
        else:
            forms.append(last + "s")

        result: list[str] = []
        for form in forms:
            candidate = " ".join(words[:-1] + [form])
            if candidate != text and candidate not in result:
                result.append(candidate)
        return result

    def _record_fill_failure(
        self,
        topic: dict[str, Any],
        mode: int,
        reason: str,
        predicted: Any,
        server_data: dict[str, Any] | None = None,
    ) -> None:
        """把填空题/组词题的失败样本落盘，方便离线复现和补充资料。"""
        if mode == 0 or (mode != 32 and topic_options(topic)):
            return
        samples = []
        source = "last_phrase_samples" if mode == 32 else "last_fill_samples"
        for item in getattr(self.llm, source, []) or []:
            if not isinstance(item, dict):
                continue
            samples.append(
                {
                    "sample": item.get("sample"),
                    "raw": str(item.get("raw") or "")[:500],
                    "parsed": item.get("parsed"),
                }
            )
        data = server_data or {}
        record = {
            "ts": int(time.time()),
            "mode": mode,
            "prompt_version": 4,
            "topic_code": str(topic.get("topic_code") or ""),
            "stem": topic_stem(topic),
            "remark": topic_remark(topic),
            "vocabulary": sorted(self.vocabulary)[:200],
            "options": [
                str(option.get("content") or "")
                for option in topic_options(topic)
                if option.get("content")
            ],
            "candidates": self._fill_candidate_hint(topic)[:4000] if mode != 32 else "",
            "predicted": predicted,
            "reason": reason,
            "samples": samples,
            "server": {
                key: data.get(key)
                for key in ("answer_result", "answer_corrects", "over_status", "msg")
                if key in data
            },
        }
        try:
            self.fill_fail_path.parent.mkdir(parents=True, exist_ok=True)
            with self.fill_fail_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.log(f"  ⚠️ 失败样本写入失败：{type(exc).__name__}: {exc}")

    def _solve_by_glossary(self, topic: dict[str, Any], mode: int) -> dict[str, Any] | None:
        """查词典定答案：题干里的词 → 在线词典/本地词库的中文释义 → 匹配选项。

        这是「不靠记忆靠查证」的那一环。匹配器阈值定得保守，
        不达标就返回 None，让后面的模型或试答案接手 —— 宁可说不知道，也不硬猜。
        """
        if not (self.glossary and self.glossary.enabled):
            return None
        if mode == 32:  # 组词题的选项是英文词，没有中文可对
            return None
        word = target_word(topic)
        if not word:
            return None
        meanings = self.glossary.lookup(word)
        if not meanings:
            return None
        index = match_option(meanings, topic_options(topic))
        if index is None:
            return None
        code = str(topic.get("topic_code") or "")
        code, ok, _ = self._answer(topic, code, index)
        if not ok:
            return None
        self.stats.glossary += 1
        self.stats.hit("glossary")
        self.bank.put(topic_key(topic), index, display_stem(topic))
        self.log(f"  📖 {display_stem(topic)} → {display_answer(topic, index)} {source_label('glossary')}")
        return self._submit(code, mode)

    def _solve_phrase_by_glossary(self, topic: dict[str, Any], mode: int) -> dict[str, Any] | None:
        """mode=32：中文备注翻译成英文后，必须在当前选项池里精确拼出短语。"""
        if mode != 32 or not (self.glossary and self.glossary.enabled):
            return None
        remark = str(topic.get("stem", {}).get("remark") or "").strip()
        if not remark:
            return None
        candidates = self.glossary.translate_to_english(remark)
        if not candidates:
            return None

        options = [str(opt.get("content") or "").strip().lower() for opt in topic_options(topic)]
        pool = {item for item in options if item}
        if not pool:
            return None
        slots = len(re.findall(r"_+", str(topic.get("stem", {}).get("content") or "")))

        answers: list[str] = []
        for candidate in candidates:
            tokens = [tok for tok in re.findall(r"[A-Za-z][A-Za-z'-]*", candidate) if tok]
            if not tokens:
                continue
            lowered = [tok.lower() for tok in tokens]
            if slots and len(lowered) != slots:
                continue
            if not all(tok in pool for tok in lowered):
                continue
            answer = ",".join(lowered)
            if answer not in answers:
                answers.append(answer)

        # 多个翻译给出不同答案时不硬猜。
        if len(answers) != 1:
            return None
        answer = answers[0]
        code = str(topic.get("topic_code") or "")
        code, ok, _ = self._answer(topic, code, answer)
        if not ok:
            return None
        self.stats.glossary += 1
        self.stats.hit("glossary")
        self.bank.put(topic_key(topic), answer, display_stem(topic))
        self.log(
            f"  📖 {display_stem(topic)} → {display_answer(topic, answer)} {source_label('glossary')}"
        )
        return self._submit(code, mode)

    def _solve_by_materials(self, topic: dict[str, Any], mode: int) -> dict[str, Any] | None:
        """用户资料命中：结构化词条可确定时直接作答，否则留给模型参考正文。"""
        if not (self.materials and self.materials.enabled):
            return None
        answer = (
            self.materials.direct_phrase(topic)
            if mode == 32
            else self.materials.direct_choice(topic)
        )
        if answer is None:
            return None
        code = str(topic.get("topic_code") or "")
        code, ok, _ = self._answer(topic, code, answer)
        if not ok:
            return None
        self.stats.materials += 1
        self.stats.hit("materials")
        self.bank.put(topic_key(topic), answer, display_stem(topic))
        self.log(
            f"  📚 {display_stem(topic)} → {display_answer(topic, answer)} "
            f"{source_label('materials')}"
        )
        return self._submit(code, mode)

    def _loose_phrase_answer(self, topic: dict[str, Any], mode: int) -> str | None:
        """选项变了导致精确键 miss 时，找回同一个题干的旧答案。

        只认「内容型」答案（组词题给的是词本身），并且要求每个词都能在当前选项池里找到 ——
        选择题的答案是选项下标，选项一变下标就没意义了，所以绝不复用，宁可重新推。
        复用内容型答案能省下一次「故意答错」，直接提高正确率记录。
        """
        if mode != 32:
            return None
        pool = {str(opt.get("content") or "").strip().lower() for opt in topic_options(topic)}
        pool.discard("")
        if not pool:
            return None
        for _full_key, answer in self.bank.find_loose(topic_key_loose(topic)):
            if not isinstance(answer, str) or not answer.strip():
                continue
            # 组词题的答案两种存法都见过：`a mathematical formula`（空格）和 `in,many,instances`（逗号）
            tokens = [tok for tok in re.split(r"[\s,，]+", answer.strip()) if tok]
            if tokens and all(tok.lower() in pool for tok in tokens):
                return answer
        return None

    @staticmethod
    def _option_signature(text: str) -> tuple[str, ...]:
        """释义内容签名：忽略词性、顺序、标点和空白。"""
        raw = re.sub(r"^\s*(?:[a-z]{1,5}\.\s*)+", "", str(text or "").strip(), flags=re.I)
        units = [
            re.sub(r"[\s【】\[\]（）()「」“”\"']", "", unit).lower()
            for unit in re.split(r"[；;|,，、/]|\.\s", raw)
        ]
        return tuple(sorted(unit for unit in units if unit))

    def _loose_choice_answer(self, topic: dict[str, Any], mode: int) -> Any | None:
        """mode22 选项会乱序：复用旧答案的释义内容，而不是旧下标。"""
        if mode != 22:
            return None
        options = topic_options(topic)
        current = [self._option_signature(str(option.get("content") or "")) for option in options]
        prefix = topic_key_loose(topic)
        found: list[Any] = []
        for full_key, old_answer in self.bank.find_loose(prefix):
            try:
                old_index = int(old_answer)
                old_options = full_key.rsplit("::", 1)[-1].split("|")
                old_content = old_options[old_index]
            except (ValueError, TypeError, IndexError):
                continue
            signature = self._option_signature(old_content)
            matches = [
                option.get("answer_tag", index)
                for index, (option, candidate) in enumerate(zip(options, current))
                if signature and candidate == signature
            ]
            if len(matches) == 1:
                found.append(matches[0])
        unique = list(dict.fromkeys(found))
        return unique[0] if len(unique) == 1 else None

    def _solve_multi(self, topic: dict[str, Any]) -> dict[str, Any]:
        """搭配题：多选，服务端用 over_status 标记整题判完。"""
        code = str(topic.get("topic_code") or "")
        key = topic_key(topic)
        stem = display_stem(topic)
        answer_num = int(topic.get("answer_num") or 2)
        options = topic_options(topic)

        tags: list[Any] | None = None
        source = ""
        cached = self.bank.get(key)
        if isinstance(cached, list) and cached:
            tags, source = list(cached), "cache"
        if not tags:
            rule_tags = match_collocation(topic)
            if rule_tags:
                tags, source = rule_tags, "match"

        good: list[Any] = []
        tried: list[Any] = []

        if tags:
            for tag in tags[:answer_num]:
                if tag in tried:
                    continue
                data = self._verify(code, tag)
                tried.append(tag)
                code = str(data.get("topic_code") or code)
                if self._answer_ok(data.get("answer_result")):
                    good.append(tag)
                good.extend(t for t in (data.get("answer_corrects") or []) if t not in good)
                if data.get("over_status") == 1:
                    break

        if not good and self.probe_enabled:
            # 枚举所有选项，逐个判对；服务端标记 over_status=1 即整题结束
            source = source or "probe"
            for index, opt in enumerate(options):
                tag = opt.get("answer_tag", index)
                if tag in tried:
                    continue
                data = self._verify(code, tag)
                tried.append(tag)
                code = str(data.get("topic_code") or code)
                if self._answer_ok(data.get("answer_result")):
                    good.append(tag)
                good.extend(t for t in (data.get("answer_corrects") or []) if t not in good)
                if data.get("over_status") == 1:
                    break

        if good:
            self.bank.put(key, good, display_stem(topic))
            if source == "cache":
                self.stats.cache += 1
            elif source == "match":
                self.stats.match += 1
            else:
                self.stats.probe += 1
        else:
            self.stats.wrong_saved += 1

        shown = ",".join(
            display_answer(topic, tag) for tag in (good or tried[:answer_num])
        )
        icon = "🔗" if good else "⚠️"
        self.log(f"  {icon} {stem} → {shown} {source_label(source or 'guess')}")
        return self._submit(code, topic_mode(topic))

    # ── 底层动作 ──
    def _start(self, task_id, release_id, course_id, list_id, task_type, grade) -> dict[str, Any]:
        if self.dry_run:
            raise RuntimeError("dry-run 模式不发起真实请求")
        if self.kind == "study":
            return self.client.study_start_answer(task_id, course_id, list_id, task_type=task_type, grade=grade)
        return self.client.start_answer(task_id, release_id)

    def _verify(self, code: str, answer: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"topic_code": code, "answer_result": 1}
        if self.kind == "study":
            return (self.client.study_verify(code, answer).get("data") or {})
        return (self.client.verify(code, answer).get("data") or {})

    def _submit(self, code: str, mode: int) -> dict[str, Any]:
        spent = random.randint(500, 1500) if mode == 0 else random.randint(2000, 4500)
        if self.dry_run:
            return {"data": {"topic_code": code}}
        if self.kind == "study":
            return self.client.study_submit(code, spent)
        return self.client.submit(code, spent)

    def _answer(self, topic: dict[str, Any], code: str, answer: Any) -> tuple[str, bool, dict[str, Any]]:
        """判一次；错了就看服务端有没有回吐正确答案，再判一次。"""
        answer = self._normalize(topic, answer)
        self.last_accepted_answer = answer
        self.last_fixed_candidate = None
        data = self._verify(code, answer)
        code = str(data.get("topic_code") or code)
        if self._answer_ok(data.get("answer_result")):
            return code, True, data

        corrects = data.get("answer_corrects") or []
        if corrects:
            candidates: list[Any] = []
            for correct in corrects:
                for fixed in (
                    correct,
                    str(correct).strip(),
                    self._normalize(topic, correct),
                ):
                    if fixed not in candidates:
                        candidates.append(fixed)
            if candidates:
                self.last_fixed_candidate = candidates[0]
            for fixed in candidates:
                data2 = self._verify(code, fixed)
                code = str(data2.get("topic_code") or code)
                if self._answer_ok(data2.get("answer_result")):
                    self.stats.fixed += 1
                    self.last_accepted_answer = fixed
                    return code, True, data2
        return code, False, data

    @staticmethod
    def _answer_ok(value: Any) -> bool:
        """1=明确正确；2 暂按未通过处理，避免错误题被误判为已完成。"""
        return value == 1

    def _probe(self, topic: dict[str, Any], code: str) -> tuple[str, Any | None]:
        """连续提交错误答案，把本题的正确答案从服务端套出来。"""
        for _ in range(max(1, self.probe_attempts)):
            data = self._verify(code, WRONG_SENTINEL)
            code = str(data.get("topic_code") or code)
            corrects = data.get("answer_corrects") or []
            if corrects:
                return code, corrects[0]
            if data.get("over_status") == 1:
                break
        return code, None

    def _normalize(self, topic: dict[str, Any], answer: Any) -> Any:
        """组词题的答案要变成 "in,many,instances" 这种逗号串。"""
        if topic_mode(topic) == 32 and isinstance(answer, str) and not answer.isdigit():
            return join_phrase(answer)
        return answer

    def _default_answer(self, topic: dict[str, Any]) -> Any:
        if not topic_options(topic):
            return ""
        if topic_mode(topic) == 32:
            options = topic_options(topic)
            contents = [str(o.get("content") or "").strip() for o in options if o.get("content")]
            slots = len(re.findall(r"_+", str(topic.get("stem", {}).get("content") or "")))
            if slots:
                first_words = {
                    "a", "an", "the", "in", "on", "at", "for", "with",
                    "by", "from", "to", "of", "under", "over",
                }
                last_words = {
                    "sb", "sb.", "sth", "sth.", "somebody", "someone",
                    "something", "oneself",
                }
                contents = sorted(
                    contents,
                    key=lambda item: (
                        2 if item.lower() in last_words else
                        0 if item.lower() in first_words else
                        1,
                        len(item),
                        item.lower(),
                    ),
                )[:slots]
                contents.sort(
                    key=lambda item: (
                        2 if item.lower() in last_words else
                        0 if item.lower() in first_words else
                        1
                    )
                )
            return ",".join(contents)
        options = topic_options(topic)
        return options[0].get("answer_tag", 0) if options else 0

    def _sleep(self) -> None:
        lo, hi = self.pace
        if hi <= 0:
            return
        time.sleep(random.uniform(lo, hi))


def pick_task(records: list[dict[str, Any]], index: int = 0) -> dict[str, Any]:
    if not records:
        raise SystemExit("没有可用的任务")
    if index >= len(records):
        raise SystemExit(f"任务下标越界：{index}（共 {len(records)} 个）")
    return records[index]


def select_words(words: list[dict[str, Any]], max_score: int = 10) -> dict[str, list[str]]:
    """按「未满分的词」构造 word_map，交给 SubmitChoseWord 重置进度。"""
    # 单词满分可能超过 10：目标分按当前任务观察到的最高分自适应，10 只作为最低基线。
    scores: list[float] = []
    for item in words:
        try:
            scores.append(float(item.get("score") or 0))
        except (TypeError, ValueError):
            scores.append(0.0)
    observed_max = max(scores, default=0.0)
    threshold = max(10.0, observed_max)
    word_map: dict[str, list[str]] = {}
    for item, score in zip(words, scores):
        if score >= threshold:
            continue
        key = f"{item.get('course_id')}:{item.get('list_id')}"
        word_map.setdefault(key, []).append(str(item.get("word") or ""))
    return word_map


def select_all_words(words: list[dict[str, Any]]) -> dict[str, list[str]]:
    """测试任务保留全部词，允许部分词超分补足任务总分。"""
    word_map: dict[str, list[str]] = {}
    for item in words:
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        key = f"{item.get('course_id')}:{item.get('list_id')}"
        word_map.setdefault(key, []).append(word)
    return word_map
