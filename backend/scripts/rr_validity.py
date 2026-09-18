# -*- coding: utf-8 -*-
"""综合推荐分 R 的收益侧回测：信号日买入 → 其后 1/2/3 年点对点总收益（后复权）。

这是 R 出厂检验的第二半：r_validity.py 验证过「门槛内 R 高的公司其后基本面变坏的少」
（转亏/减值单调），本脚本补「R 高的公司其后**股价**长得多的多」——数据面是新回补的
个股日线（collector/scripts/fetch_daily_prices.py → data/prices/<code>.json，hfq，
点对点涨幅与前复权数学等价、历史不因未来分红重写）。

**为什么判决书用 R_q（无价格版）而 R_full 只当污染对照**：面板上 V 的便宜那 65 分只能配
今天的市值——今天的价格里装着信号日之后的所有涨跌，拿它进分数再预测收益，等于把答案抄进
考卷。R_q = 0.6×V质量块 + 0.4×G 不含任何价格，是收益侧唯一干净的打分器。R_full 那一列
照打出来但标死「前视污染」，只用于看形状，不进判决书。

预登记判决线（一条不动）：
- 甲：R_q 五分位 → 其后 2 年超额收益中位数单调不降（相邻档容 1.5pp）且 Q5−Q1 ≥ 3pp；
      1 年腿同向；信号年逐年 Q5>Q1 要 ≥2/3。
- 乙：2 年腿 Q5−Q1 ≥ max(V质量块, G 单轴同腿) − 2pp（合成不许稀释成分）。
- 丙（对照不判）：R_full 前视污染列，只打印方向。

超额收益 = 个股收益 − 同（信号年 × 期限）队列的中位收益：2019~2025 各年市场整体涨跌
差异巨大，绝对收益的档位差混着 beta；减去队列中位把市场项拿掉，剩下的才是横截面选择力。

口径（继承 r_validity，面板观测直接复用其 --dump 缓存）：
1. 信号日 = FY-T 年报的「可用日」（公告日与更新日次日的较早者，Avail 同款），
   买入价 = 信号日当天或其后的第一个交易日收盘（hfq）；退出价同理取 1/2/3 年后
   第一个 ≥ 纪念日的交易日。窗口内停牌无交易日的观测判不动（不进分母）。
2. 面板 A 股 2021~2023（与 r_validity 同一批 16,373 条观测，读 _tmp/r_obs.json）。
3. 幸存者内偏差：样本只有当前挂牌的公司，退市股的价格文件为空、整条观测判不动——
   低分组的收益被系统性高估（坏公司死了就不在样本里），所以甲量到的是下界。

用法（只读库与本地价格文件、零积分）:
    cd backend; python -X utf8 -m scripts.rr_validity
    python -X utf8 -m scripts.rr_validity --obs ../_tmp/r_obs.json --horizons 1 2 3
"""
import argparse
import bisect
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Security  # noqa: E402
from scripts.fraud_validity import Avail, load_announce  # noqa: E402
from scripts.g_validity import _mono, quintiles_of  # noqa: E402
from scripts.trap_validity import TABLES  # noqa: E402
from scripts.v_validity import BATCH  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
PRICE_DIR = BACKEND / "collector" / "data" / "prices"
DEFAULT_OBS = BACKEND.parent / "_tmp" / "r_obs.json"
TOL_PP = 0.015          # 相邻档容 1.5pp：分位边界的一次抖动不算形状
REQ_2Y = 0.03           # 2 年腿 Q5−Q1 线：3pp 超额
HORIZONS = (1, 2, 3)


def load_prices(limit_codes=None):
    """{code: (dates, closes)}；空档/坏档不进。"""
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
            out[p.stem] = (d["dates"], d["closes"])
    return out


def px_at_or_after(dates, closes, iso):
    """≥ iso 的第一个交易日收盘；没有（停牌到窗尾/退市）返回 None。"""
    i = bisect.bisect_left(dates, iso)
    return closes[i] if i < len(dates) else None


def fwd_return(prices, sig_iso, years):
    """信号日买入 → 信号日+years 年卖出的点对点收益；两端任一无效则 None。"""
    dates, closes = prices
    p0 = px_at_or_after(dates, closes, sig_iso)
    if p0 is None:
        return None
    y, m, dd = int(sig_iso[:4]), int(sig_iso[5:7]), int(sig_iso[8:10])
    try:
        tgt = date(y + years, m, dd).isoformat()
    except ValueError:  # 2/29 纪念日
        tgt = date(y + years, m, 28).isoformat()
    p1 = px_at_or_after(dates, closes, tgt)
    if p1 is None or p1 <= 0:
        return None
    return p1 / p0 - 1.0


