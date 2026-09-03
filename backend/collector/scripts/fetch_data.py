#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取 A 股财务数据，生成供 GitHub Pages 托管的静态 JSON。

数据源（全部为公开免费接口，无需 token）：
- 财务指标（ROE/毛利率/净利率等）：同花顺摘要接口
- 三大报表（利润表/资产负债表/现金流量表）：新浪财经
- 最新估值快照（最新价/PE/PB/市值）：腾讯行情接口
- 分红历史：巨潮资讯
- 定期报告 PDF 链接：巨潮资讯
- 审计信息（事务所/意见类型）：定期报告 PDF 文本解析（巨潮直链）

用法：
    python fetch_data.py                      # 抓取 config.DEFAULT_COMPANIES 精选池
    python fetch_data.py --all-market         # 全 A 股（沪深京 ≈ 5500 只）
    python fetch_data.py --all-market --workers 4 --resume
                                              # 首轮全量：4 并发 + 断点续跑
    python fetch_data.py --all-market --shard 1/4   # 分片多进程并行
    python fetch_data.py --codes 600028,920000       # 指定代码增量补抓
    python fetch_data.py --codes HK.00700            # 港股代码带前缀（不补零到 6 位，不会砸 A 股同名文件）
    python fetch_data.py --hk-connect --workers 6    # 只跑港股通名单（≈621 只）
    python fetch_data.py --us-indexes --workers 6    # 只跑美股五大指数成分（≈765 只）
    python fetch_data.py --snapshot-only      # 仅用腾讯批量行情刷全市场估值（约 1 分钟）

index.json 采用“合并写”：每次只覆盖本次跑到的条目，其余保留，
因此 --limit/--codes/--shard 部分执行不会把名单截断（旧版会）。
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pymupdf
import requests

# [collector 改造] 损坏年报 PDF 的 xref 告警会刷屏(本地实测 30MB+/分钟日志,拖慢解析)，
# 官方开关静默;不影响解析结果
try:
    pymupdf.TOOLS.mupdf_display_errors(False)
except Exception:
    pass

# [卡死护栏①] 兼容性兜底，但**对 requests/SSL 路径不可靠**（见护栏②），
# 只能挡住少数直接走 socket 的调用，别再把它当成防挂死的依据。
socket.setdefaulttimeout(45)


# [卡死护栏②] akshare 内部调 requests 一律不传 timeout，urllib3 在调用方给 None 时会
# 把 socket 超时显式清成 None，所以 setdefaulttimeout 对它们无效——实测 2026-09-02 22:27
# 三个工人带着护栏仍永久停在 ssl.read（堆栈：stock_financial_report_sina → requests.content
# → read_chunked → ssl.read）。这里在传输层补默认值：只把 None 换掉，我们自己显式传过的
# （年报 PDF 下载 timeout=90）原样放过。
def _force_default_timeout(connect=10, read=45):
    try:
        from requests.adapters import HTTPAdapter
    except ImportError:  # requests 不在，不阻断抓取
        return
    if getattr(HTTPAdapter.send, "_va_timeout", False):
        return
    _orig_send = HTTPAdapter.send

    def send(self, request, timeout=None, *args, **kwargs):
        if timeout is None:
            timeout = (connect, read)   # 元组：连接超时 + 单次读超时
        return _orig_send(self, request, timeout=timeout, *args, **kwargs)

    send.__name__ = "send"
    send._va_timeout = True
    HTTPAdapter.send = send


_force_default_timeout()

from config import DEFAULT_COMPANIES, REQUEST_INTERVAL
from scoring import compute_scores  # 预计算评分（与 assets/stock.js 一致性由 _score_check.py 验证）

# 输出目录：<仓库>/stock-data/data
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
COMPANIES_DIR = OUTPUT_DIR / "companies"

# 输出报告期数基准（实际输出 MAX_PERIODS+1 期 ≈ 8 年；
# 多 1 期用于最早一期单季值的还原，相应多抓 1 期被丢弃）
MAX_PERIODS = 32

# 巨潮资讯定期报告类别（年报/半年报/一季报/三季报）
REPORT_CATEGORIES = ["年报", "半年报", "一季报", "三季报"]

# 中国大陆时区（用于 updated_at 时间戳）
CN_TZ = timezone(timedelta(hours=8))


