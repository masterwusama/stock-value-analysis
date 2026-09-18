# -*- coding: utf-8 -*-
"""评分公式的边界与缺失数据探针（合成输入，不碰数据库、不碰真实公司）。

覆盖六处口径，都是「拿真实公司跑一遍全量」看不出来的那类：
1. 流动比率打分在 1.5 这一点必须与前一段衔接（曾出现比率变好、分数反而掉 5 分）；
2. 施洛斯的风险扣分必须真的落到总分上（曾被 ±可评估权重的夹逼整段吞掉，扣多少都是 0）；
3. 5 年累计净现比只按「同年净利润与经营现金流都有数」的年份配对，并如实报出配对年数
   （曾把两列各自的和相除，缺失年份不重合时比值不对应任何一段真实经营期）；
4. 覆盖度归一只补偿正分，缺项不得把负分放大（曾把格防的 −28 推成 −31.11，越过 −30 下限）；
5. 成长综合分 G 的分母固定为 100 权重：缺项只压低总分，绝不按可用项重新归一；
6. 价值综合分 V 同一条固定分母，加上它特有的两态：快照没有市值（或市值为 0）时价格那三项
   一起判不动而剩下那 35 分照算，有形账面价值算得出但为负时留在 0 分档而不是判不动。

每个断言同时跑 Python（scoring.py）与 JS（stockLegacy.js，经 Node 抽取原函数）两侧：
两边必须在同一批合成输入上给出相同总分——真实数据的逐项一致性由 _score_check.py 全量校验，
这里只保证边界形状两边一致。

合成 fixture 只服务「隔离单个输入变量」，科目之间不做会计恒等式（例如流动负债可以大于
负债合计以外的一切约束、存货挤在固定的资产总计里），别把它的数字当真实财报读。

用法（在 backend/collector 目录下）：
    python -X utf8 scripts/_score_formula_check.py
"""
import copy
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# SCORING_PATH 指一份替代的 scoring.py 时用那份跑：探针要能抓出旧口径的问题才算数
ALT = os.environ.get('SCORING_PATH')
if ALT:
    _spec = importlib.util.spec_from_file_location('scoring_alt', ALT)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    value_analysis, value_scores = _mod.value_analysis, _mod.value_scores
    growth_score, _school_total = _mod.growth_score, _mod._school_total
    value_score = _mod.value_score
    price_references = _mod.price_references
    compute_scores, trap_score = _mod.compute_scores, _mod.trap_score
else:
    from scoring import (value_analysis, value_scores, growth_score,  # noqa: E402
                         value_score, _school_total, price_references,
                         compute_scores, trap_score)

FIX_DIR = HERE.parent / "_tmp" / "formula-fixtures"
YEARS = [2020, 2021, 2022, 2023, 2024]
LAST = "2024-12-31"


def ind_row(day, net=1e9, **ov):
    r = {'报告期': day, '净利润': net, '营业总收入': 1e10, '净资产收益率': 0.2,
         '资产负债率': 0.4, '销售毛利率': 0.3, '销售净利率': 0.1,
         '基本每股收益': 1.0, '扣非净利润': net}
    r.update(ov)
    return r


def ba_row(day=LAST, ca=4e10, cl=1e10, tl=2e10, assets=4e10, eq=1e10, **ov):
    r = {'报告日': day, '流动资产合计': ca, '流动负债合计': cl, '负债合计': tl,
         '资产总计': assets, '归属于母公司股东权益合计': eq, '货币资金': 1e9,
         '商誉': 0.0, '无形资产': 0.0}
    r.update(ov)
    return r


def cf_row(day=LAST, ocf=1e9, capex=0.0):
    return {'报告日': day, '经营活动产生的现金流量净额': ocf,
            '购建固定资产、无形资产和其他长期资产所支付的现金': capex,
            '销售商品、提供劳务收到的现金': 1e10}


def company(ba=None, ind=None, cf=None, snap=None, divs=None, notes=None, income=None):
    """一家「其他项都落在中性/满分位」的公司，只留调用方要动的那几个量。"""
    return {
        'code': 'FIXTURE', 'name': '合成', 'market': 'A',
        'indicators': ind if ind is not None else [ind_row(f"{y}-12-31") for y in YEARS],
        'balance': ba if ba is not None else [ba_row()],
        'cashflow': cf if cf is not None else [cf_row()],
        'income': income if income is not None else [],
        'dividends': divs if divs is not None else [],
        # 施洛斯的价格项落在满分位（PB ≤ 0.75、PE ≤ 10），流动资产远大于市值
        'snapshot': snap if snap is not None else {
            'price': 10.0, 'market_cap': 1e9, 'pe_ttm': 8.0, 'pb': 0.5},
        'notes': notes,
    }


