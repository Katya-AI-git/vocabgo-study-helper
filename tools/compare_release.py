# -*- coding: utf-8 -*-
"""对比「运行版」与「脱敏发布版」的运行时取值是否完全一致。

意义：脱敏工具只能改写法，不能改行为。
源码文本必然不同（字符串被转成 \\uXXXX 转义、包名改成 vgb、标识符改名），
但只要**运行时取到的值**逐项相同，就说明脱敏没有伤到功能。
"""

from __future__ import annotations

import json
import subprocess
import sys

PY = r"C:\Users\a\Desktop\cidaren-wechat-auto\.venv\Scripts\python.exe"

PROBE = r'''
import sys, json
sys.path.insert(0, ROOT)
from PKG.client import CLASS_BASE, STUDY_BASE, DEFAULT_UA, DERIVED_AUTH_V
from PKG.crypto import VERSION, SALT, JV_RULES
from PKG import glossary
from PKG import templates
from PKG.solver import MAX_TOPIC_RETRIES, MAX_RESYNCS
from PKG.version import __version__

# 字典模板题：mode 51 补全单词
fill_topic = {"topic_mode": 51, "stem": {"content": "{}  with", "remark": "y"}, "w_tip": "str", "w_lens": [8]}
# 多空首字母：mode 73
spell_topic = {"topic_mode": 73, "stem": {"content": "{ze} {wa}", "w_lens": [4, 5]}}

out = {
    "CLASS_BASE": CLASS_BASE,
    "STUDY_BASE": STUDY_BASE,
    "UA": DEFAULT_UA,
    "AUTH_V": DERIVED_AUTH_V,
    "VERSION": VERSION,
    "SALT": SALT,
    "JV_KEYS": sorted(JV_RULES.keys()),
    # 脱敏时这个常量改了名（YOUDOU_API → DICT_API），两边取值必须指向同一个地址
    "DICT_API": getattr(glossary, "DICT_API", None) or getattr(glossary, "YOUDOU_API", None),
    "MAX_TOPIC_RETRIES": MAX_TOPIC_RETRIES,
    "MAX_RESYNCS": MAX_RESYNCS,
    "version": __version__,
    "mode51": templates.solve_fill_word(fill_topic, ["struggle"]),
    "mode73": templates.solve_spelling(spell_topic, ["zero", "waste"]),
}
print(json.dumps(out, ensure_ascii=False))
'''


def probe(root: str, pkg: str) -> dict:
    code = PROBE.replace("ROOT", repr(root)).replace("PKG", pkg)
    result = subprocess.run(
        [PY, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        return {"__error__": (result.stderr or "")[-600:]}
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    run = probe(r"C:\Users\a\Desktop\cidaren-wechat-auto", "vocabgo")
    rel = probe(r"C:\Users\a\Desktop\cidaren-wechat-auto-release", "vgb")

    if "__error__" in run or "__error__" in rel:
        print("探测失败：")
        print(run.get("__error__") or rel.get("__error__"))
        return 1

    print(f"{'项目':<20}{'运行版':<34}{'发布版':<34}判定")
    print("-" * 104)
    same = 0
    for key in run:
        left, right = str(run[key]), str(rel.get(key))
        ok = run[key] == rel.get(key)
        same += ok
        mark = "OK" if ok else "差异"
        print(f"{key:<20}{left[:32]:<34}{right[:32]:<34}{mark}")
    print("-" * 104)
    print(f"运行时取值一致：{same}/{len(run)}")
    return 0 if same == len(run) else 2


if __name__ == "__main__":
    raise SystemExit(main())