def sina_symbol(code: str) -> str:
    """6/9 开头 → sh，0/2/3 开头 → sz，4/8/92 开头 → bj

    920 段是北交所新代码段(2024 起),需先于 "9→sh" 判定,否则会被当成沪市。
    """
    if code.startswith("92") or code.startswith(("4", "8")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    return "bj" + code


def parse_number(value):
    """把 '1.47亿'、'23.38%'、'--' 等原始字符串解析为 float，无法解析返回 None。

    单位规则：万亿/亿/万 后缀为数量级倍率；% 后缀除以 100（比率统一为小数）。
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s in ("--", "-", "False", "None", "nan", "NaN"):
        return None
    mult = 1.0
    for suffix, factor in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if s.endswith(suffix):
            mult = factor
            s = s[: -len(suffix)]
            break
    if s.endswith("%"):
        s = s[:-1]
        mult = mult / 100.0
    try:
        return round(float(s) * mult, 4)
    except ValueError:
        return None


def to_iso(date_value) -> str:
    """'20251231' / '2025-12-31' → '2025-12-31'；无法识别原样返回；NaN/NaT → None"""
    s = str(date_value).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    digits = s.replace("-", "").replace("/", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def sleep_between():
    time.sleep(REQUEST_INTERVAL)


def df_to_records(df, numeric_cols=None):
    """DataFrame → 记录数组；数值列统一 parse_number，日期列转 ISO，NaN 转 None"""
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            val = row[col]
            if pd_isna(val):
                rec[str(col)] = None
            elif numeric_cols and col in numeric_cols:
                rec[str(col)] = parse_number(val)
            elif "日" in str(col) and ("报告" in str(col) or col in ("date",)):
                rec[str(col)] = to_iso(val)
            elif isinstance(val, (int, float)):
                rec[str(col)] = float(val)
            else:
                rec[str(col)] = str(val).strip()
        records.append(rec)
    return records


def pd_isna(val):
    try:
        import pandas as pd
        return pd.isna(val)
    except Exception:
        return val is None


def fetch_indicators(code: str):
    """同花顺财务摘要：关键指标，按报告期倒序取最近 MAX_PERIODS 期。

    财务数据为累计口径（一季报=Q1，半年报=Q1+Q2，三季报=前三季，年报=全年），
    额外计算单季口径（营业总收入/净利润）：本期累计 - 上期累计；
    一季报(03-31)本身就是单季。新增字段 `*_单季`，保留原始累计值。
    多抓 2 期：1 期供最早一期的单季还原，另 1 期因无上期而被丢弃。
    """
    df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    if df is None or df.empty:
        return []
    df = df.copy()
    df["_dt"] = pd_to_datetime(df["报告期"])
    df = df.sort_values("_dt", ascending=False).head(MAX_PERIODS + 2)
    df = df.sort_values("_dt", ascending=True).reset_index(drop=True)
    # 单季化（升序遍历，累计差）
    for col in ("营业总收入", "净利润"):
        single = []
        for i, row in df.iterrows():
            cum = parse_number(row[col])
            if i == 0:
                single.append(None)  # 最早一期无上期，无法还原
            elif str(row["报告期"]).endswith("03-31"):
                single.append(cum)
            else:
                prev = parse_number(df.loc[i - 1, col])
                single.append(None if (cum is None or prev is None) else round(cum - prev, 4))
        df[f"{col}_单季"] = single
    df = df.iloc[1:]  # 丢弃最早一期（该期之前已用其一季报还原相邻期单季值）
    df = df.sort_values("_dt", ascending=False)
    df = df.drop(columns=["_dt"])
    # 除报告期外均为数值列（带亿/% 等单位的原始字符串，需统一解析）
    numeric_cols = [c for c in df.columns if c != "报告期"]
    records = df_to_records(df, numeric_cols=numeric_cols)
    for rec in records:
        if rec.get("报告期"):
            rec["报告期"] = to_iso(rec["报告期"])
    return records


def pd_to_datetime(series):
    import pandas as pd
    return pd.to_datetime(series, errors="coerce")


def fetch_report(code: str, kind: str):
    """新浪三大报表：kind ∈ {'利润表', '资产负债表', '现金流量表'}"""
    df = ak.stock_financial_report_sina(stock=sina_symbol(code), symbol=kind)
    if df is None or df.empty:
        return []
    df = df.copy()
    # 新浪返回全部历史报告期，按报告日倒序取最近 MAX_PERIODS 期
    df["_dt"] = pd_to_datetime(df["报告日"])
    df = df.sort_values("_dt", ascending=False).head(MAX_PERIODS)
    df = df.drop(columns=["_dt"])
    # 除元数据列外的都是数值列
    meta_cols = {"报告日", "数据源", "是否审计", "公告日期", "币种", "类型", "更新日期"}
    numeric_cols = [c for c in df.columns if c not in meta_cols]
    records = df_to_records(df, numeric_cols=numeric_cols)
    for rec in records:
        rec["报告日"] = to_iso(rec.get("报告日"))
        if rec.get("公告日期"):
            rec["公告日期"] = to_iso(rec["公告日期"])
    return records


def fetch_info(code: str):
    """个股基本信息：优先东财，失败时用巨潮 profile 兜底（提供行业）；
    两者都失败时抛异常，由调用方记录到 errors（不静默吞掉）。"""
    try:
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            return {str(r["item"]): r["value"] for _, r in df.iterrows()}
    except Exception:
        pass
    try:
        df = ak.stock_profile_cninfo(symbol=code)
        if df is not None and not df.empty:
            row = df.iloc[0]
            info = {}
            if row.get("所属行业") is not None:
                info["行业"] = str(row["所属行业"]).strip()
            if row.get("A股简称") is not None:
                info["股票简称"] = str(row["A股简称"]).strip()
            if row.get("上市日期") is not None:
                info["上市日期"] = to_iso(str(row["上市日期"])[:10])
            return info
    except Exception:
        pass
    raise RuntimeError("东财/巨潮基本信息接口均不可用")


# 东财/巨潮的“行业”字段值（如“汽车制造业”）为证监会行业分类，原样保留即可


def iso_or_none(v):
    """NaN/NaT → None，否则转 ISO 日期"""
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        if v is None:
            return None
    return to_iso(v)


def format_quote_time(raw):
    """行情时间 → ISO：A 股 '20260807161442' / 港股 '2026/08/21 16:08:14' / 美股 '2026-08-25 16:00:01'"""
    s = str(raw).strip()
    if len(s) == 14 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}+08:00"
    m = re.match(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}+08:00"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}+08:00"
    return None


def fetch_snapshot(code: str):
    """最新估值快照：腾讯行情接口（最新价/涨跌幅/PE/PB/市值/换手率），A 股专用"""
    url = f"http://qt.gtimg.cn/q={sina_symbol(code)}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    parts = r.text.strip().split(";")[0].split("~")
    if len(parts) < 50:
        return {}

    def num(i):
        try:
            v = float(parts[i])
            return v
        except (ValueError, IndexError):
            return None

    price = num(3)
    snapshot = {
        "name": parts[1].strip() or None,
        "price": price,
        "change_pct": None if num(32) is None else round(num(32) / 100.0, 6),
        "pe_ttm": num(39),
        "pb": num(46),
        "market_cap": None if num(45) is None else round(num(45) * 1e8, 2),
        "float_market_cap": None if num(44) is None else round(num(44) * 1e8, 2),
        "turnover_rate": None if num(38) is None else round(num(38) / 100.0, 6),
        "time": format_quote_time(parts[30]),
    }
    return snapshot


# ==================== 港股数据源（东财港股 + 腾讯行情） ====================
# 东财港股三大报表为长表（每行一个科目），科目名按 IFRS 口径，映射回前端 A 股字段名。
# 注意：东财已把各公司财年末统一映射为 12-31 报告期，与 A 股 schema 完全对齐。

# 资产负债表科目映射：东财科目名 → 前端字段（未列出的科目保留原名）
HK_BALANCE_MAP = {
    "流动资产合计": "流动资产合计",
    "流动负债合计": "流动负债合计",
    "总负债": "负债合计",
    "总资产": "资产总计",
    "现金及等价物": "货币资金",
    # 类现金科目归并到“交易性金融资产”，供净现金/市值加权计算
    "短期投资": "交易性金融资产",
    "指定以公允价值记账之金融资产(流动)": "交易性金融资产",
    "无形资产": "无形资产",
    "商誉": "商誉",
    "短期贷款": "短期借款",
    "长期贷款": "长期借款",
    "融资租赁负债(非流动)": "租赁负债",
    "股东权益": "所有者权益(或股东权益)合计",
}

# 利润表科目映射
HK_INCOME_MAP = {
    "营业额": "营业总收入",
    "营运收入": "营业收入",
    "股东应占溢利": "净利润",
    "毛利": "营业利润",
    "税项": "所得税费用",
}

# 现金流量表科目映射（港股无“销售商品提供劳务收到的现金”，收现比留空）
HK_CASHFLOW_MAP = {
    "经营业务现金净额": "经营活动产生的现金流量净额",
    "购建固定资产": "购建固定资产、无形资产和其他长期资产所支付的现金",
    "购建无形资产及其他资产": "购建无形资产及其他资产支付的现金",
    "期末现金": "期末现金及现金等价物余额",
}

# 东财港股分析指标（英文列）→ 前端 indicators 字段；比率类为百分数数值需 /100
HK_IND_MAP = {
    "OPERATE_INCOME": "营业总收入",
    "HOLDER_PROFIT": "净利润",
    "BASIC_EPS": "基本每股收益",
    "BPS": "每股净资产",
}
HK_IND_PCT = {
    "GROSS_PROFIT_RATIO": "销售毛利率",
    "NET_PROFIT_RATIO": "销售净利率",
    "ROE_AVG": "净资产收益率",
    "ROA": "总资产净利率",
    "DEBT_ASSET_RATIO": "资产负债率",
}


def _pct(v):
    """百分数数值（50.87）→ 小数（0.5087）"""
    n = parse_number(v)
    return None if n is None else round(n / 100.0, 4)


def fetch_hk_report(code: str, kind: str, item_map: dict):
    """东财港股三大报表：长表（科目×金额）→ 宽表（报告日 × 字段），按报告日倒序"""
    df = ak.stock_financial_hk_report_em(stock=code, symbol=kind)
    if df is None or df.empty:
        return []
    by_date = {}
    for _, r in df.iterrows():
        dt = to_iso(str(r["REPORT_DATE"])[:10])
        if not dt:
            continue
        item = item_map.get(str(r["STD_ITEM_NAME"]).strip(), str(r["STD_ITEM_NAME"]).strip())
        val = parse_number(r["AMOUNT"])
        by_date.setdefault(dt, {})[item] = val
    records = [{"报告日": dt, **fields} for dt, fields in by_date.items()]
    records.sort(key=lambda r: r["报告日"], reverse=True)
    return records[:MAX_PERIODS]


def fetch_hk_indicators(code: str):
    """东财港股财务分析指标：映射为前端 indicators 字段，含单季还原（累计差），按报告期倒序"""
    df = ak.stock_financial_hk_analysis_indicator_em(symbol=code)
    if df is None or df.empty:
        return []
    df = df.copy()
    df = df.sort_values("REPORT_DATE", ascending=True).reset_index(drop=True)
    rows = []
    for _, r in df.iterrows():
        rec = {"报告期": to_iso(str(r["REPORT_DATE"])[:10])}
        for en, zh in HK_IND_MAP.items():
            rec[zh] = parse_number(r[en])
        for en, zh in HK_IND_PCT.items():
            rec[zh] = _pct(r[en])
        rec["流动比率"] = parse_number(r["CURRENT_RATIO"])
        rows.append(rec)
    # 单季还原：本期累计 - 上期累计（港股无 A 股式一季报特例，统一按累计差）
    for col in ("营业总收入", "净利润"):
        for i, rec in enumerate(rows):
            cum = rec[col]
            if i == 0:
                rec[col + "_单季"] = None
            else:
                prev = rows[i - 1][col]
                rec[col + "_单季"] = None if (cum is None or prev is None) else round(cum - prev, 4)
    rows = rows[-MAX_PERIODS:]
    return rows[::-1]


def fetch_hk_snapshot(code: str):
    """港股快照：腾讯行情（价格/涨跌幅/时间）+ 东财指标（PE/PB/市值）

    腾讯港股字段与 A 股不同：3=最新价、30=时间、31=涨跌额、32=涨跌幅、44=流通市值(亿港元)、45=总市值(亿港元)
    """
    url = f"http://qt.gtimg.cn/q=hk{code}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    parts = r.text.strip().split(";")[0].split("~")
    if len(parts) < 40:
        return {}

    def num(i):
        try:
            return float(parts[i])
        except (ValueError, IndexError):
            return None

    snapshot = {
        "name": parts[1].strip() or None,
        "price": num(3),
        "change_pct": None if num(32) is None else round(num(32) / 100.0, 6),
        "time": format_quote_time(parts[30]),
    }
    try:
        df = ak.stock_hk_financial_indicator_em(symbol=code)
        if df is not None and not df.empty:
            row = df.iloc[0]
            snapshot["pe_ttm"] = parse_number(row["市盈率"])
            snapshot["pb"] = parse_number(row["市净率"])
            snapshot["market_cap"] = parse_number(row["总市值(港元)"])
            snapshot["float_market_cap"] = None if num(44) is None else round(num(44) * 1e8, 2)
            snapshot["turnover_rate"] = None
    except Exception:
        snapshot["pe_ttm"] = None
        snapshot["pb"] = None
        snapshot["market_cap"] = None
        snapshot["float_market_cap"] = None
        snapshot["turnover_rate"] = None
    return snapshot


def fetch_hk_dividends(code: str):
    """东财港股分红：方案文本解析每股派息 → 每10股口径，日期统一 ISO"""
    df = ak.stock_hk_dividend_payout_em(symbol=code)
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        plan = str(row["分红方案"]).strip()
        bonus = None
        m = re.search(r"每股派(?:人民币|港币)?([\d.]+)元", plan)
        if m:
            bonus = round(float(m.group(1)) * 10, 4)  # 每股 → 每10股
        else:
            m = re.search(r"每10股派(?:人民币|港币)?([\d.]+)元", plan)
            if m:
                bonus = round(float(m.group(1)), 4)
        kind = str(row["分配类型"]).strip()
        annual = ("年度" in kind) or ("末期" in kind)
        rec_date = str(row["截至过户日"]).strip() if row["截至过户日"] else ""
        rec_date = to_iso(rec_date.split("-")[0].split("至")[0]) if rec_date else None
        records.append(
            {
                "year": str(row["财政年度"]).strip() + ("年报" if annual else "中报"),
                "type": "年度分红" if annual else "中期分红",
                "announce_date": iso_or_none(str(row["最新公告日期"])[:10]),
                "record_date": rec_date,
                "ex_date": iso_or_none(str(row["除净日"])[:10]),
                "pay_date": iso_or_none(str(row["发放日"])[:10]),
                "bonus_per_10": bonus,
                "transfer_per_10": None,
                "description": plan,
            }
        )
    records.sort(key=lambda r: r["announce_date"] or "", reverse=True)
    return records


_HK_INDUSTRY_CACHE = None


def hk_industry(code: str):
    """港股通名单自带的行业名（东财 f100，见 universe.load_universe_hk）。

    东财港股个股接口不给行业，但拉名单时一次就带全 621 只，所以在此查表而不是逐只加请求。
    名单缓存缺失/损坏时返回 None：只丢行业列，不阻断抓取。
    """
    global _HK_INDUSTRY_CACHE
    if _HK_INDUSTRY_CACHE is None:
        try:
            import universe as uni
            _HK_INDUSTRY_CACHE = {c: ind for c, _, ind in uni.load_universe_hk(quiet=True)}
        except Exception:
            _HK_INDUSTRY_CACHE = {}
    return _HK_INDUSTRY_CACHE.get(code)


def fetch_company_hk(code: str, name: str):
    """抓取港股公司：指标/三大报表/快照/分红（东财 + 腾讯）；无定期报告 PDF 与审计信息"""
    result = {"code": code, "name": name, "market": "HK"}
    result["updated_at"] = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    errors = []
    industry = hk_industry(code)
    # 行业来自港股通名单（东财 f100）；不在名单里的港股（如非港股通标的）留空
    result["info"] = {"行业": industry} if industry else {}

    try:
        result["indicators"] = fetch_hk_indicators(code)
    except Exception as e:
        result["indicators"] = []
        errors.append(f"indicators: {e}")
    sleep_between()

    for key, kind, item_map in (
        ("income", "利润表", HK_INCOME_MAP),
        ("balance", "资产负债表", HK_BALANCE_MAP),
        ("cashflow", "现金流量表", HK_CASHFLOW_MAP),
    ):
        try:
            result[key] = fetch_hk_report(code, kind, item_map)
        except Exception as e:
            result[key] = []
            errors.append(f"{key}: {e}")
        sleep_between()

    # 归母权益 = 股东权益 - 少数股东权益（杜邦拆解口径）
    for rec in result["balance"]:
        eq = rec.get("所有者权益(或股东权益)合计")
        mi = rec.get("少数股东权益")
        if eq is not None and mi is not None:
            rec["归属于母公司股东权益合计"] = round(eq - mi, 4)

    try:
        result["snapshot"] = fetch_hk_snapshot(code)
    except Exception as e:
        result["snapshot"] = {}
        errors.append(f"snapshot: {e}")
    sleep_between()

    try:
        result["dividends"] = fetch_hk_dividends(code)
    except Exception as e:
        result["dividends"] = []
        errors.append(f"dividends: {e}")
    sleep_between()

    result["reports"] = []  # 港股无巨潮定期报告，前端已容错
    result["errors"] = errors if errors else None
    return result


# ==================== 美股数据源（东财美股 + 腾讯行情） ====================
# 东财美股三大报表为长表（科目×报告期），科目中文命名；财务指标仅年报口径
# （金额单位为美元；真实财年末由我们自己压平成 12-31 对齐 A 股 schema，见 fiscal_year_end）；
# 无分红/定期报告接口（前端已容错空列表）。

# 财务指标映射：前端字段 → 东财列名候选（取第一个非空）；比率类为百分数数值需 /100
# 候选列名为何不止一个：东财美股指标分三张表，叫法不一样——通用表 G 给
# OPERATE_INCOME/ROE_AVG/DEBT_ASSET_RATIO，银行表 B（券商也算）与保险表 I
# （REIT 也算）给 TOTAL_INCOME/ROE/DEBT_RATIO
US_IND_MAP = {
    "营业总收入": ("OPERATE_INCOME", "TOTAL_INCOME"),
    "净利润": ("PARENT_HOLDER_NETPROFIT",),
    "基本每股收益": ("BASIC_EPS",),   # 不取 B/I 表的 BASIC_EPS_CS：它按 A 类股本算（BRK_B 给出 46563），
    # 而快照价是 B 类，两相除就差 1500 倍；美股 EPS 只喂派息率（美股无分红接口），留空无损
}
US_IND_PCT = {
    "销售毛利率": ("GROSS_PROFIT_RATIO",),
    "销售净利率": ("NET_PROFIT_RATIO",),
    "净资产收益率": ("ROE_AVG", "ROE"),
    "总资产净利率": ("ROA",),
    "资产负债率": ("DEBT_ASSET_RATIO", "DEBT_RATIO"),
}

# akshare 查不到时补查的三张表（自己拼请求，除表名外其余参数照 akshare 同构）
US_IND_TABLES = ("RPT_USF10_FN_GMAININDICATOR",
                 "RPT_USF10_FN_BMAININDICATOR", "RPT_USF10_FN_IMAININDICATOR")
US_IND_TABLE_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

# 资产负债表科目映射：东财美股科目名 → 前端字段（未列出的科目保留原名）
US_BALANCE_MAP = {
    "现金及现金等价物": "货币资金",
    # 类现金科目归并到“交易性金融资产”，供净现金/市值加权计算
    "有价证券投资(流动)": "交易性金融资产",
    "短期投资": "交易性金融资产",
    "应收账款": "应收账款",
    "存货": "存货",
    "流动资产合计": "流动资产合计",
    "物业、厂房及设备": "固定资产",
    "无形资产": "无形资产",
    "商誉": "商誉",
    "总资产": "资产总计",
    "短期债务": "短期借款",
    "流动负债合计": "流动负债合计",
    "长期负债": "长期借款",
    "总负债": "负债合计",
    "股东权益合计": "所有者权益(或股东权益)合计",
    "归属于母公司股东权益": "归属于母公司股东权益合计",
}

# 利润表科目映射
US_INCOME_MAP = {
    "主营收入": "营业总收入",
    "营业收入": "营业收入",
    "营业成本": "营业成本",
    "营业利润": "营业利润",
    "归属于母公司股东净利润": "净利润",
    "所得税": "所得税费用",
}

# 现金流量表科目映射（美股无“销售商品提供劳务收到的现金”，收现比留空）
US_CASHFLOW_MAP = {
    "经营活动产生的现金流量净额": "经营活动产生的现金流量净额",
    "购买固定资产": "购建固定资产、无形资产和其他长期资产所支付的现金",
    "购建无形资产及其他资产": "购建无形资产及其他资产支付的现金",
    "现金及现金等价物期末余额": "期末现金及现金等价物余额",
}


def fiscal_year_end(date_str):
    """美股真实财年末 → 对齐用的 12-31 报告期：1-6 月 → 上年 12-31，7-12 月 → 当年 12-31。

    为什么压平发生在我们而不在源（2026-09-03 实测东财接口）：源给的就是真实财年末
    ——NVDA 全是 1 月末（2026-01-25…）、AAPL 9 月末、MSFT 6 月末，只有我们的
    fetch_us_report/_us_ind_row 调了本函数。压平不能去掉：scoring.py 有 6 处靠
    `'12-31' in 报告日` 选年报行（annualRows/annualBalanceRows/prev_ann 等），美股原样
    入库就一条年报都选不出来，整套评分对 765 只美股直接失效。

    压平丢掉的是“这一行到底盖住哪 12 个月”：MSFT 截至 2025-06-30 的财年会被标成
    2024-12-31，跟日历年公司并排比增速时点上差半年。这个信息用 `财年截止` 字段补回
    （随 extras 全量入库、API 原样还原，见 app/fin_columns.py），不再靠读的人猜。
    """
    try:
        y, m, _ = str(date_str).split("-")
        y, m = int(y), int(m)
        return f"{y - (1 if m <= 6 else 0)}-12-31"
    except (ValueError, TypeError):
        return date_str


def us_report_period(raw):
    """东财 REPORT_DATE → (对齐用的 12-31 标签, 真实财年末)；识别不了时两者同为原值。"""
    real = to_iso(str(raw)[:10])
    return fiscal_year_end(real), real


def fetch_us_report(code: str, kind: str, item_map: dict):
    """东财美股三大报表：长表（科目×金额）→ 宽表（报告日 × 字段），按报告日倒序"""
    df = ak.stock_financial_us_report_em(stock=code, symbol=kind, indicator="年报")
    if df is None or df.empty:
        return []
    by_date = {}
    real_by_label = {}   # 12-31 标签 → 该行实际来自哪个财年末
    for _, r in df.iterrows():
        dt, real = us_report_period(r["REPORT_DATE"])
        if not dt:
            continue
        prev = real_by_label.get(dt)
        if prev and prev != real:
            # 两个不同财年末压进同一个 12-31（换财年的过渡期、53 周制公司）时，老写法会把
            # 两年的科目并成一行——那不是“取其一”，是造出一个哪年都不对的行。留财年末更晚
            # 的整行，先清掉旧的再填，并喊一声让人去核
            print("  [warn] %s %s: 财年 %s 与 %s 同落 %s，只留较晚的" % (code, kind, prev, real, dt))
            if prev > real:
                continue
            by_date[dt] = {}
        real_by_label[dt] = real
        item = item_map.get(str(r["ITEM_NAME"]).strip(), str(r["ITEM_NAME"]).strip())
        val = parse_number(r["AMOUNT"])
        by_date.setdefault(dt, {})[item] = val
    records = [{"报告日": dt, "财年截止": real_by_label.get(dt), **fields}
               for dt, fields in by_date.items()]
    records.sort(key=lambda r: r["报告日"], reverse=True)
    return records[:MAX_PERIODS]


def _us_ind_row(r):
    """东财美股指标行（pandas Series 或自拼请求的 dict 都行）→ 前端字段行

    列名按候选逐个试、缺失不报错：三张表叫法不同，且同属保险表的
    BRK_B（37 列）与 AAPL（49 列）给的列也不齐。
    """
    def pick(cols, conv):
        # 不能停在“第一个列名存在”上：东财有空串/"-" 占位，解析出 None 还得往下个候选试
        for c in cols:
            if r.get(c) is not None:
                v = conv(r[c])
                if v is not None:
                    return v
        return None

    label, real = us_report_period(r.get("REPORT_DATE") or "")
    rec = {"报告期": label, "财年截止": real}
    for zh, cols in US_IND_MAP.items():
        rec[zh] = pick(cols, parse_number)
    for zh, cols in US_IND_PCT.items():
        rec[zh] = pick(cols, _pct)
    rec["流动比率"] = parse_number(r.get("CURRENT_RATIO"))   # 银行/保险两张表不给
    # 美股仅年报（无单季口径），单季字段留空，前端季视图退化为报告期序列
    rec["营业总收入_单季"] = None
    rec["净利润_单季"] = None
    return rec


def _us_indicators_fallback(code: str):
    """指标兜底：直接请求东财通用 G / 银行 B / 保险 I 三张表，返回按报告期升序

    为何连通用表也要自己再查一遍：akshare 靠“代码含下划线 = 保险”这个粗规则选表，
    BF_B（百富门 B 类，酒类）因此被送去保险表、拿回空（实测三张表里只有通用表有它）。
    filter 用裸 ticker（SECURITY_CODE="JPM"）即可，不必像 akshare 那样先换成
    带交易所后缀的 SECUCODE（实测两种写法行数一致）。
    """
    for rpt in US_IND_TABLES:
        params = {
            "reportName": rpt, "columns": "ALL", "quoteColumns": "",
            "pageNumber": "", "pageSize": "", "sortTypes": "-1",
            "sortColumns": "REPORT_DATE", "source": "SECURITIES", "client": "PC",
            "filter": f'(SECURITY_CODE="{code}")(DATE_TYPE_CODE="001")',
        }
        try:
            data = (requests.get(US_IND_TABLE_URL, params=params, timeout=25)
                    .json().get("result") or {}).get("data") or []
        except Exception:
            continue
        if data:
            return sorted((_us_ind_row(d) for d in data), key=lambda r: r["报告期"])
    return []


def fetch_us_indicators(code: str):
    """东财美股财务指标：仅年报口径（无季报），映射前端字段，按报告期倒序

    为何要兼容三张表：东财把美股指标拆成通用 G / 银行 B（券商也算）/ 保险 I（REIT 也算），
    而 akshare 只按“代码含下划线”切到保险表，其余一律走通用表——金融/地产类在通用表
    返回 result=None，akshare 直接下标炸 TypeError（实测 765 只里 75 只中招，
    全是 JPM/BAC/GS/O 这种权重股）。通用表拿不到就补查 B/I。
    """
    err = None
    try:
        df = ak.stock_financial_us_analysis_indicator_em(symbol=code)
    except Exception as e:
        df, err = None, e        # 通用表无此股（金融/REIT），走兜底表
    rows = []
    if df is not None and not df.empty:
        df = df.sort_values("REPORT_DATE", ascending=True).reset_index(drop=True)
        rows = [_us_ind_row(r) for _, r in df.iterrows()]
    if not rows:
        rows = _us_indicators_fallback(code)
    if not rows:
        if err:
            raise err           # 三张表都没有：保留原异常，不让它静默成“指标为空”
        return []
    rows = rows[-MAX_PERIODS:]
    return rows[::-1]


def us_code_variants(code: str):
    """带级股票的写法变体（去重保序）：BRK_B ↔ BRK.B。

    东财把伯克希尔 B 写成 BRK_B，标普名单与腾讯行情写 BRK.B，两边互不认账。
    落盘统一用东财形态（五个数据项里四个走东财），只有腾讯快照一处需要反过来试。
    """
    return list(dict.fromkeys((code, code.replace("_", "."), code.replace(".", "_"))))


def fetch_us_snapshot(code: str):
    """美股快照：腾讯行情（价格/涨跌幅/时间/PE/市值，单位美元）。

    腾讯美股字段：3=最新价、30=时间、32=涨跌幅%、39=PE(TTM)、44=流通市值(亿美元)、45=总市值(亿美元)；
    无 PB 与换手率，留空。

    写法变体都得试：名单里存的是东财形态 BRK_B，而腾讯 usBRK_B 返回空、只认 usBRK.B。
    """
    parts = []
    for cand in us_code_variants(code):
        r = requests.get(f"http://qt.gtimg.cn/q=us{cand}", timeout=10)
        r.encoding = "gbk"
        parts = r.text.strip().split(";")[0].split("~")
        if len(parts) >= 40 and parts[3]:
            break
    if len(parts) < 40:
        return {}

    def num(i):
        try:
            return float(parts[i])
        except (ValueError, IndexError):
            return None

    return {
        "name": parts[1].strip() or None,
        "price": num(3),
        "change_pct": None if num(32) is None else round(num(32) / 100.0, 6),
        "pe_ttm": num(39),
        "pb": None,
        "market_cap": None if num(45) is None else round(num(45) * 1e8, 2),
        "float_market_cap": None if num(44) is None else round(num(44) * 1e8, 2),
        "turnover_rate": None,
        "time": format_quote_time(parts[30]),
    }


_US_INDUSTRY_CACHE = None


def us_industry(code: str):
    """美股行业名（东财 f100，跟着指数成分名单一次带全，见 universe.load_universe_us）。

    与港股同一套路：东财美股个股接口不给行业，逐只加请求不划算，改成查名单缓存。
    名单缓存缺失/损坏时返回 None，只丢行业列，不阻断抓取。
    """
    global _US_INDUSTRY_CACHE
    if _US_INDUSTRY_CACHE is None:
        try:
            import universe as uni
            _US_INDUSTRY_CACHE = {r[0]: r[2] for r in uni.load_universe_us(quiet=True)}
        except Exception:
            _US_INDUSTRY_CACHE = {}
    return _US_INDUSTRY_CACHE.get(code)


def fetch_company_us(code: str, name: str):
    """抓取美股公司：指标/三大报表/快照（东财 + 腾讯）；无分红与定期报告"""
    result = {"code": code, "name": name, "market": "US"}
    result["updated_at"] = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    errors = []
    industry = us_industry(code)
    # 行业来自五大指数成分名单；不在名单里的美股（自选代码）留空
    result["info"] = {"行业": industry} if industry else {}

    try:
        result["indicators"] = fetch_us_indicators(code)
    except Exception as e:
        result["indicators"] = []
        errors.append(f"indicators: {e}")
    sleep_between()

    for key, kind, item_map in (
        ("income", "综合损益表", US_INCOME_MAP),
        ("balance", "资产负债表", US_BALANCE_MAP),
        ("cashflow", "现金流量表", US_CASHFLOW_MAP),
    ):
        try:
            result[key] = fetch_us_report(code, kind, item_map)
        except Exception as e:
            result[key] = []
            errors.append(f"{key}: {e}")
        sleep_between()

    try:
        result["snapshot"] = fetch_us_snapshot(code)
    except Exception as e:
        result["snapshot"] = {}
        errors.append(f"snapshot: {e}")
    sleep_between()

    result["dividends"] = []  # 美股无分红接口
    result["reports"] = []  # 美股无定期报告接口
    result["errors"] = errors if errors else None
    return result


def _div_year_label(report_date: str):
    """'2025-12-31' → ('2025年报', '年度分红')：对齐巨潮的 year/type 口径。"""
    d = str(report_date or "")
    yy, mm = d[:4], d[5:7]
    suffix = {"12": "年报", "06": "半年报", "03": "一季报", "09": "三季报"}.get(mm, "")
    return f"{yy}{suffix}", ("年度分红" if mm == "12" else "中期分红")


def _dividends_cninfo(code: str):
    """巨潮分红历史（送股/转增/派息比例 + 关键日期），按公告日期倒序"""
    df = ak.stock_dividend_cninfo(symbol=code)
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "year": str(row["报告时间"]).strip(),
                "type": str(row["分红类型"]).strip(),
                "announce_date": iso_or_none(row["实施方案公告日期"]),
                "record_date": iso_or_none(row["股权登记日"]),
                "ex_date": iso_or_none(row["除权日"]),
                "pay_date": iso_or_none(row["派息日"]),
                "bonus_per_10": parse_number(row["派息比例"]),
                "transfer_per_10": parse_number(row["转增比例"]),
                "description": str(row["实施方案分红说明"]).strip(),
            }
        )
    records.sort(key=lambda r: r["announce_date"] or "", reverse=True)
    return records


def _dividends_em(code: str):
    """东财分红送配兜底（巨潮 403 限流时用）；只留已实施方案，字段对齐巨潮记录。"""
    df = ak.stock_fhps_detail_em(symbol=code)
    if df is None or df.empty:
        return []
    records = []
    for _, row in df.iterrows():
        progress = str(row.get("方案进度") or "")
        if "实施" not in progress:
            continue          # 预案/中止/未通过不入历史（无除权日即未落地）
        report = to_iso(row.get("报告期")) or ""
        year, dtype = _div_year_label(report)
        ex = iso_or_none(row.get("除权除息日"))
        announce = iso_or_none(row.get("最新公告日期")) or iso_or_none(row.get("预案公告日")) or ex
        records.append(
            {
                "year": year,
                "type": dtype,
                "announce_date": announce,
                "record_date": iso_or_none(row.get("股权登记日")),
                "ex_date": ex,
                "pay_date": ex,       # A 股派息日通常与除权日同日
                "bonus_per_10": parse_number(row.get("现金分红-现金分红比例")),
                "transfer_per_10": parse_number(row.get("送转股份-转股比例")),
                "description": str(row.get("现金分红-现金分红比例描述") or "").strip(),
            }
        )
    records.sort(key=lambda r: r["announce_date"] or "", reverse=True)
    return records


def fetch_dividends(code: str):
    """分红历史：巨潮优先，限流/异常时回退东财分红送配接口"""
    if cninfo_available():
        try:
            recs = _dividends_cninfo(code)
            cninfo_note(True)
            return recs
        except Exception:
            cninfo_note(False)
    return _dividends_em(code)


# 事务所名称前的常见修饰语（聘任表格文本：本次变更业经/续聘/已由等）
FIRM_NOISE = (
    "本次变更业经", "变更业经", "业经", "已由", "本次", "变更",
    "续聘", "改聘", "聘请", "聘任", "拟聘", "公司", "经", "由",
)

# 会计师事务所全称（名称 2-15 字 + 可选（特殊普通合伙）/（普通合伙）后缀，内部允许空白）
FIRM_RE = r"([\u4e00-\u9fa5]{2,15}?会计师事务所(?:[（(]\s*(?:特殊普通|普通)?\s*合伙\s*[）)])?)"


def clean_firm(raw: str):
    """去掉事务所名称前的修饰语，如「本次变更业经立信会计师事务所」→「立信会计师事务所」"""
    raw = raw.strip()
    m = re.search(FIRM_RE, raw)
    if m:
        raw = m.group(1)
    idx = 0
    for w in FIRM_NOISE:
        p = raw.find(w)
        if p >= 0:
            idx = max(idx, p + len(w))
    return raw[idx:].strip() if idx else raw


def first_firm(text: str):
    """提取会计师事务所名称：优先沪市「境内会计师事务所名称」/深市「审计机构名称」表格，
    兜底全文首个带（特殊普通合伙）后缀的全称。"""
    for anchor in ("境内会计师事务所名称", "审计机构名称"):
        m = re.search(anchor + r"\s*\n?\s*" + FIRM_RE, text)
        if m:
            return clean_firm(m.group(1))
    m = re.search(FIRM_RE, text)
    return clean_firm(m.group(1)) if m else None


def has_real_retention(text: str) -> bool:
    """判断是否真的为保留意见（排除「标准(的)无保留意见」中的子串误命中）。"""
    t = text
    for kw in ("标准的无保留意见", "标准无保留意见", "带强调事项段的无保留意见", "无保留意见"):
        t = t.replace(kw, "")
    return "保留意见" in t


def classify_opinion(text: str):
    """全文关键词精判审计意见类型。

    注意：「无保留意见」包含子串「保留意见」，必须先排除「无」前缀。
    """
    if "无法表示意见" in text:
        return "无法表示意见"
    if "否定意见" in text:
        return "否定意见"
    if "带强调事项段" in text or "带持续经营重大不确定性事项段" in text:
        return "带强调事项段的无保留意见"
    if has_real_retention(text):
        return "保留意见"
    if "标准的无保留意见" in text or "标准无保留意见" in text:
        return "标准无保留意见"
    if "无保留意见" in text:
        return "无保留意见"
    return None


def extract_audit(text: str, is_annual: bool = False):
    """从年报/半年报 PDF 文本提取审计信息（会计师事务所 + 审计意见）。

    深市披露表：「审计机构名称」「审计意见类型」；
    沪市表格：「境内会计师事务所名称」+ 董事会「非标准意见审计报告」说明
    （√/☑不适用 = 标准无保留意见，√/☑适用 = 非标准意见 → 转关键词精判）。
    以审计报告正文标志「我们审计了」判定是否真的审计：
    半年报未审计时（仅“聘任会计师事务所”表格），一律返回空，避免误导。
    """
    if re.search(r"我们\s*审\s*计\s*了", text) is None:
        # 年报理论必有审计报告；扫描版/解析失败时保守返回空
        return None, None

    # 1) 会计师事务所名称
    firm = first_firm(text)

    # 2) 审计意见类型
    opinion = None
    m = re.search(r"非标准意见审计报告.{0,200}?说明.{0,80}?[√☑✓]\s*不适用", text, re.S)
    if m:
        opinion = "标准无保留意见"
    else:
        m = re.search(r"非标准意见审计报告.{0,200}?说明.{0,80}?[√☑✓]\s*适用", text, re.S)
        if m:
            opinion = classify_opinion(text)
        else:
            m = re.search(r"审计意见类型\s*\n?\s*([^\n]{0,18})", text)
            if m:
                v = m.group(1).strip()
                if "无法表示意见" in v:
                    opinion = "无法表示意见"
                elif "否定意见" in v:
                    opinion = "否定意见"
                elif has_real_retention(v):
                    opinion = "保留意见"
                elif "标准的无保留意见" in v or "标准无保留意见" in v:
                    opinion = "标准无保留意见"
                elif "无保留意见" in v:
                    opinion = "无保留意见"
            else:
                opinion = classify_opinion(text)
    # 有审计报告且未检出非标准信号 → 标准无保留意见
    if opinion is None:
        opinion = "标准无保留意见"
    return firm, opinion


# 审计 PDF 解析范围（全市场扩量开关）：审计意见只供详情页展示,不参与评分,
# 而每份年报/半年报 PDF 达几 MB——它是全量抓取的最大耗时项。子进程走 spawn 不继
# 承父进程全局变量,故由主进程写入环境变量后再读。
AUDIT_SCOPES = {"full": ("年报", "半年报"), "annual": ("年报",), "none": ()}
AUDIT_SCOPE = os.getenv("VA_AUDIT_SCOPE", "full")


def fetch_audit(code: str, reports: list, scope: str = None):
    """解析定期报告 PDF 的审计信息（事务所 + 意见类型），写回 reports 条目。

    仅年报/半年报可能附审计报告（季报不审计，保持为空）；
    已解析且 PDF 链接未变化的条目直接复用旧 JSON 缓存，避免重复下载。
    返回失败下载数（网络抖动时由调用方记录到 errors）。
    """
    wanted = AUDIT_SCOPES.get(scope or AUDIT_SCOPE, AUDIT_SCOPES["full"])
    if not wanted:
        return 0
    old = {}
    try:
        prev = json.loads((COMPANIES_DIR / f"{code}.json").read_text(encoding="utf-8"))
        for r in prev.get("reports") or []:
            if r.get("audit_firm") or r.get("audit_opinion"):
                old[r.get("pdf_url")] = {
                    "audit_firm": r.get("audit_firm"),
                    "audit_opinion": r.get("audit_opinion"),
                }
    except (OSError, ValueError):
        pass
    headers = {"User-Agent": "Mozilla/5.0"}
    failed = 0
    for r in reports:
        if r["category"] not in wanted:
            continue
        cached = old.get(r.get("pdf_url"))
        if cached:
            r.update(cached)
            continue
        ok = False
        for attempt in range(2):  # 瞬时网络失败自动重试一次
            try:
                resp = requests.get(r["pdf_url"], headers=headers, timeout=90)
                if resp.status_code == 404:
                    # 巨潮归档已移除/更换该 PDF，跳过且不计失败（不影响财务数据）
                    ok = True
                    break
                resp.raise_for_status()
                doc = pymupdf.open(stream=resp.content, filetype="pdf")
                text = "".join(page.get_text() for page in doc)
                doc.close()
                firm, opinion = extract_audit(text, r["category"] == "年报")
                r["audit_firm"] = firm
                r["audit_opinion"] = opinion
                ok = True
                break
            except Exception:
                time.sleep(2)
        if not ok:
            failed += 1
            r["audit_firm"] = None
            r["audit_opinion"] = None
    return failed


# ==================== 定期报告：巨潮 + 东财公告双源 ====================
# 巨潮 hisAnnouncement 对高频抓取返回 HTTP 403（正文空 → JSON 解析失败），而定期报告
# 是审计意见的唯一来源，单源断供会让全市场 5500 只的审计字段全空。故：
#   1) 进程内熔断：连续失败 5 次 → 冷却 10 分钟内直接走东财，不再白打巨潮；
#   2) 东财公告接口兜底（np-anotice-stock 列公告 + pdf.dfcfw.com 取正文 PDF）。
EM_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EM_PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf"
EM_DETAIL_URL = "https://data.eastmoney.com/notices/detail/{code}/{art}.html"
_CNINFO_FAILS = 0
_CNINFO_COOLDOWN_UNTIL = 0.0


def cninfo_available():
    """巨潮是否值得再试（熔断冷却期内为 False）。"""
    return time.time() >= _CNINFO_COOLDOWN_UNTIL


def cninfo_note(ok: bool):
    """记录巨潮成败：连续 5 次失败即熔断 10 分钟（全市场抓取时避免逐只空转）。"""
    global _CNINFO_FAILS, _CNINFO_COOLDOWN_UNTIL
    if ok:
        _CNINFO_FAILS = 0
        return
    _CNINFO_FAILS += 1
    if _CNINFO_FAILS >= 5:
        _CNINFO_COOLDOWN_UNTIL = time.time() + 600
        _CNINFO_FAILS = 0


def em_category(title: str):
    """东财公告标题 → 定期报告类别；非定期报告正文返回 None（对齐巨潮类别名）。"""
    t = re.sub(r"\s", "", str(title))
    if "摘要" in t or "提示" in t or "公告" in t or "意见" in t or "决议" in t:
        return None
    if not re.search(r"报告(?:[（(](?:更正|更新后)[）)])?$", t):
        return None
    if "半年度报告" in t or "中期报告" in t:
        return "半年报"
    if "第一季度报告" in t or "一季度报告" in t:
        return "一季报"
    if "第三季度报告" in t or "三季度报告" in t:
        return "三季报"
    if "年度报告" in t or "年报" in t:
        return "年报"
    return None


def fetch_reports_em(code: str, start_iso: str):
    """东财公告兜底：分页拉公司公告，按标题识别定期报告（翻到 3 年前即停）。"""
    headers = {"User-Agent": "Mozilla/5.0"}
    out, page = [], 1
    while page <= 6:
        try:
            r = requests.get(
                EM_ANN_URL,
                params={
                    "sr": "-1", "page_size": "100", "page_index": str(page),
                    "ann_type": "A", "client_source": "web",
                    "stock_list": code, "f_node": "0", "s_node": "0",
                },
                headers=headers, timeout=15,
            )
            rows = (r.json().get("data") or {}).get("list") or []
        except Exception:
            rows = []
        if not rows:
            break
        for row in rows:
            date = str(row.get("notice_date") or "")[:10]
            cat = em_category(row.get("title") or "")
            art = str(row.get("art_code") or "")
            if not (cat and date and art) or date < start_iso:
                continue
            out.append({
                "title": str(row["title"]).strip(),
                "category": cat,
                "date": date,
                "pdf_url": EM_PDF_URL.format(art=art),
                "detail_url": EM_DETAIL_URL.format(code=code, art=art),
            })
        # 公告按时间倒序：末页已早于 3 年窗口（或无下一页）即停
        oldest = str(rows[-1].get("notice_date") or "")[:10]
        if len(rows) < 100 or oldest < start_iso:
            break
        page += 1
    return out


def fetch_reports(code: str):
    """定期报告列表（官方披露 PDF 直链），按日期倒序：巨潮优先，东财公告兜底"""
    today = datetime.now()
    start = (today - timedelta(days=365 * 3)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    start_iso = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    reports = []
    if cninfo_available():
        failed = 0
        for cat in REPORT_CATEGORIES:
            try:
                df = ak.stock_zh_a_disclosure_report_cninfo(
                    symbol=code,
                    category=cat,
                    start_date=start,
                    end_date=end,
                )
                cninfo_note(True)
            except Exception:
                failed += 1
                cninfo_note(False)
                continue
            if df is None or df.empty:
                continue
            for _, row in df.head(MAX_PERIODS).iterrows():
                title = str(row["公告标题"]).strip()
                if "摘要" in title:
                    continue
                date = str(row["公告时间"])[:10]
                detail = str(row["公告链接"])
                try:
                    aid = detail.split("announcementId=")[1].split("&")[0]
                except IndexError:
                    continue
                reports.append(
                    {
                        "title": title,
                        "category": cat,
                        "date": date,
                        "pdf_url": f"http://static.cninfo.com.cn/finalpage/{date}/{aid}.PDF",
                        "detail_url": detail,
                    }
                )
        # 四类全挂（多为 403 限流）才走兜底；巨潮正常返回空（次新股）不重复抓
        if not reports and failed == len(REPORT_CATEGORIES):
            reports = fetch_reports_em(code, start_iso)
    else:
        reports = fetch_reports_em(code, start_iso)
    reports.sort(key=lambda r: r["date"], reverse=True)
    return reports[: MAX_PERIODS * 2]


def fetch_company(code: str, name: str, market: str = "A"):
    """按市场分流：A 股走同花顺/新浪/巨潮，港股走东财港股接口，美股走东财美股接口"""
    if market == "HK":
        return fetch_company_hk(code, name)
    if market == "US":
        return fetch_company_us(code, name)
    return fetch_company_a(code, name)


def fetch_company_a(code: str, name: str):
    """抓取 A 股单家公司全部数据，失败项单独降级，不中断"""
    result = {"code": code, "name": name, "market": "A"}
    result["updated_at"] = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    errors = []

    try:
        result["info"] = fetch_info(code)
    except Exception as e:
        result["info"] = {}
        errors.append(f"info: {e}")
    sleep_between()

    try:
        result["indicators"] = fetch_indicators(code)
    except Exception as e:
        result["indicators"] = []
        errors.append(f"indicators: {e}")
    sleep_between()

    for key, kind in (
        ("income", "利润表"),
        ("balance", "资产负债表"),
        ("cashflow", "现金流量表"),
    ):
        try:
            result[key] = fetch_report(code, kind)
        except Exception as e:
            result[key] = []
            errors.append(f"{key}: {e}")
        sleep_between()

    try:
        result["snapshot"] = fetch_snapshot(code)
    except Exception as e:
        result["snapshot"] = {}
        errors.append(f"snapshot: {e}")
    sleep_between()

    try:
        result["dividends"] = fetch_dividends(code)
    except Exception as e:
        result["dividends"] = []
        errors.append(f"dividends: {e}")
    sleep_between()

    try:
        result["reports"] = fetch_reports(code)
    except Exception as e:
        result["reports"] = []
        errors.append(f"reports: {e}")

    # 审计信息：解析年报/半年报 PDF（事务所 + 意见类型），失败不中断
    try:
        failed = fetch_audit(code, result["reports"])
        if failed:
            errors.append(f"audit: {failed} 份报告 PDF 解析失败（下次抓取自动重试）")
    except Exception as e:
        errors.append(f"audit: {e}")

    result["errors"] = errors if errors else None
    return result


def save_json(path: Path, data):
    """原子写入：先写临时文件再替换，避免半截文件被 Pages 读取。紧凑格式压缩体积（约减半）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp, path)


