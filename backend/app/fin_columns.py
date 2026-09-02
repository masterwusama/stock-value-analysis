# -*- coding: utf-8 -*-
"""财务报表中文科目 ↔ 实体列映射(导入与 API 共用,单一事实来源)。

约定:每列对应一组候选中文键,按优先顺序取第一个非空;
extras JSON 全量保留原始行(含中文原键名),实体核心列仅作 SQL 查询索引——
还原时无损且无键名歧义(如港股 balance 用"股本"而 A 股用"实收资本(或股本)")。
"""

# fin_indicator 核心列(同花顺摘要指标)
INDICATOR_CORE = {
    "revenue": ["营业总收入"],
    "net_profit": ["净利润"],
    "eps": ["基本每股收益"],
    "bps": ["每股净资产"],
    "gross_margin": ["销售毛利率"],
    "net_margin": ["销售净利率"],
    "roe": ["净资产收益率"],
    "current_ratio": ["流动比率"],
    "quick_ratio": ["速动比率"],
    "debt_ratio": ["资产负债率"],
    "ocf_per_share": ["每股经营现金流"],
    "revenue_q": ["营业总收入_单季"],
    "net_profit_q": ["净利润_单季"],
}

INCOME_CORE = {
    "revenue_total": ["营业总收入"],
    "operating_profit": ["营业利润"],
    "net_profit": ["净利润"],
    "deducted_net_profit": ["扣除非经常性损益后的净利润", "扣非净利润"],
}

BALANCE_CORE = {
    "total_assets": ["资产总计"],
    "cash": ["货币资金"],
    "trading_fin_assets": ["交易性金融资产"],
    "notes_receivable": ["应收票据"],
    "other_current_assets": ["其他流动资产"],
    "total_liabilities": ["负债合计"],
    "equity_parent": ["归属于母公司股东权益合计"],
    "equity_total": ["所有者权益(或股东权益)合计"],
    "paid_in_capital": ["实收资本(或股本)", "股本"],
    "short_loan": ["短期借款"],
    "long_loan": ["长期借款"],
    "bond_payable": ["应付债券"],
    "lease_liability": ["租赁负债"],
    "noncurrent_due_1y": ["一年内到期的非流动负债"],
}

CASHFLOW_CORE = {
    "ocf": ["经营活动产生的现金流量净额"],
    "capex": ["购建固定资产、无形资产和其他长期资产所支付的现金"],
    "cash_received_from_sales": ["销售商品、提供劳务收到的现金"],
}


def split_core(row: dict, core_map: dict):
    """中文科目行 → (核心列值 dict, extras dict)。

    extras 全量保留原始行(中文原键名无损),核心列仅作查询索引。
    """
    core = {}
    for col, keys in core_map.items():
        for k in keys:
            if k in row and row[k] is not None:
                core[col] = row[k]
                break
    return core, dict(row)


def restore_row(entity) -> dict:
    """DB 实体 → 原中文键行(extras 即原始行,直接还原)。"""
    return dict(entity.extras or {})
