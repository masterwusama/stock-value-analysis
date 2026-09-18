# -*- coding: utf-8 -*-
"""综合推荐分 R 的判别效度回测：R = 0.6×V + 0.4×G，门槛（造假≤40 · 陷阱≤20）之外不发分。

拟议的 R 是把三条已各自过出厂检验的轴（V/G/T/造假）收进一个「值得看」的入口分。它必须先回答
三件事，而不是先写进 scoring.py 再找理由——

① 判别效度（甲）：门槛内人群按 R 的**无价格版** R_q 五分位，其后坏结局率要单调不升。
   与 V 的甲-2 同一条纪律：本库没有历史市值，V 便宜那 65 分在面板上只能配今天的市值（前视），
   所以验收用 R_q = 0.6×(V 的 C+D 质量块，块内归一) + 0.4×G；带价格的 R_full 只作前视对照、
   不进判决书。
② 合成价值（乙）：R_q 的两条腿（转亏 / 减值≥5%净资产）首末差不得明显弱于任一成分单轴
   （book / G 各自五分位同腿首末差）——弱于成分 ⇒ 合成只是把好轴稀释了，不该上线。
   线：R_q 首末差 ≥ max(成分) − 2pp（分位噪声的量级），两条腿都要。
③ 门槛有效性（丙）：被门槛剔掉的人群，其坏结局率要高于门槛内人群（方向性 + 两比例 z）。
   门槛若剔不掉坏公司，它剩下的作用就只是砍覆盖。

面板与口径完全继承 v_validity / g_validity / trap_validity（复用同一套时点机器，不写第五份）：
A 股 · 信号年 2021~2023（V/G 面板的交集；FY2016 前不可见）· 分项只喂信号日已披露的年报 ·
锚点年跟着可见序列走 · 结局取信号日之后公开的第一份年报（18 个月窗）。陷阱/造假在事件时点
重算（SHIPPED_BAD 出厂常量），门槛判定与生产 trap_score 同一条舍入。

预登记判决线（一条不动）：
- 甲：R_q 五分位 → 转亏率单调不升（相邻档容 2pp）且 Q1−Q5 ≥5pp；减值≥5% 同口径且 ≥2pp；
      信号年逐年 Q1 转亏率 > Q5 要 3/3。
- 乙：两条腿的首末差 ≥ max(book 单轴, G 单轴) − 2pp。
- 丙：gated-out 的转亏率 > gated-in 的转亏率（z 报出来，不做硬线——门槛的量化收益本来就
      是「少看几百家」，不是统计功效）。
甲乙丙全过 ⇒ R 进阶段 1（双侧实现 + parity）；甲不过 ⇒ R 不上线；乙不过 ⇒ 只上更优的那条
单轴或重议权重；丙不过 ⇒ 门槛重议。

已知局限（打在输出里）：
- 幸存者内偏差：样本只有当前挂牌的 A 股，低分组的恶化率被系统性低估。
- R_full（含便宜 65 分）在面板上是今天市值 × 历史年报，只回答形状，不是预测效度。
- 权重 0.6/0.4 是先验声明（V 量便宜+质量、G 量成长，价值取向给 V 六成），不做拟合——拟合
  只会把权重编成对这段历史的过拟合。

用法（只读库、零积分、不需要服务在跑）:
    cd backend; python -X utf8 -m scripts.r_validity
    python -X utf8 -m scripts.r_validity --limit 300      # 冒烟
    python -X utf8 -m scripts.r_validity --dump _tmp/r_obs.json
    python -X utf8 -m scripts.r_validity --load _tmp/r_obs.json --wv 0.5
                                                          # 变体：改 V/G 权重回放（缓存秒级）
"""
import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from app.db import SessionLocal  # noqa: E402
from scripts.fraud_validity import (Avail, ashare_sids, load_announce,  # noqa: E402
                                    load_batch, score_at)
from scripts.g_validity import _mono, _pp, g_raw, g_score, quintiles_of  # noqa: E402
from scripts.trap_validity import (TABLES, SHIPPED_BAD, outcomes_at,  # noqa: E402
                                   feats_at, load_events)
from scripts.v_validity import (BATCH, v_events, v_facts,  # noqa: E402
                                v_raw, v_score, blk_score, BOOK_KEYS, PRICE_KEYS,
                                load_mcap)
