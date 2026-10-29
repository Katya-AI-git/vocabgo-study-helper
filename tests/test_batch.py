# -*- coding: utf-8 -*-
"""一键批量的任务筛选测试。"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.batch import (  # noqa: E402
    class_task_kind,
    pending_class_tasks,
    pending_study_tasks,
    rounds_for_task,
    score_of,
)
from vocabgo.client import VocabgoError  # noqa: E402
from vocabgo.gui import VocabgoApp  # noqa: E402


class BatchTaskFilterTest(unittest.TestCase):
    def test_score_parsing(self) -> None:
        self.assertEqual(score_of({"score": "9.5"}), 9.5)
        self.assertEqual(score_of({"score": ""}), 0.0)
        self.assertEqual(score_of({"score": "bad"}), 0.0)

    def test_pending_class_skips_unstarted_and_completed(self) -> None:
        records = [
            {"task_id": -1, "task_type": 1, "progress": 0, "score": 0},
            {"task_id": 1, "task_type": 1, "progress": 87, "score": 100},
            {"task_id": 2, "task_type": 1, "progress": 100, "score": 66.6},
            {"task_id": 3, "task_type": 2, "progress": 100, "score": 66.6},
        ]
        self.assertEqual(
            pending_class_tasks(records, 10),
            [records[1]],
            "默认只处理进度未满的班级任务",
        )
        self.assertEqual(
            pending_class_tasks(records, 10, include_completed=True),
            [records[1], records[2]],
            "显式允许重刷时才包含进度100%的班级学习任务，测试任务始终跳过",
        )

    def test_pending_study_keeps_uncreated_task(self) -> None:
        records = [
            {"task_id": -1, "score": 0},
            {"task_id": 3, "score": 99.9},
            {"task_id": 4, "score": 100},
        ]
        self.assertEqual(pending_study_tasks(records, 10), records[:2])

    def test_task_kind_and_rounds(self) -> None:
        learning_task = {"task_type": 1}
        test_task = {"task_type": 2}
        self.assertEqual(class_task_kind(learning_task), "learning")
        self.assertEqual(class_task_kind(test_task), "test")
        self.assertEqual(
            rounds_for_task("class", test_task, class_rounds=8, study_rounds=3), 1
        )
        self.assertEqual(
            rounds_for_task("class", learning_task, class_rounds=8, study_rounds=3), 8
        )
        self.assertEqual(rounds_for_task("study", {}, class_rounds=8, study_rounds=3), 3)


class ClassPreparationTest(unittest.TestCase):
    def test_prepare_uses_probe_and_stops_at_safe_coverage(self) -> None:
        class FakeClient:
            def chose_word_list(self, _task_id):
                return {"data": {"word_list": [{"word": "abandon"}, {"word": "benefit"}]}}

            def study_task_list(self, course_id):
                self.course_id = course_id
                return {
                    "data": {
                        "task_list": [
                            {
                                "task_name": "对应自学任务",
                                "course_id": course_id,
                                "list_id": "L1",
                                "task_id": 9,
                            }
                        ]
                    }
                }

        class FakeBank:
            def __init__(self):
                self.values = iter([0, 2])

            def cover_count(self, _names):
                return next(self.values)

        app = object.__new__(VocabgoApp)
        app.log = lambda _message: None
        app.cancel = threading.Event()
        probe_flags: list[bool | None] = []
        runs: list[str] = []

        def make_solver(_client, _bank, _kind, _config, *, force_probe=None):
            probe_flags.append(force_probe)
            return SimpleNamespace()

        def run_once(_client, _solver, _task, kind, _config, _max_score, _bank, **_kwargs):
            runs.append(kind)
            return SimpleNamespace(cancelled=False)

        app._make_solver = make_solver  # type: ignore[method-assign]
        app._run_once = run_once  # type: ignore[method-assign]
        config = {
            "study": {"course_id": "CET4_v2"},
            "runtime": {
                "prepare_class": True,
                "prepare_rounds": 3,
                "class_min_coverage": 85,
            },
        }
        chosen = {
            "task_id": 1,
            "course_id": "CET4_v2",
            "list_id": "L1",
            "task_name": "班级任务",
        }
        coverage = app._prepare_for_class(  # noqa: SLF001
            FakeClient(), chosen, config, 10, FakeBank()
        )
        self.assertEqual(coverage, 100)
        self.assertEqual(probe_flags, [True], "备题必须强制开启试答案，才能真正补齐题库")
        self.assertEqual(runs, ["study"])


class BatchOrchestrationTest(unittest.TestCase):
    def test_run_all_processes_only_unfinished_class_tasks(self) -> None:
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeClient:
            def page_task(self, **_kwargs):
                return {
                    "data": {
                        "records": [
                            {
                                "task_id": 1,
                                "task_type": 1,
                                "release_id": 11,
                                "task_name": "未完成",
                                "course_id": "CET4_v2",
                                "list_id": "L1",
                                "progress": 50,
                                "score": 80,
                            },
                            {
                                "task_id": 2,
                                "task_type": 2,
                                "release_id": 12,
                                "task_name": "已完成",
                                "course_id": "CET4_v2",
                                "list_id": "L2",
                                "progress": 100,
                                "score": 66.6,
                            },
                        ]
                    }
                }

        folder = Path(tempfile.mkdtemp())
        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.var_max_score = Var("10")
        app.var_repeat_completed = Var(False)
        app.var_class_loop = Var("1")
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app._refresh_status = lambda: None
        app.collect_config = lambda: {"runtime": {}}
        app._client = lambda: FakeClient()
        app._make_solver = lambda *_args, **_kwargs: SimpleNamespace(llm=None)
        runs: list[str] = []

        def run_once(_client, _solver, chosen, kind, *_args, **_kwargs):
            runs.append(f"{kind}:{chosen['task_name']}")
            return SimpleNamespace(cancelled=False)

        app._run_once = run_once  # type: ignore[method-assign]
        bank_path = folder / "bank.json"
        with patch("vocabgo.gui.default_bank_path", return_value=bank_path):
            app._do_run_all()  # noqa: SLF001

        self.assertEqual(
            runs,
            ["class:未完成"],
            "一键全刷应只处理进度未满的班级任务，不处理自学任务",
        )

    def test_class_run_reselects_and_continues_until_progress_100(self) -> None:
        class FakeClient:
            def __init__(self):
                self.submits = 0
                self.reset_flags = []
                self.reset_flags = []
                self.reset_flags = []

            def chose_word_list(self, _task_id):
                return {
                    "data": {
                        "word_list": [
                            {
                                "word": "retreat",
                                "score": 0,
                                "course_id": "CET4_v2",
                                "list_id": "CET4_v2_18",
                            }
                        ]
                    }
                }

            def submit_chose_word(self, _task_id, _word_map, **kwargs):
                self.submits += 1
                self.reset_flags.append(bool(kwargs.get("reset_chose_words")))
                return {"data": {}}

            def task_info(self, _task_id, _release_id):
                return {
                    "data": {
                        "progress": 50 if self.submits <= 1 else 100,
                        "exist_little_task": 2,
                    }
                }

            def signin(self):
                return {"data": {}}

        class FakeSolver:
            kind = "class"
            runs = 0

            def run(self, **_kwargs):
                self.runs += 1
                return SimpleNamespace(cancelled=False)

        class FakeBank:
            def __len__(self):
                return 1

            def cover_count(self, _names):
                return 1

        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        client = FakeClient()
        solver = FakeSolver()
        chosen = {"task_id": 1, "release_id": 2, "task_name": "List 18"}
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client,
                solver,
                chosen,
                "class",
                {"study": {}},
                10,
                FakeBank(),
            )
        self.assertEqual(client.submits, 2)
        self.assertEqual(
            client.reset_flags,
            [True, False],
            "首次开始可重置，自动续答不能再次重置",
        )
        self.assertEqual(solver.runs, 2)

    def test_selected_learning_task_uses_class_loop_for_session_count(self) -> None:
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeSolver:
            kind = "class"

        folder = Path(tempfile.mkdtemp())
        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.var_kind = Var("class")
        app.var_class_loop = Var("3")
        app.var_loop = Var("3")
        app.var_max_score = Var("10")
        app.tasks = [
            {
                "task_id": 1,
                "release_id": 2,
                "task_name": "学习任务",
                "task_type": 1,
                "progress": 50,
            }
        ]
        app.collect_config = lambda: {"runtime": {}}
        app._client = lambda: object()
        app._make_solver = lambda *_args, **_kwargs: FakeSolver()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app.progress = SimpleNamespace(configure=lambda **_kwargs: None)
        limits: list[int] = []

        def run_once(*_args, **kwargs):
            limits.append(int(kwargs["session_limit"]))
            return SimpleNamespace(cancelled=False)

        app._run_once = run_once  # type: ignore[method-assign]
        with (
            patch("vocabgo.gui.default_bank_path", return_value=folder / "bank.json"),
            patch("vocabgo.gui.save_settings"),
        ):
            app._do_run(0)  # noqa: SLF001

        self.assertEqual(limits, [1, 1, 1], "班级学习任务应由班级学习轮数控制，不再固定 6 会话")

    def test_selected_test_task_keeps_six_internal_sessions(self) -> None:
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeSolver:
            kind = "class"

        folder = Path(tempfile.mkdtemp())
        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.var_kind = Var("class")
        app.var_class_loop = Var("3")
        app.var_loop = Var("3")
        app.var_max_score = Var("10")
        app.tasks = [
            {
                "task_id": 1,
                "release_id": 2,
                "task_name": "测试任务",
                "task_type": 2,
                "progress": 50,
            }
        ]
        app.collect_config = lambda: {"runtime": {}}
        app._client = lambda: object()
        app._make_solver = lambda *_args, **_kwargs: FakeSolver()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app.progress = SimpleNamespace(configure=lambda **_kwargs: None)
        limits: list[int] = []

        def run_once(*_args, **kwargs):
            limits.append(int(kwargs["session_limit"]))
            return SimpleNamespace(cancelled=False)

        app._run_once = run_once  # type: ignore[method-assign]
        with (
            patch("vocabgo.gui.default_bank_path", return_value=folder / "bank.json"),
            patch("vocabgo.gui.save_settings"),
        ):
            app._do_run(0)  # noqa: SLF001

        self.assertEqual(limits, [6], "测试任务只有一次机会，仍应允许完成整套题的 6 个内部会话")

    def test_test_task_continues_when_progress_is_unchanged(self) -> None:
        class FakeClient:
            def __init__(self):
                self.submits = 0
                self.reset_flags = []

            def chose_word_list(self, _task_id):
                return {
                    "data": {
                        "word_list": [
                            {
                                "word": "orbit",
                                "score": 6.6,
                                "course_id": "CET4_v2",
                                "list_id": "CET4_v2_23",
                            }
                        ]
                    }
                }

            def submit_chose_word(self, _task_id, _word_map, **_kwargs):
                self.submits += 1
                self.reset_flags.append(bool(_kwargs.get("reset_chose_words")))
                return {"data": {}}

            def task_info(self, _task_id, _release_id):
                return {"data": {"progress": 66, "exist_little_task": 2}}

            def signin(self):
                return {"data": {}}

        class FakeSolver:
            kind = "class"

            def __init__(self):
                self.runs = 0

            def run(self, **_kwargs):
                self.runs += 1
                return SimpleNamespace(cancelled=False)

        class FakeBank:
            def __len__(self):
                return 0

            def cover_count(self, _names):
                return 0

        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app.progress = SimpleNamespace(configure=lambda **_kwargs: None)
        client = FakeClient()
        solver = FakeSolver()
        chosen = {
            "task_id": 1,
            "release_id": 2,
            "task_name": "测试任务",
            "task_type": 2,
            "progress": 66,
        }
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client,
                solver,
                chosen,
                "class",
                {"study": {}},
                10,
                FakeBank(),
                session_limit=3,
            )

        self.assertEqual(solver.runs, 3)
        self.assertEqual(client.submits, 3, "测试任务进度暂不增长时也不能提前停止")
        self.assertEqual(
            client.reset_flags,
            [True, False, False],
            "测试任务后续会话不应反复重置到第一题",
        )

    def test_test_task_continues_when_words_are_submitted_in_current_section(self) -> None:
        class FakeClient:
            def __init__(self):
                self.submits = 0
                self.reset_flags = []

            def chose_word_list(self, _task_id):
                return {
                    "data": {
                        "word_list": [
                            {
                                "word": "stack",
                                "score": 6.6,
                                "status": 1,
                                "course_id": "CET4_v2",
                                "list_id": "CET4_v2_23",
                            }
                        ]
                    }
                }

            def submit_chose_word(self, _task_id, _word_map, **kwargs):
                self.submits += 1
                self.reset_flags.append(bool(kwargs.get("reset_chose_words")))
                return {"data": {}}

            def task_info(self, _task_id, _release_id):
                return {"data": {"progress": 66, "exist_little_task": 1}}

            def signin(self):
                return {"data": {}}

        class FakeSolver:
            kind = "class"

            def __init__(self):
                self.runs = 0

            def run(self, **_kwargs):
                self.runs += 1
                return SimpleNamespace(cancelled=False)

        class FakeBank:
            def __len__(self):
                return 0

            def cover_count(self, _names):
                return 0

        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app.progress = SimpleNamespace(configure=lambda **_kwargs: None)
        client = FakeClient()
        solver = FakeSolver()
        chosen = {
            "task_id": 1,
            "release_id": 2,
            "task_name": "测试任务",
            "task_type": 2,
            "progress": 66,
        }
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client,
                solver,
                chosen,
                "class",
                {"study": {}},
                10,
                FakeBank(),
                session_limit=3,
            )

        self.assertEqual(solver.runs, 3)
        self.assertEqual(client.submits, 0, "未完成小节应直接续答，不能重新选词回到第一题")

    def test_test_task_selects_words_after_current_section_ends(self) -> None:
        class FakeClient:
            def __init__(self):
                self.submits = 0
                self.info_calls = 0

            def chose_word_list(self, _task_id):
                return {
                    "data": {
                        "word_list": [
                            {
                                "word": "stack",
                                "score": 6.6,
                                "status": 1,
                                "course_id": "CET4_v2",
                                "list_id": "CET4_v2_23",
                            }
                        ]
                    }
                }

            def submit_chose_word(self, *_args, **_kwargs):
                self.submits += 1
                return {"data": {}}

            def task_info(self, _task_id, _release_id):
                self.info_calls += 1
                little_task = 1 if self.info_calls == 1 else 2
                return {"data": {"progress": 66, "exist_little_task": little_task}}

            def signin(self):
                return {"data": {}}

        class FakeSolver:
            kind = "class"

            def __init__(self):
                self.runs = 0

            def run(self, **_kwargs):
                self.runs += 1
                return SimpleNamespace(cancelled=False)

        class FakeBank:
            def __len__(self):
                return 0

            def cover_count(self, _names):
                return 0

        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        app.log = lambda _message: None
        app.ui = lambda *_args, **_kwargs: None
        app.progress = SimpleNamespace(configure=lambda **_kwargs: None)
        client = FakeClient()
        solver = FakeSolver()
        chosen = {
            "task_id": 1,
            "release_id": 2,
            "task_name": "测试任务",
            "task_type": 2,
            "progress": 66,
        }
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client,
                solver,
                chosen,
                "class",
                {"study": {}},
                10,
                FakeBank(),
                session_limit=2,
            )

        self.assertEqual(solver.runs, 2)
        self.assertEqual(client.submits, 1, "当前小节结束后应提交选词进入下一阶段")

    def test_class_task_retries_with_reset_on_little_task_error(self) -> None:
        class FakeClient:
            def __init__(self):
                self.submits = 0
                self.reset_flags = []

            def chose_word_list(self, _task_id):
                return {
                    "data": {
                        "word_list": [
                            {
                                "word": "folk",
                                "score": 6.6,
                                "course_id": "CET4_v2",
                                "list_id": "CET4_v2_19",
                            }
                        ]
                    }
                }

            def submit_chose_word(self, _task_id, _word_map, **kwargs):
                self.submits += 1
                reset = bool(kwargs.get("reset_chose_words"))
                self.reset_flags.append(reset)
                if self.submits == 1:
                    raise VocabgoError("接口报错 code=0 msg=对不起，存在小节任务未完成，请先完成小节任务")
                return {"data": {}}

            def task_info(self, _task_id, _release_id):
                return {
                    "data": {
                        "progress": 100 if self.submits >= 2 else 87,
                        "exist_little_task": 0 if self.submits == 0 else 1,
                        "task_type": 2,
                    }
                }

        class FakeSolver:
            kind = "class"

            def run(self, **_kwargs):
                return SimpleNamespace(cancelled=False)

        class FakeBank:
            def __len__(self):
                return 1

            def cover_count(self, _names):
                return 1

        app = object.__new__(VocabgoApp)
        app.cancel = threading.Event()
        logs: list[str] = []
        app.log = logs.append
        app.ui = lambda *_args, **_kwargs: None
        client = FakeClient()
        chosen = {
            "task_id": 1,
            "release_id": 2,
            "task_name": "List 19",
            "task_type": 2,
            "progress": 87,
        }
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client,
                FakeSolver(),
                chosen,
                "class",
                {"study": {}},
                10,
                FakeBank(),
            )
        self.assertEqual(client.reset_flags, [False, True])
        self.assertTrue(any("自动重置选词后重试" in line for line in logs))

if __name__ == "__main__":
    unittest.main(verbosity=2)
