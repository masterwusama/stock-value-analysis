# -*- coding: utf-8 -*-
"""一次性修复:把库里的港股/美股估值分位行回灌进 data/valuation/latest.json。

背景:2026-10-04 fetch_valuation_em 首跑时还是「整文件覆盖」语义,把 Wind 时代
写入的 HK:* 条目从采集产物里抹掉了(DB 靠 upsert 不删还留着 09-25 的旧行)。
本脚本从 valuation_pctile 读非 A 股行重建条目并 merge 回 latest.json,让
「采集产物 = 全量累计快照」的对账基线恢复(verify_filters 的 pbp 探针靠它)。
fetch_valuation_em 已改为写前 merge,本脚本只需在存量修复时各跑一次
(本地一台、云端一台;云端 latest.json 是服务器自有数据,deploy 不覆盖)。

用法:cd backend && python -X utf8 scripts/repair_valuation_latest.py [--dry]
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # backend/,让 app.* 可导入

from app.db import SessionLocal  # noqa: E402
from app.models import entities as E  # noqa: E402
from sqlalchemy import select  # noqa: E402

LATEST = HERE.parent / "collector" / "data" / "valuation" / "latest.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印将写入的条目数,不落盘")
    args = ap.parse_args()

    db = SessionLocal()
    rows = db.execute(
        select(E.Security.code, E.Security.market, E.ValuationPctile)
        .join(E.ValuationPctile, E.ValuationPctile.sid == E.Security.sid)
        .where(E.Security.market != "A")
    ).all()
    items = {}
    for code, market, vp in rows:
        item = {"trade_date": vp.trade_date.isoformat(),
                "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "src": "wind"}
        for m in ("pe", "pb", "ps"):
            pct = getattr(vp, f"{m}_pctile")
            if pct is not None:
                item[m] = {"pct": pct, "days": getattr(vp, f"{m}_days"),
                           "date": vp.trade_date.isoformat()}
        if any(m in item for m in ("pe", "pb", "ps")):
            items[f"{market}:{code}"] = item

    old = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {}
    old_items = old.get("items") or {}
    merged = dict(old_items)
    for k, v in items.items():
        merged[k] = v  # 库里是权威存量,非 A 股 EM 反正不覆盖,直接压过
    print(f"库内非 A 股分位行 {len(items)} 条;latest.json 现有 {len(old_items)} 条 "
          f"→ 合并后 {len(merged)} 条(新增 {len(merged) - len(old_items)})")
    if args.dry:
        return 0
    payload = {"fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
               "src": old.get("src") or "em", "items": merged}
    tmp = LATEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, LATEST)
    print(f"已写 {LATEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
