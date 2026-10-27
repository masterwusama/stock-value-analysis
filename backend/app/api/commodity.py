# -*- coding: utf-8 -*-
"""大宗商品供需模块 API(一期黄金 2026-10-27;二期白银 2026-10-27 同款)。

数据链:collector/commodity-{gold,silver}/scripts/fetch_*.py(免费源,每天
06:05/06:15)→ commodity-*/data/*.json → import_legacy --only-{gold,silver} →
cmd_indicator/cmd_value 两表(commodity 列分行)→ 本接口读库。
与 agro EDB 分表的原因见 CmdIndicator 模型注与 import_legacy._import_cmd_series 头注。

读时合成(不在采集器算,改口径不用重刷数据):
    - 每序列 KPI:最新值/近1周·4周·1年变动/52周高低/52周分位——前端卡片直接渲染;
    - 新鲜度按频率分档(日>7天/周>21天/月>70天报陈旧),序列缺测不算失败,
      采集器降级留下的旧点照常展示(学习 etf 页红条教训,新鲜度透明给用户);
    - 白话释义 desc + 页面导读 guide 按商品注入(文案在 _DESC/_GUIDE)。
设计文档:docs/设计-大宗商品供需模块-{黄金,白银}.md。
"""
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import CmdIndicator, CmdValue

router = APIRouter(prefix="/api/commodity", tags=["commodity"])

_FREQ_CN = {"day": "日", "week": "周", "month": "月", "year": "年"}
# 新鲜度红线(天):按频率分档——周度 CFTC 周五发布,月度储备滞后 2 个月是常态
_STALE_DAYS = {"day": 7, "week": 21, "month": 70, "year": 400}

