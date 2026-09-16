# -*- coding: utf-8 -*-
"""陷阱分项的判别效度回测：事件时信息集下的 16 个分项 → 6 类可观测结局。

为什么要跑它：拟议的「陷阱折损 T」是一组扣分项，而扣分项在这个仓库里有两个已付过代价的坑
——缺项当 0 分会把"没抓到"判成"没有"（造假分那轮踩过），以及同一个变量在一处加分、在另一处
扣分（商誉那轮就是这么摘掉护城河 3 分的）。所以定权重之前，每个分项先单独量一遍判别力。
16 项里 14 项是候选证据，另两项不进 T：商誉暴露当**标尺**用（上一轮实测它「商誉+无形/总资产
≥10%」在减值结局上 lift 2.36×、z +20.68，本轮同一列跑出同量级才说明接线是对的），造假分当
**对照**用（量一下新分项相对现成量有没有增量）。

入 T 的门槛不是 z：样本 3.2 万条观测时 z 普遍几十倍，单看它什么也筛不掉。三条都要过——
最强结局的 lift ≥ 1.4、逐年方向一致（6 个 T 年里坏侧发生率都高于好侧）、以及在**连续值**层面
与已入选的分项不相关（|r| ≤ 0.5，见 `hazard`）。lift < 1 是方向反了，这类分项压根不能按拟议的
符号进 T，本脚本实测有两项如此（见「判决书」）。

与 `fraud_validity.py` 的关系：时点机器直接 import 它那套（`Avail` / `load_announce` /
`score_at` / 事件时结局窗 / 两比例 z），以免两处各写一份「什么时候能看到」的判定而漂移；
结局口径与分项则完全是另一套，不复用它的结论。

口径（继承 fraud_validity 前四条，另加三条本脚本特有的）：
1. 分项只喂「信号日当天真的公开了」的年报行（可用日 = 公告日期与更新日期次日取更早、
   且落在报告期后 10~400 天带内的那个；都不合格才退法定期限，兜底只会说晚不会说早）。
2. 结局取信号日之后公开的第一份年报（上限 18 个月 = 548 天，只挡停牌级披露空洞）。
3. **判不动不进分母**：缺科目/缺权益/缺基期值的逐项给 None，不当 0。
4. 只测 A 股：减值科目名、定增/回购源、分红归属年度口径都只在这一侧成立。
5. **分项一律取斜率/持续性/中断，不取水平**——水平归拟议的价值分 V，同变量取不同统计量
   才不至于两处重复计分（ROE 拆中位数那轮的先例）。
6. 增长率类先夹到 ±50%/年再分档（winsorize 输入而不是输出：一个 −90% 的样本会把三分位
   边界整个拖走）。摊薄不用"股本"列的增减——送股/转增会把它放大却不摊薄任何人的权益，
   故分母取 归母权益/每股净资产 的隐含股本，分子只数实际发行的那部分。
7. 定增/回购的可观测史由上游决定（实测 5,468 行定增 / 2,678 家、5,410 行回购 / 2,872 家，
   每家最多 8 笔定增、10 笔回购，2010 年起），所以这两族与「新定增」结局都有左右截断。

分项坏侧怎么定：`lo`/`hi` = 最坏的那个三分位（边界按各分项自己的可得样本算），`bin` = 取值 1，
`pos` = 大于 0（零膨胀项不能用三分位，否则边界塌在 0 上、全员进坏侧），阈值类照名。

用法（只读库、零积分、不需要服务在跑）:
    cd backend; python -X utf8 -m scripts.trap_validity
    python -X utf8 -m scripts.trap_validity --years 2021 2022 2023
    python -X utf8 -m scripts.trap_validity --orthogonal
    python -X utf8 -m scripts.trap_validity --detail bvps_g5 seo_dilu
"""
import argparse
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (Dividend, FinBalance, FinCashflow, FinIncome,  # noqa: E402
                        ShareAction)
from scripts.fraud_validity import (WINDOW_MAX, Avail, ashare_sids, load_announce,  # noqa: E402
                                    load_audit, load_batch, score_at, ztest, _d, _num, _plus)

BATCH = 300
# 有息负债全口径：与 scoring.py 的 int_debt 同五个科目（缺键当 0）
IDEBT_KEYS = ("短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债")
TABLES = (("ba", FinBalance), ("cf", FinCashflow), ("inc", FinIncome))
G_CAP = 0.5      # 增长率 winsorize 边界（±50%/年）

