# -*- coding: utf-8 -*-
"""题型模板引擎：把「结构性题型」变成可计算的答案，不靠大模型猜。

为什么要这一层
--------------
大模型对选择题还算稳，但遇到这几种题型基本靠运气，而这些题恰恰是**能算出来的**：

* **补全单词**（mode 51/52/53/54）：服务端给了前缀提示 `w_tip` 和单词长度 `w_lens`，
  当前任务的词表又是一份现成的候选池 —— 前缀 + 长度一交叉，答案几乎唯一。
  以前这类题直接丢给大模型自由生成，经常因为词形不对被判错。
* **多空首字母**（mode 73）：题干里是 `{gr}{sto}` 这种碎片前缀，`w_lens` 给出每空长度，
  答案要按 JSON 数组提交。以前完全没处理。
* **补全句子**（mode 41/42/43/44）：从选项里挑词填进 `{}`，中文备注已经说明要什么，
  用词表按释义反查比让模型自由想更准。
* **看义选词**（mode 17/18）：题干是中文释义，选项是英文词 —— 反过来查词表即可。

这一层的规则移植自社区维护得最勤的那份实现（Easy_Cidaren）的题型分发，
按本项目的接口重新写：只吃 `topic` 字典，不依赖任何全局状态，纯函数、可单测。

设计原则
--------
1. **算不出来就返回 None**，绝不硬猜 —— 交给后面的题库 / 大模型 / 试答案链路。
2. 所有「候选池」都来自**当前任务词表**，不是自由生成，所以答案一定在考纲内。
3. 题干里已经出现的固定词必须排除，否则 `green {gr}{sto}` 会把 green 当成答案。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .matcher import (
    norm,
    topic_mode,
    topic_options,
    topic_remark,
    topic_stem,
)

# 补全单词类题型：题干是句子，空格处按前缀 + 长度定位
FILL_MODES = {51, 52, 53, 54}
# 补全句子类题型：从选项池里挑一个词填进 {}
SENTENCE_MODES = {41, 42, 43, 44}
# 多空首字母题：提交 JSON 数组
SPELLING_MODE = 73
# 看义选词：题干是中文释义，选项是英文词
MEAN_TO_WORD_MODES = {17, 18}

# 题干里的花括号碎片：{gr} / {sto} / {}
TIP_TOKEN = re.compile(r"\{([^{}]*)\}")
# 题干固定词（用来排除误把固定词当答案）
WORD_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*")

# 虚词表：这些词几乎不会是答案，兜底排序时靠后
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "we", "you", "they", "he", "she",
    "not", "no", "so", "if", "than", "then", "there", "here", "what", "which",
}


def topic_tips(topic: dict[str, Any]) -> list[str]:
    """题干里的前缀提示。

    有两个来源，按优先级：
      1. 顶层 `w_tip` —— mode 51~54 的补全单词题把前缀单独放在这里，
         此时题干长这样：`"{}  with"`，花括号里是**空的**；
      2. 题干花括号里的内容 —— mode 73 的多空首字母题写成 `{gr}{sto}`。

    两者都取不到就返回空列表（没有线索，不该硬猜）。
    """
    tip = str(topic.get("w_tip") or "").strip()
    if tip:
        return [tip]
    return TIP_TOKEN.findall(topic_stem(topic))


def topic_word_lens(topic: dict[str, Any]) -> list[int]:
    """每空的目标词长；服务端字段名是 w_lens（列表），缺省时退回 w_len（单值）。"""
    raw = topic.get("w_lens")
    out: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                out.append(0)
    if not out:
        try:
            single = int(topic.get("w_len") or 0)
        except (TypeError, ValueError):
            single = 0
        if single:
            out = [single]
    return out


def split_words(text: Any) -> list[str]:
    """把短语拆成单词池：按空白 / 连字符 / 括号 / 逗号 / 斜杠切。"""
    return [piece for piece in re.split(r"[\s\-(),/，；;]+", str(text or "")) if piece]


def fixed_words(topic: dict[str, Any]) -> set[str]:
    """题干里已经出现的固定词（小写）。

    必须排除：`green {gr}{sto}` 里 green 是固定词，`{gr}` 会误命中它；
    `Lucid waters and {an}...` 里 and 也会被 `{an}` 误命中。
    """
    stem = TIP_TOKEN.sub(" ", topic_stem(topic))
    return {word.lower() for word in WORD_TOKEN.findall(stem)}


def vocabulary_pool(vocabulary: Iterable[str]) -> list[str]:
    """任务词表 → 去重保序的候选词池（保持原形，大小写按服务端给的）。"""
    pool: list[str] = []
    seen: set[str] = set()
    for item in vocabulary or ():
        for piece in split_words(item):
            lowered = piece.lower()
            if lowered and lowered not in seen:
                seen.add(lowered)
                pool.append(piece)
    return pool


def expand_vocabulary(pool: list[str]) -> list[str]:
    """词表补全：词表里是原形，题目却可能要复数/时态。

    只做**可逆的**几种变化，不做词性推导 —— 补出来的东西仍然要能在服务端判对。

    返回的每一项都是 `(单词, 原形)` 二元组：**必须带着原形走**。
    否则固定词排除会失效 —— 踩过的坑：`green {gr}{sto}` 里 green 是题干固定词，
    但词形扩展生出 `greens`，一查 `greens not in {green}` 就绕过排除，
    把题干里本来就有的一词塞进答案，整题直接判错。
    """
    out: list[tuple[str, str]] = [(word, word.lower()) for word in pool]
    seen = {word.lower() for word in pool}
    for word in pool:
        lowered = word.lower()
        variants: list[str] = []
        if lowered.endswith("y") and len(lowered) > 2 and lowered[-2] not in "aeiou":
            variants.append(word[:-1] + "ies")
        if lowered.endswith(("s", "x", "z", "ch", "sh")):
            variants.append(word + "es")
        else:
            variants.append(word + "s")
        if lowered.endswith("e"):
            variants.append(word[:-1] + "ing")
        else:
            variants.append(word + "ing")
        variants.append(word + "ed")
        for variant in variants:
            if variant.lower() not in seen:
                seen.add(variant.lower())
                out.append((variant, lowered))
    return out


def match_prefix(tips: list[str], word_lens: list[int], pool: list[str] | list[tuple[str, str]], *, strict_len: bool = True, exclude: set[str] | None = None) -> list[str] | None:
    """按前缀（和长度）在候选池里挑词；全部空位都能命中才返回。

    候选池既接受纯字符串，也接受 `(单词, 原形)` 二元组 —— 后者用来做固定词排除：
    只要**原形**是题干固定词，它就一定不是答案。
    """
    exclude = exclude or set()
    answers: list[str] = []
    used: set[str] = set()
    for index, tip in enumerate(tips):
        want_len = word_lens[index] if index < len(word_lens) else 0
        hit = None
        for item in pool:
            word, base = item if isinstance(item, tuple) else (item, item.lower())
            lowered = word.lower()
            if base in exclude or lowered in exclude or lowered in used:
                continue
            if not lowered.startswith(tip.lower()):
                continue
            if strict_len and want_len and len(word) != want_len:
                continue
            hit = word
            break
        if hit is None:
            return None
        answers.append(hit)
        used.add(hit.lower())
    return answers


def solve_spelling(topic: dict[str, Any], vocabulary: Iterable[str]) -> str | None:
    """mode 73：多空首字母补全，提交 JSON 数组字符串。

    服务端要的就是 `["zero","waste"]` 这种形式，直接给列表会被判格式错。
    """
    if topic_mode(topic) != SPELLING_MODE:
        return None
    tips = topic_tips(topic)
    word_lens = topic_word_lens(topic)
    exclude = fixed_words(topic)
    base = vocabulary_pool(vocabulary)
    if not base:
        return None

    if not tips or all(not tip for tip in tips):
        # 无前缀空位：只能靠选项池交叉。选项里出现过的词才可信。
        contents = [
            str(opt.get("content") or "").strip()
            for opt in topic_options(topic)
            if opt.get("content")
        ]
        pool_lower = {item.lower() for item in contents}
        picked = [word for word in base if word.lower() in pool_lower and word.lower() not in exclude]
        if not picked:
            return None
        slots = topic_stem(topic).count("{}") or len(tips) or len(picked)
        return json.dumps(picked[:slots], ensure_ascii=False)

    # 严格长度 → 宽松长度 → 词形扩展后再试。逐级放宽，能算出就不放弃。
    # expand_vocabulary 返回 (变体, 原形)，固定词排除才能穿透到变体上。
    for pool in (base, expand_vocabulary(base)):
        for strict in (True, False):
            hit = match_prefix(tips, word_lens, pool, strict_len=strict, exclude=exclude)
            if hit:
                return json.dumps(hit, ensure_ascii=False)
    return None


def solve_fill_word(topic: dict[str, Any], vocabulary: Iterable[str]) -> str | None:
    """mode 51~54：补全单词（单空），给普通字符串。

    依据是服务端白送的 `w_tip`（前缀）和 `w_lens`（长度）—— 这比让大模型生成可靠得多，
    而且算出来的答案一定在当前任务词表里，不会跑出考纲。
    """
    if topic_mode(topic) not in FILL_MODES:
        return None
    tips = [tip for tip in topic_tips(topic) if tip]
    word_lens = topic_word_lens(topic)
    if not tips:
        return None  # 没有前缀提示，交给大模型
    exclude = fixed_words(topic)
    base = vocabulary_pool(vocabulary)
    if not base:
        return None

    for pool in (base, expand_vocabulary(base)):
        for strict in (True, False):
            hit = match_prefix(tips[:1], word_lens, pool, strict_len=strict, exclude=exclude)
            if hit:
                return hit[0]
    return None


def _cjk_overlap(definition: str, remark: str) -> int:
    """中文重叠度：释义里的汉字有多少比例出现在备注里。

    为什么不用「子串包含」：释义是「意识到的」，备注写「环保意识强的制造业」——
    「意识到的」不是「环保意识强的制造业」的子串，但显然说的是一件事。
    按字重叠就稳得多，而且不会因为多一个「的」字就全盘作废。

    只算汉字，标点和虚字先剔掉，避免「的」「使」这种把分数刷高。
    """
    def cjk(text: str) -> list[str]:
        return [ch for ch in str(text or "") if "\u4e00" <= ch <= "\u9fff" and ch not in "的了地得使"]

    units = cjk(definition)
    if len(units) < 2:
        return 0
    pool = set(cjk(remark))
    if not pool:
        return 0
    hit = sum(1 for ch in units if ch in pool)
    ratio = hit / len(units)
    # 命中比例够高才算数；比例低说明只是碰巧共用了几个常用字
    return hit if ratio >= 0.6 else 0


def solve_sentence_choice(topic: dict[str, Any], word_defs: dict[str, list[str]]) -> Any | None:
    """mode 41~44：选一个词补全句子。

    中文备注说明了要填什么含义，所以：把「备注」和「mode=0 学到的释义」做重叠比对，
    命中的选项直接提交。比对保守 —— 不够确定就返回 None。

    注意排除题干固定词：`environmentally {} manufacturing`（备注「环保意识强的制造业」）
    里 manufacturing 是题干原有的词，释义「制造业」虽然和备注重叠，但它绝不该是答案。
    """
    if topic_mode(topic) not in SENTENCE_MODES:
        return None
    options = topic_options(topic)
    if not options:
        return None
    remark = str(topic_remark(topic) or "").strip()
    if not remark:
        return None

    stem_fixed = fixed_words(topic)
    best_tag: Any = None
    best_score = 0
    for opt in options:
        content = str(opt.get("content") or "").strip()
        if not content or content.lower() in stem_fixed:
            continue
        score = 0
        for definition in word_defs.get(content.lower()) or []:
            core = re.sub(r"^[a-z]+\.\s*", "", str(definition))
            for piece in re.split(r"[；;，,、/]", core):
                score = max(score, _cjk_overlap(piece, remark))
        if score > best_score:
            best_score, best_tag = score, opt.get("answer_tag")
    if best_tag is None:
        return None
    # 至少命中两个汉字才算数，避免「使」「的」这种字误判
    return best_tag if best_score >= 2 else None


def solve_mean_to_word(topic: dict[str, Any], word_defs: dict[str, list[str]]) -> Any | None:
    """mode 17/18：题干是中文释义，选项是英文词 —— 反查释义表。"""
    if topic_mode(topic) not in MEAN_TO_WORD_MODES:
        return None
    options = topic_options(topic)
    if not options:
        return None
    target = norm(topic_stem(topic))
    if not target:
        return None

    for opt in options:
        word = str(opt.get("content") or "").strip().lower()
        for definition in word_defs.get(word) or []:
            core = re.sub(r"^[a-z]+\.\s*", "", str(definition))
            for piece in re.split(r"[；;，,、/]", core):
                piece = norm(piece)
                if not piece or len(piece) < 2:
                    continue
                if piece == target or piece in target or target in piece:
                    return opt.get("answer_tag")
    return None


def default_answer(topic: dict[str, Any]) -> Any:
    """实在没线索时的兜底作答：不保证对，只保证流程往前走。

    比原来的「永远选第 0 项」好一点的地方：多空首字母题会按空位数拼出等长的数组，
    格式对、只是词可能错 —— 而格式错是必然判错的。
    """
    mode = topic_mode(topic)
    options = topic_options(topic)

    if mode == SPELLING_MODE:
        slots = len([tip for tip in topic_tips(topic)]) or 1
        return json.dumps([""] * slots, ensure_ascii=False)

    if not options:
        return ""

    if mode in SENTENCE_MODES:
        # 补全句子：优先挑实词，别填 the / and 这类东西
        ranked = sorted(
            options,
            key=lambda opt: (
                1 if str(opt.get("content") or "").strip().lower() in STOP_WORDS else 0,
                -len(str(opt.get("content") or "")),
            ),
        )
        return ranked[0].get("answer_tag", 0)

    return options[0].get("answer_tag", 0)
