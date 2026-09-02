# -*- coding: utf-8 -*-
"""农价/EDB 接口:结构与原 agro-price/data/{products,edb}.json 对齐。"""
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AgroPrice, AgroProduct, EdbIndicator, EdbValue

router = APIRouter(prefix="/api/agro", tags=["agro"])

# edb_indicator.freq(枚举) → 原 edb.json 中文值
_FREQ_BACK = {"day": "日", "week": "周", "month": "月"}


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
    return {
        "updated_at": updated.isoformat() if updated else None,
        "products": [
            {
                "id": a.product_id, "name": a.name, "category": a.category,
                "spec": a.spec, "unit": a.unit,
                "prices": by_prod.get(a.product_id, []),
            }
            for a in products
        ],
    }


@router.get("/edb")
def edb_indicators(db: Session = Depends(get_session)):
    """Wind EDB 宏观行业量价(同原 edb.json:categories→indicators→points)。

    原始展示顺序由导入时记录的 extra.cat_idx / ind_idx 还原。
    """
    inds = db.execute(select(EdbIndicator)).scalars().all()
    pts = defaultdict(list)
    for code, d, v in db.execute(
        select(EdbValue.edb_code, EdbValue.data_date, EdbValue.val).order_by(EdbValue.data_date)
    ):
        pts[code].append([d.isoformat(), _f(v)])

    cats: dict[str, dict] = {}
    for ind in inds:
        extra = ind.extra or {}
        cat = cats.setdefault(ind.category, {
            "id": ind.category, "name": extra.get("cat_name") or ind.category,
            "_idx": extra.get("cat_idx"), "indicators": [],
        })
        cat["indicators"].append({
            "code": ind.edb_code, "name": extra.get("name"), "label": ind.name,
            "unit": ind.unit, "freq": _FREQ_BACK.get(ind.freq, ind.freq),
            "source": extra.get("source"), "group": ind.display_group,
            "_idx": extra.get("ind_idx"), "points": pts.get(ind.edb_code, []),
        })

    def _key(x):
        return (x.get("_idx") is None, x.get("_idx") or 0)

    for cat in cats.values():
        cat["indicators"].sort(key=_key)
        for i in cat["indicators"]:
            i.pop("_idx", None)
    ordered = sorted(cats.values(), key=_key)
    for cat in ordered:
        cat.pop("_idx", None)

    begin, end = db.execute(
        select(func.min(EdbValue.data_date), func.max(EdbValue.data_date))
    ).one()
    return {
        "updated_at": end.isoformat() if end else None,
        "range": {"begin": begin.isoformat() if begin else None,
                  "end": end.isoformat() if end else None},
        "categories": ordered,
    }
