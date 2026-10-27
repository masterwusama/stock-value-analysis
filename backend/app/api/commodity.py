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

_FREQ_CN = {"day": "日", "week": "周", "month": "月"}
# 新鲜度红线(天):按频率分档——周度 CFTC 周五发布,月度储备滞后 2 个月是常态
_STALE_DAYS = {"day": 7, "week": 21, "month": 70}

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
    """cmd_* 两表按 commodity 取行 → 全序列 + 读时统计 + 释义/导读注入。"""
    inds = db.execute(
        select(CmdIndicator).where(CmdIndicator.commodity == commodity)
    ).scalars().all()
    pts = defaultdict(list)
    for code, d, v in db.execute(
        select(CmdValue.cmd_code, CmdValue.data_date, CmdValue.val).order_by(CmdValue.data_date)
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
            "points": points,
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
