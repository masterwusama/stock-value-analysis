# -*- coding: utf-8 -*-
"""行业 EDB 量价抓取器（汽车 / 电解铝 / 航运 / 轮胎橡胶 / 地产链 / 煤炭 / 钢铁）。

数据源：本地 Wind 金融能力 .agents/skills/wind-mcp-skill/scripts/cli.mjs
        economic_data.query_economic_indicator_data（question 直接传 EDB 代码，逗号分隔）。

策略（对齐"周/月聚合、省积分"）：
  - 一次调用按分类批量传该类的多个 EDB 代码（共享同一日期区间），减少调用数。
  - 日频序列自动聚合到"每周最后交易日的值"（周中密度 > 1/周 才折叠）；
    周频、月频原生序列保持不变（月频无法由更少数据补出，原样保留）。
  - 只保留最近一年（--begin/--end，默认今天往前 365 天）；新分类单独 --only
    --begin 2025-01-01 抓取后合并写回，分类级 range 记录各自区间。
  - 默认走增量窗口：起点取该分类各指标在现存 edb.json 里最旧的那个“最新点”，
    并往后封顶 --max-span-days（周任务下就是“只补最近 1~2 个月”），比固定 365 天
    窗口少要 5~30 倍数据量。人工回填历史用 --begin 显式指定，不受此限。
    跑之前用 --dry-run 看本轮计划（几次调用、几个指标、多少天窗口），不碰 Wind。

产物：../data/edb.json  结构：
  { "updated_at", "range":{begin,end}, "categories":[
      { "id","name","indicators":[{"code","name","unit","freq","source",
                                   "points":[["YYYY-MM-DD", value], ...]}] } ] }

用法：python fetch_edb.py            # 抓取全部分类
      python fetch_edb.py --only alu # 只抓某一类（id）

调度：2026-09-03 起不进自动链（原先挂在 agro job 里每天跟生意社价格跑两轮）。上面策略写的就是
      “周/月聚合、省积分”的低频序列，放进日更链本无意义，却要把一个自动链管不到的外部
      依赖（本机 Wind 客户端）掺进来：2026-09-03 09:05 那轮 `fetch_edb.py exit=1` 直接把
      agro 记成 failed（etl_job_log 里 agro 至今唯一一条记录），而价格数据本身已落盘、
      回灌也成功了。要更新跑 python -m collector.run edb（带回灌）。
"""
import argparse
import datetime as dt
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 仓库根 = stock-data/agro-price/scripts -> 上溯三级
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
# [collector 改造] Wind MCP skill 在博客仓,仓库相对路径失效,用 WIND_SKILL_DIR 环境变量定位
SKILL_DIR = os.environ.get("WIND_SKILL_DIR") or os.path.join(REPO, ".agents", "skills", "wind-mcp-skill")
CLI = os.path.join("scripts", "cli.mjs")
OUT = os.path.join(HERE, "..", "data", "edb.json")