# ==================== 全市场扩量支持 [collector 改造] ====================
# index.json 既服务列表页也快充当“行情快照载体”：每日仅刷估值时只改 index.json
# 的 quote/price 字段,不重写 companies/*.json(5000+ 只 ≈ 1.6GB),使 --resume 的
# 文件 mtime 判新仍然代表“财务数据新鲜度”。

INDEX_PATH = OUTPUT_DIR / "index.json"
LOCK_PATH = OUTPUT_DIR / ".fetch.lock"
LOCK_STALE_SECS = 45 * 60      # 持锁进程死掉后超过此时长才可接手
QUOTE_FIELDS = ("price", "change_pct", "pe_ttm", "pb", "market_cap",
                "float_market_cap", "turnover_rate", "time")


def _pid_alive(pid: int) -> bool:
    """跨平台判活。

    Windows 上不要用 os.kill(pid, 0)：它走 OpenProcess,句柄拿不到时抛 WinError 87,
    且 CPython 会把它包装成 SystemError(非 OSError 子类)逃逸 except 分支。
    这里直接调 kernel32.OpenProcess 查退出码,拿不到句柄即视为已死。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            # GetExitCodeProcess 失败时 code 未定义,保守按存活(避免误抢占锁)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock():
    """单一抓取入口互斥：index.json 是全量重写,两个进程并行会互相覆盖条目。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        pid, age = 0, 0.0
        try:
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
            age = time.time() - LOCK_PATH.stat().st_mtime
        except (ValueError, OSError):
            pass
        if _pid_alive(pid) and age < LOCK_STALE_SECS:
            print(f"[lock] 已有抓取在跑（PID {pid}，持锁 {age / 60:.0f} 分钟），本次退出；"
                  f"确认无进程可删 data/.fetch.lock", flush=True)
            sys.exit(2)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def load_index_map():
    """读现有 index.json → {code|market: entry}（损坏/缺失时返回空）。"""
    if not INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {f"{c['code']}|{c.get('market') or 'A'}": c
            for c in data.get("companies") or [] if c.get("code")}


