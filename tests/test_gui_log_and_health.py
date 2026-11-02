# -*- coding: utf-8 -*-
"""界面行为测试：任务体检。

痛点：**开刷前看不出任务还能不能刷**。测试任务只有一次机会，一旦在答题中途被中断，
服务端会把成绩固定在断点处，之后怎么刷都不动。以前只能「刷一轮试试」，
白耗时间。现在开刷前先体检。
"""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.client import VocabgoError  # noqa: E402
from vocabgo.gui import VocabgoApp  # noqa: E402


class TaskHealthCheckTest(unittest.TestCase):
    """任务体检：只读服务端数据，把任务真实状态摆出来。

    注意这里**刻意不检查「可以刷」这类结论**：实测证明「服务端还出题」
    根本不能推出「答完算数」—— 被固定的任务在官方小程序里照样能答，
    提交后一样报错、进度不变。体检只报事实，判断交给「两次对比」。
    """

    def setUp(self) -> None:
        # 体检会顺手把状态写回配置；测试里绝不能真写盘
        self._saver = patch("vocabgo.gui.save_settings", return_value=None)
        self._saver.start()
        self.addCleanup(self._saver.stop)

    def _app(self) -> VocabgoApp:
        app = object.__new__(VocabgoApp)
        app.config = {"runtime": {}}
        app.logs = []
        app.log = app.logs.append  # type: ignore[method-assign]
        return app

    CHOSEN = {
        "task_id": 123,
        "release_id": 456,
        "task_name": "CET4（第二版）List  22",
        "task_type": 2,
        "progress": 66,
    }

    def _client(self, *, info: dict, words: list | None = None, start: object = None):
        class FakeClient:
            def task_info(self, _task_id, _release_id):
                return {"data": info}

            def chose_word_list(self, _task_id):
                return {"data": {"word_list": words or []}}

            def start_answer(self, _task_id, _release_id):
                if isinstance(start, Exception):
                    raise start
                return {"data": start or {}}

        return FakeClient()

    def _seed_previous(self, app: VocabgoApp, *, minutes_ago: int, progress: int, spent: int) -> None:
        """预置一条「上次体检」记录，用来验证对比逻辑。"""
        at = datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)
        app.config["runtime"]["task_snapshots"] = {
            "123": {
                "progress": progress,
                "time_spent": spent,
                "at": at.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }

    def test_warns_when_task_not_started(self) -> None:
        app = self._app()
        chosen = dict(self.CHOSEN, task_id=-1)
        app._check_task_health(self._client(info={}), chosen)  # noqa: SLF001
        self.assertTrue(any("还没开始过" in line for line in app.logs))

    def test_reports_facts_without_overclaiming(self) -> None:
        """只报事实：进度、词分、服务端能不能出题 —— 不说「可以刷」。"""
        app = self._app()
        client = self._client(
            info={"progress": 66, "score": 64.4, "time_spent": 191938, "exist_little_task": 1},
            words=[{"word": "forge", "score": 6.6}, {"word": "gaze", "score": 3.3}],
            start={"topic_code": "abc", "topic_done_num": 3, "topic_total": 60},
        )
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        joined = "\n".join(app.logs)
        self.assertIn("服务端可出题", joined)
        self.assertIn("未达最高分的 1 个", joined)  # gaze 低于 forge
        # 不能出现「可以刷」这种把「能出题」当「能刷完」的结论
        self.assertNotIn("可以刷", joined)

    def test_first_check_records_and_does_not_flag(self) -> None:
        """首次体检没有可比对象，只记状态、不乱下结论。"""
        app = self._app()
        app._check_task_health(self._client(info={"progress": 66, "time_spent": 191938}), self.CHOSEN)  # noqa: SLF001
        joined = "\n".join(app.logs)
        self.assertIn("已记下本次状态", joined)
        self.assertNotIn("已被固定", joined)
        self.assertIn("123", app.config["runtime"]["task_snapshots"], "应当写下快照")

    def test_flags_task_that_never_moves(self) -> None:
        """核心场景：两次体检之间进度和累计用时一点没变 → 判定被固定。

        这是唯一可靠的判据。用户实测：跑完整轮进度仍是 66%，
        而服务端照样能出题（官方小程序也能做，只是做完报错）。
        """
        app = self._app()
        self._seed_previous(app, minutes_ago=10, progress=66, spent=191938)
        client = self._client(
            info={"progress": 66, "score": 64.4, "time_spent": 191938, "exist_little_task": 1},
            start={"topic_code": "abc", "topic_done_num": 20, "topic_total": 60},
        )
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        joined = "\n".join(app.logs)
        self.assertIn("完全没变", joined)
        self.assertIn("换一条", joined)

    def test_progress_moving_is_not_flagged(self) -> None:
        """进度确实在涨的，不能被误判成固定。"""
        app = self._app()
        self._seed_previous(app, minutes_ago=10, progress=66, spent=191938)
        client = self._client(
            info={"progress": 78, "score": 72.0, "time_spent": 260000},
            start={"topic_code": "abc", "topic_done_num": 5, "topic_total": 60},
        )
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        joined = "\n".join(app.logs)
        self.assertNotIn("完全没变", joined)
        self.assertIn("已记下本次状态", joined)

    def test_short_interval_is_not_flagged(self) -> None:
        """两次体检间隔太短时不判定 —— 刚跑完就查，本来就可能没变化。"""
        app = self._app()
        self._seed_previous(app, minutes_ago=1, progress=66, spent=191938)
        client = self._client(info={"progress": 66, "score": 64.4, "time_spent": 191938})
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        self.assertNotIn("完全没变", "\n".join(app.logs))

    def test_completed_task_is_not_flagged(self) -> None:
        """进度 100% 时，服务端说已完成是正常的，不该报警。"""
        app = self._app()
        client = self._client(
            info={"progress": 100, "score": 98.8},
            start=VocabgoError("接口报错 code=20004 msg=任务已完成！"),
        )
        app._check_task_health(client, dict(self.CHOSEN, progress=100))  # noqa: SLF001
        joined = "\n".join(app.logs)
        self.assertIn("不用再刷", joined)
        self.assertNotIn("已被固定", joined)

    def test_deadline_passed(self) -> None:
        app = self._app()
        client = self._client(
            info={"progress": 87},
            start=VocabgoError("接口报错 code=20006 msg=任务已截止"),
        )
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        self.assertTrue(any("已截止" in line for line in app.logs))

    def test_needs_word_selection_is_reported_as_normal(self) -> None:
        """服务端要求重新选词，是正常状态，不是故障。"""
        app = self._app()
        client = self._client(
            info={"progress": 66},
            start=VocabgoError("接口报错 code=20001 msg=需要选词！"),
        )
        app._check_task_health(client, self.CHOSEN)  # noqa: SLF001
        self.assertTrue(any("本节已答完" in line for line in app.logs))
        self.assertFalse(any("已截止" in line for line in app.logs))

    def test_info_failure_is_reported_not_raised(self) -> None:
        """读任务信息失败要提示出来，不能把异常抛到界面上。"""
        app = self._app()

        class BrokenClient:
            def task_info(self, _a, _b):
                raise VocabgoError("网络不可达")

        app._check_task_health(BrokenClient(), self.CHOSEN)  # noqa: SLF001
        self.assertTrue(any("读任务信息失败" in line for line in app.logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)