def attach_returns(obs, prices, code_of, ann_by_sid):
    """给观测挂上信号日与前瞻收益；返回 (挂上的, 未挂上的统计)。"""
    got, miss = [], Counter()
    for r in obs:
        code = code_of.get(r["sid"])
        pr = prices.get(code) if code else None
        if pr is None:
            miss["无价格文件"] += 1
            continue
        av = ann_by_sid.get(r["sid"])
        sig = av.of(date(r["t"], 12, 31)) if av else None
        if sig is None:
            miss["无可用日"] += 1
            continue
        sig_iso = sig.isoformat()
        if sig_iso < pr[0][0]:
            miss["信号日早于价格序列"] += 1
            continue
        rets = {}
        for h in HORIZONS:
            rets[h] = fwd_return(pr, sig_iso, h)
        if all(v is None for v in rets.values()):
            miss["三个窗口都判不动"] += 1
            continue
        r2 = dict(r)
        r2["sig"] = sig_iso
        r2["ret"] = rets
        got.append(r2)
    return got, miss


def median_map(rs, key_fn, val_fn):
    by = defaultdict(list)
    for r in rs:
        k = key_fn(r)
        if k is not None and val_fn(r) is not None:
            by[k].append(val_fn(r))
    return {k: statistics.median(v) for k, v in by.items()}


def excess(rs):
    """减去同（信号年×期限）队列中位：把 2019~2025 各年 beta 拿掉。"""
    for h in HORIZONS:
        mm = median_map(rs, lambda r: (r["t"], h), lambda r: r["ret"].get(h))
        for r in rs:
            v = r["ret"].get(h)
            r.setdefault("ex", {})[h] = (v - mm[(r["t"], h)]) if v is not None else None
    return rs


def fmt_med(rs, h, ex=True):
    vs = [r["ex" if ex else "ret"][h] for r in rs if r["ex" if ex else "ret"].get(h) is not None]
    return f"{statistics.median(vs) * 100:+.1f}%" if vs else "—"


def n_of(rs, h, ex=True):
    return sum(1 for r in rs if r["ex" if ex else "ret"].get(h) is not None)


def band_table(rs, getter, head):
    cells, _ = quintiles_of([r for r in rs if getter(r) is not None], getter)
    if not cells:
        print(f"\n  {head}\n    可评估观测不足，无法分档")
        return {}
    print(f"\n  {head}")
    print("    档      观测   " + "".join(f"{'超额' + str(h) + 'y中位':>10}" for h in HORIZONS)
          + "   " + "".join(f"{'绝对' + str(h) + 'y中位':>10}" for h in HORIZONS))
    res = {}
    for i, c in enumerate(cells):
        print("    Q%d  %6d   " % (i + 1, len(c))
              + "".join(f"{fmt_med(c, h):>10}" for h in HORIZONS) + "   "
              + "".join(f"{fmt_med(c, h, False):>10}" for h in HORIZONS))
    for h in HORIZONS:
        ns = []
        for c in cells:
            vs = [r["ex"][h] for r in c if r["ex"].get(h) is not None]
            ns.append(statistics.median(vs) if vs else None)
        m, d = _mono([-x if x is not None else None for x in ns], TOL_PP)
        res[h] = (m, d, ns)
        print(f"    超额{h}y：随档位{'不降 ✓' if m else '有下降 ✗'}；Q1−Q5 = {d * 100:+.1f}pp"
              + (f"（线 ≥{REQ_2Y * 100:.0f}pp，2y 腿适用）" if h == 2 else ""))
    return res


def year_dirs(rs, getter, h=2):
    by = defaultdict(list)
    for r in rs:
        if r["ex"].get(h) is not None and getter(r) is not None:
            by[r["t"]].append(r)
    pos = tot = 0
    for t, sub in sorted(by.items()):
        cells, _ = quintiles_of(sub, getter)
        if not cells:
            continue
        vs5 = [r["ex"][h] for r in cells[-1] if r["ex"].get(h) is not None]
        vs1 = [r["ex"][h] for r in cells[0] if r["ex"].get(h) is not None]
        if not vs5 or not vs1:
            continue
        tot += 1
        pos += 1 if statistics.median(vs5) > statistics.median(vs1) else 0
    return pos, tot


