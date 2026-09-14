# -*- coding: utf-8 -*-
"""造假分（详情页 ⑥ 那 0~100 的红旗分）判别效度回测：T 年的分 → T+1 年的三类结局。

为什么要跑它：`fraud` 是 8 项量化红旗加权出来的一个数，系统里已经拿它做两件事——列表页
门槛筛选、以及估值分位刷池（>50 的不刷 Wind）。可它到底有没有判别力一直没验过：如果
「红旗高」与「次年真的出事」无关，这一列就只是装饰，拿它筛人就是错的。本脚本就是那一次
验证，口径全在库里，不花积分、不碰 Wind、不改任何数据。

怎么做的（三条都不妥协，否则结论没意义）：
1. **T 年的分必须按 T 年可得的数据重算**——不能拿 `score_daily` 的最新分往回贴，那样
   T+1 年的财报会漏进 T 年的分数里，测出来的是「已经出事」而不是「会出事」。做法是把
   财报序列截断到 T-12-31 之后，调线上同一个 `scoring.fraud_analysis`。
2. **结局只算判得动的**：该年年报没有审计意见的行不进非标口径的分母，缺减值科目或缺
   净资产的不进减值分母。缺数据不等于没出事，硬当 0 会把发生率整体压低、把 lift 做没。
3. **只测 A 股**：结局三件套（审计意见、利润表「资产减值损失/信用减值损失」科目名）与
   红旗口径同源，港股没有审计意见、美股财年归一且科目名不同一套，硬并进来只会污染分母。

结局定义（T+1 = Y）：
- `非标`   —— FY Y 年报审计意见不在 `标准无保留意见`/`无保留意见` 之内（`periodic_report`
              的期次从 `title` 解析，那表的 `report_date` 是公告日，见 §10.4）
- `大额减值` —— Y 年「资产减值损失＋信用减值损失」的净损失 ≥ 当年期末归母净资产的 5%
              （次要口径 3% 一并给出）。利润表这两项按准则以负数列示，故取负号还原成损失；
              银人口径的「减值及拨备」不计入——拨备是常态科目，不是爆雷信号
- `转亏`   —— Y 年净利润 < 0 且 T 年 ≥ 0（盈利公司次年转负）

用法(只读库，不需要服务在跑):
    cd backend; python -X utf8 -m scripts.fraud_validity
    python -X utf8 -m scripts.fraud_validity --years 2022 2023 2024 --imp-share 0.05 0.03
"""
import argparse
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (FinBalance, FinCashflow, FinIncome, FinIndicator,  # noqa: E402
                        PeriodicReport, Security)
from scoring import fraud_analysis  # noqa: E402  线上同一个实现，不在脚本里复算

AUDIT_CLEAN = {"标准无保留意见", "无保留意见"}   # 与 import_legacy.AUDIT_CLEAN 同集
IMP_KEYS = ("资产减值损失", "信用减值损失")
RPT_YEAR = re.compile(r"(20\d\d)\s*年")
# 分档按整十切，与列表页门槛（造假 ≤）和刷池口径（>50 不刷）大致对齐，
# 但不照抄 50 那条线——否则被人问「50 与 51 凭什么不同档」
BUCKETS = ((0, 20, "低 <20"), (20, 40, "中 20~40"), (40, 60, "高 40~60"), (60, 101, "极高 ≥60"))
LABS = [b[2] for b in BUCKETS]
OUTCOMES = ("aud", "imp", "imp3", "loss")
BATCH = 300   # 一批 300 家的四表行：全 A 股一次读会到 GB 量级，分批就只在内存里留一批


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(r):
    return "      —" if r is None else f"{r * 100:6.2f}%"


def ashare_sids(db):
    """A 股 sid 升序清单——分批的边界，也是把港美股挡在外面的那一步。"""
    return [sid for (sid,) in db.execute(
        select(Security.sid).where(Security.market == "A").order_by(Security.sid))]