# 分类 -> 指标清单（code, 展示名, 分组标签）。展示名以本地探测为准，
# 单位/频率最终以 Wind 返回 meta 为准。
CATEGORIES = [
    {
        "id": "auto", "name": "汽车",
        # 口径统一用中汽协（月度完整，无统计局 1-2 月合并发布导致的缺口）；单位「辆」。
        "indicators": [
            ("S0105523", "汽车产量", "产量"),
            ("S0105710", "汽车销量", "产量"),
            ("S0105526", "乘用车产量", "产量"),
            ("S6139215", "新能源汽车产量", "新能源"),
            ("S6139212", "新能源汽车销量", "新能源"),
            ("X2694913", "新能源渗透率", "新能源"),
            ("S0105689", "比亚迪产量", "厂商"),
        ],
    },
    {
        "id": "alu", "name": "电解铝",
        "indicators": [
            ("S0179655", "铝锭A00现货", "价格"),
            ("Z9174481", "氧化铝", "成本"),
            ("S0029755", "LME铝", "价格"),
            ("S0031718", "铝锭月均价", "价格"),
        ],
    },
    {
        "id": "shipping", "name": "航运",
        "indicators": [
            ("S0000066", "CCFI综合", "集运指数"),
            ("S0114089", "SCFI综合", "集运指数"),
            ("S0000073", "CCFI美西", "航线"),
            ("S0000075", "CCFI欧洲", "航线"),
            ("S0000072", "CCFI美东", "航线"),
            ("S0000069", "CCFI东南亚", "航线"),
            ("D9483906", "沿海散货", "散货油运"),
            ("S0031553", "BDTI原油运输", "散货油运"),
            ("S0031550", "BDI干散货", "散货油运"),
        ],
    },
    {
        "id": "tire", "name": "轮胎橡胶",
        # 产业链：产量/出口（需求）+ 开工率（景气）+ 天然/合成橡胶（原料成本）。
        # 价格类为日频，脚本自动折周；产量用橡胶信息贸易网口径（月度连续，无统计局 1-2 月缺口）。
        "indicators": [
            ("F0040955", "轮胎产量", "产销"),
            ("S0270241", "橡胶轮胎出口额", "产销"),
            ("S9987482", "全钢胎开工率", "开工率"),
            ("S6124651", "半钢胎开工率", "开工率"),
            ("S5470428", "天然橡胶", "原料价格"),
            ("S5470420", "丁苯橡胶", "原料价格"),
        ],
    },
    {
        "id": "realestate", "name": "地产链",
        # 统计局月度累计值经 monthly_from_cumulative 差分回当月值展示（每年首点为
        # 1-2 月合并发布值）；70 城同比与 30 城成交为市场高频侧确认。
        # 码源：search_economic_indicator 检索。
        "indicators": [
            ("S0029658", "商品房销售面积当月", "销售"),
            ("S0029659", "商品房销售额当月", "销售"),
            ("S0029656", "开发投资完成额当月", "投资"),
            ("S0029669", "房屋新开工面积当月", "施工"),
            ("S0029670", "房屋竣工面积当月", "施工"),
            ("S2707411", "70城新房价格同比", "价格"),
            ("S2707380", "30城日均成交(月均)", "销售"),
        ],
    },
    {
        "id": "coal", "name": "煤炭",
        # 产/需（统计局原煤、海关进口、统计局焦炭）+ 价格（秦皇岛动力煤周频、焦链日频折周）。
        "indicators": [
            ("S0026989", "原煤产量", "产销"),
            ("S0027001", "煤炭进口量", "产销"),
            ("S0026997", "焦炭产量", "产销"),
            ("S5104572", "秦皇岛动力煤Q5500", "价格"),
            ("S5132102", "炼焦煤均价", "价格"),
            ("S5132320", "冶金焦平仓价", "价格"),
        ],
    },
    {
        "id": "steel", "name": "钢铁",
        # 统计局当月产量 + 螺纹钢现货（日频折周）+ 进口矿月度均价 + 钢材社会库存（周频）。
        "indicators": [
            ("S0027374", "粗钢产量", "产量"),
            ("S0027370", "生铁产量", "产量"),
            ("S0027378", "钢材产量", "产量"),
            ("S5707798", "螺纹钢价格", "价格"),
            ("S5704501", "铁矿石进口均价", "价格"),
            ("L3818799", "钢材社会库存", "库存"),
        ],
    },
]


