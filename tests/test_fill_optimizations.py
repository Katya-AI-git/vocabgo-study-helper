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


if __name__ == "__main__":
    unittest.main(verbosity=2)