from scoring import TRAP_SUM_W, TRAP_W  # noqa: E402

PANEL_YEARS = (2021, 2022, 2023)
OUT_LABEL = {"loss": "转亏", "imp5": "减值≥5%净资产", "imp3": "减值≥3%",
             "divcut": "分红中断", "bvpsdn": "每股净资产降≥10%"}
DETER_OUTS = ("loss", "imp5")          # 判决书两条腿（与 V 的甲-2 同腿同线）
REQ = {"loss": 0.05, "imp5": 0.02}
TOL_PP = 0.02
FRAUD_GATE = 40.0                      # 与刷池线同源：管理分<30/造假分>50 不刷 Wind 分位
TRAP_GATE = 20.0                       # 出厂档位「单点」上沿之内（C ≤ 0.98）


def trap_c_at(feats):
    """出厂常量下的 C（未归一证据合计）；与 trap_score 同一判定与舍入口径。"""
    if feats is None:
        return None
    return sum(TRAP_W[k] for k, fn in SHIPPED_BAD.items()
               if feats.get(k) is not None and fn(feats.get(k)))


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
        dv_set, s_num, s_amt, cx5, _cap = load_events(db, chunk)
        dv_amt, _known = v_events(db, chunk)
        mc, mday, _md = load_mcap(db, chunk)
        for sid in chunk:
            ind, ba, cf = g["ind"].get(sid, []), g["ba"].get(sid, []), g["cf"].get(sid, [])
            if not ind:
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = v_facts(g, av, sid)
            d_y, d_amt = dv_set.get(sid, set()), dv_amt.get(sid, {})
            s_dates = [dt for dt, _ in s_num.get(sid, [])]
            for t in years:
                sig = av.of(date(t, 12, 31))
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                if len(yrs) < 3:
                    counts["公开年报不足3期"] += 1
                    continue
                # V：全量 raw（便宜块配今天市值，前视）；验收只用 C+D 质量块
                raw_v, ay = v_raw(f, yrs, mc.get(sid), d_amt, True)
                _v_tot, _ev, sc_v = v_score(raw_v)
                book = blk_score(raw_v, sc_v, BOOK_KEYS)
                cheap = blk_score(raw_v, sc_v, PRICE_KEYS)
                # G：同一信息集
                raw_g, _ayg, _bg = g_raw(f, yrs)
                g_tot, _eg, _sg = g_score(raw_g)
                # 门槛：事件时点的造假与陷阱（出厂常量）
                feats, _ayt = feats_at(f, yrs, s_num.get(sid, []), s_amt.get(sid, []),
                                       cx5.get(sid, []), d_y)
                feats = feats or {}
                feats["fraud"], _ = score_at(ind, ba, cf, av, t, "公告日")
                c = trap_c_at(feats)
                trap_tot = (math.floor(c / TRAP_SUM_W * 1000 + 0.5) / 10.0) if c is not None else None
                fraud = feats["fraud"]
                outs, y = outcomes_at(f, ay, t, sig, d_y, s_dates)
                if y is None:
                    counts["事件窗内没出年报"] += 1
                counts["观测"] += 1
                obs.append({"sid": sid, "t": t, "ay": ay, "book": book, "cheap": cheap,
                            "g": g_tot, "fraud": fraud, "trap": trap_tot, "o": outs})
    db.close()
    return obs, counts


# ---------- 统计 ----------

def _rate(rs, key):
    got = [r["o"].get(key) for r in rs if r["o"].get(key) is not None]
    return (sum(1 for x in got if x) / len(got), len(got)) if got else (None, 0)


def _two_prop_z(a, na, b, nb):
    """a/n vs b/n 的两比例 z；样本太小返回 None。"""
    if not na or not nb:
        return None
    p = (a + b) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    return (a / na - b / nb) / se if se else None