def save_index(by_code):
    """全量重写 index.json：A 股在前、按代码升序，每 N 只调用一次中断不丢。"""
    items = sorted(by_code.values(),
                   key=lambda x: (x.get("market") != "A", str(x.get("code"))))
    now = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    save_json(INDEX_PATH, {"updated_at": now, "count": len(items), "companies": items})
    try:
        LOCK_PATH.touch()        # 顺带刷锁,供并发入口区分“活锁”与残留锁
    except OSError:
        pass
    return items


def fresh_codes(max_age_days):
    """companies/ 下 mtime 在 max_age_days 内的代码集（--resume 跳过集）。"""
    cutoff = time.time() - max_age_days * 86400
    if not COMPANIES_DIR.exists():
        return set()
    out = set()
    for path in COMPANIES_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime >= cutoff:
                out.add(path.stem)
        except OSError:
            pass
    return out


def _quote_of(snapshot):
    """snapshot 字典 → index 条目用的精简行情块（只留入库字段，控制文件体积）。"""
    snap = snapshot or {}
    q = {k: snap.get(k) for k in QUOTE_FIELDS if snap.get(k) is not None}
    return q or None


def build_entry(code, name, market, data, scores):
    snap = data.get("snapshot") or {}
    return {
        "code": code,
        "name": name,
        "market": market,
        "industry": (data.get("info") or {}).get("行业"),
        "price": snap.get("price"),
        "updated_at": data["updated_at"],
        "scores": scores,
        "quote": _quote_of(snap),
    }