# (key, 中文标签, 坏侧规则)
FEATS = (
    ("bvps_g5",   "每股净资产5年增速",         "lo"),
    ("equity_g5", "归母净资产5年增速",         "lo"),
    ("rev_g5",    "营收5年增速",               "lo"),
    ("roe_delta", "ROE最新−5年中位",           "lo"),
    ("gm_delta",  "毛利率最新−5年中位",        "lo"),
    ("ocfnp_med", "净现比5年中位",             "lo"),
    ("ded_ratio", "扣非/报告净利",             "lo"),
    ("ded_half",  "扣非<0.5×报告净利",         "bin"),
    ("wc_gap",    "(应收+存货)增速−营收增速",  "hi"),
    ("debt_gap",  "有息负债增速−营收增速",      "hi"),
    ("seo_dilu",  "近5年定增摊薄/股本",         "pos"),
    ("seo_ocf",   "近5年定增募资/经营现金流",   "pos"),
    ("no_div3",   "近3个财年无现金分红",        "bin"),
    ("no_cxl5",   "近5年无注销式回购",          "bin"),
    ("gw_asset",  "商誉/总资产（标尺）",        "ge0.10"),
    ("fraud",     "造假分（现成量，对照）",     "gt50"),
)
OUTS = (("loss", "转亏"), ("imp5", "减值≥5%"), ("imp3", "减值≥3%"),
        ("divcut", "分红中断"), ("bvpsdn", "BVPS降≥10%"), ("seo", "新定增"))
OKEYS = [k for k, _ in OUTS]
# 定权只准用这组"变坏了"的结局（减值按两条线各算一次）。「新定增」是公司做的一件事而不是一个
# 坏结局（它衡量的是融资活跃度，做回购的公司同样更爱做定增），拿它定权会把方向反了的
# 分项抬进合成分——上一版就是这么让「近5年无注销式回购」以 ln=1.01 混进来的。
DETER = ("loss", "imp5", "imp3", "divcut", "bvpsdn")
RULES = {"lo": lambda v, c: v <= c[0], "hi": lambda v, c: v >= c[1], "bin": lambda v, c: v >= 1.0,
         "pos": lambda v, c: v > 0, "ge0.10": lambda v, c: v >= 0.10, "gt50": lambda v, c: v > 50}
LABEL = {k: lab for k, lab, _ in FEATS}
# 「增长水平」那一族——它们同时是拟议成长分 G 的主料。留在 T 里，T 的第二名就等于把
# 「这家公司这几年净资产不怎么涨」又数了一遍，两轴退化成一条轴。--orthogonal 剔掉它，
# 量 T 在只保留「减速 + 盈利质量 + 稀释 + 减值弹药」时还剩多少判别力。
GROWTH_LVL = ("bvps_g5", "equity_g5", "rev_g5")


def _g(cur, prev, span):
    """年化增速，夹到 ±G_CAP。基期缺失或非正 → None（这列压根算不出，不进分母）。"""
    if cur is None or prev is None or span <= 0 or prev <= 0:
        return None
    if cur <= 0:
        return -G_CAP                       # 基期为正、当期转负：记成下界，不是"算不出"
    return max(-G_CAP, min(G_CAP, (cur / prev) ** (1.0 / span) - 1.0))


def _med(vals):
    got = [v for v in vals if v is not None]
    return statistics.median(got) if len(got) >= 3 else None


