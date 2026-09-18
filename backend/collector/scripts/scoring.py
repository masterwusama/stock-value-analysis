"""评分计算 —— stock.js valueAnalysis/valueScores 的 Python 移植

用途：抓取脚本在生成 index.json 时预计算四大流派总分，前端列表页直接读取，
无需再下载全部公司 JSON。详情页完整明细仍由前端 JS 计算渲染。

⚠ 修改评分规则必须同步修改 assets/stock.js 与本文件，并以 JS 为基准，
用 scripts/_score_check.py（Node 抽取 stock.js 原函数对比）做一致性校验。
"""

from datetime import datetime, timedelta, timezone
import math

from equity import equity_of

# 10年期国债收益率参考值（与 stock.js BOND_10Y 一致，仅用于股债利差展示，不影响评分）
BOND_10Y = 0.017


def ssum(vals):
    """对应 JS sum()：忽略 null，全部为 null 时返回 None"""
    s, hit = 0.0, False
    for v in vals:
        if v is not None:
            s += v
            hit = True
    return s if hit else None


def annual_rows(indicators):
    """对应 JS annualRows：年报序列（报告期升序）"""
    rows = [r for r in (indicators or [])
            if '12-31' in str(r.get('报告期') or '')]
    return sorted(rows, key=lambda r: str(r.get('报告期') or ''))


def sheet_row_by_date(rows, date):
    """对应 JS sheetRowByDate：按报告日（YYYY-MM-DD）取行"""
    for r in (rows or []):
        if str(r.get('报告日') or '')[:10] == date:
            return r
    return None


def annual_balance_rows(rows):
    """对应 JS annualBalanceRows：三大报表年报序列（报告日 12-31，升序）"""
    rows = [r for r in (rows or [])
            if '12-31' in str(r.get('报告日') or '')]
    return sorted(rows, key=lambda r: str(r.get('报告日') or ''))


def ar_of(row):
    """对应 JS arOf：应收账款读取（港股报表科目为“应收帐款”，双科目兼容，优先 A 股口径）"""
    if not row:
        return None
    v = row.get('应收账款')
    return v if v is not None else row.get('应收帐款')


def cagr(cur, prev, years):
    # cur ≤ 0 时负基数开小数次方为复数，无实数解，返回 None（与 JS 一致）
    if cur is None or prev is None or prev <= 0 or cur <= 0 or not years:
        return None
    return (cur / prev) ** (1.0 / years) - 1.0


# 本币 → 人民币的粗略汇率（按市场取，A 股恒为 1）。
# 只服务格防的规模硬门槛：1e10/5e9/3e9 这三个数当年是按 A 股人民币定的，直接拿港元/美元
# 的资产数值去比，等于把体量相当的港美股系统性降 1~2 档（实测改动 251 只的格防分：HKD 35 /
# USD 216，多数 +11.76 一档，A 股一分不动；ACADIA 医药总资产 15.6 亿美元按原口径拿 0 分，
# 折回人民币应得 10 分）。
# 用固定年均汇率而不是实时汇率：门槛本身是三档粗刻度，差 3% 的汇率波动不影响分档；
# 真要精确可比就得接汇率源，那是另一个数据面，不该由评分引擎自己算。
FX_TO_CNY = {'A': 1.0, 'HK': 0.92, 'US': 7.15}


def to_cny(v, market):
    """本币金额折成人民币当量（市场未知/未列时按人民币处理，即不折）。"""
    if v is None:
        return None
    return v * FX_TO_CNY.get(str(market or 'A'), 1.0)


def per_share_div(dividends, year):
    """对应 JS perShareDiv：某年每股派息合计（元/股，含中期分红）"""
    total, hit = 0.0, False
    for r in (dividends or []):
        m = str(r.get('year') or '')
        y = m[:4]
        if y.isdigit() and int(y) == year and r.get('bonus_per_10') is not None:
            total += r['bonus_per_10']
            hit = True
    return total / 10.0 if hit else None


def consecutive_div_years(dividends, anchor=None):
    """对应 JS consecutiveDivYears：从最新年份倒推的连续（现金）分红年数。

    只数真派过现金的年份（bonus_per_10 > 0）：送股不是现金回报，送股年不应顶替
    派现年守住「连续」二字（实测 161 家最新归属年只有送股仍被计入）。
    anchor（取最新年报年，只在分红史全量可查的 A 股传）：最后现金派现年落在
    anchor-2 或更早，说明最近两个归属年都没派——按断档清零，历史连发长度不再算
    「连续」。原实现从最后一次派现年倒推，对最近的削减分红天然失明（实测 634 家
    A 股停派 ≥2 年仍拿着满额连续分红分，000002 停在 2022、倒推 32 年拿 15/15）。
    港美股不传 anchor：那侧分红源覆盖不全，停在多年前更多是没抓到而非没派过。
    """
    years = set()
    for r in (dividends or []):
        m = str(r.get('year') or '')
        y = m[:4]
        if y.isdigit() and (r.get('bonus_per_10') or 0) > 0:
            years.add(int(y))
    ys = sorted(years, reverse=True)
    if not ys:
        return 0
    if anchor is not None and ys[0] < anchor - 1:
        return 0
    n = 1
    for i in range(1, len(ys)):
        if ys[i] == ys[i - 1] - 1:
            n += 1
        else:
            break
    return n


def _recent_cutoff(now):
    """对应 JS recentDividends 的 cutoff（now-1 年，UTC；闰日退化为 3/1 同 JS）"""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        cutoff = now.replace(year=now.year - 1)
    except ValueError:
        cutoff = now.replace(year=now.year - 1, day=28) + timedelta(days=1)
    return cutoff.strftime('%Y-%m-%d')


def recent_dividends(d, now=None):
    """对应 JS recentDividends：近一年分红记录（数据按日期倒序）"""
    cutoff_str = _recent_cutoff(now)
    return [r for r in (d.get('dividends') or [])
            if (str(r.get('pay_date') or r.get('announce_date') or '')[:10]) >= cutoff_str]


def lerp_score(v, a, b, ma, mb):
    """对应 JS lerpScore：v ≤ a 取 ma；v ≥ b 取 mb；中间线性"""
    if v is None or v != v:
        # v != v 即 NaN。JS 侧 isNaN 会返回 null；Python 若放行，NaN 两个比较都为假，
        # 一路走到调用方的 min(100.0, nan) → 100.0，等于把垃圾数据顶到榜首
        return None
    if v <= a:
        return ma
    if v >= b:
        return mb
    return ma + (v - a) / (b - a) * (mb - ma)


def lerp_score_nonneg(v, a, b, ma, mb):
    """对应 JS lerpScoreNonneg：定义域为正数的“越小越好”项（PE/PB 等，ma > mb）的符号护栏。
    v ≤ 0 是亏损/资不抵债的“不达标”，不是数据缺失，直接给最低分 mb；
    否则 lerp_score 的 v ≤ a 分支会把负值夹到满分端（pb=-0.5 比 pb=0.7 得分还高）"""
    if v is None:
        return None
    return mb if v <= 0 else lerp_score(v, a, b, ma, mb)


def _yoy(cur, prev):
    """同比增长率：基期为负时用 |基期| 作分母，使亏损扩大→负、亏损收窄/扭亏→正；
    基期为正时与 cur/prev-1 完全等价，基期为 0 或缺失时无定义返回 None"""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


# 周期位置分 8 维权重：cycle_analysis 阶段二与 cycle_history 必须同尺度。
# 合计 105 而非 100 —— 这正是旧代码要靠 min(100, sum) 硬夹的原因，归一化后不再需要
CYCLE_POS_W = (25, 15, 15, 10, 10, 10, 10, 10)


def _weighted_total(scored):
    """scored = [(得分 or None, 该项满分权重)] → 按“可用项的有效满分”归一到 0~100。
    不能直接 sum(可用项)：造假分与周期位置分都是越低越好，缺一项就少扣一项，
    数据不全的公司反而显得更干净、更接近周期底部（实测 1620 家亏损公司缺造假分里
    25 分的净现比项）。归一化让缺项变成中性，而不是占便宜。"""
    avail = [(s, w) for s, w in scored if s is not None]
    wsum = sum(w for _, w in avail)
    if wsum <= 0:
        return None
    return min(100.0, sum(s for s, _ in avail) / wsum * 100.0)


def _school_total(items, weights, penalties=()):
    """四派总分：正分项按可用满分归一，再夹到 ±「可评估权重」，扣分项原样相加。

    为什么不能直接 ssum(items)（None 当 0 分）：缺一项等于白扣该项满分。施洛斯股息率项缺
    44.1%、巴菲特净现比与净利 CAGR 两项各缺 23.8%/32.4%，港股美股字段本就稀疏，实测平均
    被压 3~6 分而 A 股只 1~2 分，跨市场不可比。

    但也不能纯归一：银行/券商/保险缺的恰好是按业务模型本就不适用的偿债项（格防的流动比率
    与营运资本共 40 分权重、施洛斯的流动比率与市值/流动资产共 30 分权重），剩余项全满就是
    100 分，实测把格防前 15 里的 12 席、施洛斯前 15 里的 10 席换成金融股，真达标的公司被
    挤出去。夹到 ±可评估权重后：缺项仍中性，但最多只能拿到「桌上真正摆着的分」——覆盖度
    满 100 时与旧口径完全相同，四派前 15 名换手实测均为 0，港股美股仍拿回大部分补偿
    （格攻 A +0.23 / HK +0.71 / US +1.53，巴菲特 A +1.86 / HK +2.88 / US +5.51）。
    扣分项不参与归一：缺数据本就给 0，已中性；若按 |最低分| 也归一，施洛斯分母会变成 137，
    一家满分公司只能拿 73 分。penalties 里的项由构造保证非 None，故直接求和。

    补偿只给正分一侧。负分再除以更小的分母，等于让缺项替公司把坏消息放大——四派里只有格防
    有天然负分项（流动比率<1、营运资本为负、过半亏损、净利负增长，合计最深 −30），实测 261 家
    因此最多多扣 7.5 分，603356 拿到 −31.11 越过 −30 的设计下限。缺项在负分侧按满分折算才是
    中性的，也正合「缺项不加分也不减分」的本意。

    扣分要在夹逼之后加：先加后夹等于「基础分顶到 +wsum 的公司扣不动」——施洛斯的破净股
    风险扣分（最高 −9）实测对 55 家满分公司完全不生效，红旗分 60 与 0 的同分。放在夹逼
    之后，总分下限由扣分自己决定（−wsum − |扣分|），仍是有界值而非失控负数。
    """
    avail = [(s, w) for s, w in zip(items, weights) if s is not None]
    wsum = sum(w for _, w in avail)
    if wsum <= 0:
        return None
    raw = sum(s for s, _ in avail)
    total = raw / (wsum if raw > 0 else sum(weights)) * 100.0
    return max(-wsum, min(wsum, total)) + sum(penalties)


def value_analysis(d, now=None):
    """对应 JS valueAnalysis —— 仅计算评分依赖字段（股息/净现比/CAGR/杜邦 ROE）"""
    ind = d.get('indicators') or []
    annual = annual_rows(ind)
    cf_list = sorted(d.get('cashflow') or [], key=lambda r: str(r.get('报告日') or ''))
    divs = d.get('dividends') or []
    s = d.get('snapshot') or {}
    price = s.get('price')
    last = annual[-1] if annual else None
    last_date = str(last.get('报告期') or '')[:10] if last else None
    last_year = int(last_date[:4]) if last_date else None

    # ---- 股东回报 ----
    per_share_12m = None
    hit12 = False
    for r in recent_dividends(d, now):
        if r.get('bonus_per_10') is not None:
            per_share_12m = (per_share_12m or 0.0) + r['bonus_per_10']
            hit12 = True
    per_share_12m = per_share_12m / 10.0 if hit12 else None
    div_yield = per_share_12m / price if (price and per_share_12m is not None) else None
    per_share_y = per_share_div(divs, last_year) if last_year is not None else None
    eps_y = last.get('基本每股收益') if last else None
    payout = per_share_y / eps_y if (per_share_y is not None and eps_y is not None and eps_y > 0) else None
    # 断档锚定只在 A 股传（分红史全量可查，停派是公开事实）；港美覆盖不全，见函数头
    div_anchor = last_year if (d.get('market') or 'A') == 'A' else None
    div_consecutive = consecutive_div_years(divs, div_anchor)

    # ---- 现金流质量（近 5 年年报）----
    cf_rows = []
    for i in range(max(0, len(annual) - 5), len(annual)):
        date = str(annual[i].get('报告期') or '')[:10]
        c = sheet_row_by_date(cf_list, date)
        net = annual[i].get('净利润')
        revenue = annual[i].get('营业总收入')
        ocf = c.get('经营活动产生的现金流量净额') if c else None
        capex = c.get('购建固定资产、无形资产和其他长期资产所支付的现金') if c else None
        if capex is not None and (capex < 0 or (revenue is not None and capex > revenue * 1.5)):
            capex = None
        cf_rows.append({
            'net': net, 'ocf': ocf,
            'ratio': ocf / net if (net is not None and ocf is not None and net > 0) else None,
            'fcf': ocf - capex if (ocf is not None and capex is not None) else None,
        })
    # 只累加「同年 net 与 ocf 都有数」的配对（与下方造假分的同口径配对一致）：两列各自求和
    # 时缺失年份不重合，分子分母来自不同年份集合，比值不对应任何一段真实经营期
    paired = [(r['net'], r['ocf']) for r in cf_rows if r['net'] is not None and r['ocf'] is not None]
    ratio_years = len(paired)
    sum_net = sum(n for n, _ in paired) if paired else None
    sum_ocf = sum(o for _, o in paired) if paired else None
    ratio5 = sum_ocf / sum_net if (sum_net is not None and sum_net > 0) else None

    # ---- 杜邦分析（近 5 年年报，仅取评分用 ROE 披露值）----
    dupont_roe = []
    for j in range(max(0, len(annual) - 5), len(annual)):
        dupont_roe.append(annual[j].get('净资产收益率'))

    # ---- 成长性 ----
    # 基期必须真是 5 年前：早先直接取 annual[0]，窗口等于数据有多深就多深（实测 6911 家里
    # 97.2% 跨度≠5 年，7 年 4051 家、19 年 176 家），却按「5 年 CAGR」喂给 _fair_pe 与
    # 巴菲特净利成长项（阈值 10% 是按 5 年标定的）。取不晚于 last_year-5 的最近年报作基期，
    # 历史不足 5 年时退回最早年报（与 management_analysis 的营收 CAGR 同口径）。
    net_cagr5 = None
    if len(annual) >= 2 and last_year is not None:
        base = None
        for r in reversed(annual[:-1]):
            if int(str(r.get('报告期') or '')[:4]) <= last_year - 5:
                base = r
                break
        if base is None:
            base = annual[0]
        span = last_year - int(str(base.get('报告期') or '')[:4])
        if span > 0:
            net_cagr5 = cagr(last.get('净利润'), base.get('净利润'), span)

    return {
        'divYield': div_yield, 'payout': payout, 'divConsecutive': div_consecutive,
        'ratio5': ratio5, 'ratioYears': ratio_years, 'netCagr5': net_cagr5, 'dupontRoe': dupont_roe,
        # 评分基准报告期：四派分与造假/管理分都建在 annual 这条序列上（annual_rows 只取 12-31），
        # 落库时得说清“这批分用的是哪一期财报”，不能拿跑数日兜底（见 compute_scores.reportDate）
        'annualDate': last_date,
    }