def guess_market(code, data, pool):
    """判定 companies/<code>.json 属于哪个市场。

    优先读 JSON 自带的 market（三个 fetch_company_* 都会写）；落到 config 精选池；
    再落到代码形态——5 位纯数字必是港股（A 股一律 6 位）。
    只看 config 不够：--hk-connect 灌进来的 621 只不在精选池里，会被标成 A 股，
    于是港元报表进 A 股池，列表页与筛选全部串味。
    """
    m = (data or {}).get("market")
    if m:
        return m
    if code in pool:
        return pool[code]
    if code.isdigit() and len(code) == 5:
        return "HK"
    if not code.isdigit():
        return "US"
    return "A"


def backfill_index(by_code):
    """companies/ 里已落盘但 index.json 无条目的股票（上次被抓断/ --resume 跳过）
    → 从 JSON 重建条目，避免部分完成的抓取在索引里默默丢股。"""
    pool = {c[0]: c[2] for c in DEFAULT_COMPANIES if len(c) >= 3}
    # 只把“已有且带行情块”的条目视为完整：早期抓取写的条目没有 quote，
    # 回读明细补齐后 import_legacy 才能拿到真实交易快照日期
    have = {k.split("|")[0] for k, v in by_code.items() if v.get("quote")}
    added = 0
    for path in sorted(COMPANIES_DIR.glob("*.json")):
        code = path.stem
        if code in have:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        market = guess_market(code, data, pool)
        try:
            scores = compute_scores(data)
        except Exception:
            scores = None
        key = f"{code}|{market}"
        by_code[key] = build_entry(code, data.get("name") or code, market, data, scores)
        added += 1
    return added


