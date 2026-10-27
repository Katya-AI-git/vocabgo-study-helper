# -*- coding: utf-8 -*-
"""LLM 兜底与读图（DeepSeek / OpenAI / Ollama …，只要是 OpenAI 兼容接口）。

DeepSeek 两个要点（2026 年的现状，别用老写法）：
  * 模型名：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用，
    正式调用名是 `deepseek-flash`（当前由 DeepSeek-V4.1-Flash-1017 提供服务）
    和 `deepseek-v4-pro`；
    思考模式拆成了独立参数（`thinking`），V4 默认开启，做短答提取建议关掉。
  * 图片：`deepseek-flash` 支持图片输入（识别截图文字、描述图片），
    所以同一把 key 既能当答题兜底、也能当读图后端。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable

import requests

from . import net
from .matcher import display_stem, topic_mode, topic_options, topic_remark, topic_stem

SYSTEM_PROMPT = "你是英语词汇专家，只输出答案本身，不解释。"

# provider 预设：填了 provider 就只需再填 key
# short = 界面上显示的中文名；配置里存的一律是 key（deepseek / openai / …）
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "thinking": "off",
        "env_key": "DEEPSEEK_API_KEY",
        "label": "DeepSeek（答题 + 读图都能干）",
        "short": "DeepSeek（推荐）",
    },
    "openai": {
        "url": "https://api.openai.com",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "thinking": "auto",
        "env_key": "OPENAI_API_KEY",
        "label": "OpenAI",
        "short": "OpenAI",
    },
    "ollama": {
        "url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl",
        "thinking": "auto",
        "env_key": "",
        "label": "本地 Ollama（不联网、不花钱）",
        "short": "本地 Ollama（免费离线）",
    },
    "custom": {
        "url": "",
        "model": "",
        "vision_model": "",
        "thinking": "auto",
        "env_key": "",
        "label": "自定义（任意 OpenAI 兼容端点）",
        "short": "自定义接口",
    },
}

# 思考模式的中文显示名（配置里仍是 auto / off / on）
THINKING_LABELS = {
    "auto": "自动（听服务端默认）",
    "off": "关闭（更快更省）",
    "on": "开启（更慢更贵）",
}


def provider_label(provider: str) -> str:
    """内部标识 → 中文显示名。"""
    if not provider:
        return "自定义接口"
    preset = PROVIDER_PRESETS.get(provider)
    return str(preset.get("short") or preset.get("label")) if preset else f"自定义（{provider}）"


def provider_from_label(label: str) -> str:
    """中文显示名 → 内部标识（已经是标识就原样返回）。"""
    for key, preset in PROVIDER_PRESETS.items():
        if label in (preset.get("short"), preset.get("label"), key):
            return key
    return label


def thinking_label(value: str) -> str:
    return THINKING_LABELS.get(value, THINKING_LABELS["auto"])


def thinking_from_label(label: str) -> str:
    for key, text in THINKING_LABELS.items():
        if label == text or label == key:
            return key
    return "auto"

THINKING_PAYLOAD = {
    "off": {"thinking": {"type": "disabled"}},
    "on": {"thinking": {"type": "enabled"}},
}


class LlmHelper:
    """一次调用只干一件事：把题干丢给模型，拿回答案或读图结果。"""

    def __init__(
        self,
        url: str = "",
        key: str = "",
        model: str = "",
        *,
        provider: str = "",
        thinking: str = "auto",
        vision_model: str = "",
        timeout: float = 60.0,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.key = (key or "").strip()
        self.model = (model or "").strip()
        self.provider = (provider or "").strip().lower()
        self.thinking = (thinking or "auto").strip().lower()
        self.vision_model = (vision_model or self.model).strip()
        self.timeout = timeout
        self.log = logger
        self.auth_failed = False
        self.last_fill_samples: list[dict[str, Any]] = []
        self.last_phrase_samples: list[dict[str, Any]] = []
        self.usage = {
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }

    def reset_usage(self) -> None:
        for key in self.usage:
            self.usage[key] = 0

    def _record_usage(self, body: dict[str, Any]) -> None:
        usage = body.get("usage") or {}
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        if not miss and prompt:
            miss = max(0, prompt - hit)
        self.usage["prompt_cache_hit_tokens"] += hit
        self.usage["prompt_cache_miss_tokens"] += miss
        self.usage["prompt_tokens"] += prompt
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        self.usage["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)
        self.usage["total_tokens"] += int(usage.get("total_tokens") or 0)
        self.usage["calls"] += 1

    def usage_summary(self) -> str:
        hit = self.usage["prompt_cache_hit_tokens"]
        miss = self.usage["prompt_cache_miss_tokens"]
        input_total = hit + miss
        rate = hit * 100 / input_total if input_total else 0
        return (
            f"调用 {self.usage['calls']} 次，缓存命中 {hit}，未命中 {miss}"
            f"（命中率 {rate:.1f}%），输出 {self.usage['completion_tokens']}"
            f"（推理 {self.usage['reasoning_tokens']}），总计 {self.usage['total_tokens']}"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key and self.model)

    # ── 端点 ──
    def _endpoint(self, path: str) -> str:
        base = self.url.rstrip("/")
        return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

    def endpoint_chat(self) -> str:
        return self._endpoint("/chat/completions")

    def endpoint_models(self) -> str:
        return self._endpoint("/models")

    # ── 底层 ──
    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}

    def _post(self, payload: dict[str, Any], *, path: str = "/chat/completions") -> tuple[dict[str, Any] | None, str]:
        """发请求；带 thinking 参数被拒时自动退回不带该参数再试一次。"""
        url = self._endpoint(path)
        attempts: list[dict[str, Any]] = [payload]
        if "thinking" in payload:
            stripped = {k: v for k, v in payload.items() if k != "thinking"}
            attempts.append(stripped)

        last_error = ""
        for index, body in enumerate(attempts):
            try:
                resp = net.request("POST", url, headers=self._headers(), json=body, timeout=self.timeout)
            except Exception as exc:
                last_error = net.friendly_error(exc, url)
                continue
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code == 401:
                    self.auth_failed = True
                # 只有「thinking 参数不被接受」才值得换个姿势重试，其余错误直接返回
                if index == 0 and "thinking" in body and "thinking" in resp.text.lower():
                    self.log("    ℹ️ 该模型不接受「思考模式」参数，已自动去掉重试")
                    continue
                return None, last_error
            try:
                body = resp.json()
                self._record_usage(body)
                return body, ""
            except ValueError:
                return None, f"返回不是 JSON: {resp.text[:200]}"
        return None, last_error

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        *,
        thinking: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        selected = (self.thinking if thinking is None else thinking).strip().lower()
        if selected in THINKING_PAYLOAD:
            payload.update(THINKING_PAYLOAD[selected])
        return payload

    @staticmethod
    def _content_of(body: dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        # 思考型模型会把推理放在 reasoning_content，答案仍在 content
        return str(message.get("content") or "").strip()

    def _chat(
        self,
        prompt: str,
        *,
        model: str = "",
        max_tokens: int = 512,
        sample_index: int = 0,
        thinking: str | None = None,
    ) -> str | None:
        if self.auth_failed:
            return None
        last_error = ""
        token_limit = max(16, int(max_tokens))
        for attempt in range(3):
            body, error = self._post(
                self._payload(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"{prompt}\n\n[采样序号 {sample_index}]"
                                if sample_index
                                else prompt
                            ),
                        },
                    ],
                    model or self.model,
                    token_limit,
                    thinking=thinking,
                )
            )
            if body is None:
                last_error = error
                if attempt < 2 and not self.auth_failed:
                    continue
                break
            text = self._content_of(body)
            if text:
                return text
            choice = ((body.get("choices") or [{}])[0]) or {}
            if choice.get("finish_reason") == "length" and attempt < 2:
                token_limit = min(2048, token_limit * 2)
                self.log(f"    ⚠️ 推理额度用完，提升到 {token_limit} tokens 后重试")
                continue
            if attempt < 2:
                self.log("    ⚠️ 大模型返回空内容，正在重试")
        if last_error:
            self.log(f"    ⚠️ 大模型调用失败: {last_error}")
        return None

    # ── 业务能力 ──
    @staticmethod
    def _consensus(values: list[Any], votes: int) -> Any | None:
        """多次作答取多数；没有达到简单多数就不硬选。"""
        if not values:
            return None
        value, count = Counter(values).most_common(1)[0]
        return value if count >= max(1, votes // 2 + 1) else None

    @staticmethod
    def _parse_choice(text: str, options: list[dict[str, Any]]) -> int | None:
        """兼容数字、选项字母、带前缀编号和直接输出选项内容。"""
        value = str(text or "").strip().strip("`。.!！")
        if not value or not options:
            return None

        # 最常见：0 / 2 / 选项 1 / 答案：3
        match = re.search(r"(?:选项|答案|option|answer)?\s*[:：]?\s*(\d+)", value, re.IGNORECASE)
        if match:
            index = int(match.group(1))
            if 0 <= index < len(options):
                return index

        # 模型有时只回 A/B/C/D
        letter = re.fullmatch(r"([A-Da-d])", value)
        if not letter:
            prefixed = re.match(r"^\s*([A-Da-d])\s*[\.\)、:：]", value)
            if prefixed:
                letter = prefixed
        if letter:
            index = ord(letter.group(1).upper()) - ord("A")
            return index if 0 <= index < len(options) else None

        # 最后兼容直接输出完整选项内容
        lowered = value.lower()
        for index, option in enumerate(options):
            content = str(option.get("content") or "").strip()
            if content and (content.lower() in lowered or lowered in content.lower()):
                return index
        return None

    def ask_choice(
        self,
        topic: dict[str, Any],
        word_defs: dict[str, list[str]],
        hint: str = "",
        material_hint: str = "",
        *,
        votes: int = 1,
        thinking: str | None = None,
    ) -> Any | None:
        """选择题：可多次独立作答并投票。hint 是词典查到的权威释义。"""
        options = topic_options(topic)
        if not options:
            return None
        answers: list[int] = []
        prompt = self.build_prompt(topic, word_defs, hint, material_hint)
        wanted = max(1, int(votes))
        for sample_index in range(1, wanted + 1):
            if self.auth_failed:
                break
            text = self._chat(
                prompt,
                max_tokens=256,
                sample_index=sample_index,
                thinking=thinking,
            )
            parsed = self._parse_choice(text or "", options)
            if parsed is not None:
                answers.append(parsed)
        return self._consensus(answers, wanted)

    @staticmethod
    def _parse_phrase(text: str, topic: dict[str, Any]) -> str | None:
        """短语必须严格使用题目选项，并与空格组数一致。"""
        options = topic_options(topic)
        pool = {
            str(option.get("content") or "").strip().lower()
            for option in options
            if str(option.get("content") or "").strip()
        }
        if not pool:
            return None

        def canonical_phrase_key(value: str) -> str:
            key = str(value or "").strip(".,!?;:").lower().replace("’", "'")
            return {
                "somebody": "sb",
                "someone": "sb",
                "something": "sth",
                "sb.'s": "sb_poss",
                "sb's": "sb_poss",
                "somebody's": "sb_poss",
                "someone's": "sb_poss",
                "sth.'s": "sth_poss",
                "sth's": "sth_poss",
                "something's": "sth_poss",
            }.get(key, key)

        pool_by_key: dict[str, str] = {}
        for item in pool:
            key = canonical_phrase_key(item)
            pool_by_key.setdefault(key, item)
        stem = topic_stem(topic)
        slots = len(re.findall(r"_+", stem))
        raw_tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'’.-]*", str(text or ""))
        ]
        if not raw_tokens:
            return None

        def pool_token(token: str) -> str | None:
            key = canonical_phrase_key(token)
            variants = [key]
            if key.endswith("ies"):
                variants.append(key[:-3] + "y")
            if key.endswith("es"):
                variants.append(key[:-2])
            if key.endswith("s"):
                variants.append(key[:-1])
            for variant in variants:
                if variant in pool_by_key:
                    return pool_by_key[variant]
            return None

        tokens = [pool_token(token) for token in raw_tokens]
        if any(token is None for token in tokens):
            return None
        normalized = [str(token) for token in tokens]

        # 模型常漏掉冠词。空位数只差一个时，按首词读音补 a/an。
        if slots and len(normalized) == slots - 1:
            article = "an" if normalized[0][0] in "aeiou" else "a"
            if article in pool:
                normalized.insert(0, article)
        if slots and len(normalized) != slots:
            return None
        return ",".join(normalized)

    def ask_phrase(
        self,
        topic: dict[str, Any],
        word_defs: dict[str, list[str]],
        hint: str = "",
        material_hint: str = "",
        *,
        votes: int = 1,
        thinking: str | None = None,
    ) -> str | None:
        """组词题：可多次独立作答，按完整短语投票。"""
        answers: list[str] = []
        trace: list[dict[str, Any]] = []
        prompt = self.build_prompt(topic, word_defs, hint, material_hint)
        wanted = max(1, int(votes))
        max_samples = wanted if wanted == 1 else wanted + 2
        for sample_index in range(1, max_samples + 1):
            if self.auth_failed:
                break
            text = self._chat(
                prompt,
                max_tokens=768,
                sample_index=sample_index,
                thinking=thinking,
            )
            parsed = self._parse_phrase(text or "", topic)
            trace.append(
                {
                    "sample": sample_index,
                    "raw": text or "",
                    "parsed": parsed,
                }
            )
            if parsed:
                answers.append(parsed)
                result = self._consensus(answers, wanted)
                if result is not None:
                    self.last_phrase_samples = trace
                    return result
        result = self._consensus(answers, wanted)
        self.last_phrase_samples = trace
        return result

    @staticmethod
    def _parse_fill(text: str, topic: dict[str, Any] | None = None) -> str | None:
        """解析无选项填空题答案：只接受 1 到 6 个英文词。"""
        value = str(text or "").strip().strip("`\"'。.!！")
        lines = [line.strip().strip("`\"'。.!！") for line in value.splitlines() if line.strip()]
        if not lines:
            return None
        candidate = lines[-1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z' -]{0,79}", candidate):
            return None
        tokens = candidate.split()
        if not 1 <= len(tokens) <= 6:
            return None

        # 模型有时会把固定搭配整段抄回来；只提交空格中的部分。
        if topic:
            stem = str((topic.get("stem") or {}).get("content") or "")
            parts = re.split(r"\{\}|_+", stem, maxsplit=1)
            if len(parts) == 2:
                before = re.findall(r"[A-Za-z][A-Za-z'-]*", parts[0].lower())
                after = re.findall(r"[A-Za-z][A-Za-z'-]*", parts[1].lower())
                start, end = 0, len(tokens)
                for fixed in before:
                    if start < end and tokens[start].lower() == fixed:
                        start += 1
                    else:
                        break
                for fixed in reversed(after):
                    if end > start and tokens[end - 1].lower() == fixed:
                        end -= 1
                    else:
                        break
                tokens = tokens[start:end]
                if not 1 <= len(tokens) <= 6:
                    return None
        return " ".join(token.lower() for token in tokens)

    def ask_fill(
        self,
        topic: dict[str, Any],
        word_defs: dict[str, list[str]],
        hint: str = "",
        material_hint: str = "",
        *,
        fill_candidates: str = "",
        votes: int = 1,
        thinking: str | None = None,
    ) -> str | None:
        """mode=51 无选项填空题：独立作答并投票。"""
        answers: list[str] = []
        trace: list[dict[str, Any]] = []
        prompt = self.build_prompt(
            topic,
            word_defs,
            hint,
            material_hint,
            fill_candidates=fill_candidates,
            include_word_defs=False,
        )
        wanted = max(1, int(votes))
        max_samples = wanted if wanted == 1 else wanted + 2
        for sample_index in range(1, max_samples + 1):
            if self.auth_failed:
                break
            text = self._chat(
                prompt,
                max_tokens=512,
                sample_index=sample_index,
                thinking=thinking,
            )
            parsed = self._parse_fill(text or "", topic)
            trace.append(
                {
                    "sample": sample_index,
                    "raw": text or "",
                    "parsed": parsed,
                }
            )
            if parsed:
                answers.append(parsed)
                result = self._consensus(answers, wanted)
                if result is not None:
                    self.last_fill_samples = trace
                    return result
        result = self._consensus(answers, wanted)
        self.last_fill_samples = trace
        return result

    def ask_topic(
        self,
        topic: dict[str, Any],
        word_defs: dict[str, list[str]],
        hint: str = "",
        material_hint: str = "",
        *,
        mode: int,
        fill_candidates: str = "",
        votes: int = 1,
        thinking: str | None = None,
    ) -> Any | None:
        """按题型分派，并允许本次调用单独覆盖思考模式。"""
        if mode == 32:
            return self.ask_phrase(
                topic,
                word_defs,
                hint,
                material_hint,
                votes=votes,
                thinking=thinking,
            )
        if not topic_options(topic) and mode not in (0, 32):
            return self.ask_fill(
                topic,
                word_defs,
                hint,
                material_hint,
                fill_candidates=fill_candidates,
                votes=votes,
                thinking=thinking,
            )
        return self.ask_choice(
            topic,
            word_defs,
            hint,
            material_hint,
            votes=votes,
            thinking=thinking,
        )

    def describe_image(self, data_uri: str, prompt: str) -> str | None:
        """读图：把 data URI 图片交给多模态模型识别（DeepSeek 用 deepseek-flash）。"""
        if not self.enabled:
            return None
        body, error = self._post(
            self._payload(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
                self.vision_model or self.model,
                1024,
            )
        )
        if body is None:
            self.log(f"    ⚠️ 读图失败: {error}")
            return None
        return self._content_of(body) or None

    def list_models(self) -> list[str]:
        """拉模型列表（用于确认 key 能用、以及有哪些模型名可用）。"""
        try:
            resp = net.request("GET", self.endpoint_models(), headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            return [str(item.get("id") or "") for item in data if item.get("id")]
        except Exception as exc:
            if getattr(exc, "response", None) is not None and exc.response.status_code == 401:
                self.auth_failed = True
            self.log(f"    ⚠️ 拉取模型列表失败: {net.friendly_error(exc, self.endpoint_models())}")
            return []

    def diagnose(self, image_data_uri: str = "") -> dict[str, Any]:
        """体检：模型列表 + 一次真实对话 +（可选）一次真实读图。"""
        report: dict[str, Any] = {
            "provider": self.provider or "custom",
            "endpoint": self.endpoint_chat(),
            "model": self.model,
            "vision_model": self.vision_model,
            "thinking": self.thinking,
        }
        if self.auth_failed:
            report.update({"ok": False, "reason": "API Key 鉴权失败（HTTP 401），请重新创建密钥"})
            return report
        if not self.enabled:
            report.update({"ok": False, "reason": "缺少 url / key / model"})
            return report

        report["models"] = self.list_models()
        answer = self._chat("Reply with exactly: OK", max_tokens=8)
        report["chat_ok"] = bool(answer)
        report["chat_reply"] = answer
        if image_data_uri:
            text = self.describe_image(image_data_uri, "只输出图中的英文文字，不要解释。")
            report["vision_ok"] = bool(text)
            report["vision_text"] = text
        report["ok"] = bool(report["chat_ok"])
        return report

    # ── 提示词 ──
    @staticmethod
    def build_prompt(
        topic: dict[str, Any],
        word_defs: dict[str, list[str]],
        hint: str = "",
        material_hint: str = "",
        *,
        fill_candidates: str = "",
        include_word_defs: bool = True,
    ) -> str:
        mode = topic_mode(topic)
        stem = topic_stem(topic)
        remark = topic_remark(topic)
        options = topic_options(topic)
        opts_text = "\n".join(f"{i}. {opt.get('content', '')}" for i, opt in enumerate(options))

        glossary = ""
        # 在线词典/本地词库查到的释义优先采信 —— 比模型凭记忆靠谱
        if hint.strip():
            glossary += f"权威词典释义（与被考单词匹配的优先采信）: {hint.strip()}\n\n"
        if include_word_defs and word_defs:
            lines = [f"  {w}: {'; '.join(ds)}" for w, ds in list(word_defs.items())[:40]]
            glossary += "已知单词释义:\n" + "\n".join(lines) + "\n\n"
        materials = ""
        if material_hint.strip():
            materials = (
                "用户本地资料检索（只采用与本题明确相关的内容）:\n"
                f"{material_hint.strip()[:2400]}\n\n"
            )

        if mode == 32:
            return (
                f"{glossary}{materials}题目: 用下面的选项词组成短语, 中文含义是「{remark}」\n"
                f"空格数: {stem}\n选项:\n{opts_text}\n\n"
                "请给出正确的词及其顺序。只输出逗号分隔的词, 例如 in,many,instances, 不要解释。"
            )
        if not options and mode not in (0, 32):
            reference = f"中文含义: {remark}\n" if remark else ""
            candidates = ""
            if fill_candidates.strip():
                candidates = (
                    "当前任务词表与相关候选"
                    "（词表是原形；按句子补正确词形，允许复数、时态等变化，禁止同义词）:\n"
                    f"{fill_candidates.strip()[:2000]}\n\n"
                )
            return (
                f"{glossary}{materials}{candidates}"
                "题目: 根据句意和中文含义，填写 {} 中缺少的英语单词或短语\n"
                f"句子: {stem}\n{reference}\n"
                "只输出应填入的英文内容，优先且尽量只使用任务词表中的词，"
                "保持题目要求的单复数、时态和固定搭配，"
                "不要输出序号、引号或解释。"
            )
        if mode == 31:
            return (
                f"{glossary}{materials}题目: 下面哪个是单词「{stem}」的正确释义?\n选项:\n{opts_text}\n\n"
                "只输出选项编号, 例如 0, 不要解释。"
            )
        if mode == 11:
            reference = f"句子中文翻译(参考): {remark}\n" if isinstance(remark, str) and remark else ""
            return (
                f"{glossary}{materials}题目: 判断句中 {{}} 内单词在该句中的具体含义, 选出正确释义\n"
                f"句子: {stem}\n{reference}选项:\n{opts_text}\n\n"
                "注意一词多义与词性, 必须结合上下文。只输出选项编号, 例如 0, 不要解释。"
            )
        return (
            f"{glossary}{materials}题目 (mode={mode}) 题干: {display_stem(topic)}\n备注: {remark}\n选项:\n{opts_text}\n\n"
            "选择题输出选项编号; 组词题输出逗号分隔的词。不要解释。"
        )


# 视觉模型的常见命名特征，用来从模型列表里认出「哪个能读图」
VISION_HINTS = ("vision", "vl", "visual", "omni", "image")


def guess_models(ids: list[str], provider: str = "") -> tuple[str, str]:
    """从接口拉回来的模型列表里，猜出「答题模型」和「读图模型」。

    优先级：服务商预设点名的模型 → 名字带视觉特征的 → 第一个可用模型。
    """
    if not ids:
        return "", ""
    preset = PROVIDER_PRESETS.get(provider) or {}
    text = preset.get("model") if preset.get("model") in ids else ""
    vision = preset.get("vision_model") if preset.get("vision_model") in ids else ""

    if not vision:
        vision = next((item for item in ids if any(hint in item.lower() for hint in VISION_HINTS)), "")
    if not text:
        text = next((item for item in ids if item != vision and not any(h in item.lower() for h in VISION_HINTS)), ids[0])
    if not vision:
        vision = text  # 兜底：只有一个模型时，答题读图都用它
    return text, vision


def load_llm_from_config(config: dict[str, Any], logger: Callable[[str], None] = print) -> LlmHelper:
    """按 provider 预设 + 显式配置合成一个 LlmHelper。"""
    from .settings import resolve_llm

    resolved = resolve_llm(config)
    return LlmHelper(
        url=resolved["url"],
        key=resolved["key"],
        model=resolved["model"],
        provider=resolved["provider"],
        thinking=resolved["thinking"],
        vision_model=resolved["vision_model"],
        logger=logger,
    )


def preset_choices() -> list[tuple[str, str]]:
    """给界面用的下拉项：[(内部标识, 中文名)]。"""
    return [(key, str(value.get("short") or value.get("label") or key)) for key, value in PROVIDER_PRESETS.items()]


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