def value_scores(d, va):
    """对应 JS valueScores —— 返回四大流派总分（满分 100）。

    估值量一律取快照 market_cap/pe_ttm/pb（当期评分口径）。曾有 k（市值缩放）与
    use_fundamental（财报驱动每股量反推）两个参数，只服务已删除的买入价二分反推。
    """
    annual = annual_rows(d.get('indicators') or [])
    last = annual[-1] if annual else None
    last_date = str(last.get('报告期') or '')[:10] if last else None
    last_year = int(last_date[:4]) if last_date else None
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    last_ba = sheet_row_by_date(ba_list, last_date) if last_date else None
    s = d.get('snapshot') or {}
    mcap, pe, pb = s.get('market_cap'), s.get('pe_ttm'), s.get('pb')
    # 快照缺 PB（腾讯行情不返回美股 PB）时用财报每股净资产补算：股价÷每股净资产，缺则归母权益/股本反推
    if pb is None:
        bps_fb = _latest_field(d.get('indicators') or [], '每股净资产')
        if bps_fb is None and last_ba is not None:
            eq_fb = equity_of(last_ba)
            sh_fb = _share_count(d.get('balance') or [],
                                 (mcap / s.get('price')) if (mcap is not None and s.get('price')) else None, None)
            if eq_fb is not None and sh_fb:
                bps_fb = eq_fb / sh_fb
        if bps_fb is not None and bps_fb > 0 and s.get('price') is not None and s.get('price') > 0:
            pb = s.get('price') / bps_fb
    div_consecutive = va['divConsecutive'] or 0
    div_yield = va['divYield']

    # ---- 基础量（最新年报）----
    def g(ba, key):
        return ba.get(key) if ba else None

    ca = g(last_ba, '流动资产合计')
    cl = g(last_ba, '流动负债合计')
    tl = g(last_ba, '负债合计')
    assets = g(last_ba, '资产总计')
    cash = g(last_ba, '货币资金')
    st_debt = g(last_ba, '短期借款')
    lt_debt = g(last_ba, '长期借款')
    bond = g(last_ba, '应付债券')
    due1y = g(last_ba, '一年内到期的非流动负债')
    lease = g(last_ba, '租赁负债')
    intang = g(last_ba, '无形资产')
    goodwill = g(last_ba, '商誉')
    net_profit = last.get('净利润') if last else None
    debtr = last.get('资产负债率') if last else None
    g_margin = last.get('销售毛利率') if last else None
    n_margin = last.get('销售净利率') if last else None

    int_debt = ssum([st_debt, due1y, lt_debt, bond, lease])
    if int_debt is None:
        int_debt = 0.0
    # 格攻净现金（2026-09 口径升级）：分子改加权类现金——交易性金融资产×0.7、应收票据×0.4、
    # 其他流动资产非存款×0.3、附注闭合才采信的定期存款×1.0、受限货币资金剔除——分母仍是
    # 有息负债，即格雷厄姆 net-cash 原文的「现金＋有价证券−有息债」。旧口径分子只认货币资金
    # 一行，把存款/理财重的公司判成负净现金（603599 广信股份：账上近 80 亿类现金、窄口径
    # −1.5 亿）。无交易性/票据/其他流动且无附注时与旧口径逐位相同；报表行仍取评分基准年报。
    _nf = _note_latest(d.get('notes'), last_date)
    w_cash_a = _weighted_cash(last_ba, _nf[1] if _nf else None)
    net_cash = (w_cash_a - int_debt) if w_cash_a is not None else None
    ncav = ca - tl if (ca is not None and tl is not None) else None
    wc = ca - cl if (ca is not None and cl is not None) else None
    ltd = ssum([due1y, lt_debt, bond, lease])
    if ltd is None:
        ltd = 0.0
    cur_ratio = ca / cl if (ca is not None and cl is not None and cl > 0) else None
    liq_ratio = ca / tl if (ca is not None and tl is not None and tl > 0) else None
    pncav = mcap / ncav if (mcap is not None and ncav is not None and ncav > 0) else None
    pnetcash = mcap / net_cash if (mcap is not None and net_cash is not None and net_cash > 0) else None
    pepb = pe * pb if (pe is not None and pb is not None) else None
    intang_share = intang / assets if (intang is not None and assets is not None and assets > 0) else None

    # 近5年年报净利润（盈利稳定性）与近5年净利累计增长
    # pos_n 只数有数的行；有效行不足 5 时整项判不动（None → _school_total 中性）：
    # 次新公司窗口不满、或某年净利字段缺失，都不是「某年亏损」，按非正年压分违反
    # 全库缺项中性纪律（实测 199 家被压到 9/15 或 4/15）
    net5 = [r.get('净利润') for r in annual[-5:]]
    valid5 = [v for v in net5 if v is not None]
    pos_n = len([v for v in valid5 if v > 0])
    # 基期取「不晚于 last_year-5 的最近年报」（与 value_analysis.netCagr5 同一选基纪律），
    # 累计增长对齐 0.33 阈值的 5 年标定。旧实现用 annual[-5:] 首尾比——5 行只有 4 个
    # 间隔，实为 4 年增长却按 5 年阈值打分，历史 ≥5 年的公司全部命中错窗
    grow5 = None
    if len(annual) >= 2 and last is not None and last_year is not None and net_profit is not None:
        base = None
        for r in reversed(annual[:-1]):
            if int(str(r.get('报告期') or '')[:4]) <= last_year - 5:
                base = r
                break
        if base is None:
            base = annual[0]
        bv = base.get('净利润')
        if bv is not None and bv > 0:
            grow5 = net_profit / bv - 1.0

    # ---- ROE 近 5 年年报序列：水平取中位数，持续性取「达标（≥10%）年数占比」----
    # 不用均值：披露口径的 ROE 在薄权益/负权益处会炸到 ±几千个百分点（实测 155 家有单年 >100%，
    # 美股 90 家最集中），均值与中位数在这份数据上的相关系数只有 0.2486，6912 家里 402 家
    # （5.8%）两种算法会跨过 15% 这条线，且错的方向随机：Home Depot 均值 −4357%（某年权益为负）
    # 而中位 +385%，被误判 0 分；O'Reilly 均值 984% 而中位 −168%，白拿满分。中位数只取中间那一年，
    # 炸群的一年进不了统计量。早先这两个 ROE 项（护城河 4 + 盈利质量 25）同用这一个均值，
    # 实测相关 0.9801，等于 29/100 押在同一个数上——现拆成「水平」与「持续」两个量。
    roe_vals = [v for v in va['dupontRoe'] if v is not None]
    n_roe = len(roe_vals)
    roe_med5 = None
    roe_ok_frac = None
    if n_roe:
        sv = sorted(roe_vals)
        mid = n_roe // 2
        roe_med5 = sv[mid] if n_roe % 2 else (sv[mid - 1] + sv[mid]) / 2.0
    # 不足 3 年无从谈「持续」，按缺失处理（_school_total 会把它当中性，不虚给也不白扣）
    if n_roe >= 3:
        roe_ok_frac = len([v for v in roe_vals if v >= 0.10]) / float(n_roe)

    # ---- 格雷厄姆 · 进取型烟蒂 ----
    g_a_items = (
        # 价格/净流动资产（市值/NCAV）≤ 0.67×，30 分
        None if ncav is None else (lerp_score(pncav, G_A_PNCAV_FULL, 1.5, 30, 0) if ncav > 0 else 0.0),
        # 价格/净现金 ≤ 1×，20 分
        None if net_cash is None else (lerp_score(pnetcash, 1, 2, 20, 0) if net_cash > 0 else 0.0),
        # 流动资产/总负债 ≥ 2，20 分
        lerp_score(liq_ratio, 1, 2, 0, 20),
        # 最新年报净利润 > 0，15 分
        15.0 if (net_profit is not None and net_profit > 0) else 0.0,
        # 资产负债率 ≤ 60%，10 分
        lerp_score(debtr, 0.6, 0.8, 10, 0),
        # 连续分红 ≥ 3 年，5 分
        (5.0 if div_consecutive >= 3 else (2.5 if div_consecutive >= 1 else 0.0)),
    )
    g_a_total = _school_total(g_a_items, (30, 20, 20, 15, 10, 5))

    # ---- 格雷厄姆 · 防御型烟蒂（规模硬门槛 + 负分惩罚）----
    # 门槛按人民币当量计：assets 是本币原值，港美股不折就会降档，见 FX_TO_CNY
    def size_score(v):
        if v is None:
            return None
        if v >= 1e10:
            return 10.0
        if v >= 5e9:
            return 6.0
        if v >= 3e9:
            return 3.0
        return 0.0

    def div_score10(years):
        if years >= 10:
            return 15.0
        if years >= 7:
            return 10.0
        if years >= 5:
            return 5.0
        if years >= 3:
            return 2.0
        return 0.0

    if wc is None:
        ltd_score = None
    elif wc <= 0:
        ltd_score = -10.0
    elif ltd <= wc:
        ltd_score = 20.0
    elif ltd <= wc * 1.5:
        ltd_score = lerp_score(ltd / wc, 1, 1.5, 20, 5)
    else:
        ltd_score = 0.0

    # 1.5~2 区间从 5 分起坡：写成 0 分会在 1.5 这一点与前一段的固定 5 分打架，
    # 流动比率从 1.49 改善到 1.50 反而掉 5 分
    cur_score = None if cur_ratio is None else (
        20.0 if cur_ratio >= 2 else
        (lerp_score(cur_ratio, 1.5, 2, 5, 20) if cur_ratio >= 1.5 else
         (5.0 if cur_ratio >= 1 else -10.0)))

    # 有效行不足 5 → 整项判不动：不满窗不是「某年亏了」（缺项中性，见 net5 处注释）
    pos_score = None if len(valid5) < 5 else (
        15.0 if pos_n >= 5 else (9.0 if pos_n == 4 else (4.0 if pos_n == 3 else -5.0)))

    grow_score = None if grow5 is None else (
        10.0 if grow5 >= 0.33 else (lerp_score(grow5, 0, 0.33, 0, 10) if grow5 >= 0 else -5.0))

    pepb_score = None
    if pepb is not None:
        # 单负/双负都属不达标：双负相乘得正会骗过 ≤22.5 阈值，故先按 pe/pb 符号判定
        pepb_score = 0.0 if (pe <= 0 or pb <= 0) else (
            5.0 if pepb <= 22.5 else (lerp_score(pepb, 22.5, 45, 5, 0) if pepb <= 45 else 0.0))

    g_d_items = (
        size_score(to_cny(assets, d.get('market'))), cur_score, ltd_score, pos_score,
        div_score10(div_consecutive), grow_score, lerp_score_nonneg(pe, G_D_PE_FULL, 25, 5, 0), pepb_score,
    )
    # 其中 cur_score/ltd_score/pos_score/grow_score 可为负（最低各 −10/−10/−5/−5），
    # 归一分母仍是满分合计 100，故 −30 的下限不变
    g_d_total = _school_total(g_d_items, (10, 20, 20, 15, 15, 10, 5, 5))

    # ---- 施洛斯烟蒂 ----
    s_items = (
        lerp_score_nonneg(pb, S_PB_FULL, 1.5, 25, 0),    # 市净率 ≤ 0.75（亦是买点倍数）
        lerp_score_nonneg(pe, 10, 20, 20, 0),       # 市盈率 ≤ 10
        lerp_score(liq_ratio, 1, 2, 0, 20),        # 流动资产/总负债 ≥ 2
        lerp_score(div_yield, 0, 0.03, 0, 15),  # 股息率 ≥ 3%
        10.0 if (net_profit is not None and net_profit > 0) else 0.0,  # 最新年报净利 > 0
        # 市值 ≤ 流动资产
        ((10.0 if mcap <= ca else lerp_score(mcap / ca, 1, 2, 10, 0))
         if (mcap is not None and ca is not None and ca > 0) else None),
    )
    # ---- 施洛斯风险扣分（与 JS valueScores 中 riskItems 一一对应）----
    ba_annual = annual_balance_rows(d.get('balance') or [])
    in_annual = annual_balance_rows(d.get('income') or [])
    cf_annual = annual_balance_rows(d.get('cashflow') or [])
    last_eq = equity_of(last_ba)
    earliest_eq = equity_of(ba_annual[0]) if len(ba_annual) >= 5 else None
    int_debt_now = _int_debt(last_ba)
    int_debt_earliest = _int_debt(ba_annual[0]) if len(ba_annual) >= 5 else None
    # 近5年扣非亏损年数（annual 最后 5 行）
    adj_net = [r.get('扣非净利润') for r in annual[-5:]]
    adj_loss_n = len([v for v in adj_net if v is not None and v < 0])
    adj_valid = len([v for v in adj_net if v is not None])
    # 应收账款/营收 3 年年报均值（位置对齐，缺失年忽略；港股“应收帐款”科目兼容）
    ar3 = [ar_of(r) for r in ba_annual[-3:]]
    rev3 = [r.get('营业总收入') for r in in_annual[-3:]]
    ar_rev3 = None
    if len(ar3) == 3 and len(rev3) == 3:
        s_ar, s_rev = ssum(ar3), ssum(rev3)
        if s_ar is not None and s_rev is not None and s_rev > 0:
            ar_rev3 = s_ar / s_rev
    # 近3年累计经营现金流 vs 累计利息费用
    ocf3 = [r.get('经营活动产生的现金流量净额') for r in cf_annual[-3:]]
    int_exp3 = [r.get('利息费用') for r in in_annual[-3:]]
    ocf3_sum, int_exp3_sum = ssum(ocf3), ssum(int_exp3)
    ocf_covers = (ocf3_sum >= int_exp3_sum) if (ocf3_sum is not None and int_exp3_sum is not None) else None
    # 近5年累计经营现金流（区分扩张举债 vs 补亏举债）
    ocf5_sum = ssum([r.get('经营活动产生的现金流量净额') for r in cf_annual[-5:]])
    # 5 年趋势（最新 vs 最早年报，要求 ≥5 个年报）
    span_ok = len(annual) >= 5
    rev_now = last.get('营业总收入') if last else None
    rev_earliest = annual[0].get('营业总收入') if span_ok else None
    g_margin_now = last.get('销售毛利率') if last else None
    g_margin_earliest = annual[0].get('销售毛利率') if span_ok else None
    eq_grow = (last_eq / earliest_eq - 1.0) if (last_eq is not None and earliest_eq is not None and earliest_eq > 0) else None
    int_debt_grow = (int_debt_now / int_debt_earliest - 1.0) if (int_debt_now is not None and int_debt_earliest is not None and int_debt_earliest > 0) else None
    rev_grow = (rev_now / rev_earliest - 1.0) if (rev_now is not None and rev_earliest is not None and rev_earliest > 0) else None
    g_margin_delta = (g_margin_now - g_margin_earliest) if (g_margin_now is not None and g_margin_earliest is not None) else None
    gw_int_sum = (goodwill or 0.0) + (intang or 0.0)
    # 负权益有两种，不能一律豁免：回购把权益打成负数而公司仍在赚钱（达美乐/HCA 这类）
    # 属正常资本结构；亏损导致的资不抵债、账上却还压着商誉无形，才是本项最该扣的情形
    # ——比值在负权益下算不出来，但实质是"无形压在已被抹平的权益基数上"，按最重档处理
    eq_distress = (last_eq is not None and last_eq <= 0 and gw_int_sum > 0
                   and not (net_profit is not None and net_profit > 0))
    inv = last_ba.get('存货') if last_ba else None
    # 9 个量化扣分项（与 JS riskItems 阈值/分值完全一致），数据不足给 0 不误伤
    risk_items = (
        # 净资产5年变动（归母权益）
        0.0 if eq_grow is None else (-5.0 if eq_grow <= -0.4 else (-3.0 if eq_grow <= -0.2 else 0.0)),
        # 近5年扣非亏损年数
        0.0 if adj_valid < 3 else (-5.0 if adj_loss_n >= 3 else (-3.0 if adj_loss_n == 2 else 0.0)),
        # (商誉+无形资产)/归母权益：负权益下比值无意义，改由 eq_distress 区分对待
        (-4.0 if eq_distress else 0.0) if (last_eq is None or last_eq <= 0)
        else (-4.0 if gw_int_sum / last_eq > 0.6 else (-2.0 if gw_int_sum / last_eq > 0.3 else 0.0)),
        # 应收账款/营收（3年年报均值）
        0.0 if ar_rev3 is None else (-3.0 if ar_rev3 > 0.6 else (-1.5 if ar_rev3 > 0.4 else 0.0)),
        # 存货/总资产（最新年报）
        0.0 if (inv is None or assets is None or assets <= 0) else (-2.0 if inv / assets > 0.5 else (-1.0 if inv / assets > 0.35 else 0.0)),
        # 有息负债5年变动（翻倍且 5 年经营现金流为负 → 补亏举债重扣）
        0.0 if int_debt_grow is None else (-6.0 if (int_debt_grow > 1 and ocf5_sum is not None and ocf5_sum < 0) else (-3.0 if int_debt_grow > 1 else (-2.0 if int_debt_grow > 0.5 else 0.0))),
        # 近3年经营现金流 vs 利息费用
        -4.0 if ocf_covers is False else 0.0,
        # 营收5年变动
        0.0 if rev_grow is None else (-4.0 if rev_grow <= -0.5 else (-2.0 if rev_grow <= -0.2 else 0.0)),
        # 毛利率5年变动
        0.0 if g_margin_delta is None else (-4.0 if g_margin_delta <= -0.2 else (-2.0 if g_margin_delta <= -0.1 else 0.0)),
    )
    # 正分项归一 + 9 个扣分项原样相加：扣分项缺数据本就给 0，不参与归一（理由见 _school_total）
    s_total = _school_total(s_items, (25, 20, 20, 15, 10, 10), risk_items)

    # ---- 巴菲特芒格 ----
    moat_items = (
        lerp_score(g_margin, 0.2, 0.4, 0, 5),          # 销售毛利率 ≥ 40%
        # 近5年 ROE ≥ 10% 的达标年数占比：2/5 起给分，5/5 满分（护城河看的是持续，不是某一年）
        lerp_score(roe_ok_frac, 0.4, 1.0, 0, 4),
        # 只认无形资产、不含商誉：A 股 33,318 个「公司×年」实测（scripts/fraud_validity.py B 节），
        # 商誉/总资产 ≥10% 那组其后出现大额减值（≥净资产5%）的比例 29.01% vs 其余 12.28%
        # （lift 2.36、z=+20.7、四档单调），无形 ≥10% 那组 13.73% vs 13.22%（lift 1.04，分不开）。
        # 商誉是减值前兆而非定价权证据，且它在施洛斯侧已按 /归母权益 扣分，同侧再给分是自我反号
        lerp_score(intang_share, 0, 0.1, 0, 3),        # 无形资产占比 ≥ 10%
        # 连续分红 ≥ 5 年且分红率 ≤ 70%
        (3.0 if (va['payout'] is not None and va['payout'] <= 0.7) else 1.5)
        if div_consecutive >= 5 else 0.0,
    )
    b_items = (
        # ROE 水平档：与护城河那 4 分测的不是同一个量（一边是近5年中位数是否 ≥15%，
        # 一边是达标年数占比），实测 corr(中位数, 达标占比) = 0.1012，不再是一个变量押 29 分
        lerp_score(roe_med5, 0.10, 0.15, 0, 25),       # ROE 近5年中位数 ≥ 15%
        lerp_score(n_margin, 0.05, 0.10, 0, 15),       # 净利率 ≥ 10%
        lerp_score(debtr, 0.5, 0.75, 15, 0),           # 负债率 ≤ 50%
        lerp_score(va['ratio5'], 0.5, 1, 0, 15),       # 5年净现比 ≥ 1
        lerp_score(va['netCagr5'], 0, 0.1, 0, 15),     # 净利 5 年 CAGR ≥ 10%
    )
    # 缺得最多的是净现比（23.8%）与净利 5 年 CAGR（32.4%），各 15 分，早先一律白扣
    b_total = _school_total(moat_items + b_items, (5, 4, 3, 3, 25, 15, 15, 15, 15))

    return {
        'grahamAgg': g_a_total,
        'grahamDef': g_d_total,
        'schloss': s_total,
        'buffett': b_total,
    }


