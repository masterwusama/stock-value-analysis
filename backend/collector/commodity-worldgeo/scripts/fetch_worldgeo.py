# -*- coding: utf-8 -*-
"""世界局势推演模块·派生序列采集(2026-11-15 起)。

定位:局势温度计的跨商品比价派生层。设计文档:docs/设计-世界局势推演.md。

与其它 commodity-* 采集器的根本差异:**零网络请求**。原料全部来自兄弟模块
已落盘的 JSON(gold/crudeoil/copper——它们 06:05~06:55 先跑,本任务 07:55
殿后保证当日新点已就位),本脚本只做按日内连接相除:

    GEO_GOLD_OIL = gold.XAU_USD ÷ crudeoil.CL_MAIN        金油比(盎司/桶)
                   避险×能源双温度:飙升=避险与供给冲击共振(海湾战争/2020-04/俄乌),
                   温和走低=需求主导的周期行情。
    GEO_OIL_CU   = crudeoil.CL_MAIN ÷ copper.CAD_MAIN ×1000  油铜比(×1000)
                   通胀交易 vs 增长交易:走高=供给冲击/通胀担忧主导,走低=需求
                   衰退定价(铜先崩)或供给过剩(油先崩)——方向语义看分母谁先动。

温度计其余 8 个指标(GPR/VIXCLS/金银比/铜金比/布伦特-WTI/美元宽/10Y实际利率/
人民币中间价)不在此重复注册——复用序列已在 cmd_value(gold/copper/crudeoil 行),
同 code 二次注册会被 _import_cmd_series 的 upsert 翻转 commodity 字段(互灌),
API /api/world-geo/overview 直接按 code 跨 commodity 读库合成。

P1/P2 扩展(2026-11-16):
    --backtest   矩阵经验弹性回测:读 history_events.json(~40 个历史事件)+
                 geo_matrix.json + 兄弟序列,对每个 (资产,事件类别) 格子算
                 「事件日 T→T+30 自然日窗口内代理序列涨跌」分布(样本数/均值/
                 方向命中率) → backtest.json。独立 job world-geo-backtest
                 (每月 1 号 08:05,NO_IMPORT——纯文件工件),也可手动跑。
    GDELT 候选雷达:每天派生后顺手抓 GDELT DOC 2.0 API(免费无 key)的世界
                 局势英语新闻候选(ceasefire/sanctions/OPEC/Hormuz 等),
                 落 candidates.json 供「候选雷达」人工审阅转正——只做发现
                 不自动入事件库。网络失败静默降级(留旧档),不影响派生主线。

降级语义:兄弟 JSON 缺文件/缺 code/交集为空时,该派生序列从上一轮产物原样
继承(数据不丢,warnings 注记);两派生全失败才让 job 变红(exit=1)。

用法(backend/ 目录): python -m collector.run world-geo
产物: commodity-worldgeo/data/worldgeo.json → import_legacy --only-worldgeo
      回灌 cmd_indicator/cmd_value(commodity='worldgeo',GEO_ 前缀防互灌)。
调度: 每天 07:55(scheduler.py),晚于 gold(06:05)/crudeoil(06:55)/copper(06:25)。
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-worldgeo" / "data"
OUT = DATA / "worldgeo.json"
BACKTEST_OUT = DATA / "backtest.json"
CAND_OUT = DATA / "candidates.json"

# 原料来源(兄弟模块产物;只读不写)
SRC = {
    "gold": BACKEND / "collector" / "commodity-gold" / "data" / "gold.json",
    "crudeoil": BACKEND / "collector" / "commodity-crudeoil" / "data" / "crudeoil.json",
    "copper": BACKEND / "collector" / "commodity-copper" / "data" / "copper.json",
}

# 派生定义:(code, 分子模块, 分子code, 分母模块, 分母code, scale, 小数位)
DERIVED = [
    ("GEO_GOLD_OIL", "gold", "XAU_USD", "crudeoil", "CL_MAIN", 1.0, 2),
    ("GEO_OIL_CU", "crudeoil", "CL_MAIN", "copper", "CAD_MAIN", 1000.0, 2),
]

# 展示清单(顺序=页面序;世界局势页不用这个折叠区,cat/group 仅入库元数据)
SPEC = {
    "GEO_GOLD_OIL": ("金油比(伦敦金÷WTI)", "金油比", "跨商品比价", "局势比价",
                     "盎司/桶", "日", "自算(gold.XAU_USD÷crudeoil.CL_MAIN)"),
    "GEO_OIL_CU": ("油铜比(WTI÷LME铜×1000)", "油铜比", "跨商品比价", "局势比价",
                   "比值×1000", "日", "自算(crudeoil.CL_MAIN÷copper.CAD_MAIN×1000)"),
}

# 原料新鲜度红线(天):兄弟 JSON 的 updated_at 超过该天数,派生值可能在用旧点
SRC_STALE_DAYS = 3


def _load_series():
    """读三个兄弟 JSON → ({code: [(date,val)]}, warnings)。"""
    pts, warns = {}, []
    for key, path in SRC.items():
        if not path.exists():
            warns.append("%s: 产物不存在(%s),派生走继承" % (key, path.name))
            continue
        try:
            src = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 损坏文件按缺席处理
            warns.append("%s: 解析失败 %s" % (key, type(e).__name__))
            continue
        for s in src.get("series", []):
            pts[s["code"]] = [
                (p[0], float(p[1])) for p in s.get("points", [])
                if len(p) == 2 and p[0] and p[1] is not None
            ]
        # 新鲜度:updated_at 太旧提示(不阻断,派生仍做,表层会显示陈旧)
        try:
            upd = datetime.fromisoformat(src.get("updated_at") or "")
            age = (datetime.now().astimezone() - upd).days
            if age > SRC_STALE_DAYS:
                warns.append("%s: 产物已 %d 天未更新(%s),当日派生基于旧原料" % (
                    key, age, upd.date().isoformat()))
        except (ValueError, TypeError):
            pass
    return pts, warns


def _ratio(a_pts, b_pts, scale, digits):
    """按日内连接相除 a/b×scale,双正向值过滤。"""
    mb = dict(b_pts)
    out = []
    for d, v in a_pts:
        w = mb.get(d)
        if w and w > 0 and v > 0:
            out.append((d, round(v / w * scale, digits)))
    return sorted(out)


# ---- P2:矩阵经验弹性回测(--backtest 模式,独立 job) ----
# 资产代理序列映射:矩阵行的资产 ref → 兄弟 JSON 里的 code(sector 面不回测——
# 板块无单序列代理,quote_daily 不在 JSON 侧)
_BACKTEST_CODE = {
    "gold": ("gold", "XAU_USD"), "silver": ("gold", "XAG_USD"),
    "copper": ("copper", "CAD_MAIN"), "aluminum": ("aluminum", "AHD_MAIN"),
    "lithium": ("lithium", "LC0_CLOSE"), "crudeoil": ("crudeoil", "CL_MAIN"),
    "ferrous": ("ferrous", "RB0_CLOSE"), "agri": ("agri", "LH0_CLOSE"),
    "fx_cny": ("gold", "USDCNY_FIX"),
}
_BT_WINDOW_DAYS = 30


def run_backtest():
    """对矩阵每个 (资产,事件类别) 格子回测历史弹性。

    口径:该类别全部历史事件 T→T+30 自然日内代理序列首末涨跌的分布。
    hit=方向与矩阵先验(d)一致的比例(d=0 的格子 hit=None——先验中性,无方向可命中)。
    相邻事件样本相关(如 2022-02 三连),均值仅供参考、样本数是主读数。
    """
    he = json.loads((DATA / "history_events.json").read_text(encoding="utf-8"))
    mx = json.loads((DATA / "geo_matrix.json").read_text(encoding="utf-8"))
    raw, warns = _load_series()
    cats = mx["categories"]
    cells = {}
    for row in mx["rows"]:
        ref = row["ref"]
        src = _BACKTEST_CODE.get(ref)
        seq = raw.get(src[1]) or [] if src else []
        mseq = {d: v for d, v in seq}
        ds = sorted(mseq)
        for ci, cat in enumerate(cats):
            cell = (row.get("cells") or [{}] * len(cats))[ci]
            d_pri = cell.get("d", 0)
            chgs = []
            for ev in he.get("events", []):
                if ev.get("category") != cat:
                    continue
                try:
                    t0 = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                except (ValueError, KeyError, TypeError):
                    continue
                t1 = t0 + timedelta(days=_BT_WINDOW_DAYS)
                win = [mseq[d] for d in ds if t0.isoformat() <= d <= t1.isoformat()]
                if len(win) >= 2 and win[0]:
                    chgs.append((win[-1] / win[0] - 1) * 100)
            key = "%s|%s" % (ref, cat)
            if chgs:
                hit = None
                if d_pri > 0:
                    hit = round(sum(1 for c in chgs if c > 0) / len(chgs), 2)
                elif d_pri < 0:
                    hit = round(sum(1 for c in chgs if c < 0) / len(chgs), 2)
                cells[key] = {
                    "n": len(chgs), "mean": round(sum(chgs) / len(chgs), 1), "hit": hit,
                }
            else:
                cells[key] = {"n": 0, "mean": None, "hit": None}
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window_days": _BT_WINDOW_DAYS,
        "note": ("矩阵经验弹性回测:历史事件 T→T+30 自然日窗口内资产代理序列首末涨跌分布。"
                 "n=样本数(主读数,相邻事件样本相关如 2022-02 三连,均值仅供参考);"
                 "hit=实际涨跌方向与矩阵先验一致的比例(n<3 统计意义弱);"
                 "hit=null=先验中性(d=0)无方向可命中;央行储备列是结构性 regime 无离散事件(n=0)。"),
        "cells": cells,
    }
    BACKTEST_OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    filled = sum(1 for c in cells.values() if c["n"])
    print("[worldgeo] 回测写盘 %s: %d 格,有样本 %d 格" % (
        BACKTEST_OUT.name, len(cells), filled), flush=True)
    return 0


# ---- P2:GDELT 候选雷达(每天派生后顺手抓,失败静默降级) ----
# 降噪双层:①查询用「资产词 AND 事件词」共现(单关键词 gold/OPEC 会命中金奖/明星等海量噪音
# ——首跑实测 30 条里"Gold Duke of Edinburgh"级噪音占多数);②标题白名单二次过滤。
# 宁缺毋滥:过滤后不足 5 条也如实落盘,候选是线索不是结论。
GDELT_URL = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s"
             "&mode=ArtList&maxrecords=75&format=json&sort=DateDesc")
GDELT_QUERY = ('(oil OR crude OR gold OR silver OR copper OR aluminum OR nickel OR wheat '
               'OR tanker OR freight) AND (sanctions OR ceasefire OR "export control" OR '
               'Hormuz OR "Red Sea" OR OPEC OR tariff OR embargo OR blockade OR '
               '"production cut" OR strike) sourcelang:english')
# 标题白名单用 \b 词边界正则("war" 子串会误命中 award/warranty——首跑实测踩坑)
_TITLE_RE = re.compile(
    r"\b(ceasefire|sanctions?|opec|hormuz|red sea|tariffs?|embargo|crude|oil price|brent|wti"
    r"|gold price|bullion|gold market|export control|export ban|export restriction|blockade"
    r"|drone attack|attack on|strike on|pipeline|tankers?|freight|supply cut|production cut"
    r"|output cut|central bank gold|war)\b")


def fetch_candidates():
    """GDELT DOC 2.0 文章候选:世界局势英语新闻雷达,人工审阅转正事件。

    GDELT DOC API 有请求频率限制(限流文案「one every 5 seconds」,实测 IP 级
    冷却可达分钟级)——本任务每天仅一次,加 sleep 20 重试一次兜短窗限流;再失败
    静默降级留旧档(调用方 try 包裹)。
    """
    import urllib.parse
    import urllib.request
    url = GDELT_URL % urllib.parse.quote(GDELT_QUERY)
    data = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            break
        except json.JSONDecodeError:
            # 限流文案是纯文本,JSON 解析失败=限流/错误页——退避后重试一次
            if attempt == 0:
                print("[worldgeo] GDELT 限流(返回非 JSON),20 秒后重试一次", flush=True)
                time.sleep(20)
                continue
            raise
    if data is None:
        raise ValueError("GDELT 两次尝试均未返回 JSON")
    arts, seen = [], set()
    for a in data.get("articles", []):
        title = (a.get("title") or "").strip()
        tl = title.lower()
        if not title or title[:60].lower() in seen:
            continue
        if not _TITLE_RE.search(tl):
            continue  # 标题白名单二次过滤(词边界正则,查询共现后仍有周边噪音)
        seen.add(title[:60].lower())
        arts.append({
            "date": (a.get("seendate") or "")[:15],
            "title": title, "url": a.get("url") or "", "domain": a.get("domain") or "",
        })
    if not arts:
        raise ValueError("GDELT 过滤后空返回(宁缺毋滥)")
    payload = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
               "query": GDELT_QUERY,
               "note": ("候选事件雷达:GDELT 自动抓取的局势相关英语新闻(资产词×事件词共现查询+标题白名单"
                        "二次过滤),只做发现线索,转正事件需人工核对后写入 events.json(不自动入库)。"),
               "articles": arts}
    CAND_OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("[worldgeo] 候选雷达: %d 条 → %s" % (len(arts), CAND_OUT.name), flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印派生计划,不读写文件")
    ap.add_argument("--backtest", action="store_true",
                    help="只跑矩阵经验弹性回测写 backtest.json,不做派生/不回灌(独立 job)")
    ap.add_argument("--no-candidates", action="store_true",
                    help="跳过 GDELT 候选雷达抓取")
    a = ap.parse_args()
    if a.backtest:
        return run_backtest()
    plan = ["%s = %s.%s ÷ %s.%s ×%g" % t for t in
            ((d[0], d[1], d[2], d[3], d[4], d[5]) for d in DERIVED)]
    print("[worldgeo] 派生计划: %s" % "; ".join(plan), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []
    raw, warns = _load_series()
    warnings += warns

    # 上一轮产物(继承用):兄弟原料缺席的派生序列原样带过
    prev = {}
    if OUT.exists():
        try:
            for s in json.loads(OUT.read_text(encoding="utf-8")).get("series", []):
                prev[s["code"]] = [(p[0], float(p[1])) for p in s.get("points", [])]
        except Exception:  # noqa: BLE001 旧档读不了就走全量重算路径
            prev = {}

    series_out = []
    ok = 0
    for idx, (code, src_mod, src_code, dst_mod, dst_code, scale, digits) in enumerate(DERIVED):
        a_pts = raw.get(src_code) or []
        b_pts = raw.get(dst_code) or []
        pts = _ratio(a_pts, b_pts, scale, digits) if a_pts and b_pts else []
        if pts:
            ok += 1
            print("[worldgeo] %s: %d 点 %s~%s" % (
                code, len(pts), pts[0][0], pts[-1][0]), flush=True)
        else:
            pts = prev.get(code) or []
            if pts:
                warnings.append("%s: 原料缺席(%s/%s),继承上轮 %d 点(截至 %s)" % (
                    code, src_code, dst_code, len(pts), pts[-1][0]))
                print("[worldgeo] %s 继承上轮: %d 点(截至 %s)" % (
                    code, len(pts), pts[-1][0]), flush=True)
            else:
                warnings.append("%s: 首跑且原料缺席,本轮无序列" % code)
        name, label, cat, group, unit, freq, source = SPEC[code]
        if pts:
            series_out.append({
                "code": code, "idx": idx, "name": name, "label": label,
                "cat": cat, "group": group, "unit": unit, "freq": freq,
                "source": source,
                "points": [[d, v] for d, v in pts],
            })

    all_ds = [p[0] for s in series_out for p in s["points"]]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commodity": "worldgeo",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[worldgeo] 写盘 %s: %d 序列 %d 点,warning %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ";".join(w.split(":")[0] for w in warnings) or "-",
        time.time() - t0), flush=True)

    # GDELT 候选雷达:主线的附加层,失败只记 print 不动 warnings/job 色
    if not a.no_candidates:
        try:
            fetch_candidates()
        except Exception as e:  # noqa: BLE001 候选雷达是辅助面,降级不红
            print("[worldgeo] 候选雷达降级: %s %s(留旧档)" % (
                type(e).__name__, str(e)[:100]), flush=True)

    # 两派生全缺席才红:单序列继承是常态降级,兄弟模块次日自愈
    return 0 if ok >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
