# -*- coding: utf-8 -*-
"""面板构建:月末网格上逐股逐月判定「从线上方跌进买点」事件,挂前瞻收益。

面板行 = (网格下标, sid, pb_t, r6m, r1y, r2y, ev{(派,折扣%): True}),
事件定义:上一网格日 名义价 > 买价×D% 且本月 ≤ 买价×D%(两格各自的点时买价);
同股同派同折扣 180 天(6 个网格)只留首个。
"""
from collections import defaultdict

from . import HORIZONS
from .prices import fwd_return, px_idx_at_or_after
from .synthesis import BATCH, load_avails, load_batch, refs_at

LISTING_BARS = 250     # 上市不足 250 个交易日的格点剔除(新股效应与买点语义无关)
DEDUP_GRIDS = 6        # 同股同派 180 天(6 个月网格)只留首个事件


def build_panel(db, sec, prices, grids, schools, discounts, batches=0):
    """→ (panel, miss, n_sids)。sec = {sid: (code, industry)}。"""
    sids = [sid for sid, (code, _) in sec.items() if code in prices]
    if batches:
        sids = sids[: batches * BATCH]
    panel = []
    miss = defaultdict(int)
    done = 0
    for bi in range(0, len(sids), BATCH):
        chunk = sids[bi:bi + BATCH]
        fin = load_batch(db, chunk)
        load_avails(db, chunk, fin)
        for sid in chunk:
            code, _ = sec[sid]
            dates, closes, raws = prices[code]
            rows = fin[sid]
            prev = None            # (grid_idx, price, buys)
            last_ev = {}           # (sch, d%) -> grid_idx
            for gi, G in enumerate(grids):
                i0 = px_idx_at_or_after(dates, G)
                if i0 is None or i0 < LISTING_BARS:
                    continue
                if raws is None or raws[i0] is None:
                    miss["无名义价"] += 1
                    continue
                price = raws[i0]
                buys, diag = refs_at(rows, rows["div"], price, G)
                if buys is None:
                    miss["无 as-of 财务"] += 1
                    prev = None
                    continue
                bps = diag["bps"]
                r = {h: fwd_return(dates, closes, i0, m) for m, h in HORIZONS}
                ev = {}
                if prev is not None:
                    for s in schools:
                        b_now, b_prev = buys[s], prev[2][s]
                        for d in discounts:
                            if b_now is not None and b_prev is not None:
                                thr_now, thr_prev = b_now * d / 100.0, b_prev * d / 100.0
                                if prev[1] > thr_prev and price <= thr_now:
                                    if gi - last_ev.get((s, d), -99) >= DEDUP_GRIDS:
                                        ev[(s, d)] = True
                                        last_ev[(s, d)] = gi
                panel.append((gi, sid, (price / bps if bps else None),
                              r["6m"], r["1y"], r["2y"], ev))
                prev = (gi, price, buys)
        done += len(chunk)
        print(f"  面板进度 {done}/{len(sids)} 家 · 网格 {len(grids)} · 观测 {len(panel)}",
              flush=True)
    return panel, miss, len(sids)
