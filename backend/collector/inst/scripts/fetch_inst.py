# -*- coding: utf-8 -*-
"""社保及外资持仓模块采集(2026-11-18 P0,设计:docs/设计-社保及外资持仓模块.md)。

源:东财数据中心 RPT_F10_EH_FREEHOLDERS(十大流通股东口径,pageSize=1000 实测可用)。
四类:社保/QFII/保险走 HOLDER_NEWTYPE 枚举;养老组合的 HOLDER_NEWTYPE 是「其他」,
按 HOLDER_TYPE=基本养老基金 定向拉(2026-11-18 枚举探测:基本养老基金 197 行,
香港中央结算/私募/员工持股等同在「其他」桶,不采)。

口径三坑(采集端处理 1、3,展示层处理 2):
  1. IS_MAX_REPORTDATE 是「该股该机构末次持有」标记,会混入 2005-2019 陈旧行——
     以行内标准报告期(REPORT_DATE_NAME 命中 20XX一季报/中报/三季报/年报)的最大
     END_DATE 为当期:当期之前的行丢弃(历史期由 backfill_inst.py 冻结回填),
     当期之后的(定增/回购股东名单等临时披露)is_std_period=false 一并入库;
  2. 前十大口径=进不了前十的持仓系统性缺席,不补全(页面固定横幅);
  3. 战略持股(汇丰/交行 25% 级)与交易性 QFII 在表内无差别,采集端不删,
     展示层按 ≥5% 且连续多期「不变」打标签可过滤。

用法(backend/): python -m collector.run inst   (调度:每周日 10:25)
产物: collector/inst/data/freeholders_{当期YYYYMMDD}.json(当期+期后临时披露)
      + data/manifest.json(行数断言/updated_at,回灌端不依赖)
回灌: --only-inst → inst_holding
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "inst" / "data"

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126'}
SLEEP = 0.4
RETRY = 3
PAGE = 1000  # 2026-11-18 实测 pageSize=1000 生效(786 行社保一页拿完)

# 行数断言(设计:EM 页面会迁移,jgcc.html 404 前科)——低于软阈值打警告,零行硬失败
MIN_ROWS = {"社保": 500, "QFII": 1500, "保险": 500, "养老": 50}

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE = ('https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_F10_EH_FREEHOLDERS&source=WEB&client=WEB'
        '&pageSize=%d&pageNumber=%%d&columns=ALL&filter=%%s' % PAGE)

# holder_type 归一 → 源侧过滤(社保/QFII/保险按 HOLDER_NEWTYPE;养老按 HOLDER_TYPE)。
# 当期行用 END_DATE=当期 直拉(IS_MAX 标记会漏「三季报在列+期后又在临时名单」的
# 组合,2026-11-18 实测社保漏 42 行);期后临时披露用 IS_MAX 拉取切出;陈旧行丢弃。
TYPES = [
    ("社保", '(HOLDER_NEWTYPE="社保")'),
    ("QFII", '(HOLDER_NEWTYPE="QFII")'),
    ("保险", '(HOLDER_NEWTYPE="保险")'),
    ("养老", '(HOLDER_TYPE="基本养老基金")'),
]

STD_RE = re.compile(r"^20\d{2}(一季报|中报|三季报|年报)$")


def fetch(url):
    last = None
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(
                req, timeout=25, context=ctx).read().decode('utf-8', 'ignore'))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(SLEEP * (i + 2))
    raise RuntimeError('fetch failed: %s (%r)' % (url[:120], last))


def fetch_all(filter_expr):
    """一个过滤表达式全量翻页;pageSize=1000 下社保 1 页/QFII 3 页。"""
    rows, page = [], 1
    while True:
        d = fetch(BASE % (page, urllib.parse.quote(filter_expr)))
        r = d.get('result')
        if not r or not r.get('data'):
            break
        rows.extend(r['data'])
        if len(rows) >= r.get('count', 0) or page >= 60:
            break
        page += 1
        time.sleep(SLEEP)
    return rows


def _num(v):
    """源侧数值列混排数值与字符串(如 FREE_RATIO_QOQ='1.85827822'/'不变')。"""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_row(r, holder_type):
    """源行 → 库行;HOLD_NUM_CHANGE 混排「新进/减持/不变」与增减数,拆两列。"""
    raw_change = r.get('HOLD_NUM_CHANGE')
    change_num = _num(raw_change)
    if change_num is not None:
        change_type = "增持" if change_num > 0 else "减持" if change_num < 0 else "不变"
    else:
        change_type = str(raw_change) if raw_change else None
    report_name = r.get('REPORT_DATE_NAME') or None
    return {
        "holder_code": str(r.get('HOLDER_NEW') or r.get('HOLDER_CODE') or '').strip(),
        "holder_name": (r.get('HOLDER_NAME') or '').strip(),
        "holder_type": holder_type,
        "holder_type_sub": r.get('HOLDER_TYPE'),
        "secu_code": str(r.get('SECURITY_CODE') or '').strip(),
        "secu_name": (r.get('SECURITY_NAME_ABBR') or '').strip(),
        "end_date": (r.get('END_DATE') or '')[:10],
        "report_name": report_name,
        "is_std_period": bool(report_name and STD_RE.match(report_name)),
        "hold_num": _num(r.get('HOLD_NUM')),
        "free_ratio": _num(r.get('FREE_HOLDNUM_RATIO')),
        "free_ratio_qoq": _num(r.get('FREE_RATIO_QOQ')),
        "change_type": change_type,
        "change_num": change_num,
        "hold_mktcap": _num(r.get('HOLDER_MARKET_CAP')),
        "holder_rank": int(r['HOLDER_RANK']) if r.get('HOLDER_RANK') else None,
        "shares_type": r.get('SHARES_TYPE') or 'A股',
    }


def main():
    now = datetime.now()
    warns = []
    per_type = {}
    for tname, fexpr in TYPES:
        # 第一步:IS_MAX 拉取(末次持有行,量小)定当期并切期后临时披露。
        # 翻期护栏:财报季早期新期样本稀疏(一两家先披露就翻期,当期文件变成稀疏
        # 半成品),新期标准期行数须 ≥ 上一期 50%(且 ≥50 行)才翻,否则维持上一期
        rows_max = [norm_row(r, tname) for r in fetch_all(fexpr + '(IS_MAX_REPORTDATE="1")')]
        cnt = {}
        for x in rows_max:
            if x["is_std_period"]:
                cnt[x["end_date"]] = cnt.get(x["end_date"], 0) + 1
        if not cnt:
            warns.append(f"{tname}: 无标准报告期行(源结构变更?)")
            per_type[tname] = {"std": 0, "adhoc": 0, "period": None, "rows": []}
            continue
        periods_desc = sorted(cnt, reverse=True)
        period = periods_desc[0]
        for i, p in enumerate(periods_desc):
            nxt = periods_desc[i + 1] if i + 1 < len(periods_desc) else None
            if nxt is None or cnt[p] >= max(50, int(cnt[nxt] * 0.5)):
                period = p
                break
        # 第二步:当期 END_DATE 直拉补全(IS_MAX 会漏期后又上临时名单的组合)
        std_rows = [norm_row(r, tname) for r in
                    fetch_all("%s(END_DATE='%s')" % (fexpr, period))]
        adhoc = [x for x in rows_max if x["end_date"] > period]
        keep = std_rows + adhoc
        lo = MIN_ROWS.get(tname, 0)
        if len(std_rows) == 0:
            warns.append(f"{tname}: 当期 {period} 标准期 0 行(源结构变更?)")
        elif len(std_rows) < lo:
            warns.append(f"{tname}: 当期 {period} 行数 {len(std_rows)} < 软阈值 {lo}(页面迁移前科,人工核对)")
        per_type[tname] = {
            "std": len(std_rows), "adhoc": len(adhoc), "period": period,
            "std_mktcap_yi": round(sum(x["hold_mktcap"] or 0 for x in std_rows) / 1e8, 1),
            "rows": keep,
        }
        print(f'[inst] {tname}: 当期 {period} 标准期 {len(std_rows)} 行 + 临时披露 {len(adhoc)} 行'
              f' + 丢弃陈旧行 {len(rows_max) - len(adhoc)}', flush=True)
        time.sleep(SLEEP)

    # 全局当期 = 各类标准期的最大者(养老偶发滞后一期属正常,行内 END_DATE 自带)
    periods = [v["period"] for v in per_type.values() if v.get("period")]
    if not periods:
        print('[inst] 全部类型无当期数据,不落盘', flush=True)
        return 1
    period = max(periods)
    # 防御性去重:PK 五元组(含 holder_name——同机构战略/QFII 双名单靠名称后缀
    # 区分,源侧同 MXID 噪声行名称全同恰好被去重),最后写入者胜
    rows, seen = [], set()
    for v in per_type.values():
        for x in v["rows"]:
            key = (x["holder_code"], x["holder_name"], x["secu_code"],
                   x["end_date"], x["shares_type"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(x)

    DATA.mkdir(parents=True, exist_ok=True)
    period_tag = period.replace("-", "")
    payload = {"period": period, "updated_at": now.isoformat(timespec="seconds"),
               "rows": rows}
    (DATA / f"freeholders_{period_tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    manifest = {
        "updated_at": now.isoformat(timespec="seconds"), "period": period,
        "report_name": next((x["report_name"] for v in per_type.values() for x in v["rows"]
                             if x["end_date"] == period and x["is_std_period"]), None),
        "types": {t: {k: v2 for k, v2 in v.items() if k != "rows"}
                  for t, v in per_type.items()},
        "kept_rows": len(rows), "warns": warns,
    }
    (DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f'[inst] 当期 {period}:落盘 {len(rows)} 行 → freeholders_{period_tag}.json', flush=True)
    for w in warns:
        print(f'[inst][警告] {w}', flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