_CODE_MARKETS = ("A", "HK", "US")


def normalize_code(raw, market):
    """按市场规整代码长度——这一步弄错会写掉别人的文件。

    A 股 6 位、港股 5 位、美股原样大写。港股若跟着 zfill(6)，00700 会变 000700，
    直接覆盖 A 股「模塑科技」的 companies/000700.json——入库侧有 uk_code_market 挡着，
    文件侧没有任何保护。
    """
    if market == "US":
        return raw.upper()
    if market == "HK":
        return raw.zfill(5)
    return raw.zfill(6)


def resolve_codes(spec, default_market="A"):
    """--codes 增量补抓目标准：支持 "HK.00700" 前缀混写，或靠 --codes-market 整批指定市场。

    名称优先取已落盘 JSON（补抓场景本来就有文件），其次查 A 股/港股通名单，最后回退代码。
    """
    import universe as uni

    a_names = hk_names = us_names = None
    out = []
    for raw in str(spec).split(","):
        raw = raw.strip()
        if not raw:
            continue
        market = (default_market or "A").upper()
        head, sep, tail = raw.partition(".")
        if sep and head.upper() in _CODE_MARKETS:
            market, raw = head.upper(), tail.strip()
        code = normalize_code(raw, market)
        name = None
        path = COMPANIES_DIR / f"{code}.json"
        if path.exists():
            try:
                name = json.loads(path.read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError):
                name = None
        if not name:
            try:
                if market == "HK":
                    if hk_names is None:
                        hk_names = {c: n for c, n, _ in uni.load_universe_hk(quiet=True)}
                    name = hk_names.get(code)
                elif market == "US":
                    if us_names is None:
                        us_names = {c: n for c, n, _, _ in uni.load_universe_us(quiet=True)}
                    # 手写 US.BRK.B 时归位到名单里的东财形态 BRK_B，
                    # 否则会另存一份 companies/BRK.B.json，同一家公司两份数据
                    for cand in us_code_variants(code):
                        if cand in us_names:
                            code, name = cand, us_names[cand]
                            break
                else:
                    if a_names is None:
                        a_names = dict(uni.load_universe(quiet=True))
                    name = a_names.get(code)
            except Exception as e:
                print(f"[universe] 名单不可用: {e!r}", flush=True)
        out.append((code, name or code, market))
    return out


