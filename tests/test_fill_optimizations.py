# -*- coding: utf-8 -*-
"""mode=51 填空优化：候选约束、固定搭配裁剪和失败样本。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vocabgo.bank import Bank
from vocabgo.glossary import Glossary
from vocabgo.llm import LlmHelper
from vocabgo.solver import Solver


class _Client:
    pass


class _Verify2Client:
    def verify(self, code, answer):
        return {
            "data": {
                "answer_result": 2,
                "over_status": 2,
                "answer_corrects": [],
                "topic_code": code,
            }
        }


class _CorrectsClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, code, answer):
        self.calls.append(str(answer))
        if str(answer) == "orbit the sun":
            return {"data": {"answer_result": 1, "over_status": 1, "topic_code": code}}
        return {
            "data": {
                "answer_result": 2,
                "over_status": 1,
                "answer_corrects": ["orbit the sun　"],
                "topic_code": code,
            }
        }


class _FormClient:
    def __init__(self) -> None:
        self.verified: list[str] = []

    def verify(self, code, answer):
        self.verified.append(str(answer))
        ok = str(answer) == "conventions"
        return {
            "data": {
                "answer_result": 1 if ok else 2,
                "over_status": 1,
                "answer_corrects": [],
                "topic_code": code,
            }
        }

    def submit(self, code, spent):
        return {"data": {"topic_code": code}}


class _LockedPhraseClient:
    def __init__(self) -> None:
        self.verified: list[str] = []

    def verify(self, code, answer):
        self.verified.append(str(answer))
        return {
            "data": {
                "answer_result": 2,
                "over_status": 1,
                "answer_corrects": ["orbit the sun　"],
                "topic_code": code,
            }
        }

    def submit(self, code, spent):
        return {"data": {"topic_code": code}}


class _FakeLlm:
    enabled = True
    auth_failed = False
    last_fill_samples = [
        {"sample": 1, "raw": "in batches", "parsed": "batches"},
        {"sample": 2, "raw": "in batches", "parsed": "batches"},
    ]
    last_phrase_samples = [
        {"sample": 1, "raw": "violate sb.'s privacy", "parsed": "violate,sb.'s,privacy"},
    ]


def fill_topic() -> dict:
    return {
        "topic_mode": 51,
        "topic_code": "fill-1",
        "stem": {"content": "in {}", "remark": "分批次"},
        "options": [],
    }


class FillOptimizationTest(unittest.TestCase):
    def make_solver(self, folder: Path, *, vocabulary=("batch",)) -> Solver:
        glossary = Glossary(folder / "glossary.json", logger=lambda _message: None)
        glossary._cache["@zh:分批次"] = ["in batches", "batch", "feasible"]  # noqa: SLF001
        return Solver(
            _Client(),  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            glossary=glossary,
            vocabulary=vocabulary,
            fill_fail_path=folder / "failed_fill.jsonl",
            probe=False,
            logger=lambda _message: None,
        )

    def test_candidate_hint_uses_task_vocabulary_and_strips_fixed_words(self) -> None:
        folder = Path(tempfile.mkdtemp())
        solver = self.make_solver(folder)

        hint = solver._fill_candidate_hint(fill_topic())  # noqa: SLF001

        self.assertIn("batches", hint)
        self.assertIn("batch", hint)
        self.assertNotIn("feasible", hint, "与任务词表无关的候选不应进入提示词")

    def test_fill_prompt_keeps_candidates_and_drops_unrelated_word_defs(self) -> None:
        prompt = LlmHelper.build_prompt(
            fill_topic(),
            {"unrelated": ["不应出现的旧释义"]},
            fill_candidates="1. batches\n2. batch",
            include_word_defs=False,
        )

        self.assertIn("当前任务词表与相关候选", prompt)
        self.assertIn("batches", prompt)
        self.assertNotIn("unrelated", prompt)
        self.assertNotIn("不应出现的旧释义", prompt)

    def test_candidate_hint_includes_vocabulary_even_when_translation_is_a_synonym(self) -> None:
        folder = Path(tempfile.mkdtemp())
        glossary = Glossary(folder / "glossary.json", logger=lambda _message: None)
        glossary._cache["@zh:与…斗争"] = ["struggle", "fight"]  # noqa: SLF001
        solver = Solver(
            _Client(),  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            glossary=glossary,
            vocabulary=("contend", "chorus", "stack", "submit"),
            fill_fail_path=folder / "failed_fill.jsonl",
            probe=False,
            logger=lambda _message: None,
        )
        topic = {
            "topic_mode": 51,
            "stem": {"content": "{} with", "remark": "与…斗争"},
            "options": [],
        }

        hint = solver._fill_candidate_hint(topic)  # noqa: SLF001

        self.assertIn("当前任务词表", hint)
        self.assertIn("contend", hint)

    def test_ask_fill_strips_fixed_phrase_and_keeps_raw_trace(self) -> None:
        helper = LlmHelper(url="https://x", key="k", model="m", logger=lambda _message: None)
        with patch.object(LlmHelper, "_chat", side_effect=["in batches", "in batches", "batch"]):
            result = helper.ask_fill(
                fill_topic(),
                {},
                fill_candidates="1. batches\n2. batch",
                votes=3,
            )

        self.assertEqual(result, "batches")
        self.assertEqual(len(helper.last_fill_samples), 2)
        self.assertEqual(helper.last_fill_samples[0]["raw"], "in batches")
        self.assertEqual(helper.last_fill_samples[0]["parsed"], "batches")

    def test_failed_fill_is_written_as_jsonl(self) -> None:
        folder = Path(tempfile.mkdtemp())
        solver = self.make_solver(folder)
        solver.llm = _FakeLlm()  # type: ignore[assignment]

        solver._record_fill_failure(  # noqa: SLF001
            fill_topic(),
            51,
            "server_rejected",
            "batches",
            {"answer_result": 0, "answer_corrects": ["batch"]},
        )

        record = json.loads((folder / "failed_fill.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(record["mode"], 51)
        self.assertEqual(record["prompt_version"], 4)
        self.assertEqual(record["predicted"], "batches")
        self.assertEqual(record["reason"], "server_rejected")
        self.assertEqual(record["samples"][0]["raw"], "in batches")
        self.assertEqual(record["server"]["answer_corrects"], ["batch"])

    def test_phrase_fallback_places_sb_last(self) -> None:
        folder = Path(tempfile.mkdtemp())
        solver = self.make_solver(folder)
        topic = {
            "topic_mode": 32,
            "stem": {"content": "___ ___", "remark": "引用某人的话"},
            "options": [{"content": "sb."}, {"content": "quote"}],
        }

        self.assertEqual(solver._default_answer(topic), "quote,sb.")  # noqa: SLF001

    def test_phrase_failure_records_raw_samples(self) -> None:
        folder = Path(tempfile.mkdtemp())
        solver = self.make_solver(folder)
        solver.llm = _FakeLlm()  # type: ignore[assignment]
        topic = {
            "topic_mode": 32,
            "topic_code": "phrase-1",
            "stem": {"content": "___ ___ ___", "remark": "侵犯某人的隐私"},
            "options": [{"content": "violate"}, {"content": "sb.'s"}, {"content": "privacy"}],
        }

        solver._record_fill_failure(  # noqa: SLF001
            topic,
            32,
            "server_rejected",
            "violate,sb.'s,privacy",
            {"answer_result": 2, "over_status": 2},
        )

        record = json.loads((folder / "failed_fill.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(record["mode"], 32)
        self.assertEqual(record["options"], ["violate", "sb.'s", "privacy"])
        self.assertEqual(record["samples"][0]["raw"], "violate sb.'s privacy")

    def test_answer_result_2_is_not_treated_as_success(self) -> None:
        folder = Path(tempfile.mkdtemp())
        solver = Solver(
            _Verify2Client(),  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            probe=False,
            logger=lambda _message: None,
        )
        topic = {
            "topic_mode": 51,
            "stem": {"content": "social {}", "remark": "社会习俗"},
            "options": [],
        }

        _code, ok, data = solver._answer(topic, "c1", "convention")  # noqa: SLF001

        self.assertFalse(ok, "answer_result=2 不能直接当成正确，否则错词不会继续重刷")
        self.assertEqual(data["answer_result"], 2)

    def test_answer_corrects_tries_space_format_before_comma_format(self) -> None:
        folder = Path(tempfile.mkdtemp())
        client = _CorrectsClient()
        solver = Solver(
            client,  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            probe=False,
            logger=lambda _message: None,
        )
        topic = {
            "topic_mode": 32,
            "stem": {"content": "_  _  _", "remark": "绕太阳旋转"},
            "options": [{"content": "sun　"}, {"content": "the"}, {"content": "orbit"}],
        }

        _code, ok, _data = solver._answer(topic, "c1", "orbit,the,sun")  # noqa: SLF001

        self.assertTrue(ok)
        self.assertEqual(solver.last_accepted_answer, "orbit the sun")
        self.assertEqual(client.calls[:2], ["orbit,the,sun", "orbit the sun　"])

    def test_fill_word_forms_cover_common_plurals(self) -> None:
        self.assertEqual(Solver._fill_word_forms("convention"), ["conventions"])  # noqa: SLF001
        self.assertEqual(Solver._fill_word_forms("study"), ["studies"])  # noqa: SLF001
        self.assertEqual(Solver._fill_word_forms("watch"), ["watches"])  # noqa: SLF001

    def test_rejected_singular_retries_plural_form(self) -> None:
        class FakeLlm:
            enabled = True
            auth_failed = False

            def ask_fill(self, *_args, **_kwargs):
                return "convention"

            def ask_choice(self, *_args, **_kwargs):
                raise AssertionError("mode 51 不应调用选择题模型")

        folder = Path(tempfile.mkdtemp())
        client = _FormClient()
        solver = Solver(
            client,  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            llm=FakeLlm(),  # type: ignore[arg-type]
            vocabulary={"convention"},
            probe=False,
            logger=lambda _message: None,
        )
        topic = {
            "topic_mode": 51,
            "stem": {"content": "social {}", "remark": "社会习俗"},
            "options": [],
        }

        solver._solve_one(topic)  # noqa: SLF001

        self.assertEqual(client.verified, ["convention", "conventions"])

    def test_locked_phrase_corrects_are_cached_for_next_round(self) -> None:
        class FakeLlm:
            enabled = True
            auth_failed = False

            def ask_phrase(self, *_args, **_kwargs):
                return "orbit,the,sun"

            def ask_choice(self, *_args, **_kwargs):
                raise AssertionError("mode 32 不应调用选择题模型")

        folder = Path(tempfile.mkdtemp())
        bank = Bank(folder / "bank.json")
        solver = Solver(
            _LockedPhraseClient(),  # type: ignore[arg-type]
            bank,
            llm=FakeLlm(),  # type: ignore[arg-type]
            probe=False,
            logger=lambda _message: None,
        )
        topic = {
            "topic_mode": 32,
            "stem": {"content": "_  _  _", "remark": "绕太阳旋转"},
            "options": [{"content": "orbit"}, {"content": "the"}, {"content": "sun　"}],
        }

        solver._solve_one(topic)  # noqa: SLF001

        hits = bank.find_loose("32::norm::_ _ _::绕太阳旋转")
        self.assertTrue(hits)
        self.assertEqual(hits[0][1], "orbit the sun　")


if __name__ == "__main__":
    unittest.main(verbosity=2)
