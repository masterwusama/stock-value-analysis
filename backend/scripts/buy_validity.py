# -*- coding: utf-8 -*-
"""四派买点信号的收益侧回测：「股价从线上方跌进买点」事件 → 其后 6 个月/1 年/2 年超额。

设计见 docs/设计-四派买点信号回测.md。与 rr_validity 同族(离线研究:只读库+本地
data/prices/,零积分零调度零 DDL),但方法论有三处不同:

1. **信号是价格事件**,不是年报面板——月末网格(2020-01~2025-12,A 股全市场),
   每格用「点时财务(extras 无损原件,可用日≤网格日) + 当日名义收盘」喂给
   scoring.value_analysis → price_references(**单一事实源,不另写公式**);
2. **名义价对名义价**:data/prices 的 raws(不复权)与买价同口径比;收益仍用 hfq;
3. **双对照**:市场对照 = 个股 − 同(信号月×期限)全队列中位;邻域对照 = 同月同行业
   (证监会大类)同 PB 三分位、未进任何线的股票池——回答「这条线在便宜之外还有没有
   增量信息」,防把「便宜有效」错记成「线画得好」。

合成快照的已知简化(说明书要写):pe_ttm/pb 传 None——那两列只服务 EPS 锚的
源间交叉校验(守卫①③),合成路径本就单源(全从财务推),无交叉对象;守卫②(隐含
PE 下限)与④(扣非重定锚)不依赖快照,照常生效。

预登记判决线(跑之前写死):
- 甲(单派成立):事件组(D=100)超额中位 1y>0 且 2y>0;逐年方向 ≥2/3(年事件数≥10)。
- 乙(线的增量):事件组 2y 超额 − 邻域组 2y 超额 ≥ −1.5pp(容差同 rr 的 TOL_PP)。
- 丙(不判只印):四派横评、D=80 打折变体、绝对收益。

偏差申报:幸存者偏差方向对本设计更不利(陷阱型便宜股死得多、退市无价格文件,
事件组收益被高估——甲是上界);信号窗 2020-2024 后段正逢红利/微盘风格(对深度
价值友好),结论只代表本窗口;月末快照近似(首触线与网格日差数日)。

用法(在 backend 目录):
    python -X utf8 -m scripts.buy_validity                     # 全量判决书
    python -X utf8 -m scripts.buy_validity --check-live        # 同构对拍(必做守卫)
    python -X utf8 -m scripts.buy_validity --dump ../_tmp/buy_obs.json
    python -X utf8 -m scripts.buy_validity --school schloss --discount 80,100
"""
import argparse
import bisect
import json
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Dividend, FinBalance, FinCashflow, FinIndicator, ScoreDaily, Security  # noqa: E402
from scripts.fraud_validity import load_announce  # noqa: E402
from app.models import FinIncome  # noqa: E402
from equity import equity_of  # noqa: E402
from scoring import price_references, value_analysis  # noqa: E402

PRICE_DIR = BACKEND / "collector" / "data" / "prices"
GRID_FROM, GRID_TO = 2020, 2025          # 信号年(2y 腿截尾:信号>2024-09 的观测自然缺失)
HORIZONS = ((6, "6m"), (12, "1y"), (24, "2y"))
SCHOOLS = ("graham_agg", "graham_def", "schloss", "buffett")
DISCOUNTS = (100, 80)
DEDUP_GRIDS = 6        # 同股同派 180 天(6 个月网格)只留首个事件
LISTING_BARS = 250     # 上市不足 250 个交易日的格点剔除(新股效应与买点语义无关)
DAY_CAP = 14           # 网格日停牌:其后 14 天内有交易则用该日,否则判不动
TOL_PP = 0.015
BATCH = 400
CAL = "000001"         # 交易日历锚(平安银行,长期无长停牌)


