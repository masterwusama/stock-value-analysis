# -*- coding: utf-8 -*-
"""情绪日报接口:状态卡/明日预案/板块效应/候选池 与 逐日状态序列。

数据由 collector/scripts/fetch_sentiment.py 每个交易日 16:30 采集,
import_legacy(--only-sentiment)回灌 sentiment_daily / sentiment_sector 两表。

温度 T/极端度/连续天数为**读时计算**(collector.scripts.sentiment_score,与采集端
同一份实现):不落库、零迁移,公式演进对全部历史一致生效,防口径双源漂移。
"""
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import SentimentDaily, SentimentSector
from collector.scripts import sentiment_score

router = APIRouter(prefix="/api/sentiment", tags=["sentiment"])

# 前端徽章配色(与状态语义绑定,别在模板里散着写)
STATE_TONE = {"极强": "strong", "回暖": "warm", "普通": "flat", "极弱": "weak"}


def _score_of(row: SentimentDaily, streak: int) -> dict | None:
    return sentiment_score.score_card(
        {"red_rate": row.red_rate, "zt_count": row.zt_count, "zha_rate": row.zha_rate,
         "dt_count": row.dt_count, "yz_dt_count": row.yz_dt_count, "r5": row.r5},
        row.state, streak)


def _streak_at(db: Session, d) -> int:
    """当日连续同档天数:≤d 的近期状态序(升序)推尾巴;行缺失由状态切换自然截断。
    窗口取 500 与采集端 history 留存对齐——limit(15) 会把第 16 天起的长连续
    系统性少报成 15,与日报文案、/history 三处数字打架。"""
    states = db.execute(
        select(SentimentDaily.state).where(SentimentDaily.trade_date <= d)
        .order_by(SentimentDaily.trade_date.desc()).limit(500)
    ).scalars().all()
    return sentiment_score.streak_of(list(reversed(states)))[-1] if states else 0


def _daily_out(row: SentimentDaily, score: dict | None = None) -> dict:
    return {
        "date": row.trade_date.isoformat(),
        "state": row.state,
        "prev_state": row.prev_state,
        "red_rate": row.red_rate,
        "zt_count": row.zt_count,
        "zha_count": row.zha_count,
        "zha_rate": row.zha_rate,
        "dt_count": row.dt_count,
        "yz_dt_count": row.yz_dt_count,
        "r5": row.r5,
        "transitions": row.transitions or {},
        "trap": bool(row.trap),
        "tone": STATE_TONE.get(row.state, "flat"),
        "score": score,
        "report": row.report or {},
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


@router.get("/daily")
def sentiment_daily(d: date_type | None = Query(None, alias="date"),
                    db: Session = Depends(get_session)):
    """最新(或指定交易日)的情绪日报;含板块明细与三个名单。"""
    if d is None:
        row = db.execute(
            select(SentimentDaily).order_by(SentimentDaily.trade_date.desc()).limit(1)
        ).scalar_one_or_none()
    else:
        row = db.get(SentimentDaily, d)
    if row is None:
        raise HTTPException(404, "该日期无情绪日报(数据自 2026-09 起积累)")
    sectors = db.execute(
        select(SentimentSector)
        .where(SentimentSector.trade_date == row.trade_date)
        .order_by(SentimentSector.limit_count.desc())
    ).scalars().all()
    out = _daily_out(row, _score_of(row, _streak_at(db, row.trade_date)))
    # 两层板块:broad=证监会大类(≥5家且≥2%),fine=东财细分行业(≥3家,带连板/封单)
    out["sectors"] = [{
        "industry": s.industry, "limit_count": s.limit_count,
        "share_of_industry": s.share_of_industry, "amount_yi": s.amount_yi,
    } for s in sectors if (s.level or "broad") == "broad"]
    out["sectors_fine"] = [{
        "industry": s.industry, "limit_count": s.limit_count,
        "share_of_zt": s.share_of_industry, "max_streak": s.max_streak,
        "seal_yi": s.seal_amount_yi,
    } for s in sectors if s.level == "fine"]
    return out


@router.get("/history")
def sentiment_history(limit: int = Query(120, ge=1, le=500),
                      db: Session = Depends(get_session)):
    """逐日状态序列(状态机/红盘率/涨停跌停/炸板率/R5/温度),用于走势条与状态带。"""
    rows = db.execute(
        select(SentimentDaily).order_by(SentimentDaily.trade_date.desc()).limit(limit * 2)
    ).scalars().all()
    rows = list(reversed(rows))
    streaks = sentiment_score.streak_of([r.state for r in rows])
    # 头部多取 limit 行做 streak 预热再裁掉:否则窗口首行即使处在长连续中段也从 1 重数
    cut = max(0, len(rows) - limit)
    rows, streaks = rows[cut:], streaks[cut:]
    out = []
    for r, stk in zip(rows, streaks):
        sc = _score_of(r, stk)
        out.append({
            "date": r.trade_date.isoformat(), "state": r.state,
            "tone": STATE_TONE.get(r.state, "flat"),
            "red_rate": r.red_rate, "zt_count": r.zt_count, "zha_rate": r.zha_rate,
            "dt_count": r.dt_count, "yz_dt_count": r.yz_dt_count,
            "r5": r.r5, "trap": bool(r.trap),
            "t": sc["t"] if sc else None, "streak": stk,
        })
    return out
