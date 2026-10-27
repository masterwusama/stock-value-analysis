# -*- coding: utf-8 -*-
"""农化制品价格接口:结构与 agro-price/data/products.json 对齐。

2026-10-27 起模块聚焦农化制品:行业 EDB 五板块(电解铝/航运/轮胎/地产/钢铁)整体
移除(视图/采集链/调度),/edb 端点同步下线;edb_indicator/edb_value 两表与模型
保留为历史数据存档,不再有写入方。
"""
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AgroPrice, AgroProduct

router = APIRouter(prefix="/api/agro", tags=["agro"])

# 断档阈值，与 collector/agro-price/scripts/fetch_prices.py 的 STALE_MAX_DAYS 同值（那边是告警
# 口径的唯一权威）。两个目录不同包、agro-price 带连字符不能 import，只能靠注释互指；改一处
# 忘改另一处的后果只是前端徽章颜色早/晚几天亮，不影响作业告警本身。
STALE_MAX_DAYS = 21


def _f(v):
    return float(v) if v is not None else None


@router.get("/products")
def agro_products(db: Session = Depends(get_session)):
    """农化产品价格序列(同原 products.json)。"""
    products = db.execute(
        select(AgroProduct).where(AgroProduct.active.is_(True)).order_by(AgroProduct.product_id)
    ).scalars().all()
    by_prod = defaultdict(list)
    for pid, pdate, price, source, note in db.execute(
        select(AgroPrice.product_id, AgroPrice.price_date, AgroPrice.price, AgroPrice.source, AgroPrice.note)
        .order_by(AgroPrice.price_date)
    ):
        by_prod[pid].append({
            "date": pdate.isoformat(), "price": _f(price), "source": source, "note": note,
        })
    updated = db.execute(select(func.max(AgroPrice.price_date))).scalar_one()
    today = dt.date.today()
    items = []
    for a in products:
        pl = by_prod.get(a.product_id, [])
        # 每条序列单独报新鲜度：顶层 updated_at 取的是全局最大日期，只要还有一个品种在
        # 更新它就永远是“今天”，看不出草甘膦/甲基硫菌灵这种已断档 50 天的序列（2026-09-03 实测）
        latest = pl[-1]["date"] if pl else None
        stale_days = (today - dt.date.fromisoformat(latest)).days if latest else None
        items.append({
            "id": a.product_id, "name": a.name, "category": a.category,
            "spec": a.spec, "unit": a.unit,
            "prices": pl,
            "latest": latest,
            "stale_days": stale_days,
            # 阈值判断放在服务端，免得前端再抄一个 21
            "stale": bool(stale_days is None or stale_days > STALE_MAX_DAYS),
        })
    return {
        "updated_at": updated.isoformat() if updated else None,
        "products": items,
    }
