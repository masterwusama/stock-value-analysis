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

降级语义:兄弟 JSON 缺文件/缺 code/交集为空时,该派生序列从上一轮产物原样
继承(数据不丢,warnings 注记);两派生全失败才让 job 变红(exit=1)。

用法(backend/ 目录): python -m collector.run world-geo
产物: commodity-worldgeo/data/worldgeo.json → import_legacy --only-worldgeo
      回灌 cmd_indicator/cmd_value(commodity='worldgeo',GEO_ 前缀防互灌)。
调度: 每天 07:55(scheduler.py),晚于 gold(06:05)/crudeoil(06:55)/copper(06:25)。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-worldgeo" / "data"
OUT = DATA / "worldgeo.json"

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印派生计划,不读写文件")
    a = ap.parse_args()
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
    # 两派生全缺席才红:单序列继承是常态降级,兄弟模块次日自愈
    return 0 if ok >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
