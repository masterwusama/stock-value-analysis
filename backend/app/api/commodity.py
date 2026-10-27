# -*- coding: utf-8 -*-
"""大宗商品供需模块 API(第一期:黄金)。

数据链:collector/commodity-gold/scripts/fetch_gold.py(免费源,每天 06:05)→
commodity-gold/data/gold.json → import_legacy --only-gold → cmd_indicator/cmd_value
两表 → 本接口读库。与 agro EDB 分表的原因见 CmdIndicator 模型注与 import_gold 头注。

读时合成(不在采集器算,改口径不用重刷数据):
    - 每序列 KPI:最新值/近1周·4周·1年变动/52周高低/52周分位——前端卡片直接渲染;
    - 新鲜度按频率分档(日>7天/周>21天/月>70天报陈旧),序列缺测不算失败,
      采集器降级留下的旧点照常展示(学习 etf 页红条教训,新鲜度透明给用户)。
设计文档:docs/设计-大宗商品供需模块-黄金.md(三层量化框架:P0 数据层,P2 上因子记分卡)。
"""
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import CmdIndicator, CmdValue

router = APIRouter(prefix="/api/commodity", tags=["commodity"])

_FREQ_CN = {"day": "日", "week": "周", "month": "月"}
# 新鲜度红线(天):按频率分档——周度 CFTC 周五发布,月度储备滞后 2 个月是常态
_STALE_DAYS = {"day": 7, "week": 21, "month": 70}


def _chg(points, days):
    """近 N 天变动(现值 - 窗口内最早值);窗口内无数据返回 None。"""
    if not points:
        return None
    d0, v0 = points[-1]
    cut = dt.date.fromisoformat(d0) - dt.timedelta(days=days)
    for d, v in points:
        if dt.date.fromisoformat(d) >= cut:
            return round(v0 - v, 4)
    return None


def _stats(points, freq):
    """单序列读时统计:KPI 卡片与新鲜度。"""
    if not points:
        return {"latest": None, "stale": True}
    today = dt.date.today()
    latest_d, latest_v = points[-1]
    win52 = [(dt.date.fromisoformat(d), v) for d, v in points
             if dt.date.fromisoformat(d) >= today - dt.timedelta(days=365)]
    vals = [v for _, v in win52] or [latest_v]
    return {
        "latest": latest_v,
        "latest_date": latest_d,
        "chg_1w": _chg(points, 7),
        "chg_4w": _chg(points, 30),
        "chg_1y": _chg(points, 365),
        "hi_52w": max(vals),
        "lo_52w": min(vals),
        # 52 周分位(0~1):最新值在窗口内的位置,相对贵贱的粗口径
        "pct_52w": round((latest_v - min(vals)) / (max(vals) - min(vals)), 4)
        if max(vals) > min(vals) else 0.5,
        "stale_days": (today - dt.date.fromisoformat(latest_d)).days,
        "stale": (today - dt.date.fromisoformat(latest_d)).days > _STALE_DAYS.get(freq, 14),
    }


@router.get("/gold")
def gold_series(db: Session = Depends(get_session)):
    """黄金供需研究 P0:全序列 + 读时统计。"""
    inds = db.execute(
        select(CmdIndicator).where(CmdIndicator.commodity == "gold")
    ).scalars().all()
    pts = defaultdict(list)
    for code, d, v in db.execute(
        select(CmdValue.cmd_code, CmdValue.data_date, CmdValue.val).order_by(CmdValue.data_date)
    ):
        pts[code].append([d.isoformat(), float(v) if v is not None else None])

    series = []
    # idx=0 的序列(伦敦金)不能用 `or 999` 兜底——0 是 falsy 会被顶到队尾(实测踩坑)
    def _idx(ind):
        v = (ind.extra or {}).get("idx")
        return 999 if v is None else v

    for ind in sorted(inds, key=_idx):
        points = pts.get(ind.cmd_code, [])
        series.append({
            "code": ind.cmd_code, "name": (ind.extra or {}).get("name") or ind.name,
            "label": ind.name, "cat": ind.category, "group": ind.display_group,
            "unit": ind.unit, "freq": _FREQ_CN.get(ind.freq, ind.freq),
            "source": (ind.extra or {}).get("source"),
            "stats": _stats(points, ind.freq),
            "points": points,
        })
    cats = []
    for s in series:  # cat 首见序即展示序(fetch_gold SPEC 顺序)
        if s["cat"] not in cats:
            cats.append(s["cat"])
    return {
        "updated_at": max((s["stats"].get("latest_date") for s in series), default=None),
        "cats": cats,
        "series": series,
    }
