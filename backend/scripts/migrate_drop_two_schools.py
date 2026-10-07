# -*- coding: utf-8 -*-
"""一次性迁移:score_daily 摘除格攻/巴菲特两派的 9 列与 2 个索引(2026-10-06)。

产品口径收两派(施洛斯/格防),依据 docs/设计-四派买点信号回测.md v2 判决书:
格防御与巴菲特两线的收益侧证据为负,格攻在近六年 A 股近乎无事件。
本地与云端各跑一次(deploy 不覆盖云端库):
    cd backend && python -X utf8 scripts/migrate_drop_two_schools.py
幂等:列/索引不存在即跳过。导入器(import_legacy)与新代码同步不再写这些列,
迁移前它们的历史值随列一并删除。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402

DROP_INDEXES = ("idx_list_graham_agg", "idx_list_buffett")
DROP_COLUMNS = (
    "score_graham_agg", "score_buffett", "fair_liq",
    "buy_graham_agg", "sell_cons_graham_agg", "sell_fair_graham_agg",
    "buy_buffett", "sell_cons_buffett", "sell_fair_buffett",
)


def main():
    with engine.connect() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'score_daily'"))}
        idxs = {r[0] for r in conn.execute(text(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'score_daily'"))}
        for ix in DROP_INDEXES:
            if ix in idxs:
                conn.execute(text(f"ALTER TABLE score_daily DROP INDEX `{ix}`"))
                print(f"dropped index {ix}")
        for c in DROP_COLUMNS:
            if c in cols:
                conn.execute(text(f"ALTER TABLE score_daily DROP COLUMN `{c}`"))
                print(f"dropped column {c}")
        conn.commit()
    left = {r[0] for r in engine.connect().execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'score_daily'"))}
    gone = [c for c in DROP_COLUMNS if c in left]
    print("残留列:", gone or "无,迁移完成")
    return 1 if gone else 0


if __name__ == "__main__":
    sys.exit(main())
