# -*- coding: utf-8 -*-
"""世界局势推演 API(2026-11-15)。

三层数据三种供法(与模块分工对齐):
    1. /overview 局势温度计——读时合成:10 个温度计指标里 8 个是复用序列
       (GPR/VIXCLS/金银比/铜金比/布伦特-WTI/美元宽/实际利率/中间价,已在
       cmd_value 的 gold/copper/crudeoil 行),2 个跨商品派生(GEO_ 前缀,
       commodity='worldgeo' 行,fetch_worldgeo.py 产)。本端点按 code 跨
       commodity 拉全量点,算当前值/52周分位/全史分位/1月变化——分位口径
       与 commodity._stats 同源(52 周窗口内位置),另加全史分位。
    2. /events /scenarios /matrix——人工结构化档直读(commodity-worldgeo/data/
       下的 events/scenarios/geo_matrix/playbooks 四个 JSON,aviation_ipo.json
       与 forecast targets.json 同款模式),零采集零落表。
    3. 应验度核对(verify)在 /events 读时合成:事件日→date+window_days 窗口内,
       该资产代理序列首末涨跌 vs 预期方向——是事实核对不是评分(黄金记分卡
       两连 FAIL 判死教训:不做自动判分,概率与情景标注「主观估计」)。

纪律红线(与 commodity.aviation-stress 同款):呈现指标事实与方向先验,
综合研判留给人;sector/stock 面不做自动核对(业务结构决定敏感度,见
playbook 2024-2026 的 lesson),标人工。
"""
import datetime as dt
import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import CmdValue, QuoteDaily, Security

router = APIRouter(prefix="/api/world-geo", tags=["world-geo"])

_GEO_DIR = Path(__file__).resolve().parents[2] / "collector" / "commodity-worldgeo" / "data"

# 新鲜度红线(天):与 commodity.py 同语义(月度 GPR 滞后两月是常态)
_STALE_DAYS = {"day": 7, "week": 21, "month": 70, "year": 400}

# ---- 温度计 10 卡(顺序=页面顺序)。code 跨 commodity 复用,GEO_ 两个是派生 ----
_THERMO = [
    ("GPR", "GPR 地缘风险", "地缘政治风险指数(月度)", "点", "month",
     "局势直接读数:升温=地缘紧张。1985→ 长历史,全史分位才有意义。"),
    ("VIXCLS", "VIX 恐慌", "美股恐慌指数", "点", "day",
     "即时恐慌读数:急升=风险资产普跌,金先被抛后涨(两段式)。"),
    ("GEO_GOLD_OIL", "金油比", "伦敦金÷WTI", "盎司/桶", "day",
     "避险×能源双温度:飙升=避险与供给冲击共振(海湾战争/2020-04/俄乌);温和走低=需求主导行情。"),
    ("GOLD_SILVER", "金银比", "伦敦金÷伦敦银", "倍", "day",
     ">80 偏「银便宜」、<50 偏「银贵」;高位=恐慌溢价集中在金,白银工业属性被抛。"),
    ("CU_GOLD", "铜金比", "LME铜÷伦敦金", "吨/盎司", "day",
     "风险偏好温度计:历史低位=衰退定价(铜崩金稳);走高=周期与增长主导。"),
    ("GEO_OIL_CU", "油铜比", "WTI÷LME铜×1000", "×1000", "day",
     "通胀交易 vs 增长交易:走高=供给冲击/通胀担忧主导;走低=需求衰退(铜先崩)或供给过剩(油先崩)——看谁先动。"),
    ("BRENT_WTI", "布伦特-WTI", "海运-陆上原油价差", "美元/桶", "day",
     "走阔=美国以外海运市场紧张(制裁/航线/保费);收窄=美国本土紧张。"),
    ("DTWEXBGS", "美元宽口径", "贸易加权美元指数", "点", "day",
     "全球定价分母:美元荒=金油铜同承压;美元弱=商品普涨土壤。美元与金同涨=流动性恐慌指纹。"),
    ("DFII10", "10Y 实际利率", "美国 10Y TIPS 实际利率", "%", "day",
     "金融条件核心:抬升压制贵金属与无现金流资产;跳水常伴金融稳定事件。"),
    ("USDCNY_FIX", "人民币中间价", "美元兑人民币中间价", "元", "day",
     "贬值压力=贸易摩擦/博弈升温的汇率对冲形态(2018/2025 实测)。"),
]

