# -*- coding: utf-8 -*-
"""点时合成面:从 DB 装载 extras 无损原件,按可用日切片,拼成公司 dict 喂
scoring 的原函数算四派买价——不另写第二份公式(单一事实源)。

check-live 对拍逼出来的三条硬经验,改动前先读注释:
1. 指标行切片用**三表可用日并集**(见 load_avails);
2. 股本先验用**最新一期归母权益 × 同报告期每股净资产**(见 refs_at);
3. **不要**把实收资本当先验(万元口径科目会把 NCAV 锚放大 10⁴ 倍)。
"""
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.models import Dividend, FinBalance, FinCashflow, FinIndicator, FinIncome  # noqa: E402
from scripts.fraud_validity import load_announce  # noqa: E402
from equity import equity_of  # noqa: E402
from scoring import price_references, value_analysis  # noqa: E402

from . import SCHOOLS  # noqa: E402

BATCH = 400

_SCHOOL_CN = {"graham_agg": "grahamAgg", "graham_def": "grahamDef",
              "schloss": "schloss", "buffett": "buffett"}


def school_cn(s):
    return _SCHOOL_CN[s]


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


def load_avails(db, chunk, fin):
    """三表各自的可用日 + 三表并集(_av_ind,给指标行切片用)。

    指标行 extras 不带公告日期(实测只有报告期),同一份披露文件里利润表有;但部分
    公司的季报只在指标表有行、三张报表缺该期——只用利润表代切会把这些季报行错杀,
    TTM 窗口断掉、EPS 锚整派丢失(check-live 全量实测 301/688 段数百例)。并集口径:
    任一报表记录了该报告期的公告日,该期即以(各表最早的)那日为可用日;三表全无
    该期的行,才视为无证据、丢弃。"""
    per = {}
    for tag, M in (("ba", FinBalance), ("cf", FinCashflow), ("inc", FinIncome)):
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
    """(四派买价 dict, 诊断 dict)。诊断含 bps(供 PB 三分位)与股本先验细目。"""
    ind = slice_asof(rows["ind"], rows["_av_ind"], grid_iso)
    ba = slice_asof(rows["ba"], rows["_av_ba"], grid_iso)
    cf = slice_asof(rows["cf"], rows["_av_cf"], grid_iso)
    if not ind or not ba:
        return None, None
    # 股本先验:最新一期资产负债表的归母权益 × 同报告期每股净资产(缺则最新 bps)。
    # 必须「最新期配对」:2026 年送转/增发的公司新股份只在半年报 bps 里,年报期配对
    # 会拿旧股本,NCAV 锚系统性高 ~30%(check-live 568 家、中位 +31% 实测);跨期乱配
    # 更会把 shares 推偏 90 倍。live 侧的等价物是「快照市值/价格」,语义就是当前股本。
    # **不要**把实收资本当先验:个别公司该科目是万元口径(688428 实测 23493.75=
    # 23.5亿股),当先验会把 NCAV 锚放大 10^4 倍;引擎的 ±5%/±25% 置信窗本来就是设计
    # 来让财报实收资本对着先验过检的——先验给好数,脏实收资本自然被拒。
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
    shares = ((eq / bps) if (eq and bps) else None)
    if shares is None:
        shares = next((r.get("实收资本(或股本)") for r in reversed(annual_ba)
                       if isinstance(r.get("实收资本(或股本)"), (int, float))
                       and r.get("实收资本(或股本)") > 0), None)
    mcap = price * shares if shares else None
    d = {"market": "A", "indicators": ind, "balance": ba, "cashflow": cf,
         "dividends": divs, "notes": [],
         "snapshot": {"price": price, "market_cap": mcap, "pe_ttm": None, "pb": None}}
    va = value_analysis(d, now=date.fromisoformat(grid_iso))
    refs = price_references(d, va)
    buys = {s: refs[school_cn(s)]["buy"] for s in SCHOOLS}
    diag = {"bps": bps, "shares": shares, "eq": eq, "n_ind": len(ind), "n_ba": len(ba),
            "netCagr5": va.get("netCagr5"), "ann_day": last_day}
    return buys, diag