# ---- 价格参考（买入/保守卖出/公允卖出）----
# 三档都锚定各流派自己的估值阈值，不做任何反推：
#   买点   = 账面派（格攻/施洛斯）取资产折价线；收益派（格防/巴菲特）取保守卖价 × 2/3 安全边际
#   保守卖 = 该派核心估值锚：格攻 每股净流动资产、格防 15×EPS、施洛斯 每股净资产、巴菲特 公允PE×EPS
#   公允卖 = 保守卖上浮：格攻/施洛斯 1.5 倍（正是价格项归零点）、格防 4/3 倍（PE 20，半分位）、
#            巴菲特 1.3 倍（无对应评分项）
# 两条限制：
#   ① 收益派的 EPS 锚先用四道可信度门槛过滤（符号、隐含 PE、量级背离、经常性口径），过不了
#      就没有锚，三档一起空；
#   ② 账面派的三档里只有买点是「无条件目标价」，两档卖价只在该派估值射程（现价 ≤ SELL_BAND×
#      资产锚）内给出——资产锚是清算底，对 99% 的市场它不构成卖出参考。
# 曾用「二分反推使总分 ≥ 90 的最高价」，实测 6939 家里 56%~96% 的公司分数上限本就不足
# 90，tgt = min(90, t_max) 把目标悄悄降级成公司自己的上限，产出的买价与 90 分再无关系；
# 且买价中位数只有现价的 8%（格攻）~40%（格防），即要跌 60%~92% 才触发，不是参考价。
BUY_MARGIN = 2.0 / 3.0    # 收益派（格防/巴菲特）买点相对公允倍数的安全边际
G_A_PNCAV_FULL = 0.67     # 格攻：市值/净流动资产 ≤ 0.67 拿满 30 分，亦是买点倍数
G_D_PE_FULL = 15.0        # 格防：市盈率 ≤ 15 拿满 5 分，亦是保守卖价倍数
S_PB_FULL = 0.75          # 施洛斯：市净率 ≤ 0.75 拿满 25 分，亦是买点倍数
# 低于一分钱的参考价一律视为「无」：没有任何市场按这个价位报价，而它当分母会把
# 「折价率 1-现价/买价」吹到 1e20 量级。EPS 的滚动 TTM 是三项相减，留得住浮点零渣
# （实测 3 家：比依股份 5.6e-17、安旭生物 5.6e-17、中科通达 1.7e-18）。
MIN_PRICE_REF = 0.01
# 收益锚可信度：符号相反只是其中一种坏法，量级差一个数量级同样是坏锚（一次性损益、
# 股本口径错、年报窗口与快照 TTM 错配）。坏锚会占满「买入性价比」榜首：*ST华幸 买价
# 55.81 对现价 1.08、金科股份 46.4 倍、BKNG 13.9 倍、和黄医药 买价 28.91 对现价 19.14。
# 隐含 PE 下限取 3 倍：一家持续经营的公司不可能长期只值自己三年利润，市场按这个价位
# 报价就是在说这笔盈利不可重复。现行流水线实测，下限从 1 倍提到 3 倍多拦 3 家：碧桂园
# 1.54（债务重组收益）、RIGEL 2.42、天立国际 2.71。3~5 倍带内还有 32 家有锚公司，中国铁建
# 3.76、中国中铁 3.96、新华保险 4.08 是低 PE 常态化的真深度价值，诺比侃 3.13、天能动力 3.27
# 则是 EPS 字段与快照 PE 背离 8~9 倍的坏锚——那类归量级门槛（gate ③）管：年报窗口容忍从 10 倍
# 收到 5 倍能多砍 17 家锚，但会连带 Merck、太古这类年报 EPS 与快照真实差 5~10 倍的转折公司，
# 故不靠本下限去拦。
EPS_PE_MIN = 3.0          # 隐含 PE（现价/EPS）低于 3 倍：这笔盈利不可重复，锚作废
EPS_MAG_TTM = 3.0         # 自算值与快照同为 TTM 口径时，隐含 PE 允许的最大倍数差
EPS_MAG_ANNUAL = 10.0     # 最新报告期是年报（自算值为上一财年）时放宽到 10 倍，只砍数量级背离
# 经常性收益门槛：扣非后不足报告净利润一半，说明报告利润的大头来自非经常项目（处置资产、
# 政府补助、债务重组收益），按扣非后的规模重定锚；扣非转负则该派没有可资本化的盈利，锚作废。
# 只压缩不放大：扣非高于报告（一次性亏损压住报表）时维持报告口径，让锚停在偏保守一侧。
# 现行流水线实测：4655 家有收益锚的公司里 297 家被压缩、3 家归零（安达科技、地纬智能、张裕Ａ）。
# 两道覆盖边界写在前面，别把「没触发」当成「没问题」：① 扣非是 A 股科目，港美股不披露、门槛
# 不介入，那侧全靠上面的隐含 PE 下限；② 券商保险的公允价值变动损益按准则不计入非经常性损益
# （中国人寿 TTM 扣非/报告 = 1.001），投资浮盈撑高 TTM 盈利的场景本门槛结构上抓不到。
RECURRING_MIN_RATIO = 0.5
# 资产派（格攻/施洛斯）卖价射程：现价超过 2× 资产锚就不再给卖出参考。资产锚是清算底/
# 账面底，健康持续经营的公司现价本就常年在其 5~10 倍处，把它当「卖到这就该减仓」的触发线，
# 等于对几乎整个市场亮红灯。实测（6939 家，有锚家数 → 其中现价≥保守卖价的比例）：
#   格攻   4435 → 245 家，命中率 99.2% → 84.9%
#   施洛斯 6809 → 2337 家，命中率 90.6% → 72.7%
# 收益派两派不套射程（现价高于 15×EPS 或公允 PE 是有意义的信号），命中率仍为 83% 左右——
# 市场确实长期站在格雷厄姆的 PE 15 线之上，这是口径本身的性质，不是缺陷。
# 取 2 倍是为了让账面派的公允卖价（1.5× 锚，即评分里价格项的归零点）留在射程内。
SELL_BAND = 2.0


def _fair_pe(net_cagr5):
    """巴菲特合理市盈率 = 净利5年CAGR×100，夹在 [8, 25]；无数据取 15"""
    if net_cagr5 is None:
        return 15.0
    return max(8.0, min(25.0, net_cagr5 * 100.0))


def _ttm_net_profit(rows, field='净利润'):
    """滚动 TTM 利润（利润表为累计口径）：最新报告期累计 + 上年年报 - 上年同期累计；
    最新报告期为年报时直接取年报数；任一要素缺失返回 None"""
    by_date = {}
    for r in (rows or []):
        p = str(r.get('报告期') or '')
        if len(p) >= 10:
            by_date[p[:10]] = r.get(field)
    if not by_date:
        return None
    latest_p = max(by_date)  # YYYY-MM-DD 字符串排序即时间序
    cur = by_date[latest_p]
    y, m, d = latest_p[:4], latest_p[5:7], latest_p[8:10]
    if m == '12':
        return cur
    prev_ann = by_date.get(str(int(y) - 1) + '-12-31')
    prev_same = by_date.get(str(int(y) - 1) + '-' + m + '-' + d)
    if cur is None or prev_ann is None or prev_same is None:
        return None
    return cur + prev_ann - prev_same


def _share_count(balance_rows, shares_fallback, bps_field=None):
    """财报股本优先（最新年报实收资本，港股退而取股本），与快照股本偏差 >5% 视为面值异常/口径不同时回退。
    仍不行时用归母权益/每股净资产反推（数据源口径、随财报更新）；
    港股“股本”常为面值总额（面值 0.1/0.01/0.001 等），再按常见面值反推股数。
    快照股本 mcap/price 随实时价抖动（含快照舍入/滞后），财报股本使每股量完全财报驱动"""
    rows = [r for r in (balance_rows or []) if str(r.get('报告日') or '').endswith('12-31')]
    row = None
    if rows:
        row = sorted(rows, key=lambda r: str(r.get('报告日')))[-1]
    cap_cn = None
    cap_hk = None
    eq = None
    if row:
        cap_cn = row.get('实收资本(或股本)')
        cap_hk = row.get('股本')
        eq = equity_of(row)
    for c in (cap_cn, cap_hk):
        if c and shares_fallback and 0.95 <= c / shares_fallback <= 1.05:
            return c
    # 权益/每股净资产反推（每股净资产=权益/股数，数据源算好的财报口径）；偏差 25% 内视为同口径
    if bps_field and eq and shares_fallback:
        c2 = eq / bps_field
        if 0.75 <= c2 / shares_fallback <= 1.25:
            return c2
    # 港股“股本”常为面值总额，按常见面值（0.1/0.01/0.001）反推股数；偏差 12% 内视为同口径
    if cap_hk and shares_fallback:
        for mul in (10, 100, 1000):
            c3 = cap_hk * mul
            if 0.88 <= c3 / shares_fallback <= 1.12:
                return c3
    return shares_fallback