def us_index_targets(quiet=False):
    """美股五大指数成分并集（≈765 只）→ (code, name, "US")；行业由 fetch_company_us 查名单缓存补齐。"""
    import universe as uni
    try:
        rows = uni.load_universe_us(quiet=quiet)
    except Exception as e:
        print(f"[universe] 美股成分名单不可用: {e!r}", flush=True)
        return []
    return [(c, n, "US") for c, n, _, _ in rows]


def hk_connect_targets(quiet=False):
    """港股通标的（≈621 只）→ (code, name, "HK")；行业由 fetch_company_hk 查名单缓存补齐。"""
    import universe as uni
    try:
        rows = uni.load_universe_hk(quiet=quiet)
    except Exception as e:
        print(f"[universe] 港股通名单不可用: {e!r}", flush=True)
        return []
    return [(c, n, "HK") for c, n, _ in rows]


def resolve_targets(args):
    """目标准：--codes > --hk-connect(港股通) / --us-indexes(美股成分) / --all-market(全 A) > config 精选池。

    --hk-connect / --us-indexes 与 --all-market 可同给，合为一轮跑完，省一次 index.json 全量重写。
    """
    if args.codes:
        return resolve_codes(args.codes, args.codes_market)

    targets = []
    if args.hk_connect:
        targets += hk_connect_targets(quiet=args.quiet)
    if args.us_indexes:
        targets += us_index_targets(quiet=args.quiet)
    if args.all_market:
        try:
            import universe as uni
            names = dict(uni.load_universe(quiet=True))
        except Exception as e:
            print(f"[universe] 名单不可用: {e!r}", flush=True)
            names = {}
        targets += [(c, n, "A") for c, n in sorted(names.items())]
        # 非 A 股精选（港/美）不在交易所名单里，顺带刷新一遍
        targets += [tuple(i) for i in DEFAULT_COMPANIES if len(i) >= 3 and i[2] != "A"]
    if targets:
        # 去重：--all-market 附带的 5 只港股精选与 --hk-connect 名单重叠，
        # 留着会让同一只在同一轮里抓两次、后一次还可能覆盖前一次的成品
        seen, uniq = set(), []
        for t in targets:
            if (t[0], t[2]) in seen:
                continue
            seen.add((t[0], t[2]))
            uniq.append(t)
        return uniq

    targets = [tuple(i) if len(i) >= 3 else (i[0], i[1], "A") for i in DEFAULT_COMPANIES]
    if args.limit > 0:
        targets = targets[: args.limit]
    return targets


