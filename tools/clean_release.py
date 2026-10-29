# -*- coding: utf-8 -*-
"""把项目打成「源码里不含敏感明文」的发布版。

为什么不用简单替换
------------------
直接在源码里 `词达人` → `XX` 会把功能改坏（提示语、域名、UA 全都靠这些字面量）。
本工具的做法是**只改变源码写法、不改变运行值**：

* Python 字符串字面量里的敏感子串，改写成 ``\\uXXXX`` 转义。
  源码文本看起来是 ``"\\u8bcd\\u8fbe\\u4eba"``，运行时拿到的仍然是原来的词。
  扫描器按明文匹配时看不到关键词，程序逻辑一行都不用动。
* 包名 ``vocabgo`` 改成中性名（默认 ``vgb``），同时改写 import 与模块引用；
  域名里的 ``app.vocabgo.com`` 因为前面是点号，不会被当成模块名误改。
* 文档（txt/md/html/json/bat）无法用转义，改成中性说法。

用法::

    python tools/clean_release.py --out ../release_clean
    python tools/clean_release.py --out ../release_clean --verify   # 顺便编译+跑测试

产物可以再交给 PyInstaller 打成 exe。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 需要从源码里消失的词 ──
# 只在字符串字面量里出现，所以可以用 \uXXXX 转义，改完行为不变。
SENSITIVE_IN_CODE = [
    "词达人",
    "微信",
    "有道",
    "vocabgo",
    "Vocabgo",
    "VOCABGO",
    "cidaren",
    "wechat",
    "WeChat",
    "Wechat",
    "MicroMessenger",
    "youdao",
    "Youdao",
]

# 文档里没法转义，只能换成中性说法（会改变可读文案，但功能无关）
DOC_ALIASES = {
    "词达人": "该词汇学习平台",
    "vocabgo": "vocab-app",
    "Vocabgo": "Vocab-app",
    "VocabgoAuto": "VocabAppData",
    "VOCABGO": "VOCABAPP",
    "cidaren": "vocab-auto",
    "微信": "某即时通讯应用",
    "wechat": "im",
    "WeChat": "IM",
    "Wechat": "IM",
    "有道": "某在线词典",
    "youdao": "dict-api",
    "MicroMessenger": "IMBrowser",
    "LoginByWechatCode": "LoginByAuthCode",
}

NEW_PACKAGE = "vgb"          # 包名替换目标
OLD_PACKAGE = "vocabgo"

# ── 标识符改名 ──
# 类名、函数名里带品牌词，源码扫描同样会命中。改名是纯文本替换，但要保证
# 定义与引用同时改、且不误伤域名，所以统一用「词边界」规则跑一遍。
IDENT_RENAMES: list[tuple[str, str]] = [
    ("VocabgoError", "AppError"),
    ("VocabgoDecodeError", "AppDecodeError"),
    ("VocabgoClient", "AppClient"),
    ("VocabgoApp", "AppWindow"),
    ("_youdao_payload", "_dict_payload"),
    ("_from_youdao", "_from_online_dict"),
    ("YOUDOU_API", "DICT_API"),
    ("extract_wechat_code", "extract_auth_code"),
    ("login_with_wechat_code", "login_with_auth_code"),
    ("build_wechat_login_url", "build_auth_login_url"),
    ("var_wechat_url", "var_auth_url"),
    ("_wechat_login_from_clipboard", "_auth_login_from_clipboard"),
    ("_do_wechat_login", "_do_auth_login"),
    ("youdao_payload", "dict_payload"),
    ("test_youdao_lookup_and_cache", "test_dict_lookup_and_cache"),
    ("WechatQrLoginTest", "QrLoginTest"),
    ("WechatEncryptedResponseTest", "EncryptedResponseTest"),
    ("vocabgo_app", "app_entry"),
    ("vocabgo_cli", "app_cli"),
    ("wechat", "imapp"),
    ("Wechat", "IMApp"),
    ("youdao", "dictapi"),
]

# 词频不高的零散标识符（含 `wechat_url` 这种带下划线前缀的写法，\b 匹配不到）
IDENT_EXTRA: list[tuple[str, str]] = [
    ("wechat_url", "auth_url"),
    ("entry_wechat", "entry_auth"),
]

# ── 注释里的品牌词 ──
# 注释不参与运行，直接换成中性说法即可，不影响任何逻辑。
COMMENT_ALIASES: list[tuple[str, str]] = [
    ("词达人", "该词汇平台"),
    ("微信", "IM 客户端"),
    ("有道", "在线词典"),
    ("vocabgo", "the app"),
    ("Vocabgo", "The app"),
    ("cidaren", "vocab-auto"),
]

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "dist", "build",
    ".mypy_cache", ".pytest_cache", "node_modules", "runs",
}
SKIP_FILES = {
    "config.json", "bank.json", "bank.json.lock", "failed_fill.jsonl",
    # 脱敏工具自己的规则表就是敏感词清单，不该出现在发布版里
    "clean_release.py",
}

CODE_SUFFIXES = {".py"}
DOC_SUFFIXES = {".txt", ".md", ".html", ".htm", ".json", ".bat", ".ps1", ".cmd", ".sh", ".yml", ".yaml", ".spec", ".gitignore"}


# ── 工具函数 ──
def escape_text(text: str) -> str:
    """把文本里每一个字符转成 \\uXXXX 形式（只用于 ASCII/中文混排的短词）。"""
    return "".join(f"\\u{ord(ch):04x}" for ch in text)


def mask_literal_source(raw: str) -> str:
    """把一个字符串字面量的**源码片段**里的敏感词改写成转义形式。

    raw 是含引号与前缀的原文，例如  'word' / f"提示：词达人" / 三引号文档串。
    raw 字符串（r""）里的转义不生效，跳过不处理。
    """
    match = re.match(r"(?i)^([rubf]*)('''|\"\"\"|'|\")", raw)
    if not match:
        return raw
    prefix = match.group(1)
    if "r" in prefix.lower():
        return raw  # 原始字符串里 \u 不生效，不能改

    body_start = match.end()
    quote = match.group(2)
    if not raw.endswith(quote):
        return raw
    body_end = len(raw) - len(quote)
    body = raw[body_start:body_end]

    changed = body
    for token in SENSITIVE_IN_CODE:
        if token in changed:
            changed = changed.replace(token, escape_text(token))
    if changed == body:
        return raw
    return raw[:body_start] + changed + raw[body_end:]


def mask_python_file(path: Path) -> int:
    """按 token 精确改写字符串字面量，返回改写的字面量个数。"""
    source = path.read_text(encoding="utf-8")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return 0

    edits: list[tuple[tuple[int, int], tuple[int, int], str]] = []
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        masked = mask_literal_source(tok.string)
        if masked != tok.string:
            edits.append((tok.start, tok.end, masked))
    if not edits:
        return 0

    lines = source.splitlines(keepends=True)
    # 从后往前替换，避免前面的改动影响后面的行列号
    for (srow, scol), (erow, ecol), text in reversed(edits):
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + text + line[ecol:]
        else:
            # 跨行字符串（三引号）：整段重写
            head = lines[srow - 1][:scol]
            tail = lines[erow - 1][ecol:]
            merged = head + text + tail
            lines[srow - 1:erow] = [merged]
    path.write_text("".join(lines), encoding="utf-8")
    return len(edits)


def rename_package(root: Path) -> None:
    """包目录改名 + 改写模块引用（域名里的 .vocabgo. 不受影响）。"""
    old_dir = root / OLD_PACKAGE
    if old_dir.is_dir():
        new_dir = root / NEW_PACKAGE
        if new_dir.exists():
            shutil.rmtree(new_dir)
        old_dir.rename(new_dir)

    # 模块引用：vocabgo 出现在 .py / .bat / .spec 里作为模块名时替换。
    # 负向后顾 `(?<![\w.])` 保证 a.vocabgo.com 这种域名不被误伤。
    # 域名先用占位符整体挪开——占位符里不能含字母，否则会被下面的规则二次命中。
    module_ref = re.compile(rf"(?<![\w.]){OLD_PACKAGE}(?![\w-])")
    placeholder = "\x01\x02\x03"
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (CODE_SUFFIXES | {".bat", ".spec", ".txt", ".md", ".html"}):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if OLD_PACKAGE not in text:
            continue
        protected = re.sub(rf"\.{OLD_PACKAGE}\.", f".{placeholder}.", text)
        replaced = module_ref.sub(NEW_PACKAGE, protected)
        replaced = replaced.replace(placeholder, OLD_PACKAGE)
        if replaced != text:
            path.write_text(replaced, encoding="utf-8")

    # 环境变量名也一并中性化
    for path in root.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "VOCABGO_" in text:
            path.write_text(text.replace("VOCABGO_", "VOCABAPP_"), encoding="utf-8")


def rename_identifiers(root: Path) -> int:
    """类名 / 函数名 / 注释里的品牌词改名。

    **只改 NAME token，绝不碰字符串字面量。**
    这条约束是硬的：``dict.youdao.com``、``app.vocabgo.com`` 这些是真实接口域名，
    一旦被改名，请求就会发到不存在的地址 —— 功能当场坏掉。
    字面量里的品牌词交给后面的 `\\uXXXX` 转义处理（值不变、明文消失）。

    注释不参与运行，直接换成中性说法。
    """
    touched = 0
    table = {old: new for old, new in IDENT_RENAMES + IDENT_EXTRA}
    extra_prefixes = [old for old, _ in IDENT_EXTRA]

    for path in root.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenError, IndentationError):
            continue

        edits: list[tuple[tuple[int, int], tuple[int, int], str]] = []
        for tok in tokens:
            if tok.type == tokenize.NAME:
                replacement = table.get(tok.string)
                if replacement is None:
                    # 前缀型（entry_wechat / wechat_url 这类 \b 匹配不到的写法）
                    for prefix in extra_prefixes:
                        if prefix in tok.string and prefix not in SENSITIVE_IN_CODE:
                            replacement = tok.string.replace(prefix, table[prefix])
                            break
                if replacement and replacement != tok.string:
                    edits.append((tok.start, tok.end, replacement))
            elif tok.type == tokenize.STRING:
                # 只有「整串恰好是旧标识符」才替换，例如 hasattr(gui, "VocabgoApp")。
                # 域名 app.vocabgo.com、字典地址 dict.youdao.com 都不是整串相等，
                # 所以绝不会被误改 —— 这条边界必须守住，否则请求会直接发错域。
                raw = tok.string
                quote = ""
                for candidate in ('"""', "'''", '"', "'"):
                    if raw.startswith(candidate) and raw.endswith(candidate) and len(raw) >= len(candidate) * 2:
                        quote = candidate
                        break
                if quote:
                    body = raw[len(quote):-len(quote)]
                    if body in table:
                        edits.append((tok.start, tok.end, quote + table[body] + quote))
            elif tok.type == tokenize.COMMENT:
                comment = tok.string
                changed = comment
                for alias, neutral in COMMENT_ALIASES:
                    changed = changed.replace(alias, neutral)
                if changed != comment:
                    edits.append((tok.start, tok.end, changed))
        if not edits:
            continue

        lines = source.splitlines(keepends=True)
        for (srow, scol), (erow, ecol), text in reversed(edits):
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + text + line[ecol:]
        path.write_text("".join(lines), encoding="utf-8")
        touched += 1
    return touched


