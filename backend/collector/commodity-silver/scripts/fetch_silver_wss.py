# -*- coding: utf-8 -*-
"""白银供需平衡表·Silver Institute WSS 年报解析(P1,2026-10-27 起)。

World Silver Survey 官网免费直链 PDF(无需注册,优于黄金 WGC 的注册墙),
每年 4 月发布上一年全量平衡表。本脚本解析 Appendix 1 主表(2017→+当年F 预测,
约 10-11 年),产出 11 条年度序列并入白银页「供需平衡」分组:
    供给   WSS_SUPPLY 总供给 / WSS_MINE 矿产 / WSS_RECYCLE 回收
    需求   WSS_DEMAND 总需求 / WSS_INDUSTRIAL 工业 / WSS_PV 光伏 /
           WSS_COIN 金条金币 / WSS_JEWELRY 珠宝
    平衡   WSS_BALANCE 市场平衡(负=短缺) / WSS_ETP ETF净投资(吨) /
           WSS_BAL_EX_ETP 扣 ETF 后平衡(观察"自由市场真实缺口")

数据链: silverinstitute.org PDF → 本地缓存 data/wss*.pdf → silver_wss.json
→ import_legacy --only-silver(import_silver 合并读 silver.json + 本文件)。
调度: 月频(每月 1 号 07:25,silver-wss job)——新版发布后次月自动捡起;
当年 URL 未发布(每年 1-4 月)自动回落上年文件。

用法(backend/ 目录): python -m collector.run silver-wss
"""
import argparse
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import os
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests  # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-silver" / "data"
OUT = DATA / "silver_wss.json"
WSS_URL = "https://silverinstitute.org/wp-content/uploads/%d/04/World-Silver-Survey-%d.pdf"

S_SI = "The Silver Institute World Silver Survey(年报,免费直链;次年 4 月发布)"

# 行名(与 PDF extract_text 匹配;长名在前+一行只归一个规则,防前缀覆盖) → (code, 中文名)
ROWS = [
    ("Market Balance less ETPs", "WSS_BAL_EX_ETP", "市场平衡·扣ETF后"),
    ("which photovoltaics", "WSS_PV", "光伏用银需求"),  # 行首是省略号,用子串匹配
    ("Market Balance", "WSS_BALANCE", "市场平衡(供给-需求)"),
    ("Net Investment in ETPs", "WSS_ETP", "ETF净投资"),
    ("Total Supply", "WSS_SUPPLY", "白银总供给"),
    ("Mine Production", "WSS_MINE", "矿产银产量"),
    ("Recycling", "WSS_RECYCLE", "回收银供给"),
    ("Total Demand", "WSS_DEMAND", "白银总需求"),
    ("Industrial (total)", "WSS_INDUSTRIAL", "工业需求(合计)"),
    ("Coin & Net Bar Demand", "WSS_COIN", "金条金币投资需求"),
    ("Jewelry", "WSS_JEWELRY", "珠宝需求"),
]

# 页面顺序(供需平衡组内);与 ROWS 的解析顺序无关
SPEC = [
    ("WSS_BALANCE", "市场平衡(供给−需求)", "市场平衡", "平衡", "吨",
     "负值=短缺。2021 起连续第 6 年短缺,2022 年最深(-7,899 吨)。"),
    ("WSS_ETP", "ETF净投资(吨)", "ETF净投资", "平衡", "吨",
     "全球白银 ETF 年度净流入。近年流入被用于弥合实物缺口。"),
    ("WSS_BAL_EX_ETP", "市场平衡·扣ETF后", "扣ETF平衡", "平衡", "吨",
     "剔除 ETF 买入后的真实缺口——比表观平衡更紧,衡量自由市场紧张度。"),
    ("WSS_SUPPLY", "白银总供给", "总供给", "供给", "吨", "矿产+回收+净对冲释放。"),
    ("WSS_MINE", "矿产银产量", "矿产银", "供给", "吨",
     "约 70% 是金/铜/铅锌伴生矿,供给对银价不敏感(刚性)。"),
    ("WSS_RECYCLE", "回收银供给", "回收银", "供给", "吨", "再生银,随银价升而增(价格弹性供给)。"),
    ("WSS_DEMAND", "白银总需求", "总需求", "需求", "吨", "工业+投资+珠宝+银器合计。"),
    ("WSS_INDUSTRIAL", "工业需求(合计)", "工业需求", "需求", "吨", "约占总需求 59%,白银的制造业属性来源。"),
    ("WSS_PV", "光伏用银需求", "光伏用银", "需求", "吨",
     "白银最大单一工业需求。2025 见顶回落——银浆单耗下降开始覆盖装机增长。"),
    ("WSS_COIN", "金条金币投资需求", "金条金币", "需求", "吨", "零售投资需求,避险情绪温度计。"),
    ("WSS_JEWELRY", "珠宝需求", "珠宝需求", "需求", "吨", "价格敏感型消费,银价高企时受抑。"),
]


