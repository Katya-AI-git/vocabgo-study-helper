# -*- coding: utf-8 -*-
"""界面行为测试：日志滚动 + 任务体检。

两个都是实际用出来的痛点：

1. **日志被拽回底部**。刷题时每答完一个词就写一行日志，旧代码无条件
   `see("end")`，于是想往上翻看前面答了什么，每 0.15 秒就被拉回底部，根本看不了。

2. **开刷前看不出任务还能不能刷**。测试任务只有一次机会，一旦在答题中途被中断，
   服务端会把成绩固定在断点处，之后怎么刷都不动。以前只能「刷一轮试试」，
   白耗时间。现在开刷前先体检。

Tk 相关用例在没有图形环境时会自动跳过（CI / 无头机器），不会让整套测试失败。
"""

from __future__ import annotations

import datetime
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo.client import VocabgoError  # noqa: E402
from vocabgo.gui import VocabgoApp  # noqa: E402


class LogScrollTest(unittest.TestCase):
    """日志区：用户往上翻时，新日志不能把视图拽走。"""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # 无图形环境
            self.skipTest(f"没有图形环境：{exc}")
        self.root.geometry("600x400")
        self.app = VocabgoApp(self.root)
        self.root.update()
        # 灌满日志，制造可滚动的空间
        self.app.log_text.configure(state="normal")
        for i in range(300):
            self.app.log_text.insert("end", f"刷题日志行 {i}\n")
        self.app.log_text.configure(state="disabled")
        self.root.update()

    def tearDown(self) -> None:
        # 先取消挂着的定时器再销毁窗口：否则 Tk 会在下个用例里报
        # "invalid command name ..._drain_log"，把测试输出弄得很脏。
        for attr in ("_log_job",):
            job = getattr(self.app, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_follows_bottom_when_at_bottom(self) -> None:
        """停在末尾时应当自动跟随，不然刷题时看不到最新日志。"""
        self.app.log_text.see("end")
        self.root.update()
        self.assertTrue(self.app._log_sticks_to_bottom())  # noqa: SLF001

    def test_detects_scrolled_up(self) -> None:
        """用户翻上去后应当被识别出来。"""
        self.app.log_text.yview_moveto(0.2)
        self.root.update()
        self.assertFalse(self.app._log_sticks_to_bottom())  # noqa: SLF001

    def test_view_stays_put_while_scrolled_up(self) -> None:
        """核心行为：翻上去看历史时，新日志不能把视图拽回底部。"""
        self.app.log_text.yview_moveto(0.2)
        self.root.update()
        before = self.app.log_text.yview()[0]

        for i in range(5):
            self.app.log_queue.put(f"第 {i} 个词答完")
        self.app._drain_log()  # noqa: SLF001
        self.root.update()

        after = self.app.log_text.yview()[0]
        self.assertLess(abs(after - before), 0.03, "视图漂移过大，等于又把用户拽走了")

    def test_following_resumes_after_returning_to_bottom(self) -> None:
        """回到末尾后要恢复自动跟随，不能一翻上去就再也不跟。"""
        self.app.log_text.yview_moveto(0.2)
        self.root.update()
        self.app.log_text.see("end")
        self.root.update()

        self.app.log_queue.put("新的一行")
        self.app._drain_log()  # noqa: SLF001
        self.root.update()

        self.assertGreaterEqual(self.app.log_text.yview()[1], 0.999, "应当重新贴着底部")

    def test_unrendered_widget_defaults_to_following(self) -> None:
        """控件还没显示出来时，不能误判成「用户翻到顶了」。

        实测：未布局的 Text 会给出 (0.0, 0.077) 这种假值，
        不加判断就会被当成「用户在翻看历史」，从此再也不自动跟随 —— 另一个方向的毛病。
        """
        app = object.__new__(VocabgoApp)
        app.log_text = tk.Text(self.root)  # 没有 pack，所以未显示
        self.assertFalse(app.log_text.winfo_ismapped(), "前提：控件确实没显示")
        self.assertTrue(app._log_sticks_to_bottom())  # noqa: SLF001


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