def call_wind(codes, begin, end):
    """一次 economic_data 调用，逗号批量传代码；返回 code->metric dict。"""
    params = {"question": ",".join(codes), "beginDate": begin, "endDate": end}
    suffix = "edb-%d" % int(dt.datetime.now().timestamp())
    pf = os.path.join("scripts", "request-%s.json" % suffix)
    pfull = os.path.join(SKILL_DIR, pf.replace("/", os.sep))
    with io.open(pfull, "w", encoding="utf-8") as f:
        f.write(json.dumps(params, ensure_ascii=False))
    try:
        r = subprocess.run(
            ["node", CLI, "call", "economic_data",
             "query_economic_indicator_data", "@" + pf],
            cwd=SKILL_DIR, capture_output=True, text=True,
            encoding="utf-8", timeout=180,
        )
    finally:
        try:
            os.remove(pfull)
        except OSError:
            pass
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError("wind empty stdout; stderr=%s" % (r.stderr or "")[:300])
    env = json.loads(out)
    if not env.get("content"):
        raise RuntimeError("wind envelope: %s" % json.dumps(env, ensure_ascii=False)[:300])
    payload = json.loads(env["content"][0]["text"])
    result = {}
    for mt in payload.get("metrics", []):
        meta = mt.get("meta", {})
        code = meta.get("code") or mt.get("code")
        result[code] = {
            "meta": meta,
            "date": mt.get("date", []),
            "value": mt.get("value", []),
        }
    return result


def parse_date(s):
    s = str(s).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def to_num(v):
    try:
        f = float(v)
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def collapse_weekly(dates, values):
    """日频->每周最后一点；周/月频（每周<=1点）原样返回。"""
    pts = []
    for ds, vs in zip(dates, values):
        d = parse_date(ds)
        n = to_num(vs)
        if d is None or n is None:
            continue
        pts.append((d, n))
    if len(pts) < 2:
        return [[d.strftime("%Y-%m-%d"), v] for d, v in pts]
    # 判断原生密度：跨度天数 / 点数 < 5 视为日频，需折叠
    span = (pts[-1][0] - pts[0][0]).days + 1
    if span / max(len(pts), 1) >= 5:
        return [[d.strftime("%Y-%m-%d"), v] for d, v in pts]
    weekly = {}
    for d, v in pts:
        iso = d.isocalendar()
        weekly[(iso[0], iso[1])] = (d, v)  # 同周后者覆盖前者（周内最后交易日）
    return [[d.strftime("%Y-%m-%d"), v] for (_y, _w), (d, v) in sorted(weekly.items())]


def month_mean(dates, values):
    """日频波动大的成交类序列 -> 每月均值（抹平周内噪声，对齐月度展示）。"""
    buckets = {}
    for ds, vs in zip(dates, values):
        d = parse_date(ds)
        n = to_num(vs)
        if d is None or n is None:
            continue
        buckets.setdefault((d.year, d.month), []).append((d, n))
    out = []
    for (_y, _m), items in sorted(buckets.items()):
        last_d = items[-1][0]
        avg = sum(v for _d, v in items) / len(items)
        out.append([last_d.strftime("%Y-%m-%d"), round(avg, 2)])
    return out


# 成交面积等日频量能指标单日值周内噪声大，不走折周取末点，改按月均聚合
MONTH_MEAN_CODES = {"S2707380"}

# 统计局累计值口径序列：抓取后差分回当月值展示（分析看单月动能，累计锯齿不便读）
CUM_TO_MONTHLY_CODES = {"S0029658", "S0029659", "S0029656", "S0029669", "S0029670"}

# 需要“窗口外前值”才能算对的口径：累计差分要上月累计值，月均要整月日点。
# 只对含这些指标的分类多拉一段前量（目前只有 realestate），其余分类不多花积分。
DERIVED_NEED_PREV = CUM_TO_MONTHLY_CODES | MONTH_MEAN_CODES


def monthly_from_cumulative(points):
    """统计局累计值序列 -> 当月值差分（仅同年且上月有点才差分，否则 `m = v` 原样输出）。

    走 `m = v` 的两种含义要分清：
      - 序列首点：统计局 1-2 月合并发布，原样保留 = 前两月合计，这是设计如此；
      - 窗口首点 / 缺上月点：输出的其实是“1~本月累计”却被当成当月值，量级差 8~9 倍，
        是要防的坑——main() 因此对含本口径的分类往前多拉 buffer 天算前值，算完再裁。
    """
    out = []
    prev = None  # (date, 累计值)
    for ds, v in points:
        d = parse_date(ds)
        if d is None or v is None:
            continue
        if prev and prev[0].year == d.year and d.month - prev[0].month == 1:
            m = v - prev[1]
        else:
            m = v
        out.append([d.strftime("%Y-%m-%d"), round(m, 2)])
        prev = (d, v)
    return out


