# -*- coding: utf-8 -*-
"""定增史的本地查表（fetch_data 落明细与 _refresh_scores 重算共用）。

单独成模块而不是留在 fetch_data 里：那边 import 一长串行情/报表依赖，只为取这个查表函数
会带进 50 多行 pandas 与 NumPy 版本告警，日更日志里塞一段假 traceback 形状的东西，看红
的人就分不清哪条是真的。
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
# code -> 定增行；None 表示还没试过，False 表示试过但读不到（别让后面 5,500 家各自再解一遍 12MB）
_SEO_ACTION_CACHE = None
SEO_ACTION_KEYS = ("issue_date", "listing_date", "num")


def seo_actions(code: str):
    """从本地快照 data/actions/latest.json 切出这家公司的定增史，不发网络请求。

    该快照由 fetch_actions.py 每日整表重抓（实测 5,468 行 / 2,678 家），逐只去调接口拿同样
    的数据没有任何收益，所以在此查表；只留评分要用的三列（其余 17 列留在快照里）。

    返回 None 与返回 `[]` 是两件事，别合并：None 是快照缺失（评分把这一项判成「取不到」），
    `[]` 是快照在而查无此码（「近 5 年无定增」是公开事实，记 0）。混起来等于在快照没落地之前
    替全市场担保没摊薄过。
    """
    global _SEO_ACTION_CACHE
    if _SEO_ACTION_CACHE is None:
        try:
            raw = json.loads((DATA / "actions" / "latest.json").read_text(encoding="utf-8"))
            grouped = {}
            for r in raw.get("seo") or []:
                grouped.setdefault(str(r.get("code") or ""), []).append(
                    {k: r.get(k) for k in SEO_ACTION_KEYS}
                )
            _SEO_ACTION_CACHE = grouped or False
        except Exception:
            _SEO_ACTION_CACHE = False
    if _SEO_ACTION_CACHE is False:
        return None
    return _SEO_ACTION_CACHE.get(code, [])
