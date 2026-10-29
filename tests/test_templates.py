# -*- coding: utf-8 -*-
"""结构性题型模板引擎测试。

这层存在的意义：把「能算出来的题」从大模型手里拿回来。
补全单词题服务端白送了前缀（`w_tip`）和长度（`w_lens`），
当前任务词表就是候选池 —— 交叉一下答案基本唯一，比让模型自由生成可靠得多。

测试用的题干结构全部取自真实题目（见 docs/测试任务中断排查与修复.txt 与
运行时落盘的 failed_fill.jsonl），不是自己编的样例。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo import templates  # noqa: E402
from vocabgo.bank import Bank  # noqa: E402
from vocabgo.matcher import topic_key  # noqa: E402
from vocabgo.solver import Solver  # noqa: E402


class SpellingTest(unittest.TestCase):
    """mode 73：多空首字母，必须提交 JSON 数组。"""

    VOCAB = ["zero", "waste", "green", "stock", "festival", "utilization", "conservation"]

    def test_two_prefix_slots(self) -> None:
        topic = {
            "topic_mode": 73,
            "stem": {"content": "We must aim for {ze} {wa} in daily life.", "remark": "生活中要做到零浪费"},
            "w_lens": [4, 5],
        }
        result = templates.solve_spelling(topic, self.VOCAB)
        self.assertEqual(json.loads(result or "[]"), ["zero", "waste"])

    def test_fixed_words_are_excluded(self) -> None:
        """`green {gr}{sto}` 里 green 是题干固定词，`{gr}` 不能命中它。

        这是社区版本踩过的坑：不排除固定词，`{gr}` 会匹配到 green，整题直接判错。
        growth 不在词表里 → 严格与宽松都算不出 → 必须返回 None（绝不硬猜 green）。
        """
        topic = {
            "topic_mode": 73,
            "stem": {"content": "green {gr}{sto}", "remark": "绿色增长与绿色库存"},
            "w_lens": [6, 5],
        }
        self.assertIsNone(templates.solve_spelling(topic, self.VOCAB))

    def test_fixed_words_excluded_but_real_answer_found(self) -> None:
        """同样的固定词场景，词表里真有答案时要能算出来（而不是被 green 抢走）。"""
        topic = {
            "topic_mode": 73,
            "stem": {"content": "green {gr}{sto}", "remark": "绿色增长与绿色库存"},
            "w_lens": [6, 5],
        }
        vocab = ["green", "growth", "stock", "zero"]
        picked = json.loads(templates.solve_spelling(topic, vocab) or "[]")
        self.assertEqual(picked, ["growth", "stock"])

    def test_loose_length_when_strict_fails(self) -> None:
        """长度对不上时放宽再试 —— 宁可交一个可能错但格式对的，也别跳过。"""
        topic = {
            "topic_mode": 73,
            "stem": {"content": "{res} shortage", "remark": "资源短缺"},
            "w_lens": [8],  # 真实长度 9（resources），严格模式匹配不到
        }
        result = templates.solve_spelling(topic, ["resources"])
        self.assertEqual(json.loads(result or "[]"), ["resources"])

    def test_no_prefix_uses_option_pool(self) -> None:
        topic = {
            "topic_mode": 73,
            "stem": {"content": "{} and {}", "remark": "零和浪费"},
            "options": [{"content": "zero"}, {"content": "waste"}, {"content": "green"}],
            "w_lens": [],
        }
        result = templates.solve_spelling(topic, self.VOCAB)
        picked = json.loads(result or "[]")
        self.assertEqual(len(picked), 2)
        self.assertTrue(all(word.lower() in {"zero", "waste"} for word in picked))

    def test_empty_vocabulary_returns_none(self) -> None:
        topic = {"topic_mode": 73, "stem": {"content": "{ze}", "remark": "零"}, "w_lens": [4]}
        self.assertIsNone(templates.solve_spelling(topic, []))


class FillWordTest(unittest.TestCase):
    """mode 51~54：单个补全单词。"""

    VOCAB = ["struggle", "convention", "conventions", "adopt", "brand", "chorus"]

    def test_prefix_and_length_pin_the_answer(self) -> None:
        topic = {
            "topic_mode": 51,
            "stem": {"content": "{} with", "remark": "与…斗争"},
            "w_tip": "str",
            "w_lens": [8],
        }
        self.assertEqual(templates.solve_fill_word(topic, self.VOCAB), "struggle")

    def test_prefix_without_length_still_works(self) -> None:
        topic = {
            "topic_mode": 51,
            "stem": {"content": "a school {}", "remark": "校合唱团"},
            "w_tip": "cho",
            "w_lens": [],
        }
        self.assertEqual(templates.solve_fill_word(topic, self.VOCAB), "chorus")

    def test_plural_form_is_reachable(self) -> None:
        """词表给的是原形，题目要复数 —— 词形扩展要能补出来。"""
        topic = {
            "topic_mode": 51,
            "stem": {"content": "break the {}", "remark": "打破惯例"},
            "w_tip": "conven",
            "w_lens": [11],  # conventions
        }
        self.assertEqual(templates.solve_fill_word(topic, self.VOCAB), "conventions")

    def test_no_tip_means_no_rule(self) -> None:
        """没有前缀提示就没有线索，必须交回大模型，不能瞎猜。"""
        topic = {"topic_mode": 51, "stem": {"content": "a {} day", "remark": "美好的一天"}, "w_lens": [8]}
        self.assertIsNone(templates.solve_fill_word(topic, self.VOCAB))


class SentenceChoiceTest(unittest.TestCase):
    """mode 41~44：从选项里挑词补全句子。"""

    def test_remark_overlaps_definition(self) -> None:
        word_defs = {
            "conscious": ["adj. 意识到的；神志清醒的"],
            "manufacturing": ["n. 制造业"],
            "green": ["adj. 绿色的"],
        }
        topic = {
            "topic_mode": 42,
            "stem": {"content": "environmentally {} manufacturing", "remark": "环保意识强的制造业"},
            "options": [
                {"content": "green", "answer_tag": 0},
                {"content": "conscious", "answer_tag": 1},
                {"content": "manufacturing", "answer_tag": 2},
            ],
        }
        # 题干里 manufacturing 是固定词，不能被选中
        self.assertEqual(templates.solve_sentence_choice(topic, word_defs), 1)

    def test_no_defs_returns_none(self) -> None:
        topic = {
            "topic_mode": 42,
            "stem": {"content": "a {} plan", "remark": "一个计划"},
            "options": [{"content": "vague", "answer_tag": 0}],
        }
        self.assertIsNone(templates.solve_sentence_choice(topic, {}))


class MeanToWordTest(unittest.TestCase):
    """mode 17/18：题干中文，选项英文。"""

    def test_reverse_lookup(self) -> None:
        word_defs = {
            "sponsor": ["vt. 赞助；发起"],
            "chip": ["n. 芯片"],
        }
        topic = {
            "topic_mode": 17,
            "stem": {"content": "赞助；发起"},
            "options": [
                {"content": "chip", "answer_tag": 0},
                {"content": "sponsor", "answer_tag": 1},
            ],
        }
        self.assertEqual(templates.solve_mean_to_word(topic, word_defs), 1)


class DefaultAnswerShapeTest(unittest.TestCase):
    """算不出答案时，至少要把**提交格式**摆对。"""

    def test_spelling_default_is_json_array_of_right_length(self) -> None:
        topic = {"topic_mode": 73, "stem": {"content": "{ze} {wa}", "w_lens": [4, 5]}}
        shaped = templates.default_answer(topic)
        self.assertEqual(len(json.loads(shaped)), 2, "空位数必须和题干一致，否则格式错必然判零")

    def test_sentence_default_avoids_stop_words(self) -> None:
        topic = {
            "topic_mode": 42,
            "stem": {"content": "{} importance", "remark": "重要意义"},
            "options": [
                {"content": "the", "answer_tag": 0},
                {"content": "vital", "answer_tag": 1},
            ],
        }
        self.assertEqual(templates.default_answer(topic), 1, "别把 the 这种虚词当答案")


class SolverIntegrationTest(unittest.TestCase):
    """接进 Solver：模板命中要计入统计，并且写进题库供下次复用。"""

    class FakeClient:
        def __init__(self) -> None:
            self.verified: list = []
            self.submitted = 0

        def verify(self, code, answer):
            self.verified.append(answer)
            return {"data": {"topic_code": code, "answer_result": 1}}

        def submit(self, code, spent):
            self.submitted += 1
            return {"data": {"topic_code": code}}

    def _solver(self, client, vocabulary):
        folder = Path(tempfile.mkdtemp())
        return Solver(
            client,  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            kind="class",
            probe=False,
            vocabulary=vocabulary,
            pace=(0.0, 0.0),
            logger=lambda _m: None,
        )

    def test_spelling_answered_by_template_not_llm(self) -> None:
        client = self.FakeClient()
        solver = self._solver(client, ["zero", "waste"])
        topic = {
            "topic_code": "c1",
            "topic_mode": 73,
            "stem": {"content": "{ze} {wa}", "remark": "零浪费"},
            "w_lens": [4, 5],
        }
        solver._solve_one(topic)  # noqa: SLF001

        self.assertEqual(len(client.verified), 1)
        self.assertEqual(json.loads(client.verified[0]), ["zero", "waste"])
        self.assertEqual(solver.stats.template, 1)
        self.assertEqual(client.submitted, 1)
        # 答案要落进题库，下次同题直接命中
        self.assertEqual(len(solver.bank), 1)

    def test_fill_word_answered_by_template(self) -> None:
        client = self.FakeClient()
        solver = self._solver(client, ["struggle"])
        topic = {
            "topic_code": "c2",
            "topic_mode": 51,
            "stem": {"content": "{} with", "remark": "与…斗争"},
            "w_tip": "str",
            "w_lens": [8],
        }
        solver._solve_one(topic)  # noqa: SLF001
        self.assertEqual(client.verified, ["struggle"])
        self.assertEqual(solver.stats.template, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
