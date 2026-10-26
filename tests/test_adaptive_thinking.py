# -*- coding: utf-8 -*-
"""自适应思考模式：基础题先省，无稳定结果再升级。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vocabgo.bank import Bank
from vocabgo.solver import Solver


class _Client:
    pass


class _FakeLlm:
    enabled = True
    auth_failed = False

    def __init__(self, choice_results=None, phrase_results=None) -> None:
        self.choice_results = list(choice_results or [])
        self.phrase_results = list(phrase_results or [])
        self.calls: list[tuple[str, str, int]] = []

    def ask_choice(self, _topic, _defs, _hint="", material_hint="", *, votes=1, thinking=None):
        del material_hint
        self.calls.append(("choice", str(thinking), votes))
        return self.choice_results.pop(0) if self.choice_results else None

    def ask_phrase(self, _topic, _defs, _hint="", material_hint="", *, votes=1, thinking=None):
        del material_hint
        self.calls.append(("phrase", str(thinking), votes))
        return self.phrase_results.pop(0) if self.phrase_results else None

    def ask_fill(
        self,
        _topic,
        _defs,
        _hint="",
        material_hint="",
        *,
        fill_candidates="",
        votes=1,
        thinking=None,
    ):
        del material_hint
        del fill_candidates
        self.calls.append(("fill", str(thinking), votes))
        return None


def choice_topic(mode: int = 31) -> dict:
    return {
        "topic_mode": mode,
        "stem": {"content": "ubiquitous" if mode == 31 else "It is {ubiquitous}."},
        "options": [
            {"content": "罕见的", "answer_tag": 0},
            {"content": "普遍存在的", "answer_tag": 1},
        ],
    }


class AdaptiveThinkingTest(unittest.TestCase):
    def make_solver(self, llm: _FakeLlm) -> Solver:
        folder = Path(tempfile.mkdtemp())
        return Solver(
            _Client(),  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            llm=llm,  # type: ignore[arg-type]
            probe=False,
            llm_votes=3,
            adaptive_thinking=True,
            logger=lambda _message: None,
        )

    def test_basic_choice_starts_off_and_retries_on_with_one_vote(self) -> None:
        llm = _FakeLlm(choice_results=[None, 1])
        solver = self.make_solver(llm)
        result = solver._ask_llm(choice_topic(), 31, "", "")  # noqa: SLF001

        self.assertEqual(result, 1)
        self.assertEqual(llm.calls, [("choice", "off", 3), ("choice", "on", 1)])
        self.assertEqual(solver.stats.thinking_retry, 1)
        self.assertEqual(solver.stats.thinking_on, 1)

    def test_stable_off_result_does_not_retry(self) -> None:
        llm = _FakeLlm(choice_results=[1])
        solver = self.make_solver(llm)
        self.assertEqual(solver._ask_llm(choice_topic(), 31, "", ""), 1)  # noqa: SLF001
        self.assertEqual(llm.calls, [("choice", "off", 3)])
        self.assertEqual(solver.stats.thinking_retry, 0)

    def test_context_without_local_evidence_starts_on(self) -> None:
        llm = _FakeLlm(choice_results=[1])
        solver = self.make_solver(llm)
        self.assertEqual(solver._ask_llm(choice_topic(11), 11, "", ""), 1)  # noqa: SLF001
        self.assertEqual(llm.calls, [("choice", "on", 3)])
        self.assertEqual(solver.stats.thinking_on, 1)

    def test_context_with_dictionary_hint_can_start_off(self) -> None:
        llm = _FakeLlm(choice_results=[1])
        solver = self.make_solver(llm)
        self.assertEqual(
            solver._ask_llm(choice_topic(11), 11, "adj. 普遍存在的", ""),  # noqa: SLF001
            1,
        )
        self.assertEqual(llm.calls, [("choice", "off", 3)])

    def test_phrase_without_consensus_retries_on(self) -> None:
        llm = _FakeLlm(phrase_results=[None, "many,examples"])
        solver = self.make_solver(llm)
        topic = {
            "topic_mode": 32,
            "stem": {"content": "___ ___", "remark": "许多例子"},
            "options": [{"content": "many"}, {"content": "examples"}],
        }
        self.assertEqual(
            solver._ask_llm(topic, 32, "", ""),  # noqa: SLF001
            "many,examples",
        )
        self.assertEqual(llm.calls, [("phrase", "off", 3), ("phrase", "on", 1)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
