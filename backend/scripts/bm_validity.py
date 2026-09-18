# -*- coding: utf-8 -*-
"""商业模式特征标签的预研：五个候选标签各自定阈值、跑历史面板，数据决定谁过关。

预研问题（与「打总分」路线的分歧记录在案）：不给商业模式打 0~100 分——跨行业可比性
会崩（V 分的银行占榜已演示过一次），且假精确。改做**逐项亮灯的特征标签**：每个标签
独立定义、独立回测、独立过/不上线；用户看到的是「预收模式 ✓ 轻资产 ✗」，判断留给人。

五个候选标签（全部只读年报科目，锚点 = 信号日可见的最近年报）：
1. prepay  预收模式    ：(合同负债＋预收款项) ÷ 营收 ≥ 阈值        ——先收钱后交付
2. light   轻资产自供  ：capex ÷ 经营现金流 ≤ 阈值（要求 OCF>0）   ——扩张不靠烧钱
3. ar_low  客户不占款  ：应收账款 ÷ 营收 ≤ 阈值                    ——下游不拖欠
4. pricing 定价权      ：毛利率 − 5 年前毛利率 ≥ −3pp              ——没被通胀/竞争磨掉
5. inc_roic增量回报    ：3 年 Δ净利 ÷ Δ总资产 ≥ 阈值；Δ资产≤0 且 Δ净利>0 直接亮灯
                          ——资产不增长利润还在涨，是最好的资本纪律

结局（与 r_validity/rr_validity 同一套）：其后第一份年报的转亏 / 减值≥5%净资产；
前瞻 1 年超额收益（相对同信号年队列中位，hfq 点对点，价格来自 data/prices/）。

**预登记判据（一条不动）**：
- 单标签过关：存在阈值档，使 旗占比 ∈ [5%, 40%]、其后转亏 lift ≤ 0.85，
  且 1 年超额差（旗 − 其余）≥ −1pp（收益侧只设「不明显反向」的底线——本窗
  2022~2024 质量/成长风格杀跌，rr_validity 已证收益侧对质量类证据苛刻）。
- 组合过关：持 ≥3 枚 vs 持 0 枚，转亏率差 ≥ 5pp 或 1 年超额差 ≥ 3pp。
- 单标签全灭 ⇒ 该标签不上线（特征标签只上被数据认过的）；组合不过 ⇒ 标签
  只做独立展示、不做组合叙事。

已知局限：幸存者内偏差（退市股不在样本，好标签的坏结局率被低估）；合同负债
2020 年前叫预收款项，两键并读；增量回报的 Δ总资产含并购扰动。

用法（只读库 + 本地价格文件、零积分）:
    cd backend; python -X utf8 -m scripts.bm_validity
    python -X utf8 -m scripts.bm_validity --limit 200      # 冒烟
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

YEARS = (2021, 2022, 2023, 2024)
# (key, 中文, 方向, 阈值档)：ge = 值≥阈值亮灯；le = 值≤阈值亮灯
TAGS = (
    ("prepay",   "预收模式", "ge", (0.10, 0.20, 0.30)),
    ("light",    "轻资产自供", "le", (0.4, 0.6, 0.8)),
    ("ar_low",   "客户不占款", "le", (0.10, 0.20, 0.30)),
    ("pricing",  "定价权", "ge", (-0.03, -0.01, 0.0)),
    ("inc_roic", "增量回报", "ge", (0.10, 0.15, 0.25)),
)
LINE_FLAG = (0.05, 0.40)
LINE_LOSS_LIFT = 0.85
LINE_EX1 = -0.01


def extra_facts(g, sid):
    """面板标准事实之外的三样：合同负债(＋预收款项)、capex、折旧——按年报年归档。"""
    out = {}
    for ex, _p, _t, rd in g["ba"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        vals = [x for x in (_num(ex.get("合同负债")), _num(ex.get("预收款项"))) if x is not None]
        if vals:
            out.setdefault(rd.year, {})["contract"] = sum(vals)
    for ex, rd in g["cf"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        cx = _num(ex.get("购建固定资产、无形资产和其他长期资产所支付的现金"))
        dp = _num(ex.get("固定资产折旧、油气资产折耗、生产性生物资产折旧"))
        if cx is not None:
            out.setdefault(rd.year, {})["capex"] = cx
        if dp is not None:
            out.setdefault(rd.year, {})["dep"] = dp
    return out


def bm_tags(f, yrs):
    """锚点 = 可见年报序列最后一年 → {标签: 原始值|None}；历史不足 3 期整体判不动。"""
    out = {k: None for k, _, _, _ in TAGS}
    out["ay"] = None
    if len(yrs) < 3:
        return out
    ay = yrs[-1]
    out["ay"] = ay
    cur = f[ay]
    rev, ta, net = cur.get("rev"), cur.get("ta"), cur.get("net")
    ct = cur.get("contract")
    if ct is not None and rev:
        out["prepay"] = ct / rev
    cx, ocf = cur.get("capex"), cur.get("ocf")
    if cx is not None and ocf and ocf > 0:
        out["light"] = cx / ocf
    ar = cur.get("ar")
    if ar is not None and rev:
        out["ar_low"] = ar / rev
    gm_now = cur.get("gm")
    old5 = f.get(ay - 5)
    if gm_now is not None and old5 and old5.get("gm") is not None:
        out["pricing"] = gm_now - old5["gm"]
    old3 = f.get(ay - 3)
    if net is not None and old3 and old3.get("net") is not None \
            and ta is not None and old3.get("ta") is not None:
        dn, dta = net - old3["net"], ta - old3["ta"]
        out["inc_roic"] = (999.0 if dn > 0 else (0.0 if dn < 0 else None)) if dta <= 0 else dn / dta
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
            for y, ex in extra_facts(g, sid).items():
                if y in f:
                    f[y].update(ex)
            for t in years:
                sig = av.of(date(t, 12, 31))
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                tags = bm_tags(f, yrs)
                if tags["ay"] is None:
                    counts["公开年报不足3期"] += 1
                    continue
                outs, _carrier = outcomes_at(f, tags["ay"], t, sig, set(), [])
                obs.append({"sid": sid, "t": t, "sig": sig.isoformat(), "tags": tags, "o": outs})
    db.close()
    return obs, counts


def main():
    ap = argparse.ArgumentParser(description="商业模式特征标签预研（只读库 + 本地价格文件）")
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
    print("=" * 104)
    print(f"样本：A 股 {len({o['sid'] for o in obs})} 家 · 观测 {n} 条 · 信号年 {YEARS}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("  预登记线：旗占比 5%~40% 且 转亏 lift ≤ 0.85 且 1y 超额差 ≥ −1pp（风格窗底线）")

    passed = {}
    for key, lab, direc, ths in TAGS:
        print("\n" + "-" * 104)
        print(f"◆ {lab}（值 {'>=' if direc == 'ge' else '<='} 阈值亮灯）")
        for th in ths:
            flag = [o for o in obs if o["tags"][key] is not None
                    and ((o["tags"][key] >= th) if direc == "ge" else (o["tags"][key] <= th))]
            rest = [o for o in obs if o["tags"][key] is None
                    or not ((o["tags"][key] >= th) if direc == "ge" else (o["tags"][key] <= th))]
            share = len(flag) / max(1, len(obs))

            def loss_rate(rs):
                got = [o["o"].get("loss") for o in rs if o["o"].get("loss") is not None]
                return (sum(1 for x in got if x) / len(got)) if got else None

            def imp5_rate(rs):
                got = [o["o"].get("imp5") for o in rs if o["o"].get("imp5") is not None]
                return (sum(1 for x in got if x) / len(got)) if got else None

            lf, lr = loss_rate(flag), loss_rate(rest)
            lift = (lf / lr) if (lf is not None and lr) else None
            exf = [o["ex1"] for o in flag if o["ex1"] is not None]
            exr = [o["ex1"] for o in rest if o["ex1"] is not None]
            spread = (statistics.median(exf) - statistics.median(exr)) if exf and exr else None
            ok = (LINE_FLAG[0] <= share <= LINE_FLAG[1] and lift is not None
                  and lift <= LINE_LOSS_LIFT and spread is not None and spread >= LINE_EX1)
            if ok:
                passed[key] = th
            print(f"  阈值 {th:>5}: 旗 {share * 100:5.1f}% | 转亏 {lf * 100 if lf is not None else 0:4.1f}% vs "
                  f"{lr * 100 if lr is not None else 0:4.1f}%  lift {lift if lift is not None else 0:.2f} | "
                  f"减值5% lift {(imp5_rate(flag) / max(imp5_rate(rest), 1e-9)) if imp5_rate(flag) and imp5_rate(rest) else 0:.2f} | "
                  f"1y超额差 {(spread * 100) if spread is not None else 0:+.1f}pp"
                  + ("  ← 过线" if ok else ""))

    print("\n" + "=" * 104)
    # 组合：按中途档阈值数亮灯数
    held_counts = Counter()
    for o in obs:
        cnt = 0
        for key, lab, direc, ths in TAGS:
            th = passed.get(key, ths[len(ths) // 2])
            v = o["tags"][key]
            if v is not None and ((v >= th) if direc == "ge" else (v <= th)):
                cnt += 1
        o["held"] = cnt
        held_counts[cnt] += 1
    print("组合亮灯数（中途档）分布：" + " · ".join(f"{k}枚 {v}条" for k, v in sorted(held_counts.items())))

    def band_stats(lo, hi):
        rs = [o for o in obs if lo <= o["held"] <= hi]
        got = [o["o"].get("loss") for o in rs if o["o"].get("loss") is not None]
        lr = (sum(1 for x in got if x) / len(got)) if got else None
        ex = [o["ex1"] for o in rs if o["ex1"] is not None]
        return (lr, len(got), statistics.median(ex) if ex else None)

    l0, n0, e0 = band_stats(0, 0)
    l3, n3, e3 = band_stats(3, 5)
    ok_comp = (l0 is not None and l3 is not None
               and ((l0 - l3) >= 0.05 or (e0 is not None and e3 is not None and e3 - e0 >= 0.03)))
    print(f"  持 0 枚：转亏 {l0 * 100 if l0 is not None else 0:.1f}%（n={n0}） 1y超额 "
          f"{e0 * 100 if e0 is not None else 0:+.1f}%")
    print(f"  持 ≥3 枚：转亏 {l3 * 100 if l3 is not None else 0:.1f}%（n={n3}） 1y超额 "
          f"{e3 * 100 if e3 is not None else 0:+.1f}%")
    comp_ok = (l0 is not None and l3 is not None and (l0 - l3) >= 0.05) or \
              (e0 is not None and e3 is not None and (e3 - e0) >= 0.03)
    print(f"  组合线（转亏差 ≥5pp 或 超额差 ≥3pp）：{'过' if comp_ok else '不过'}")

    print("\n" + "=" * 104)
    print("预研结论")
    print("=" * 104)
    if passed:
        names = {k: lab for k, lab, _, _ in TAGS}
        print("  过线标签（含采用阈值）：" +
              "、".join(f"{names[k]} ≥{passed[k]:g}" if k in ('prepay', 'inc_roic', 'pricing')
                        else f"{names[k]} ≤{passed[k]:g}" for k in TAG_ORDER if k in passed))
    else:
        print("  无标签过线：五个候选全部不上线，特征标签方向按预登记判据否决。")
    if comp_ok:
        print("  组合亮灯数与结局单调/有差 ⇒ 上线后可展示「亮灯数」作为辅助信息。")
    else:
        print("  组合线不过 ⇒ 上线后只做独立标签展示，不做亮灯数叙事。")
    print("  ⚠ 幸存者内偏差：退市股不在样本，好标签的坏结局率被低估——lift 是下界。")
    return 0


TAG_ORDER = tuple(k for k, _, _, _ in TAGS)

if __name__ == "__main__":
    sys.exit(main())
