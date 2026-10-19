# -*- coding: utf-8 -*-
"""路径解析：源码运行 vs PyInstaller 打包后运行，配置/题库都要落在「软件自己那一层」。

打包成 exe 后 `__file__` 指向临时解包目录（会被清掉），必须改用 exe 所在目录，
否则用户填的 token、积累的题库一关软件就没了。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DATA_DIR_NAME = "VocabgoAuto"


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """软件目录：源码运行 = 项目根；打包后 = exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读资源目录：打包后是 PyInstaller 的解包目录 _MEIPASS。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def data_dir() -> Path:
    """用户运行数据目录：题库和词典缓存不放桌面，避免同步/文件扫描占用。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_DATA_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_DATA_DIR_NAME


def config_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else app_dir() / path
    env_path = os.environ.get("VOCABGO_CONFIG")
    if env_path:
        return Path(env_path)
    return app_dir() / "config.json"


def bank_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else app_dir() / path
    target = data_dir() / "bank.json"
    legacy = app_dir() / "bank.json"
    if legacy.exists() and not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
        except OSError:
            # 文件被同步/扫描软件占用时，本次先用空题库启动；下次仍会重试迁移。
            pass
    return target


def materials_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """用户资料库目录；留空时放在 LOCALAPPDATA，避免分享包携带个人资料。"""
    if override:
        path = Path(override)
        return path if path.is_absolute() else app_dir() / path
    return data_dir() / "materials"


def asset(relative: str) -> Path:
    """取打包进去的资源（图标等）。"""
    return resource_dir() / "assets" / relative