FIX = {}          # name -> 输入
EXP = {}          # name -> Python 侧四项总分，供 JS 对端比对
G_EXP = {}        # name -> (成长总分, 可评估项数)，同一批 fixture 的 G 侧对端
V_EXP = {}        # name -> (价值总分, 可评估项数)，同一批 fixture 的 V 侧对端


def probe(name, d):
    FIX[name] = d
    t = value_scores(d, value_analysis(d))
    EXP[name] = t
    return t


def g_probe(name, d):
    """只取成长分的探针：输入仍登记进 FIX（JS 对端跑同一批文件），产出登记进 G_EXP。"""
    FIX[name] = d
    r = growth_score(d)
    G_EXP[name] = (r['total'], r['evaluated'])
    return r


def v_probe(name, d):
    """只取价值分的探针，形状同 g_probe（产出登记进 V_EXP）。"""
    FIX[name] = d
    r = value_score(d)
    V_EXP[name] = (r['total'], r['evaluated'])
    return r


fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def close(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


# ==================== 1) 流动比率：1.5 处不得回坠 ====================
# 只动 流动负债合计：流动比率 = 流动资产 ÷ 流动负债，而营运资本里的长期有息负债为 0，
# 长期负债项在 流动比率 > 1 时恒为满分、≤ 1 时恒为 −10，格防总分于是只剩流动比率在动。
def cur_probe(ratio):
    ca = 3e10
    d = company(ba=[ba_row(ca=ca, cl=ca / ratio, tl=4e10)])
    return probe(f"cur_{ratio:.4f}", d)['grahamDef']


RS = [0.5, 0.7, 0.9, 0.999, 1.0, 1.001, 1.1, 1.25, 1.4, 1.49, 1.5, 1.6,
      1.75, 1.9, 1.999, 2.0, 2.2, 2.5, 3.0]
TS = [cur_probe(r) for r in RS]
for i in range(1, len(TS)):
    check(TS[i] >= TS[i - 1] - 1e-9,
          f"流动比率单调性：比率 {RS[i - 1]}→{RS[i]} 总分从 {TS[i - 1]} 掉到 {TS[i]}")
# 1.0~1.5 恒为 5 分、1.5~2 线性到 20：衔接点上不得出现回坠或跳空
i12, i15, i175, i20 = (RS.index(v) for v in (1.25, 1.5, 1.75, 2.0))
check(close(TS[i15], TS[i12]), f"流动比率 1.5 应仍是 5 分档：{TS[i12]} vs {TS[i15]}")
check(close(TS[i175] - TS[i15], 7.5), f"流动比率 1.5→1.75 应加 7.5 分：{TS[i175] - TS[i15]}")
check(close(TS[i20] - TS[i15], 15.0), f"流动比率 1.5→2.0 应加 15 分：{TS[i20] - TS[i15]}")
check(close(TS[i20], TS[-1]), "流动比率 ≥ 2 之后应封顶（2.2/2.5/3.0 同分）")

# ==================== 2) 施洛斯扣分：夹逼之后仍要真的扣分 ====================
# 无分红 → 股息率项 None → 可评估权重 85，其余正分项全满 → 基础分顶在 85 的夹逼上界。
# 旧口径把扣分加在夹逼之前，这里扣多少都看不见；新口径应逐分落地。
PEN_CASES = (
    ("pen_none", {}, 0.0),
    ("pen_goodwill3", {'商誉': 3.5e9}, 2.0),      # (商誉+无形)/归母权益 > 0.3 → −2
    ("pen_goodwill7", {'商誉': 7e9}, 4.0),        # > 0.6 → −4
    ("pen_inv", {'存货': 2.1e10}, 2.0),           # 存货/总资产 > 0.5 → −2
    ("pen_both", {'商誉': 7e9, '存货': 2.1e10}, 6.0),
)
base_s = None
gd_of = {}
for name, ba_ov, pen in PEN_CASES:
    t = probe(name, company(ba=[ba_row(**ba_ov)]))
    gd_of[name] = t['grahamDef']
    if base_s is None:
        base_s = t['schloss']
        continue
    check(close(base_s - t['schloss'], pen),
          f"{name}：扣分应让总分降 {pen}，实降 {base_s - t['schloss']}（旧口径此处恒为 0）")

# 同一批输入下格防/格攻不受施洛斯扣分影响（9 个扣分项只挂在施洛斯上）
for name in ('pen_goodwill7', 'pen_inv', 'pen_both'):
    check(close(gd_of['pen_none'], gd_of[name]),
          f"{name}：施洛斯的扣分项串到了格防（{gd_of['pen_none']} → {gd_of[name]}）")

# ==================== 2b) 覆盖度补偿只给正分：缺项不得放大坏消息 ====================
# 归一是为了「缺一项不等于白扣该项满分」，那是正分侧的问题。负分侧同一次除法等于让缺项
# 把坏消息按 100/可评估权重 放大：四派里只有格防有天然负分项（合计最深 −30），于是 −28 在
# 只评估到 90 分权重时变成 −31.11，越过设计下限。
check(close(_school_total([-28.0, None], (90, 10)), -28.0),
      f"负分不得按覆盖度放大：−28（缺 10 分权重）实为 {_school_total([-28.0, None], (90, 10))}")
check(_school_total([-28.0, None], (90, 10)) >= -30.0 - 1e-9,
      "格防总分必须守住 −30 设计下限")
check(close(_school_total([28.0, None], (90, 10)), 28.0 / 90 * 100),
      f"正分侧的覆盖度补偿不能跟着变：28（缺 10 分权重）实为 {_school_total([28.0, None], (90, 10))}")
# 同一套断言走一遍真实评分入口，让 JS 对端也吃到这条。两个变体只差在「两项 PE 是否可评估」，
# 且 PE 项取 0 分位（市盈率远超 15、PE×PB 远超 22.5），这样原始分相同、只有可评估权重不同，
# 分数必须一致；旧口径下缺项那侧会再低一档。
NEG_BA = ba_row(ca=8e9, cl=1e10, tl=4e10)          # 流动比率 0.8 → 流动比率与营运资本各 −10
NEG_IND = [ind_row(f"{y}-12-31", net=-1e9) for y in YEARS]   # 过半亏损 + 净利不增长
snap_pe = {'price': 10.0, 'market_cap': 1e9, 'pe_ttm': 200.0, 'pb': 0.5}
snap_nope = {'price': 10.0, 'market_cap': 1e9, 'pb': 0.5}    # PE 与 PE×PB 两项缺位
d_pe = probe('neg_pe_zero', company(ba=[NEG_BA], ind=NEG_IND, snap=snap_pe))['grahamDef']
d_no = probe('neg_pe_missing', company(ba=[NEG_BA], ind=NEG_IND, snap=snap_nope))['grahamDef']
check(close(d_pe, d_no),
      f"同为 0 分的两项 PE 缺位不该让负分更深：在位 {d_pe} vs 缺位 {d_no}")
check(d_pe >= -30.0 - 1e-9 and d_no >= -30.0 - 1e-9,
      f"评分入口的格防越界：{d_pe} / {d_no}")

# ==================== 3) 净现比：按年配对 + 如实报出配对年数 ====================
# 净利润 5 年齐全、经营现金流只有近 2 年：旧式 5 年净利除 2 年现金流，比值被稀释一半以上
cf2 = [cf_row(f"{y}-12-31", ocf=2e9) for y in (2023, 2024)]
va = value_analysis(company(cf=cf2))
check(close(va['ratioYears'], 2), f"配对年数应为 2，实为 {va['ratioYears']}")
check(close(va['ratio5'], 2.0),
      f"净现比应为同年配对 (2+2)/(1+1)=2.0，实为 {va['ratio5']}（旧口径 4/5=0.8，跨了三年错配）")

# 全齐时仍是 5 年、数值与旧口径相同（改动不影响数据完整的公司）
va5 = value_analysis(company(cf=[cf_row(f"{y}-12-31", ocf=1e9) for y in YEARS]))
check(close(va5['ratioYears'], 5) and close(va5['ratio5'], 1.0),
      f"5 年齐全时应为 5 年/1.0，实为 {va5['ratioYears']} 年/{va5['ratio5']}")

# 有现金流行但年份与年报错开 → 一年都配不上，按缺失处理而不是硬凑一个比值
va0 = value_analysis(company(cf=[cf_row('2019-12-31', ocf=1e9)]))
check(va0['ratioYears'] == 0 and va0['ratio5'] is None,
      f"零配对应计为缺失，实为 {va0['ratioYears']} 年/{va0['ratio5']}")

# 配对年数只说「几年有数」，不因合计净利为负而虚报比值
va_neg = value_analysis(company(
    ind=[ind_row(f"{y}-12-31", net=1e9) for y in (2020, 2021, 2022)]
        + [ind_row(f"{y}-12-31", net=-1e9) for y in (2023, 2024)],
    cf=[cf_row(f"{y}-12-31", ocf=1e9) for y in (2023, 2024)]))
check(va_neg['ratioYears'] == 2 and va_neg['ratio5'] is None,
      f"配对 2 年但合计净利为负：应报 2 年且比值为空，实为 {va_neg['ratioYears']}/{va_neg['ratio5']}")

# 净利润缺失的年份不得被现金流的和顶进分子
va_m = value_analysis(company(
    ind=[ind_row('2020-12-31', net=None), ind_row('2021-12-31', net=1e9),
         ind_row('2022-12-31', net=1e9), ind_row('2023-12-31', net=1e9),
         ind_row('2024-12-31', net=1e9)],
    cf=[cf_row(f"{y}-12-31", ocf=1e9) for y in YEARS]))
check(va_m['ratioYears'] == 4 and close(va_m['ratio5'], 1.0),
      f"2020 缺净利应按 4 年配对（4/4=1.0），旧口径拿 5 年现金流之和除 4 年净利得 1.25："
      f"实为 {va_m['ratioYears']} 年/{va_m['ratio5']}")

# ==================== 4) 空数据 / 极端输入不炸 ====================
# 空输入不等于四派都算不出：盈利稳定性、连续分红这类项仍能确定地给 0 分或负分（0/5 年为正
# → −5），归一后照样出分。这里要守的是「不抛异常、不出 NaN、落在各派设计区间内」，
# 上下限与 _selfcheck.py 一致。
RANGE = (('grahamAgg', 0.0, 100.0), ('grahamDef', -30.0, 100.0),
         ('schloss', -37.0, 100.0), ('buffett', 0.0, 100.0))
EXTREME = {
    'empty': company(ind=[], ba=[], cf=[], snap={}),
    'neg_equity': company(ba=[ba_row(eq=-1e9, 商誉=7e9)]),
    'zero_cl': company(ba=[ba_row(cl=0.0)]),
    'big_ratio': company(ba=[ba_row(ca=1e15, cl=1e3, tl=1e3)]),
    'zero_net_all_years': company(ind=[ind_row(f"{y}-12-31", net=0.0) for y in YEARS]),
}
for name, d in EXTREME.items():
    t = probe(name, d)
    for key, lo, hi in RANGE:
        v = t[key]
        check(v is None or (isinstance(v, (int, float)) and math.isfinite(v) and lo - 1e-9 <= v <= hi + 1e-9),
              f"{name}.{key} 越界或非法：{v}（应在 {lo}~{hi}）")
    gr = g_probe(name, d)      # 同一批极端输入喂给成长分：不炸、不出 NaN、落在 0~100 或判不动
    check(gr['total'] is None or (math.isfinite(gr['total']) and -1e-9 <= gr['total'] <= 100 + 1e-9),
          f"{name}.growth 越界或非法：{gr['total']}")
    vr = v_probe(name, d)      # 价值分同一条：极端输入只允许「算得出在区间内」或「判不动」两种出口
    check(vr['total'] is None or (math.isfinite(vr['total']) and -1e-9 <= vr['total'] <= 100 + 1e-9),
          f"{name}.value 越界或非法：{vr['total']}")

# ==================== 5) 成长分 G：分母固定 100 权重，缺项不得把分顶上去 ====================
# 与第 2b 条相反的方向：四派给正分做覆盖度补偿，G 刻意不补偿。按可用项归一会让「只披露得
# 起 ROE 的公司」和「七项齐全的公司」平起平坐，披露越差反而显得更能长——当初否掉
# weightedTotal 的正是这条，而真实数据九成公司七项齐全，全量比对看不见这个失效形状。
# （实测对照：把分母换成可评估权重跑同一批 fixture，抽掉两项披露反而 92.9 → 95.2。）
def g_ind(drop=()):
    """七项全可评估的干净历史：净利/营收/每股净资产逐年递增、ROE 高位微升（5 期，跨度 4 年）。"""
    rows = []
    for i, y in enumerate(YEARS):
        r = ind_row(f"{y}-12-31", net=1e9 * 1.2 ** i, **{
            '营业总收入': 1e10 * 1.15 ** i, '净资产收益率': 0.15 + 0.01 * i,
            '每股净资产': 3.0 * 1.18 ** i})
        for col in drop:
            r[col] = None
        rows.append(r)
    return rows


# 三份输入只在「某几列披不披露」上不同，分项数值一模一样：分母若跟着可用权重缩水，
# 三个总分就会相等，下面这条严格递减立刻红。
r7 = g_probe('g_full7', company(ind=g_ind()))
r5 = g_probe('g_no_roe', company(ind=g_ind(('净资产收益率',))))
r4 = g_probe('g_no_roe_bps', company(ind=g_ind(('净资产收益率', '每股净资产'))))
r3 = g_probe('g_two_years', company(ind=[ind_row(f"{y}-12-31") for y in YEARS[:2]]))
check(r7['evaluated'] == 7 and r5['evaluated'] == 5 and r4['evaluated'] == 4,
      f"可评估项数应随披露缺列如实掉：7 项/{r7['evaluated']}、抽掉 ROE/{r5['evaluated']}、"
      f"再抽掉每股净资产/{r4['evaluated']}")
check(r7['total'] is not None and 0 <= r7['total'] <= 100,
      f"七项齐全且都在高增长位时应落在 0~100 的高段，实为 {r7['total']}")
check(r7['total'] > r5['total'] > r4['total'],
      f"缺项必须压低总分（分母固定为 100 权重，不得按可用项归一）："
      f"7 项 {r7['total']} / 5 项 {r5['total']} / 4 项 {r4['total']}")
# 年报不足 3 期是「判不动」，不是 0 分——落成 0 会跟「查过了，一点没长」混成一格
check(r3['total'] is None and r3['evaluated'] == 0,
      f"只有 2 期年报时应整分判不动，实为 {r3['total']}/可评估 {r3['evaluated']}")

# ==================== 6) 价值综合分 V：便宜那三项全吃快照市值 ====================
# 真实数据里快照恒有市值，所以「行情源不返 market_cap」这个失效形状跑全量比对永远看不见，
# 只能摆合成输入。另一条同理：有形账面价值为负是「确实没有安全边际」（0 分档），归母权益
# 缺行才是「取不到」（判不动）——两者在库里都稀有，混起来的后果是资不抵债的公司不被扣分。
def v_ind(drop=()):
    """七项全可评估的干净历史：净利逐年增、ROE 中段、每股净资产齐。"""
    rows = []
    for i, y in enumerate(YEARS):
        r = ind_row(f"{y}-12-31", net=1e9 * 1.1 ** i, **{
            '净资产收益率': 0.10 + 0.01 * i, '每股净资产': 5.0, '资产负债率': 0.4})
        for col in drop:
            r[col] = None
        rows.append(r)
    return rows


V_DIVS = [{'year': y, 'bonus_per_10': 2.0} for y in (2021, 2022, 2023)]
V_BASE = dict(ind=v_ind(), divs=V_DIVS,
              cf=[cf_row(f"{y}-12-31", ocf=1e9) for y in YEARS])
w7 = v_probe('v_full7', company(**V_BASE))
w_nocap = v_probe('v_no_mcap', company(
    snap={'price': 10.0, 'pe_ttm': 8.0, 'pb': 0.5}, **V_BASE))
w_zerocap = v_probe('v_zero_mcap', company(
    snap={'price': 10.0, 'market_cap': 0.0, 'pe_ttm': 8.0, 'pb': 0.5}, **V_BASE))
w_negbook = v_probe('v_neg_equity', company(ba=[ba_row(eq=-1e9)], **V_BASE))
w_noeq = v_probe('v_no_equity', company(ba=[ba_row(eq=None)], **V_BASE))
d_us = company(**{k: v for k, v in V_BASE.items() if k != 'divs'})
d_us['market'] = 'US'
d_us['dividends'] = []      # 查无派现记录：分不清「没分过」与「这一侧没覆盖」
w_us = v_probe('v_us_nodiv', d_us)
w_2y = v_probe('v_two_years', company(
    ind=[ind_row(f"{y}-12-31") for y in YEARS[:2]], ba=[], cf=[]))

check(w7['evaluated'] == 7 and w7['total'] is not None and 0 <= w7['total'] <= 100,
      f"七项齐全时应落在 0~100，实为 {w7['total']}/可评估 {w7['evaluated']}")
# 缺市值 ⇒ 账面折扣、盈利收益率、股息率三项一起判不动，质量那 35 分照算
check(w_nocap['evaluated'] == 4 and w_nocap['total'] is not None
      and w_nocap['total'] < w7['total'],
      f"无市值应剩 4 项且总分更低：{w_nocap['total']}/可评估 {w_nocap['evaluated']}"
      f"（七项那份是 {w7['total']}）")
check(close(w_nocap['total'], w_zerocap['total'])
      and w_zerocap['evaluated'] == 4,
      "市值为 0 应与「没有市值」同一形状：0 不是极度便宜，是行情行坏了")
# 算得出而为负 = 0 分档；科目取不到 = 判不动。两条必须给出不同的 evaluated
check(w_negbook['raw'].get('edge_tbv') is not None and w_negbook['evaluated'] == 7,
      f"资不抵债仍应算得出来（记 0 分档）：raw={w_negbook['raw'].get('edge_tbv')}"
      f" 可评估 {w_negbook['evaluated']}")
check(w_noeq['raw'].get('edge_tbv') is None and w_noeq['evaluated'] == 5,
      f"缺归母权益应判不动（连带股息率也断，剩 5 项）："
      f"可评估 {w_noeq['evaluated']}")
check(w_negbook['total'] < w7['total'] and w_noeq['total'] < w7['total'],
      f"两种缺法都不该把分顶上去：负账面 {w_negbook['total']} / 缺权益 {w_noeq['total']}"
      f" vs 七项 {w7['total']}")
# 非 A 股的分红史不是全量可查：查不到分不清「没分过」与「这一侧没覆盖」
check(w_us['raw'].get('return_cash') is None and w_us['evaluated'] == 6,
      f"美股无分红记录时 return_cash 应判不动（不是 0 分档）："
      f"可评估 {w_us['evaluated']}")
check(w_2y['total'] is None and w_2y['evaluated'] == 0,
      f"年报不足 3 期应整分判不动，实为 {w_2y['total']}/可评估 {w_2y['evaluated']}")

# ==================== 6b) V 的行情输入：批次行情只喂 V 的市值 ====================
# companies/<代码>.json 的 snapshot 是深抓那一刻切的，A 股与本页行情中位差 2.18%、p90 7.98%
# （5,551 家两侧都有可用市值，其中 5,466 家这两个数不同），所以 V 那 65 分会滞后一整周、
# 并与详情页现算的对不上。
# _refresh_scores 现在把批次行情作为 batch_quote 注进输入，但只喂 V 的市值那一项——
# 把 snapshot 整体换掉的实测代价是 10,406 处非 V 字段（施洛斯的市值档、12 个参考价、
# 净现金/市值），所以这条边界钉在探针里，而不是靠改代码的人自觉。
SNAP_CAP_1E9 = {'price': 10.0, 'market_cap': 1e9, 'pe_ttm': 8.0, 'pb': 0.5}


def bq_case(bq, snap=None):
    """一家只有「批次行情给不给市值」这一个变量的公司。bq=None 即改前的老形状。"""
    d = company(snap=dict(SNAP_CAP_1E9 if snap is None else snap), **V_BASE)
    if bq is not None:
        d['batch_quote'] = bq
    return d


BQ_FIX = []


def bq_probe(name, d):
    BQ_FIX.append(name)
    return v_probe(name, d)


b_ref = bq_probe('bq_snapshot', bq_case(None))
b_2x = bq_probe('bq_2x', bq_case({'market_cap': 2e9}))
b_far = bq_probe('bq_100x', bq_case({'market_cap': 1e11}))
b_snap_nocap = bq_probe('bq_snapshot_nocap', bq_case(
    {'market_cap': 1e11}, snap={'price': 10.0, 'pe_ttm': 8.0, 'pb': 0.5}))
for bq_name, bad_bq in (('zero', {'market_cap': 0.0}), ('neg', {'market_cap': -5e8}),
                        ('nocol', {'price': 11.0}), ('empty', {})):
    b_bad = bq_probe(f"bq_bad_{bq_name}", bq_case(bad_bq))
    check(b_bad['total'] == b_ref['total'] and b_bad['evaluated'] == b_ref['evaluated'],
          f"批次行情给的市值不可用（{bad_bq}）时应退回深抓快照，而不是把 65 分整块判不动")

# 批次行情换市值 ⇒ 吃市值那三项按比值同步挪，不吃市值那四项分毫不动
check(close(b_2x['raw']['ep'], b_ref['raw']['ep'] / 2.0, 1e-12)
      and close(b_2x['raw']['cash_yld'], b_ref['raw']['cash_yld'] / 2.0, 1e-12)
      and close(b_2x['raw']['edge_tbv'], b_ref['raw']['edge_tbv'] - math.log(2), 1e-12),
      f"V 的市值项没跟着批次行情走：ep {b_ref['raw']['ep']} → {b_2x['raw']['ep']}")
check(all(b_2x['raw'][k] == b_ref['raw'][k] for k in ('roe_med5', 'debt_rev', 'ocfnp')),
      "批次行情不该动质量那几项")
# 挪出锚点区间才会显出分差：默认快照那份三项都在满分位上
check(b_far['total'] < b_ref['total'] and b_far['evaluated'] == b_ref['evaluated'] == 7,
      f"市值涨到 100 倍后 V 应明显走低：{b_ref['total']} → {b_far['total']}")
# 深抓快照压根没市值（行情源那次没返）是真实形状：批次行情补上就不该整块判不动
check(b_snap_nocap['evaluated'] == 7 and b_snap_nocap['total'] is not None,
      f"快照缺市值但批次行情有时应算得满 7 项，实为 {b_snap_nocap['evaluated']} 项")

_d_ref, _d_bq = bq_case(None), bq_case({'market_cap': 1e11})
_va_ref, _va_bq = value_analysis(_d_ref), value_analysis(_d_bq)
check(value_scores(_d_ref, _va_ref) == value_scores(_d_bq, _va_bq),
      "批次行情动了四派总分或价格锚（清算/净现金）")
check(price_references(_d_ref, _va_ref) == price_references(_d_bq, _va_bq),
      "批次行情动了 12 个买卖参考价")
check(growth_score(_d_ref) == growth_score(_d_bq) and trap_score(_d_ref) == trap_score(_d_bq),
      "批次行情动了成长分或陷阱分")
_kept = lambda s: {k: v for k, v in s.items() if k not in ('value', 'valueEval')}
check(_kept(compute_scores(_d_ref)) == _kept(compute_scores(_d_bq)),
      f"compute_scores 全字段里除 value/valueEval 外出现了差异："
      f"{[k for k in _kept(compute_scores(_d_ref)) if _kept(compute_scores(_d_ref))[k] != _kept(compute_scores(_d_bq))[k]]}")

E_EXP = {}
if not ALT:
    from equity import PARENT_KEYS, TOTAL_KEYS, equity_of, hk_equity_patch

    def equity_probe(name, fields, expected):
        d = company(ba=[ba_row(eq=None, **fields)], **V_BASE)
        d['snapshot'] = {'price': 10.0, 'market_cap': 2e10, 'pe_ttm': 8.0, 'pb': None}
        d['seo_actions'] = [{'issue_date': LAST, 'num': 1e7}]
        canonical = copy.deepcopy(d)
        canonical['balance'][0] = ba_row(eq=expected)
        check(equity_of(d['balance'][0]) == expected, name + ': 权益取值错误')
        scores = compute_scores(d)
        check(scores == compute_scores(canonical), name + ': 别名改变评分或参考价')
        probe(name, d)
        g_probe(name, d)
        v_probe(name, d)
        E_EXP[name] = {'equities': [expected], 'trap': scores['trap'],
                       'trapEval': scores['trapEval'],
                       'priceRefs': {k: scores['priceRefs'][k] for k in
                                     ('grahamAgg', 'grahamDef', 'schloss', 'buffett')}}

    for i, key in enumerate(PARENT_KEYS):
        for amount in (0.0, -1e9, 1e10):
            equity_probe(f'eq_parent_{i}_{amount}', {key: amount, TOTAL_KEYS[0]: 3e10}, amount)
    for i, key in enumerate(TOTAL_KEYS):
        equity_probe(f'eq_total_{i}', {key: 3e10}, 3e10)
    equity_probe('eq_null_parent', {PARENT_KEYS[1]: None, TOTAL_KEYS[0]: 3e10}, 3e10)
    equity_probe('eq_parent_priority', {PARENT_KEYS[0]: 1e10, PARENT_KEYS[1]: 2e10}, 1e10)

    original = {PARENT_KEYS[0]: 70.0, TOTAL_KEYS[0]: 85.0, '少数股东权益': 15.0,
                '总权益': 100.0, '资产总计': 180.0, '负债合计': 80.0}
    patch, reason = hk_equity_patch(original, legacy=True)
    check(patch == {PARENT_KEYS[0]: 85.0, TOTAL_KEYS[0]: 100.0}, '港股双扣修复值错误')
    check(reason == 'repaired' and original[PARENT_KEYS[0]] == 70.0, '修复探针不应原地改输入')
    repaired = {**original, **patch}
    check(hk_equity_patch(repaired, legacy=True) == ({}, 'unchanged'), '港股修复必须幂等')
    check(hk_equity_patch({**repaired, PARENT_KEYS[1]: 95.0}, legacy=True)
          == ({}, 'conflict_parent'), '规范键已平衡也必须报告原披露冲突')
    source = {k: v for k, v in original.items() if k not in (PARENT_KEYS[0], TOTAL_KEYS[0])}
    source['股东权益'] = 85.0
    check(hk_equity_patch(source)[0] == patch, '源规范化与历史修复必须同口径')
    check(hk_equity_patch({**source, **patch}) == ({}, 'unchanged'), '新源规范化必须幂等')
    for field in ('少数股东权益', '总权益', '资产总计', '负债合计'):
        missing = {k: v for k, v in original.items() if k != field}
        check(not hk_equity_patch(missing, legacy=True)[0], f'缺 {field} 不得猜测修复')
    for fields in ({'总权益': 105.0}, {'资产总计': 190.0}, {PARENT_KEYS[0]: 60.0},
                   {PARENT_KEYS[1]: 90.0}, {'非控股股东权益': 18.0},
                   {'股东权益合计': 110.0}, {'股东权益': 90.0}):
        check(not hk_equity_patch({**original, **fields}, legacy=True)[0],
              f'冲突记录不得覆盖: {fields}')
    check(not hk_equity_patch({**source, PARENT_KEYS[0]: 1.0})[0], '新源不得覆盖冲突派生值')
    check(hk_equity_patch({**original, '资产总计': 180.5}, legacy=True)[0] == patch,
          '元级舍入差在容差内应可勾稽')
    check(not hk_equity_patch({**original, '资产总计': 181.1}, legacy=True)[0],
          '超过元级容差不得修复')
    zero_mi = {**original, PARENT_KEYS[0]: 100.0, TOTAL_KEYS[0]: 100.0, '少数股东权益': 0.0}
    check(hk_equity_patch(zero_mi, legacy=True) == ({}, 'unchanged'), '少数为零不需要修复')
    for amount in (None, True, float('nan'), float('inf'), '123'):
        check(equity_of({PARENT_KEYS[0]: amount, TOTAL_KEYS[0]: 0.0}) == 0.0,
              '无效归母数值应跳过，零总权益不可丢失')

    import ast
    from datetime import datetime, timezone
    from types import SimpleNamespace

    # 抽取采集函数隔离网络与 PDF 依赖，仍执行实际映射和公司组装代码。
    tree = ast.parse((HERE / 'fetch_data.py').read_text(encoding='utf-8'))
    names = {'HK_BALANCE_MAP', 'HK_INCOME_MAP', 'HK_CASHFLOW_MAP',
             'fetch_hk_report', 'fetch_company_hk'}
    nodes = [n for n in tree.body if
             (isinstance(n, ast.FunctionDef) and n.name in names) or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]

    class ReportRows:
        empty = False

        def iterrows(self):
            for i, (key, value) in enumerate(source.items()):
                yield i, {'REPORT_DATE': LAST, 'STD_ITEM_NAME': key, 'AMOUNT': value}

    env = {'datetime': datetime, 'CN_TZ': timezone.utc, 'MAX_PERIODS': 40,
           'hk_equity_patch': hk_equity_patch, 'to_iso': lambda value: value,
           'parse_number': lambda value: value,
           'ak': SimpleNamespace(stock_financial_hk_report_em=lambda **kw: ReportRows()),
           'hk_industry': lambda code: None, 'sleep_between': lambda: None,
           'fetch_hk_indicators': lambda code: [], 'fetch_hk_snapshot': lambda code: {},
           'fetch_hk_dividends': lambda code: []}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'fetch_data.py', 'exec'), env)
    fetched = env['fetch_company_hk']('FIXTURE', '合成')['balance'][0]
    check(all(fetched.get(k) == v for k, v in source.items()), '新采集必须保留原始权益科目')
    check(all(fetched.get(k) == v for k, v in patch.items()), '新采集不应再次扣除少数股东权益')

