# -*- coding: utf-8 -*-
"""模拟组合接口:概览/调仓流水/净值曲线(结构与原 portfolio/data/*.json 对齐)。

原版持仓的现价/市值/盈亏为引擎静态生成;重构后由服务端按最新行情实时计算。
"""
from fastapi import Depends, APIRouter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import PfNav, PfPosition, PfStrategy, PfTrade, QuoteDaily, Security

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def _f(v):
    return float(v) if v is not None else None


def _tranches(v):
    """int 档位存为 {"count": n};还原时回退裸 int(与原 JSON 一致)。"""
    if isinstance(v, dict) and set(v.keys()) == {"count"}:
        return v["count"]
    return v


@router.get("")
def portfolio_overview(db: Session = Depends(get_session)):
    """三策略概览(同原 portfolio.json):最新净值 + 持仓实时市值/盈亏。"""
    strategies = db.execute(select(PfStrategy).order_by(PfStrategy.strat_key)).scalars().all()
    snap_quote = db.execute(select(func.max(QuoteDaily.trade_date))).scalar_one()
    price_map = {
        sid: _f(price)
        for sid, price in db.execute(
            select(QuoteDaily.sid, QuoteDaily.price).where(QuoteDaily.trade_date == snap_quote)
        )
    }

    out = {}
    as_of = None
    for st in strategies:
        navs = db.execute(
            select(PfNav).where(PfNav.strat_key == st.strat_key).order_by(PfNav.nav_date.desc()).limit(2)
        ).scalars().all()
        latest = navs[0] if navs else None
        as_of = latest.nav_date if latest else as_of

        positions = []
        pos_rows = db.execute(
            select(PfPosition, Security)
            .join(Security, Security.sid == PfPosition.sid)
            .where(PfPosition.strat_key == st.strat_key)
            .order_by(PfPosition.bought_at, Security.code)
        ).all()
        for pos, sec in pos_rows:
            price = price_map.get(pos.sid)
            if price is None:
                price = _f(pos.cost)  # 无行情(如退市)回退成本价
            shares, cost = int(pos.shares), _f(pos.cost)
            value = round(shares * price, 2)
            pnl = round(value - shares * cost, 2)
            positions.append({
                "code": sec.code, "name": sec.name,
                "shares": shares, "cost": cost,
                "bought_at": pos.bought_at.isoformat(),
                "tranches": _tranches(pos.tranches),
                "price": price, "value": value, "pnl": pnl,
                "pnl_pct": round(pnl / (shares * cost) * 100, 2) if cost and shares else 0.0,
                "days": (latest.nav_date - pos.bought_at).days if latest else 0,
            })

        out[st.strat_key] = {
            "label": st.label,
            "init_cap": _f(st.init_cap),
            "cash": _f(latest.cash) if latest else None,
            "nav": _f(latest.nav) if latest else None,
            "prev_nav": _f(navs[1].nav) if len(navs) > 1 else None,
            "day_pnl": _f(latest.day_pnl) if latest else None,
            "day_pnl_pct": latest.day_pnl_pct if latest else None,
            "total_pnl": _f(latest.total_pnl) if latest else None,
            "total_pnl_pct": latest.total_pnl_pct if latest else None,
            "position_pct": latest.position_pct if latest else None,
            "as_of": latest.nav_date.isoformat() if latest else None,
            "positions": positions,
        }

    return {"as_of": as_of.isoformat() if as_of else None, "strategies": out}


@router.get("/trades")
def portfolio_trades(db: Session = Depends(get_session)):
    """调仓流水(同原 trades.json:{策略: 时间正序行},含联表 name)。"""
    rows = db.execute(
        select(PfTrade, Security.code, Security.name)
        .join(Security, Security.sid == PfTrade.sid)
        .order_by(PfTrade.trade_date, PfTrade.id)
    ).all()
    out: dict[str, list] = {}
    for t, code, name in rows:
        out.setdefault(t.strat_key, []).append({
            "date": t.trade_date.isoformat(), "code": code,
            "name": name, "side": t.side,
            "price": _f(t.price), "shares": int(t.shares) if t.shares is not None else None,
            "amount": _f(t.amount), "reason": t.reason,
        })
    return out
