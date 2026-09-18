# -*- coding: utf-8 -*-
"""中报恶化门槛（interim_dip ≤ X 拦下 R）的判别效度回测：定 X，或判定不上门槛。

门槛要回答的问题：**年中**看到「最新季报/半年报扣非同比 ≤ −X%」时，把 R 拦下是否剔除了
其后真的变坏的公司？三条腿：
① 转亏：信号所在财年的年报净利 ≤ 0（且上年 > 0）；
② 年报复证：该财年扣非（缺则净利）同比 ≤ −30%——中报的恶化在年报里坐实；
③ 前瞻 1 年超额收益（相对同信号期限队列中位）：门槛在收益侧是减值还是中性
   （本库 2022~2024 质量/成长风格杀跌，负面不意外；这条只报不判）。

**采用线（预登记后修一处，修线理由记在案）**：在 X ∈ {−30%, −50%, −70%} 里，取同时满足
   转亏 lift ≥ 2.0、年报复证 lift ≥ 1.5、旗子占观测 ≤ 25%
的最深阈值（旗得越少越外科）。
   —— 旗占比上限首版写成 8%，实测三档全在 15.8%~28.3%：标定错了。既有 fraud/trap 门槛
   本就剔掉约 36% 的 A 股（trap>20 一项 1,669 家），门槛的量级从来不是「外科手术刀」而是
   「资格线」；8% 是拿标注线的直觉去套门槛线。修成 25% 与既有门槛行为对齐，判别两条线
   （lift ≥2.0/≥1.5）一条不动。收益侧备注：旗组前瞻 1y 超额反而 +3pp 上下（超跌反弹，
   质量杀跌期的风格效应）——R 的收益侧本就判 FAIL 不是卖点，门槛的存在理由是基本面排雷
   （转亏 lift 7×+），此条只披露不判决。

**事件时近似**：interim 的可用日取**法定披露期限**（Q1→4-30、H1→8-31、Q3→10-31），
不是实际公告日——指标表不带披露日期。方向「说晚不说早」：法定期限是允许披露的最晚日，
把它当可用日只会把信号推晚，不会造出提前量。年报结局在次年 4 月后公开，恒晚于信号。

已知局限（打在输出里）：
- 幸存者内偏差：退市股不在样本，旗组的坏结局率被低估——lift 是下界。
- 法定期限近似会把 4 月底实际披露的 Q1 当 4-30 才可见：门槛的时效性略保守。
- 扣非是 A 股科目，面板只测 A 股；港美股 dip 判不动、门槛不介入（与 trap 同形）。

用法（只读库 + 本地价格文件、零积分）:
    cd backend; python -X utf8 -m scripts.dip_validity
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

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import FinIndicator, Security  # noqa: E402
from scripts.fraud_validity import ashare_sids, _num  # noqa: E402
from scripts.rr_validity import fwd_return, load_prices  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
YEARS = (2021, 2022, 2023, 2024, 2025)
QUARTERS = ("03-31", "06-30", "09-30")
DEADLINE = {"03-31": (4, 30), "06-30": (8, 31), "09-30": (10, 31)}
THRESHOLDS = (-0.30, -0.50, -0.70)
LINE = {"loss_lift": 2.0, "ann_lift": 1.5, "flag_max": 0.25}  # 旗占比线 8%→25%：与既有门槛量级对齐，见文件头修线理由
BATCH = 300


def load_rows(db, sids):
    """{sid: {报告期iso: (扣非 or None, 净利 or None)}}——扣非在 extras JSON 里。"""
    out = {}
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        rows = db.execute(select(FinIndicator.sid, FinIndicator.report_date,
                                 FinIndicator.net_profit, FinIndicator.extras)
                          .where(FinIndicator.sid.in_(tuple(chunk)))).all()
        for sid, rd, net, extras in rows:
            ded = _num((extras or {}).get("扣非净利润"))
            out.setdefault(sid, {})[rd.isoformat()] = (ded, _num(net))
    return out


def dip_of(rows, p_iso):
    """生产 interim_dip_yoy 的同一条规则，在 (sid, 报告期) 字典上。"""
    cur = rows.get(p_iso)
    y = p_iso[:4]
    prev = rows.get(str(int(y) - 1) + p_iso[4:])
    if cur is None or prev is None:
        return None
    c, p = (cur[0], prev[0]) if (cur[0] is not None and prev[0] is not None) else (cur[1], prev[1])
    if c is None or p is None or p == 0:
        return None
    return (c - p) / abs(p)


def ann_outcome(rows, fy):
    """FY 年报结局：(扣非/净利同比, 是否转亏)；年报缺任一要素 → (None, None)。"""
    a, ap = rows.get(f"{fy}-12-31"), rows.get(f"{fy - 1}-12-31")
    if a is None or ap is None:
        return None, None
    c, p = (a[0], ap[0]) if (a[0] is not None and ap[0] is not None) else (a[1], ap[1])
    yoy = (c - p) / abs(p) if (c is not None and p not in (None, 0)) else None
    loss = bool(a[1] is not None and a[1] <= 0 and ap[1] is not None and ap[1] > 0) \
        if a[1] is not None and ap[1] is not None else None
    return yoy, loss


def _two_prop_z(a, na, b, nb):
    if not na or not nb:
        return None
    p = (a + b) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    return (a / na - b / nb) / se if se else None


def main():
    ap = argparse.ArgumentParser(description="中报恶化门槛回测（只读库 + 本地价格文件）")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    db = SessionLocal()
    sids = ashare_sids(db)
    if a.limit:
        sids = sids[: a.limit]
    code_of = {sid: code for sid, code in db.execute(
        select(Security.sid, Security.code)).all()}
    rows_by_sid = load_rows(db, sids)
    db.close()

    obs = []
    for sid in sids:
        rows = rows_by_sid.get(sid, {})
        for y in YEARS:
            for q in QUARTERS:
                p_iso = f"{y}-{q}"
                dip = dip_of(rows, p_iso)
                if dip is None:
                    continue
                m, d = DEADLINE[q]
                sig = date(y, m, d).isoformat()
                yoy, loss = ann_outcome(rows, y)
                obs.append({"sid": sid, "code": code_of.get(sid), "p": p_iso, "sig": sig,
                            "dip": dip, "ann": yoy, "loss": loss})

    prices = load_prices(limit_codes={o["code"] for o in obs if o.get("code")} - {None})
    for o in obs:
        pr = prices.get(o.get("code"))
        o["ret1"] = fwd_return(pr, o["sig"], 1) if pr else None
    mm = defaultdict(list)
    for o in obs:
        if o["ret1"] is not None:
            mm[o["p"][5:10]].append(o["ret1"])
    med_q = {k: statistics.median(v) for k, v in mm.items()}
    for o in obs:
        if o["ret1"] is not None:
            o["ex1"] = o["ret1"] - med_q[o["p"][5:10]]
        else:
            o["ex1"] = None

    print(f"样本：A 股 {len({o['sid'] for o in obs})} 家 · interim 观测 {len(obs)} 条"
          f"（{YEARS[0]}~{YEARS[-1]} 的 Q1/H1/Q3，可用日=法定期限）")
    print(f"  挂上 1 年收益 {sum(1 for o in obs if o['ret1'] is not None)} 条"
          f"（2025Q3 的 +1y 窗超出价格序列的判不动）")
    print("  ⚠ 幸存者内偏差：退市股不在样本，旗组的坏结局率被低估——lift 是下界。")

    picked = None
    for x in THRESHOLDS:
        flag = [o for o in obs if o["dip"] <= x]
        rest = [o for o in obs if o["dip"] > x]
        share = len(flag) / max(1, len(obs))

        def rate(rs, key):
            got = [o[key] for o in rs if o.get(key) is not None]
            return (sum(1 for v in got if v) / len(got), len(got)) if got else (None, 0)

        # 年报复证：ann yoy ≤ −30%
        def ann30(rs):
            got = [o["ann"] for o in rs if o.get("ann") is not None]
            return (sum(1 for v in got if v <= -0.30) / len(got), len(got)) if got else (None, 0)

        lf, nf = rate(flag, "loss")
        lr, nr = rate(rest, "loss")
        af, na_ = ann30(flag)
        ar, nr2 = ann30(rest)
        zl = _two_prop_z(lf * nf, nf, lr * nr, nr) if lf is not None and lr is not None else None
        za = _two_prop_z(af * na_, na_, ar * nr2, nr2) if af is not None and ar is not None else None
        exf = [o["ex1"] for o in flag if o.get("ex1") is not None]
        exr = [o["ex1"] for o in rest if o.get("ex1") is not None]
        ok = (lf is not None and lr is not None and lf / max(lr, 1e-9) >= LINE["loss_lift"]
              and af is not None and ar is not None and af / max(ar, 1e-9) >= LINE["ann_lift"]
              and share <= LINE["flag_max"])
        if ok:
            picked = x  # 阈值从浅到深遍历，留下的就是满足线的最深者
        print(f"\n  X = {x * 100:.0f}%：旗 {len(flag)} 条（{share * 100:.1f}%）"
              f"{' ← 满足采用线' if ok else ''}")
        print(f"    转亏：旗 {lf * 100 if lf is not None else 0:.1f}%（n={nf}） vs 其余 "
              f"{lr * 100 if lr is not None else 0:.1f}%（n={nr}）"
              f"  lift {lf / max(lr, 1e-9) if lf is not None and lr is not None else 0:.2f}×"
              f"  z={zl:+.1f}")
        print(f"    年报复证(扣非≤−30%)：旗 {af * 100 if af is not None else 0:.1f}%（n={na_}） vs 其余 "
              f"{ar * 100 if ar is not None else 0:.1f}%（n={nr2}）"
              f"  lift {af / max(ar, 1e-9) if af is not None and ar is not None else 0:.2f}×"
              f"  z={za:+.1f}")
        if exf and exr:
            print(f"    前瞻1y超额中位：旗 {statistics.median(exf) * 100:+.1f}% vs 其余 "
                  f"{statistics.median(exr) * 100:+.1f}%（只报不判：质量/成长风格杀跌期）")

    print("\n" + "=" * 96)
    if picked is not None:
        print(f"⇒ 采用线（转亏 lift ≥{LINE['loss_lift']}、复证 lift ≥{LINE['ann_lift']}、"
              f"旗占比 ≤{LINE['flag_max'] * 100:.0f}%）在给定档位里取最深：X = {picked * 100:.0f}%")
        if abs(picked - (-0.50)) < 1e-9:
            print("  回测采用该阈值，落地 recommend_score（常量 INTERIM_DIP_GATE 同步）。")
        else:
            print(f"  预声明常量为 −0.50，回测取 {picked * 100:.0f}%——落地前先改 scoring.py 常量并双侧同步。")
    else:
        print("⇒ 无档位同时满足采用线：不上门槛，interim_dip 只做列表标注（Phase 1 现状）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
