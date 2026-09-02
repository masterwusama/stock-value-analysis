# -*- coding: utf-8 -*-
"""ORM 模型:db_va 全部 19 张表。

约定:
- security.sid 为内键,子表以 sid 外联(避免重复 (code, market) 复合键);
- 金额一律为元、比率一律为小数(与原 JSON 服务口径一致);
- 中文科目仅保留核心评分/展示所需实体列,长尾科目进 extras JSON 兜底;
- score_daily 为每日评分快照,列表筛选/排序/买卖点判断全部下推 SQL。
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Double,
    Enum,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    BigInteger,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Security(Base):
    """证券主表:全市场代码清单。"""

    __tablename__ = "security"

    sid: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    market: Mapped[str] = mapped_column(Enum("A", "HK", "US"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(
        Enum("active", "suspended", "delisted"), nullable=False, default="active"
    )
    list_date: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("code", "market", name="uk_code_market"),
        Index("idx_market_industry", "market", "industry"),
    )


class QuoteDaily(Base):
    """每日行情/估值快照(腾讯/东财口径,同原 snapshot)。"""

    __tablename__ = "quote_daily"

    sid: Mapped[int] = mapped_column(primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    change_pct: Mapped[float | None] = mapped_column(Double)  # 比率类计算值保真全精度
    pe_ttm: Mapped[float | None] = mapped_column(Double)
    pb: Mapped[float | None] = mapped_column(Double)
    market_cap: Mapped[float | None] = mapped_column(Numeric(20, 2))
    float_market_cap: Mapped[float | None] = mapped_column(Numeric(20, 2))
    turnover_rate: Mapped[float | None] = mapped_column(Double)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("idx_quote_date", "trade_date"),)


class FinIndicator(Base):
    """财务摘要指标(同花顺口径,报告期维度)。"""

    __tablename__ = "fin_indicator"

    sid: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(primary_key=True)
    revenue: Mapped[float | None] = mapped_column(Numeric(20, 2))
    net_profit: Mapped[float | None] = mapped_column(Numeric(20, 2))
    eps: Mapped[float | None] = mapped_column(Numeric(12, 6))
    bps: Mapped[float | None] = mapped_column(Numeric(12, 6))
    gross_margin: Mapped[float | None] = mapped_column(Double)
    net_margin: Mapped[float | None] = mapped_column(Double)
    roe: Mapped[float | None] = mapped_column(Double)
    current_ratio: Mapped[float | None] = mapped_column(Double)
    quick_ratio: Mapped[float | None] = mapped_column(Double)
    debt_ratio: Mapped[float | None] = mapped_column(Double)
    ocf_per_share: Mapped[float | None] = mapped_column(Numeric(12, 6))
    revenue_q: Mapped[float | None] = mapped_column(Numeric(20, 2))
    net_profit_q: Mapped[float | None] = mapped_column(Numeric(20, 2))
    extras: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FinIncome(Base):
    """利润表(报告日维度,核心科目实体列 + extras)。"""

    __tablename__ = "fin_income"

    sid: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(primary_key=True)
    revenue_total: Mapped[float | None] = mapped_column(Numeric(20, 2))
    operating_profit: Mapped[float | None] = mapped_column(Numeric(20, 2))
    net_profit: Mapped[float | None] = mapped_column(Numeric(20, 2))
    deducted_net_profit: Mapped[float | None] = mapped_column(Numeric(20, 2))
    extras: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FinBalance(Base):
    """资产负债表(净现金/清算价值等评分科目全部实体化)。"""

    __tablename__ = "fin_balance"

    sid: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(primary_key=True)
    total_assets: Mapped[float | None] = mapped_column(Numeric(20, 2))
    cash: Mapped[float | None] = mapped_column(Numeric(20, 2))
    trading_fin_assets: Mapped[float | None] = mapped_column(Numeric(20, 2))
    notes_receivable: Mapped[float | None] = mapped_column(Numeric(20, 2))
    other_current_assets: Mapped[float | None] = mapped_column(Numeric(20, 2))
    total_liabilities: Mapped[float | None] = mapped_column(Numeric(20, 2))
    equity_parent: Mapped[float | None] = mapped_column(Numeric(20, 2))
    equity_total: Mapped[float | None] = mapped_column(Numeric(20, 2))
    paid_in_capital: Mapped[float | None] = mapped_column(Numeric(20, 2))
    short_loan: Mapped[float | None] = mapped_column(Numeric(20, 2))
    long_loan: Mapped[float | None] = mapped_column(Numeric(20, 2))
    bond_payable: Mapped[float | None] = mapped_column(Numeric(20, 2))
    lease_liability: Mapped[float | None] = mapped_column(Numeric(20, 2))
    noncurrent_due_1y: Mapped[float | None] = mapped_column(Numeric(20, 2))
    extras: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class FinCashflow(Base):
    """现金流量表。"""

    __tablename__ = "fin_cashflow"

    sid: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(primary_key=True)
    ocf: Mapped[float | None] = mapped_column(Numeric(20, 2))
    capex: Mapped[float | None] = mapped_column(Numeric(20, 2))
    cash_received_from_sales: Mapped[float | None] = mapped_column(Numeric(20, 2))
    extras: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ScoreDaily(Base):
    """每日评分快照:四流派分/造假/管理/周期 + 价格参考 + Wind 事件增量。

    列表页的全部筛选与排序均在本表 + quote_daily 上 SQL 完成。
    """

    __tablename__ = "score_daily"

    sid: Mapped[int] = mapped_column(primary_key=True)
    trade_date: Mapped[date] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(nullable=False)
    score_graham_agg: Mapped[float | None] = mapped_column(Double)
    score_graham_def: Mapped[float | None] = mapped_column(Double)
    score_schloss: Mapped[float | None] = mapped_column(Double)
    score_buffett: Mapped[float | None] = mapped_column(Double)
    fraud: Mapped[float | None] = mapped_column(Double)
    mgmt: Mapped[float | None] = mapped_column(Double)
    cycle: Mapped[float | None] = mapped_column(Double)
    cyclical: Mapped[bool | None] = mapped_column(Boolean)
    cycle_trend: Mapped[str | None] = mapped_column(String(8))
    fair_liq: Mapped[float | None] = mapped_column(Double)
    net_cash_ratio: Mapped[float | None] = mapped_column(Double)
    net_cash_calc: Mapped[dict | None] = mapped_column(JSON)
    buy_graham_agg: Mapped[float | None] = mapped_column(Double)
    sell_cons_graham_agg: Mapped[float | None] = mapped_column(Double)
    sell_fair_graham_agg: Mapped[float | None] = mapped_column(Double)
    buy_graham_def: Mapped[float | None] = mapped_column(Double)
    sell_cons_graham_def: Mapped[float | None] = mapped_column(Double)
    sell_fair_graham_def: Mapped[float | None] = mapped_column(Double)
    buy_schloss: Mapped[float | None] = mapped_column(Double)
    sell_cons_schloss: Mapped[float | None] = mapped_column(Double)
    sell_fair_schloss: Mapped[float | None] = mapped_column(Double)
    buy_buffett: Mapped[float | None] = mapped_column(Double)
    sell_cons_buffett: Mapped[float | None] = mapped_column(Double)
    sell_fair_buffett: Mapped[float | None] = mapped_column(Double)
    wind_fraud_delta: Mapped[float | None] = mapped_column(Double)
    wind_mgmt_delta: Mapped[float | None] = mapped_column(Double)
    wind_flags: Mapped[list | None] = mapped_column(JSON)
    # events/index.json byCode 原始覆盖层条目全量(⑥⑦脚注/⑨总览芯片按原字段透传)
    wind_overlay: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_list_fraud", "trade_date", "fraud"),
        Index("idx_list_mgmt", "trade_date", "mgmt"),
        Index("idx_list_cycle", "trade_date", "cycle"),
        Index("idx_list_graham_agg", "trade_date", "score_graham_agg"),
        Index("idx_list_graham_def", "trade_date", "score_graham_def"),
        Index("idx_list_schloss", "trade_date", "score_schloss"),
        Index("idx_list_buffett", "trade_date", "score_buffett"),
    )


class Dividend(Base):
    """分红送配历史(全量)。"""

    __tablename__ = "dividend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(nullable=False)
    div_year: Mapped[str | None] = mapped_column(String(16))
    div_type: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(128))
    bonus_per_10: Mapped[float | None] = mapped_column(Numeric(10, 4))
    transfer_per_10: Mapped[float | None] = mapped_column(Numeric(10, 4))
    announce_date: Mapped[date | None] = mapped_column(Date)
    record_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    pay_date: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("sid", "div_year", "div_type", name="uk_sid_div"),
        Index("idx_div_sid_ex", "sid", "ex_date"),
    )


class PeriodicReport(Base):
    """定期报告(巨潮,含官方 PDF 直链与审计信息)。"""

    __tablename__ = "periodic_report"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(nullable=False)
    report_date: Mapped[date] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128))
    pdf_url: Mapped[str | None] = mapped_column(String(512))
    detail_url: Mapped[str | None] = mapped_column(String(512))
    audit_firm: Mapped[str | None] = mapped_column(String(128))
    audit_opinion: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint("sid", "report_date", "category", name="uk_sid_date_cat"),
    )


class WindEvent(Base):
    """Wind 公司事件明细(增减持/并购/违规/诉讼/ST)。

    注意:无自然唯一键,INSERT IGNORE 不生效;导入按 sid 先删后插(见 import_legacy)。
    """

    __tablename__ = "wind_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(nullable=False)
    etype: Mapped[str] = mapped_column(String(16), nullable=False)
    event_date: Mapped[date] = mapped_column(nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("idx_event_sid", "sid", "etype", "event_date"),)


class WindHolder(Base):
    """Wind 股东结构(已透视:每家每期一行/名次一行)。

    注意:无自然唯一键,导入按 sid 先删后插(见 import_legacy)。
    """

    __tablename__ = "wind_holder"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sid: Mapped[int] = mapped_column(nullable=False)
    holder_type: Mapped[str] = mapped_column(
        Enum("top10", "top10_float", "institution", "controller", "unlock"),
        nullable=False,
    )
    report_date: Mapped[date | None] = mapped_column(Date)
    rank_no: Mapped[int | None] = mapped_column(SmallInteger)
    holder_name: Mapped[str | None] = mapped_column(String(128))
    shares: Mapped[float | None] = mapped_column(Numeric(24, 4))
    shares_change: Mapped[float | None] = mapped_column(Numeric(24, 4))
    pct: Mapped[float | None] = mapped_column(Double)
    pct_change: Mapped[float | None] = mapped_column(Double)
    shares_type: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("idx_holder_sid", "sid", "holder_type", "report_date"),)


class PfStrategy(Base):
    """模拟组合策略定义(档位/上限/门槛参数)。"""

    __tablename__ = "pf_strategy"

    strat_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    init_cap: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False)


class PfNav(Base):
    """策略每日净值(兼作引擎幂等判定)。"""

    __tablename__ = "pf_nav"

    strat_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    nav_date: Mapped[date] = mapped_column(primary_key=True)
    cash: Mapped[float | None] = mapped_column(Numeric(18, 2))
    nav: Mapped[float | None] = mapped_column(Numeric(18, 2))
    position_pct: Mapped[float | None] = mapped_column(Double)
    day_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2))
    day_pnl_pct: Mapped[float | None] = mapped_column(Double)
    total_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total_pnl_pct: Mapped[float | None] = mapped_column(Double)


class PfPosition(Base):
    """策略持仓(含引擎内部档位状态,替代 _state.json)。"""

    __tablename__ = "pf_position"

    strat_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    sid: Mapped[int] = mapped_column(primary_key=True)
    bought_at: Mapped[date] = mapped_column(nullable=False)
    shares: Mapped[int] = mapped_column(nullable=False)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    tranches: Mapped[dict] = mapped_column(JSON, nullable=False)
    div_last: Mapped[date | None] = mapped_column(Date)


class PfTrade(Base):
    """调仓流水(买/卖/分红)。

    注意:无自然唯一键,导入按 strat_key 先删后插(trades.json 即全量权威)。
    """

    __tablename__ = "pf_trade"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strat_key: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(nullable=False)
    sid: Mapped[int] = mapped_column(nullable=False)
    side: Mapped[str] = mapped_column(Enum("buy", "sell", "dividend"), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(12, 6))
    shares: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    reason: Mapped[str | None] = mapped_column(String(256))

    __table_args__ = (Index("idx_pftrade_strat", "strat_key", "trade_date"),)


class AgroProduct(Base):
    """农价跟踪产品定义。"""

    __tablename__ = "agro_product"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    spec: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(32))
    primary_source: Mapped[str | None] = mapped_column(String(32))
    config: Mapped[dict | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AgroPrice(Base):
    """农价价格点(同日多源共存)。"""

    __tablename__ = "agro_price"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    price_date: Mapped[date] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    price: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    note: Mapped[str | None] = mapped_column(String(128))


class EdbIndicator(Base):
    """Wind EDB 宏观行业指标定义。"""

    __tablename__ = "edb_indicator"

    edb_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    freq: Mapped[str] = mapped_column(Enum("day", "week", "month"), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    display_group: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict | None] = mapped_column(JSON)


class EdbValue(Base):
    """EDB 指标数据点。"""

    __tablename__ = "edb_value"

    edb_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    data_date: Mapped[date] = mapped_column(primary_key=True)
    val: Mapped[float | None] = mapped_column(Numeric(24, 6))


class EtlJobLog(Base):
    """采集任务运行日志(替代 GitHub Actions 运行记录页)。"""

    __tablename__ = "etl_job_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(
        Enum("running", "success", "failed"), nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("idx_job_name", "job_name", "started_at"),)