# 白话释义:每序列一句"这是什么+怎么读"。读时注入(不动库),改文案无需重刷数据;
# 页面导读与指标卡直接渲染,是"清晰易懂"层(2026-10-27)的核心交付。按商品分 dict。
_DESC = {
    "gold": {
        "XAU_USD": "国际金价基准。实际利率降、美元走弱时倾向上涨。",
        "GC_MAIN": "纽约期货金价,与伦敦现货的价差反映跨市场套利与交割摩擦。",
        "SGE_AU9999": "国内现货金价基准(元/克)。对国际价有溢价属常态(含税与需求)。",
        "SHGOLD_PM": "上海金下午定盘价,国内 ETF 与机构合同的定价锚。",
        "AU0_CLOSE": "沪金期货价。相对 SGE 现货升水放大=套利与投机情绪升温。",
        "GOLD_SILVER": "一盎司金换几盎司银。比值高于 80 历史上偏「银便宜」,低于 50 偏「银贵」。",
        "SGE_PREMIUM": "内盘对国际价的溢价率,中国实物需求温度计。旺季冲高、淡季回落。",
        "USDCNY_FIX": "人民币中间价。人民币贬值直接抬升以元计价的国内金价。",
        "SPDR_TON": "全球最大黄金 ETF 持仓。持续增仓=西方金融资金流入金市。",
        "SPDR_IMPLIED": "总价值÷持仓盎司,与盘面价差应<0.5%,差大说明数据源异常。",
        "COMEX_INV": "纽约可交割库存。库存下降+价格上行=现货趋紧/挤仓预警。",
        "CFTC_COMM_NET": "产业套保盘净头寸,习惯高抛低吸,与价格方向常相反。",
        "CFTC_NC_NET": "基金投机盘净头寸。极端净多=仓位拥挤,历史上常是阶段顶部。",
        "LONDON_VAULT": "伦敦金库总库存。趋势性下降=实物市场收紧。",
        "AU0_OI": "沪金持仓量,国内参与热度。价涨且仓增=趋势更健康。",
        "CN_GOLD_WAN": "央行官方黄金储备,月度。连续增持=结构性买盘(2022 年来主要力量)。",
        "DFII10": "持有黄金的机会成本,与金价负相关——最核心的单一定价因子。",
        "T10YIE": "市场通胀预期。上行利好黄金,「通胀升+实际利率降」组合最利多。",
        "US10Y": "名义利率=实际利率+通胀预期,快变量,拆开看才有方向。",
        "DTWEXBGS": "贸易加权美元指数。美元走弱降低非美买家成本,利多金价。",
        "VIXCLS": "美股恐慌指数。急升时避险资金流向黄金。",
        "GPR": "地缘政治风险指数(月度)。升温=黄金避险溢价。",
    },
    "silver": {
        "XAG_USD": "国际银价基准。货币+工业双属性,波动约为黄金 2 倍。",
        "SI_MAIN": "纽约白银期货,机构套保与投机主战场,与伦敦现货价差极小。",
        "AG0_CLOSE": "国内白银期货价(元/千克)。相对国际价升水放大=内盘投机情绪升温。",
        "AGT_CLOSE": "SGE 白银延期,国内现货风向标(元/千克)。",
        "AG_GSR": "一盎司金换几盎司银,白银第一估值锚。>80 偏「银便宜」、<50 偏「银贵」。",
        "AG_PREMIUM": "内盘 Ag(T+D) 对国际银价的溢价率(含交易费率),中国需求温度计。",
        "AG0_OI": "沪银持仓量,国内参与热度。价涨且仓增=趋势更健康。",
        "SLV_TON": "全球最大白银 ETF 持仓。增仓=西方资金流入;近年减持常被用于弥合供需缺口。",
        "SLV_IMPLIED": "总价值÷持仓盎司,与盘面价差应<1%,差大提示数据源异常。",
        "COMEX_AG_INV": "纽约可交割白银库存。库存降+价格涨=挤仓预警(2021 年挤仓前正是此形态)。",
        "CFTC_AG_COMM": "产业套保盘净头寸,与价格常反向(高抛低吸)。",
        "CFTC_AG_NC": "基金投机盘净头寸。极端净多=拥挤,历史上常是阶段顶部。",
        "LONDON_VAULT_AG": "伦敦金库白银库存(月度),全球实物白银存量水位。",
        "PS_MAIN": "多晶硅期货价(元/吨),光伏排产温度计——硅价涨常预示开工强,利多银浆需求。",
        "PV_ETF": "光伏 ETF 净值,光伏产业景气代理(白银最大工业需求)。",
        "ZN0_CLOSE": "沪锌主力。锌矿是白银伴生主力,锌价高→锌矿扩产→伴生银供给增(滞后)。",
        "PB0_CLOSE": "沪铅主力。铅矿同为白银伴生来源,逻辑同沪锌。",
        "AG_DFII10": "实际利率,白银金融属性端的核心定价因子(与金同源,负相关)。",
        "AG_DTWEXBGS": "美元宽口径指数。美元弱利多白银(与黄金同向)。",
        "WSS_BALANCE": "年度市场平衡=供给−需求(吨),负值=短缺。2021 起连续第 6 年短缺,但被 ETF 流出与库存弥合。",
        "WSS_ETP": "全球白银 ETF 年度净投资(吨)。近年大流入在弥合实物缺口。",
        "WSS_BAL_EX_ETP": "剔除 ETF 买入后的真实缺口——比表观平衡更紧,衡量自由市场紧张度。",
        "WSS_SUPPLY": "白银年度总供给=矿产+回收+净对冲(吨)。Silver Institute 口径,次年 4 月发布。",
        "WSS_MINE": "矿产银年产量(吨)。约 70% 是金/铜/铅锌伴生矿——供给对银价不敏感(刚性)。",
        "WSS_RECYCLE": "回收银年供给(吨)。再生银随银价升而增,是唯一有价格弹性的供给。",
        "WSS_DEMAND": "白银年度总需求(吨)=工业+投资+珠宝+银器。",
        "WSS_INDUSTRIAL": "工业需求合计(吨),约占总需求 59%——白银制造业属性的来源。",
        "WSS_PV": "光伏用银(吨),白银最大单一工业需求。2025 见顶回落,银浆单耗下降开始覆盖装机增长。",
        "WSS_COIN": "金条金币零售投资需求(吨),避险与滞胀情绪温度计。",
        "WSS_JEWELRY": "珠宝需求(吨),价格敏感型消费,银价高企时受抑。",
    },
    "copper": {
        "CAD_MAIN": "国际铜价基准(LME 三月,美元/吨)。全球经济温度计,中国占全球消费约 55%。",
        "HG_MAIN": "纽约铜期货(美分/磅),与 LME 高度联动,跨市场套利窗口偶有挤仓行情。",
        "CU0_CLOSE": "国内铜期货价(元/吨)。相对 LME 溢价放大=中国需求走强(进口亏损)。",
        "CU_GOLD": "每盎司金的美元价能买几吨铜。比值越高=市场越押周期与增长;走低=避险主导。",
        "CU0_OI": "沪铜持仓量,国内产业资金参与热度。价涨且仓增=趋势更健康。",
        "SHFE_LME": "沪伦比=沪铜÷(LME×汇率)。>1=内盘溢价(中国需求强/进口亏损),持续>1 常见于挤行情。",
        "CN_PMI": "中国制造业 PMI(月度)。50 是荣枯线,铜价与中国制造业景气高度相关。",
        "CFTC_CU_NC": "基金投机盘净头寸。极端净多=仓位拥挤,历史上常是阶段顶部。",
        "SHFE_CU_INV": "沪铜库存(仅滚动近 3 月)。去库+价格上行=国内供需转紧。",
        "CU_DTWEXBGS": "美元宽口径指数。铜以美元计价,美元弱降低非美买家成本,利多铜价。",
    },
    "aluminum": {
        "AHD_MAIN": "国际铝价基准(LME 三月,美元/吨)。中国占全球产量约 60%,沪铝话语权高。",
        "AL0_CLOSE": "国内铝期货价(元/吨)。相对 LME 溢价放大=中国供需转紧(进口亏损)。",
        "AL_SHFE_LME": "沪伦比=沪铝÷(LME×汇率)。>1=内盘溢价,持续高位常见于国内供给约束行情。",
        "AL0_OI": "沪铝持仓量,产业资金参与热度。价涨且仓增=趋势更健康。",
        "AO0_CLOSE": "氧化铝期货价(元/吨),电解铝的直接原料(1.93 吨产 1 吨)。涨价=冶炼利润被压缩。",
        "AO0_OI": "氧化铝持仓量,原料端资金热度。急涨急跌常伴随挤仓(2024 年氧化铝行情)。",
        "AL_PMI": "中国制造业 PMI(月度)。50 是荣枯线,铝下游(型材/板带箔)随制造业景气波动。",
        "SHFE_AL_INV": "沪铝库存(仅滚动近 3 月)。去库+价格上行=国内供需转紧。",
        "AL_DTWEXBGS": "美元宽口径指数。铝以美元计价,美元弱利多铝价。",
    },
    "lithium": {
        "LC0_CLOSE": "碳酸锂期货价(广期所,元/吨)。锂电需求占 80%+,供给周期(澳矿/盐湖/云母)是主矛盾。",
        "EV_ETF": "新能源车 ETF,终端需求代理。锂价常态与终端景气同向。",
        "BAT_ETF": "电池 ETF,中游排产景气代理——电池厂开工直接决定碳酸锂采购节奏。",
        "LC_INV": "广期所注册仓单(滚动近 3 月)。仓单激增/注销是挤仓行情的核心变量。",
        "LC0_OI": "碳酸锂持仓量,锂矿/电池厂套保与投机热度。",
        "LI_PMI": "中国制造业 PMI(月度)。50 是荣枯线。",
        "LI_DTWEXBGS": "美元宽口径指数。锂精矿美元计价,美元强=进口成本抬升。",
    },
    "crudeoil": {
        "CL_MAIN": "WTI 原油期货(美元/桶),美国基准。全球流量大宗之王,1996 年起 30 年历史。",
        "OIL_MAIN": "布伦特原油(美元/桶),海运基准,OPEC+ 定价锚,全球约 2/3 实货挂靠。",
        "BRENT_WTI": "布伦特减 WTI(美元/桶)。价差走阔=美国以外市场更紧张(制裁/海运风险),收窄=美国紧张。",
        "SC0_CLOSE": "上海原油期货(元/桶)。亚洲需求定价参考,相对布伦特溢价反映中国采购节奏。",
        "SC0_OI": "沪原油持仓量,国内产业资金参与热度。",
        "CRACK_HO": "取暖油/原油比价。馏分油炼制利润的相对水平——比值走高=成品油强于原料(炼厂需求旺/柴油紧张)。",
        "HO_MAIN": "取暖油期货(馏分油,含柴油)。冬季取暖与全球柴油需求的温度计。",
        "NG_MAIN": "天然气期货。发电/取暖替代品,与原油存在替代弹性(油气比价)。",
        "CFTC_OIL_COMM": "产业套保盘净头寸(周度,1986→),与价格常反向——产油国高抛低吸。",
        "OIL_DTWEXBGS": "美元宽口径指数。原油美元计价,美元弱利多油价(经典负相关)。",
    },
    "ferrous": {
        "RB0_CLOSE": "螺纹钢期货(元/吨),建筑业长材温度计。地产/基建需求的直接定价。",
        "I0_CLOSE": "铁矿石期货(元/吨)。澳洲/巴西双寡头供给 vs 中国需求,黑色系的成本大头(1.6 吨/吨钢)。",
        "JM0_CLOSE": "焦煤期货(元/吨)。山西安监与澳煤进口是供给变量,吨钢耗 0.5 吨。",
        "MILL_MARGIN": "钢厂盘面利润=螺纹−1.6×铁矿−0.5×焦煤。>0 即期有冶炼利润;持续>500 刺激复产、<0 触发减产。",
        "HC_RB_SPREAD": "卷螺差=热卷−螺纹。走阔=制造业强于建筑业(板材需求);收窄=地产基建相对占优。",
        "HC0_CLOSE": "热卷期货(元/吨),制造业板材(汽车/家电/机械)。",
        "RB0_OI": "螺纹持仓量,产业资金参与热度。",
        "I0_OI": "铁矿持仓量,钢厂套保与贸易商投机。",
        "JM0_OI": "焦煤持仓量,煤矿套保热度。",
        "FE_PMI": "中国制造业 PMI(月度)。50 是荣枯线,黑色终端需求的总景气。",
    },
    "agri": {
        "LH0_CLOSE": "生猪期货(元/吨,÷1000≈元/公斤)。猪周期直接定价,2021 年上市——唯一可交易的猪价序列。",
        "HOG_FEED": "猪粮比=生猪价÷玉米价。6 是官方盈亏平衡预警线,<6 深度亏损(触发收储预期),>9 利润丰厚刺激扩栏。",
        "M0_CLOSE": "豆粕期货(元/吨)。饲料蛋白核心,生猪养殖的主要可变成本。",
        "C0_CLOSE": "玉米期货(元/吨)。能量饲料锚,猪粮比分母;也是通胀 CPI 的粮价输入。",
        "JD0_CLOSE": "鸡蛋期货(元/500千克)。蛋鸡链观察口,同样吃玉米豆粕成本,与猪价有替代消费联动。",
        "LH0_OI": "生猪持仓量,养殖产业套保热度。",
        "M0_OI": "豆粕持仓量,饲料厂与油厂套保。",
    },
}
# 页面导读(人话版,匹配 2026-10-27 二轮减法后的首屏:四件套+核心图+折叠全量)
_GUIDE = {
    "gold": (
        "先看四个关键数:价格(伦敦金)、定价锚(10Y 实际利率,与金价负相关,是最稳的锚)、"
        "资金(SPDR 持仓,西方金融资金流向)、估值(金银比,>80 偏「银便宜」、<50 偏「银贵」)。"
        "核心图看金价与实际利率是否反向。想看更细(央行储备/CFTC 仓位/伦敦金库/宏观因子),点下方「展开全部指标」。"
    ),
    "silver": (
        "白银=黄金的贝塔×工业供需。先看四个关键数:价格(伦敦银)、估值(金银比,>80 偏「银便宜」)、"
        "资金(SLV 持仓,近年减持在弥合连续 6 年的供需缺口)、工业(多晶硅,光伏排产温度计——白银第一大需求是光伏)。"
        "核心图看金银同向性与弹性差(银涨跌更陡)。CFTC 仓位/伦敦金库/伴生矿代理都在下方「展开全部指标」。"
    ),
    "copper": (
        "铜=全球经济温度计,中国占全球消费约 55%,定价核心是制造业景气而非金融属性。"
        "先看四个关键数:价格(LME铜)、需求(制造业PMI,50 是荣枯线)、仓位(CFTC 非商业净持仓)、"
        "估值(铜金比,越高=市场越押周期/增长)。核心图看铜价与美元是否反向。"
        "沪伦比(中国需求温度计)与沪铜库存在下方「展开全部指标」。"
    ),
    "ferrous": (
        "黑色系=中国定价权最强的产业链:地产/基建需求→钢材(螺纹/热卷)→原料(铁矿/焦煤)。"
        "先看四个关键数:价格(螺纹)、成本(铁矿)、利润(钢厂盘面利润=螺纹−1.6矿−0.5煤,"
        ">0 即期有冶炼利润)、结构(卷螺差,制造业 vs 建筑业)。核心图看钢价与铁矿的成本挤压。"
        "普氏指数与港口库存无免费源,P2 再补。"
    ),
    "agri": (
        "农产品=猪周期链条:猪价÷玉米价=猪粮比,6 是官方盈亏平衡线(低于触发收储),对 CPI 与"
        "养殖股(牧原/温氏)的周期位置直接相关。先看四个关键数:价格(生猪)、比价(猪粮比)、"
        "饲料(豆粕,蛋白成本)、粮价(玉米)。核心图看猪价与玉米的成本传导。"
        "能繁母猪存栏(产能先行指标)无免费源记 P2,生猪期货 2021 年上市历史短。"
    ),
    "crudeoil": (
        "原油=全球流量大宗之王:OPEC+ 供给管理 vs 全球需求,美元计价。先看四个关键数:"
        "价格(WTI)、比价(布伦特-WTI价差,反映美国以外市场紧张度)、仓位(CFTC 商业净持仓)、"
        "炼制(取暖油/原油比价,成品油相对原料的强弱)。核心图看油价与美元是否反向。"
        "EIA 周度库存与贝克休斯钻机数的免费源已停更,记 P2(可注册 EIA 免费 key 接入)。"
    ),
    "lithium": (
        "碳酸锂=新能源周期品种:锂电需求占 80%+,2022 年 60 万→2024 年 7 万的完整周期由供给释放主导,"
        "当前处于反弹段。先看四个关键数:价格(广期所主力)、需求(新能车ETF)、库存(广期所仓单)、"
        "仓位(持仓量)。核心图看价格与终端需求的同向/背离——背离常意味着供给才是主矛盾。"
        "现货价(SMM)与锂矿成本线(云母约 9 万/盐湖约 4 万)无免费源,P2 再补。"
    ),
    "aluminum": (
        "铝=供给约束品种:中国占全球产量约 60%,电解铝产能有红线,定价核心在成本(氧化铝+电力)与需求"
        "(新能源车轻量化/光伏边框对冲地产)。先看四个关键数:价格(LME铝)、成本(氧化铝,原料锚)、"
        "需求(制造业PMI)、比价(沪伦比,>1=内盘溢价)。核心图看成本传导:氧化铝相对涨得快=冶炼利润被压缩。"
        "沪铝库存在下方「展开全部指标」;LME 铝库存与 CFTC 铝(流动性极低)无可用免费源,已注明。"
    ),
}


