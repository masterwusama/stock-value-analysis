# Python scoring.py 与 frontend/src/lib/stockLegacy.js 原函数（Node 抽取）的逐项一致性对比
# Node 全量跑一遍约 7 分钟；只改 Python 侧时可设 VA_JS_CACHE 复用上次结果：
#   node scripts/_score_check_node.js > _tmp/js.json && VA_JS_CACHE=_tmp/js.json python -X utf8 scripts/_score_check.py
import json
import os
import subprocess
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).parent))
from scoring import (compute_scores, cycle_analysis, cycle_history,  # noqa: E402
                       value_score, V_ITEMS)

BASE = Path(__file__).parent.parent
companies_dir = BASE / 'data' / 'companies'

cache = os.environ.get('VA_JS_CACHE')
if cache:
    js_scores = json.loads(Path(cache).read_text(encoding='utf-8'))
else:
    js_out = subprocess.run(
        ['node', str(Path(__file__).parent / '_score_check_node.js')],
        capture_output=True, timeout=1800,
    )
    if js_out.returncode != 0:
        print('NODE FAIL:', js_out.stderr.decode('utf-8', 'replace')[:2000])
        sys.exit(1)
    js_scores = json.loads(js_out.stdout)

diffs = []
v_stats = {}        # 市场 -> V 的覆盖累计，跑完打印
for f in sorted(companies_dir.glob('*.json')):
    d = json.loads(f.read_text(encoding='utf-8'))
    code = d['code']
    py = compute_scores(d)
    js = js_scores.get(code, {})
    for key in ('grahamAgg', 'grahamDef', 'schloss', 'buffett'):
        p, j = py[key], js.get(key)
        if p is None and j is None:
            continue
        if p is None or j is None or abs(p - j) > 1e-6:
            diffs.append((code, key, p, j))
    # 价格参考对比（金额为量级较大的数值，用相对容差）
    py_refs, js_refs = py.get('priceRefs') or {}, js.get('priceRefs') or {}
    for key in ('grahamAgg', 'grahamDef', 'schloss', 'buffett'):
        pr, jr = py_refs.get(key) or {}, js_refs.get(key) or {}
        for fld in ('buy', 'sellCons', 'sellFair'):
            p, j = pr.get(fld), jr.get(fld)
            if p is None and j is None:
                continue
            tol = 1e-9 if p is None else max(1e-9, abs(p) * 1e-9)
            if p is None or j is None or abs(p - j) > tol:
                diffs.append((code, key + '.' + fld, p, j))
    # 公允清算价值（每股）对比
    p, j = py_refs.get('fairLiq'), js_refs.get('fairLiq')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > max(1e-9, abs(p or 0) * 1e-9):
        diffs.append((code, 'fairLiq', p, j))
    # 净现金/市值（比率）对比
    p, j = py_refs.get('netCashRatio'), js_refs.get('netCashRatio')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > max(1e-9, abs(p or 0) * 1e-9):
        diffs.append((code, 'netCashRatio', p, j))
    # 净现金/市值 代入明细（字典）逐字段对比
    pc, jc = py_refs.get('netCashCalc'), js_refs.get('netCashCalc')
    if pc is None and jc is None:
        pass
    elif (pc is None) != (jc is None):
        diffs.append((code, 'netCashCalc', bool(pc), bool(jc)))
    else:
        for k in ('cash', 'fin', 'notes', 'otherCA', 'tl', 'mcap', 'report',
                  'termDeposit', 'restricted', 'noteReport'):
            pv, jv = pc.get(k), jc.get(k)
            if pv == jv:
                continue
            if (isinstance(pv, (int, float)) and isinstance(jv, (int, float))
                    and abs(pv - jv) <= max(1e-6, abs(pv or 0) * 1e-9)):
                continue
            diffs.append((code, 'netCashCalc.' + k, pv, jv))
    # 造假风险分对比（百分制，舍入后一位小数，容差 1e-9）
    p, j = py.get('fraud'), js.get('fraud')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > 1e-9:
        diffs.append((code, 'fraud', p, j))
    # 管理层管理水平分对比（同造假分口径）
    p, j = py.get('mgmt'), js.get('mgmt')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > 1e-9:
        diffs.append((code, 'mgmt', p, j))
    # 周期模块对比：周期性判定（bool）+ 周期强度分 + 周期位置分（同口径）
    if py.get('cyclical') != js.get('cyclical'):
        diffs.append((code, 'cyclical', py.get('cyclical'), js.get('cyclical')))
    p, j = py.get('cycle'), js.get('cycle')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > 1e-9:
        diffs.append((code, 'cycle', p, j))
    p, j = cycle_analysis(d)['cyclicalScore'], js.get('cyclicalScore')
    if p is None and j is None:
        pass
    elif p is None or j is None or abs(p - j) > 1e-9:
        diffs.append((code, 'cyclicalScore', p, j))
    # 周期趋势对比：逐年回溯分数组（年份+分数）+ 趋势状态（仅周期性公司）
    if py.get('cyclical'):
        ph = cycle_history(d)
        jh = js.get('cycleHistory') or []
        if len(ph) != len(jh):
            diffs.append((code, 'cycleHistory.len', len(ph), len(jh)))
        else:
            for idx, (a, b) in enumerate(zip(ph, jh)):
                if a['year'] != b['year']:
                    diffs.append((code, 'cycleHistory[%d].year' % idx, a['year'], b['year']))
                elif a['score'] is None and b['score'] is None:
                    pass
                elif a['score'] is None or b['score'] is None or abs(a['score'] - b['score']) > 1e-9:
                    diffs.append((code, 'cycleHistory[%d].score' % idx, a['score'], b['score']))
    if py.get('cycleTrend') != js.get('cycleTrend'):
        diffs.append((code, 'cycleTrend', py.get('cycleTrend'), js.get('cycleTrend')))
    # 评分基准报告期（入库成 score_daily.report_date）：期次错一位，报告龄与「用的哪一期财报」就全错
    if py.get('reportDate') != js.get('reportDate'):
        diffs.append((code, 'reportDate', py.get('reportDate'), js.get('reportDate')))
    # 价值陷阱分：按 compute_scores 的产出键比（score_daily 落的就是这三个值），
    # 单独调 trap_score 只能对算法，对不了「接线有没有把它带进分数块」
    for fld, jv in (('trap', js.get('trap')), ('trapC', js.get('trapC')),
                    ('trapEval', js.get('trapEval'))):
        pv = py.get(fld)
        if pv is None and jv is None:
            continue
        if (pv is None or jv is None or abs(pv - jv) > 1e-9):
            diffs.append((code, fld, pv, jv))
    # 成长综合分：同样按 compute_scores 的产出键比（列表页与 score_daily 用的就是这两个值）
    for fld, jv in (('growth', js.get('growth')), ('growthEval', js.get('growthEval'))):
        pv = py.get(fld)
        if pv is None and jv is None:
            continue
        if (pv is None or jv is None or abs(pv - jv) > 1e-9):
            diffs.append((code, fld, pv, jv))
    # 价值综合分：同一个出口比，顺带累计三市场覆盖（便宜那 65 分吃快照市值，
    # 港美股的行情源哪天不返 market_cap，这一列会整块塌掉，必须在上线前看见）
    for fld, jv in (('value', js.get('value')), ('valueEval', js.get('valueEval'))):
        pv = py.get(fld)
        if pv is None and jv is None:
            continue
        if (pv is None or jv is None or abs(pv - jv) > 1e-9):
            diffs.append((code, fld, pv, jv))
    # 综合推荐分 R：分数按数值比，门槛状态按字符串比（两侧必须给出同一个无分原因）
    pv, jv = py.get('recommend'), js.get('recommend')
    if not (pv is None and jv is None) and (pv is None or jv is None or abs(pv - jv) > 1e-9):
        diffs.append((code, 'recommend', pv, jv))
    if (py.get('recommendGate') or None) != (js.get('recommendGate') or None):
        diffs.append((code, 'recommendGate', py.get('recommendGate'), js.get('recommendGate')))
    # 中报恶化 dip：数值比对（None 当相等）
    pv, jv = py.get('interimDip'), js.get('interimDip')
    if not (pv is None and jv is None) and (pv is None or jv is None or abs(pv - jv) > 1e-9):
        diffs.append((code, 'interimDip', pv, jv))
    vr = value_score(d)
    st = v_stats.setdefault(d.get('market') or '?', {'n': 0, 'scored': 0, 'mcap': 0, 'ev': 0,
                                                     'na': {}})
    st['n'] += 1
    st['scored'] += vr['total'] is not None
    st['mcap'] += bool((d.get('snapshot') or {}).get('market_cap'))
    st['ev'] += vr['evaluated']
    for k, _w, _lo, _hi in V_ITEMS:      # 分项整体没进 raw 也算判不动，不能只数存在的键
        st['na'][k] = st['na'].get(k, 0) + (vr['raw'].get(k) is None)

