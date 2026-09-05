# -*- coding: utf-8 -*-
"""价值分析功能自检：数据完整性 / index 交叉一致性 / 价格参考内部关系 / 重算同步 / TTM 抽查"""
import io
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).parent))
from config import DEFAULT_COMPANIES  # noqa: E402
from scoring import compute_scores, _ttm_net_profit, _eps_ttm_field, MIN_PRICE_REF  # noqa: E402

BASE = Path(__file__).parent.parent
DATA = BASE / 'data'
fails = []
warns = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# ---------- 1) 数据完整性 ----------
idx = json.loads(io.open(DATA / 'index.json', encoding='utf-8').read())
cfg = {c[0]: c for c in DEFAULT_COMPANIES}
# index.json 是全市场（数千家），DEFAULT_COMPANIES 只是自选清单：
# 市场归属必须以 index 条目为准，否则非自选的港股/美股会被兜底成 'A' 并按 A 股期数下限误报
mkt_by_code = {c['code']: c.get('market') for c in idx['companies']}

companies = {}
for f in sorted((DATA / 'companies').glob('*.json')):
    d = json.loads(f.read_text(encoding='utf-8'))
    code = d.get('code') or f.stem
    companies[code] = d
    if d.get('errors'):
        fails.append(f'{code} errors={d["errors"]}')
    mkt = mkt_by_code.get(code) or next((c[2] for c in DEFAULT_COMPANIES if c[0] == code), 'A')
    # 期数下限：A股三大报表及指标均≥10；港股东财源指标仅≈9期；美股无公告列表
    for k, mn in (('indicators', 6 if mkt != 'A' else 10),
                  ('income', 8 if mkt != 'A' else 10),
                  ('balance', 8 if mkt != 'A' else 10),
                  ('cashflow', 8 if mkt != 'A' else 10)):
        n = len(d.get(k) or [])
        check(n >= mn, f'{code} {k} 仅 {n} 期 (<{mn})')
    if mkt == 'A':
        check(len(d.get('reports') or []) >= 5, f'{code} reports 仅 {len(d.get("reports") or [])} 条')
    snap = d.get('snapshot') or {}
    check(float(snap.get('price') or 0) > 0, f'{code} snapshot.price 异常: {snap.get("price")}')

check(idx.get('count') == len(idx.get('companies') or []),
      f'count 不一致: idx.count={idx.get("count")} len={len(idx.get("companies") or [])}')
codes_idx = {c['code'] for c in idx['companies']}
# 全市场索引远大于自选清单，不能再要求两者相等；只校验任何规模下都该成立的不变量
missing_file = sorted(codes_idx - set(companies))
check(not missing_file, f'索引里有但缺数据文件 {len(missing_file)} 家: {missing_file[:10]}')
check(set(cfg) <= codes_idx, f'自选清单未进索引: {sorted(set(cfg) - codes_idx)}')
for c in idx['companies']:
    d = companies.get(c['code'])
    if d is None:
        continue            # 已记为失败，跳过以免 KeyError 打断后续所有检查
    check(c.get('name') == d.get('name'),
          f'{c["code"]} 名称不一致: idx={c.get("name")} file={d.get("name")}')
    cc = cfg.get(c['code'])
    if cc is not None:
        check(c.get('name') == cc[1], f'{c["code"]} 名称与自选清单不一致: idx={c.get("name")} cfg={cc[1]}')
    # 分数范围（防御型/施洛斯按设计含负分惩罚项，下限不是 0）
    s = c.get('scores') or {}
    for k, lo in (('grahamAgg', 0), ('grahamDef', -30), ('schloss', -37), ('buffett', 0)):
        v = s.get(k)
        check(v is None or lo - 1e-9 <= v <= 100 + 1e-9, f'{c["code"]} {k} 超范围: {v}')