def ensure_pdf():
    """本地最新 wss*.pdf;无缓存或当年(≥5 月)应有新版时尝试下载,当年 404 回落上年。"""
    import datetime as _dt
    cache = sorted(DATA.glob("wss*.pdf"))
    year_now = _dt.date.today().year
    new_out = _dt.date.today().month >= 5  # 每年 4 月发布,5 月起应有当年版
    want = [year_now, year_now - 1] if new_out else [year_now - 1, year_now - 2]
    for y in want:
        local = DATA / ("wss%d.pdf" % y)
        if local.exists():
            return local
        try:
            r = requests.get(WSS_URL % (y, y), timeout=240)
            if r.status_code == 200 and len(r.content) > 1e6:
                local.write_bytes(r.content)
                print("[silver-wss] 下载 %d 版 %dKB" % (y, len(r.content) // 1024), flush=True)
                return local
        except Exception as e:  # noqa: BLE001
            print("[silver-wss] 下载 %d 版失败: %s" % (y, str(e)[:60]), flush=True)
            time.sleep(2)
    return cache[-1] if cache else None


def parse(pdf_path):
    import pdfplumber
    pdf = pdfplumber.open(pdf_path)
    table_page = None
    for page in pdf.pages:
        t = page.extract_text() or ""
        if "Appendix 1" in t and "Total Supply" in t:
            table_page = t
            break
    if table_page is None:
        raise ValueError("未找到 Appendix 1 平衡表页")
    lines = table_page.splitlines()
    # 表头年份行: 'Tons 2017 2018 ... 2026F 2025 2026F'(定位以 Tons 开头的行)
    years = []
    for ln in lines:
        if ln.strip().startswith("Tons 20"):
            years = sorted({int(x) for x in re.findall(r"(20\d{2})F?", ln)})[:11]
            break
    if not years:
        raise ValueError("未找到表头年份行(Tons 20xx)")
    out = {}
    for line in lines:
        for row_name, code, _cn in ROWS:
            # 一行只归一个规则(长前缀已在 ROWS 前列,防 less-ETPs 行被 Market Balance 覆盖)
            if row_name in line:
                # 逐 token 解析:'3%'/'na' 等 YoY 列自然过滤,取前 N 个纯数字
                vals = []
                for tok in line.split():
                    try:
                        vals.append(float(tok.replace(",", "")))
                    except ValueError:
                        continue
                if len(vals) >= len(years) - 1:  # 至少缺一个预测列也可接受
                    out[code] = [(f"{y}-01-01", vals[i]) for i, y in enumerate(years)
                                 if i < len(vals)]
                break
    return out, years


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print("[silver-wss] P1: Silver Institute 平衡表解析", flush=True)
    if a.dry_run:
        return 0
    t0 = time.time()
    pdf = ensure_pdf()
    if pdf is None:
        print("[silver-wss] 无 PDF 可用(本地无缓存且下载失败),留旧数据", flush=True)
        return 1
    parsed, years = parse(pdf)
    if len(parsed) < 8:
        print("[silver-wss] 解析行数不足(%d),疑似版式变化,人工核对", flush=True)
        return 1
    series_out = []
    for i, (code, name, label, group, unit, hint) in enumerate(SPEC):
        pts = parsed.get(code)
        if not pts:
            continue
        series_out.append({
            "code": code, "idx": 100 + i, "name": name, "label": label,
            "cat": "供需平衡", "group": group, "unit": unit, "freq": "年",
            "source": S_SI + "(%s 版,%d-%d)" % (pdf.stem[-4:], years[0], years[-1]),
            "points": [[d, round(v, 1)] for d, v in pts],
        })
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commodity": "silver",
        "pdf": pdf.name,
        "range": {"begin": "%d-01-01" % years[0], "end": "%d-01-01" % years[-1]},
        "warnings": [],
        "series": series_out,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[silver-wss] 写盘 %s: %d 序列 %d 点(%s,%s~%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, pdf.name,
        "%d" % years[0], "%d" % years[-1], time.time() - t0), flush=True)
    for s in series_out:
        print("  %s: %s" % (s["code"], s["points"][-2:]), flush=True)
    return 0 if len(series_out) >= 8 else 1


if __name__ == "__main__":
    sys.exit(main())