if diffs:
    print('不一致 %d 处:' % len(diffs))
    for code, key, p, j in diffs:
        print(f'  {code} {key}: Python={p} JS={j}')
    sys.exit(1)
else:
    print('全部一致: %d 家 × (4 项分数 + 价格参考含净现金代入明细 + 造假分 + 管理分 + 周期判定/强度/位置 '
          ' + 趋势回溯 + 评分基准报告期 + 陷阱分含证据权重与覆盖项数 + 成长分与价值分含覆盖项数) 完全相同' % len(js_scores))
    # 价值综合分三市场覆盖：便宜那 65 分只吃快照市值，市值不返的家在这里现形
    print('V 覆盖（按市场）:')
    print('  市场   家数   算得出分   快照有市值   平均可评估项   判不动最多的分项')
    for mkt in sorted(v_stats):
        st = v_stats[mkt]
        n = st['n']
        worst = sorted(st['na'].items(), key=lambda kv: -kv[1])[:2]
        tail = '、'.join('%s %d 家(%.1f%%)' % (k, c, c / n * 100) for k, c in worst)
        print('  %-6s %5d  %6.1f%%  %9.1f%%  %10.2f/%d   %s'
              % (mkt, n, st['scored'] / n * 100, st['mcap'] / n * 100, st['ev'] / n, len(V_ITEMS), tail))
    print('示例 3 家:')
    for f in sorted(companies_dir.glob('*.json'))[:3]:
        d = json.loads(f.read_text(encoding='utf-8'))
        py = compute_scores(d)
        print(' ', d['code'], d['name'], py)