# ---- 应验度代理序列映射:events.json 的 asset.ref → cmd_value 的 code ----
_VERIFY_CODE = {
    "crudeoil": "CL_MAIN", "gold": "XAU_USD", "silver": "XAG_USD",
    "copper": "CAD_MAIN", "aluminum": "AHD_MAIN", "lithium": "LC0_CLOSE",
    "ferrous": "RB0_CLOSE", "agri": "LH0_CLOSE", "fx_cny": "USDCNY_FIX",
    "natgas": "NG_MAIN", "jetfuel": "AV_JETFUEL", "ironore": "I0_CLOSE",
}


def _load_json(name, default):
    """人工档直读:损坏/缺失按空档降级不抛(与 aviation_ipo 同款)。"""
    p = _GEO_DIR / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 人工档损坏不阻断端点
        return default


def _series_pts(db: Session, code: str):
    rows = db.execute(
        select(CmdValue.data_date, CmdValue.val)
        .where(CmdValue.cmd_code == code).order_by(CmdValue.data_date)
    ).all()
    return [(d.isoformat(), float(v)) for d, v in rows if v is not None]


def _series_tail(db: Session, code: str, n: int = 90):
    """最近 n 个点(传导链节点实时读数用,避免拉全量长序列)。"""
    rows = db.execute(
        select(CmdValue.data_date, CmdValue.val)
        .where(CmdValue.cmd_code == code)
        .order_by(CmdValue.data_date.desc()).limit(n)
    ).all()
    return [(d.isoformat(), float(v)) for d, v in reversed(rows) if v is not None]


def _quote_pts(db: Session, code: str, start: dt.date, end: dt.date):
    """个股窗口收盘价(股票/板块代理核对用;quote_daily 历史 2026-09 起)。"""
    rows = db.execute(
        select(QuoteDaily.trade_date, QuoteDaily.price)
        .join(Security, Security.sid == QuoteDaily.sid)
        .where(Security.code == code,
               QuoteDaily.trade_date >= start, QuoteDaily.trade_date <= end)
        .order_by(QuoteDaily.trade_date)
    ).all()
    return [(d.isoformat(), float(p)) for d, p in rows if p is not None]


def _exposure_primary(ref: str):
    """geo_exposure.json 的板块→代表标的(代理核对用);读不到返回 None。"""
    ex = _load_json("geo_exposure.json", {"exposure": {}})
    return (ex.get("exposure", {}).get(ref) or {}).get("primary")


def _pctile(points, days=None):
    """最新值在窗口内的分位(0~1)。days=None 即全史窗口。"""
    if not points:
        return None
    latest = points[-1][1]
    vals = points if days is None else points[-days:]
    lo, hi = min(v for _, v in vals), max(v for _, v in vals)
    if hi <= lo:
        return 0.5
    return round((latest - lo) / (hi - lo), 4)


def _thermo_one(points, freq):
    """单温度计卡:当前值+52周/全史分位+1月变化+新鲜度。"""
    if not points:
        return {"latest": None, "stale": True}
    today = dt.date.today()
    latest_d = dt.date.fromisoformat(points[-1][0])
    cut30 = (latest_d - dt.timedelta(days=30)).isoformat()
    chg_1m = None
    for d, v in points:
        if d >= cut30:
            chg_1m = round(points[-1][1] - v, 4)
            break
    win52 = [p for p in points if dt.date.fromisoformat(p[0]) >= today - dt.timedelta(days=365)]
    return {
        "latest": points[-1][1],
        "latest_date": points[-1][0],
        "chg_1m": chg_1m,
        "pct_52w": _pctile(win52) if win52 else None,
        "pct_all": _pctile(points),
        "begin": points[0][0],
        "stale_days": (today - latest_d).days,
        "stale": (today - latest_d).days > _STALE_DAYS.get(freq, 14),
    }


# 温度计缓存:指纹=(10 code 的 max(data_date), 总行数);日更后指纹变化自动失效
_thermo_cache: dict = {}
_thermo_lock = threading.Lock()
_THERMO_TTL = 600.0


def _thermo_fingerprint(db: Session) -> tuple:
    codes = [t[0] for t in _THERMO]
    row = db.execute(
        select(func.max(CmdValue.data_date), func.count())
        .where(CmdValue.cmd_code.in_(codes))
    ).one()
    return tuple(str(v) for v in row)