def _chg(points, days):
    """近 N 天变动(现值 - 窗口内最早值);窗口内无数据返回 None。"""
    if not points:
        return None
    d0, v0 = points[-1]
    cut = dt.date.fromisoformat(d0) - dt.timedelta(days=days)
    for d, v in points:
        if dt.date.fromisoformat(d) >= cut:
            return round(v0 - v, 4)
    return None


def _stats(points, freq):
    """单序列读时统计:KPI 卡片与新鲜度。"""
    if not points:
        return {"latest": None, "stale": True}
    today = dt.date.today()
    latest_d, latest_v = points[-1]
    win52 = [(dt.date.fromisoformat(d), v) for d, v in points
             if dt.date.fromisoformat(d) >= today - dt.timedelta(days=365)]
    vals = [v for _, v in win52] or [latest_v]
    return {
        "latest": latest_v,
        "latest_date": latest_d,
        "chg_1w": _chg(points, 7),
        "chg_4w": _chg(points, 30),
        "chg_1y": _chg(points, 365),
        "hi_52w": max(vals),
        "lo_52w": min(vals),
        # 52 周分位(0~1):最新值在窗口内的位置,相对贵贱的粗口径
        "pct_52w": round((latest_v - min(vals)) / (max(vals) - min(vals)), 4)
        if max(vals) > min(vals) else 0.5,
        "stale_days": (today - dt.date.fromisoformat(latest_d)).days,
        "stale": (today - dt.date.fromisoformat(latest_d)).days > _STALE_DAYS.get(freq, 14),
    }


