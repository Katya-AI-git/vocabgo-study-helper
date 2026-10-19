# -*- coding: utf-8 -*-
"""打发布包：整理目录 → 生成说明书与推荐配置 → 压成 zip（含校验值）。

    python build_release.py                 # 用现有 dist/Vocabgo 组装发布包
    python build_release.py --rebuild       # 先重新打包 exe 再组装
    python build_release.py --with-cli      # 连命令行版一起放进去（体积翻倍）
    python build_release.py --onefile       # 用单文件版 exe 组装（体积小、启动慢几秒）

产物：release/vocabgo-auto-v<版本>-win64/ 与同名 .zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from vocabgo import __version__                      # noqa: E402
from vocabgo.settings import RECOMMENDED, apply_recommended, load_settings  # noqa: E402

DIST = ROOT / "dist"
RELEASE = ROOT / "release"
PACKAGE_NAME = f"vocabgo-auto-v{__version__}-win64"

# 发出去之前必须为空的东西：个人配置、题库、抓包结果
FORBIDDEN_FILES = ("config.json", "bank.json", "materials.json", "captured_headers.json")
# 逐字节扫描的凭据特征（含运行库二进制一起扫）
SECRET_PATTERNS = {
    "API 密钥（sk-…）": re.compile(rb"sk-[A-Za-z0-9_\-]{16,}"),
    "Bearer 令牌": re.compile(rb"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    "疑似用户令牌": re.compile(rb'"usertoken"\s*:\s*"[A-Za-z0-9]{20,}"'),
    "疑似 abc 鉴权字段": re.compile(rb'"abc"\s*:\s*"[A-Za-z0-9]{20,}"'),
}

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def folder_size(path: Path) -> float:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) / 1024 / 1024


def scan_package(folder: Path) -> list[str]:
    """发出去之前的安全自检：包里不能有密钥、令牌、个人配置。

    逐字节扫描（连 _internal 里的二进制一起扫），所以不会漏在奇怪的地方。
    """
    problems: list[str] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if path.name in FORBIDDEN_FILES:
            problems.append(f"包含个人文件：{relative}（里面有你的密钥/令牌/题库）")
            continue
        try:
            data = path.read_bytes()
        except Exception:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                problems.append(f"{relative} 里发现{label}")
    return problems


def build_exe(onefile: bool, with_cli: bool) -> None:
    print("🔨 先重新打包程序…")
    command = [sys.executable, str(ROOT / "build_exe.py")]
    if onefile:
        command.append("--onefile")
    if not with_cli:
        command.append("--gui-only")
    subprocess.run(command, cwd=ROOT, check=True)


def write_recommended_config(target: Path) -> None:
    """按代码里的 RECOMMENDED 生成推荐配置（保证与界面按钮一致）。"""
    config = apply_recommended(load_settings())
    payload = {
        section: values
        for section, values in config.items()
        if isinstance(values, dict) and not section.startswith("_")
    }
    payload.setdefault("llm", {})["key"] = ""     # 用户密钥不写进模板
    payload.setdefault("auth", {})["usertoken"] = ""
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def assemble(onefile: bool, with_cli: bool) -> Path:
    gui_source = DIST / "Vocabgo"
    if not gui_source.exists():
        raise SystemExit("❌ 没找到 dist/Vocabgo，先跑一次 python build_exe.py（或加 --rebuild）")

    target = RELEASE / PACKAGE_NAME
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    # 1) 程序本体
    if onefile:
        exe = gui_source / "Vocabgo.exe"
        if exe.exists():
            shutil.copy2(exe, target / exe.name)
    else:
        for item in gui_source.iterdir():
            if item.name in ("config.example.json", "selftest.txt", "config.json", "bank.json"):
                continue
            if item.is_dir():
                shutil.copytree(item, target / item.name)
            else:
                shutil.copy2(item, target / item.name)

    # 2) 推荐配置（用户说明、技术说明和版本记录已由 build_exe 放入）
    write_recommended_config(target / "config.推荐.json")

    # 3) 可选：命令行版
    if with_cli and (DIST / "VocabgoCLI").exists():
        cli_target = target / "命令行版"
        cli_target.mkdir(exist_ok=True)
        for item in (DIST / "VocabgoCLI").iterdir():
            if item.name in (
                "config.example.json",
                "manual.html",
                "open_manual.bat",
                "技术说明.txt",
                "版本记录.txt",
                "selftest.txt",
                "config.json",
                "bank.json",
            ):
                continue
            if item.is_dir():
                shutil.copytree(item, cli_target / item.name)
            else:
                shutil.copy2(item, cli_target / item.name)

    return target


def make_zip(folder: Path) -> Path:
    archive = RELEASE / f"{folder.name}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for item in sorted(folder.rglob("*")):
            if item.is_file():
                zf.write(item, Path(folder.name) / item.relative_to(folder))
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="打发布包")
    parser.add_argument("--rebuild", action="store_true", help="先重新打包 exe")
    parser.add_argument("--onefile", action="store_true", help="用单文件版 exe 组装")
    parser.add_argument("--with-cli", action="store_true", help="连命令行版一起打进包里")
    parser.add_argument("--allow-secrets", action="store_true", help="安全自检发现密钥时仍继续打包（自用备份才用）")
    args = parser.parse_args()

    if args.rebuild:
        build_exe(args.onefile, args.with_cli)

    folder = assemble(args.onefile, args.with_cli)

    print("\n=== 安全自检（防止把自己的密钥打进包里）===")
    problems = scan_package(folder)
    if problems:
        for item in problems:
            print(f"  ⚠️ {item}")
        if not args.allow_secrets:
            print("\n❌ 检测到敏感内容，已中止打包（zip 不会生成）。")
            print("   处理办法：删掉上面列出的文件（一般就是 config.json），或把里面的 key / usertoken 清空后重跑。")
            print("   确实要带着打包（比如自用备份）：加 --allow-secrets")
            return 2
        print("  （已指定 --allow-secrets，继续打包 —— 请确认你不会把这个 zip 发给别人）")
    else:
        print(f"  扫描 {sum(1 for p in folder.rglob('*') if p.is_file())} 个文件，"
              "✅ 没有密钥 / 令牌 / 个人配置")

    archive = make_zip(folder)

    print("\n=== 发布包 ===")
    print(f"目录：{folder}　（{folder_size(folder):.1f} MB）")
    print(f"压缩：{archive}　（{archive.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"SHA256：{sha256_of(archive)}")
    print("\n包内清单：")
    for item in sorted(folder.iterdir()):
        if item.is_dir():
            print(f"  {item.name}/（{item.name == '_internal' and '运行库' or '目录'}）")
        else:
            print(f"  {item.name}　{item.stat().st_size / 1024:.0f} KB")
    print("\n提示：把 zip 发出去时请注明「仅供本人账号使用」，并提醒对方别把 config.json 一起分享。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