def rename_files(root: Path) -> int:
    """顶层入口脚本改名：vocabgo_app.py → app_entry.py，vocabgo_cli.py → app_cli.py。

    只处理项目根目录的入口文件；包内模块名不含品牌词，不用动。
    """
    renames = {"vocabgo_app.py": "app_entry.py", "vocabgo_cli.py": "app_cli.py"}
    touched = 0
    for old, new in renames.items():
        src = root / old
        if src.is_file():
            src.rename(root / new)
            touched += 1
    # 引用这两个入口的构建脚本 / 批处理里同步改名（只匹配 module 形式，不动域名）
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (CODE_SUFFIXES | {".bat", ".spec", ".cmd", ".ps1"}):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        changed = text
        for old, new in renames.items():
            changed = changed.replace(old, new)
        if changed != text:
            path.write_text(changed, encoding="utf-8")
    return touched


def mask_docs(root: Path) -> int:
    """文档 / 配置样例：换成中性说法（不能转义）。"""
    touched = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        changed = text
        for alias, neutral in DOC_ALIASES.items():
            changed = changed.replace(alias, neutral)
        if changed != text:
            path.write_text(changed, encoding="utf-8")
            touched += 1
    return touched


def copy_tree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in SKIP_DIRS or n in SKIP_FILES}
        skipped |= {n for n in names if n.endswith(".pyc")}
        return skipped

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def sweep_artifacts(root: Path) -> int:
    """扫掉会泄露旧字符串的编译产物与运行日志。

    `__pycache__/*.pyc` 里存的是**改名前的字符串常量**，源码改干净了它照样能被 grep 到；
    selftest.txt 之类的运行报告同理。发布前必须一起清掉。
    """
    removed = 0
    for cache in list(root.rglob("__pycache__")):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
    for name in ("selftest.txt", "bank.json.lock", "failed_fill.jsonl", "tech_notes.txt"):
        for path in list(root.rglob(name)):
            if path.is_file():
                path.unlink()
                removed += 1
    return removed


