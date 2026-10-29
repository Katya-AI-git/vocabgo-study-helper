# -*- coding: utf-8 -*-
"""测试任务的「内部答题会话」续答策略回归测试。

线上表现：测试任务只有一次机会，却常在 30%~60% 处停下，日志写「任务已完成」或
干脆没有任何收尾说明。查下来的原因是 `_run_once` 里三处判定：

1. 内部会话数被 6 死锁，答不完也收工 → 现在进度还在涨就放宽上限；
2. 读 `task_info` 失败时 `progress = 0`，于是「没进展」判定误触发 → 现在沿用上一次进度；
3. 进度没涨仍然继续跑（测试任务专用分支）→ 保留，但一旦不涨又到上限就必须说清楚
   停在哪、为什么停，不能默默结束。

这些行为只能对着 GUI 的 `_run_once` 测，所以把 client/solver/bank 全部打桩。
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.gui import TEST_SESSION_HARD_LIMIT, VocabgoApp  # noqa: E402


class FakeClient:
    """按给定的进度序列回应；submit_chose_word 只记录次数。

    注意序列的第一个值会被 `_run_once` 开跑前的 `initial_info` 读走（判断是否要重置选词），
    之后每个答题会话各读一次。所以 plan 的长度 = 1 + 会话数。
    """

    def __init__(self, progress_plan: list[int], *, little_task: int = 2) -> None:
        self.progress_plan = list(progress_plan)
        self.submits = 0
        self.reset_flags: list[bool] = []
        self.little_task = little_task

    def chose_word_list(self, _task_id):
        return {
            "data": {
                "word_list": [
                    {"word": "stack", "score": 6.6, "course_id": "CET4_v2", "list_id": "CET4_v2_23"}
                ]
            }
        }

    def submit_chose_word(self, _task_id, _word_map, **kwargs):
        self.submits += 1
        self.reset_flags.append(bool(kwargs.get("reset_chose_words")))
        return {"data": {}}

    def task_info(self, _task_id, _release_id):
        if not self.progress_plan:
            return {"data": {"progress": 100, "exist_little_task": 0}}
        value = self.progress_plan.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"data": {"progress": value, "exist_little_task": self.little_task}}

    def signin(self):
        return {"data": {}}


class FakeSolver:
    kind = "class"

    def __init__(self) -> None:
        self.runs = 0

    def run(self, **_kwargs):
        self.runs += 1
        return SimpleNamespace(cancelled=False)


class FakeBank:
    def __len__(self) -> int:
        return 0

    def cover_count(self, _names) -> int:
        return 0


def make_app():
    app = object.__new__(VocabgoApp)
    app.cancel = threading.Event()
    app.logs: list[str] = []
    app.log = app.logs.append  # type: ignore[method-assign]
    app.ui = lambda *_a, **_kw: None
    app.progress = SimpleNamespace(configure=lambda **_kw: None)
    return app


CHOSEN = {
    "task_id": 1,
    "release_id": 2,
    "task_name": "测试任务",
    "task_type": 2,
    "progress": 10,
}


class TestSessionKeepGoingTest(unittest.TestCase):
    def test_growing_progress_extends_beyond_initial_limit(self) -> None:
        """进度一直在涨就接着答 —— 不再被 6 个会话卡死。"""
        app = make_app()
        # 首值给 initial_info 用，之后 10 个会话的进度
        client = FakeClient([10, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        solver = FakeSolver()
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client, solver, CHOSEN, "class", {"study": {}}, 10, FakeBank(), session_limit=6
            )
        self.assertEqual(solver.runs, 10, "进度涨到 100% 之前不应该停")
        self.assertTrue(any("放宽" in line for line in app.logs), "放宽上限要写进日志")
        self.assertTrue(any("100%" in line for line in app.logs))

    def test_progress_read_failure_does_not_reset_to_zero(self) -> None:
        """读进度失败沿用上一次的值：不能因此判成「没进展」提前停。"""
        app = make_app()
        client = FakeClient([20, 20, RuntimeError("network down"), 100])
        solver = FakeSolver()
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client, solver, CHOSEN, "class", {"study": {}}, 10, FakeBank(), session_limit=6
            )
        self.assertEqual(solver.runs, 3, "读进度失败后必须继续，不能提前结束")
        self.assertTrue(any("读取任务进度失败" in line for line in app.logs))

    def test_stops_with_explanation_when_progress_flat_at_limit(self) -> None:
        """进度不涨又到上限：要明确说停在几成、为什么，不能默默结束。"""
        app = make_app()
        client = FakeClient([40, 40, 40])
        solver = FakeSolver()
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client, solver, CHOSEN, "class", {"study": {}}, 10, FakeBank(), session_limit=2
            )
        self.assertEqual(solver.runs, 2)
        self.assertTrue(any("没刷到 100%" in line for line in app.logs), "必须给出停下来的原因")
        # 旧版这里提示「可接着答」；实测冻结任务连官方小程序都救不回来，
        # 所以改成中性的「再跑一次看进度会不会变」，不误导、也不做自动判定。
        self.assertTrue(any("再跑一次" in line for line in app.logs), "要告诉用户怎么判断")

    def test_hard_limit_is_respected(self) -> None:
        """进度一直涨也不能无限跑：硬上限兜住。"""
        app = make_app()
        client = FakeClient([1, *range(2, TEST_SESSION_HARD_LIMIT + 6)])
        solver = FakeSolver()
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client, solver, CHOSEN, "class", {"study": {}}, 10, FakeBank(), session_limit=2
            )
        self.assertEqual(solver.runs, TEST_SESSION_HARD_LIMIT)

    def test_first_session_resets_words_only_once(self) -> None:
        """测试任务只在首会话重置选词，后续会话不能反复回到第一题。"""
        app = make_app()
        client = FakeClient([40, 40, 40])
        solver = FakeSolver()
        with patch("vocabgo.gui.time.sleep", return_value=None):
            app._run_once(  # noqa: SLF001
                client, solver, CHOSEN, "class", {"study": {}}, 10, FakeBank(), session_limit=2
            )
        self.assertEqual(solver.runs, 2)
        self.assertEqual(client.reset_flags, [True, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