# ==================== JS 对端：同一批输入必须给同样的总分 ====================
if ALT:
    print("== 评分公式边界探针（替代实现 %s，跳过 JS 对端）==" % Path(ALT).name)
else:
    print("== 评分公式边界探针 ==")
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    for f in FIX_DIR.glob('*.json'):
        f.unlink()
    for name, d in FIX.items():
        (FIX_DIR / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    js_out = subprocess.run(
        ['node', str(HERE / '_score_formula_check_node.js'), str(FIX_DIR)],
        capture_output=True, timeout=300,
    )
    if js_out.returncode != 0:
        fails.append('JS 对端失败：' + js_out.stderr.decode('utf-8', 'replace')[:800])
    else:
        js = json.loads(js_out.stdout)
        for name, t in EXP.items():
            for key in ('grahamAgg', 'grahamDef', 'schloss', 'buffett'):
                p, j = t[key], (js.get(name) or {}).get(key)
                check(close(p, j, 1e-9), f"{name}.{key}: Python={p} JS={j}")
        for name, (tot, ev) in G_EXP.items():
            j = js.get(name) or {}
            check(close(tot, j.get('growth'), 1e-9) and ev == j.get('growthEval'),
                  f"{name}.growth: Python={tot}/{ev} 项 JS={j.get('growth')}/{j.get('growthEval')} 项")
        for name, (tot, ev) in V_EXP.items():
            j = js.get(name) or {}
            check(close(tot, j.get('value'), 1e-9) and ev == j.get('valueEval'),
                  f"{name}.value: Python={tot}/{ev} 项 JS={j.get('value')}/{j.get('valueEval')} 项")
        for name, expected in E_EXP.items():
            actual = js.get(name) or {}
            for key in ('equities', 'trap', 'trapEval'):
                check(expected[key] == actual.get(key), f'{name}.{key}: Python/JS 不一致')
            for school, refs in expected['priceRefs'].items():
                for field, value in refs.items():
                    got = (actual.get('priceRefs', {}).get(school) or {}).get(field)
                    check(close(value, got), f'{name}.{school}.{field}: Python={value} JS={got}')

print(f"  输入 {len(FIX)} 组（流动比率扫点 {len(RS)} / 扣分 {len(PEN_CASES)} / "
      f"成长分覆盖度 {len(G_EXP) - len(EXTREME) - len(E_EXP)} / "
      f"价值分覆盖度 {len(V_EXP) - len(EXTREME) - len(E_EXP) - len(BQ_FIX)} / "
      f"V 批次行情 {len(BQ_FIX)} / 权益别名 {len(E_EXP)} / "
      f"其余为缺失与极端；另验港股规范化、冲突拒绝与幂等）")
if fails:
    print(f"  不通过 {len(fails)} 项:")
    for m in fails:
        print("   ", m)
    sys.exit(1)
print("  全部通过：流动比率单调、施洛斯扣分足额落地、净现比按年配对、成长分与价值分固定分母"
      "（含市值缺位与负账面那两态）、V 只从批次行情取市值而四派分与参考价不受它影响"
      + ("" if ALT else "，且 Python/JS 一致"))