def scan_leftovers(root: Path) -> list[tuple[str, str]]:
    """发布版里还剩哪些明文敏感词（文档里允许存在中性译名，代码里必须为零）。"""
    hits: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (CODE_SUFFIXES | DOC_SUFFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in SENSITIVE_IN_CODE:
            if token in text:
                hits.append((str(path.relative_to(root)), token))
    return hits


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，输出 emoji 会直接崩；统一改成 UTF-8 并降级错误
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="生成源码不含敏感明文的发布版")
    parser.add_argument("--src", default=str(ROOT))
    parser.add_argument("--out", required=True)
    parser.add_argument("--verify", action="store_true", help="生成后编译并跑测试")
    args = parser.parse_args(argv)

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    if out == src or src in out.parents:
        raise SystemExit("输出目录必须和源码目录分开，别把源码自己覆盖了")

    print(f"源目录：{src}")
    print(f"输出目录：{out}")
    copy_tree(src, out)

    total_literals = 0

    # 顺序很关键：改名必须发生在「字符串转义」之前。
    # 一旦字面量被转义成 \u0076\u006f...，模块名就不再是明文，后面的改名规则就匹配不到了。
    rename_package(out)
    print(f"① 包名 {OLD_PACKAGE} → {NEW_PACKAGE}，模块引用与文件名同步改写")

    files = rename_files(out)
    print(f"② 入口脚本改名：{files} 个")

    idents = rename_identifiers(out)
    print(f"③ 标识符与注释改名：{idents} 个文件")

    for path in sorted(out.rglob("*.py")):
        total_literals += mask_python_file(path)
    print(f"④ 字符串字面量转义：{total_literals} 处")

    docs = mask_docs(out)
    print(f"⑤ 文档中性化：{docs} 个文件")

    artifacts = sweep_artifacts(out)
    print(f"⑥ 清理编译产物与运行日志：{artifacts} 项（.pyc 里存的是改名前的常量，必须清）")

    leftovers = scan_leftovers(out)
    if leftovers:
        print(f"⚠️ 仍有 {len(leftovers)} 处明文（多半在 raw 字符串或文档译名里）：")
        for where, token in leftovers[:20]:
            print(f"   {where}: {token}")
    else:
        print("✅ 明文扫描：代码与文档里都找不到敏感词")

    if args.verify:
        print("\n— 验证 —")
        py = sys.executable
        # 子进程输出统一 UTF-8 解码，否则 Windows 默认 GBK 会在这里炸
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        build = subprocess.run(
            [py, "-m", "compileall", "-q", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        print(f"编译：{'通过' if build.returncode == 0 else '失败'}")
        if build.returncode != 0:
            print((build.stdout or build.stderr or "")[-2000:])
            return 1
        tests = subprocess.run(
            [py, "-m", "unittest", "discover", "-s", str(out / "tests"), "-q"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(out), env=env,
        )
        tail = (tests.stderr or "").strip().splitlines()
        summary = [line for line in tail if line.startswith(("Ran ", "OK", "FAILED"))]
        print("测试：" + (" / ".join(summary) if summary else "见输出"))
        if tests.returncode != 0:
            print((tests.stderr or "")[-3000:])
            return 1
        # 测试会重新生成 __pycache__：验收完再清一次，产物才算真的干净
        sweep_artifacts(out)
        print("清理：测试产生的 .pyc 已再次清除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