def load_audit(db):
    """(sid, 财报年份) → 该年年报是否非标；没有意见的年份压根不进这个 dict（= 不进分母）。"""
    out = {}
    for sid, title, opinion in db.execute(
            select(PeriodicReport.sid, PeriodicReport.title, PeriodicReport.audit_opinion)
            .where(PeriodicReport.category == "年报", PeriodicReport.audit_opinion.isnot(None))):
        m = RPT_YEAR.search(str(title or ""))
        if not m:
            continue
        key = (sid, int(m.group(1)))
        bad = str(opinion).strip() not in AUDIT_CLEAN
        out[key] = out.get(key, False) or bad
    return out


def load_batch(db, sids):
    """一批公司的四张表 → sid: 行列表。每行都带原始中文科目行（`extras` 即无损原件），
    指标表另带 `net_profit`、资产负债表另带两列权益，结局口径直接用它，不再从中文科目猜。"""
    ins = tuple(sids)
    out = {k: defaultdict(list) for k in ("ind", "ba", "cf", "inc")}
    for sid, ex, net in db.execute(
            select(FinIndicator.sid, FinIndicator.extras, FinIndicator.net_profit)
            .where(FinIndicator.sid.in_(ins)).order_by(FinIndicator.sid, FinIndicator.report_date)):
        out["ind"][sid].append((ex or {}, net))
    for sid, ex, parent, total, rd in db.execute(
            select(FinBalance.sid, FinBalance.extras, FinBalance.equity_parent,
                   FinBalance.equity_total, FinBalance.report_date)
            .where(FinBalance.sid.in_(ins)).order_by(FinBalance.sid, FinBalance.report_date)):
        out["ba"][sid].append((ex or {}, parent, total, rd))
    for sid, ex in db.execute(
            select(FinCashflow.sid, FinCashflow.extras)
            .where(FinCashflow.sid.in_(ins)).order_by(FinCashflow.sid, FinCashflow.report_date)):
        out["cf"][sid].append(ex or {})
    for sid, ex in db.execute(
            select(FinIncome.sid, FinIncome.extras)
            .where(FinIncome.sid.in_(ins)).order_by(FinIncome.sid, FinIncome.report_date)):
        out["inc"][sid].append(ex or {})
    return out


def annual_row(rows, date_key, year):
    """取该年 12-31 那条（利润表/指标的年报行本身就是全年累计值）。"""
    want = f"{year}-12-31"
    for row in rows:
        if str(row.get(date_key) or "")[:10] == want:
            return row
    return None


def imp_loss(inc_row):
    """净减值损失（正＝计提、负＝转回）；两个科目都缺给 None（不进分母）。"""
    if not inc_row:
        return None
    tot, hit = 0.0, False
    for k in IMP_KEYS:
        v = _num(inc_row.get(k))
        if v is not None:
            tot -= v      # 准则以负数列示损失，取负还原成「损失为正」
            hit = True
    return tot if hit else None


def score_at(ind, ba, cf, t_year):
    """把财报序列截断到 T-12-31 后重算红旗分：与线上 `fraud_analysis` 同一实现、同一入参形状。"""
    cut = f"{t_year}-12-31"
    d = {"indicators": [r for r in ind if str(r.get("报告期") or "")[:10] <= cut],
         "balance": [r for r in ba if str(r.get("报告日") or "")[:10] <= cut],
         "cashflow": [r for r in cf if str(r.get("报告日") or "")[:10] <= cut]}
    return fraud_analysis(d) if d["indicators"] else None


def bucket_of(score):
    for lo, hi, lab in BUCKETS:
        if lo <= score < hi:
            return lab
    return LABS[-1]


class Cell:
    """一个「档 × 结局」的格子：命中数 / 判得动数，外加该档观测数（重算出分的次数）。"""

    def __init__(self):
        self.n = dict.fromkeys(OUTCOMES, 0)
        self.hit = dict.fromkeys(OUTCOMES, 0)
        self.observations = 0

    def add(self, key, outcome):
        if outcome is None:
            return
        self.n[key] += 1
        self.hit[key] += 1 if outcome else 0

    def rate(self, key):
        return self.hit[key] / self.n[key] if self.n[key] else None


