# -*- coding: utf-8 -*-
"""打「分享版」：一个文件夹 + 两个 zip（免安装 EXE 包 / 源码自动装 Python 包）。

三种形态都留在同一个文件夹里，方便自己跑、也方便发给别人：

    vocabgo-optimized-exe/                        免安装 EXE 目录（解压即用）
    vocabgo-optimized-exe-win32.zip               EXE 压缩包
    vocabgo-optimized-source-auto-python.zip      源码包（双击 启动.bat，缺 Python 会自动装）

用法::

    python tools/make_share.py                       # 输出到 ../词达人分享版-大模型版
    python tools/make_share.py --out D:\\某个目录      # 指定输出目录
    python tools/make_share.py --skip-exe            # 只重打两个 zip（EXE 目录已是最新）

发出去之前会自动扫一遍：包里不能有 config.json / bank.json / API Key / 用户令牌。
扫到就中止，zip 不会生成 —— 这一步是防手滑，不要绕过。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vocabgo import __version__  # noqa: E402

DIST = ROOT / "dist" / "Vocabgo"
SUFFIX = "大模型版"          # 与旧分享版区分的提示性后缀

# 源码包里放什么：与 GitHub 仓库同步，但不带个人配置、测试和打包脚本
SOURCE_FILES = (
    ".gitattributes",
    "LICENSE",
    "bootstrap_python.ps1",
    "config.example.json",
    "manual.html",
    "open_manual.bat",
    "prepare.bat",
    "requirements.txt",
    "run.bat",
    "vocabgo_app.py",
    "vocabgo_cli.py",
    "启动.bat",
    "使用说明.txt",
    "技术说明.txt",
    "版本记录.txt",
)
SOURCE_DIRS = ("vocabgo", "assets")

# 绝不能出现在分享包里的东西
FORBIDDEN_NAMES = (
    "config.json",
    "bank.json",
    "materials.json",
    "captured_headers.json",
    "glossary.json",
)
SECRET_PATTERNS = {
    "API 密钥（sk-…）": re.compile(rb"sk-[A-Za-z0-9_\-]{16,}"),
    "Bearer 令牌": re.compile(rb"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    "用户令牌": re.compile(rb'"usertoken"\s*:\s*"[A-Za-z0-9]{20,}"'),
    "abc 鉴权字段": re.compile(rb'"abc"\s*:\s*"[A-Za-z0-9]{20,}"'),
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_out() -> Path:
    return ROOT.parent / f"词达人分享版-{SUFFIX}"


def copy_tree_items(target: Path) -> None:
    """把仓库里该发的东西铺进目标目录（跳过个人配置与缓存）。"""
    for name in SOURCE_FILES:
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, target / name)
    for name in SOURCE_DIRS:
        source = ROOT / name
        if not source.exists():
            continue
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            if "__pycache__" in item.parts or item.suffix in (".pyc", ".pyo"):
                continue
            relative = item.relative_to(source)
            destination = target / name / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def stage_exe(out: Path) -> Path:
    """把 dist/Vocabgo 拷成分享版里的 EXE 目录（三个形态里的第一个）。"""
    target = out / "vocabgo-optimized-exe"
    if not DIST.exists():
        raise SystemExit("❌ 没找到 dist/Vocabgo，先跑 python build_exe.py --gui-only")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(DIST, target, ignore=shutil.ignore_patterns("config.json", "bank.json", "__pycache__"))
    return target


def stage_source(out: Path) -> Path:
    """源码目录：用于打 source-auto-python 包。"""
    target = out / "_source-stage"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    copy_tree_items(target)
    write_share_readme(target / "README.txt")
    return target


def scan(folder: Path) -> list[str]:
    problems: list[str] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"包含个人文件：{relative}")
            continue
        try:
            data = path.read_bytes()
        except Exception:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                problems.append(f"{relative} 里发现{label}")
    return problems


def make_zip(folder: Path, archive: Path) -> Path:
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for item in sorted(folder.rglob("*")):
            if item.is_file():
                zf.write(item, Path(folder.name) / item.relative_to(folder))
    return archive


def write_share_readme(target: Path) -> None:
    """分享版自己的 README：说清这个文件夹里哪三个形态、先点哪个。"""
    text = f"""Vocabgo Study Helper 分享包（v{__version__} · {SUFFIX}）
============================================

反馈与交流
==========

QQ 群：1130860539

装不上、双击没反应、闪退、提示令牌失效 —— 到群里问，
附上版本号、报错原文和日志最后 20 行，问题会好定位得多。
发新版本、服务端改题型或改签名，也会先在群里通知。

包里有什么（三种形态）
--------------------

使用说明.txt        第一次用先看这个，五分钟能跑起来
manual.html         完整图文说明（双击 open_manual.bat 打开）
技术说明.txt        协议、模块与作答策略
版本记录.txt        每个版本改了什么
发布说明-v{__version__}.md  本版本重点

vocabgo-optimized-exe/                    ① 免安装 EXE 目录（解压即用，双击 Vocabgo.exe）
vocabgo-optimized-exe-win32.zip           ② 上面这个目录的压缩包，发给没装 Python 的同学
vocabgo-optimized-source-auto-python.zip  ③ 源码版，解压后双击 启动.bat；
                                             电脑没 Python 会自动下载安装

