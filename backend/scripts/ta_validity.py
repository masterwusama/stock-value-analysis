# -*- coding: utf-8 -*-
"""交易性金融资产折算系数定标预研：净现金里的「交易性×c」该按几折计入？

问题（加权类现金梯子的校准）：交易性金融资产现按 ×0.7 折算（介于货币资金 ×1.0
与其他流动资产 ×0.3 之间）。用户质疑：公允价值计量、市价可询，×0.7 过度保守，
按市价 ×1.0 才合理。不靠观点拍板——扫描系数 c，看哪个 c 让「净现金」对**其后
真实坏结局**的判别力最强，那个 c 就是数据验证的标定值。

三个可分假说：
- H1 交易性≈现金（c→1.0）：公允价值计量 + 交易所流动性，市价即真值（格雷厄姆
  net-cash 原文「现金＋有价证券按市价」）
- H2 交易性有实质折价（c≤0.7）：A 股构成不透明（理财/资管/非上市股权混装）、
  公允价值评估弹性大、fire sale 折价
- H0 中性：构成不改变结局

测试设计（事件时面板，A 股 2021~2023，同 dip/bm 预研机器）：
对每个公司×信号年：net_cash(c) = 货币资金 + c×交易性金融资产 − 负债合计（全部
取锚点年报行），outcome = 其后第一份年报的转亏 / 减值≥5%净资产。
判别力 = net_cash(c) 五分位的 Q1−Q5 转亏率差（Q1=净现金最差）；**c* = 使差最大
的系数**——真值若为 v，则 c=v 的版本最如实刻画 cushion，判别力最大。

**预登记判据**：
- c* ∈ [0.85, 1.0] → 现行 0.7 偏保守有实证，上调交易性折算至 c*
- c* ∈ [0.55, 0.85) → 0.7 恰当，维持
- c* < 0.55 → 交易性比现金口径预想更"虚"，下调
- 全程以「货币资金纯态（c 只作用于现金）」做占位对照：c 的判别力若与纯现金版本
  无差别，说明交易性构成在结局上与货币资金无异，折算系数失去意义

构成侧证据（第二题）：fin_share = 交易性÷(货币资金＋交易性) 三分组的其后
转亏率/超额收益——回答「持有交易性资产本身是否更危险」。

已知局限：幸存者内偏差；构成混杂（理财/股票/资管不可分）；2022~2024 风格窗
对收益侧的污染（仅作参考列）。

用法：cd backend; python -X utf8 -m scripts.ta_validity [--limit 200]
"""
import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from app.db import SessionLocal  # noqa: E402
from scripts.fraud_validity import (Avail, ashare_sids, load_announce,  # noqa: E402
                                    load_batch, _num)
from scripts.g_validity import quintiles_of  # noqa: E402
from scripts.rr_validity import fwd_return, load_prices  # noqa: E402
from scripts.trap_validity import TABLES, fy_facts, outcomes_at  # noqa: E402
from scripts.v_validity import BATCH  # noqa: E402

YEARS = (2021, 2022, 2023)
COEFS = (0.4, 0.7, 0.85, 1.0)


