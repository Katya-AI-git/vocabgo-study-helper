# -*- coding: utf-8 -*-
"""「刷一半任务就结束」三个根因的回归测试。

线上表现
--------
测试任务只能做一次，但经常刷到 30%~60% 就停：日志里一串「本题异常，跳过」，
或者干脆显示「已结束」，剩下的一次机会就废掉了。

根因有三个，各自独立、都能单独造成「中途结束」：

1. **响应解不开就当场退场**。服务端会灰度轮换 jv 混淆方案；方案没登记时
   `unwrap` 把原始 base64 字符串塞回 data，上层拿它当正常数据用，
   拿不到题目就把这一轮判成「结束」。
2. **网络抖一下也被当成致命错误**。`_request` 一次失败就直接抛，
   一抛就冒到 solver 的 except 里，那一段原来的写法是「本题异常，跳过 → return」。
3. **内部会话数写死**。测试任务固定 6 个会话，答不完也收工；
   而且读进度失败时把 progress 当成 0，于是「没进展」判定也会误触发。

下面每个类对应一个根因，跑的是真实代码路径（mock 只在最外层打桩）。
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocabgo import crypto  # noqa: E402
from vocabgo.bank import Bank  # noqa: E402
from vocabgo.client import VocabgoClient, VocabgoDecodeError, VocabgoError  # noqa: E402
from vocabgo.crypto import decode_jv, unwrap  # noqa: E402
from vocabgo.solver import Solver  # noqa: E402


class UnknownJvTest(unittest.TestCase):
    """根因 1：服务端上了没登记的 jv 方案。"""

    def test_unknown_scheme_with_padding_still_decodes(self) -> None:
        """新方案通常只是「头部多插了几个字符」，抢救式解码要能认出来。"""
        payload = {"topic_code": "abc", "topic_mode": 11, "topic_done_num": 7}
        plain = base64.b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        self.assertIsNone(crypto.JV_RULES.get("9_9999"))
        decoded = decode_jv("ZZ" + plain, "9_9999")
        self.assertEqual(decoded, payload)

    def test_unknown_scheme_unrecoverable_is_flagged(self) -> None:
        """真解不开时必须显式标成解码失败，不能让上层把原文当数据用。

        这就是以前「刷一半结束」的开关：data 被塞回原始 base64 字符串，
        上层 pick_topic 拿不到题，于是把这一轮判成结束。
        """
        body = unwrap({"code": 1, "jv": "9_9999", "data": "!!!not-base64!!!"})
        self.assertIn("_decode_error", body)

    def test_decode_error_raises_dedicated_type(self) -> None:
        """解码失败要抛 VocabgoDecodeError（可重试），而不是被当成业务错误码。"""
        client = VocabgoClient("tok")
        resp = SimpleNamespace(status_code=200, text="{}", json=lambda: {"code": 1, "jv": "9_9999", "data": "!!!"})
        with patch.object(client.session, "get", return_value=resp), patch(
            "vocabgo.client.time.sleep", return_value=None
        ):
            with self.assertRaises(VocabgoDecodeError):
                client._get("/Student/ClassTask/Info", {"a": 1})  # noqa: SLF001


class TransientRetryTest(unittest.TestCase):
    """根因 2：网络抖一下不该终止整场。"""

    def test_connection_error_is_retried(self) -> None:
        client = VocabgoClient("tok")
        ok = SimpleNamespace(status_code=200, text="{}", json=lambda: {"code": 1, "data": {"ok": True}})
        calls = {"n": 0}

        def flaky(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("connection reset")
            return ok

        with patch.object(client.session, "get", side_effect=flaky), patch(
            "vocabgo.client.time.sleep", return_value=None
        ):
            data = client._get("/Student/ClassTask/Info", {"a": 1})  # noqa: SLF001
        self.assertEqual(data["data"], {"ok": True})
        self.assertEqual(calls["n"], 2, "第一次失败后必须自动重试")

    def test_persistent_failure_still_reports(self) -> None:
        client = VocabgoClient("tok")
        with patch.object(
            client.session, "get", side_effect=requests.ConnectionError("down")
        ), patch("vocabgo.client.time.sleep", return_value=None):
            with self.assertRaises(VocabgoError) as ctx:
                client._get("/Student/ClassTask/Info", {"a": 1})  # noqa: SLF001
        self.assertNotIsInstance(ctx.exception, VocabgoDecodeError)

    def test_business_error_is_not_retried(self) -> None:
        """业务错误码（如 11003 令牌失效）重试没用，必须原样抛出。"""
        client = VocabgoClient("tok")
        calls = {"n": 0}

        def deny(*_a, **_kw):
            calls["n"] += 1
            return SimpleNamespace(status_code=200, text="{}", json=lambda: {"code": 11003, "msg": "需安全验证"})

        with patch.object(client.session, "post", side_effect=deny):
            with self.assertRaises(VocabgoError):
                client._post("/Student/ClassTask/VerifyAnswer", {})  # noqa: SLF001
        self.assertEqual(calls["n"], 1, "业务错误不该重试")


class SolverSelfHealTest(unittest.TestCase):
    """根因 2 的另一半：单题异常不再让整个会话退场。"""

    class FlakyClient:
        """第一题正常；第二题提交时抛一次网络错；之后恢复正常。"""

        def __init__(self) -> None:
            self.starts = 0
            self.verifies = 0
            self.submits = 0

        def start_answer(self, task_id, release_id):
            self.starts += 1
            return {
                "data": {
                    "topic_code": "t1",
                    "topic_mode": 16,
                    "topic_done_num": 0,
                    "topic_total": 3,
                    "stem": {"content": "retreat"},
                    "options": [{"content": "n. 撤退", "answer_tag": 0}],
                }
            }

        def verify(self, code, answer):
            self.verifies += 1
            return {"data": {"topic_code": code, "answer_result": 1}}

        def submit(self, code, spent):
            self.submits += 1
            if self.submits == 1:
                raise VocabgoError("连接被重置：网络抖动")
            if self.submits == 2:
                return {
                    "data": {
                        "topic_code": "t2",
                        "topic_mode": 16,
                        "topic_done_num": 1,
                        "topic_total": 3,
                        "stem": {"content": "peel"},
                        "options": [{"content": "v. 剥皮", "answer_tag": 0}],
                    }
                }
            return {"data": {"topic_code": "t2", "topic_done_num": 3, "topic_total": 3}}

        def task_info(self, task_id, release_id):
            return {"data": {"progress": 100}}

    def _solver(self, client) -> Solver:
        folder = Path(tempfile.mkdtemp())
        return Solver(
            client,  # type: ignore[arg-type]
            Bank(folder / "bank.json"),
            kind="class",
            probe=False,
            pace=(0.0, 0.0),
            logger=lambda _m: None,
        )

    def test_one_bad_submit_does_not_end_the_session(self) -> None:
        client = self.FlakyClient()
        solver = self._solver(client)
        with patch("vocabgo.solver.time.sleep", return_value=None):
            stats = solver.run(task_id=1, release_id=2)
        self.assertGreaterEqual(client.submits, 2, "出错后必须继续往下答，而不是当场收工")
        self.assertEqual(stats.errors, 1, "错误要计数，但不能把会话判死")
        self.assertFalse(stats.cancelled)

    def test_resync_when_same_topic_keeps_failing(self) -> None:
        """同一题怎么都过不去时，重新 StartAnswer 向服务端要当前进度，接着答。"""

        class StuckClient:
            def __init__(self) -> None:
                self.starts = 0
                self.submits = 0

            def start_answer(self, task_id, release_id):
                self.starts += 1
                # 第二次 StartAnswer 起，服务端给出下一题，说明进度真的往前走了
                code = "t1" if self.starts == 1 else "t2"
                return {
                    "data": {
                        "topic_code": code,
                        "topic_mode": 16,
                        "topic_done_num": 0 if code == "t1" else 2,
                        "topic_total": 2,
                        "stem": {"content": "retreat"},
                        "options": [{"content": "n. 撤退", "answer_tag": 0}],
                    }
                }

            def verify(self, code, answer):
                return {"data": {"topic_code": code, "answer_result": 1}}

            def submit(self, code, spent):
                self.submits += 1
                if code == "t1":
                    raise VocabgoError("code=500 服务端抽风")
                return {"data": {"topic_code": code, "topic_done_num": 2, "topic_total": 2}}

            def task_info(self, task_id, release_id):
                return {"data": {"progress": 100}}

        client = StuckClient()
        solver = self._solver(client)
        with patch("vocabgo.solver.time.sleep", return_value=None):
            stats = solver.run(task_id=1, release_id=2)
        self.assertEqual(client.starts, 2, "重试用尽后必须重新 StartAnswer 续答")
        self.assertEqual(stats.errors, 3, "两次原地重试 + 一次续答前的失败")
        self.assertEqual(stats.done, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