# ---------- 2) priceRefs 内部一致性 ----------
n_fairliq = 0
n_netcash = 0
for c in idx['companies']:
    pr = (c.get('scores') or {}).get('priceRefs') or {}
    fa = pr.get('fairLiq')

    def close(a, b, tol=1e-6):
        return a is not None and b is not None and abs(a - b) <= max(tol, abs(b) * tol)

    if fa is not None:
        n_fairliq += 1
        ga = pr.get('grahamAgg') or {}
        check(close(fa, ga.get('sellCons')), f'{c["code"]} fairLiq({fa}) != grahamAgg.sellCons({ga.get("sellCons")})')
        check(close(ga.get('sellFair'), 1.5 * fa), f'{c["code"]} grahamAgg.sellFair != 1.5×fairLiq')
    # 净现金/市值：字段存在且是有限数（重负债基建可为深度负值，不设量级硬边界）
    ncr = pr.get('netCashRatio')
    check(ncr is None or (isinstance(ncr, (int, float)) and math.isfinite(ncr)),
          f'{c["code"]} netCashRatio 异常: {ncr}')
    if ncr is not None:
        n_netcash += 1
        calc = pr.get('netCashCalc')
        check(isinstance(calc, dict), f'{c["code"]} netCashRatio 有值但缺 netCashCalc 明细')
        if isinstance(calc, dict):
            m0, tl0, ca0 = calc.get('mcap'), calc.get('tl'), calc.get('cash')
            check(isinstance(m0, (int, float)) and m0 > 0, f'{c["code"]} netCashCalc.mcap 异常: {m0}')
            check(tl0 is not None and ca0 is not None, f'{c["code"]} netCashCalc 缺 货币资金/负债合计')
            for kk in ('fin', 'notes', 'otherCA'):
                vv = calc.get(kk)
                check(vv is None or (isinstance(vv, (int, float)) and math.isfinite(vv)),
                      f'{c["code"]} netCashCalc.{kk} 非法: {vv}')
            rep = calc.get('report')
            check(rep is None or re.match(r'^\d{4}-\d{2}-\d{2}$', rep), f'{c["code"]} netCashCalc.report 格式异常: {rep}')
            # 明细反算与存储比率一致（加权系数与 scoring.py 保持同步）
            wsum = sum((calc.get(kk) or 0) * w for kk, w in (('cash', 1.0), ('fin', 0.7), ('notes', 0.4), ('otherCA', 0.3)))
            recalc = (wsum - tl0) / m0
            check(abs(recalc - ncr) <= max(1e-9, abs(ncr) * 1e-9),
                  f'{c["code"]} netCashCalc 反算({recalc}) != netCashRatio({ncr})')
    # 四派三档都是保守卖价的固定倍数（scoring.py 的锚常量），买点为空只允许是
    # 「倍数×保守卖价 跌破一分钱下限」这一种原因，否则说明 ref/锚另有问题
    for tag, (bm, fm) in (('grahamAgg', (0.67, 1.5)),
                          ('grahamDef', (2.0 / 3.0, 4.0 / 3.0)),
                          ('schloss', (0.75, 1.5)),
                          ('buffett', (2.0 / 3.0, 1.3))):
        r = pr.get(tag) or {}
        buy, cons, fair = r.get('buy'), r.get('sellCons'), r.get('sellFair')
        if cons is None:
            check(buy is None, f'{c["code"]} {tag} 保守卖价为空却仍给了买点({buy})')
            continue
        check(close(fair, fm * cons),
              f'{c["code"]} {tag}.sellFair({fair}) != {fm:.4g}×sellCons({cons})')
        check(close(buy, bm * cons) if buy is not None else bm * cons < MIN_PRICE_REF,
              f'{c["code"]} {tag}.buy({buy}) 与 {bm:.4g}×sellCons({cons}) 不符一分钱下限口径')
        # 买点必须严格低于保守卖点：同价意味着「买入区」与「卖出区」在同一点相遇，无参考价值
        check(buy is None or buy < cons, f'{c["code"]} {tag}.buy({buy}) >= sellCons({cons})')
    # 一分钱下限：低于它的参考价没有任何市场能成交，留着还会把「折价率 1-现价/买价」吹成天文数字
    for tag in ('grahamAgg', 'grahamDef', 'schloss', 'buffett'):
        r = pr.get(tag) or {}
        for k in ('buy', 'sellCons', 'sellFair'):
            v = r.get(k)
            check(v is None or v >= MIN_PRICE_REF,
                  f'{c["code"]} {tag}.{k}={v} 低于一分钱下限 {MIN_PRICE_REF}，应为 None')
        # 保守/公允同生同灭：卖点筛选是「同时 ≥ 两档」，半档会把这家公司永久排除
        cons, fair = r.get('sellCons'), r.get('sellFair')
        check((cons is None) == (fair is None),
              f'{c["code"]} {tag} 保守卖价({cons})与公允卖价({fair})只有一个为空')

