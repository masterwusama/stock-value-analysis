# -*- coding: utf-8 -*-
"""命令行入口:全量判决书 + --check-live 同构对拍(必做守卫)。

用法(在 backend 目录):
    python -X utf8 -m scripts.buy_validity                     # 全量判决书
    python -X utf8 -m scripts.buy_validity --check-live        # 同构对拍
    python -X utf8 -m scripts.buy_validity --dump ../_tmp/buy_obs.json
    python -X utf8 -m scripts.buy_validity --school schloss --discount 80,100
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ScoreDaily, Security

from . import CAL, GRID_FROM, GRID_TO, SCHOOLS
from .panel import build_panel
from .prices import grid_dates, load_prices, px_idx_at_or_after
from .synthesis import BATCH, load_avails, load_batch, refs_at
from .verdict import render

BACKEND = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description="四派买点信号回测(只读库+本地价格文件)")
    ap.add_argument("--dump", default="")
    ap.add_argument("--school", choices=SCHOOLS, default=None, help="只跑一派")
    ap.add_argument("--discount", default="100,80")
    ap.add_argument("--check-live", action="store_true",
                    help="同构对拍:网格=score_daily 最新日,逐派比对物化 buy_*")
    ap.add_argument("--batches", type=int, default=0, help="只跑前 N 批(调试)")
    a = ap.parse_args()
    schools = [a.school] if a.school else list(SCHOOLS)
    discounts = tuple(int(x) for x in a.discount.split(","))

    db = SessionLocal()
    sec = {sid: (code, ind) for sid, code, ind in db.execute(
        select(Security.sid, Security.code, Security.industry)
        .where(Security.market == "A"))}
    prices, bad = load_prices(limit_codes={c for c, _ in sec.values()})
    if bad:
        print(f"[load_prices] 坏档 {sum(bad.values())} 个: "
              + " · ".join(f"{k} {v}" for k, v in bad.items()), flush=True)
    cal = prices.get(CAL)
    if not cal or not cal[2]:
        print("缺交易日历锚(000001)或其 raws——先跑 fetch_daily_prices 补 raws 列")
        return 2

    if a.check_live:
        return check_live(db, sec, prices)

    grids = grid_dates(cal[0], GRID_FROM, GRID_TO)
    panel, miss, n_sids = build_panel(db, sec, prices, grids, schools, discounts,
                                      batches=a.batches)
    db.close()
    miss.update(bad)

    verdict = render(panel, grids, sec, miss, schools, discounts, n_sids)
    if a.dump:
        Path(a.dump).write_text(json.dumps(
            {"panel_size": len(panel), "verdict": {f"{s}|{d}": v for (s, d), v in verdict.items()}},
            ensure_ascii=False), encoding="utf-8")
        print(f"(摘要已写 {a.dump})")
    return 0


def check_live(db, sec, prices):
    """同构对拍:网格=score_daily 里「也是交易日历内一天」的最新日,逐派比对重建 buy_* vs 物化 buy_*。

    对拍日必须落在交易日历里——score_daily 尾部会有假日孤立快照(如 10-01 国庆行),
    那天的买价对的是当时快照价,而价格文件里根本没有那天的收盘,拿下一个交易日凑会
    把差异全记成错。
    """
    cal_dates = prices[CAL][0]
    days = [d for d in db.execute(
        select(ScoreDaily.trade_date).distinct().order_by(ScoreDaily.trade_date.desc()).limit(5)
    ).scalars() if str(d) in cal_dates]
    if not days:
        print("score_daily 近 5 个日期都不在交易日历内,无法对拍")
        return 2
    day = days[0]
    day_iso = str(day)
    rows = db.execute(
        select(ScoreDaily.sid, ScoreDaily.buy_graham_def, ScoreDaily.buy_schloss)
        .where(ScoreDaily.trade_date == day)).all()
    live = {r[0]: {"graham_def": r[1], "schloss": r[2]}
            for r in rows}
    print(f"对拍日:{day_iso}(日历内最新)· score_daily {len(live)} 行")
    sids = [sid for sid, (code, _) in sec.items() if code in prices and sid in live]
    stat = defaultdict(lambda: [0, 0, 0])     # school -> [match, mismatch, 重建缺锚(线在)]
    mism = []
    priced = 0
    for bi in range(0, len(sids), BATCH):
        chunk = sids[bi:bi + BATCH]
        fin = load_batch(db, chunk)
        load_avails(db, chunk, fin)
        for sid in chunk:
            code, _ = sec[sid]
            dates, _, raws = prices[code]
            if raws is None:
                continue
            i = px_idx_at_or_after(dates, day_iso)
            if i is None or raws[i] is None:
                continue
            priced += 1
            buys, _ = refs_at(fin[sid], fin[sid]["div"], raws[i], max(day_iso, dates[i]))
            if buys is None:
                for s in SCHOOLS:
                    if live[sid][s] is not None:
                        stat[s][2] += 1
                continue
            for s in SCHOOLS:
                lv, rc = live[sid][s], buys[s]
                if lv is None and rc is None:
                    stat[s][0] += 1
                elif lv is None or rc is None:
                    stat[s][1] += 1
                    mism.append((code, s, lv, rc))
                elif abs(rc - lv) <= max(0.02, 0.01 * abs(lv)):
                    stat[s][0] += 1
                else:
                    stat[s][1] += 1
                    mism.append((code, s, lv, rc))
    print(f"有名义价可重建 {priced} 家")
    # 差异分类统计 + 全量落盘(供事后解剖)
    out = BACKEND.parent / "_tmp"
    out.mkdir(exist_ok=True)
    (out / "check_live_mism.json").write_text(json.dumps(
        [{"code": c, "school": s_, "live": lv, "rebuild": rc} for c, s_, lv, rc in mism],
        ensure_ascii=False), encoding="utf-8")
    for s_ in SCHOOLS:
        ms = [(lv, rc) for c2, s2, lv, rc in mism if s2 == s_]
        if not ms:
            continue
        only_live = sum(1 for lv, rc in ms if lv is not None and rc is None)
        only_rb = sum(1 for lv, rc in ms if lv is None and rc is not None)
        scaled = [(lv, rc) for lv, rc in ms if lv is not None and rc is not None]
        ratios = sorted(round(rc / lv, 2) for lv, rc in scaled if lv and rc and lv > 0)
        med_r = ratios[len(ratios) // 2] if ratios else None
        print(f"  {s_}: 双有值 {len(scaled)}(中位 rebuild/live={med_r}) · live独有 {only_live} · 重建独有 {only_rb}")
    print(f"{'流派':14s} 一致     不一致   重建缺锚(线在)")
    bad = 0
    for s in SCHOOLS:
        m, x, o = stat[s]
        bad += x
        print(f"{s:14s} {m:6d}  {x:6d}  {o:6d}")
    for c, s, lv, rc in sorted(mism, key=lambda t: -abs((t[2] or 0) - (t[3] or 0)))[:12]:
        print(f"  {c} {s}: live={lv} rebuild={rc}")
    print(f"\n{'同构对拍通过' if bad == 0 else '对拍存在差异 %d 处(先解释再进判决书)' % bad}")
    print("  已知可解释差异:①快照 pe/pb 的源间交叉校验(守卫①③)在合成路径为 None,")
    print("  live 侧多拦/少拦的坏锚会表现为此处不一致;②score_daily 的财务可用日以抓取时")
    print("  点为准,与网格日的可用日可能有公告时差。差异集中在这两类且占比 <5% 可放行。")
    return 0 if bad == 0 else 1