def corr(pairs):
    """分与 0/1 结局的相关系数（点二列）：一个数概括「分高是否更可能出事」。"""
    n = len(pairs)
    if n < 3:
        return None
    sx = sum(s for s, _ in pairs)
    sy = sum(o for _, o in pairs)
    sxx = sum(s * s for s, _ in pairs)
    syy = sum(o * o for _, o in pairs)
    sxy = sum(s * o for s, o in pairs)
    den = math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy))
    return None if den <= 0 else (n * sxy - sx * sy) / den


def ztest(h1, n1, h2, n2):
    """两比例 z 检验：样本几千时 0.5 个百分点的差能不能分开，靠它而不是靠眼看。"""
    if min(n1, n2) < 5:
        return None
    p = (h1 + h2) / float(n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    return None if se <= 0 or p in (0.0, 1.0) else ((h1 / float(n1) - h2 / float(n2)) / se)


def run(years, imp_shares):
    db = SessionLocal()
    sids = ashare_sids(db)
    audit = load_audit(db)
    cells = {(t, lab): Cell() for t in years for lab in LABS}
    pooled = {lab: Cell() for lab in LABS}
    scored = dict.fromkeys(years, 0)
    # 相关系数要逐次观测的 (分, 结局)，只留这三个口径（imp3 是给主口径做敏感性对照的）
    pairs = {k: [] for k in ("aud", "imp", "loss")}

    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g = load_batch(db, chunk)
        for sid in chunk:
            ind_rows = [r for r, _ in g["ind"].get(sid, [])]
            ba_rows = [r for r, _, _, _ in g["ba"].get(sid, [])]
            cf_rows = g["cf"].get(sid, [])
            inc_rows = g["inc"].get(sid, [])
            if not ind_rows:
                continue
            # 权益用实体列（与 gate 的 neg_equity 同一列同一口径），只认 12-31 那行
            eq = {rd.year: float(parent if parent is not None else total)
                  for _, parent, total, rd in g["ba"].get(sid, [])
                  if rd is not None and rd.month == 12 and rd.day == 31
                  and (parent is not None or total is not None)}
            net = {}
            for row, col in g["ind"].get(sid, []):
                if str(row.get("报告期") or "")[5:10] != "12-31":
                    continue
                y = int(str(row["报告期"])[:4])
                v = _num(row.get("净利润"))
                if v is None and col is not None:
                    v = float(col)
                if v is not None:
                    net[y] = v

            for t in years:
                sc = score_at(ind_rows, ba_rows, cf_rows, t)
                if sc is None:
                    continue
                scored[t] += 1
                lab = bucket_of(sc)
                y = t + 1
                cell, pc = cells[(t, lab)], pooled[lab]
                cell.observations += 1
                pc.observations += 1
                aud = audit.get((sid, y))
                eqy = eq.get(y)
                loss = imp_loss(annual_row(inc_rows, "报告日", y))
                imp = imp3 = None
                if loss is not None and eqy and eqy > 0:
                    imp = loss >= imp_shares[0] * eqy
                    imp3 = loss >= imp_shares[1] * eqy
                n_t, n_y = net.get(t), net.get(y)
                flip = None if (n_t is None or n_y is None or n_t < 0) else n_y < 0
                for key, out in (("aud", aud), ("imp", imp), ("imp3", imp3), ("loss", flip)):
                    cell.add(key, out)
                    pc.add(key, out)
                for key, out in (("aud", aud), ("imp", imp), ("loss", flip)):
                    if out is not None:
                        pairs[key].append((sc, 1.0 if out else 0.0))
    db.close()
    return cells, pooled, pairs, scored


def report(cells, pooled, pairs, scored, years, imp_shares):
    ks = (("aud", "非标意见"), ("imp", f"减值≥{imp_shares[0]:.0%}净资产"),
          ("imp3", f"减值≥{imp_shares[1]:.0%}净资产"), ("loss", "次年净利转负"))
    print(f"样本：A 股；T ∈ {list(years)}，红旗分按截至 T-12-31 的财报重算；结局看 T+1 年")
    for t in years:
        print(f"\n—— T = {t}（重算出分 {scored[t]} 家）——")
        print("  档位        档内家数   " + "".join(f"{lab:>18}" for _, lab in ks))
        for lab in LABS:
            c = cells[(t, lab)]
            print(f"  {lab:<10} {c.observations:>8}   "
                  + "".join(f"{_pct(c.rate(k)):>18}" for k, _ in ks))

    print("\n==== 各年合并（同一档位把 T=2022~2024 的观测叠在一起）====")
    print("  档位        观测数     " + "".join(f"{lab:>18}" for _, lab in ks))
    for lab in LABS:
        c = pooled[lab]
        print(f"  {lab:<10} {c.observations:>8}   "
              + "".join(f"{_pct(c.rate(k)):>18}" for k, _ in ks))

    top = pooled[LABS[-1]]
    print("\n  极高档 vs 其余三档：发生率、lift、两比例 z（|z|≥1.96 才算真分得开）")
    for k, lab in ks:
        rest = [pooled[lab2] for lab2 in LABS[:-1]]
        h1, n1 = top.hit[k], top.n[k]
        h2 = sum(c.hit[k] for c in rest)
        n2 = sum(c.n[k] for c in rest)
        r1 = h1 / n1 if n1 else None
        r2 = h2 / n2 if n2 else None
        z = ztest(h1, n1, h2, n2)
        lift = (r1 / r2) if (r1 and r2) else None
        print(f"   {lab:<18} {h1:>5}/{n1:<6}={_pct(r1)}   其余 {h2:>5}/{n2:<7}={_pct(r2)}   "
              + (f"lift={lift:.2f}×  z={z:+.2f}" if (lift and z is not None) else "分不开"))
    print("\n  分与结局的相关系数（点二列，全部观测）：")
    for k in ("aud", "imp", "loss"):
        r = corr(pairs[k])
        print(f"   {dict(ks)[k]:<18} " + (f"r={r:+.4f}  n={len(pairs[k])}" if r is not None else "—"))
    # 上面的 z 把「家 × 年」当独立观测，同一家在三个 T 上会各计一次，因而偏乐观；
    # 这一条不依赖独立性假设：逐个 T 年看方向，再看合并四档是否单调
    print("\n  方向一致性（不拿独立性假设充数）：")
    for k, lab in ks:
        both = [t for t in years
                if cells[(t, LABS[-1])].rate(k) is not None and cells[(t, LABS[0])].rate(k) is not None]
        wins = sum(1 for t in both if cells[(t, LABS[-1])].rate(k) > cells[(t, LABS[0])].rate(k))
        seq = [pooled[l].rate(k) for l in LABS]
        got = [v for v in seq if v is not None]
        mono = len(got) == len(seq) and all(x <= y for x, y in zip(got, got[1:]))
        print(f"   {lab:<18} {wins}/{len(both)} 个 T 年极高档高于低档；合并四档单调上升={mono}")


def main():
    ap = argparse.ArgumentParser(description="造假分判别效度回测（只读库、零积分）")
    ap.add_argument("--years", nargs="*", type=int, default=[2022, 2023, 2024],
                    help="作为「T」的报告年份，结局看各年的下一年")
    ap.add_argument("--imp-share", nargs=2, type=float, default=[0.05, 0.03],
                    help="大额减值占当年期末归母净资产的比例：主口径 次要口径")
    a = ap.parse_args()
    cells, pooled, pairs, scored = run(a.years, a.imp_share)
    report(cells, pooled, pairs, scored, a.years, a.imp_share)


if __name__ == "__main__":
    main()
