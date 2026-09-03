-- db_va 全库 DDL(由 app/models/entities.py 导出,勿手改)

CREATE TABLE security (
	sid INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(16) NOT NULL, 
	market ENUM('A','HK','US') NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	industry VARCHAR(64), 
	exchange VARCHAR(16), 
	currency VARCHAR(3) NOT NULL, 
	status ENUM('active','suspended','delisted') NOT NULL, 
	list_date DATE, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid), 
	CONSTRAINT uk_code_market UNIQUE (code, market)
);

CREATE INDEX idx_market_industry ON security (market, industry);

CREATE TABLE quote_daily (
	sid INTEGER NOT NULL, 
	trade_date DATE NOT NULL, 
	price NUMERIC(12, 4), 
	change_pct DOUBLE, 
	pe_ttm DOUBLE, 
	pb DOUBLE, 
	market_cap NUMERIC(20, 2), 
	float_market_cap NUMERIC(20, 2), 
	turnover_rate DOUBLE, 
	fetched_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, trade_date)
);

CREATE INDEX idx_quote_date ON quote_daily (trade_date);

CREATE TABLE fin_indicator (
	sid INTEGER NOT NULL, 
	report_date DATE NOT NULL, 
	revenue NUMERIC(20, 2), 
	net_profit NUMERIC(20, 2), 
	eps NUMERIC(12, 6), 
	bps NUMERIC(12, 6), 
	gross_margin DOUBLE, 
	net_margin DOUBLE, 
	roe DOUBLE, 
	current_ratio DOUBLE, 
	quick_ratio DOUBLE, 
	debt_ratio DOUBLE, 
	ocf_per_share NUMERIC(12, 6), 
	revenue_q NUMERIC(20, 2), 
	net_profit_q NUMERIC(20, 2), 
	extras JSON, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, report_date)
);

CREATE TABLE fin_income (
	sid INTEGER NOT NULL, 
	report_date DATE NOT NULL, 
	revenue_total NUMERIC(20, 2), 
	operating_profit NUMERIC(20, 2), 
	net_profit NUMERIC(20, 2), 
	deducted_net_profit NUMERIC(20, 2), 
	extras JSON, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, report_date)
);

CREATE TABLE fin_balance (
	sid INTEGER NOT NULL, 
	report_date DATE NOT NULL, 
	total_assets NUMERIC(20, 2), 
	cash NUMERIC(20, 2), 
	trading_fin_assets NUMERIC(20, 2), 
	notes_receivable NUMERIC(20, 2), 
	other_current_assets NUMERIC(20, 2), 
	total_liabilities NUMERIC(20, 2), 
	equity_parent NUMERIC(20, 2), 
	equity_total NUMERIC(20, 2), 
	paid_in_capital NUMERIC(20, 2), 
	short_loan NUMERIC(20, 2), 
	long_loan NUMERIC(20, 2), 
	bond_payable NUMERIC(20, 2), 
	lease_liability NUMERIC(20, 2), 
	noncurrent_due_1y NUMERIC(20, 2), 
	extras JSON, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, report_date)
);

CREATE TABLE fin_cashflow (
	sid INTEGER NOT NULL, 
	report_date DATE NOT NULL, 
	ocf NUMERIC(20, 2), 
	capex NUMERIC(20, 2), 
	cash_received_from_sales NUMERIC(20, 2), 
	extras JSON, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, report_date)
);

CREATE TABLE score_daily (
	sid INTEGER NOT NULL, 
	trade_date DATE NOT NULL, 
	report_date DATE NOT NULL, 
	score_graham_agg DOUBLE, 
	score_graham_def DOUBLE, 
	score_schloss DOUBLE, 
	score_buffett DOUBLE, 
	fraud DOUBLE, 
	mgmt DOUBLE, 
	cycle DOUBLE, 
	cyclical BOOL, 
	cycle_trend VARCHAR(8), 
	fair_liq DOUBLE, 
	net_cash_ratio DOUBLE, 
	net_cash_calc JSON, 
	buy_graham_agg DOUBLE, 
	sell_cons_graham_agg DOUBLE, 
	sell_fair_graham_agg DOUBLE, 
	buy_graham_def DOUBLE, 
	sell_cons_graham_def DOUBLE, 
	sell_fair_graham_def DOUBLE, 
	buy_schloss DOUBLE, 
	sell_cons_schloss DOUBLE, 
	sell_fair_schloss DOUBLE, 
	buy_buffett DOUBLE, 
	sell_cons_buffett DOUBLE, 
	sell_fair_buffett DOUBLE, 
	wind_fraud_delta DOUBLE, 
	wind_mgmt_delta DOUBLE, 
	wind_flags JSON, 
	wind_overlay JSON, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (sid, trade_date)
);