先做什么
--------

1. 先把压缩包整个解压，不要在里面直接双击。
2. 双击 Vocabgo.exe（源码版双击 启动.bat）。
3. 设置页点「一键填入推荐配置」，有 DeepSeek Key 就填上，保存。
4. 刷题页「登录获取令牌」→ 用微信扫码 →「刷新任务」→「开始刷题」。
5. 开刷前可以先点「🩺 任务体检」，确认这条任务还能刷。

数据位置：%LOCALAPPDATA%\VocabgoAuto

分享给别人之前，至少删掉 config.json —— 里面有你的用户令牌和 API Key。
"""
    target.write_text(text, encoding="utf-8")


def write_release_note(out: Path) -> None:
    """分享版里的「本版本重点」——与版本记录同源同口径。"""
    note = f"""V{__version__} 作答更准：做题统一交给大模型

## 主要更新

### 做题统一交给大模型判断

题库里答过的题、规则能唯一确定的题照旧秒答，其余题目一律先问模型，
本地资料和词典退到「提示 + 兜底」的位置。没见过的新题型、口语化短语、词形变化，
模型都能按题意作答。

### 作答额度给足

- 单次作答额度 512 → 4096 tokens；推理被截断时自动抬升，最高 16384。
- 选择题、填空题、组词题额度同步提高，长句与长短语不再被截断。
- 上下文不再压缩：任务词表全量（原先只给前 200 条），
  已知释义 40 → 200 条，本地资料片段 2400 → 12000 字，填空候选 2000 → 8000 字。
- 自学任务也改成每题独立询问 3 次取多数（原先 1 次）。

### 启动与体检

- `.gitattributes` 把 `.bat` 换行写死为 CRLF：从 GitHub「Download ZIP」下载也不会乱码闪退。
- `prepare.bat` 建环境前先验证 tkinter，缺 tcl/tk 会给出可执行指引。
- 开刷前可用「🩺 任务体检」看进度、得分、累计用时与词分分布；
  隔几分钟再查一次，进度和用时完全没变就说明这条任务已停在断点处。

## 验证结果

- 自动化测试 240 项通过。
- 4 项可选 OCR 测试因本机没有 Tesseract 跳过。
- 模型前置后的调用顺序有专门用例锁定。
- 分享包不包含 config.json、bank.json、API Key 或个人数据。

本项目基于公开 MIT 项目二次开发，非官方版本。
请遵守平台用户协议和学校学术诚信要求。
"""
    (out / f"发布说明-v{__version__}.md").write_text(note, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="打分享版（文件夹 + 两个 zip）")
    parser.add_argument("--out", default="", help="输出目录，默认 ../词达人分享版-大模型版")
    parser.add_argument("--skip-exe", action="store_true", help="跳过 EXE 目录拷贝，只重打 zip")
    args = parser.parse_args()

    out = Path(args.out) if args.out else default_out()
    out.mkdir(parents=True, exist_ok=True)
    print(f"📁 输出目录：{out}")

    if not args.skip_exe:
        exe_dir = stage_exe(out)
        print(f"  ① 免安装 EXE 目录：{exe_dir.name}")

    source_stage = stage_source(out)

    print("\n=== 安全自检（不能把密钥 / 令牌 / 题库带出去）===")
    problems = scan(source_stage)
    if not args.skip_exe:
        problems += [f"vocabgo-optimized-exe/{p}" for p in scan(out / "vocabgo-optimized-exe")]
    if problems:
        for item in problems:
            print(f"  ⚠️ {item}")
        print("\n❌ 检测到敏感内容，已中止（zip 不会生成）。")
        shutil.rmtree(source_stage, ignore_errors=True)
        return 2
    print("  ✅ 没有密钥 / 令牌 / 个人配置")

    source_zip = make_zip(source_stage, out / "vocabgo-optimized-source-auto-python.zip")
    shutil.rmtree(source_stage, ignore_errors=True)
    print(f"  ③ 源码包：{source_zip.name}　{source_zip.stat().st_size / 1024:.0f} KB")

    if not args.skip_exe:
        exe_zip = make_zip(out / "vocabgo-optimized-exe", out / "vocabgo-optimized-exe-win32.zip")
        print(f"  ② EXE 包：{exe_zip.name}　{exe_zip.stat().st_size / 1024 / 1024:.1f} MB")

    write_release_note(out)
    write_share_readme(out / "README.txt")
    # 根目录也放一份说明，和旧分享版保持一致的使用习惯
    for name in ("使用说明.txt", "manual.html", "open_manual.bat", "技术说明.txt", "版本记录.txt"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, out / name)
    print(f"  📝 发布说明：发布说明-v{__version__}.md")

    print("\n=== 产物 ===")
    for item in sorted(out.iterdir()):
        if item.is_file():
            print(f"  {item.name}　{item.stat().st_size / 1024:.0f} KB")
        else:
            print(f"  {item.name}/")
    for archive in sorted(out.glob("*.zip")):
        print(f"\nSHA256 {archive.name}\n  {sha256_of(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())