def cash_facts(g, sid):
    """货币资金 / 交易性金融资产 / 负债合计，按年报年归档。"""
    out = {}
    for ex, _p, _t, rd in g["ba"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        d = out.setdefault(rd.year, {})
        for k, col in (("cash", "货币资金"), ("fin", "交易性金融资产"), ("tl", "负债合计")):
            v = _num(ex.get(col))
            if v is not None:
                d[k] = v
    return out


def run(years, limit=0):
    db = SessionLocal()
    sids = ashare_sids(db)
    if limit:
        sids = sids[:limit]
    obs, counts = [], Counter()
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g = load_batch(db, chunk)
        ann = {tag: load_announce(db, M, chunk) for tag, M in TABLES}
        for sid in chunk:
            if not g["ind"].get(sid):
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = fy_facts(g, av, {}, sid)
            cf_extra = cash_facts(g, sid)
            for y, ex in cf_extra.items():
                if y in f:
                    f[y].update(ex)
            for t in years:
                sig = av.of(date(t, 12, 31))
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                if len(yrs) < 3:
                    counts["公开年报不足3期"] += 1
                    continue
                ay = yrs[-1]
                cur = f[ay]
                cash, fin, tl = cur.get("cash"), cur.get("fin"), cur.get("tl")
                if cash is None or tl is None:
                    counts["缺货币资金或负债合计"] += 1
                    continue
                outs, _carrier = outcomes_at(f, ay, t, sig, set(), [])
                obs.append({"sid": sid, "t": t, "sig": sig.isoformat(), "ay": ay,
                            "cash": cash, "fin": fin or 0.0, "tl": tl,
                            "o": outs})
    db.close()
    return obs, counts


def main():
    ap = argparse.ArgumentParser(description="交易性金融资产折算系数定标（只读库+本地价格）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    obs, counts = run(YEARS, a.limit)
    db = SessionLocal()
    from sqlalchemy import select  # noqa: E402
    from app.models import Security  # noqa: E402
    code_of = dict(db.execute(select(Security.sid, Security.code)).all())
    db.close()
    prices = load_prices(limit_codes={code_of.get(o["sid"]) for o in obs} - {None})
    for o in obs:
        pr = prices.get(code_of.get(o["sid"]))
        o["ret1"] = fwd_return(pr, o["sig"], 1) if pr else None
    med_q = {}
    for o in obs:
        if o["ret1"] is not None:
            med_q.setdefault(o["t"], []).append(o["ret1"])
    med_q = {k: statistics.median(v) for k, v in med_q.items()}
    for o in obs:
        o["ex1"] = (o["ret1"] - med_q[o["t"]]) if o["ret1"] is not None else None

    n = len(obs)
    for o in obs:
        pool = o["cash"] + o["fin"]
        o["fin_share"] = (o["fin"] / pool) if pool > 0 else None
    print("=" * 100)
    print(f"样本：A 股 {len({o['sid'] for o in obs})} 家 · 观测 {n} 条 · 信号年 {YEARS}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("  口径：净现金(c) = 货币资金 + c×交易性金融资产 − 负债合计（锚点年报行）")
    print("  判别力 = 五分位 Q1(净现金最差) 转亏率 − Q5(最好) 转亏率，越大 = 该系数越如实刻画 cushion")

    def loss_spread(rs, key):
        qs, _ = quintiles_of([o for o in rs if o.get(key) is not None], lambda o: o[key])
        if not qs:
            return None
        rates = []
        for q in qs:
            got = [o["o"].get("loss") for o in q if o["o"].get("loss") is not None]
            rates.append(sum(1 for x in got if x) / len(got) if got else None)
        if any(x is None for x in rates):
            return None
        return rates[0] - rates[-1]

    print("\n" + "-" * 100)
    print("◆ 系数扫描：净现金(c) 五分位 → 其后转亏率差（Q1−Q5，越大 = 高净现金越安全）")
    best_c, best_spread = None, None
    for c in COEFS:
        key = f"nc{c}"
        for o in obs:
            if o["cash"] is not None and o["tl"] is not None:
                o[key] = o["cash"] + c * (o["fin"] or 0.0) - o["tl"]
            else:
                o[key] = None
        sp = loss_spread(obs, key)
        if sp is not None and (best_spread is None or sp > best_spread):
            best_c, best_spread = c, sp
        print(f"  c={c:<4}: Q1−Q5 转亏率差 {sp * 100 if sp is not None else 0:+.2f}pp")
    print(f"  ⇒ 转亏判别力最大的系数 c* = {best_c}")

    print("\n" + "-" * 100)
    print("◆ 构成侧：交易性÷(货币资金＋交易性) 三分组 → 其后转亏率 / 减值5% / 1y 超额")
    fin_only = [o for o in obs if o["fin_share"] is not None]
    ter = _terciles(fin_only)
    for i, q in enumerate(ter):
        got = [o["o"].get("loss") for o in q if o["o"].get("loss") is not None]
        lr = sum(1 for x in got if x) / len(got) if got else None
        ex = [o["ex1"] for o in q if o["ex1"] is not None]
        print(f"    T{i+1}（交易性占比 {['低','中','高'][i]}）n={len(q)}  "
              f"转亏 {lr * 100 if lr is not None else 0:.1f}%  "
              f"1y超额 {(statistics.median(ex) * 100) if ex else 0:+.1f}pp")

    print("\n" + "=" * 100)
    print("预研结论")
    print("=" * 100)
    if best_c is None:
        print("  样本不足以定标。")
        return 0
    lo, hi = 0.55, 0.85
    if best_c >= 0.85:
        act = f"上调交易性折算至 {best_c}（接近市价，格雷厄姆原文口径）"
    elif best_c >= lo:
        act = "现行 0.7 恰当，维持"
    else:
        act = f"下调交易性折算至 {best_c}（交易性资产实证风险高于现金）"
    print(f"  实证标定 c* = {best_c}（转亏判别力最大）→ {act}")
    print("  ⚠ 构成混杂不可分（理财/股票/资管混装）+ 幸存者内偏差；系数定标为面板统计，")
    print("    非资产真实价值——上线与否仍需结合「折算梯子服务的问题」（偿债压力 vs 估值缓冲）判断。")
    return 0


def _terciles(rs):
    rs = sorted(rs, key=lambda o: o["fin_share"])
    k = max(1, len(rs) // 3)
    return [rs[:k], rs[k:2 * k], rs[2 * k:]]


if __name__ == "__main__":
    sys.exit(main())
