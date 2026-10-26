# -*- coding: utf-8 -*-
"""GitHub 更新检查：版本比较与 Release 解析。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from vocabgo.gui import VocabgoApp
from vocabgo.updates import ReleaseInfo, check_for_update, is_newer, parse_version


class _Response:
    status_code = 200

    def json(self):
        return {
            "tag_name": "v1.0.4",
            "name": "V1.0.4",
            "html_url": "https://github.com/Katya-AI-git/vocabgo-study-helper/releases/tag/v1.0.4",
            "body": "release body",
            "draft": False,
            "prerelease": False,
        }


class UpdateCheckTest(unittest.TestCase):
    def test_parse_version_accepts_v_prefix(self) -> None:
        self.assertEqual(parse_version("v1.0.3"), (1, 0, 3))
        self.assertEqual(parse_version("V2.1"), (2, 1))
        self.assertIsNone(parse_version("not-a-version"))

    def test_newer_comparison(self) -> None:
        self.assertTrue(is_newer("v1.0.3", "1.0.2"))
        self.assertTrue(is_newer("1.1", "1.0.9"))
        self.assertFalse(is_newer("v1.0.2", "1.0.2"))
        self.assertFalse(is_newer("v1.0.1", "1.0.2"))

    def test_check_for_update_returns_release(self) -> None:
        with patch("vocabgo.updates.net.request", return_value=_Response()) as request:
            info = check_for_update("1.0.3")

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.tag, "v1.0.4")
        self.assertIn("/v1.0.4", info.html_url)
        self.assertEqual(request.call_count, 1)

    def test_no_update_when_current_is_newer_or_equal(self) -> None:
        with patch("vocabgo.updates.net.request", return_value=_Response()):
            self.assertIsNone(check_for_update("1.0.4"))
            self.assertIsNone(check_for_update("2.0.0"))

    def test_ignore_update_remembers_version(self) -> None:
        app = object.__new__(VocabgoApp)
        app.config = {"runtime": {}}
        info = ReleaseInfo(
            tag="v1.0.4",
            name="V1.0.4",
            html_url="https://example.test/v1.0.4",
            body="",
        )

        with (
            patch("vocabgo.gui.messagebox.askyesno", return_value=False),
            patch("vocabgo.gui.save_settings") as save,
        ):
            app._prompt_update(info)  # noqa: SLF001

        self.assertEqual(app.config["runtime"]["ignored_update_version"], "v1.0.4")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