@router.get("/overview")
def overview(db: Session = Depends(get_session)):
    """局势温度计:10 卡读时合成(复用序列 8 + 跨商品派生 2)。"""
    with _thermo_lock:
        ent = _thermo_cache.get("thermo")
    if ent is not None:
        fp_now = _thermo_fingerprint(db)
        if ent[0] == fp_now and time.time() - ent[2] < _THERMO_TTL:
            return Response(content=ent[1], media_type="application/json")
    codes = [t[0] for t in _THERMO]
    pts = {c: _series_pts(db, c) for c in codes}
    thermo = []
    for code, label, name, unit, freq, note in _THERMO:
        thermo.append({
            "code": code, "label": label, "name": name, "unit": unit, "freq": freq,
            "note": note, "stats": _thermo_one(pts.get(code) or [], freq),
        })
    payload = {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "thermo": thermo,
        "method": (
            "温度计是多单一事实并排(各自分位),不做加权综合分——复合评分记分卡已判死"
            "(黄金两连 FAIL 教训),局势研判是「组合读数+人工判别」。"
            "52周分位=最新值在近一年窗口的位置;全史分位=数据起点起的位置(GEO_OIL_CU 仅 2016→,"
            "GPR 1985→——起点短的序列全史分位覆盖不了完整周期,读数时看 begin)。"
            "GPR 月度滞后约两月,即时局势看 VIX 与油运保费类代理。"
        ),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _thermo_lock:
        _thermo_cache["thermo"] = (_thermo_fingerprint(db), body, time.time())
        return Response(content=body, media_type="application/json")


def _verify_asset(db: Session, ev_date: str, window_days: int,
                  atype: str, ref: str, direction: str, threshold: float):
    """单资产应验度核对:窗口内首末涨跌 vs 预期方向。事实核对,不评分。

    P1 扩展:商品/fx 面查 cmd_value;stock 面直接查 quote_daily(个股行情,
    历史 2026-09 起——更早的事件窗口自然 insufficient 降级,诚实);sector 面
    用 geo_exposure.json 的代表标的代理(primary,标注 proxy)。窗口内点 <2
    一律 insufficient,不编数据。
    """
    start = dt.date.fromisoformat(ev_date)
    end = start + dt.timedelta(days=window_days)
    today = dt.date.today()
    extra: dict = {}
    # ① 解析代理数据源(商品/fx→cmd_value 全量序列;stock/sector→quote_daily 窗口查询)
    if atype in ("commodity", "fx"):
        code = _VERIFY_CODE.get(ref)
        if not code:
            return {"status": "manual", "note": "该 ref 无代理映射,人工核对"}
    elif atype == "stock":
        code = ref
        extra["proxy_note"] = "个股行情核对(quote_daily)"
    elif atype == "sector":
        code = _exposure_primary(ref)
        if not code:
            return {"status": "manual", "note": "板块无代表标的,人工核对"}
        extra["proxy_note"] = "代表标的 %s 代理(板块综合非个股)" % code
    else:
        return {"status": "manual", "note": "该资产类型不做自动核对,人工"}
    # ② 未到期统一 pending(任何类型)
    if today < end:
        return {"status": "pending", "code": code,
                "window_end": end.isoformat(), "note": "核对窗口未满", **extra}
    # ③ 窗口数据(点 <2 一律 insufficient,不编数据)
    if atype in ("commodity", "fx"):
        pts = _series_pts(db, code)
        win = [p for p in pts if start <= dt.date.fromisoformat(p[0]) <= end]
    else:
        win = _quote_pts(db, code, start, end)
    if not win or len(win) < 2:
        return {"status": "insufficient", "code": code,
                "note": ("quote_daily 窗口内数据不足(历史深度 2026-09 起)"
                         if atype in ("stock", "sector") else "窗口内数据点不足(序列断档?)"),
                **extra}
    base, last = win[0][1], win[-1][1]
    chg = (last / base - 1) * 100 if base else None
    if chg is None:
        return {"status": "insufficient", "code": code, "note": "基期值为 0,无法算涨跌", **extra}
    if abs(chg) < threshold:
        verdict = "flat"
    elif (direction == "bullish" and chg > 0) or (direction == "bearish" and chg < 0):
        verdict = "confirmed"
    else:
        verdict = "refuted"
    return {
        "status": "done", "code": code,
        "window_start": win[0][0], "window_end": win[-1][0],
        "base": base, "last": last, "chg_pct": round(chg, 1),
        "verdict": verdict, **extra,
    }


@router.get("/events")
def events(db: Session = Depends(get_session)):
    """活跃事件库 + 应验度自动核对(商品/fx/stock/sector 代理面) + 掘金台联动映射。"""
    data = _load_json("events.json", {"events": []})
    threshold = float(data.get("verify_threshold_pct", 1.0))
    out = []
    for ev in data.get("events", []):
        ev = dict(ev)
        wdays = int((ev.get("expectation") or {}).get("window_days") or 90)
        for a in ev.get("assets", []):
            a["verify"] = _verify_asset(
                db, ev.get("date") or "1970-01-01", wdays,
                a.get("type") or "", a.get("ref") or "",
                a.get("direction") or "neutral", threshold)
        # 事件级汇总:仅统计可自动核对的资产
        done = [a["verify"] for a in ev.get("assets", [])
                if a.get("verify", {}).get("status") == "done"]
        ev["verify_summary"] = {
            "done": len(done),
            "confirmed": sum(1 for v in done if v["verdict"] == "confirmed"),
            "refuted": sum(1 for v in done if v["verdict"] == "refuted"),
            "flat": sum(1 for v in done if v["verdict"] == "flat"),
            "due": bool(done) and all(
                dt.date.today() >= dt.date.fromisoformat(ev.get("date") or "1970-01-01")
                + dt.timedelta(days=wdays) for v in done),
        }
        # horizon=structural(结构性趋势)不进入到期判定
        ev["is_structural"] = any(
            a.get("horizon") == "structural" for a in ev.get("assets", []))
        out.append(ev)
    return {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated": data.get("updated"),
        "note": data.get("note"),
        "verify_note": data.get("verify_note"),
        "events": out,
        # 掘金台联动:资产→代表标的映射(前端按 active 事件的 ref 反查标的挂 pill)
        "exposure": _load_json("geo_exposure.json", {"exposure": {}}).get("exposure", {}),
    }


@router.get("/chains")
def chains(db: Session = Depends(get_session)):
    """传导链图谱:节点代理序列注入最近读数(tail 90 点算 latest/1月变化)。"""
    data = _load_json("chains.json", {"chains": []})
    out = []
    for ch in data.get("chains", []):
        ch = dict(ch)
        for nd in ch.get("nodes", []):
            c = nd.get("code")
            if not c:
                continue
            tail = _series_tail(db, c, 60)
            if not tail:
                nd["live"] = {"latest": None, "stale": True}
                continue
            latest = tail[-1][1]
            base = next((v for d, v in tail
                         if dt.date.fromisoformat(d) >= dt.date.fromisoformat(tail[-1][0]) - dt.timedelta(days=30)),
                        latest)
            chg = round(latest / base - 1, 4) * 100 if base else None
            nd["live"] = {"latest": latest, "chg_1m": round(chg, 1) if chg is not None else None}
        out.append(ch)
    return {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated": data.get("updated"),
        "note": data.get("note"),
        "chains": out,
    }


@router.get("/candidates")
def candidates():
    """GDELT 候选事件雷达(采集产物直读;未采集/失败留旧档或空)。"""
    data = _load_json("candidates.json", {"articles": []})
    return {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated": data.get("updated_at"),
        "note": data.get("note"),
        "articles": data.get("articles", []),
    }


@router.get("/scenarios")
def scenarios():
    """情景树人工档(概率为主观估计·非模型输出)。"""
    data = _load_json("scenarios.json", {"sets": []})
    return {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated": data.get("updated"),
        "note": data.get("note"),
        "sets": data.get("sets", []),
    }


@router.get("/matrix")
def matrix():
    """敏感度矩阵(含历史弹性回测对照) + 历史剧本(合并端点:静态人工档)。"""
    m = _load_json("geo_matrix.json", {"categories": [], "rows": []})
    pb = _load_json("playbooks.json", {"playbooks": []})
    bt = _load_json("backtest.json", {"cells": {}})
    cats = m.get("categories", [])
    for row in m.get("rows", []):
        cells = row.get("cells") or []
        for ci, cell in enumerate(cells):
            if ci < len(cats):
                cell["bt"] = bt.get("cells", {}).get("%s|%s" % (row.get("ref"), cats[ci]))
    return {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated": m.get("updated"),
        "note": m.get("note"),
        "bt_note": bt.get("note"),
        "categories": cats,
        "rows": m.get("rows", []),
        "playbooks": pb.get("playbooks", []),
    }