def crawl_one(item):
    """单只：抓取 → 落盘 → 评分 → index 条目（异常上抛由 safe 层捕获）。"""
    code, name, market = item
    data = fetch_company(code, name, market)
    save_json(COMPANIES_DIR / f"{code}.json", data)
    # 预计算四大流派总分（与前端 JS 一致性由 scripts/_score_check.py 验证）
    try:
        scores = compute_scores(data)
    except Exception:
        scores = None
    return build_entry(code, name, market, data, scores), (data.get("errors") or [])


def _safe_crawl(item):
    code, name, market = item
    t0 = time.time()
    try:
        entry, errs = crawl_one(item)
        return code, entry, errs, time.time() - t0, None
    except Exception:
        return code, None, [], time.time() - t0, traceback.format_exc(limit=3)


def run_snapshot_only(args):
    """仅刷估值快照：腾讯批量行情 → index.json 的 quote/price（分钟级日更）。"""
    import universe as uni

    by_code = load_index_map()
    if not by_code:
        print("[snapshot-only] index.json 为空，请先跑一次全量抓取")
        return 1
    codes = sorted({key.split("|")[0] for key in by_code
                    if key.endswith("|A")})
    print(f"[snapshot-only] 腾讯批量刷新 {len(codes)} 只 A 股估值 ...", flush=True)
    spot = uni.tencent_spot(codes, quiet=args.quiet)
    by_num = {key.split("|")[0]: key for key in by_code if key.endswith("|A")}
    hit = 0
    now = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    for code, snap in spot.items():
        if not snap.get("price"):
            continue                      # 停牌/退市无价：保留旧快照，不抹空
        key = by_num.get(code)
        if not key:
            continue
        entry = by_code[key]
        entry["price"] = snap.get("price")
        entry["quote"] = _quote_of(snap)
        entry["quote_at"] = now
        hit += 1
    items = save_index(by_code)
    print(f"[snapshot-only] 行情更新 {hit}/{len(codes)} 只，index 共 {len(items)} 条")
    return 0


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def crawl_targets(targets, args, consume, flush):
    """抓取执行器：workers<=1 串行；否则分块起进程池。

    为何是进程而不是线程：akshare 部分接口用 py_mini_racer（内嵌 V8），
    多线程并发初始化会触发 Chromium FATAL:partition_address_space.cc 直接搞挂解释器；
    分块建池（每块一个新进程组）同时做到崩溃隔离与内存有界。
    """
    workers = max(1, args.workers)
    if workers <= 1:
        for item in targets:
            consume(_safe_crawl(item))
        return
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    ctx = mp.get_context("spawn")
    size = max(args.chunk, workers)
    for ci, group in enumerate(_chunks(targets, size), 1):
        started = time.time()
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            try:
                for res in pool.map(_safe_crawl, group, chunksize=1):
                    consume(res)
                continue
            except (BrokenProcessPool, EOFError, OSError, RuntimeError) as e:
                print(f"  [进程池中断] 第 {ci} 块: {e!r} → 本块改串行续跑", flush=True)
        # 子进程被杀：本块未落盘的串行补跑（已抓完的按文件 mtime 跳过）
        for item in group:
            path = COMPANIES_DIR / f"{item[0]}.json"
            if path.exists() and path.stat().st_mtime >= started:
                continue
            consume(_safe_crawl(item))
        flush()


def main():
    parser = argparse.ArgumentParser(description="抓取 A 股财务数据 → 静态 JSON")
    parser.add_argument("--limit", type=int, default=0, help="最多抓取的公司数（测试用）")
    parser.add_argument("--all-market", action="store_true",
                        help="全 A 股（沪深京）名单，来自 universe.py")
    parser.add_argument("--hk-connect", action="store_true",
                        help="港股通标的（≈621 只，名单来自 universe.load_universe_hk，可与 --all-market 同给）")
    parser.add_argument("--us-indexes", action="store_true",
                        help="美股五大指数成分并集（≈765 只：标普500/纳指100/道指/费半/NBI，"
                             "名单来自 universe.load_universe_us，可与 --all-market/--hk-connect 同给）")
    parser.add_argument("--codes", help="逗号分隔代码，只抓这些（增量补抓）")
    parser.add_argument("--codes-market", choices=list(_CODE_MARKETS), default="A",
                        help="--codes 的市场；代码写成 HK.00700 时可免填")
    parser.add_argument("--workers", type=int, default=1, help="并发拓取进程数（建议 4）")
    parser.add_argument("--chunk", type=int, default=400,
                        help="每个进程池处理的股票数（崩溃隔离与内存有界粒度）。注意这是**静态切块**："
                             "一个工人领完一块才领下一块，块太大时多数工人会提前空等、尾巴压在少数人身上"
                             "（看起来就像“卡死”）；跑尾段时建议 60-100")
    parser.add_argument("--resume", action="store_true",
                        help="跳过 companies/ 里仍新鲜的股票（断点续跑）")
    parser.add_argument("--max-age", type=float, default=1.0,
                        help="--resume 的新鲜度阈值（天），全量重抓可设 6")
    parser.add_argument("--shard", help="i/N 分片，如 2/4 只抓第 2 片（多进程并行）")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="仅刷全市场估值快照，不重抓财务")
    parser.add_argument("--flush-every", type=int, default=50,
                        help="每完成 N 只重写一次 index.json")
    parser.add_argument("--audit-scope", choices=list(AUDIT_SCOPES), default="full",
                        help="审计 PDF 解析范围：full=年报+半年报 annual=仅年报 none=跳过"
                             "（全市场首轮建议 annual）")
    parser.add_argument("--quiet", action="store_true", help="只打总结行")
    args = parser.parse_args()

    global AUDIT_SCOPE
    AUDIT_SCOPE = args.audit_scope
    os.environ["VA_AUDIT_SCOPE"] = args.audit_scope   # spawn 子进程靠环境继承

    acquire_lock()     # index.json 全量重写不能并发,抢不到锁直接退出
    try:
        if args.snapshot_only:
            sys.exit(run_snapshot_only(args))
        crawl_all(args)
    finally:
        release_lock()


def crawl_all(args):
    """抓取主流程（已持锁）：定单 → 并发抓取 → 边跑边落 index.json。"""
    targets = resolve_targets(args)
    if args.shard:
        idx, _, total = args.shard.partition("/")
        idx, total = int(idx), int(total or 1)
        targets = targets[idx - 1::total]
    if args.limit > 0:
        targets = targets[: args.limit]

    skip = fresh_codes(args.max_age) if args.resume else set()
    if skip:
        before = len(targets)
        targets = [t for t in targets if t[0] not in skip]
        print(f"[resume] 跳过 {before - len(targets)} 只新鲜数据", flush=True)
    if not targets:
        print("无待抓标的")
        return

    workers = max(1, args.workers)
    print(f"[{datetime.now(CN_TZ).strftime('%F %T')}] 开始抓取 {len(targets)} 家公司"
          f"（workers={workers}）...", flush=True)

    by_code = load_index_map()
    fixed = backfill_index(by_code)
    if fixed:
        print(f"[index] 从已落盘 JSON 补齐 {fixed} 条缺失条目", flush=True)
    failed = []
    done = ok = part = 0
    t0 = time.time()

    def consume(result):
        nonlocal done, ok, part
        code, entry, errs, el, err = result
        done += 1
        if entry:
            key = f"{entry['code']}|{entry['market']}"
            by_code[key] = entry
            if errs:
                part += 1
            else:
                ok += 1
        else:
            failed.append(code)
        if not args.quiet or done % 200 == 0 or err:
            eta = (time.time() - t0) / done * (len(targets) - done) / 3600
            head = code + " " + (entry["name"] if entry else "")
            mark = "失败" if err else ("部分失败" if errs else "完成")
            print(f"  [{done}/{len(targets)}] {head} {mark} {el:.0f}s"
                  f"  ok={ok} part={part} fail={len(set(failed))} ETA={eta:.1f}h", flush=True)
        if err:
            print(f"      {err.strip().splitlines()[-1][:160]}", flush=True)
        if args.flush_every and done % args.flush_every == 0:
            save_index(by_code)

    crawl_targets(targets, args, consume, lambda: save_index(by_code))

    items = save_index(by_code)
    secs = time.time() - t0
    if failed:
        save_json(OUTPUT_DIR / "crawl_failed.json", sorted(set(failed)))
    print(f"完成：本次 {done} 只 ok={ok} 部分失败={part} 异常={len(set(failed))}，"
          f"耗时 {secs / 3600:.1f}h；index 共 {len(items)} 条")
    if failed:
        print(f"失败列表已写 data/crawl_failed.json（{len(set(failed))} 只）")
    # 全量跑允许少量失败；异常率 >20% 视为数据源异常，非零退出供调度记录
    if done and len(set(failed)) / done > 0.2:
        sys.exit(1)


if __name__ == "__main__":
    main()
