# -*- coding: utf-8 -*-
"""一次性回灌:陷阱标注制(2026-10-17)上线后,重算存量 score_daily 的 R 两列。

背景:recommend_score 的陷阱线由「拦分」改「标注」(scoring.py R_TRAP_GATE 注释)——
trap>20 不再没收 R,只把 gate 记 'trap' 交前端挂 ⚠。改动当天的快照还是旧口径
(trap 命中 ⇒ recommend=NULL),等当晚深抓重算才能自愈;本脚本用库内已物化的
V/G/fraud/trap/interim_dip 五列,喂生产同一个 recommend_score 重算 recommend 与
recommend_gate,让改动即时可见。不碰其他任何列;当晚的整链重算会原样覆盖,
两口径对同一输入产同一输出,不存在双源漂移。

用法:cd backend && python -X utf8 scripts/refresh_recommend_gate.py [--dry]
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # backend/,让 app.*/collector.* 可导入
sys.path.insert(0, str(HERE.parent / "collector" / "scripts"))  # scoring 的同级平铺 import

from app.db import SessionLocal  # noqa: E402
from app.models import entities as E  # noqa: E402
from collector.scripts.scoring import recommend_score  # noqa: E402
from sqlalchemy import select  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计,不写库")
    args = ap.parse_args()

    db = SessionLocal()
    td = db.execute(select(E.ScoreDaily.trade_date)
                    .order_by(E.ScoreDaily.trade_date.desc()).limit(1)).scalar_one()
    # recommend_gate 列是 varchar(16)，导入侧对超长串显式截头（strict 模式直写会整批回滚）；
    # 'fraud+trap+interim' 恰好 18 字符会被截成 'fraud+trap+inter'——与生产入库同一口径
    gate_lim = E.ScoreDaily.__table__.c.recommend_gate.type.length or 16
    rows = db.execute(select(E.ScoreDaily).where(E.ScoreDaily.trade_date == td)).scalars().all()
    changed = 0
    gained = 0
    for r in rows:
        rec, gate = recommend_score(r.value, r.growth, r.fraud, r.trap, r.interim_dip)
        if isinstance(gate, str) and len(gate) > gate_lim:
            gate = gate[:gate_lim]
        if rec == r.recommend and gate == r.recommend_gate:
            continue
        changed += 1
        if r.recommend is None and rec is not None:
            gained += 1
        if not args.dry:
            r.recommend = rec
            r.recommend_gate = gate
    print(f"评分日 {td}:共 {len(rows)} 行,R/gate 有变化 {changed} 行"
          f"(其中从无 R 变有 R {gained} 行)")
    if not args.dry:
        db.commit()
        print("已写库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
