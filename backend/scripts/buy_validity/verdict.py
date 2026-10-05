# -*- coding: utf-8 -*-
"""判决面:双对照聚合(市场中位/邻域)与预登记判决书的渲染。

预登记判决线(跑之前写死):
- 甲(单派成立):事件组(D=100)超额中位 1y>0 且 2y>0;逐年方向 ≥2/3(年事件数≥10)。
- 乙(线的增量):事件组 2y 超额 − 邻域组 2y 超额 ≥ −1.5pp。
- 丙(不判只印):四派横评、D=80 打折变体、绝对收益。
"""
import statistics
from collections import defaultdict

from . import HORIZONS, SCHOOLS, TOL_PP


def render(panel, grids, sec, miss, schools, discounts, n_sids):
    """渲染判决书,→ verdict{(派, 折扣): (甲, 乙, 逐年正, 逐年计) | None}。"""
    h_idx = {h: i for i, (_, h) in enumerate(HORIZONS)}

    # ---- 市场对照:同(信号月×期限)全队列中位 ----
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
    print(f"样本:面板观测 {len(panel)} 条(网格 {grids[0]} ~ {grids[-1]},A 股 {n_sids} 家)")
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
            cells, meds = [], {}
            for _, h in HORIZONS:
                vs = [ex(r, h) for r in evs]
                vs = [v for v in vs if v is not None]
                if not vs:
                    break
                meds[h] = statistics.median(vs)
                cells.append(f"{h}={meds[h] * 100:+.1f}%(n={len(vs)})")
            abs_2y = [r[5] for r in evs if r[5] is not None]
            if len(meds) < len(HORIZONS) or not abs_2y:
                # 某期限腿全缺(事件全挤在窗口尾部/2y 全 None)时不能拿 median 硬算
                print(line + " ".join(cells) + " · 有效收益样本不足,不判")
                verdict[(s, d)] = None
                continue
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
            else:
                print("    乙(邻域差,2y):邻域组全缺,不判")
            e1 = [v for v in (ex(r, '1y') for r in evs) if v is not None]
            e2 = [v for v in (ex(r, '2y') for r in evs) if v is not None]
            ok_a = bool(e1 and e2) and statistics.median(e1) > 0 and statistics.median(e2) > 0 \
                and tot >= 2 and pos * 3 >= tot * 2      # 预登记是「≥2/3」,tot=2 时 1/2 不该过
            # 邻域全缺(无同月同行业同分位的干净邻居)时乙**不判**:缺证据当 PASS 会
            # 把「无法对证」混进「增量成立」
            ok_b = None if diff2 is None else diff2 >= -TOL_PP
            verdict[(s, d)] = (ok_a, ok_b, pos, tot)
            print(f"    甲:{'PASS' if ok_a else 'FAIL'}(1y/2y 超额为正+逐年 {pos}/{tot}≥2/3)"
                  + (f" · 乙:{'PASS' if ok_b else 'FAIL'}" if d == 100 and ok_b is not None else ""))

    print("\n" + "=" * 104)
    print("判决书(预登记:甲=超额为正+逐年≥2/3 · 乙(仅 D=100)=邻域差≥−1.5pp,邻域缺不判 · 丙=横评不判)")
    for (s, d), v in verdict.items():
        if v is None:
            continue
        bs = (" · 乙 不判(邻域缺)" if (d == 100 and v[1] is None)
              else (f" · 乙 {'PASS' if v[1] else 'FAIL'}" if d == 100 else ""))
        print(f"  {s:12s} D={d}: 甲 {'PASS' if v[0] else 'FAIL'}"
              + bs + f" · 逐年 {v[2]}/{v[3]}")
    return verdict