def _cmd_series(db: Session, commodity: str):
    """cmd_* 两表按 commodity 取行 → 全序列 + 读时统计 + 释义/导读注入。

    性能(2026-10-27 优化,首屏 5.6s→~1s):① cmd_value 查询按本商品的 code
    集合 IN 过滤——此前全表拉 28 万行(八商品全部点)再丢弃七成;② 日频序列
    points 降采样(近 180 天保留全部,更早按 ISO 周桶取首见,~1100 点封顶),
    响应体积约 1/5——stats(52周分位/变动/高低)始终用全量点算,不受采样影响;
    图表 1000+ 点视觉无损,周/月/年频序列点少不采。
    """
    inds = db.execute(
        select(CmdIndicator).where(CmdIndicator.commodity == commodity)
    ).scalars().all()
    pts = defaultdict(list)
    codes = [ind.cmd_code for ind in inds]
    if codes:
        for code, d, v in db.execute(
            select(CmdValue.cmd_code, CmdValue.data_date, CmdValue.val)
            .where(CmdValue.cmd_code.in_(codes)).order_by(CmdValue.data_date)
        ):
            pts[code].append([d.isoformat(), float(v) if v is not None else None])

    desc = _DESC.get(commodity, {})
    series = []
    # idx=0 的序列(伦敦金/伦敦银)不能用 `or 999` 兜底——0 是 falsy 会被顶到队尾(实测踩坑)
    def _idx(ind):
        v = (ind.extra or {}).get("idx")
        return 999 if v is None else v

    for ind in sorted(inds, key=_idx):
        points = pts.get(ind.cmd_code, [])
        series.append({
            "code": ind.cmd_code, "name": (ind.extra or {}).get("name") or ind.name,
            "label": ind.name, "cat": ind.category, "group": ind.display_group,
            "unit": ind.unit, "freq": _FREQ_CN.get(ind.freq, ind.freq),
            "source": (ind.extra or {}).get("source"),
            "desc": desc.get(ind.cmd_code),
            "stats": _stats(points, ind.freq),
            "points": _sample(points, ind.freq),
        })
    cats = []
    for s in series:  # cat 首见序即展示序(fetch SPEC 顺序)
        if s["cat"] not in cats:
            cats.append(s["cat"])
    return {
        "updated_at": max((s["stats"].get("latest_date") for s in series), default=None),
        "commodity": commodity,
        "cats": cats,
        "guide": _GUIDE.get(commodity),
        "series": series,
    }