def _bands(rs, getter, head, wv=0.6):
    cells, _qs = quintiles_of(rs, getter)
    print(f"\n  {head}")
    if not cells:
        print("    可评估观测不足，无法分档")
        return {}
    keys = ("loss", "imp5", "imp3", "divcut", "bvpsdn")
    print("    档      观测   子分区间      " + "".join(f"{OUT_LABEL[k] + '率':<14}" for k in keys))
    drop = {}
    for k in DETER_OUTS:
        ns = [_rate(c, k)[0] for c in cells]
        m, d = _mono([-x if x is not None else None for x in ns], TOL_PP)
        drop[k] = (m, d, ns)
    for i, c in enumerate(cells):
        vs = [getter(r) for r in c]
        print("    Q%d  %6d  %8s  " % (i + 1, len(c), f"{min(vs):.0f}~{max(vs):.0f}")
              + "".join(f"{_pp(*_rate(c, k)):<14}" for k in keys))
    for k in DETER_OUTS:
        m, sp, _ns = drop[k]
        hit = sp is not None and sp >= REQ[k]
        print(f"    {OUT_LABEL[k]}率：随档位{'不升 ✓' if m else '有回升 ✗'}"
              + (f"；Q1−Q5 = {sp * 100:+.1f}pp（线 ≥{REQ[k] * 100:.0f}pp）"
                 f"{'✓' if hit else '✗'}" if sp is not None else "；有档位判不动 ✗"))
    return drop


def _years_mono(rs, wv, key="loss"):
    by = defaultdict(list)
    for r in rs:
        by[r["t"]].append(r)
    pos = tot = 0
    for t, sub in sorted(by.items()):
        cells, _ = quintiles_of(sub, lambda r: r["rq"])
        if not cells:
            continue
        a, _ = _rate(cells[0], key)
        b, _ = _rate(cells[-1], key)
        if a is None or b is None:
            continue
        tot += 1
        pos += 1 if a > b else 0
    return pos, tot


def report(obs, counts, wv):
    ok_ax = ok_book = ok_v = None
    n = len(obs)
    both = [r for r in obs if r["book"] is not None and r["g"] is not None]
    for r in both:
        r["rq"] = round(wv * r["book"] + (1 - wv) * r["g"], 2)
        r["rf"] = round(wv * (r["cheap"] * 0.65 + r["book"] * 0.35) + (1 - wv) * r["g"], 2) \
            if r["cheap"] is not None else None
    gate_in = [r for r in both if (r["fraud"] is None or r["fraud"] <= FRAUD_GATE)
               and (r["trap"] is None or r["trap"] <= TRAP_GATE)]
    in_ids = {id(r) for r in gate_in}
    gate_out = [r for r in both if id(r) not in in_ids]

    print("=" * 110)
    print(f"样本：A 股 {len({r['sid'] for r in obs})} 家 · 观测 {counts['观测']} 条 · "
          f"信号年 {sorted({r['t'] for r in obs})} · V/G 双可算 {len(both)} 条")
    for k in ("无指标行", "公开年报不足3期", "事件窗内没出年报"):
        if counts.get(k):
            print(f"  {k}: {counts[k]}")
    print(f"  门槛（造假≤{FRAUD_GATE:g} · 陷阱≤{TRAP_GATE:g}，事件时点重算）："
          f"过门 {len(gate_in)}（{len(gate_in) * 100 / max(1, len(both)):.1f}%）· "
          f"被剔 {len(gate_out)}")
    print(f"  ⚠ 幸存者内偏差：样本只有当前挂牌的 A 股，低分组恶化率被低估。")

    # 丙：门槛剔掉的是不是更坏的人群
    print("\n" + "=" * 110)
    print(f"③ 丙 门槛有效性：被剔 vs 过门（z 为两比例检验，方向对即过，不做硬线）")
    for k in ("loss", "imp5"):
        a, na = _rate(gate_out, k)
        b, nb = _rate(gate_in, k)
        z = _two_prop_z(a * na, na, b * nb, nb) if a is not None and b is not None else None
        ok_v = (ok_v is not False) and (a is None or b is None or a > b)
        print(f"    {OUT_LABEL[k]}率：被剔 {_pp(a, na)} vs 过门 {_pp(b, nb)}"
              + (f"　z={z:+.2f}" if z is not None else ""))

    # 甲：R_q 五分位
    print("\n" + "=" * 110)
    print(f"① 甲 判别效度（判决书）：R_q = {wv:g}×V质量块 + {1 - wv:g}×G 五分位 → 其后坏结局"
          f"　n={len(gate_in)}")
    drop = _bands(gate_in, lambda r: r["rq"], f"按 R_q 五分位（Q1＝最差 → Q5＝最好）", wv)
    ok_ax = bool(drop) and all(drop.get(k, (False, None))[0]
                               and (drop[k][1] or 0) >= REQ[k] for k in DETER_OUTS)
    pos, tot = _years_mono(gate_in, wv)
    ym = tot >= 2 and pos == tot
    print(f"    逐年（Q1 转亏率 > Q5）：{pos}/{tot} {'✓' if ym else '✗'}")
    ok_ax = ok_ax and ym

    # 乙：成分对照
    print("\n" + "=" * 110)
    print(f"② 乙 合成价值：R_q 两条腿的首末差 vs 成分单轴（同一人群 {len(gate_in)} 条各自分位）")
    db_ = _bands(gate_in, lambda r: r["book"], "按 V 质量块单轴五分位", wv)
    dg = _bands(gate_in, lambda r: r["g"], "按 G 单轴五分位", wv)
    ok_book = True
    for k in DETER_OUTS:
        cand = [drop.get(k, (None, None))[1], db_.get(k, (None, None))[1],
                dg.get(k, (None, None))[1]]
        cand = [x for x in cand if x is not None]
        if not cand:
            ok_book = False
            continue
        r_q = drop.get(k, (None, None))[1]
        need = max(cand) - TOL_PP
        hit = r_q is not None and r_q >= need
        ok_book = ok_book and hit
        print(f"    {OUT_LABEL[k]}腿：R_q {_pp(r_q, 0)} · book {_pp(db_.get(k, (None, None))[1], 0)}"
              f" · G {_pp(dg.get(k, (None, None))[1], 0)} → 线 ≥ max−2pp = {need * 100:.1f}pp"
              f" {'✓' if hit else '✗'}")

    # 前视对照（不进判决）
    flt = [r for r in gate_in if r.get("rf") is not None]
    if flt:
        _bands(flt, lambda r: r["rf"],
               "前视对照（不进判决）：R_full = 含便宜 65 分（今天市值 × 历史年报）", wv)
        print("    ⚠ R_full 的市值是今天的，装着信号日之后所有消息：只看形状，不是预测。")

    verdict(ok_ax, ok_book, ok_v)


