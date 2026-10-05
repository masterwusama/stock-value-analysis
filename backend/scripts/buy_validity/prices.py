# -*- coding: utf-8 -*-
"""价格面:data/prices 文件装载、交易日历、月末网格、索引与前瞻收益。

全部纯函数、零 DB——命名价与 hfq 分工也在这层定死:raws(不复权)只用于
「名义价 vs 名义买价」的比较,closes(hfq)只用于区间收益。
"""
import bisect
import json
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PRICE_DIR = BACKEND / "collector" / "data" / "prices"
DAY_CAP = 14           # 网格日停牌:其后 14 天内有交易则用该日,否则判不动


def load_prices(limit_codes=None):
    """→ (prices{code:(dates,closes,raws)}, bad{原因:n})。

    坏 JSON / raws 与 dates 长度错位的文件不再静默消失:错位 raws 置 None 进
    「无名义价」缺失计数,坏文件计入 bad——静默丢股会加重幸存者偏差且不可见
    (fetch 侧 2026-10-05 起落盘前断言等长,此守卫兜存量与旧档)。"""
    out = {}
    bad = {}
    if not PRICE_DIR.exists():
        return out, bad
    for p in PRICE_DIR.glob("*.json"):
        if limit_codes is not None and p.stem not in limit_codes:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            bad["坏价格文件"] = bad.get("坏价格文件", 0) + 1
            continue
        if d.get("dates") and d.get("closes"):
            raws = d.get("raws")
            if raws is not None and len(raws) != len(d["dates"]):
                bad["raws/dates 错位(当无名义价)"] = bad.get("raws/dates 错位(当无名义价)", 0) + 1
                raws = None
            out[p.stem] = (d["dates"], d["closes"], raws)
    return out, bad


def grid_dates(dates, y_from, y_to):
    """日历里每年每月的最后一个交易日。"""
    by_month = {}
    for d in dates:
        if y_from * 10000 <= int(d.replace("-", "")) <= y_to * 10000 + 1231:
            by_month[d[:7]] = d
    return [by_month[m] for m in sorted(by_month)]


def px_idx_at_or_after(dates, iso, cap_days=DAY_CAP):
    """≥iso 的第一个交易日下标;超过 iso+cap_days 或到头则 None。"""
    i = bisect.bisect_left(dates, iso)
    if i >= len(dates):
        return None
    if (date.fromisoformat(dates[i]) - date.fromisoformat(iso)).days > cap_days:
        return None
    return i


def fwd_return(dates, closes, i0, months):
    """从下标 i0 起持有 months 个月的点对点收益(hfq);两端任一无效 None。"""
    try:
        tgt = date.fromisoformat(dates[i0]).isoformat()[:8] + "01"
        y, m = int(tgt[:4]), int(tgt[5:7])
        y2, m2 = y + (m - 1 + months) // 12, (m - 1 + months) % 12 + 1
        day = min(28, date.fromisoformat(dates[i0]).day)   # 闰日纪念近似
        tgt = date(y2, m2, day).isoformat()
    except ValueError:
        return None
    i1 = px_idx_at_or_after(dates, tgt, cap_days=45)
    if i1 is None or i1 <= i0 or closes[i1] <= 0:
        return None
    return closes[i1] / closes[i0] - 1.0
