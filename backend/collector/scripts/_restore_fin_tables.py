# -*- coding: utf-8 -*-
"""把采集失败抹空的财务表从 MySQL 补回 companies/*.json（只补整张为空的表）。

为什么需要：`fetch_company_*` 对四张表各自 try/except，一次失败就把该表写成 `[]`
并记进 `errors`，上一轮成功抓到的行被就地抹掉。评分正是建在这些行上——抹空之后
格防/施洛斯/巴菲特的多项一起塌成缺项，列表分数看起来像“公司变差了”，而实际上只是
我们抓不到了。库里 `fin_indicator/fin_income/fin_balance/fin_cashflow` 的 `extras`
列存着原始中文科目行，所以不必重抓全市场（数小时）就能把明细补回来。

边界（三条都不越）：
- 只写“JSON 里这张表为空”且“库里有行”的那张表，有数据的表一律不覆盖（抓新数据是采集的活）；
- 库里也没有的公司（多为从未抓成功）保持原样并报出来，不拿空表冒充恢复；
- 默认试运行，`--apply` 才写文件，写之前逐个备份到 `_tmp/fin-restore-<时刻>/`。

恢复来源与期次写进 JSON 的 `restored` 字段（行数/最新报告期/来源表/恢复时刻），同时把
对应的 `<表>: ...` 错误串去掉——数据已到位，留着会让 `_selfcheck.py` 一直红在已修复的项上。

用法（任意 cwd）：
    python -X utf8 backend/collector/scripts/_restore_fin_tables.py
    python -X utf8 backend/collector/scripts/_restore_fin_tables.py --apply
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

from sqlalchemy import select, tuple_  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    FinBalance, FinCashflow, FinIncome, FinIndicator, Security,
)

DATA = HERE.parent / "data"
COMPANIES = DATA / "companies"
LOCK = DATA / ".fetch.lock"
REPO = BACKEND.parent
BATCH = 200
# 与 fetch_data.MAX_PERIODS 同窗口：多出来的老期评分用不到，只会让文件比抓取产出更大
MAX_PERIODS = 40
TABLES = (("indicators", FinIndicator), ("income", FinIncome),
          ("balance", FinBalance), ("cashflow", FinCashflow))
MODELS = dict(TABLES)


def scan():
    """空表清单 → [(path, code, market, [表名])]，外加扫描文件数。

    判空只看 JSON 自己，不按市场挑：三个市场的 `fetch_company_*` 写坏的方式相同。
    市场取 JSON 自带的 `market`（三个 fetch_company_* 都会写），缺了才回退 index.json。
    """
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    mkt = {c.get("code"): c.get("market") for c in idx.get("companies") or []}
    out, n = [], 0
    for path in sorted(COMPANIES.glob("*.json")):
        n += 1
        d = json.loads(path.read_text(encoding="utf-8"))
        empties = [key for key, _ in TABLES if not d.get(key)]
        if empties:
            out.append((path, d.get("code"), d.get("market") or mkt.get(d.get("code")) or "A",
                        empties))
    return out, n


def db_rows(db, pairs):
    """[(code, market)] → {(code, market): {表名: [原始行]}}，每表按报告期倒序取最近 MAX_PERIODS 期。

    用 (code, market) 组合键而不是裸 code：跨市场代码长度虽一般不同，但恢复错公司是
    把别人的财报写进这个文件，代价远比多写一个条件贵。
    """
    want = {}
    for key, model in TABLES:
        got = {}
        stmt = (select(Security.code, Security.market, model.extras)
                .join(model, model.sid == Security.sid)
                .where(tuple_(Security.code, Security.market).in_(pairs))
                .order_by(Security.code, model.report_date.desc()))
        for code, market, ex in db.execute(stmt):
            rows = got.setdefault((code, market), [])
            if len(rows) < MAX_PERIODS and ex:
                rows.append(dict(ex))
        want[key] = got
    return want


def newest_day(rows):
    for r in rows:
        day = str(r.get("报告日") or r.get("报告期") or "")[:10]
        if day:
            return day
    return None


def main():
    ap = argparse.ArgumentParser(description="从 MySQL 补回被采集失败抹空的财务表")
    ap.add_argument("--apply", action="store_true", help="真正写文件（默认只试运行报数）")
    args = ap.parse_args()

    if LOCK.exists():
        pid = ""
        try:
            pid = LOCK.read_text(encoding="utf-8").strip()[:60]
        except OSError:
            pass
        print(f"采集中（{LOCK} 存在，{pid}）：先等这一轮跑完再恢复，否则会把抓取中的文件改花")
        return 2

    targets, total = scan()
    print(f"扫描 {total} 个公司 JSON，{len(targets)} 个存在空表")
    if not targets:
        return 0

    db = SessionLocal()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = REPO / "_tmp" / f"fin-restore-{stamp}"
    done = {}
    nothing = []
    try:
        for i in range(0, len(targets), BATCH):
            chunk = targets[i:i + BATCH]
            got = db_rows(db, [(c[1], c[2]) for c in chunk])
            for path, code, market, empties in chunk:
                d = json.loads(path.read_text(encoding="utf-8"))
                errors = [str(e) for e in (d.get("errors") or [])]
                restored, key = {}, (code, market)
                for tbl in empties:
                    rows = (got.get(tbl) or {}).get(key) or []
                    if not rows:
                        nothing.append(f"{market}:{code}.{tbl}")
                        continue
                    d[tbl] = rows
                    errors = [e for e in errors if not e.startswith(tbl + ":")]
                    restored[tbl] = {"rows": len(rows), "newest": newest_day(rows),
                                      "source": MODELS[tbl].__tablename__, "at": stamp}
                if not restored:
                    continue
                d["restored"] = {**(d.get("restored") or {}), **restored}
                d["errors"] = errors or None
                for tbl in restored:
                    done[tbl] = done.get(tbl, 0) + 1
                if args.apply:
                    backup.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup / f"{code}.json")
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")),
                                   encoding="utf-8")
                    tmp.replace(path)
    finally:
        db.close()

    print(f"恢复表数: {done}（每家每表最多 {MAX_PERIODS} 期）")
    print(f"库里也无数据、保持原样的: {len(nothing)} 张表 {sorted(set(x.split('.')[0] for x in nothing))[:10]}")
    print(("已写入，原文件备份在 " + str(backup)) if args.apply else "试运行：未写任何文件（加 --apply 才写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
