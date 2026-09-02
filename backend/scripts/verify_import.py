# -*- coding: utf-8 -*-
"""导入结果验证:行数核对 + 抽样对比源 JSON。"""
import json
from pathlib import Path

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    AgroPrice, Dividend, FinBalance, FinIndicator, PeriodicReport, PfNav,
    PfPosition, PfTrade, QuoteDaily, ScoreDaily, Security, WindEvent,
)

DATA = Path(r"<项目目录>\data")

db = SessionLocal()

print("== 1. 各表行数 ==")
for m in (Security, QuoteDaily, ScoreDaily, FinIndicator, FinBalance,
          Dividend, PeriodicReport, WindEvent, PfNav, PfPosition, PfTrade, AgroPrice):
    print(f"  {m.__tablename__}: {db.execute(select(func.count()).select_from(m)).scalar_one()}")

print("== 2. security 按市场 ==")
for market, n in db.execute(select(Security.market, func.count()).group_by(Security.market)):
    print(f"  {market}: {n}")

print("== 3. 抽样:600309 最新行情 ==")
sid = db.execute(select(Security.sid).where(Security.code == "600309")).scalar_one()
q = db.execute(select(QuoteDaily).where(QuoteDaily.sid == sid)).scalar_one()
src = json.loads((DATA / "companies" / "600309.json").read_text(encoding="utf-8"))["snapshot"]
print(f"  DB:   price={q.price} pe_ttm={q.pe_ttm} mcap={q.market_cap} date={q.trade_date}")
print(f"  JSON: price={src['price']} pe_ttm={src['pe_ttm']} mcap={src['market_cap']}")
assert float(q.price) == src["price"] and float(q.pe_ttm) == src["pe_ttm"], "MISMATCH"

print("== 4. 抽样:002027 评分 vs index.json ==")
idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
src_sc = next(c["scores"] for c in idx["companies"] if c["code"] == "002027")
sid2 = db.execute(select(Security.sid).where(Security.code == "002027")).scalar_one()
s = db.execute(select(ScoreDaily).where(ScoreDaily.sid == sid2)).scalar_one()
print(f"  DB:   buffett={s.score_buffett} fraud={s.fraud} mgmt={s.mgmt} buy_schloss={s.buy_schloss}")
print(f"  JSON: buffett={src_sc['buffett']} fraud={src_sc['fraud']} mgmt={src_sc['mgmt']} buy={src_sc['priceRefs']['schloss']['buy']}")
assert float(s.score_buffett) == src_sc["buffett"]
assert float(s.buy_schloss) == src_sc["priceRefs"]["schloss"]["buy"]
ov = json.loads((DATA / "events" / "index.json").read_text(encoding="utf-8"))["byCode"]
if "002027" in ov:
    print(f"  wind: fraudDelta={s.wind_fraud_delta} mgmtDelta={s.wind_mgmt_delta} flags={s.wind_flags}")
    assert float(s.wind_fraud_delta) == ov["002027"]["fraudDelta"]

print("== 5. 抽样:600309 资产负债表核心科目(净现金计算)==")
b = db.execute(select(FinBalance).where(FinBalance.sid == sid)
               .order_by(FinBalance.report_date.desc()).limit(1)).scalar_one()
src_b = next(r for r in json.loads(
    (DATA / "companies" / "600309.json").read_text(encoding="utf-8"))["balance"]
    if r["报告日"].startswith(str(b.report_date)))
print(f"  DB:   cash={b.cash} fin={b.trading_fin_assets} tl={b.total_liabilities}")
print(f"  JSON: cash={src_b['货币资金']} fin={src_b.get('交易性金融资产')} tl={src_b['负债合计']}")
assert float(b.cash) == src_b["货币资金"] and float(b.total_liabilities) == src_b["负债合计"]

print("== 6. pf_nav 三策略 ==")
for k, n in db.execute(select(PfNav.strat_key, func.count()).group_by(PfNav.strat_key)):
    print(f"  {k}: {n} 天")
for r in db.scalars(select(PfNav).where(PfNav.strat_key == "buffett").order_by(PfNav.nav_date)):
    print(f"  buffett {r.nav_date}: nav={r.nav} day_pnl={r.day_pnl} total_pnl={r.total_pnl}")

print("== 7. 指标期数最多的公司(top3)==")
rows = db.execute(select(FinIndicator.sid, func.count()).group_by(FinIndicator.sid)
                  .order_by(func.count().desc()).limit(3))
for sid_, n in rows:
    code = db.execute(select(Security.code).where(Security.sid == sid_)).scalar_one()
    print(f"  {code}: {n} 期")

print("== 8. 持仓与流水核对 ==")
n_pos = db.execute(select(func.count()).select_from(PfPosition)).scalar_one()
n_tr = db.execute(select(func.count()).select_from(PfTrade)).scalar_one()
src_trades = json.loads(Path(r"<项目目录>\portfolio\data\trades.json")
                        .read_text(encoding="utf-8"))
assert n_tr == sum(len(v) for v in src_trades.values()), f"trade count {n_tr}"
print(f"  pf_position={n_pos}, pf_trade={n_tr} == 源 {sum(len(v) for v in src_trades.values())}")

print("ALL CHECKS PASSED")
db.close()