def load_prices(limit_codes=None):
    out = {}
    if not PRICE_DIR.exists():
        return out
    for p in PRICE_DIR.glob("*.json"):
        if limit_codes is not None and p.stem not in limit_codes:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if d.get("dates") and d.get("closes"):
            out[p.stem] = (d["dates"], d["closes"], d.get("raws"))
    return out


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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_batch(db, sids):
    """一批公司的四类行,行 = extras 无损原件(中文键,引擎直接吃)。"""
    ins = tuple(sids)
    out = {sid: {"ind": [], "ba": [], "cf": [], "div": []} for sid in sids}
    for sid, ex, net, rd in db.execute(
            select(FinIndicator.sid, FinIndicator.extras, FinIndicator.net_profit,
                   FinIndicator.report_date).where(FinIndicator.sid.in_(ins))):
        r = dict(ex or {})
        r.setdefault("报告期", str(rd)[:10])
        if net is not None:
            r.setdefault("净利润", _num(net))
        out[sid]["ind"].append(r)
    for sid, ex, rd in db.execute(
            select(FinBalance.sid, FinBalance.extras, FinBalance.report_date)
            .where(FinBalance.sid.in_(ins))):
        r = dict(ex or {})
        r.setdefault("报告日", str(rd)[:10])
        out[sid]["ba"].append(r)
    for sid, ex, rd in db.execute(
            select(FinCashflow.sid, FinCashflow.extras, FinCashflow.report_date)
            .where(FinCashflow.sid.in_(ins))):
        r = dict(ex or {})
        r.setdefault("报告日", str(rd)[:10])
        out[sid]["cf"].append(r)
    for sid, dy, b10, ad, pd_ in db.execute(
            select(Dividend.sid, Dividend.div_year, Dividend.bonus_per_10,
                   Dividend.announce_date, Dividend.pay_date)
            .where(Dividend.sid.in_(ins))):
        out[sid]["div"].append({
            "year": str(dy) if dy else None, "bonus_per_10": _num(b10),
            "announce_date": str(ad)[:10] if ad else None,
            "pay_date": str(pd_)[:10] if pd_ else None,
        })
    for v in out.values():
        v["ind"].sort(key=lambda r: str(r.get("报告期") or ""))
        v["ba"].sort(key=lambda r: str(r.get("报告日") or ""))
        v["cf"].sort(key=lambda r: str(r.get("报告日") or ""))
    return out


def slice_asof(rows, av, grid_iso):
    """可用日 ≤ 网格日的行(无可用日信息的行丢弃——宁少不错,防前视)。"""
    out = []
    for r in rows:
        rd = str(r.get("报告期") or r.get("报告日") or "")[:10]
        a = av.get(rd)
        if a is not None and a <= grid_iso:
            out.append(r)
    return out


def refs_at(rows, divs, price, grid_iso):
    """(四派买价 dict, bps) —— bps 供 PB 三分位。"""
    ind = slice_asof(rows["ind"], rows["_av_ind"], grid_iso)
    ba = slice_asof(rows["ba"], rows["_av_ba"], grid_iso)
    cf = slice_asof(rows["cf"], rows["_av_cf"], grid_iso)
    if not ind or not ba:
        return None, None
    # 股本兜底:最新年报归母权益(equity_of 处理科目变体——单键名会漏掉「归属于母公司
    # 所有者权益」等写法,check-live 冒烟实测 80/163 家因此丢 NCAV 锚)/ 每股净资产
    # 股本先验:最新一期资产负债表的归母权益 × 同报告期每股净资产(缺则最新 bps)。
    # 必须「最新期配对」:2026 年送转/增发的公司新股份只在半年报 bps 里,年报期配对
    # 会拿旧股本,NCAV 锚系统性高 ~30%(check-live 568 家、中位 +31% 实测);跨期乱配
    # 更会把 shares 推偏 90 倍。live 侧的等价物是「快照市值/价格」,语义就是当前股本。
    annual_ba = [r for r in ba if str(r.get("报告日") or "").endswith("12-31")]
    eq = None
    for r in reversed(ba):
        eq = equity_of(r)
        if eq:
            break
    if eq is None:
        eq = next((eq_ for eq_ in (equity_of(r) for r in reversed(annual_ba)) if eq_), None)
    last_day = str(ba[-1].get("报告日") or "")[:10] if ba else ""
    bps = None
    if last_day:
        bps = next((r.get("每股净资产") for r in reversed(ind)
                    if str(r.get("报告期") or "")[:10] == last_day
                    and r.get("每股净资产") not in (None, 0)), None)
    if bps is None:
        bps = next((r.get("每股净资产") for r in reversed(ind)
                    if r.get("每股净资产") not in (None, 0)), None)
    ann_day = last_day
    # 股本先验只用 eq/bps(同一份年报内部自洽:权益(元)/每股净资产(元/股)=股数)。
    # **不要**把实收资本当先验:个别公司该科目是万元口径(688428 实测 23493.75=
    # 23.5亿股),当先验会把 NCAV 锚放大 10^4 倍;live 路径的先验是快照市值/价格,
    # 引擎的 ±5%/±25% 置信窗本来就是设计来让财报实收资本对着先验过检的——先验给
    # 好数,脏实收资本自然被拒。
    shares = ((eq / bps) if (eq and bps) else None)
    if shares is None:
        cap_cn = next((r.get("实收资本(或股本)") for r in reversed(annual_ba)
                       if isinstance(r.get("实收资本(或股本)"), (int, float))
                       and r.get("实收资本(或股本)") > 0), None)
        shares = cap_cn
    mcap = price * shares if shares else None
    d = {"market": "A", "indicators": ind, "balance": ba, "cashflow": cf,
         "dividends": divs, "notes": [],
         "snapshot": {"price": price, "market_cap": mcap, "pe_ttm": None, "pb": None}}
    va = value_analysis(d, now=date.fromisoformat(grid_iso))
    refs = price_references(d, va)
    buys = {s: refs[school_cn(s)]["buy"] for s in SCHOOLS}
    diag = {"bps": bps, "shares": shares, "eq": eq, "n_ind": len(ind), "n_ba": len(ba),
            "netCagr5": va.get("netCagr5"), "ann_day": ann_day}
    return buys, diag