def _sample(points, freq):
    """日频长序列降采样:近 180 天保留全部,更早按 ISO 周桶取首见(~1100 点封顶)。

    只用于响应里的 points(画图);stats 由调用方在全量点上先算。周/月/年频
    与短序列原样返回。图表 1000+ 点视觉无损,归一化基准点可能移位 ≤7 天,无感。
    """
    if freq != "day" or len(points) <= 1400:
        return points
    today = dt.date.today()
    cut = (today - dt.timedelta(days=180)).isoformat()
    head, tail = [], []
    for p in points:
        (tail if p[0] >= cut else head).append(p)
    out, seen = [], set()
    for p in head:
        k = dt.date.fromisoformat(p[0]).isocalendar()[:2]
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out + tail


@router.get("/gold")
def gold_series(db: Session = Depends(get_session)):
    """黄金供需研究 P0:全序列 + 读时统计。"""
    return _cmd_series(db, "gold")


@router.get("/silver")
def silver_series(db: Session = Depends(get_session)):
    """白银供需研究 P0:全序列 + 读时统计(2026-10-27 二期,金银比白银版 AG_GSR)。"""
    return _cmd_series(db, "silver")


@router.get("/copper")
def copper_series(db: Session = Depends(get_session)):
    """铜供需研究 P0:全序列 + 读时统计(2026-10-27 三期,共码序列 CU_ 前缀)。"""
    return _cmd_series(db, "copper")