def _point_keys(cats):
    """(指标 code, 日期) 全集。判定“本轮是否会把历史洗掉”必须用它而不是原始点数：
    import_edb 的撤点删除就是拿 edb.json 的点集与库里做差集，差多少删多少；
    而旧文件里同一日期若存过重点，并集合并会让原始点数假性变少。
    """
    return {(i.get("code"), p[0]) for c in cats or [] for i in c.get("indicators") or []
            for p in (i.get("points") or []) if p and p[0]}


def load_existing(path=None):
    """读现存 edb.json → (数据或 None, 能否安全对待)。

    区分“文件不存在”（(None, True)：首轮抓取，直接写）与“文件在但读不动”
    （(None, False)：必须拒写。把坏档当成空档就是拿一份增量数据洗掉全部历史）。
    增量窗口与 --dry-run 计划都靠这个返回值。
    """
    p = os.path.abspath(path or OUT)
    if not os.path.exists(p):
        return None, True
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f), True
    except Exception as e:
        print("[warn] 旧 edb.json 读不动（%r）：本轮不写盘，先人工看一下这个文件" % e)
        return None, False


def last_point_dates(existing):
    """code -> 旧文件里该指标的最新点位日期（增量窗口起点依据）。"""
    last = {}
    for c in (existing or {}).get("categories") or []:
        for i in c.get("indicators") or []:
            ds = [p[0] for p in i.get("points") or [] if p and p[0]]
            code = i.get("code")
            if code and ds:
                hi = max(ds)
                if code not in last or hi > last[code]:
                    last[code] = hi
    return last


def cat_begin(codes, last, default_begin, end, max_span_days):
    """增量窗口起点：已有数据的指标里最“落后”的那个点，封顶 max_span_days 天。

    取 min 而不是 max 是为了不漏掉断更中的序列（它落后才需要多回溯一段）；
    封顶是为了让无人值守的周任务不可能因为某条序列断更半年就去拉一大份历史——
    真要点历史是人工决定，用 --begin 显式指定（本函数直接让位给显式窗口）。
    """
    known = [last[c] for c in codes if c in last]
    if not known:
        return default_begin
    floor = (dt.date.fromisoformat(end) - dt.timedelta(days=max_span_days)).isoformat()
    return max(min(known), floor, default_begin)