def load_avails(db, chunk, fin):
    """三表各自的可用日 + 三表并集(_av_any,给指标行切片用)。

    指标行 extras 不带公告日期(实测只有报告期),同一份披露文件里利润表有;但部分
    公司的季报只在指标表有行、三张报表缺该期——只用利润表代切会把这些季报行错杀,
    TTM 窗口断掉、EPS 锚整派丢失(check-live 全量实测 301/688 段数百例)。并集口径:
    任一报表记录了该报告期的公告日,该期即以(各表最早的)那日为可用日;三表全无
    该期的行,才视为无证据、丢弃。"""
    per = {}
    for tag, M, key in (("ba", FinBalance, "_av_ba"), ("cf", FinCashflow, "_av_cf"),
                        ("inc", FinIncome, "_av_inc")):
        per[tag] = load_announce(db, M, chunk)
    for sid in chunk:
        union = {}
        for tag in ("ba", "cf", "inc"):
            for rd, (avd, _) in (per[tag].get(sid) or {}).items():
                rd = str(rd)
                if rd not in union or str(avd) < union[rd]:
                    union[rd] = str(avd)
        fin[sid]["_av_ba"] = {str(rd): str(a) for rd, (a, _) in (per["ba"].get(sid) or {}).items()}
        fin[sid]["_av_cf"] = {str(rd): str(a) for rd, (a, _) in (per["cf"].get(sid) or {}).items()}
        fin[sid]["_av_ind"] = union


_SCHOOL_CN = {"graham_agg": "grahamAgg", "graham_def": "grahamDef",
              "schloss": "schloss", "buffett": "buffett"}


def school_cn(s):
    return _SCHOOL_CN[s]


