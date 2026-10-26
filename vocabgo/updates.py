# -*- coding: utf-8 -*-
"""GitHub Release 更新检查：只提示，不自动下载或覆盖程序。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import net

GITHUB_REPO = "Katya-AI-git/vocabgo-study-helper"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    name: str
    html_url: str
    body: str


def parse_version(value: str) -> tuple[int, ...] | None:
    """解析 v1.0.3 / V1.0.3 / 1.0.3 形式的版本号。"""
    match = re.search(r"[vV]?(\d+(?:\.\d+)*)", str(value or "").strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def is_newer(latest: str, current: str) -> bool:
    latest_parts = parse_version(latest)
    current_parts = parse_version(current)
    if latest_parts is None or current_parts is None:
        return False
    length = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (length - len(latest_parts)) > current_parts + (0,) * (
        length - len(current_parts)
    )


def check_for_update(current_version: str, *, timeout: float = 6.0) -> ReleaseInfo | None:
    """查询 GitHub 最新正式 Release；网络失败时静默返回 None。"""
    try:
        response = net.request(
            "GET",
            LATEST_RELEASE_API,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"VocabgoStudyHelper/{current_version}",
            },
        )
        if response.status_code >= 400:
            return None
        data: Any = response.json()
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag, current_version):
        return None
    return ReleaseInfo(
        tag=tag,
        name=str(data.get("name") or tag),
        html_url=str(data.get("html_url") or RELEASE_PAGE),
        body=str(data.get("body") or ""),
    )