def verdict(a, b, c):
    print("\n" + "=" * 110)
    print("判决书（预登记线：甲 单调+首末差+逐年同向 · 乙 不弱于成分 −2pp · 丙 门槛剔得更坏）")
    print("=" * 110)
    print(f"  甲 判别效度：{'PASS' if a else 'FAIL'}")
    print(f"  乙 合成价值：{'PASS' if b else 'FAIL'}")
    print(f"  丙 门槛有效：{'PASS' if c else 'FAIL'}")
    if a and b and c:
        print("\n  ⇒ 三条全过：R 进阶段 1（scoring.py + stockLegacy.js 双侧实现 → parity → 入库）。")
    elif not a:
        print("\n  ⇒ 甲不过：R 的质量+成长合成在真实结局上切不开，不上线。")
    elif not b:
        print("\n  ⇒ 乙不过：合成弱于成分，只上更优的那条单轴或重议权重（可用 --wv 回放）。")
    else:
        print("\n  ⇒ 丙不过：门槛剔的不是更坏的人群，重议门槛再回甲。")


def main():
    ap = argparse.ArgumentParser(description="综合推荐分 R 的判别效度回测（只读库、零积分）")
    ap.add_argument("--years", nargs="*", type=int, default=list(PANEL_YEARS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wv", type=float, default=0.6, help="V 权重（G 取 1−wv），变体回放用")
    ap.add_argument("--dump", default="")
    ap.add_argument("--load", default="")
    a = ap.parse_args()
    if a.load:
        obs = json.loads(Path(a.load).read_text(encoding="utf-8"))
        counts = Counter({"观测": len(obs)})
        print(f"（读缓存 {a.load}：{len(obs)} 条，未碰库）")
    else:
        obs, counts = run(a.years, a.limit)
        if a.dump:
            Path(a.dump).write_text(json.dumps(obs, ensure_ascii=False), encoding="utf-8")
            print(f"（观测已写 {a.dump}）")
    if not obs:
        print("没有可评估观测")
        return 1
    report(obs, counts, a.wv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