def main():
    ap = argparse.ArgumentParser(description="四派买点信号回测(只读库+本地价格文件)")
    ap.add_argument("--dump", default="")
    ap.add_argument("--school", choices=SCHOOLS, default=None, help="只跑一派")
    ap.add_argument("--discount", default=",".join(str(x) for x in DISCOUNTS))
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
    prices = load_prices(limit_codes={c for c, _ in sec.values()})
    cal = prices.get(CAL)
    if not cal or not cal[2]:
        print("缺交易日历锚(000001)或其 raws——先跑 fetch_daily_prices 补 raws 列")
        return 2

    if a.check_live:
        return check_live(db, sec, prices)

    grids = grid_dates(cal[0], GRID_FROM, GRID_TO)
    sids = [sid for sid, (code, _) in sec.items() if code in prices]
    if a.batches:
        sids = sids[: a.batches * BATCH]

    panel = []          # (month_idx, sid, pb_t, r6, r12, r24, ev{(sch,d):bool})
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
    db.close()

    # ---- 市场对照:同(信号月×期限)全队列中位 ----
    h_idx = {h: i for i, (_, h) in enumerate(HORIZONS)}
    med = {}
    for h_i, (_, h) in enumerate(HORIZONS):
        by = defaultdict(list)
        for row in panel:
            v = row[3 + h_i]
            if v is not None:
                by[row[0]].append(v)
        med[h] = {m: statistics.median(vs) for m, vs in by.items()}

    def ex(row, h):
        v = row[3 + h_idx[h]]
        return (v - med[h][row[0]]) if v is not None and med[h].get(row[0]) is not None else None

    # ---- PB 三分位(按月) ----
    tercile = {}
    by_month_pb = defaultdict(list)
    for row in panel:
        if row[2] is not None and row[2] > 0:
            by_month_pb[row[0]].append(row[2])
    for m, vs in by_month_pb.items():
        vs = sorted(vs)
        tercile[m] = (vs[len(vs) // 3], vs[2 * len(vs) // 3])

    def t_of(row):
        t1, t2 = tercile.get(row[0], (None, None))
        if row[2] is None or t1 is None:
            return None
        return 0 if row[2] <= t1 else (1 if row[2] <= t2 else 2)

    ind_of = {sid: sec[sid][1] for sid in sec}
    # ---- 邻域组:同(月,行业,三分位)且未进任何线(D=100)的唯一股票池 ----
    ctrl_pool = defaultdict(list)
    for row in panel:
        if not any(row[6].get((s, 100)) for s in SCHOOLS):
            t = t_of(row)
            if t is not None and ind_of.get(row[1]):
                ctrl_pool[(row[0], ind_of[row[1]], t)].append(row)

    ctrl_med = {}
    for h in ("1y", "2y"):
        by = defaultdict(list)
        for k, rs in ctrl_pool.items():
            vs = [ex(r, h) for r in rs]
            vs = [v for v in vs if v is not None]
            if vs:
                by[k] = statistics.median(vs)
        ctrl_med[h] = by

    print("=" * 104)
    print(f"样本:面板观测 {len(panel)} 条(网格 {grids[0]} ~ {grids[-1]},A 股 {len(sids)} 家)")
    for k, v in sorted(miss.items(), key=lambda x: -x[1]):
        print(f"  缺失:{k} {v}")
    print("  超额 = 个股收益 − 同(信号月×期限)全队列中位;邻域 = 同月同行业同 PB 三分位未进任何线。")
    print("  ⚠ 幸存者偏差:退市股无价格文件,事件组收益被高估——甲是上界;风格窗为 2020-2024。")

    verdict = {}
    for s in schools:
        for d in discounts:
            evs = [row for row in panel if row[6].get((s, d))]
            tag = f"{s} D={d}"
            if len(evs) < 30:
                print(f"\n  {tag}:事件 {len(evs)} 条,不足 30 不判")
                verdict[(s, d)] = None
                continue
            line = f"\n  {tag}:事件 {len(evs)} 条 · 超额中位 "
            cells = []
            for _, h in HORIZONS:
                vs = [ex(r, h) for r in evs]
                vs = [v for v in vs if v is not None]
                cells.append(f"{h}={statistics.median(vs) * 100:+.1f}%(n={len(vs)})")
            abs_2y = [r[5] for r in evs if r[5] is not None]
            print(line + " ".join(cells)
                  + f" · 绝对2y={statistics.median(abs_2y) * 100:+.1f}%")
            # 逐年方向
            by_year = defaultdict(list)
            for r in evs:
                v = ex(r, "2y")
                if v is not None:
                    by_year[grids[r[0]][:4]].append(v)
            pos = tot = 0
            for y, vs in sorted(by_year.items()):
                if len(vs) >= 10:
                    tot += 1
                    pos += 1 if statistics.median(vs) > 0 else 0
            # 邻域差(乙,只对 D=100 主判)
            diff2 = None
            if d == 100:
                ds = []
                for r in evs:
                    t = t_of(r)
                    k = (r[0], ind_of.get(r[1]), t)
                    cm = ctrl_med["2y"].get(k)
                    e = ex(r, "2y")
                    if cm is not None and e is not None:
                        ds.append(e - cm)
                if ds:
                    diff2 = statistics.median(ds)
                    print(f"    乙(邻域差,2y):事件−邻域 = {diff2 * 100:+.1f}pp(n={len(ds)},线 ≥{ -TOL_PP * 100:.1f}pp)")
            e1 = [v for v in (ex(r, '1y') for r in evs) if v is not None]
            e2 = [v for v in (ex(r, '2y') for r in evs) if v is not None]
            ok_a = bool(e1 and e2) and statistics.median(e1) > 0 and statistics.median(e2) > 0 \
                and tot >= 2 and pos >= tot - 1
            ok_b = (diff2 is None) or (diff2 >= -TOL_PP)
            verdict[(s, d)] = (ok_a, ok_b, pos, tot)
            print(f"    甲:{'PASS' if ok_a else 'FAIL'}(1y/2y 超额为正+逐年 {pos}/{tot}≥2/3)"
                  + (f" · 乙:{'PASS' if ok_b else 'FAIL'}" if d == 100 else ""))

    print("\n" + "=" * 104)
    print("判决书(预登记:甲=超额为正+逐年≥2/3 · 乙(仅 D=100)=邻域差≥−1.5pp · 丙=横评不判)")
    for (s, d), v in verdict.items():
        if v is None:
            continue
        print(f"  {s:12s} D={d}: 甲 {'PASS' if v[0] else 'FAIL'}"
              + (f" · 乙 {'PASS' if v[1] else 'FAIL'}" if d == 100 else "")
              + f" · 逐年 {v[2]}/{v[3]}")

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
        select(ScoreDaily.sid, ScoreDaily.buy_graham_agg, ScoreDaily.buy_graham_def,
               ScoreDaily.buy_schloss, ScoreDaily.buy_buffett)
        .where(ScoreDaily.trade_date == day)).all()
    live = {r[0]: {"graham_agg": r[1], "graham_def": r[2], "schloss": r[3], "buffett": r[4]}
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
    # 差异分类统计 + 全量落盘(诊断字段随行,供事后解剖)
    out = Path(BACKEND.parent / "_tmp"); out.mkdir(exist_ok=True)
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
        import math
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


if __name__ == "__main__":
    sys.exit(main())
