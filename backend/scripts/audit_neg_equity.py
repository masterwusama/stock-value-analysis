# -*- coding: utf-8 -*-
"""资不抵债普查(只读):年报硬门槛拦截率 + 掘金台「最新披露期转负」盲区清单。

背景(2026-11-10):硬门槛 neg_equity 判「最新年报(12-31)归母权益为负」——
年报口径为负的公司已被 gate 拦进回避区;但中报/三季报才转负、年报仍为正的
公司是盲区,在掘金台按正常评分参与排序。本脚本不写库,只产出四张普查表:

  A  最新年报权益<0:按市场 × 最新批次 gate 分布(验证拦截无漏网)
  B  盲区 = 最新披露期权益<0 且最新年报≥0:按市场×报告期细分 + 明细
     (recommend/recommend_gate,≥BUY_MIN 即在买入区,是软封顶的受益者)
  C  年报负但最新期已修复转正(盐湖式;现行口径保留旗,确认其数量级)
  D  盲区公司在掘金台覆盖域(市值/上市年限过滤前)的家数合计

权益判定口径与 import_legacy.GATE_EQUITY_SQL 同源:COALESCE(equity_parent,
equity_total) < 0,两者皆 NULL 视为不可判、不计入。

用法:cd backend && python -X utf8 scripts/audit_neg_equity.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # backend/,让 app.* 可导入

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402

BUY_MIN = 65.0   # 与 pick.py BUY_MIN 对齐(买入档下限),改口径须两处同步
WATCH_MIN = 45.0  # 与 pick.py WATCH_MIN 对齐(观察档下限)

# 最新披露期(任意报告期)与最新年报(12-31)各自取「每家最新一行」,再按 sid 对齐——
# 一条 SQL 同时拿到两侧口径,盲区/修复两个方向都从这对值上判
CENSUS_SQL = """
SELECT s.sid, s.code, s.name, s.market,
       latest.report_date        AS latest_date,
       COALESCE(latest.equity_parent, latest.equity_total) AS latest_eq,
       annual.report_date        AS annual_date,
       COALESCE(annual.equity_parent, annual.equity_total) AS annual_eq
FROM security s
JOIN (SELECT sid, MAX(report_date) md FROM fin_balance GROUP BY sid) tl
      ON tl.sid = s.sid
JOIN fin_balance latest ON latest.sid = tl.sid AND latest.report_date = tl.md
LEFT JOIN (SELECT sid, MAX(report_date) md FROM fin_balance
           WHERE DATE_FORMAT(report_date, '%m-%d') = '12-31' GROUP BY sid) ta
      ON ta.sid = s.sid
LEFT JOIN fin_balance annual ON annual.sid = ta.sid AND annual.report_date = ta.md
"""

GATE_JOIN_SQL = """
SELECT sd.sid, sd.gate, sd.gate_flags, sd.recommend, sd.recommend_gate, sd.st_risk
FROM score_daily sd
JOIN (SELECT sid, MAX(trade_date) md FROM score_daily GROUP BY sid) t
  ON t.sid = sd.sid AND t.md = sd.trade_date
"""


def main():
    db = SessionLocal()
    rows = db.execute(text(CENSUS_SQL)).mappings().all()
    gates = {g["sid"]: g for g in db.execute(text(GATE_JOIN_SQL)).mappings().all()}
    td = db.execute(text("SELECT MAX(trade_date) FROM score_daily")).scalar()
    db.close()
    print(f"批次日 {td},security {len(rows)} 家(有 fin_balance 的才进得了普查)")

    # ---- A:最新年报口径(硬门槛判定域)----
    annual_neg = [r for r in rows if r["annual_eq"] is not None and r["annual_eq"] < 0]
    by_market: dict = {}
    leaked = []
    for r in annual_neg:
        g = gates.get(r["sid"])
        m = r["market"]
        stat = by_market.setdefault(m, {"gate1": 0, "gate0": 0, "gateNone": 0, "noRow": 0})
        if g is None:
            stat["noRow"] += 1
        elif g["gate"] == 1:  # pymysql 返回 int 而非 bool,不能 is True
            stat["gate1"] += 1
        elif g["gate"] == 0:
            stat["gate0"] += 1
            leaked.append(r)
        else:
            stat["gateNone"] += 1
    print(f"\n[A] 最新年报权益<0 共 {len(annual_neg)} 家,按市场 × gate 分布:")
    for m, st in sorted(by_market.items()):
        print(f"    {m}: 拦截 gate=1 {st['gate1']} / 漏网 gate=0 {st['gate0']}"
              f" / 不可判 NULL {st['gateNone']} / 无评分行 {st['noRow']}")
    if leaked:
        print("    ⚠ gate=0 漏网(应人工核查):",
              ", ".join(f"{r['code']} {r['name']}" for r in leaked[:20]))

    # ---- B:盲区 = 最新披露期<0 且最新年报≥0(软封顶目标域)----
    gap = [r for r in rows
           if r["latest_eq"] is not None and r["latest_eq"] < 0
           and r["annual_eq"] is not None and r["annual_eq"] >= 0]
    print(f"\n[B] 盲区(最新披露期<0 且最新年报≥0)共 {len(gap)} 家:")
    by_mp: dict = {}
    for r in gap:
        key = (r["market"], str(r["latest_date"]))
        by_mp[key] = by_mp.get(key, 0) + 1
    for (m, d), n in sorted(by_mp.items()):
        print(f"    {m} 报告期 {d}: {n} 家")
    if gap:
        print(f"    明细(权益亿 / 批次R / R-gate / ST / ≥{WATCH_MIN:.0f}观察区·≥{BUY_MIN:.0f}买入区):")
        for r in sorted(gap, key=lambda x: (x["market"], -float(x["latest_eq"] or 0))):
            g = gates.get(r["sid"]) or {}
            rec = g.get("recommend")
            leq = float(r["latest_eq"])
            aeq = float(r["annual_eq"])
            if rec is None:
                in_buy = ""
            elif rec >= BUY_MIN:
                in_buy = "★买入区"
            elif rec >= WATCH_MIN:
                in_buy = "☆观察区"
            else:
                in_buy = ""
            print(f"      {r['code']} {r['name']} [{r['market']}] "
                  f"{r['latest_date']} 权益 {leq / 1e8:.2f}亿"
                  f"(年报 {aeq / 1e8:.2f}亿) R={rec} "
                  f"gate={g.get('recommend_gate')} st={g.get('st_risk')} {in_buy}")

    # ---- C:年报负但最新期已修复(保留旗域,现行口径不变)----
    healed = [r for r in rows
              if r["annual_eq"] is not None and r["annual_eq"] < 0
              and r["latest_eq"] is not None and r["latest_eq"] >= 0]
    print(f"\n[C] 年报负但最新披露期已转正(盐湖式,保留旗)共 {len(healed)} 家:")
    for r in healed:
        g = gates.get(r["sid"]) or {}
        aeq = float(r["annual_eq"])
        leq = float(r["latest_eq"])
        print(f"      {r['code']} {r['name']} [{r['market']}] "
              f"年报 {aeq / 1e8:.2f}亿 → 最新 {r['latest_date']} "
              f"{leq / 1e8:.2f}亿 gate={g.get('gate')}")

    # ---- D:两口径并集(若盲区并入硬门槛/软封顶后的全域)----
    union = len(annual_neg) + len(gap)
    print(f"\n[D] 年报负 {len(annual_neg)} + 盲区 {len(gap)} = 软硬并集 {union} 家"
          f"(任一期为负但两口径皆非负的不存在:最新期即最大口径)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