CREATE INDEX idx_list_graham_agg ON score_daily (trade_date, score_graham_agg);

CREATE INDEX idx_list_graham_def ON score_daily (trade_date, score_graham_def);

CREATE INDEX idx_list_schloss ON score_daily (trade_date, score_schloss);

CREATE INDEX idx_list_buffett ON score_daily (trade_date, score_buffett);

CREATE INDEX idx_list_mgmt ON score_daily (trade_date, mgmt);

CREATE INDEX idx_list_fraud ON score_daily (trade_date, fraud);

CREATE INDEX idx_list_cycle ON score_daily (trade_date, cycle);

CREATE TABLE dividend (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	sid INTEGER NOT NULL, 
	div_year VARCHAR(16), 
	div_type VARCHAR(32), 
	description VARCHAR(128), 
	bonus_per_10 NUMERIC(10, 4), 
	transfer_per_10 NUMERIC(10, 4), 
	announce_date DATE, 
	record_date DATE, 
	ex_date DATE, 
	pay_date DATE, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_sid_div UNIQUE (sid, div_year, div_type)
);

CREATE INDEX idx_div_sid_ex ON dividend (sid, ex_date);

CREATE TABLE periodic_report (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	sid INTEGER NOT NULL, 
	report_date DATE NOT NULL, 
	category VARCHAR(8) NOT NULL, 
	title VARCHAR(128), 
	pdf_url VARCHAR(512), 
	detail_url VARCHAR(512), 
	audit_firm VARCHAR(128), 
	audit_opinion VARCHAR(32), 
	PRIMARY KEY (id), 
	CONSTRAINT uk_sid_date_cat UNIQUE (sid, report_date, category)
);

CREATE TABLE wind_event (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	sid INTEGER NOT NULL, 
	etype VARCHAR(16) NOT NULL, 
	event_date DATE NOT NULL, 
	title VARCHAR(256), 
	detail JSON, 
	fetched_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_event_sid ON wind_event (sid, etype, event_date);

CREATE TABLE wind_holder (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	sid INTEGER NOT NULL, 
	holder_type ENUM('top10','top10_float','institution','controller','unlock') NOT NULL, 
	report_date DATE, 
	rank_no SMALLINT, 
	holder_name VARCHAR(128), 
	shares NUMERIC(24, 4), 
	shares_change NUMERIC(24, 4), 
	pct DOUBLE, 
	pct_change DOUBLE, 
	shares_type VARCHAR(32), 
	detail JSON, 
	fetched_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_holder_sid ON wind_holder (sid, holder_type, report_date);

CREATE TABLE agro_product (
	product_id VARCHAR(64) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	spec VARCHAR(64), 
	unit VARCHAR(32), 
	primary_source VARCHAR(32), 
	config JSON, 
	active BOOL NOT NULL, 
	PRIMARY KEY (product_id)
);

CREATE TABLE agro_price (
	product_id VARCHAR(64) NOT NULL, 
	price_date DATE NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	price NUMERIC(14, 4) NOT NULL, 
	note VARCHAR(128), 
	PRIMARY KEY (product_id, price_date, source)
);

CREATE TABLE edb_indicator (
	edb_code VARCHAR(32) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	freq ENUM('day','week','month') NOT NULL, 
	unit VARCHAR(32), 
	display_group VARCHAR(64), 
	extra JSON, 
	PRIMARY KEY (edb_code)
);

CREATE TABLE edb_value (
	edb_code VARCHAR(32) NOT NULL, 
	data_date DATE NOT NULL, 
	val NUMERIC(24, 6), 
	PRIMARY KEY (edb_code, data_date)
);

CREATE TABLE etl_job_log (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	job_name VARCHAR(64) NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	status ENUM('running','success','failed') NOT NULL, 
	message TEXT, 
	stats JSON, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_job_name ON etl_job_log (job_name, started_at);

