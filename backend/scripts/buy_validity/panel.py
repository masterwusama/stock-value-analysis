# -*- coding: utf-8 -*-
"""面板构建:月末网格上逐股逐月判定「从线上方跌进买点」事件,挂前瞻收益。

面板行 = (网格下标, sid, pb_t, r6m, r1y, r2y, ev{(派,折扣%): True}),
事件定义:上一网格日 名义价 > 买价×D% 且本月 ≤ 买价×D%(两格各自的点时买价);
比价前先做**除权修正**:名义价在除权日机械跳降(10 转 10 即 -50%),而买线锚的
bps 要等下期财报才换股本——不修正的话送转股必然伪造「跌进线内」事件。修正法:
hfq 收盘/名义收盘 = 累计除权因子 F,本月价 ×(F_now/F_prev) 折算回上一格股本口径
再比线;两格之间无除权事件时 F 不变,折算后与原判完全等价。
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
            prev = None            # (grid_idx, price, buys, factor)
            last_ev = {}           # (sch, d%) -> grid_idx
            for gi, G in enumerate(grids):
                i0 = px_idx_at_or_after(dates, G)
                if i0 is None or i0 < LISTING_BARS:
                    continue
                if raws is None or raws[i0] is None:
                    miss["无名义价"] += 1
                    continue
                price = raws[i0]
                fac = closes[i0] / price     # 累计除权因子(hfq/名义),除权日跳变
                buys, diag = refs_at(rows, rows["div"], price, G)
                if buys is None:
                    miss["无 as-of 财务"] += 1
                    prev = None
                    continue
                bps = diag["bps"]
                r = {h: fwd_return(dates, closes, i0, m) for m, h in HORIZONS}
                ev = {}
                px = price * (fac / prev[3]) if (prev and prev[3] and fac) else price
                if prev is not None:
                    for s in schools:
                        b_now, b_prev = buys[s], prev[2][s]
                        for d in discounts:
                            if b_now is not None and b_prev is not None:
                                thr_now, thr_prev = b_now * d / 100.0, b_prev * d / 100.0
                                if prev[1] > thr_prev and px <= thr_now:
                                    if gi - last_ev.get((s, d), -99) >= DEDUP_GRIDS:
                                        ev[(s, d)] = True
                                        last_ev[(s, d)] = gi
                panel.append((gi, sid, (px / bps if bps else None),
                              r["6m"], r["1y"], r["2y"], ev))
                prev = (gi, price, buys, fac)
        done += len(chunk)
        print(f"  面板进度 {done}/{len(sids)} 家 · 网格 {len(grids)} · 观测 {len(panel)}",
              flush=True)
    return panel, miss, len(sids)