def fy_facts(g, av, audit, sid):
    """{财年: 那年年报的事实}，四表都只收 12-31 行合成一行，外加可用日与非标意见。"""
    f = {}

    def slot(y):
        return f.setdefault(y, {})

    for ex, net, rd in g["ind"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        d = slot(rd.year)
        for k, col in (("net", "净利润"), ("rev", "营业总收入"), ("roe", "净资产收益率"),
                       ("gm", "销售毛利率"), ("bps", "每股净资产"), ("ded", "扣非净利润")):
            v = _num(ex.get(col))
            if v is not None:
                d[k] = v
        if "net" not in d and net is not None:
            d["net"] = float(net)
    for ex, p, t, rd in g["ba"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        d = slot(rd.year)
        eq = float(p) if p is not None else (float(t) if t is not None else None)
        if eq is not None:
            d["eq"] = eq
        for k, col in (("ta", "资产总计"), ("gw", "商誉"), ("ar", "应收账款"), ("inv", "存货")):
            v = _num(ex.get(col))
            if v is not None:
                d[k] = v
        got = [_num(ex.get(c)) for c in IDEBT_KEYS]
        if any(v is not None for v in got):
            d["idebt"] = sum(v for v in got if v is not None)
    for ex, rd in g["cf"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        v = _num(ex.get("经营活动产生的现金流量净额"))
        if v is not None:
            slot(rd.year)["ocf"] = v
    for ex, rd in g["inc"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        tot, hit = 0.0, False
        for k in ("资产减值损失", "信用减值损失"):
            v = _num(ex.get(k))
            if v is not None:
                tot -= v                    # 准则以负数列示损失，取负还原成"损失为正"
                hit = True
        if hit:
            slot(rd.year)["loss"] = tot
    for y, d in f.items():
        end = date(y, 12, 31)
        d["avail"] = av.of(end)
        d["aud"] = audit.get((sid, y))
    return f


def feats_at(f, yrs, seo_num, seo_amt, cxl, div_years):
    """信息集下的分项 → ({key: float|None}, 锚定财年)；公开年报不足 3 期则整体 None。

    锚点取「信号日可见的最近一期的年报年」而不是 T 年——拖延披露的公司 FY-T 年报当天还没公开，
    拿 f[t] 会直接 KeyError；这类样本恰恰是高危的，不能剔，所以整个 5 年窗跟着锚点走。
    """
    if len(yrs) < 3:
        return None, None
    ay = yrs[-1]
    win = yrs[-5:]                              # 截至锚点的最近 5 期，不足 5 期用现有的
    cur, first = f[ay], f[win[0]]
    span = ay - win[0]
    out = {k: None for k, _, _ in FEATS}
    out["bvps_g5"] = _g(cur.get("bps"), first.get("bps"), span)
    out["equity_g5"] = _g(cur.get("eq"), first.get("eq"), span)
    out["rev_g5"] = _g(cur.get("rev"), first.get("rev"), span)
    for key, col in (("roe_delta", "roe"), ("gm_delta", "gm")):
        med = _med([f[y].get(col) for y in win])
        if med is not None and cur.get(col) is not None:
            out[key] = cur[col] - med
    out["ocfnp_med"] = _med([f[y]["ocf"] / f[y]["net"] for y in win
                             if (f[y].get("ocf") is not None) and (f[y].get("net") or 0) > 0])
    dd, npt = cur.get("ded"), cur.get("net")
    if dd is not None and npt:
        out["ded_ratio"] = max(-1.0, min(2.0, dd / npt))
        if npt > 0:
            out["ded_half"] = 1.0 if dd < 0.5 * npt else 0.0
    rg, lo_y, hi_y = out["rev_g5"], win[0], ay
    base = (first.get("ar") or 0.0) + (first.get("inv") or 0.0)
    now = (cur.get("ar") or 0.0) + (cur.get("inv") or 0.0)
    if rg is not None and base > 0:
        out["wc_gap"] = (_g(now, base, span) if now > 0 else G_CAP) - rg
    if rg is not None and (first.get("idebt") or 0) > 0:
        dg = _g(cur.get("idebt"), first["idebt"], span)
        if dg is not None:
            out["debt_gap"] = dg - rg
    # 股本事件类：只数锚点年报里已经发生的（信号日当天，日历年 ≤ ay 的事件按定义都已公开）
    issued = sum(v for d_, v in seo_num if lo_y <= d_.year <= hi_y)
    raised = sum(v for d_, v in seo_amt if lo_y <= d_.year <= hi_y)
    sh = None
    if cur.get("eq") and cur.get("bps") and cur["bps"] > 0:
        sh = cur["eq"] / cur["bps"]             # 隐含股本：避开送转股对"股本"列的污染
    # 「没做过定增」是公开事实而不是缺失（A 股定增史全量可查），所以无事件记 0；
    # 只有"有定增但分母算不出"才留 None 当判不动——把缺项一律当 0 正是造假分那轮的坑。
    out["seo_dilu"] = 0.0 if issued == 0 else (issued / sh if sh else None)
    ocf_sum = sum(f[y]["ocf"] for y in win if (f[y].get("ocf") or 0) > 0)
    if raised == 0:
        out["seo_ocf"] = 0.0
    elif ocf_sum > 0:
        out["seo_ocf"] = min(5.0, raised / ocf_sum)
    out["no_div3"] = 0.0 if any((ay - k) in div_years for k in (1, 2, 3)) else 1.0
    out["no_cxl5"] = 0.0 if any(lo_y <= d_.year <= hi_y for d_ in cxl) else 1.0
    if (cur.get("ta") or 0) > 0 and cur.get("gw") is not None:
        out["gw_asset"] = cur["gw"] / cur["ta"]
    return out, ay


def outcomes_at(f, base_y, t, sig, div_years, seo_dates):
    """六类结局 → ({key: True/False/None}, 载体财年)；None = 判不动，不进该列分母。

    基期用锚点年 base_y（信号日真的看得到那一份），载体年仍要求严格晚于信号年 T。
    """
    cands = [y for y in sorted(f) if y > t and sig < f[y]["avail"] <= _plus(sig, WINDOW_MAX)]
    if not cands:
        return dict.fromkeys(OKEYS, None), None
    y = cands[0]
    d = f[y]
    out = {}
    loss, eq = d.get("loss"), d.get("eq")
    out["imp5"] = out["imp3"] = None
    if loss is not None and eq and eq > 0:
        out["imp5"], out["imp3"] = loss >= 0.05 * eq, loss >= 0.03 * eq
    base = f.get(base_y) or {}
    nt, ny = base.get("net"), d.get("net")
    out["loss"] = None if (nt is None or ny is None or nt < 0) else ny < 0
    out["divcut"] = None if (y - 1) not in div_years else (y not in div_years)
    bt, by = base.get("bps"), d.get("bps")
    out["bvpsdn"] = None if (bt is None or by is None or bt <= 0) else by < 0.9 * bt
    out["seo"] = any(sig < dt <= _plus(sig, WINDOW_MAX) for dt in seo_dates)
    return out, y


class Cell:
    __slots__ = ("n", "hit", "obs")

    def __init__(self):
        self.n = dict.fromkeys(OKEYS, 0)
        self.hit = dict.fromkeys(OKEYS, 0)
        self.obs = 0

    def add(self, outs):
        self.obs += 1
        for k in OKEYS:
            o = outs.get(k)
            if o is not None:
                self.n[k] += 1
                self.hit[k] += 1 if o else 0

    def rate(self, k):
        return self.hit[k] / self.n[k] if self.n[k] else None


def quantiles(vals, ps):
    s = sorted(vals)
    return [s[int(round((len(s) - 1) * p))] for p in ps]


def load_events(db, sids):
    """分红归属年 + 定增（股本/金额）+ 注销式回购，四张按 sid 索引的小表。"""
    div_years, seo_num, seo_amt, cxl = (defaultdict(set), defaultdict(list),
                                        defaultdict(list), defaultdict(list))
    for sid, dy, bonus in db.execute(select(Dividend.sid, Dividend.div_year, Dividend.bonus_per_10)
                                     .where(Dividend.sid.in_(tuple(sids)))):
        m = str(dy or "")[:4]
        if m.isdigit() and int(m) >= 1990 and (bonus or 0) > 0:
            div_years[sid].add(int(m))
    for sid, kind, d1, d2, nd, fin, num, raise_, ct in db.execute(
            select(ShareAction.sid, ShareAction.kind, ShareAction.issue_date,
                   ShareAction.listing_date, ShareAction.notice_date, ShareAction.finish_date,
                   ShareAction.num, ShareAction.raise_funds, ShareAction.cancel_type)
            .where(ShareAction.sid.in_(tuple(sids)))):
        if kind == "seo":
            dt = _d(d1) or _d(d2)
            if dt is None:
                continue
            if num:
                seo_num[sid].append((dt, float(num)))
            if raise_:
                seo_amt[sid].append((dt, float(raise_)))
        elif ct == "注销":                      # 三态里的「待核/非注销」不进这项：那是没判动，不是没注销
            dt = _d(fin) or _d(nd)
            if dt is not None:
                cxl[sid].append(dt)
    for rows in seo_num.values():
        rows.sort(key=lambda r: r[0])
    n_seo_at_cap = sum(1 for rows in seo_num.values() if len(rows) >= 8)
    return div_years, seo_num, seo_amt, cxl, n_seo_at_cap


def run(years, limit=0):
    db = SessionLocal()
    sids = ashare_sids(db)
    if limit:
        sids = sids[:limit]
    audit = load_audit(db)
    obs, counts = [], Counter()
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g = load_batch(db, chunk)
        ann = {tag: load_announce(db, M, chunk) for tag, M in TABLES}
        dv, sn, sa, cx, cap = load_events(db, chunk)
        counts["seo_at_cap"] += cap
        for sid in chunk:
            ind, ba, cf = g["ind"].get(sid, []), g["ba"].get(sid, []), g["cf"].get(sid, [])
            if not ind:
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = fy_facts(g, av, audit, sid)
            d_y, s_num, s_amt, cx5 = (dv.get(sid, set()), sn.get(sid, []),
                                      sa.get(sid, []), cx.get(sid, []))
            s_dates = [dt for dt, _ in s_num]
            for t in years:
                sig = av.of(date(t, 12, 31))
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                feats, ay = feats_at(f, yrs, s_num, s_amt, cx5, d_y)
                if feats is None:
                    counts["公开年报不足3期"] += 1
                    continue
                if ay < t:
                    counts["锚点早于信号年（拖延披露）"] += 1
                feats["fraud"], _ = score_at(ind, ba, cf, av, t, "公告日")
                outs, y = outcomes_at(f, ay, t, sig, d_y, s_dates)
                if y is None:
                    counts["事件窗内没出年报"] += 1
                counts["观测"] += 1
                rec = {"sid": sid, "t": t, "y": y, "n_fy": len(yrs), "o": outs}
                rec.update(feats)
                obs.append(rec)
    db.close()
    return obs, counts


def _wide(s):
    """CJK 字符占两列，按显示宽度补齐。"""
    return sum(1 for ch in s if ord(ch) > 0x2E7F)


def _pad(s, w):
    return s + " " * max(0, w - len(s) - _wide(s))


def _cell(bad, rest, k):
    r1, r2 = bad.rate(k), rest.rate(k)
    z = ztest(bad.hit[k], bad.n[k], rest.hit[k], rest.n[k])
    if z is None:
        return _pad("—", 17)
    lift = (r1 / r2) if (r1 and r2) else None
    tag = "*" if abs(z) >= 1.96 else " "
    ls = f"{lift:.2f}x" if lift else "  - "
    return _pad(f"{ls} z{z:+.1f}{tag}", 17)


def pooled(obs, cuts):
    """{分项: (坏侧 Cell, 其余 Cell, 可评估观测数)}，外加 {(分项, T): (坏, 其余)} 供逐年核对。"""
    out, per_t = {}, {}
    years = sorted({r["t"] for r in obs})
    for k, lab, kind in FEATS:
        bad, rest, n_eval = Cell(), Cell(), 0
        cells = {t: (Cell(), Cell()) for t in years}
        for r in obs:
            v = r.get(k)
            if v is None:
                continue
            side = RULES[kind](v, cuts[k])
            c = bad if side else rest
            c.add(r["o"])
            cells[r["t"]][0 if side else 1].add(r["o"])
            n_eval += 1
        out[k] = (bad, rest, n_eval)
        per_t.update({(k, t): cs for t, cs in cells.items()})
    return out, per_t, years


def wins(per_t, years, k, ok):
    """几个 T 年上坏侧发生率高于其余——同一个方向要在 6 年里都站得住，才不是靠某一年撑起来的。"""
    both = [(b.rate(ok), o.rate(ok)) for b, o in (per_t[(k, t)] for t in years)
            if b.rate(ok) is not None and o.rate(ok) is not None]
    return sum(1 for a, b in both if a > b), len(both)


def _indicators(obs, cuts):
    """每条观测的坏侧指示 0/1（分项不可评估 → None，合成时当中性 0）。"""
    xs = []
    for r in obs:
        d = {}
        for k, lab, kind in FEATS:
            v = r.get(k)
            d[k] = None if v is None else (1 if RULES[kind](v, cuts[k]) else 0)
        xs.append(d)
    return xs


def _vc(obs, k, j):
    """两个分项在**连续值**层面的皮尔逊 r：去重必须在值层面做。
    坏侧 0/1 指示的 phi 会严重低估同源度——两个各命中 30% 的指示，哪怕底层变量 r=0.80，
    phi 也只有 0.4 上下，于是「每股净资产增速」与「归母净资产增速」会双双入选、把同一件事计两遍。
    """
    return _pear([(r.get(k), r.get(j)) for r in obs
                  if r.get(k) is not None and r.get(j) is not None])


def hazard(obs, cuts, lifts, top_o, cons, xs, drop=(), title="全候选"):
    """合成危险比：正交且逐年同向的分项按 ln(lift) 相加，看合起来能拉开多少。"""
    years = sorted({r["t"] for r in obs})
    sel, skip = [], []
    for k, lab, kind in sorted(FEATS, key=lambda x: -lifts.get(x[0], 0)):
        if k in drop:
            skip.append(f"{lab}（增长水平，归成长分 G）")
            continue
        if lifts.get(k, 0) < 1.4:
            skip.append(f"{lab}（lift {lifts.get(k, 0):.2f} < 1.4）")
            continue
        if cons[k][0] < cons[k][1]:
            skip.append(f"{lab}（逐年 {cons[k][0]}/{cons[k][1]} 不同向）")
            continue
        hit = next((j for j in sel if abs(_vc(obs, k, j) or 0) > 0.5), None)
        if hit is not None:
            skip.append(f"{lab}（与「{LABEL[hit]}」同源 r={_vc(obs, k, hit):+.2f}）")
            continue
        sel.append(k)
    if not sel:
        print("\n（没有正交成员可合成）")
        return

    # leave-one-T-year-out 的 ln(lift) 权重：同一条观测的分不参与给自己定权
    w = {t: {} for t in years}
    for t in years:
        sub = [(r, xs[i]) for i, r in enumerate(obs) if r["t"] != t]
        for k in sel:
            ok = top_o[k]
            h1 = n1 = h2 = n2 = 0
            for r, x in sub:
                if x[k] is None:
                    continue
                o = r["o"].get(ok)
                if o is None:
                    continue
                if x[k]:
                    h1, n1 = h1 + (1 if o else 0), n1 + 1
                else:
                    h2, n2 = h2 + (1 if o else 0), n2 + 1
            r1, r2 = (h1 / n1 if n1 else None), (h2 / n2 if n2 else None)
            w[t][k] = math.log(r1 / r2) if (r1 and r2 and r1 / r2 > 1.0) else 0.0

    c = []
    miss = []
    for r, x in zip(obs, xs):
        c.append(sum(w[r["t"]][k] * (x[k] or 0) for k in sel))
        miss.append(sum(1 for k in sel if x[k] is None))
    q = quantiles(c, [0.2, 0.4, 0.6, 0.8])
    print("\n" + "=" * 122)
    print("合成危险比 C = Σ ln(lift_i)·x_i · " + title +
          "（权重按 leave-one-T-year-out 估；缺项记 x=0 当中性）")
    print("=" * 122)
    print("入选 " + str(len(sel)) + " 项：" + " · ".join(
        f"{LABEL[k]} ln={statistics.mean([w[t][k] for t in years]):.2f}" for k in sel))
    print("落选 " + str(len(skip)) + " 项：" + " · ".join(skip))
    mm = sum(miss) / len(miss)
    print(f"每条观测平均有 {mm:.2f}/{len(sel)} 项按缺项当中性——这就是「假清白」的重量，"
          f"分数低不等于没陷阱")

    print("\n  名义危险比 exp(C) 的分布：")
    edges = [(-9, 0.5, "<0.5"), (0.5, 1.0, "0.5~1"), (1.0, 2.0, "1~2"),
             (2.0, 4.0, "2~4"), (4.0, 8.0, "4~8"), (8.0, 1e9, "≥8")]
    for lo, hi, lab in edges:
        n = sum(1 for v in c if lo <= math.exp(v) < hi)
        bar = "#" * int(round(n / len(c) * 60))
        print(f"    {lab:<6}{n:>7}  {n / len(c) * 100:5.1f}%  {bar}")

    def gid(v):
        return 0 if v <= q[0] else (1 if v <= q[1] else (2 if v <= q[2] else (3 if v <= q[3] else 4)))

    cells = [Cell() for _ in range(5)]
    seg = [[] for _ in range(5)]
    for i, r in enumerate(obs):
        g5 = gid(c[i])
        cells[g5].add(r["o"])
        seg[g5].append(c[i])
    print("\n  C 五分位 → 实测结局发生率：")
    print("  " + _pad("档", 6) + _pad("观测数", 9) + _pad("C 区间", 16) + _pad("名义HR", 11)
          + "".join(_pad(lb, 12) for _, lb in OUTS))
    for gi, cc in enumerate(cells):
        lo, hi = min(seg[gi]), max(seg[gi])
        print("  " + _pad(f"Q{gi+1}", 6) + _pad(str(cc.obs), 9)
              + _pad(f"{lo:.2f}~{hi:.2f}", 16)
              + _pad(f"{math.exp(statistics.mean(seg[gi])):.1f}x", 11)
              + "".join(_pad(f"{cc.rate(k2)*100:.2f}%" if cc.rate(k2) is not None else "—", 12)
                        for k2, _ in OUTS))
    top, rest = cells[4], cells[0]
    nom = math.exp(statistics.mean(seg[4]) - statistics.mean(seg[0]))
    print(f"\n  Q5 vs Q1（名义危险比 {nom:.1f}x）：")
    for ok, lb in OUTS:
        r1, r2 = top.rate(ok), rest.rate(ok)
        z = ztest(top.hit[ok], top.n[ok], rest.hit[ok], rest.n[ok])
        if not (r1 and r2) or z is None:
            continue
        obs_hr = r1 / r2
        note = f"乘性假设高估 {nom/obs_hr:.1f} 倍" if nom > obs_hr else "实测比名义更强"
        print(f"    {lb:<12} 实测 {obs_hr:5.2f}x（z={z:+.1f}） 名义 {nom:6.1f}x  {note}")


def detail_grid(obs, cuts, k, lab, kind):
    vals = [r[k] for r in obs if r.get(k) is not None]
    if kind not in ("lo", "hi") or len(vals) < 30:
        print(f"\n—— {lab}：非三分位分项，坏侧见总表 ——")
        return
    c0, c1 = cuts[k]
    labs = ["低三分位", "中三分位", "高三分位"] if kind == "hi" else ["差(低)", "中", "好(高)"]
    cells = {l: Cell() for l in labs}
    for r in obs:
        v = r.get(k)
        if v is None:
            continue
        l = labs[0] if v <= c0 else (labs[2] if v >= c1 else labs[1])
        cells[l].add(r["o"])
    print(f"\n—— {lab} 三分档（边界 {c0:+.3f} / {c1:+.3f}）——")
    print("  " + _pad("档位", 14) + _pad("观测数", 12) + "".join(_pad(lb, 12) for _, lb in OUTS))
    for l in labs:
        c = cells[l]
        print("  " + _pad(l, 14) + _pad(str(c.obs), 12)
              + "".join(_pad(f"{c.rate(k2)*100:.2f}%" if c.rate(k2) is not None else "—", 12)
                        for k2, _ in OUTS))


def report(obs, counts, cuts, detail, orthogonal=False):
    print(f"样本：A 股 {len({r['sid'] for r in obs})} 家 · 观测 {counts['观测']} 条 "
          f"（T ∈ {sorted({r['t'] for r in obs})}）")
    for k in ("公开年报不足3期", "事件窗内没出年报", "锚点早于信号年（拖延披露）", "无指标行"):
        if counts[k]:
            print(f"  {k}: {counts[k]} 条")
    print(f"  定增行数已达上游 8 笔上限的公司 {counts['seo_at_cap']} 家——这些公司的早年定增不可见，"
          f"「新定增」结局与两个定增分项都有右删失")
    yv = Counter(str(r["y"]) for r in obs)
    print("  结局载体财年：" + " · ".join(f"{k} {v}" for k, v in sorted(yv.items())))
    print("  判不动一律不进分母（缺基期/缺科目/缺上年分红），缺失当 0 会把发生率整体压低。\n")

    P, per_t, years = pooled(obs, cuts)
    print("=" * 122)
    print("坏侧 vs 其余：lift × 两比例 z（* = |z|≥1.96）")
    print("=" * 122)
    print(_pad("分项", 26) + _pad("坏侧n", 9) + "".join(_pad(lb, 17) for _, lb in OUTS)
          + _pad("覆盖率", 8) + "最强结局逐年")
    zmax, top_o = {}, {}
    for k, lab, kind in FEATS:
        bad, rest, n_eval = P[k]
        zs = []
        for ok in OKEYS:
            z = ztest(bad.hit[ok], bad.n[ok], rest.hit[ok], rest.n[ok])
            zs.append(abs(z) if z is not None else 0.0)
        zmax[k] = max(zs)
        cand = [(ztest(bad.hit[ok], bad.n[ok], rest.hit[ok], rest.n[ok]), ok) for ok in DETER]
        sig = [(z, ok) for z, ok in cand
               if z is not None and abs(z) >= 1.96 and bad.rate(ok) and rest.rate(ok)]
        pick = max(sig, key=lambda x: bad.rate(x[1]) / rest.rate(x[1]))[1] if sig \
            else max(DETER, key=lambda i: zs[OKEYS.index(i)])
        top_o[k] = pick
        w, n_yr = wins(per_t, years, k, top_o[k])
        print(_pad(lab, 26) + _pad(str(bad.obs), 9)
              + "".join(_cell(bad, rest, ok) for ok in OKEYS)
              + _pad(f"{n_eval / max(1, len(obs)) * 100:.0f}%", 8) + f"{w}/{n_yr}")

    print("\n" + "=" * 122)
    print("判决书（按最强结局的 lift 排序。n≈3.2 万时 z 普遍几十倍，单看 z 筛不掉东西；"
          "lift<1 = 方向反了，这个分项不能按拟议的符号进 T）")
    print("=" * 122)
    lmax, lifts, cons = {}, {}, {}
    for k, _, _ in FEATS:
        bad, rest, _ = P[k]
        ok = top_o[k]
        r1, r2 = bad.rate(ok), rest.rate(ok)
        lifts[k] = lmax[k] = (r1 / r2) if (r1 and r2) else 0.0
    for k, lab, kind in sorted(FEATS, key=lambda x: -lmax[x[0]]):
        bad, rest, _ = P[k]
        ok = top_o[k]
        w, n_yr = wins(per_t, years, k, ok)
        cons[k] = (w, n_yr)
        lb = dict(OUTS)[ok]
        r1, r2 = bad.rate(ok), rest.rate(ok)
        flag = "  ←方向反了" if lmax[k] < 1 else ""
        print(f"  lift={lmax[k]:5.2f}x  坏侧 {r1*100:5.2f}% vs 其余 {r2*100:5.2f}%  "
              f"{w}/{n_yr} 年同向  最强在「{lb}」  {lab}{flag}")

    xi = _indicators(obs, cuts)
    hazard(obs, cuts, lifts, top_o, cons, xi)
    if orthogonal:
        # 一次跑里并排两个变体：为比较而跑两遍全样本是 4 分钟，没必要
        hazard(obs, cuts, lifts, top_o, cons, xi, drop=GROWTH_LVL,
               title="正交变体（增长水平项已让给成长分 G）")

    print("\n" + "=" * 122)
    print("重叠度（皮尔逊 r，两列都有值的同一条观测才算；|r|>0.5 说明两处会在同一个量上重复计分）")
    print("=" * 122)
    print(_pad("分项", 28) + _pad("vs 造假分", 16) + _pad("vs BVPS5年增速", 16) + "配对样本")
    for k, lab, kind in FEATS:
        if k == "fraud":
            continue
        pr = [(r["fraud"], r[k]) for r in obs if r.get("fraud") is not None and r.get(k) is not None]
        pb = [(r["bvps_g5"], r[k]) for r in obs
              if r.get("bvps_g5") is not None and r.get(k) is not None and k != "bvps_g5"]
        rr, rb = _pear(pr), _pear(pb)
        print(_pad(lab, 28)
              + _pad(f"{rr:+.3f}" if rr is not None else "—", 16)
              + _pad(f"{rb:+.3f}" if rb is not None else "—", 16)
              + str(len(pr)))

    for k in detail:
        lab = dict((x[0], x[1]) for x in FEATS).get(k, k)
        detail_grid(obs, cuts, k, lab, dict((x[0], x[2]) for x in FEATS)[k])


def _pear(pairs):
    n = len(pairs)
    if n < 3:
        return None
    sx = sum(a for a, _ in pairs)
    sy = sum(b for _, b in pairs)
    sxx = sum(a * a for a, _ in pairs)
    syy = sum(b * b for _, b in pairs)
    sxy = sum(a * b for a, b in pairs)
    den = ((n * sxx - sx * sx) * (n * syy - sy * sy)) ** 0.5
    return None if den <= 0 else (n * sxy - sx * sy) / den


def main():
    ap = argparse.ArgumentParser(description="陷阱分项判别效度回测（只读库、零积分）")
    ap.add_argument("--years", nargs="*", type=int, default=[2019, 2020, 2021, 2022, 2023, 2024])
    ap.add_argument("--detail", nargs="*", default=[], help="这些分项额外打印三分档明细")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 家（冒烟测用）")
    ap.add_argument("--orthogonal", action="store_true",
                    help="再打印一个剔掉「增长水平」项的合成变体（那几项归成长分 G）")
    a = ap.parse_args()
    obs, counts = run(a.years, a.limit)
    cuts = {}
    for k, lab, kind in FEATS:
        vals = [r[k] for r in obs if r.get(k) is not None]
        cuts[k] = quantiles(vals, [1 / 3, 2 / 3]) if kind in ("lo", "hi") and len(vals) > 10 else (0.0, 0.0)
    report(obs, counts, cuts, a.detail, a.orthogonal)


if __name__ == "__main__":
    main()
