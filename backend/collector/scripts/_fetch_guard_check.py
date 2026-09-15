# -*- coding: utf-8 -*-
"""`fetch_data.keep_last_good` 的边界检查：坏表沿用旧行，真·空表不许被旧行顶回来。

被判坏掉的那张表要沿用上一轮的行（否则评分当场塌成一片缺项），但下面三种情形必须
"这次抓到什么就是什么"：本次抓成功、本次没报错的空表（新上市公司确实没有那张表的当期数据，
把它钉在旧行上会让文件永远不再更新）、以及首次抓取（没有可沿用的文件）。

用法：python -X utf8 backend/collector/scripts/_fetch_guard_check.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_data import keep_last_good  # noqa: E402

TABLES = ("indicators", "income", "balance", "cashflow")
OLD = {"code": "600000",
       "indicators": [{"报告期": "2025-12-31", "净利润": 30}],
       "income": [{"报告日": "2025-12-31", "营业总收入": 100}] * 3,
       "balance": [{"报告日": "2025-12-31", "资产总计": 500}],
       "cashflow": [{"报告日": "2025-12-31", "经营活动产生的现金流量净额": 60}]}

# (用例名, 本次抓取结果, 有旧文件时的期望)；无旧文件时期的期望由"本次抓到什么就是什么"推出
CASES = (
    ("坏表沿用旧行、无错空表保持空",
     {"indicators": [], "income": [], "balance": [], "cashflow": [],
      "errors": ["indicators: 接口超时", "income: Expecting value: line 1 column 1 (char 0)",
                 "cashflow: 接口超时"]},
     {"lens": {"indicators": 1, "income": 3, "balance": 0, "cashflow": 1},
      "stale": {"indicators": 1, "income": 3, "cashflow": 1}, "errors": 3}),
    ("本次抓到的新行不被旧行覆盖",
     {"indicators": [{"报告期": "2026-06-30"}], "income": [{"报告日": "2026-06-30"}],
      "balance": [], "cashflow": [], "errors": ["balance: 接口超时", "cashflow: 接口超时"]},
     {"lens": {"indicators": 1, "income": 1, "balance": 1, "cashflow": 1},
      "stale": {"balance": 1, "cashflow": 1}, "errors": 2}),
    ("无错空表（真·没有这张表）不复活旧行",
     {"indicators": [], "income": [], "balance": [], "cashflow": [], "errors": None},
     {"lens": {k: 0 for k in TABLES}, "stale": {}, "errors": 0}),
    ("已有数据且无错则原样保留（错误串与财务表无关时不动）",
     {"indicators": [{"报告期": "2026-06-30"}], "income": [{"报告日": "2026-06-30"}],
      "balance": [{"报告日": "2026-06-30"}], "cashflow": [{"报告日": "2026-06-30"}],
      "errors": ["dividends: 'NoneType' object is not subscriptable"]},
     {"lens": {k: 1 for k in TABLES}, "stale": {}, "errors": 1}),
)

print("== keep_last_good 边界检查 ==")
fails = []
with tempfile.TemporaryDirectory() as tmp:
    old = Path(tmp) / "600000.json"
    old.write_text(json.dumps(OLD, ensure_ascii=False), encoding="utf-8")
    fresh = Path(tmp) / "never-fetched.json"
    for path, seen in ((old, "有上轮文件"), (fresh, "首次抓取")):
        for label, got, want in CASES:
            # 每轮一份独立副本：keep_last_good 就地改 data，用例之间不能共享
            d = json.loads(json.dumps(got))
            keep_last_good(path, d)
            if seen == "有上轮文件":
                exp = want
            else:
                exp = {"lens": {k: len(v) for k, v in got.items() if k in TABLES},
                       "stale": {}, "errors": len(got.get("errors") or [])}
            lens = {k: len(d.get(k) or []) for k in TABLES}
            stale = d.get("stale") or {}
            errs = len(d.get("errors") or [])
            if lens != exp["lens"]:
                fails.append(f"{seen} · {label} · 行数 {lens} ≠ 期望 {exp['lens']}")
            if stale != exp["stale"]:
                fails.append(f"{seen} · {label} · stale {stale} ≠ 期望 {exp['stale']}")
            if errs != exp["errors"]:
                fails.append(f"{seen} · {label} · errors {errs} 条 ≠ 期望 {exp['errors']}")
print("  全部通过：%d 个用例 × 2 种前置状态" % len(CASES) if not fails else "\n".join("  FAIL " + f for f in fails))
sys.exit(1 if fails else 0)