@router.get("/aluminum")
def aluminum_series(db: Session = Depends(get_session)):
    """铝供需研究 P0:全序列 + 读时统计(2026-10-27 四期,共码序列 AL_ 前缀)。"""
    return _cmd_series(db, "aluminum")


@router.get("/lithium")
def lithium_series(db: Session = Depends(get_session)):
    """碳酸锂供需研究 P0:全序列 + 读时统计(2026-10-27 五期,共码序列 LI_ 前缀)。"""
    return _cmd_series(db, "lithium")


@router.get("/crudeoil")
def crudeoil_series(db: Session = Depends(get_session)):
    """原油供需研究 P0:全序列 + 读时统计(2026-10-27 六期,共码序列 OIL_ 前缀)。"""
    return _cmd_series(db, "crudeoil")


@router.get("/ferrous")
def ferrous_series(db: Session = Depends(get_session)):
    """黑色系供需研究 P0:全序列 + 读时统计(2026-10-27 七期,派生 FE_ 前缀)。"""
    return _cmd_series(db, "ferrous")


@router.get("/agri")
def agri_series(db: Session = Depends(get_session)):
    """农产品(生猪链)供需研究 P0:全序列 + 读时统计(2026-10-27 八期)。"""
    return _cmd_series(db, "agri")