def real_range(indicators):
    """分类 range 按真实点位回写，不能写本轮抓取窗口（会把“这份数据回溯到哪年”抹掉）。"""
    ds = [p[0] for i in indicators or [] for p in (i.get("points") or []) if p and p[0]]
    return {"begin": min(ds), "end": max(ds)} if ds else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="仅抓取指定分类 id（auto/alu/shipping）")
    ap.add_argument("--begin", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--buffer-days", type=int, default=45,
                    help="含差分/月均口径的分类往前多拉的天数（只用于算首点，不写进产物）")
    ap.add_argument("--max-span-days", type=int, default=120,
                    help="增量模式下单次往前多拉的天数上限（人工回填历史请用 --begin 越过此限）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本轮抓取计划（几次调用/几个指标/多少天窗口），不调 Wind")
    args = ap.parse_args()

    end = args.end or dt.date.today().strftime("%Y-%m-%d")
    begin = args.begin or (dt.date.today() - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    # 旧文件只读一次：它既是“不能洗掉的历史”（写盘前防线），也是增量窗口的起点依据
    old_file, old_ok = load_existing()
    last_pts = last_point_dates(old_file)

    cats_out = []
    total_pts = 0
    plan = []
    for cat in CATEGORIES:
        if args.only and cat["id"] != args.only:
            continue
        codes = [c for c, _n, _g in cat["indicators"]]
        # 窗口起点：显式 --begin 尊人工；否则增量（只补旧文件落后的那段）
        begin_cat = begin if args.begin else cat_begin(
            codes, last_pts, begin, end, args.max_span_days)
        # 窗口首点没有前值可差分，monthly_from_cumulative 会走 `m = v` 把“1~本月累计”
        # 当成当月值输出（2026-09-03 实测：地产链 5 条序列在 2025-09-30 = 窗口内首个
        # 月尾，值是同序列中位数的 8.8~9.3 倍，已经跟着回灌进了 edb_value），而窗口只会
        # 往后滑、再也不会回头修正这个点。所以含累计/月均口径的分类多拉 buffer 天做前值，
        # 算完再按 begin_cat 裁掉（不多写点，只多读一点数据）。
        need_prev = any(c in DERIVED_NEED_PREV for c in codes)
        q_begin = ((dt.date.fromisoformat(begin_cat) - dt.timedelta(days=args.buffer_days))
                   .isoformat() if need_prev else begin_cat)
        span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(q_begin)).days + 1
        plan.append((cat["id"], len(codes), q_begin, end, span))
        if args.dry_run:
            continue
        print("[fetch] %s: %d codes %s~%s（%d 天）%s" % (
            cat["id"], len(codes), q_begin, end, span,
            "" if not need_prev else "，含前量缓冲 %d 天" % args.buffer_days))
        data = call_wind(codes, q_begin, end)
        inds = []
        for code, disp, group in cat["indicators"]:
            mt = data.get(code)
            if not mt or not mt["date"]:
                print("  [warn] %s(%s) 无数据返回，跳过" % (disp, code))
                continue
            if code in MONTH_MEAN_CODES:
                pts = month_mean(mt["date"], mt["value"])
            else:
                pts = collapse_weekly(mt["date"], mt["value"])
            if code in CUM_TO_MONTHLY_CODES:
                pts = monthly_from_cumulative(pts)
            if need_prev:
                pts = [p for p in pts if p[0] >= begin_cat]  # 缓冲段只用于算前值，不入库
            if not pts:
                continue
            meta = mt["meta"]
            inds.append({
                "code": code,
                "name": meta.get("name") or disp,
                "label": disp,
                "unit": meta.get("unit", ""),
                "freq": meta.get("freq", ""),
                "source": meta.get("source", ""),
                "group": group,
                "points": pts,
            })
            total_pts += len(pts)
            print("  [ok] %-8s %-22s freq=%s n=%d" % (
                code, disp, meta.get("freq", "?"), len(pts)))
        if inds:
            cats_out.append({"id": cat["id"], "name": cat["name"],
                             "range": real_range(inds) or {"begin": begin_cat, "end": end},
                             "indicators": inds})

    if args.dry_run:
        # 不花积分的成本账：Wind 侧不回报余额，能控制的只有“调用次数 × 时间窗口”，
        # 这两个量就是本输出（一行一分类，一次调用）
        print("[dry-run] 计划 %d 次 wind 调用 / %d 个指标 / 共 %d 天数据区间：" % (
            len(plan), sum(p[1] for p in plan), sum(p[4] for p in plan)))
        for cid, nc, b, e, sp in plan:
            print("  %-12s %2d 指标  %s~%s  %3d 天" % (cid, nc, b, e, sp))
        print("[dry-run] 未调 Wind；旧文件里 %d 个指标、%d 个点不受影响" % (
            len(last_pts), len(_point_keys((old_file or {}).get("categories")))))
        return 0

    out = {
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "range": {"begin": begin, "end": end},
        "categories": cats_out,
    }

    # [collector 改造] 点级合并写盘:与现存 edb.json 按日期并集合并——窗口外历史点不丢;
    # 同日期值以本次抓取为准;本次失败(空返回)的指标/分类保留旧数据。
    # (取代原 --only 分类级合并,两种模式统一走此路径)
    #
    # 2026-09-03 加了两条硬防线，因为 Wind 余额只剩约 1000 分而 edb_value 里 2116 个点
    # 删了就基本补不回来（Wind 侧不回报余额、单次调用到底扣多少本地测不出来，能控制的
    # 只有调用次数与时间窗口，所以配了增量窗口 + --dry-run）：
    #   1) 合并异常不再“退回整份覆盖”——增量窗口只有一二十天，一旦覆盖写就是拿这点
    #      数据抹掉几年的历史，下一轮回灌再按差集把库里窗口外的点一起删了；现在改成直接
    #      退出、不碰旧文件（宁可不更新，也不能把历史洗了）。
    #   2) 合并“成功”但点集变小同样不写：能兜住将来改坏合并逻辑、或旧文件被人手工删过。
    if not old_ok:
        return 4
    if old_file is not None:
        try:
            existing = old_file
            old_keys = _point_keys(existing.get("categories"))
            new_by_cat = {c["id"]: {i["code"]: i for i in c["indicators"]}
                          for c in out["categories"]}
            done, final_cats = set(), []
            for c in existing.get("categories", []):
                done.add(c["id"])
                newinds = new_by_cat.get(c["id"])
                if not newinds:
                    final_cats.append(c)  # 本次未抓/整类空返回,原样保留
                    continue
                oldmap = {i["code"]: i for i in c.get("indicators") or []}
                inds = []
                for code, ni in newinds.items():
                    oi = oldmap.get(code)
                    if oi:
                        ni = dict(ni)
                        m = {p[0]: p for p in (oi.get("points") or [])}
                        m.update({p[0]: p for p in (ni.get("points") or [])})
                        ni["points"] = [m[k] for k in sorted(m)]
                    inds.append(ni)
                for code, oi in oldmap.items():  # 本次无数据的指标保留旧序列
                    if code not in newinds:
                        inds.append(oi)
                nc = dict(c)
                nc["indicators"] = inds
                final_cats.append(nc)
            for c in out["categories"]:  # 旧文件没有的新分类追加
                if c["id"] not in done:
                    final_cats.append(c)
            out["categories"] = final_cats
            # 合并后区间按真实点位回写。原来这里写的是本次抓取窗口，
            # 会把“这份数据其实回溯到哪一年”抹掉——下游想拿 range 做删除防线就会被误导。
            for c in final_cats:
                rr = real_range(c.get("indicators"))
                if rr:
                    c["range"] = rr
            lost = old_keys - _point_keys(final_cats)
            if lost:
                codes = sorted({k[0] for k in lost})
                raise RuntimeError(
                    "合并后丢了 %d 个点（指标 %s）" % (len(lost), ",".join(codes[:8])))
            print("[merge] 点级合并旧 edb.json: 分类=%d 点数=%d（旧 %d，无回撤）" % (
                len(final_cats), sum(len(i.get("points") or []) for c in final_cats
                                     for i in c.get("indicators") or []),
                sum(len(i.get("points") or []) for c in existing.get("categories") or []
                    for i in c.get("indicators") or [])))
        except Exception as e:
            # 不写盘直接退出：edb.json 保持上一轮的原样，run.py 会把 edb 记成 failed
            print("[merge] 与旧 edb.json 合并按失败处理，本轮不写盘（历史点数只进不删）：%r" % e)
            return 4

    ofull = os.path.abspath(OUT)
    os.makedirs(os.path.dirname(ofull), exist_ok=True)
    with io.open(ofull, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    # 统计口径用合并后的 out["categories"]：写 cats_out（本次抓取窗口内的）会让人以为
    # 文件里只剩这么多点；本轮新抓的点数单独标在窗口后面
    print("[done] 写入 %s  分类=%d 序列=%d 文件总点数=%d（本轮 %s~%s 抓到 %d 点）" % (
        ofull, len(out["categories"]),
        sum(len(c["indicators"]) for c in out["categories"]),
        sum(len(i.get("points") or []) for c in out["categories"]
            for i in c["indicators"]), begin, end, total_pts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
