# -*- coding: utf-8 -*-
"""P1:存量 JSON 数据导入 MySQL db_va。

数据源(原 GitHub Pages 静态服务,LEGACY_DATA_DIR 默认指向博客仓库):
    data/index.json            → security 主数据 + score_daily(评分快照)
    data/companies/*.json      → quote_daily / fin_indicator / fin_income /
                                 fin_balance / fin_cashflow / dividend / periodic_report
    data/events/*.json         → wind_event / wind_holder
    data/events/index.json     → score_daily.wind_*(事件增量覆盖层)
    portfolio/data/*.json      → pf_strategy / pf_nav / pf_position / pf_trade
    agro-price/data/products.json → agro_product / agro_price
    agro-price/data/edb.json   → edb_indicator / edb_value

幂等策略:默认先清空业务表再导入(源 JSON 为唯一权威数据)。
用法(在 backend 目录下):
    python -m scripts.import_legacy             # 清空后全量导入
    python -m scripts.import_legacy --no-clean  # 不清空,按唯一键 upsert;
        wind_event/wind_holder/pf_trade 无自然唯一键,按 sid/strat_key 先删后插(整文件权威)
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.config import LEGACY_DATA_DIR, MARKET_CURRENCY
from app.db import SessionLocal
from app.fin_columns import (
    BALANCE_CORE,
    CASHFLOW_CORE,
    INCOME_CORE,
    INDICATOR_CORE,
    split_core,
)
from app.models import (
    AgroPrice,
    AgroProduct,
    Dividend,
    EdbIndicator,
    EdbValue,
    EtlJobLog,
    FinBalance,
    FinCashflow,
    FinIncome,
    FinIndicator,
    PeriodicReport,
    PfNav,
    PfPosition,
    PfStrategy,
    PfTrade,
    QuoteDaily,
    ScoreDaily,
    Security,
    WindEvent,
    WindHolder,
)

DATA_DIR = LEGACY_DATA_DIR / "data"
PF_DIR = LEGACY_DATA_DIR / "portfolio" / "data"
AGRO_DIR = LEGACY_DATA_DIR / "agro-price" / "data"

# pf_strategy 参数(原 portfolio_engine.py STRATEGIES 常量)
PF_STRATEGIES = {
    "schloss": {"label": "施洛斯烟蒂", "school": "schloss",
                "target_w": 0.08, "buy_bands": (0.03, 0.08, 0.15),
                "sell_bands": (("sellCons", 1.0), ("sellFair", 1.0), ("sellFair", 1.05)),
                "min_mgmt": 55, "max_fraud": 30, "min_score": 0},
    "grahamDef": {"label": "格雷厄姆防御", "school": "grahamDef",
                  "target_w": 0.06, "buy_bands": (0.05, 0.10, 0.15),
                  "sell_bands": (("sellCons", 1.0), ("sellFair", 1.0), ("sellFair", 1.10)),
                  "min_mgmt": 70, "max_fraud": 30, "min_score": 75},
    "buffett": {"label": "巴菲特芒格", "school": "buffett",
                "target_w": 0.15, "buy_bands": (0.02, 0.05, 0.10),
                "sell_bands": (("sellFair", 1.0), ("sellFair", 1.25), ("sellFair", 1.50)),
                "min_mgmt": 80, "max_fraud": 30, "min_score": 70},
}

# Wind 事件类型 → etype
EVENT_TYPE_MAP = {
    "increase_hold": "增减持",
    "ma": "并购",
    "penalty": "违规",
    "lawsuit": "诉讼",
    "st_change": "ST",
}

# EDB 频率中文 → 枚举
EDB_FREQ_MAP = {"日": "day", "周": "week", "月": "month"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_date(s):
    """'2026-09-01T16:14:47+08:00' / '2026-09-01' → date;无效返回 None。"""
    if not s:
        return None
    return datetime.fromisoformat(str(s)).date()


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s))


def event_pick(row: dict):
    """从 Wind 事件行提取 (event_date, title);字段名随类型变化,按规则扫描。"""
    ed, title = None, None
    for k, v in row.items():
        if isinstance(v, str) and DATE_RE.match(v):
            if ed is None or any(t in k for t in ("日期", "披露日", "时间")):
                ed = parse_date(v)
        if title is None and isinstance(v, str) and len(v) > 6 and not DATE_RE.match(v):
            if any(t in k for t in ("标题", "名称", "内容")):
                title = v
    return ed, title


def holder_pick(row: dict, htype: str):
    """Wind 股东行 → wind_holder 列;字段名在 top10/institution 间不同。"""
    if htype in ("top10", "top10_float"):
        name_k, pct_k = "最新一期前十大股东名称", "最新一期前十大股东持股比例"
        shares_k = "最新一期前十大股东持股数量"
        chg_k, type_k = "股东持股数量变动", "最新一期前十大股东持股股本性质"
    elif htype in ("controller", "unlock"):
        # 实际控制人/限售解禁行字段结构与持股行不同,不做列映射,仅存 detail 原样行
        return {"rank_no": None, "holder_name": None, "pct": None,
                "shares": None, "shares_change": None, "shares_type": None,
                "detail": row}
    else:
        name_k, pct_k = "最新一期机构股东名称", "最新一期机构持股比例"
        shares_k = "最新一期机构持股数量"
        chg_k, type_k = "最新一期机构持股数量变动", None
    return {
        "rank_no": row.get("名次"),
        "holder_name": row.get(name_k),
        "pct": row.get(pct_k),
        "shares": row.get(shares_k),
        "shares_change": row.get(chg_k),
        "shares_type": row.get(type_k) if type_k else None,
        "detail": row,
    }


def upsert_security(db, code, market, name, industry, updated_at, list_date=None):
    vals = dict(
        code=code, market=market, name=name, industry=industry,
        currency=MARKET_CURRENCY[market], status="active",
        list_date=list_date, updated_at=updated_at,
    )
    stmt = mysql_insert(Security).values(**vals).on_duplicate_key_update(
        name=vals["name"], industry=vals["industry"],
        updated_at=vals["updated_at"], list_date=vals["list_date"],
    )
    db.execute(stmt)
    return db.execute(
        select(Security.sid).where(Security.code == code, Security.market == market)
    ).scalar_one()


def school_cols(school: str):
    """index.json 流派键 → score_daily 列名前缀。"""
    return {"grahamAgg": "graham_agg", "grahamDef": "graham_def"}.get(school, school)


def import_companies(db, stats):
    """companies/*.json + index.json → 行情/财务/分红/报告/评分。"""
    index = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))
    trade_date = parse_date(index["updated_at"])
    by_code = {c["code"]: c for c in index.get("companies", [])}

    score_rows = {}
    for c in index.get("companies", []):
        sc = c.get("scores") or {}
        refs = sc.get("priceRefs") or {}
        calc = refs.get("netCashCalc") or {}
        row = {
            "trade_date": trade_date,
            "report_date": parse_date(calc.get("report")) or trade_date,
            "score_graham_agg": sc.get("grahamAgg"),
            "score_graham_def": sc.get("grahamDef"),
            "score_schloss": sc.get("schloss"),
            "score_buffett": sc.get("buffett"),
            "fraud": sc.get("fraud"),
            "mgmt": sc.get("mgmt"),
            "cycle": sc.get("cycle"),
            "cyclical": sc.get("cyclical"),
            "cycle_trend": sc.get("cycleTrend"),
            "fair_liq": refs.get("fairLiq"),
            "net_cash_ratio": refs.get("netCashRatio"),
            "net_cash_calc": calc or None,
            "updated_at": parse_dt(index["updated_at"]),
        }
        for school in ("grahamAgg", "grahamDef", "schloss", "buffett"):
            col = school_cols(school)
            r = refs.get(school) or {}
            row[f"buy_{col}"] = r.get("buy")
            row[f"sell_cons_{col}"] = r.get("sellCons")
            row[f"sell_fair_{col}"] = r.get("sellFair")
        score_rows[c["code"]] = row

    # Wind 事件覆盖层并入评分行(三字段拆列供列表计算 + 原始条目全量透传供详情⑨总览)
    ov_path = DATA_DIR / "events" / "index.json"
    if ov_path.exists():
        overlay = json.loads(ov_path.read_text(encoding="utf-8")).get("byCode", {})
        for code, ov in overlay.items():
            if code in score_rows:
                score_rows[code]["wind_fraud_delta"] = ov.get("fraudDelta")
                score_rows[code]["wind_mgmt_delta"] = ov.get("mgmtDelta")
                score_rows[code]["wind_flags"] = ov.get("flags")
                score_rows[code]["wind_overlay"] = ov

    for path in sorted((DATA_DIR / "companies").glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        code, name = d["code"], d["name"]
        info = d.get("info") or {}
        idx_c = by_code.get(code, {})
        # A 股公司 JSON 无 market 字段,优先取 index.json,回退 A
        market = idx_c.get("market") or d.get("market") or "A"
        updated_at = parse_dt(d.get("updated_at")) or parse_dt(index["updated_at"])
        list_dt = None
        if info.get("上市日期"):
            try:
                list_dt = datetime.strptime(str(info["上市日期"])[:10], "%Y-%m-%d").date()
            except ValueError:
                list_dt = None
        sid = upsert_security(db, code, market, name,
                              idx_c.get("industry") or info.get("行业"),
                              updated_at, list_dt)
        stats["security"] += 1

        snap = d.get("snapshot") or {}
        qdate = parse_date(snap.get("time")) or updated_at.date()
        if snap:
            qvals = dict(
                sid=sid, trade_date=qdate,
                price=snap.get("price"), change_pct=snap.get("change_pct"),
                pe_ttm=snap.get("pe_ttm"), pb=snap.get("pb"),
                market_cap=snap.get("market_cap"),
                float_market_cap=snap.get("float_market_cap"),
                turnover_rate=snap.get("turnover_rate"),
                fetched_at=updated_at,
            )
            upd = {k: v for k, v in qvals.items() if k != "trade_date"}
            db.execute(mysql_insert(QuoteDaily)
                       .values(**qvals).on_duplicate_key_update(**upd))
            stats["quote_daily"] += 1

        now = updated_at
        for r in d.get("indicators") or []:
            core, extras = split_core(r, INDICATOR_CORE)
            db.execute(mysql_insert(FinIndicator).values(
                sid=sid, report_date=parse_date(r.get("报告期")),
                updated_at=now, extras=extras, **core,
            ).prefix_with("IGNORE"))
            stats["fin_indicator"] += 1
        for r in d.get("income") or []:
            core, extras = split_core(r, INCOME_CORE)
            db.execute(mysql_insert(FinIncome).values(
                sid=sid, report_date=parse_date(r.get("报告日")),
                updated_at=now, extras=extras, **core,
            ).prefix_with("IGNORE"))
            stats["fin_income"] += 1
        for r in d.get("balance") or []:
            core, extras = split_core(r, BALANCE_CORE)
            db.execute(mysql_insert(FinBalance).values(
                sid=sid, report_date=parse_date(r.get("报告日")),
                updated_at=now, extras=extras, **core,
            ).prefix_with("IGNORE"))
            stats["fin_balance"] += 1
        for r in d.get("cashflow") or []:
            core, extras = split_core(r, CASHFLOW_CORE)
            db.execute(mysql_insert(FinCashflow).values(
                sid=sid, report_date=parse_date(r.get("报告日")),
                updated_at=now, extras=extras, **core,
            ).prefix_with("IGNORE"))
            stats["fin_cashflow"] += 1

        for r in d.get("dividends") or []:
            db.execute(mysql_insert(Dividend).values(
                sid=sid, div_year=r.get("year"), div_type=r.get("type"),
                description=r.get("description"),
                bonus_per_10=r.get("bonus_per_10"), transfer_per_10=r.get("transfer_per_10"),
                announce_date=parse_date(r.get("announce_date")),
                record_date=parse_date(r.get("record_date")),
                ex_date=parse_date(r.get("ex_date")), pay_date=parse_date(r.get("pay_date")),
            ).prefix_with("IGNORE"))
            stats["dividend"] += 1

        for r in d.get("reports") or []:
            if not r.get("date"):
                continue
            db.execute(mysql_insert(PeriodicReport).values(
                sid=sid, report_date=parse_date(r["date"]), category=r.get("category", ""),
                title=r.get("title"), pdf_url=r.get("pdf_url"), detail_url=r.get("detail_url"),
                audit_firm=r.get("audit_firm"), audit_opinion=r.get("audit_opinion"),
            ).prefix_with("IGNORE"))
            stats["periodic_report"] += 1

        if code in score_rows:
            svals = dict(sid=sid, **score_rows[code])
            supd = {k: v for k, v in svals.items() if k != "trade_date"}
            db.execute(mysql_insert(ScoreDaily)
                       .values(**svals).on_duplicate_key_update(**supd))
            stats["score_daily"] += 1


def import_events(db, stats):
    """events/*.json → wind_event / wind_holder。"""
    stats.setdefault("skipped_events", 0)
    for path in sorted((DATA_DIR / "events").glob("*.json")):
        if path.name == "index.json":
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        row = db.execute(
            select(Security.sid).where(Security.code == d["code"], Security.market == "A")
        ).first()
        if not row:
            stats["skipped_events"] += 1
            continue
        sid = row[0]
        fetched = parse_dt(d.get("fetched_at")) or datetime.now()
        # [P5 修复] wind_event/wind_holder 无自然唯一键,增量回灌时 IGNORE 不生效会重复插行;
        # 单文件即该证券全量权威 → 先删后插,天然幂等且对内容变更免疫
        db.execute(text("DELETE FROM wind_event WHERE sid = :s"), {"s": sid})
        db.execute(text("DELETE FROM wind_holder WHERE sid = :s"), {"s": sid})
        for group, etype in EVENT_TYPE_MAP.items():
            for rec in (d.get("events") or {}).get(group) or []:
                ed, title = event_pick(rec)
                db.execute(mysql_insert(WindEvent).values(
                    sid=sid, etype=etype, event_date=ed or fetched.date(),
                    title=title, detail=rec, fetched_at=fetched,
                ))
                stats["wind_event"] += 1
        for group, htype in (("top10", "top10"), ("top10_float", "top10_float"),
                             ("institutions", "institution"),
                             ("actual_controller", "controller"), ("unlock", "unlock")):
            for rec in (d.get("holders") or {}).get(group) or []:
                vals = holder_pick(rec, htype)
                db.execute(mysql_insert(WindHolder).values(
                    sid=sid, holder_type=htype, report_date=None,
                    fetched_at=fetched, **vals,
                ))
                stats["wind_holder"] += 1


def import_portfolio(db, stats):
    """portfolio/data/*.json → pf_strategy / pf_nav / pf_position / pf_trade。"""
    pf = json.loads((PF_DIR / "portfolio.json").read_text(encoding="utf-8"))
    navs = json.loads((PF_DIR / "nav.json").read_text(encoding="utf-8"))
    states = json.loads((PF_DIR / "_state.json").read_text(encoding="utf-8"))
    trades = json.loads((PF_DIR / "trades.json").read_text(encoding="utf-8"))

    for key, cfg in PF_STRATEGIES.items():
        init_cap = pf["strategies"].get(key, {}).get("init_cap", 200000)
        stmt = mysql_insert(PfStrategy).values(
            strat_key=key, label=cfg["label"], init_cap=init_cap, params=cfg,
        ).on_duplicate_key_update(label=cfg["label"], init_cap=init_cap, params=cfg)
        db.execute(stmt)
        stats["pf_strategy"] += 1

    for key, rows in navs.items():
        strat = pf["strategies"].get(key, {})
        prev_nav = None
        for r in rows:
            nav = r.get("nav")
            # 当日完整盈亏来自 portfolio.json;历史行由相邻 nav 差补(当日盈亏=今-上一入账日)
            if strat.get("as_of") == r["date"]:
                extra = {k: strat.get(k) for k in
                         ("day_pnl", "day_pnl_pct", "total_pnl", "total_pnl_pct")}
            else:
                day = (float(nav) - prev_nav) if (nav is not None and prev_nav is not None) else None
                extra = {"day_pnl": day}
            nvals = dict(
                strat_key=key, nav_date=parse_date(r["date"]),
                cash=r.get("cash"), nav=nav, position_pct=r.get("position_pct"),
                **extra,
            )
            nupd = {k: v for k, v in nvals.items() if k != "nav_date"}
            db.execute(mysql_insert(PfNav)
                       .values(**nvals).on_duplicate_key_update(**nupd))
            stats["pf_nav"] += 1
            prev_nav = float(nav) if nav is not None else prev_nav

    for key, s in states.items():
        for pos in s.get("positions", []):
            sid = db.execute(select(Security.sid).where(
                Security.code == pos["code"], Security.market == "A")).scalar_one()
            tr = pos.get("tranches")
            tr_store = {"count": tr} if isinstance(tr, int) else (tr or {})
            pvals = dict(
                strat_key=key, sid=sid, bought_at=parse_date(pos["bought_at"]),
                shares=pos["shares"], cost=pos["cost"], tranches=tr_store,
                div_last=parse_date(pos.get("div_last")),
            )
            pupd = {k: v for k, v in pvals.items() if k not in ("strat_key", "sid")}
            db.execute(mysql_insert(PfPosition)
                       .values(**pvals).on_duplicate_key_update(**pupd))
            stats["pf_position"] += 1

    for key, rows in trades.items():
        # [P5 修复] pf_trade 无自然唯一键,trades.json 即该策略全量权威 → 先删后插
        db.execute(text("DELETE FROM pf_trade WHERE strat_key = :k"), {"k": key})
        for t in rows:
            sid = db.execute(select(Security.sid).where(
                Security.code == t["code"], Security.market == "A")).scalar_one()
            db.execute(mysql_insert(PfTrade).values(
                strat_key=key, trade_date=parse_date(t["date"]), sid=sid,
                side=t["side"], price=t.get("price"), shares=t.get("shares"),
                amount=t.get("amount"), reason=t.get("reason"),
            ))
            stats["pf_trade"] += 1


def import_agro(db, stats):
    """products.json → agro_product / agro_price。"""
    src = json.loads((AGRO_DIR / "products.json").read_text(encoding="utf-8"))
    for p in src.get("products", []):
        stmt = mysql_insert(AgroProduct).values(
            product_id=p["id"], name=p["name"], category=p.get("category", ""),
            spec=p.get("spec"), unit=p.get("unit"), primary_source=None,
            config=None, active=True,
        ).on_duplicate_key_update(
            name=p["name"], category=p.get("category", ""),
            spec=p.get("spec"), unit=p.get("unit"),
        )
        db.execute(stmt)
        stats["agro_product"] += 1
        for pr in p.get("prices", []):
            stmt = mysql_insert(AgroPrice).values(
                product_id=p["id"], price_date=parse_date(pr["date"]),
                source=pr.get("source", ""), price=pr["price"], note=pr.get("note"),
            ).on_duplicate_key_update(price=pr["price"], note=pr.get("note"))
            db.execute(stmt)
            stats["agro_price"] += 1


def import_edb(db, stats):
    """edb.json → edb_indicator / edb_value。"""
    src = json.loads((AGRO_DIR / "edb.json").read_text(encoding="utf-8"))
    for ci, cat in enumerate(src.get("categories", [])):
        for ii, ind in enumerate(cat.get("indicators", [])):
            freq = EDB_FREQ_MAP.get(str(ind.get("freq", "")).strip(), "month")
            extra = {
                "source": ind.get("source"), "name": ind.get("name"),
                "cat_name": cat.get("name"), "cat_idx": ci, "ind_idx": ii,
            }
            ival = dict(
                edb_code=ind["code"], name=ind.get("label") or ind.get("name", ""),
                category=cat["id"], freq=freq, unit=ind.get("unit"),
                display_group=ind.get("group"), extra=extra,
            )
            iupd = {k: v for k, v in ival.items() if k != "edb_code"}
            db.execute(mysql_insert(EdbIndicator)
                       .values(**ival).on_duplicate_key_update(**iupd))
            stats["edb_indicator"] += 1
            for pt in ind.get("points", []):
                if len(pt) != 2 or not pt[0]:
                    continue
                stmt = mysql_insert(EdbValue).values(
                    edb_code=ind["code"], data_date=parse_date(pt[0]), val=pt[1],
                ).on_duplicate_key_update(val=pt[1])
                db.execute(stmt)
                stats["edb_value"] += 1


TABLES = [
    "pf_trade", "pf_position", "pf_nav", "pf_strategy",
    "wind_holder", "wind_event", "score_daily", "periodic_report", "dividend",
    "fin_cashflow", "fin_balance", "fin_income", "fin_indicator", "quote_daily",
    "security", "agro_price", "agro_product", "edb_value", "edb_indicator", "etl_job_log",
]


def clean_tables(db):
    """按依赖倒序清空业务表(先全局关闭外键检查加速)。"""
    db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    for t in TABLES:
        db.execute(text(f"TRUNCATE TABLE {t}"))
    db.execute(text("SET FOREIGN_KEY_CHECKS=1"))


class _Stats(dict):
    """计数器:首次访问自动补 0。"""

    def __missing__(self, key):
        self[key] = 0
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-clean", action="store_true", help="不清空,直接导入(upsert)")
    args = ap.parse_args()

    started = datetime.now()
    db = SessionLocal()
    stats = _Stats()
    try:
        if not args.no_clean:
            clean_tables(db)
            print("已清空业务表")
        import_companies(db, stats)
        import_events(db, stats)
        import_portfolio(db, stats)
        import_agro(db, stats)
        import_edb(db, stats)
        db.add(EtlJobLog(
            job_name="import_legacy", started_at=started, finished_at=datetime.now(),
            status="success", message=f"导入完成: {dict(stats)}", stats=dict(stats),
        ))
        db.commit()
        print("导入完成:")
        for k in sorted(stats):
            print(f"  {k}: {stats[k]}")
    except Exception as e:
        db.rollback()
        db.add(EtlJobLog(
            job_name="import_legacy", started_at=started, finished_at=datetime.now(),
            status="failed", message=str(e)[:2000], stats=dict(stats),
        ))
        db.commit()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