def main():
    ap = argparse.ArgumentParser(description="R 的收益侧回测（只读库 + 本地价格文件）")
    ap.add_argument("--obs", default=str(DEFAULT_OBS))
    a = ap.parse_args()

    obs = json.loads(Path(a.obs).read_text(encoding="utf-8"))
    db = SessionLocal()
    code_of = {sid: code for sid, code in db.execute(
        select(Security.sid, Security.code)).all()}
    # 只为观测涉及的 sid 建 Avail：信号日 = FY-T 年报在三张财报表里的最早可用日
    # （Avail.of 取 min，映射必须按表分开传——合并成一个 dict 会丢掉较晚那张表的键）
    sids = sorted({r["sid"] for r in obs})
    ann_by_sid = {}
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        tabs = {tag: load_announce(db, M, chunk) for tag, M in TABLES}
        for sid in chunk:
            ann_by_sid[sid] = Avail(tuple(tabs[tag].get(sid, {}) for tag, _ in TABLES))
    db.close()

    prices = load_prices(limit_codes={code_of.get(s) for s in sids} - {None})
    rs, miss = attach_returns(obs, prices, code_of, ann_by_sid)
    rs = excess(rs)
    wv = 0.6
    gate_in = [r for r in rs if (r["fraud"] is None or r["fraud"] <= 40)
               and (r["trap"] is None or r["trap"] <= 20)]
    for r in gate_in:
        r["rq"] = round(wv * (r["book"] or 0) + (1 - wv) * (r["g"] or 0), 2) \
            if r["book"] is not None and r["g"] is not None else None
        r["rf"] = round(wv * (r["cheap"] * 0.65 + r["book"] * 0.35) + (1 - wv) * (r["g"] or 0), 2) \
            if (r["cheap"] is not None and r["book"] is not None and r["g"] is not None) else None

    print("=" * 108)
    print(f"样本：观测 {len(obs)} 条（r_validity 面板）· 挂上收益 {len(rs)} 条 · "
          f"门槛内 {len(gate_in)} 条 · 价格文件 {len(prices)} 只")
    for k, v in miss.items():
        print(f"  未挂上：{k} {v}")
    print("  超额 = 个股收益 − 同（信号年×期限）队列中位（拿掉各年 beta）。")
    print("  ⚠ 幸存者内偏差：退市股无价格、整条判不动，低分组收益被高估——甲量到的是下界。")

    print("\n" + "=" * 108)
    print("① 甲 判别效度（判决书）：R_q（无价格版）五分位 → 其后超额收益")
    drop = band_table(gate_in, lambda r: r["rq"], "按 R_q 五分位（Q1＝最差 → Q5＝最好）")
    ok_a = bool(drop) and 2 in drop and drop[2][0] and (drop[2][1] or 0) >= REQ_2Y
    if 1 in drop and drop[1][1] is not None:
        ok_a = ok_a and drop[1][1] > 0
    pos, tot = year_dirs(gate_in, lambda r: r["rq"])
    ym = tot >= 2 and pos >= tot - 1
    print(f"    逐年（Q5 超额2y > Q1）：{pos}/{tot} {'✓' if ym else '✗'}（线 ≥2/3）")
    ok_a = ok_a and ym

    print("\n" + "=" * 108)
    print("② 乙 合成价值：2 年腿 Q5−Q1 vs 成分单轴（同一人群）")
    dbk = band_table(gate_in, lambda r: r["book"], "按 V 质量块单轴五分位")
    dg = band_table(gate_in, lambda r: r["g"], "按 G 单轴五分位")
    ok_b = True
    for h in HORIZONS:
        cand = [drop.get(h, (None, None))[1], dbk.get(h, (None, None))[1],
                dg.get(h, (None, None))[1]]
        cand = [x for x in cand if x is not None]
        r_q = drop.get(h, (None, None))[1]
        if h != 2 or not cand:
            continue
        need = max(cand) - 0.02
        hit = r_q is not None and r_q >= need
        fmt_pp = lambda v: "—" if v is None else f"{v * 100:+.1f}"
        ok_b = ok_b and hit
        print(f"    超额{h}y 腿：R_q {fmt_pp(r_q)}pp · book {fmt_pp(dbk.get(h, (None, None))[1])}pp"
              f" · G {fmt_pp(dg.get(h, (None, None))[1])}pp → 线 ≥ max−2pp {'✓' if hit else '✗'}")

    print("\n" + "=" * 108)
    print("③ 丙 前视污染对照（不进判决书）：R_full（便宜块配今天市值）")
    flt = [r for r in gate_in if r.get("rf") is not None]
    if flt:
        band_table(flt, lambda r: r["rf"], "按 R_full 五分位——答案抄进考卷，只看形状")
        print("    ⚠ R_full 的市值是今天的，装着信号日之后的所有涨跌：它不是预测，只是形状对照。")

    print("\n" + "=" * 108)
    print("判决书（预登记：甲 2y 超额单调+≥3pp+逐年≥2/3 · 乙 2y 腿不弱于成分 −2pp）")
    print("=" * 108)
    print(f"  甲：{'PASS' if ok_a else 'FAIL'}　乙：{'PASS' if ok_b else 'FAIL'}")
    if ok_a and ok_b:
        print("\n  ⇒ 收益侧成立：R_q 高分在真实股价上也有横截面选择力，R 的推荐语义补全了一半")
        print("     （仍非个人可得的超额——幸存者偏差与交易成本未计，读法保持「证据合计」）。")
    elif not ok_a:
        print("\n  ⇒ 甲不过：R_q 在收益侧切不开。R 保持「证据分」定位（基本面侧判别力仍成立），")
        print("     说明书里「不是收益预测」的警戒维持原样。")
    else:
        print("\n  ⇒ 乙不过：合成稀释了成分，考虑只推更优单轴或重议权重（--wv 回放）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