def _latest_field(rows, field):
    """indicators 最新报告期字段值（该期缺失时回退到上一期有值的）"""
    best = None
    for r in (rows or []):
        p = str(r.get('报告期') or '')
        if len(p) >= 10 and (best is None or p > best[0]):
            v = r.get(field)
            if v is not None:
                best = (p, v)
    return best[1] if best else None


def _eps_ttm_field(rows):
    """基本每股收益字段（累计口径）滚动 TTM：最新累计 + 上年年报 - 上年同期累计；最新为年报时直接用年报值"""
    by_date = {}
    for r in (rows or []):
        p = str(r.get('报告期') or '')
        if len(p) >= 10:
            by_date[p[:10]] = r.get('基本每股收益')
    if not by_date:
        return None
    latest_p = max(by_date)
    cur = by_date[latest_p]
    if cur is None:
        return None
    y, m = latest_p[:4], latest_p[5:7]
    if m == '12':
        return cur
    prev_ann = by_date.get(str(int(y) - 1) + '-12-31')
    prev_same = by_date.get(str(int(y) - 1) + latest_p[4:10])
    if prev_ann is None or prev_same is None:
        return None
    return cur + prev_ann - prev_same


def _latest_period_is_annual(rows):
    """indicators 最新报告期是否年报。_eps_ttm_field / _ttm_net_profit 在年报期都直接
    返回当期值、不做滚动相减，所以这个判断等于「自算值是上一财年数，还是与快照 PE
    同为 TTM 口径的真滚动值」"""
    periods = [str(r.get('报告期') or '')[:10]
               for r in (rows or []) if len(str(r.get('报告期') or '')) >= 10]
    return bool(periods) and max(periods)[5:7] == '12'


def _note_latest(notes, day):
    """附注（其他流动资产构成 / 受限货币资金）→ 报告日不晚于 day 的最近一份。

    附注只在半年报与年报里披露（06-30、12-31 两个报告日），而最新一期资产负债常常
    是三季报，故按「上一次披露的构成」用。日期都是 YYYY-MM-DD，字符串序即时间序。
    返回值带上命中的报告日，附注字典本身只有数值没有日期。
    """
    if not isinstance(notes, dict) or not day:
        return None
    got = [(k[:10], v) for k, v in notes.items()
           if isinstance(k, str) and len(k) >= 10 and k[:10] <= day
           and isinstance(v, dict)]
    return max(got, key=lambda x: x[0]) if got else None


def _int_debt(row):
    """有息负债全口径（短借+一年内到期+长借+应付债券+租赁，缺键当 0）：行缺 → None。"""
    if not row:
        return None
    v = ssum([row.get('短期借款'), row.get('一年内到期的非流动负债'),
              row.get('长期借款'), row.get('应付债券'), row.get('租赁负债')])
    return 0.0 if v is None else v


def _weighted_cash(ba_row, note=None):
    """加权类现金：可用货币资金×1.0 ＋ 交易性金融资产×0.7 ＋ 应收票据×0.4
    ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0。

    定期存款/受限货币资金来自财报附注（note 字典），闭合才采信；存款不超过「其他流动资产」
    科目值，受限的按 0 折。行缺或货币资金缺（银行/外资口径行）→ None；其余科目缺按 0 折入。
    price_references（最新一期）与 value_scores 格攻净现金（年报行）共用这一条折算。
    """
    if not ba_row or ba_row.get('货币资金') is None:
        return None
    cash_v = ba_row.get('货币资金')
    fin_v = ba_row.get('交易性金融资产')
    notes_v = ba_row.get('应收票据')
    other_v = ba_row.get('其他流动资产')
    rst_v = note.get('restrictedCash') if isinstance(note, dict) else None
    dep_v = note.get('termDeposit') if isinstance(note, dict) else None
    dep_v = dep_v if isinstance(dep_v, (int, float)) else None
    rst_v = rst_v if isinstance(rst_v, (int, float)) else None
    avail_v = max(0.0, cash_v - rst_v) if (cash_v is not None and rst_v is not None) else cash_v
    if dep_v is not None:
        dep_v = None if other_v is None else min(dep_v, other_v)
    other_nd = max(0.0, other_v - dep_v) if (other_v is not None and dep_v is not None) else other_v

    def gw(v, k):
        return (v * k) if v is not None else 0.0

    return (gw(avail_v, 1.0) + gw(fin_v, 0.7) + gw(notes_v, 0.4)
            + gw(other_nd, 0.3) + gw(dep_v, 1.0))


def price_references(d, va):
    """对应 JS priceReferences：公允清算价值 + 四大流派买入/保守卖出/公允卖出价格参考
    fairLiq = 每股公允清算价值（流动资产合计-负债合计）/财报股本，格雷厄姆清算口径"""
    s = d.get('snapshot') or {}
    price0, mcap0 = s.get('price'), s.get('market_cap')
    pe0, pb0 = s.get('pe_ttm'), s.get('pb')
    if price0 is None or price0 <= 0:
        return {'fairLiq': None,
                'netCashRatio': None,
                'wCash': None,
                'intDebt': None,
                'netCashW': None,
                'netCashCalc': None,
                'grahamAgg': {'buy': None, 'sellCons': None, 'sellFair': None},
                'grahamDef': {'buy': None, 'sellCons': None, 'sellFair': None},
                'schloss': {'buy': None, 'sellCons': None, 'sellFair': None},
                'buffett': {'buy': None, 'sellCons': None, 'sellFair': None}}

    # ---- 基础量（最新年报资产负债表）----
    annual = annual_rows(d.get('indicators') or [])
    last = annual[-1] if annual else None
    last_date = str(last.get('报告期') or '')[:10] if last else None
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    last_ba = sheet_row_by_date(ba_list, last_date) if last_date else None

    def g(key):
        return last_ba.get(key) if last_ba else None

    ca, tl = g('流动资产合计'), g('负债合计')
    ncav = (ca - tl) if (ca is not None and tl is not None) else None
    last_eq = equity_of(last_ba)
    # 每股净资产优先用指标字段（数据源按财报算好、随财报更新，与实时价无关），
    # 避免快照 pb/pe 舍入与 mcap 滞后导致参考价随行情漂移（财务无变化时参考价应不变）
    bps = _latest_field(d.get('indicators') or [], '每股净资产')
    # 股本优先用财报实收资本（最新年报），快照 mcap/price 会随实时价抖动（快照舍入/滞后）
    shares = _share_count(d.get('balance') or [], mcap0 / price0 if mcap0 is not None else None, bps)
    ncav_ps = ncav / shares if (ncav is not None and shares) else None
    if bps is None and last_eq is not None and shares:
        bps = last_eq / shares
    if bps is None:
        bps = price0 / pb0 if (pb0 is not None and pb0 > 0) else None
    eps_ttm = _eps_ttm_field(d.get('indicators') or [])
    if eps_ttm is None:
        ttm_net = _ttm_net_profit(d.get('indicators') or [])
        eps_ttm = (ttm_net / shares) if (ttm_net is not None and shares) else None
    if eps_ttm is None:
        eps_ttm = price0 / pe0 if (pe0 is not None and pe0 > 0) else None
    # ---- EPS 锚可信度：坏锚会让收益派三档价一起错，还会霸榜「买入性价比」排序 ----
    is_annual = _latest_period_is_annual(d.get('indicators') or [])
    # ① 符号相反且同为 TTM 口径 → 至少一边错。最新报告期是年报时 _eps_ttm_field 直接
    # 返回上一财年 EPS，与 TTM 快照本就不同窗口，符号相反是真实的一次性减值/转折
    # （实测 65 家：GILD 上一财年 +6.84 对 TTM 隐含 -2.65、COIN +4.85 对 -3.92），保留。
    # 中间期时自算值就是 cur+上年年报-上年同期 的真 TTM，实测 37 家全是累计相减在零附近
    # 的残差（招商蛇口 0.06+0.08-0.14=0 对 pe0=+565、山东墨龙 0.0001 对 pe0=-2558）。
    if (eps_ttm is not None and pe0 is not None and pe0 != 0 and not is_annual
            and (eps_ttm > 0) != (pe0 > 0)):
        eps_ttm = None
    # ② 隐含 PE（现价/EPS）低于 EPS_PE_MIN 倍：一家持续经营的公司不可能长期只值自己三年
    # 利润，市场按这个价位报价就是在说这笔盈利不可重复（下限 1→3 倍多拦的 3 家见常量注释）。
    # 旧值 1 倍时只拦得住金科股份一家单位/口径错（快照 PE 与自算 EPS 同给 0.36）。
    if eps_ttm is not None and eps_ttm > 0 and price0 / eps_ttm < EPS_PE_MIN:
        eps_ttm = None
    # ③ 量级背离：同 TTM 口径差 3 倍以上、年报窗口差 10 倍以上（实测 7 + 8 家）。
    # 年报窗口放宽是因为真实业绩可以一年翻几倍，但 10 倍以上的背离全是坏锚
    # （和黄医药 buy 28.91 对现价 19.14、BKNG 隐含 2497.8 倍、中国中冶 0.03 倍）。
    if eps_ttm is not None and eps_ttm > 0 and pe0 is not None and pe0 > 0:
        mag = EPS_MAG_ANNUAL if is_annual else EPS_MAG_TTM
        if not (1.0 / mag <= (price0 / eps_ttm) / pe0 <= mag):
            eps_ttm = None
    # ④ 经常性收益：报告利润的大头来自非经常项目时，按扣非后的规模重定锚。实测 4655 家有收益锚
    # 的公司里 297 家被压缩、3 家归零（安达科技、地纬智能、张裕Ａ——扣非转负，报表盈利靠处置与
    # 补助撑起）。只压缩不放大：扣非高于报告时维持报告口径，让锚停在偏保守一侧。
    # 抓不到的两类写在常量注释里：港美股不披露扣非，券商保险的公允价值变动损益不计入非经常。
    if eps_ttm is not None and eps_ttm > 0:
        ttm_net = _ttm_net_profit(d.get('indicators') or [])
        adj_net = _ttm_net_profit(d.get('indicators') or [], '扣非净利润')
        if ttm_net is not None and adj_net is not None and ttm_net > 0:
            if adj_net < RECURRING_MIN_RATIO * ttm_net:
                eps_ttm = (eps_ttm * adj_net / ttm_net) if adj_net > 0 else None
    # 净现金/市值：最近一期财报（加权类现金 − 负债合计）÷ 快照总市值；
    # 类现金保守折算：可用货币资金×1.0 ＋ 交易性金融资产×0.7 ＋ 应收票据×0.4
    #                ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0；
    # 定期存款与受限货币资金来自财报附注（PDF 解析），附注缺失时两者为空、式子退回旧口径；
    # 分子随财报更新（含季报），分母随行情快照，缺失科目按 0 折入
    latest_ba = ba_list[-1] if ba_list else None

    def gb(key):
        v = latest_ba.get(key) if latest_ba else None
        return v if isinstance(v, (int, float)) else None

    cash_v = gb('货币资金')
    fin_v = gb('交易性金融资产')
    notes_v = gb('应收票据')
    other_v = gb('其他流动资产')
    tl_latest = gb('负债合计')
    # 附注拆分：「其他流动资产」里是定期存款还是留抵税额，科目层分不出来，而折算系数
    # 差 3 倍（实测广信股份 37.02 亿里 36.91 亿是定期存款）。附注只认闭合得上的数，
    # 没有就当不存在——届时 dep_v/rst_v 皆 None，下面式子与旧口径逐位相同。
    found = _note_latest(d.get('notes'), str(latest_ba.get('报告日') or '')[:10]) \
        if latest_ba else None
    note_day, note = (found if found else (None, {}))
    dep_v = note.get('termDeposit')
    dep_v = dep_v if isinstance(dep_v, (int, float)) else None
    rst_v = note.get('restrictedCash')
    rst_v = rst_v if isinstance(rst_v, (int, float)) else None
    # 受限的货币资金动不了，按 0 折；抽取侧已保证受限额不超过货币资金，这里再夹一次
    avail_v = max(0.0, cash_v - rst_v) if (cash_v is not None and rst_v is not None) else cash_v
    # 定期存款是「其他流动资产」里拆出来的一块，不能超过该科目的当期值：附注常比资产负债
    # 表早一期（三季报无附注），上一期的存款到这一期可能已到期或转出，科目也整体算不出时
    # 更无从断定它还在。超过就按科目上限夹住、科目缺失就不计，宁少不错。
    if dep_v is not None:
        dep_v = None if other_v is None else min(dep_v, other_v)
    other_nd = max(0.0, other_v - dep_v) if (other_v is not None and dep_v is not None) else other_v

    weighted_cash = _weighted_cash(latest_ba, note)
    has_core = (cash_v is not None and tl_latest is not None and mcap0)
    net_cash_ratio = ((weighted_cash - tl_latest) / mcap0) if has_core else None
    net_cash_calc = ({'cash': cash_v,
                      'fin': fin_v,
                      'notes': notes_v,
                      'otherCA': other_v,
                      'tl': tl_latest,
                      'mcap': mcap0,
                      'report': str(latest_ba.get('报告日') or '')[:10] or None,
                      'termDeposit': dep_v,
                      'restricted': rst_v,
                      'noteReport': note_day}
                     if has_core else None)
    fair_pe = _fair_pe(va.get('netCagr5'))

    def ref(v):
        """低于一分钱（含负值与浮点零渣）的参考价一律视为无"""
        return v if (v is not None and v >= MIN_PRICE_REF) else None

    def band(anchor):
        """资产派卖价只在估值射程内给出：现价已在 anchor 的 SELL_BAND 倍之外，说明该派的
        资产口径对这只股票没有「该卖」的意见（它从来不是这只股票的估值方法），留空。
        现价无快照的情形已在函数开头整体返回空，这里不必再判"""
        return anchor if anchor is None or price0 <= SELL_BAND * anchor else None

    ncav_ref = ref(ncav_ps)
    s_ref = ref(bps)
    # TTM 每股亏损（≤0）时基于 EPS 的估值锚无意义，锚位与买入价一并置空（避免负价/误导价）
    eps_ok = eps_ttm is not None and eps_ttm > 0
    gA_cons = band(ncav_ref)
    gD_cons = ref(G_D_PE_FULL * eps_ttm) if eps_ok else None
    s_cons = band(s_ref)
    b_cons = ref(fair_pe * eps_ttm) if eps_ok else None

    # 公允卖价跟着保守卖价同生同灭：每派公允都是保守的 ≥1.3 倍，保守过了一分钱下限
    # 公允必然也过；但若各自独立套下限，会在保守差一点、公允刚过点时只留半档，
    # 而卖点筛选要求「同时 ≥ 保守与公允」，半档等于把这家公司永久排除。
    # 买点不挂卖点：锚为空才没有买点；资产派「出射程」只抹掉两档卖价，买点作为目标价照旧给出。
    # 净现金三件套（列表三列用，本币）：加权类现金、有息负债、净现金=加权−有息。
    # 与 net_cash_ratio（减全部负债的宽口径比值）并存，口径差见说明书 §2.1。
    int_debt_latest = _int_debt(latest_ba)
    return {
        'fairLiq': ncav_ref,
        'netCashRatio': net_cash_ratio,
        'wCash': weighted_cash,
        'intDebt': int_debt_latest,
        'netCashW': (weighted_cash - int_debt_latest) if weighted_cash is not None else None,
        'netCashCalc': net_cash_calc,
        'grahamAgg': {
            'buy': ref(G_A_PNCAV_FULL * ncav_ref) if ncav_ref is not None else None,
            'sellCons': gA_cons,
            'sellFair': (1.5 * gA_cons) if gA_cons is not None else None,
        },
        'grahamDef': {
            'buy': ref(BUY_MARGIN * gD_cons) if gD_cons is not None else None,
            'sellCons': gD_cons,
            'sellFair': (20.0 * eps_ttm) if gD_cons is not None else None,
        },
        'schloss': {
            'buy': ref(S_PB_FULL * s_ref) if s_ref is not None else None,
            'sellCons': s_cons,
            'sellFair': (1.5 * s_cons) if s_cons is not None else None,
        },
        'buffett': {
            'buy': ref(BUY_MARGIN * b_cons) if b_cons is not None else None,
            'sellCons': b_cons,
            'sellFair': (fair_pe * eps_ttm * 1.3) if b_cons is not None else None,
        },
    }


