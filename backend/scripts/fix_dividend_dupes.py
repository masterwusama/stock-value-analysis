# -*- coding: utf-8 -*-
"""清洗 dividend 双源双计：巨潮(主源)与东财兜底对同一笔分红用过不同 div_type，
唯一键 (sid,div_year,div_type) 于是同一笔落两行，per_share_div 按年求和把股息率/
派息率翻倍（2026-09-30 实测 A 股 320 组三季报 + 20 组一季报双行，金额完全相同）。

三步：
1. companies/*.json（A 股）：div_year 后缀 一季报/三季报 而 type=中期分红 的行
   改名 季度分红（对齐巨潮事件词表，fetch_data._div_year_label 已同步改）；
   同键同额的重复行留一条。
2. DB dividend（A 股）：巨潮孪生行（季度分红）已存在的东财行（中期分红）直接删；
   没有孪生的改名——改名而非删除，防止下一轮全量导入前这两类分数引用空窗。
3. 港股不动：fetch_hk_dividends 已改为 type 逐笔取分配类型 + 美元折港币，全量
   重抓时同键自然覆盖（键一致才覆盖，改名行靠上面第 2 步处理的是 A 股）。

用法(在 backend 目录下):
    python -m scripts.fix_dividend_dupes --dry-run   # 只统计不改
    python -m scripts.fix_dividend_dupes             # 改 JSON + 清 DB
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.config import LEGACY_DATA_DIR  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Dividend, Security  # noqa: E402

COMPANIES = LEGACY_DATA_DIR / "data" / "companies"
CANON = {"一季报": "季度分红", "三季报": "季度分红"}


def fix_rows(divs):
    """词表改名 + 同事件去重。返回 (新列表, 是否变更)。

    去重两层：同 (year,type) 同额同除净日（双计行）；跨 type 同 (year,额,除净日)
    ——巨潮把同一笔分红按「季度分红+特别分红」双记过（实测 301381 2023三季报，
    金额/除净日/方案文本全同），留先出现的常规类型那条。
    """
    changed = False
    seen_key = {}
    seen_event = {}
    out = []
    for r in divs:
        r = dict(r)
        suf = str(r.get("year") or "")[4:]
        if suf in CANON and r.get("type") == "中期分红":
            r["type"] = CANON[suf]
            changed = True
        key = (r.get("year"), r.get("type"))
        sig = (r.get("bonus_per_10"), r.get("ex_date"))
        if key in seen_key and seen_key[key] == sig:
            changed = True      # A 股一期一方案，同键同额即双计行
            continue
        event = (r.get("year"), sig)
        if sig != (None, None) and event in seen_event and seen_event[event] != "特别分红":
            changed = True      # 跨类型同事件：保留先出现的常规类型那条
            continue
        seen_key[key] = sig
        seen_event.setdefault(event, r.get("type"))
        out.append(r)
    return (out if changed else divs), changed


def main():
    ap = argparse.ArgumentParser(description="清洗 dividend 双源双计")
    ap.add_argument("--dry-run", action="store_true", help="只统计不改")
    args = ap.parse_args()

    # ---- 1. companies/*.json（A 股）----
    n_files = n_rows_renamed = n_rows_dropped = 0
    for path in sorted(COMPANIES.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("market") != "A":
            continue            # 港股重抓覆盖，不在本脚本范围
        new_rows, changed = fix_rows(d.get("dividends") or [])
        if not changed:
            continue
        old_n = len(d.get("dividends") or [])
        n_files += 1
        n_rows_dropped += old_n - len(new_rows)
        n_rows_renamed += sum(
            1 for a, b in zip(d.get("dividends") or [], new_rows) if a.get("type") != b.get("type"))
        d["dividends"] = new_rows
        if not args.dry_run:
            path.write_text(
                json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # ---- 2. DB dividend（A 股）----
    # MySQL 1093：DELETE/UPDATE 目标表不能出现在子查询里，孪生判断走多表 JOIN；
    # 多表 UPDATE 的 SET 落在 JOIN 之后、WHERE 之前；LIKE 改 RIGHT() 谓词避开
    # text() 的 % 转义
    db = SessionLocal()
    cond = ("d.div_type = '中期分红' AND RIGHT(d.div_year, 3) IN ('一季报', '三季报')"
            " AND d.sid IN (SELECT sid FROM security WHERE market = 'A')")
    cnt_del = ("SELECT COUNT(*) FROM dividend d JOIN dividend d2"
               " ON d2.sid = d.sid AND d2.div_year = d.div_year"
               " AND d2.div_type = '季度分红' WHERE " + cond)
    cnt_upd = ("SELECT COUNT(*) FROM dividend d LEFT JOIN dividend d2"
               " ON d2.sid = d.sid AND d2.div_year = d.div_year"
               " AND d2.div_type = '季度分红' WHERE " + cond + " AND d2.sid IS NULL")
    if args.dry_run:
        n_del = db.execute(text(cnt_del)).scalar()
        n_upd = db.execute(text(cnt_upd)).scalar()
    else:
        n_del = db.execute(text(
            "DELETE d FROM dividend d JOIN dividend d2"
            " ON d2.sid = d.sid AND d2.div_year = d.div_year"
            " AND d2.div_type = '季度分红' WHERE " + cond)).rowcount
        n_upd = db.execute(text(
            "UPDATE dividend d LEFT JOIN dividend d2"
            " ON d2.sid = d.sid AND d2.div_year = d.div_year"
            " AND d2.div_type = '季度分红' SET d.div_type = '季度分红' WHERE "
            + cond + " AND d2.sid IS NULL")).rowcount
        db.commit()
    db.close()

    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}JSON: {n_files} 个文件改名 {n_rows_renamed} 行 / 去重 {n_rows_dropped} 行")
    print(f"{mode}DB: 孪生双行删 {n_del} 行 / 无孪生改名 {n_upd} 行")


if __name__ == "__main__":
    main()
