# -*- coding: utf-8 -*-
"""净有息负债/市值 普查(只读):为掘金台三级降档(2026-11-10)校准阈值。

口径 = score_daily.net_cash_w(加权类现金:货币资金+交易性金融资产+应收票据×0.4
+定期存款等 − 五桶有息负债,本币元)÷ quote_daily.market_cap(同币种原币市值)
——与 pick.py 即将上线的降档指标同一公式;宽口径 net_cash_ratio(减负债合计)
不进本普查。NULL=算不出(银行无货币资金科目等),单列一档不罚。

分级预览(与 pick.py 常量同步,改须两处):
  灰带扣分  ncw < -0.30(线性到 -0.55 扣满 -10)
  封顶 watch ncw < -1.00
  封顶 na   ncw < -2.00

用法:cd backend && python -X utf8 scripts/audit_net_cash.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # backend/,让 app.* 可导入

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402

GRAY_FROM = -0.30
CAP_WATCH = -1.00
CAP_NA = -2.00
BUY_MIN, WATCH_MIN, STRONG_MIN = 65.0, 45.0, 78.0  # pick.py 同步

CENSUS_SQL = """
SELECT s.sid, s.code, s.name, s.market, s.industry,
       sd.recommend, sd.recommend_gate, sd.st_risk, sd.gate,
       sd.net_cash_w, sd.net_cash_ratio,
       q.market_cap, q.trade_date AS qdate
FROM score_daily sd
JOIN (SELECT sid, MAX(trade_date) md FROM score_daily GROUP BY sid) t
  ON t.sid = sd.sid AND t.md = sd.trade_date
JOIN security s ON s.sid = sd.sid
LEFT JOIN quote_daily q ON q.sid = sd.sid
   AND q.trade_date = (SELECT MAX(trade_date) FROM quote_daily x WHERE x.sid = sd.sid)
WHERE s.market IN ('A', 'HK')
"""

BUCKETS = (
    ("≥0(净现金)", lambda r: r["ncw"] is not None and r["ncw"] >= 0),
    ("0~-30%(轻杠杆,无感)", lambda r: r["ncw"] is not None and -0.30 <= r["ncw"] < 0),
    ("-30~-50%(灰带轻扣)", lambda r: r["ncw"] is not None and -0.50 <= r["ncw"] < -0.30),
    ("-50~-100%(灰带重扣)", lambda r: r["ncw"] is not None and -1.00 <= r["ncw"] < -0.50),
    ("-100~-200%(封顶watch)", lambda r: r["ncw"] is not None and -2.00 <= r["ncw"] < -1.00),
    ("< -200%(封顶na)", lambda r: r["ncw"] is not None and r["ncw"] < -2.00),
    ("NULL(算不出,不罚)", lambda r: r["ncw"] is None),
)


def main():
    db = SessionLocal()
    rows = []
    for r in db.execute(text(CENSUS_SQL)).mappings().all():
        d = dict(r)
        d["ncw"] = (float(d["net_cash_w"]) / float(d["market_cap"])
                    if d["net_cash_w"] is not None and d["market_cap"] else None)
        rows.append(d)
    db.close()

    # 港股通近似:次新过滤未做(库内港股 list_date 缺失为主,join 后近似 pick 域即可,
    # 桶计数用于校准而非精确回放)
    print(f"批次域 {len(rows)} 家(A+HK;未做港股通/次新过滤,用于阈值校准)\n")

    total_hit = 0
    for label, cond in BUCKETS:
        bucket = [r for r in rows if cond(r)]
        total_hit += len(bucket) if not label.startswith("NULL") else 0
        print(f"[{label}] {len(bucket)} 家")
        if label.startswith(("-100", "< -200", "-50")):
            # 桶内交叉:当前批次推荐分分布 + 行业 top3
            in_buy = [r for r in bucket if r["recommend"] is not None and r["recommend"] >= BUY_MIN]
            in_watch = [r for r in bucket if r["recommend"] is not None and WATCH_MIN <= r["recommend"] < BUY_MIN]
            ind: dict = {}
            for r in bucket:
                ind[r["industry"] or "?"] = ind.get(r["industry"] or "?", 0) + 1
            top_ind = sorted(ind.items(), key=lambda kv: -kv[1])[:3]
            print(f"    其中 R≥65(买入区) {len(in_buy)} 家 / 45~65(观察区) {len(in_watch)} 家"
                  f" / avoid(gate=1) {sum(1 for r in bucket if r['gate'] == 1)} 家")
            print(f"    行业top3: {top_ind}")
            for r in sorted(bucket, key=lambda x: x["ncw"] or 0)[:12]:
                rec = r["recommend"]
                mark = "★买" if rec is not None and rec >= BUY_MIN else ("☆观" if rec is not None and rec >= WATCH_MIN else "")
                print(f"      {r['code']} {r['name']} [{r['market']}] ncw {r['ncw'] * 100:.0f}%"
                      f" R={rec if rec is None else round(rec, 1)} {mark} {r['industry'] or '?'}")

    # 关键预览:分级上线会压档的现存公司
    capped_watch = [r for r in rows if r["ncw"] is not None and CAP_WATCH > r["ncw"] >= CAP_NA]
    capped_na = [r for r in rows if r["ncw"] is not None and r["ncw"] < CAP_NA]
    gray = [r for r in rows if r["ncw"] is not None and GRAY_FROM > r["ncw"] >= CAP_WATCH]
    for label, grp in (("灰带扣分域(-30~-100%)", gray),
                       ("封顶 watch 域(-100~-200%)", capped_watch),
                       ("封顶 na 域(<-200%)", capped_na)):
        buy = [r for r in grp if r["recommend"] is not None and r["recommend"] >= BUY_MIN]
        strong = [r for r in grp if r["recommend"] is not None and r["recommend"] >= STRONG_MIN]
        av = sum(1 for r in grp if r["gate"] == 1)
        print(f"\n[{label}] {len(grp)} 家 | R≥65 {len(buy)} | R≥78 {len(strong)} | 已gate {av}")

    print(f"\n净现金可判 {total_hit + sum(1 for r in rows if BUCKETS[6][1](r))} 家,"
          f"其中负值域(-30%以下)合计 {len(gray) + len(capped_watch) + len(capped_na)} 家")
    return 0


if __name__ == "__main__":
    sys.exit(main())