def fraud_analysis(d):
    """对应 JS fraudAnalysis —— 财报造假可能性量化红旗筛查总分（0~100，越高越可疑）。
    借鉴 Beneish M-Score 思路：8 项红旗按严重度加权，数据不足项计 0 分不误伤。"""
    annual = annual_rows(d.get('indicators') or [])
    last = annual[-1] if annual else None
    prev = annual[-2] if len(annual) >= 2 else None
    last_date = str(last.get('报告期') or '')[:10] if last else None
    prev_date = str(prev.get('报告期') or '')[:10] if prev else None
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    cf_list = sorted(d.get('cashflow') or [], key=lambda r: str(r.get('报告日') or ''))
    last_ba = sheet_row_by_date(ba_list, last_date) if last_date else None
    prev_ba = sheet_row_by_date(ba_list, prev_date) if prev_date else None
    last_cf = sheet_row_by_date(cf_list, last_date) if last_date else None

    rev = last.get('营业总收入') if last else None
    rev_prev = prev.get('营业总收入') if prev else None
    net = last.get('净利润') if last else None
    ocf = last_cf.get('经营活动产生的现金流量净额') if last_cf else None
    sold_cash = last_cf.get('销售商品、提供劳务收到的现金') if last_cf else None
    assets = last_ba.get('资产总计') if last_ba else None
    ar = ar_of(last_ba)
    ar_prev = ar_of(prev_ba)
    inv = last_ba.get('存货') if last_ba else None
    inv_prev = prev_ba.get('存货') if prev_ba else None
    other_ar = None
    if last_ba:
        other_ar = last_ba.get('其他应收款')
        if other_ar is None:
            other_ar = last_ba.get('其他应收款(合计)')
    # 商誉/无形双缺是「科目没取到」不是「没有软资产」：按 0 计会让该项(5分)永不亮灯，
    # 且 0 分折进 _weighted_total 的归一化分母会稀释其余红旗——缺数据显得更干净，
    # 与该函数「缺项中性」的初衷相反（实测 256 家双缺，其中 235 家港美股）。单缺一科
    # 按另一科计：只披露其一的报表，软资产至少有披露侧的那部分。
    gw_v = last_ba.get('商誉') if last_ba else None
    it_v = last_ba.get('无形资产') if last_ba else None
    soft = None if (gw_v is None and it_v is None) else ((gw_v or 0.0) + (it_v or 0.0))
    gm = last.get('销售毛利率') if last else None
    gm_prev = prev.get('销售毛利率') if prev else None

    # 近5年累计净现比（比单年稳健：累计经营现金流 ÷ 累计净利润）
    sum_net, sum_ocf, hit = 0.0, 0.0, False
    for r in annual[-5:]:
        cf = sheet_row_by_date(cf_list, str(r.get('报告期') or '')[:10])
        n = r.get('净利润')
        o = cf.get('经营活动产生的现金流量净额') if cf else None
        if n is not None and o is not None:
            sum_net += n
            sum_ocf += o
            hit = True
    ratio5 = sum_ocf / sum_net if (hit and sum_net > 0) else None

    def grow(cur, pre):
        return (cur / pre - 1.0) if (cur is not None and pre is not None and pre > 0) else None

    rev_grow = grow(rev, rev_prev)
    ar_grow = grow(ar, ar_prev)
    inv_grow = grow(inv, inv_prev)
    ar_gap = (ar_grow - rev_grow) if (ar_grow is not None and rev_grow is not None) else None
    inv_gap = (inv_grow - rev_grow) if (inv_grow is not None and rev_grow is not None) else None
    gm_delta = (gm - gm_prev) if (gm is not None and gm_prev is not None) else None
    tata = ((net - ocf) / assets) if (net is not None and ocf is not None and assets is not None and assets > 0) else None
    other_share = other_ar / assets if (other_ar is not None and assets is not None and assets > 0) else None
    soft_share = soft / assets if (soft is not None and assets is not None and assets > 0) else None
    collect = sold_cash / rev if (sold_cash is not None and rev is not None and rev > 0) else None

    # 严重度分段：v≤a→0；a~b→0~0.5；b~c→0.5~1；≥c→1（越高越可疑）
    def sev(v, a, b, c):
        if v is None:
            return None
        if v <= a:
            return 0.0
        if v >= c:
            return 1.0
        if v <= b:
            return (v - a) / (b - a) * 0.5
        return 0.5 + (v - b) / (c - b) * 0.5

    def w(score, max_v):
        return None if score is None else score * max_v

    # 净现比：≥1 无红旗；0~1 线性升；≤0 满严重（利润无现金支撑）
    s1 = None if ratio5 is None else lerp_score(ratio5, 0, 1, 1, 0)
    # 收现比：≥100% 无红旗；60%~100% 线性升；≤60% 满严重
    s8 = None if collect is None else lerp_score(collect, 0.6, 1, 1, 0)
    # 毛利率上升才可疑（下降属经营问题）
    s5 = None if gm_delta is None else (0.0 if gm_delta <= 0 else sev(gm_delta, 0, 0.05, 0.10))

    scores = [
        w(s1, 25),                              # 5年累计净现比，25 分
        w(sev(tata, 0.02, 0.06, 0.10), 20),     # 总应计比率，20 分
        w(sev(ar_gap, 0.05, 0.20, 0.40), 15),   # 应收增速−营收增速，15 分
        w(sev(inv_gap, 0.05, 0.25, 0.50), 10),  # 存货增速−营收增速，10 分
        w(s5, 10),                              # 毛利率同比变动，10 分
        w(sev(other_share, 0.02, 0.05, 0.10), 10),  # 其他应收款占用，10 分
        w(sev(soft_share, 0.10, 0.20, 0.35), 5),    # 资产偏软，5 分
        w(s8, 5),                               # 销售收现比，5 分
    ]
    # 归一化：本分越低越可疑，缺项若只从总分里少加一笔，等于数据不全反而“更干净”
    total = _weighted_total(list(zip(scores, (25, 20, 15, 10, 10, 10, 5, 5))))
    if total is None:
        return None
    # 与 JS Math.round(total*10)/10 一致（Python round 为银行家舍入，不能直接用）
    return math.floor(total * 10 + 0.5) / 10.0


# ---- 价值陷阱分 T（0~100，分高＝坏消息堆得多）：对应 JS trapScore，逐项同构 ----
# 权重与阈值来自 backend/scripts/trap_validity.py 的实测（A 股 32,178 条「公司 × T 年」观测，
# 事件时信息集，结局取信号日之后公开的第一份年报）。ln(lift) 是该项坏侧相对其余的最强结局
# 对数危险比；TRAP_CUT 取当时三分位边界后四舍五成绝对值——阈值若按当日横截面分位算，一家
# 公司的分会被当天其他公司的涨跌改掉，按日入库就不可复现。
TRAP_W = {'ded_half': 1.63, 'gw_asset': 0.70, 'roe_delta': 0.69, 'fraud': 0.51,
          'seo_dilu': 0.49, 'gm_delta': 0.48, 'ocfnp_med': 0.42}
TRAP_SUM_W = 4.92          # Σ TRAP_W，固定分母
TRAP_CUT = {'roe_delta': -0.04, 'gm_delta': -0.03, 'ocfnp_med': 0.80}
# 回测五分位边界（C = Σ 权重×亮灯）与该档实测发生率 %：分数自己没有含义，这张表才有
TRAP_BANDS = (
    (0.001, '档1 无证据', 3.66, 6.47, 13.48, 5.92, 9.75),
    (0.50, '档2 单点', 4.87, 7.75, 16.18, 9.32, 11.35),
    (1.00, '档3 两点', 6.27, 11.76, 20.17, 8.90, 17.04),
    (1.60, '档4 成串', 10.38, 17.90, 27.26, 13.25, 22.88),
    (float('inf'), '档5 叠加', 28.70, 23.31, 35.88, 24.11, 23.44),
)
BAND_KEYS = ('label', 'loss', 'imp5', 'imp3', 'divcut', 'bvpsdn')


def trap_band_of(c):
    for row in TRAP_BANDS:
        if c <= row[0]:
            return dict(zip(BAND_KEYS, row[1:]))
    return dict(zip(BAND_KEYS, TRAP_BANDS[-1][1:]))