# ---------- 3) 重算一致性（index 与最新算法同步） ----------
mismatch = []
for c in idx['companies']:
    dfile = companies.get(c['code'])
    if dfile is None:
        continue                      # 缺数据文件已在第 1 节记为失败
    py = compute_scores(dfile)
    stored = c.get('scores') or {}

    def cmp_val(path, a, b):
        if a is None and b is None:
            return
        # 数值用相对容差；其余类型（如 netCashCalc.report 日期串）严格相等
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            ok = abs(a - b) <= max(1e-9, abs(b) * 1e-9)
        else:
            ok = a == b
        if not ok:
            mismatch.append(f'{c["code"]}.{path}: stored={b} recompute={a}')

    for k in ('grahamAgg', 'grahamDef', 'schloss', 'buffett'):
        cmp_val(k, py.get(k), stored.get(k))
    pstored = stored.get('priceRefs') or {}
    for k, sub in (py.get('priceRefs') or {}).items():
        if isinstance(sub, dict):
            for fld, v in sub.items():
                cmp_val(f'priceRefs.{k}.{fld}', v, (pstored.get(k) or {}).get(fld))
        else:
            cmp_val(f'priceRefs.{k}', sub, pstored.get(k))
fails.extend(mismatch)

# ---------- 4) 数据链抽查（华域/海螺/三角/鲁泰/中创）：报告期最新性、epsTTM、年报净利序列 ----------
print('== 数据链抽查 ==')
for code in ('600741', '600585', '601163', '000726', '601717'):
    d = companies.get(code)
    if not d:
        continue
    ind = d.get('indicators') or []
    inc = sorted(d.get('income') or [], key=lambda r: str(r.get('报告日') or ''))
    bal = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    ind_latest = max((str(r.get('报告期') or '')[:10] for r in ind if r.get('报告期')), default='-')
    inc_latest = str(bal and (inc[-1].get('报告日') or '-'))[:10] if inc else '-'
    bal_latest = str(bal[-1].get('报告日') or '-')[:10] if bal else '-'
    eps_ttm = _eps_ttm_field(ind)
    profits = [round((r.get('净利润') or 0) / 1e8, 2) for r in inc
               if '12-31' in str(r.get('报告日') or '')][-4:]
    snap = d.get('snapshot') or {}
    print(f'  {code} {d.get("name")}')
    print(f'      indicators 最新 {ind_latest} | income 最新 {inc_latest} | balance 最新 {bal_latest}')
    print(f'      近4年报净利(亿): {profits} | epsTTM: {eps_ttm} | 快照: 价 {snap.get("price")} / PE {snap.get("pe_ttm", snap.get("pe"))} / 市值(亿) {snap.get("market_cap")}')
    # 一致性：indicators 与报表同期推进（间隔不超过 2 个季度）
    try:
        di = datetime.strptime(ind_latest, '%Y-%m-%d')
        dn = datetime.strptime(inc_latest, '%Y-%m-%d')
        check(abs((di - dn).days) <= 100, f'{code} indicators({ind_latest}) 与 income({inc_latest}) 报告期严重脱节')
    except ValueError:
        fails.append(f'{code} 报告期格式异常: {ind_latest}/{inc_latest}')

print()
print(f'== 汇总 == 公司数 {len(companies)} | fairLiq 有值 {n_fairliq} 家 | 净现金/市值有值 {n_netcash} 家 | 失败 {len(fails)} 项')
# 全市场语料下失败可达数千条，逐条打印没有诊断价值：按“去掉代码、数字归一”的同类聚合
groups = {}
for x in fails:
    # 只剥掉开头的公司代码，保留其后的字段路径（重算不一致形如 "000001.priceRefs.schloss.buy: ..."）
    body = re.sub(r'^[A-Za-z0-9]+', '', x)
    groups.setdefault(re.sub(r'-?\d+(\.\d+)?', 'N', body), []).append(x)
for key in sorted(groups, key=lambda k: -len(groups[k])):
    sample = groups[key]
    print(f'  [{len(sample):5d} 项] {key[:110]}')
    for x in sample[:3]:
        print(f'             例: {x[:150]}')
sys.exit(1 if fails else 0)
