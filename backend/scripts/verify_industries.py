# -*- coding: utf-8 -*-
"""行业聚合视角的对账(集合级,对齐 verify_filters 风格;只读库,零网络)。

四道检查:
  A. Σn = 当日 score⋈quote 的 A 股行数(对账基准不用 security 总数——停牌/缺快照
     的行天然少几行,用总数会把正常缺行误报成聚合丢行);
  B. 抽 3 个行业(最大/最小/中间)用独立 SQL 重算 PB/PE/PB 分位中位(正值口径)、
     施洛斯买点命中/有锚数与 ST 高危计数,与 API 响应比对;
  C. gate 通过总数与 SQL SUM(gate) 一致;
  D. 同一请求两次结果逐字节一致(纯函数性,缓存正确性的前提)。

用法:cd backend; python -X utf8 -m scripts.verify_industries
"""
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.api.industries import _latest_date, _overview  # noqa: E402
from app.db import SessionLocal  # noqa: E402

fails = []


def check(tag, cond, got=""):
    print(("  ok   " if cond else "  FAIL ") + tag + ("  " + str(got) if got != "" else ""))
    if not cond:
        fails.append(tag)


def main():
    db = SessionLocal()
    day = _latest_date(db)
    payload = _overview(db, day)
    rows = payload["rows"]
    by_ind = {r["industry"]: r for r in rows}
    print(f"样本日 {day} · 行业 {len(rows)} 类 · 个股 {payload['meta']['n_stocks']} 只")

    # ---- A. 总数对账 ----
    n_sql = db.execute(text(
        "SELECT COUNT(*) FROM security s "
        "JOIN score_daily sd ON sd.sid=s.sid AND sd.trade_date=:d "
        "JOIN quote_daily q ON q.sid=s.sid AND q.trade_date=:d WHERE s.market='A'"
    ), {"d": day}).scalar()
    check("A. Σn = 当日三表连接的 A 股行数", sum(r["n"] for r in rows) == n_sql,
          f"Σn={sum(r['n'] for r in rows)} sql={n_sql}")

    # ---- B. 三个行业的独立重算 ----
    ranked = sorted(rows, key=lambda r: r["n"])
    picks = [ranked[-1]["industry"], ranked[len(ranked) // 2]["industry"], ranked[0]["industry"]]
    for ind in picks:
        raw = db.execute(text(
            "SELECT q.pb, q.pe_ttm, q.price, sd.buy_schloss, "
            "vp.pb_pctile, sd.st_risk "
            "FROM security s "
            "JOIN score_daily sd ON sd.sid=s.sid AND sd.trade_date=:d "
            "JOIN quote_daily q ON q.sid=s.sid AND q.trade_date=:d "
            "LEFT JOIN valuation_pctile vp ON vp.sid=s.sid "
            "WHERE s.market='A' AND s.industry=:i"
        ), {"d": day, "i": ind}).all()
        pbs = sorted(float(r[0]) for r in raw if r[0] is not None and r[0] > 0)
        pes = sorted(float(r[1]) for r in raw if r[1] is not None and r[1] > 0)
        pbpcts = sorted(float(r[4]) for r in raw if r[4] is not None)

        def med(vs):
            if not vs:
                return None
            n = len(vs)
            return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2

        def eq_med(a, b):
            if a is None or b is None:
                return a is None and b is None
            return abs(a - b) < 0.01

        anchor = sum(1 for r in raw if r[3] is not None)
        hit = sum(1 for r in raw if r[3] is not None and r[2] is not None
                  and float(r[2]) <= r[3])
        got = by_ind[ind]
        ok = (got["n"] == len(raw)
              and eq_med(got["med_pb"], med(pbs)) and eq_med(got["med_pe"], med(pes))
              and got["buy"]["schloss"]["n_anchor"] == anchor
              and got["buy"]["schloss"]["hit"] == hit
              and eq_med(got["med_pb_pct"], med(pbpcts))
              and got["st_high"] == sum(1 for r in raw if (r[5] or 0) > 0))
        check(f"B. 独立重算一致[{ind[:12]}…]", ok,
              f"n={got['n']}/{len(raw)} pb={got['med_pb']}/{med(pbs)} "
              f"schloss={got['buy']['schloss']['hit']}/{hit}锚{got['buy']['schloss']['n_anchor']}/{anchor} "
              f"pbpct={got['med_pb_pct']}/{med(pbpcts)} st={got['st_high']}")

    # ---- C. gate 总数 ----
    gate_sql = db.execute(text(
        "SELECT SUM(sd.gate=1) FROM security s "
        "JOIN score_daily sd ON sd.sid=s.sid AND sd.trade_date=:d "
        "JOIN quote_daily q ON q.sid=s.sid AND q.trade_date=:d WHERE s.market='A'"
    ), {"d": day}).scalar()
    gate_api = sum(r["gate_pass"] for r in rows)
    check("C. gate 通过总数一致", gate_api == int(gate_sql or 0), f"{gate_api}/{gate_sql}")

    # ---- D. 纯函数性 ----
    again = _overview(db, day)
    check("D. 两次聚合逐字节一致",
          json.dumps(payload, sort_keys=True) == json.dumps(again, sort_keys=True))

    db.close()
    print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