def _trap_med(vals):
    """近 5 期里那一项的中位数；不足 3 期返回 None——两期的中位数就是平均数，噪声当不了趋势。
    NaN/inf 一并丢掉（对齐 JS 的 isFinite 过滤）：带着 NaN 排序，中位数会静默错位。"""
    got = sorted(v for v in vals
                 if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
    if len(got) < 3:
        return None
    mid = len(got) // 2
    return got[mid] if len(got) % 2 else (got[mid - 1] + got[mid]) / 2.0


def trap_score(d):
    """对应 JS trapScore。返回 {total, c, band, evaluated, missing, na}；缺项按 0 计入分子、
    分母恒为 TRAP_SUM_W，**不走** _weighted_total 那套「按可用项归一」：那会让只查得动 1 项且
    恰好亮灯的公司拿满 100 分，而陷阱分最怕的正是「没查到」被读成「没毛病」。覆盖度由
    evaluated 单独出，压低方向是「看不准就别声称干净」。
    """
    d = d or {}
    if (d.get('market') or 'A') != 'A':
        # 定增/回购源、扣非口径、减值科目只在这一侧成立——不适用，不是缺失
        return {'total': None, 'c': None, 'band': None, 'evaluated': 0, 'missing': 0, 'na': 7}
    annual = annual_rows(d.get('indicators') or [])
    if not annual:
        return {'total': None, 'c': None, 'band': None, 'evaluated': 0, 'missing': 7, 'na': 0}
    win = annual[-5:]
    cur = win[-1]
    ba_annual = annual_balance_rows(d.get('balance'))
    cf_annual = annual_balance_rows(d.get('cashflow'))
    cur_date = str(cur.get('报告期') or '')[:10]
    hi_y, lo_y = int(cur_date[:4]), int(str(win[0].get('报告期') or '')[:4])
    cur_ba = sheet_row_by_date(ba_annual, cur_date) or {}
    c, ev, n_items = 0.0, 0, 0

    def hit(key, bad):
        """bad: None=判不动（不进分子也不点亮）/ 0 / 1"""
        nonlocal c, ev, n_items
        n_items += 1
        if bad is None:
            return
        ev += 1
        if bad:
            c += TRAP_W[key]

    # 1. 扣非不足报告净利一半（利润靠一次性收益撑）——实测最强单项，转亏 lift 5.08×
    ded, net = cur.get('扣非净利润'), cur.get('净利润')
    hit('ded_half', None if (ded is None or not net or net <= 0)
        else (1 if ded < 0.5 * net else 0))

    # 2. 商誉/总资产 ≥10%（减值弹药）
    gw, ta = cur_ba.get('商誉'), cur_ba.get('资产总计')
    hit('gw_asset', None if (gw is None or not ta or ta <= 0)
        else (1 if gw / ta >= 0.10 else 0))

    # 3~4. ROE / 毛利率「最新 − 近5年中位」：取减速而不是水平（水平归成长分）。
    # ROE 用披露的「净资产收益率」原列，与巴菲特那项的 va['dupontRoe'] 序列不同源——回测的
    # 权重是在这一列上量的，换源等于换分项。
    cur_roe = cur.get('净资产收益率')
    roe_med = _trap_med([r.get('净资产收益率') for r in win])
    hit('roe_delta', None if (roe_med is None or cur_roe is None)
        else (1 if cur_roe - roe_med <= TRAP_CUT['roe_delta'] else 0))
    cur_gm = cur.get('销售毛利率')
    gm_med = _trap_med([r.get('销售毛利率') for r in win])
    hit('gm_delta', None if (gm_med is None or cur_gm is None)
        else (1 if cur_gm - gm_med <= TRAP_CUT['gm_delta'] else 0))

    # 5. 净现比 5 年中位 ≤0.80。上档发生率同样偏高（净利太薄时比值虚高），故只取低侧作证据
    ratios = []
    for r in win:
        cf = sheet_row_by_date(cf_annual, str(r.get('报告期') or '')[:10]) or {}
        ocf, n = cf.get('经营活动产生的现金流量净额'), r.get('净利润')
        ratios.append(ocf / n if (ocf is not None and n and n > 0) else None)
    ocfnp = _trap_med(ratios)
    hit('ocfnp_med', None if ocfnp is None else (1 if ocfnp <= TRAP_CUT['ocfnp_med'] else 0))

    # 6. 造假分 >50：与列表页门槛、刷池线同一个数，不另起口径
    fa = fraud_analysis(d)
    hit('fraud', None if fa is None else (1 if fa > 50 else 0))

    # 7. 近 5 年定增摊薄 / 隐含股本。分母用 归母权益÷每股净资产 反推，不用「股本」列——
    # 送股转增会把那一列放大却不摊薄任何人的权益。无定增是公开事实（A 股定增史全量可查），
    # 记 0 而非判不动；但**字段压根没给**是判不动——两者差一格，混起来等于在采集接通之前
    # 替全市场担保没摊薄过。有定增而股本算不出，同样留 None。
    # 只喂定增行（d['seo_actions']）：回购族没有一项进 T（注销式回购实测 lift 1.31、方向还存疑）。
    acts = d.get('seo_actions')
    eq = equity_of(cur_ba)
    bps = cur.get('每股净资产')
    sh = eq / bps if (eq is not None and bps and bps > 0) else None
    issued, seen = 0.0, False
    for r in (acts or []):
        ds = str(r.get('issue_date') or r.get('listing_date') or '')[:10]
        if ds and lo_y <= int(ds[:4]) <= hi_y and r.get('num'):
            issued += float(r['num'])
            seen = True
    hit('seo_dilu', None if acts is None
        else (0 if not seen else (None if not sh else (1 if issued / sh > 0 else 0))))

    return {
        'total': (math.floor(c / TRAP_SUM_W * 1000 + 0.5) / 10.0) if ev else None,
        'c': c, 'band': trap_band_of(c) if ev else None,
        'evaluated': ev, 'missing': n_items - ev, 'na': 0,
    }


# ---- 成长综合分 G（0~100，分高＝过去五年更能长）：对应 JS growthScore，逐项同构 ----
# 权重与锚点来自 backend/scripts/g_validity.py 的实测（A 股 21,926 条「公司 × 信号年」观测，
# 事件时信息集，结局为锚点之后公开的真实净利增速、平滑分母口径）。锚点是**固定绝对阈值**：
# 按当日横截面分位取会让一家公司的分被其余六千家的涨跌改掉，按日入库就不可复现。
# accel 那一项的锚点按面板输入端 p10→p90（实测 −0.74/+0.42）取整定，写死 ±10% 会把 56% 的公司
# 挤在零分上、一项退化成开关。stab 的锚点写成 (3, 0) 是故意的：负增长年数越少越好。
G_ITEMS = (
    ('np_g5', 22, -0.05, 0.15),
    ('rev_g5', 16, -0.05, 0.15),
    ('roe_med', 16, 0.05, 0.18),
    ('bps_g5', 14, -0.02, 0.12),
    ('stab', 14, 3.0, 0.0),
    ('accel', 10, -0.70, 0.40),
    ('roe_trend', 8, -0.05, 0.05),
)
G_SUM_W = 100.0      # Σ G_ITEMS 权重，固定分母
G_CAP = 0.5          # 增速 winsorize 边界 ±50%/年
G_MIN_PAIRS = 4      # 5 年窗本该有 5 个同比间隔，缺 1 个仍算判得动


def _g_num(v):
    """对应 JS 的 `typeof v === 'number' && isFinite(v)`：bool、NaN、inf 都不是一个可用的数。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _g_growth(cur, prev, span):
    """年化增速，夹到 ±G_CAP。与既有的 cagr 刻意不同两处：基期为正而当期转负记成下界（那是
    真实的坏消息，不是「算不出」），以及上界夹逼（一次重组不该把整轴拉爆）。基期非正 → None。
    """
    if cur is None or prev is None or span <= 0 or prev <= 0:
        return None
    if cur <= 0:
        return -G_CAP
    return max(-G_CAP, min(G_CAP, (cur / prev) ** (1.0 / span) - 1.0))


def growth_score(d):
    """对应 JS growthScore。返回 {total, evaluated, missing, na, raw}；分母恒为 G_SUM_W，
    缺项不进分子也不重新归一（同陷阱分的纪律：按可用项归一会让只算得动一项的公司顶到榜首）。
    全市场适用——七项只读年报四列，港美股同样有，故不出 na（覆盖度差异由 evaluated 出）。
    """
    d = d or {}
    by_year = {}
    for r in annual_rows(d.get('indicators') or []):
        ys = str(r.get('报告期') or '')[:4]
        if not ys.isdigit() or int(ys) == 0:
            continue
        slot = by_year.setdefault(int(ys), {})
        for k, col in (('net', '净利润'), ('rev', '营业总收入'),
                       ('roe', '净资产收益率'), ('bps', '每股净资产')):
            v = r.get(col)
            if _g_num(v):                     # 同年后一行覆盖前一行，缺列留空
                slot[k] = v
    years = sorted(by_year)
    if len(years) < 3:
        return {'total': None, 'evaluated': 0, 'missing': len(G_ITEMS), 'na': 0, 'raw': {}}
    ay = years[-1]
    # 基期必须真是 5 年前：取不晚于「锚点年−5」的最近一期，历史不足 5 年才退回最早一期
    base = next((y for y in reversed(years[:-1]) if y <= ay - 5), years[0])
    span = ay - base
    cur, b = by_year[ay], by_year[base]

    # ROE 中位取「基期之后到锚点」那几年（与回测同窗）；不足 3 期不算
    roe_med = _trap_med([by_year[y].get('roe') for y in years if y > base])

    neg = pairs = 0
    for y in range(base, ay):
        p = by_year.get(y, {}).get('net')
        q = by_year.get(y + 1, {}).get('net')
        if p is not None and q is not None:
            pairs += 1
            if q < p:
                neg += 1
    mid = by_year.get(ay - 2, {}).get('net')
    accel = None
    if span >= 3 and mid is not None and b.get('net') is not None:
        a1, a2 = _g_growth(cur.get('net'), mid, 2), _g_growth(mid, b.get('net'), span - 2)
        if a1 is not None and a2 is not None:
            accel = a1 - a2
    raw = {
        'np_g5': _g_growth(cur.get('net'), b.get('net'), span),
        'rev_g5': _g_growth(cur.get('rev'), b.get('rev'), span),
        'roe_med': roe_med,
        'bps_g5': _g_growth(cur.get('bps'), b.get('bps'), span),
        'stab': float(neg) if pairs >= G_MIN_PAIRS else None,
        'accel': accel,
        'roe_trend': None if (roe_med is None or cur.get('roe') is None)
        else cur.get('roe') - roe_med,
    }
    ev, total = 0, 0.0
    for key, w, lo, hi in G_ITEMS:
        v = raw.get(key)
        if v is None:
            continue
        ev += 1
        total += w * max(0.0, min(1.0, (v - lo) / (hi - lo)))
    # 与 JS Math.round(total / ΣW * 1000) / 10 一致（Python round 为银行家舍入，不能直接用）
    return {'total': (math.floor(total / G_SUM_W * 1000 + 0.5) / 10.0) if ev else None,
            'evaluated': ev, 'missing': len(G_ITEMS) - ev, 'na': 0, 'raw': raw}


# 价值综合分：分项、权重与锚点冻结在 backend/scripts/v_validity.py 的定稿指表上（那份脚本是这条
# 轴的出厂检验，四条验收线的实测数打在它的文件头）。三条纪律与 growth_score 同形：锚点是固定绝对
# 阈值不按横截面重排；分母恒为 V_SUM_W、缺项不进分子也不重新归一；「算得出而为负」记 0 分档，
# 只有「科目取不到」才判不动。便宜那 65 分吃市值（取法见 _v_mcap：本批行情优先、退回深抓快照），
# 质量与回报那 35 分不吃。
V_ITEMS = (
    ('edge_tbv', 30, -1.5, 0.5),
    ('ep', 25, 0.0, 0.10),
    ('cash_yld', 10, 0.0, 0.05),
    ('roe_med5', 14, 0.0, 0.20),
    ('debt_rev', 10, 0.90, 0.30),      # 反向：负债率越低分越高
    ('ocfnp', 8, 0.8, 2.5),
    ('return_cash', 3, 0.0, 1.0),
)
V_SUM_W = 100.0      # Σ V_ITEMS 权重，固定分母
V_EDGE_FLOOR = -9.0  # 有形账面价值 ≤0 时 ln 无定义：记成一个必然夹到 0 分档的下界


def _v_num(v):
    """对应 JS 的 `typeof v === 'number' && isFinite(v)`。"""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _v_year(s):
    m = str(s or '')[:4]
    return int(m) if m.isdigit() and int(m) else None


def _v_mcap(q):
    """V 的市值：本批次行情优先，没给到可用市值才退回深抓快照。

    0 与负数市值不是「便宜」，是行情行坏了，与缺值同处理。stockLegacy.js 的 vMcap 同一条规则。
    """
    v = _v_num((q or {}).get('market_cap'))
    return v if (v is not None and v > 0) else None


def value_score(d):
    """对应 JS valueScore。返回 {total, evaluated, missing, na, raw}；分母恒为 V_SUM_W。

    市值取本批次行情 `batch_quote`（日更刷进 index.json 的那一笔），缺则退回深抓快照——
    V 是三条轴里唯一把市值当输入的入库分数，其它分与参考价照旧只认深抓快照那一个数。
    便宜度的分子分母同币种，比值天然免汇率，所以全程只做比值、不跨币种相加。
    """
    d = d or {}
    by_year = {}

    def slot(y):
        return by_year.setdefault(y, {})

    def put(o, k, v):
        n = _v_num(v)
        if n is not None:                     # 同年后一行覆盖前一行，缺列留空
            o[k] = n

    for r in annual_rows(d.get('indicators') or []):
        y = _v_year(r.get('报告期'))
        if not y:
            continue
        s = slot(y)
        for k, col in (('net', '净利润'), ('ded', '扣非净利润'), ('roe', '净资产收益率'),
                       ('bps', '每股净资产'), ('debt_r', '资产负债率')):
            put(s, k, r.get(col))
    for r in annual_balance_rows(d.get('balance')):
        y = _v_year(r.get('报告日'))
        if not y:
            continue
        s = slot(y)
        eq = equity_of(r)
        if eq is not None:
            s['eq'] = eq
        for k, col in (('ta', '资产总计'), ('tl', '负债合计'),
                       ('gw', '商誉'), ('intang', '无形资产')):
            put(s, k, r.get(col))
    for r in annual_balance_rows(d.get('cashflow')):
        y = _v_year(r.get('报告日'))
        if not y:
            continue
        put(slot(y), 'ocf', r.get('经营活动产生的现金流量净额'))

    years = sorted(by_year)
    if len(years) < 3:
        return {'total': None, 'evaluated': 0, 'missing': len(V_ITEMS),
                'na': 0, 'raw': {}}
    ay, win, cur = years[-1], years[-5:], by_year[years[-1]]
    mcap = _v_mcap(d.get('batch_quote'))
    if mcap is None:
        mcap = _v_mcap(d.get('snapshot'))
    # 派现年表：归属年 → 每 10 股现金红利合计，只数真给了钱的行（送股不派现不算回过钱）
    div_amt, div_seen = {}, False
    for r in (d.get('dividends') or []):
        y = _v_year(r.get('year'))
        b = _v_num(r.get('bonus_per_10'))
        if not y or y < 1990 or b is None or b <= 0:
            continue
        div_amt[y] = div_amt.get(y, 0.0) + b
        div_seen = True
    # 隐含股本 = 归母权益 ÷ 每股净资产（与陷阱分同一招，「股本」列会被送转股放大）；
    # 美股年报没有每股净资产那一行，所以它的股息率算不出（判不动），账面折扣照算。
    sh = cur['eq'] / cur['bps'] if (cur.get('eq') is not None and (cur.get('bps') or 0) > 0) else None
    tbv = (None if cur.get('eq') is None
           else cur['eq'] - (cur.get('gw') or 0) - (cur.get('intang') or 0))
    # ep 的分子改滚动 TTM（扣非优先，缺则净利）：年报口径的盈利收益率要等下一次年报才
    # 反映「年报之后中报塌方」（600866 实例：年报口径 13.7% vs TTM 口径约 3%）。关键性质：
    # 最新指标行是年报时 TTM==年报值，分数分毫不动——只有存在晚于最新年报的 interim 才分化。
    # 回测面板重构不了 TTM（40 期钳制 + 可用日，v_validity 的验收仍用年报口径），这是
    # 「判据不动、只升级输入端」的先例（见 v_validity 文件头「定稿后改的两处输入端」）。
    earn = _ttm_net_profit(d.get('indicators') or [], '扣非净利润')
    if earn is None:
        earn = _ttm_net_profit(d.get('indicators') or [], '净利润')
    raw = {}
    if mcap:
        if tbv is not None:
            raw['edge_tbv'] = V_EDGE_FLOOR if tbv <= 0 else math.log(tbv / mcap)
        if earn is not None:
            raw['ep'] = max(0.0, earn) / mcap
        if sh is not None:
            paid = sum(div_amt.get(y, 0.0) for y in range(ay - 3, ay))
            raw['cash_yld'] = (paid / 10.0) * sh / 3.0 / mcap
    raw['roe_med5'] = _trap_med([by_year[y].get('roe') for y in win])
    dr = cur.get('debt_r')
    if dr is None and cur.get('ta') and cur.get('tl') is not None:
        dr = cur['tl'] / cur['ta']            # 指标行缺该行时用负债/资产合计补
    raw['debt_rev'] = dr
    pairs = [(by_year[y].get('net'), by_year[y].get('ocf')) for y in win]
    pairs = [(n, o) for n, o in pairs if n is not None and o is not None]
    sn, so = sum(n for n, _ in pairs), sum(o for _, o in pairs)
    if len(pairs) >= 3 and sn > 0:
        raw['ocfnp'] = so / sn
    # A 股分红史全量可查，「近五年没派现」是公开事实（0 分档）；港美股只有查得派过才算得出，
    # 查不到分不清「没分」与「这侧的分红源没覆盖」，判不动。
    if d.get('market') == 'A' or div_seen:
        gave = sum(1 for y in range(ay - 5, ay) if div_amt.get(y))
        raw['return_cash'] = min(1.0, gave / 5.0)
    ev, total = 0, 0.0
    for key, w, lo, hi in V_ITEMS:
        v = raw.get(key)
        if v is None:
            continue
        ev += 1
        total += w * max(0.0, min(1.0, (v - lo) / (hi - lo)))
    # 与 JS Math.round(total / ΣW * 1000) / 10 一致（Python round 为银行家舍入，不能直接用）
    return {'total': (math.floor(total / V_SUM_W * 1000 + 0.5) / 10.0) if ev else None,
            'evaluated': ev, 'missing': len(V_ITEMS) - ev, 'na': 0, 'raw': raw}


# ---- 综合推荐分 R（0~100，门槛外的 0.6×V + 0.4×G）：对应 JS recommendScore ----
# 出厂检验在 backend/scripts/r_validity.py（A 股面板 2021~2023、事件时信息集、预登记三线全过）：
#   甲：门槛内 R_q（无价格验收版 = 0.6×V质量块 + 0.4×G）五分位 → 其后转亏率 16.0%→2.4%
#       单调（Q1−Q5 +13.6pp ≥5pp 线）、减值≥5% 26.8%→3.3% 单调（+23.5pp ≥2pp 线）、逐年 3/3 同向；
#   乙：两条腿首末差均 ≥ max(V质量块, G 单轴) − 2pp（转亏腿 13.6pp 还强于两个成分）；
#   丙：门槛剔除人群转亏率 19.3% vs 过门 5.8%（z=+24.4）、减值 19.4% vs 9.2%——剔的确实是更坏的人。
# 两条读法警戒与 V 同源：① 便宜那 65 分在面板上只能配今天市值（前视），判决书用的是无价格版，
# R 的高分是「质量+便宜的证据合计」不是收益预测（本库无历史行情，收益侧验证做不了）；
# ② 门槛外的公司没有 R（列表 `-`），那是「不过资格线」不是「得 0 分」。
R_W_VALUE = 0.6          # V 权重（G 取 1−0.6）：先验声明不拟合——价值取向给量便宜+质量的 V 六成
R_FRAUD_GATE = 40.0      # 造假分门槛（40=trap_validity 分项表里造假红旗的中带下沿，与刷池线同源量级）
R_TRAP_GATE = 20.0       # 陷阱分门槛：出厂档位「档2 单点」上沿之内（C ≤ 0.98），只放过无证据与单点


# ---- 中报恶化 dip：最新 interim 扣非同比（评分轴只吃年报，这里是年报盲区的探针） ----
# 评分轴（四派/V/G/T/造假/管理）全部只读 12-31 年报行，年报披露后的经营恶化要等下一次
# 年报才进分——600866 星湖科技 2026-09 的实例：FY2025 年报还是好年份（净利 9.8 亿、ROE 12%），
# 2026H1 扣非同比 −93%，R 却停在 82.3。财报期龄保险丝（13 个月）防的是「年报缺披露」，
# 防不了「年报正常、之后中报变脸」。dip 把这个盲区量化成「最新 interim 相对去年同期的
# 扣非（缺则净利）同比」，两处消费：列表标注（≤ INTERIM_DIP_NOTE 挂徽标）与 R 的中报
# 恶化门槛（≤ INTERIM_DIP_GATE 拦下，见 recommend_score）。
INTERIM_DIP_NOTE = -0.30     # 标注线：列名后挂「中报−xx%」徽标，只标注不折分
# 门槛线：dip ≤ −70% 拦下 R。阈值来自 scripts/dip_validity.py 的回测（A 股 interim 观测
# 78,277 条、可用日=法定期限）：−70% 档转亏 lift 7.83×（旗组 34.3% vs 其余 4.4%）、年报复证
# （FY 扣非同比 ≤−30%）lift 3.27×（74.0% vs 22.6%）、旗占观测 15.8%——采用线（转亏 lift ≥2.0、
# 复证 lift ≥1.5、旗占比 ≤25% 与既有 fraud/trap 门槛量级对齐）取最深满足者。收益侧备注：
# 旗组前瞻 1y 超额反而 +3.4pp（超跌反弹的风格效应），R 收益侧本就判 FAIL 非卖点，门槛的
# 存在理由是基本面排雷，此条披露不判决。
INTERIM_DIP_GATE = -0.70


def interim_dip_yoy(indicators):
    """最新一份 interim（Q1/H1/Q3）的扣非同比，缺则净利同比；判不动返回 None。

    只在最新一期**不是年报**时才有意义（年报已经是更新的一期时没有「比年报更新的消息」）；
    同比分母用 |基期|（与 _yoy 同一语义：基期为负时亏损收窄记改善）。同一报告期多行取
    任一行（by_date 覆盖，指标表同报告期不重复）。stockLegacy.js 的 interimDipYoy 同一条规则。
    """
    by_date = {}
    for r in indicators or []:
        p = str(r.get('报告期') or '')[:10]
        if len(p) == 10:
            by_date[p] = r
    if not by_date:
        return None
    latest = max(by_date)
    if latest[5:7] == '12':                    # 最新一期就是年报：无更新消息
        return None
    cur_row = by_date[latest]
    prev_row = by_date.get(str(int(latest[:4]) - 1) + latest[4:])
    if prev_row is None:
        return None
    cur = cur_row.get('扣非净利润')
    pv = prev_row.get('扣非净利润')
    if cur is None or pv is None:              # 扣非双期缺一 → 净利口径；再缺 → 判不动
        cur, pv = cur_row.get('净利润'), prev_row.get('净利润')
    if cur is None or pv is None or pv == 0:
        return None
    return (cur - pv) / abs(pv)


def recommend_score(v_total, g_total, fraud, trap_total, dip=None):
    """→ (R 总分 or None, 门槛状态)。

    门槛状态：'pass'（有分）/ 'fraud' / 'trap' / 'interim'（中报恶化 dip ≤ −70%，见
    INTERIM_DIP_GATE 注释）/ 组合（'+' 连接，如 'fraud+interim'）/ 'nodata'（门槛过了
    但 V/G 任一判不动，公开年报不足 3 期）。判不动的门槛输入（fraud/trap/dip 为 None）
    按「无证据」放行——港美股 trap 整列不适用、扣非缺失侧 dip 判不动，靠这条拿得到 R。
    stockLegacy.js 的 recommendScore 同一条规则。
    """
    fail = []
    if fraud is not None and fraud > R_FRAUD_GATE:
        fail.append('fraud')
    if trap_total is not None and trap_total > R_TRAP_GATE:
        fail.append('trap')
    if dip is not None and dip <= INTERIM_DIP_GATE:
        fail.append('interim')
    if fail:
        return None, '+'.join(fail)
    if v_total is None or g_total is None:
        return None, 'nodata'
    r = R_W_VALUE * v_total + (1.0 - R_W_VALUE) * g_total
    # 与 JS Math.round(r*10)/10 一致（Python round 为银行家舍入，不能直接用）
    return math.floor(r * 10 + 0.5) / 10.0, 'pass'


def management_analysis(d):
    """对应 JS managementAnalysis —— 管理层管理水平评分（0~100，越高越好）。
    融合 DEA 投入产出效率思想的 8 维透明加权：费用纪律/资产周转/资本回报/成长质量/
    营运资金/现金流质量/股东回报/治理诚信；数据不足项不计分不误伤。"""
    annual = annual_rows(d.get('indicators') or [])
    last = annual[-1] if annual else None
    last_date = str(last.get('报告期') or '')[:10] if last else None
    last_year = int(last_date[:4]) if last_date else None
    inc_list = sorted(d.get('income') or [], key=lambda r: str(r.get('报告日') or ''))
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    cf_list = sorted(d.get('cashflow') or [], key=lambda r: str(r.get('报告日') or ''))
    last_inc = sheet_row_by_date(inc_list, last_date) if last_date else None
    last_ba = sheet_row_by_date(ba_list, last_date) if last_date else None

    if last:
        rev = last.get('营业总收入')
        eps = last.get('基本每股收益')
    else:
        rev = last_inc.get('营业总收入') if last_inc else None
        eps = None
    # 资本回报改「近 5 年披露 ROE 中位，不足 3 期判不动」：单年披露值在薄/负权益处会
    # 炸到 ±几千 pp（见 value_scores 处同款注释，巴菲特项为此早已改中位），旧实现
    # 52 家单年 >100% 的公司直接拿满 20/20。逐年先取「净资产收益率」、缺则「-摊薄」。
    roe_vals = []
    for r in annual[-5:]:
        v = r.get('净资产收益率')
        if v is None:
            v = r.get('净资产收益率-摊薄')
        if v is not None:
            roe_vals.append(v)
    roe = None
    if len(roe_vals) >= 3:
        sv = sorted(roe_vals)
        mid_r = len(sv) // 2
        roe = sv[mid_r] if len(sv) % 2 else (sv[mid_r - 1] + sv[mid_r]) / 2.0
    sell_exp = last_inc.get('销售费用') if last_inc else None
    adm_exp = last_inc.get('管理费用') if last_inc else None
    fin_exp = last_inc.get('财务费用') if last_inc else None
    assets = last_ba.get('资产总计') if last_ba else None
    ar = ar_of(last_ba)
    inv = last_ba.get('存货') if last_ba else None

    # 1. 三费率：销售/管理费用任一存在则缺失项按 0 计（与 JS (x||0) 一致）；
    # 三费科目全缺（港股/美股报表口径）时回退替代科目近似计算：美股“营业费用”、港股“销售及分销费用”。
    # 只有财务费用时不算三费率：财务费用为净收益（利息收入>支出）会把费率压成负数，
    # 而本项“越低越好”，负值直接拿满 15 分（实测友邦保险 fee_ratio=-0.0031 → 15/15），
    # 等于用一个与费用纪律无关的科目发满分。此时按三费全缺处理，交给替代科目兜底。
    fees = [sell_exp, adm_exp, fin_exp]
    has_opex = sell_exp is not None or adm_exp is not None
    fee_sum = sum(f for f in fees if f is not None) if has_opex else None
    if fee_sum is None and last_inc is not None:
        fee_sum = last_inc.get('营业费用') if last_inc.get('营业费用') is not None else last_inc.get('销售及分销费用')
    fee_ratio = fee_sum / rev if (fee_sum is not None and rev is not None and rev > 0) else None
    # 2. 总资产周转率 = 营收 ÷ 总资产
    turnover = rev / assets if (rev is not None and assets is not None and assets > 0) else None
    # 4. 营收约 5 年 CAGR：取不晚于 last_year-5 的最近年报作基期，缺则用最早年报（跳过末期本身）
    rev_cagr = None
    if len(annual) >= 2 and last_year is not None:
        base = None
        for r in reversed(annual[:-1]):
            yy = int(str(r.get('报告期') or '')[:4])
            if yy <= last_year - 5:
                base = r
                break
        if base is None:
            base = annual[0]
        span = last_year - int(str(base.get('报告期') or '')[:4])
        if span > 0:
            rev_cagr = cagr(rev, base.get('营业总收入'), span)
    # 5. 营运资金占用（应收＋存货）÷ 营收
    wc = (ar + inv) / rev if (ar is not None and inv is not None and rev is not None and rev > 0) else None
    # 6. 近 5 年累计净现比（累计经营现金流 ÷ 累计净利润）
    sum_net, sum_ocf, hit = 0.0, 0.0, False
    for r in annual[-5:]:
        cf = sheet_row_by_date(cf_list, str(r.get('报告期') or '')[:10])
        n = r.get('净利润')
        o = cf.get('经营活动产生的现金流量净额') if cf else None
        if n is not None and o is not None:
            sum_net += n
            sum_ocf += o
            hit = True
    cash_ratio = sum_ocf / sum_net if (hit and sum_net > 0) else None
    # 7. 现金分红率 = 最近归属年每股派现合计 ÷ 最近年报每股收益；>150% 视为口径不可比置空。
    # 与 value_analysis 的 payout 同一口径（归属年合计）：旧实现取「最近一条记录」，
    # 单次中期分红会把分红率砍到几分之一，同名两口径在详情页对不上
    payout = None
    per_share_y = per_share_div(d.get('dividends') or [], last_year) if last_year is not None else None
    if per_share_y is not None and eps is not None and eps > 0:
        payout = per_share_y / eps
        if payout > 1.5:
            payout = None
    # 8. 治理诚信：造假风险分反向（越低越诚信）；异常只丢该维度不影响其余 7 项（与 JS try/catch 一致）
    try:
        fraud = fraud_analysis(d)
    except Exception:                                       # noqa: BLE001
        fraud = None

    scores = [
        lerp_score(fee_ratio, 0.10, 0.30, 15, 0),   # 费用纪律，15 分（越低越好）
        lerp_score(turnover, 0.2, 1.0, 0, 10),      # 资产周转，10 分（越高越好）
        lerp_score(roe, 0, 0.15, 0, 20),            # 资本回报，20 分（越高越好）
        lerp_score(rev_cagr, 0, 0.10, 0, 10),       # 成长质量，10 分（越高越好）
        lerp_score(wc, 0.15, 0.45, 10, 0),          # 营运资金占用，10 分（越低越好）
        lerp_score(cash_ratio, 0, 1.0, 0, 15),      # 现金流质量，15 分（越高越好）
        lerp_score(payout, 0, 0.50, 0, 10),         # 股东回报，10 分（越高越好）
        None if fraud is None else lerp_score(fraud, 0, 100, 10, 0),  # 治理诚信，10 分（造假分越低越好）
    ]
    # 归一化：本分越高越好，缺项若只是少几笔加分，数据不全的公司就被系统性压低
    total = _weighted_total(list(zip(scores, (15, 10, 20, 10, 10, 15, 10, 10))))
    if total is None:
        return None
    # 与 JS Math.round(total*10)/10 一致（Python round 为银行家舍入，不能直接用）
    return math.floor(total * 10 + 0.5) / 10.0


def _capex_prev(annual_cf, row, key):
    """row 之前最多 3 个年报的资本开支（按 row 在年报序列中的实际位置开窗）。
    不能硬切 annual_cf[-4:-1]（默认现金流表末年即当年，报表错位就取错窗口），
    也不能用 list.index（row 不在序列中时返回 -1，切片退化成“全部历史除最后一行”）"""
    ci = -1
    if row is not None:
        for i, r in enumerate(annual_cf):
            if r is row:
                ci = i
                break
    if ci < 0:
        return []
    return [r.get(key) for r in annual_cf[max(0, ci - 3):ci] if r.get(key) is not None]


def cycle_analysis(d):
    """对应 JS cycleAnalysis —— 周期性行业判定 + 周期位置评分（0~100，越低越接近底部）。
    阶段一：周期强度（净利变异系数 40 + 深度下滑频率 35 + 毛利率波动 25），≥ 40 判为周期性；
    阶段二：仅周期性公司打周期位置分；非周期性返回 {'cyclical': False, 'total': None}。"""
    annual = annual_rows(d.get('indicators') or [])
    cf_list = sorted(d.get('cashflow') or [], key=lambda r: str(r.get('报告日') or ''))
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    last = annual[-1] if annual else None
    last_date = str(last.get('报告期') or '')[:10] if last else None

    # ---- 阶段一：周期强度判定（样本标准差，窗口取近 8 年年报）----
    w8 = annual[-8:]
    nets = [r.get('净利润') for r in w8 if r.get('净利润') is not None]
    gms = [r.get('销售毛利率') for r in w8 if r.get('销售毛利率') is not None]

    def sd(arr):
        if len(arr) < 2:
            return None
        m = sum(arr) / len(arr)
        v = sum((x - m) ** 2 for x in arr) / (len(arr) - 1)
        return math.sqrt(v)

    # 1a 净利变异系数 = 标准差 ÷ |均值|（均值取绝对值防近零放大；全亏取各年绝对值均值）
    cv_net = None
    if len(nets) >= 3:
        denom = abs(sum(nets) / len(nets))
        if denom == 0:
            denom = sum(abs(x) for x in nets) / len(nets)
        if denom > 0:
            cv_net = sd(nets) / denom
    # 1b 利润深度下滑频率：年度净利同比 ≤ -30% 的年数（同比自算，与报表口径一致）。
    # 窗口与 1a/1c 同为近 8 年（函数头声明的口径）：旧实现数全史，上市越久攒够 2 次
    # 深跌的机会越多——实测 242 家的「周期性」标签因此翻转（全数偏向更周期）。
    # 此处只在基期为正时计算：“下滑 30%”对盈利基数才有意义；若把亏损扩大也算进来，
    # 尚未盈利的生物医药/新经济公司会被误判为周期性行业（实测 166 家误判）。
    # 阶段二“利润动能”用 _yoy（基期为负按 |基期|）——那里问的是“是否在离开底部”，亏损收窄正是信号。
    drops, hit_drop = 0, False
    for ci in range(max(1, len(annual) - 8), len(annual)):
        n_cur, n_pre = annual[ci].get('净利润'), annual[ci - 1].get('净利润')
        yoy = (n_cur / n_pre - 1.0) if (n_cur is not None and n_pre is not None and n_pre > 0) else None
        if yoy is not None:
            hit_drop = True
            if yoy <= -0.3:
                drops += 1
    # 1c 毛利率波动 = 年度毛利率标准差（价格驱动型周期行业毛利率大起大落）
    gm_sd = sd(gms) if len(gms) >= 3 else None

    c_scores = [
        lerp_score(cv_net, 0.3, 1.2, 0, 40),                        # 净利变异系数，40 分
        lerp_score(drops, 0, 2, 0, 35) if hit_drop else None,       # 深度下滑年数，35 分
        lerp_score(gm_sd, 0.03, 0.10, 0, 25),                       # 毛利率波动，25 分
    ]
    c_avail = [s for s in c_scores if s is not None]
    cyc = min(100.0, sum(c_avail)) if c_avail else None
    cyc = None if cyc is None else math.floor(cyc * 10 + 0.5) / 10.0
    cyclical = cyc is not None and cyc >= 40
    if not cyclical:
        return {'cyclical': False, 'cyclicalScore': cyc, 'total': None}

    # ---- 阶段二：周期位置评分（分数越低越接近周期底部）----
    def pct_of(v, arr):
        vs = [x for x in arr if x is not None]
        if v is None or len(vs) < 2:
            return None
        mn, mx = min(vs), max(vs)
        return 0.5 if mx == mn else (v - mn) / (mx - mn)

    net_last = last.get('净利润') if last else None
    rev_last = last.get('营业总收入') if last else None
    gm_last = last.get('销售毛利率') if last else None
    net_pct = pct_of(net_last, nets)
    gm_pct = pct_of(gm_last, gms)
    rev_pct = pct_of(rev_last, [r.get('营业总收入') for r in w8])
    # 利润动能必须用最新年报自身同比：上年净利缺失/为 0 时置空，
    # 不能回退到历史同比序列末位（那是数年前的值，会被当成最新动能打分）
    net_yoy = _yoy(net_last, annual[-2].get('净利润')) if len(annual) >= 2 else None
    # 最新年报净现比（底部常伴随现金流恶化）
    last_cf = sheet_row_by_date(cf_list, last_date) if last_date else None
    ocf_last = last_cf.get('经营活动产生的现金流量净额') if last_cf else None
    ncr = ocf_last / net_last if (net_last is not None and ocf_last is not None and net_last > 0) else None
    # 存货同比（去库存 → 接近底部）
    last_ba = sheet_row_by_date(ba_list, last_date) if last_date else None
    prev_date = str(annual[-2].get('报告期') or '')[:10] if len(annual) >= 2 else None
    prev_ba = sheet_row_by_date(ba_list, prev_date) if prev_date else None
    inv_now = last_ba.get('存货') if last_ba else None
    inv_prev = prev_ba.get('存货') if prev_ba else None
    inv_grow = (inv_now / inv_prev - 1.0) if (inv_now is not None and inv_prev is not None and inv_prev > 0) else None
    # 资本开支强度 = 当年购建支出 ÷ 近 3 年均值（收缩 → 供给出清接近底部）
    CAPEX_K = '购建固定资产、无形资产和其他长期资产所支付的现金'
    annual_cf = [r for r in cf_list if str(r.get('报告日') or '')[5:] == '12-31']
    capex_now = last_cf.get(CAPEX_K) if last_cf else None
    capex_prev = _capex_prev(annual_cf, last_cf, CAPEX_K)
    capex_ratio = None
    if capex_now is not None and len(capex_prev) >= 2:
        capex_avg = sum(capex_prev) / len(capex_prev)
        if capex_avg > 0:
            capex_ratio = capex_now / capex_avg
    # 最新单季营收环比（仍在回落 → 未到底；环比回升 → 开始离开底部）
    q_rows = sorted([r for r in (d.get('indicators') or [])
                     if str(r.get('报告期') or '')[5:] != '12-31'],
                    key=lambda r: str(r.get('报告期') or ''))
    qrev = [r.get('营业总收入_单季') for r in q_rows]
    qoq = None
    if len(qrev) >= 2 and qrev[-2] is not None and qrev[-2] > 0 and qrev[-1] is not None:
        qoq = qrev[-1] / qrev[-2] - 1.0

    scores = [
        None if net_pct is None else net_pct * 25,                 # 利润位置，25 分（越低越近底部）
        lerp_score(net_yoy, -0.50, 0.30, 0, 15),                   # 利润动能，15 分（深负=底部）
        None if gm_pct is None else gm_pct * 15,                   # 毛利率位置，15 分（越低越近底部）
        None if rev_pct is None else rev_pct * 10,                 # 营收位置，10 分（越低越近底部）
        lerp_score(ncr, 0, 1.2, 0, 10),                            # 现金流压力，10 分（≤ 0 底部）
        lerp_score(inv_grow, -0.10, 0.20, 0, 10),                  # 库存周期，10 分（去库存→底部）
        lerp_score(capex_ratio, 0.7, 1.3, 0, 10),                  # 资本开支周期，10 分（收缩→出清）
        lerp_score(qoq, -0.10, 0.05, 0, 10),                       # 单季环比，10 分（仍在探底→低分）
    ]
    # 归一化：本分越低越接近底部，缺项若只是少扣一笔，数据不全的公司就被判成“更接近底部”。
    # 顺带去掉旧代码的 min(100, sum) 硬夹 —— 8 维权重合计 105，归一化后天然不越界
    total = _weighted_total(list(zip(scores, CYCLE_POS_W)))
    if total is None:
        return {'cyclical': True, 'cyclicalScore': cyc, 'total': None}
    # 与 JS Math.round(total*10)/10 一致（Python round 为银行家舍入，不能直接用）
    return {'cyclical': True, 'cyclicalScore': cyc,
            'total': math.floor(total * 10 + 0.5) / 10.0}


def cycle_history(d):
    """对应 JS cycleHistory —— 逐年回溯周期位置分（趋势图/趋势状态共用）。
    每个年报年以该年为窗口末尾取最近 8 年年报，用与 cycle_analysis 阶段二相同的 8 维逻辑；
    单季环比逐年参与：历史年用该年自身单季营收环比，末年用全局最新单季环比（与当期总分口径对齐），
    故各年均为满 8 维、同口径可比。"""
    annual = annual_rows(d.get('indicators') or [])
    cf_list = sorted(d.get('cashflow') or [], key=lambda r: str(r.get('报告日') or ''))
    ba_list = sorted(d.get('balance') or [], key=lambda r: str(r.get('报告日') or ''))
    CAPEX_K = '购建固定资产、无形资产和其他长期资产所支付的现金'
    annual_cf = [r for r in cf_list if str(r.get('报告日') or '')[5:] == '12-31']
    q_rows = sorted([r for r in (d.get('indicators') or [])
                     if str(r.get('报告期') or '')[5:] != '12-31'],
                    key=lambda r: str(r.get('报告期') or ''))
    qrev = [r.get('营业总收入_单季') for r in q_rows]
    qoq = None
    if len(qrev) >= 2 and qrev[-2] is not None and qrev[-2] > 0 and qrev[-1] is not None:
        qoq = qrev[-1] / qrev[-2] - 1.0
    # 按年归档单季营收（升序，保留 None），供逐年环比：各年取该年最后两个单季（Q3/Q2）
    interim_by_year = {}
    for r in q_rows:
        yr = str(r.get('报告期') or '')[:4]
        interim_by_year.setdefault(yr, []).append(r.get('营业总收入_单季'))

    def year_qoq(yr):
        vals = interim_by_year.get(str(yr), [])
        if len(vals) >= 2 and vals[-2] is not None and vals[-2] > 0 and vals[-1] is not None:
            return vals[-1] / vals[-2] - 1.0
        return None

    def pct_of(v, arr):
        vs = [x for x in arr if x is not None]
        if v is None or len(vs) < 2:
            return None
        mn, mx = min(vs), max(vs)
        return 0.5 if mx == mn else (v - mn) / (mx - mn)

    out = []
    for i in range(2, len(annual)):  # 需至少 3 年窗口且同比可算（i≥2）
        row = annual[i]
        year = int(str(row.get('报告期') or '')[:4])
        win = annual[max(0, i - 7):i + 1]
        net = row.get('净利润')
        prev = annual[i - 1].get('净利润')
        yoy = _yoy(net, prev)
        date = str(row.get('报告期') or '')[:10]
        cf = sheet_row_by_date(cf_list, date)
        ba = sheet_row_by_date(ba_list, date)
        ba_prev = sheet_row_by_date(ba_list, str(annual[i - 1].get('报告期') or '')[:10])
        ocf = cf.get('经营活动产生的现金流量净额') if cf else None
        ncr = ocf / net if (net is not None and ocf is not None and net > 0) else None
        inv_now = ba.get('存货') if ba else None
        inv_prev = ba_prev.get('存货') if ba_prev else None
        inv_grow = (inv_now / inv_prev - 1.0) if (inv_now is not None and inv_prev is not None and inv_prev > 0) else None
        capex_now = cf.get(CAPEX_K) if cf else None
        capex_prev = _capex_prev(annual_cf, cf, CAPEX_K)
        capex_ratio = None
        if capex_now is not None and len(capex_prev) >= 2:
            capex_avg = sum(capex_prev) / len(capex_prev)
            if capex_avg > 0:
                capex_ratio = capex_now / capex_avg
        # 单季环比：末年用全局最新单季环比（与当期总分一致），历史年用该年自身单季环比（满 8 维）
        qoq_i = qoq if i == len(annual) - 1 else year_qoq(year)
        scores = [
            None if pct_of(net, [r.get('净利润') for r in win]) is None
            else pct_of(net, [r.get('净利润') for r in win]) * 25,
            lerp_score(yoy, -0.50, 0.30, 0, 15),
            None if pct_of(row.get('销售毛利率'), [r.get('销售毛利率') for r in win]) is None
            else pct_of(row.get('销售毛利率'), [r.get('销售毛利率') for r in win]) * 15,
            None if pct_of(row.get('营业总收入'), [r.get('营业总收入') for r in win]) is None
            else pct_of(row.get('营业总收入'), [r.get('营业总收入') for r in win]) * 10,
            lerp_score(ncr, 0, 1.2, 0, 10),
            lerp_score(inv_grow, -0.10, 0.20, 0, 10),
            lerp_score(capex_ratio, 0.7, 1.3, 0, 10),
            lerp_score(qoq_i, -0.10, 0.05, 0, 10),
        ]
        # 与阶段二同口径归一化：各年可用维度数不同，不归一就没法逐年比（cycle_trend 正是逐年比）
        total = _weighted_total(list(zip(scores, CYCLE_POS_W)))
        sc = math.floor(total * 10 + 0.5) / 10.0 if total is not None else None
        out.append({'year': year, 'score': sc})
    return out


def cycle_trend(hist):
    """对应 JS cycleTrendOf —— 趋势状态（最新年相对上一年）：
    rev=反转（上一年还在底部区≤30 且明显回升）/ up=上行 / flat=筑底（低位≤40横盘）/ down=下行"""
    h = [x for x in (hist or []) if x.get('score') is not None]
    if not h:
        return None
    cur = h[-1]
    # 必须取“上一年”而非“上一条有分的年”：中间年缺分时两者跨年，会把两年前的分当成上一年
    prev = next((x for x in reversed(h[:-1]) if x.get('year') == cur['year'] - 1), None)
    if prev is None:
        return None
    d1 = cur['score'] - prev['score']
    if d1 > 5:
        return 'rev' if prev['score'] <= 30 else 'up'
    if d1 < -5:
        return 'down'
    if cur['score'] <= 40:
        return 'flat'
    return 'up' if d1 >= 0 else 'down'


def compute_scores(company, now=None):
    """抓取后调用：返回四大流派总分 + 价格参考 + 造假风险分 + 管理分 + 周期分/趋势 dict（供 index.json 直接使用）"""
    va = value_analysis(company, now)
    scores = value_scores(company, va)
    scores['priceRefs'] = price_references(company, va)
    scores['fraud'] = fraud_analysis(company)
    scores['mgmt'] = management_analysis(company)
    ca = cycle_analysis(company)
    scores['cycle'] = ca['total']
    scores['cyclical'] = ca['cyclical']
    tp = trap_score(company)
    scores['trap'] = tp['total']
    # C = Σ ln(lift)×亮灯，是分数未经归一的原始证据权重合计；档位表按 C 查而不是按分数查
    # （分数是 C/ΣW 舍入到一位小数，边界上会串档），存下来也便于回库核对。
    scores['trapC'] = tp['c']
    # 可评估项数必须与分数并列：固定分母下缺项只压低分数，约三成公司一项证据都没亮，
    # 光看 0 分会被读成「干净」。整列不适用（非 A 股）给 None 而不是 0——那是两件事。
    scores['trapEval'] = None if tp['na'] else tp['evaluated']
    # 成长综合分：七项各自夹到 0~1 后按先验权重折成 0~100，分母固定，故可评估项数必须并列
    gr = growth_score(company)
    scores['growth'] = gr['total']
    scores['growthEval'] = gr['evaluated']
    # 价值综合分：同一套固定分母纪律，七项里便宜那 65 分吃市值（本批行情优先），两笔都缺才整块判不动
    vv = value_score(company)
    scores['value'] = vv['total']
    scores['valueEval'] = vv['evaluated']
    # 中报恶化 dip：年报盲区探针（标注 + R 门槛共用），口径见 interim_dip_yoy 文件头。
    # 先算再喂 R：门槛是它存在的第一理由。
    scores['interimDip'] = interim_dip_yoy(company.get('indicators'))
    # 综合推荐分 R：门槛外的 V/G 合成（见 recommend_score 文件头）
    scores['recommend'], scores['recommendGate'] = recommend_score(
        scores['value'], scores['growth'], scores['fraud'], scores['trap'],
        scores['interimDip'])
    # 趋势状态仅周期性公司（非周期不打分不显示趋势）
    scores['cycleTrend'] = cycle_trend(cycle_history(company)) if ca['total'] is not None else None
    # 评分基准报告期（最新年报期）：入库成 score_daily.report_date。
    # 原先只有净现金代入明细里顺带带的 report 可用，算不出净现金的公司就回落成跑数日
    # （实测 2026-09-12 那轮 5095/6939 行如此），导致报告龄与「这批分用的哪一期财报」都无法回答。
    scores['reportDate'] = va.get('annualDate')
    return scores
