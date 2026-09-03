# -*- coding: utf-8 -*-
"""回归:fetch_edb 写盘前的三道历史防线（不碰 Wind、不碰数据库）。

为什么必须离线：Wind 积分只剩约 1000，真跑一轮 fetch_edb 就要烧掉几百，
而这里要验的三条路径全是"写盘前后的文件形状"，跟数据来自哪里无关。改
fetch_edb.py 的合并/窗口逻辑后，先跑这个再谈真实抓取。

  A 正常合并：窗口前量只拿来算前值、不入库；累计口径在窗口首点也能差分（不再
    把"1~本月累计"当单月值）；range 按真实点位回写，历史点一个不丢。
  B 旧文件读不出来：合并异常 → 退出码 4，旧文件字节级不变（绝不"退回整份覆盖"）。
  C Wind 整类空返回：旧序列原样保留。
  D 增量窗口 cat_begin 的取法与封顶 + --dry-run 一次 Wind 也不调。

用法(零积分): python -X utf8 scripts/verify_edb_merge.py
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "collector", "agro-price", "scripts", "fetch_edb.py")
spec = importlib.util.spec_from_file_location("fe_mod", os.path.abspath(SRC))
fe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fe)

CAT = {"id": "realestate", "name": "地产链",
       "indicators": [("S0029658", "商品房销售面积当月", "销售")]}
META = {"name": "商品房销售面积当月", "unit": "万平方米", "freq": "月", "source": "国家统计局"}
# 统计局公布的"1~本月累计"。8 月 54204.99 / 9 月 65834.79 ⇒ 9 月单月应为 11629.8
CUM = {"20250831": 54204.99, "20250930": 65834.79, "20251031": 73919.31,
       "20251130": 82520.32, "20251231": 96865.47}
# 旧 edb.json 里已有的历史点（窗口外，已差分好的单月值）——这些是"删了就补不回来"的东西
OLD_POINTS = [["2024-11-30", 7100.0], ["2024-12-31", 15900.0], ["2025-06-30", 9200.0],
              ["2025-07-31", 8700.0]]

CALLS = []


def make_wind(mapping):
    def _wind(codes, begin, end):
        CALLS.append((tuple(codes), begin, end))
        return {c: {"meta": META, "date": mapping[c]["date"], "value": mapping[c]["value"]}
                for c in codes if c in mapping}
    return _wind


def write_old(path, cats):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": "2026-09-02T21:49:44+08:00",
                   "range": {"begin": "2025-09-02", "end": "2026-09-02"},
                   "categories": cats}, f, ensure_ascii=False)


def old_cats(points=OLD_POINTS):
    return [{"id": "realestate", "name": "地产链",
             "range": {"begin": "2024-11-30", "end": "2026-08-31"},
             "indicators": [{"code": "S0029658", "name": "商品房销售面积当月",
                             "unit": "万平方米", "freq": "月", "group": "销售",
                             "points": points}]}]


def run_main(argv):
    sys.argv = ["fetch_edb.py"] + argv
    return fe.main()


def check(label, cond, detail=""):
    print("  [%s] %s%s" % ("ok" if cond else "FAIL", label, ("  <- " + detail) if detail and not cond else ""))
    return bool(cond)


def main():
    fe.CATEGORIES = [CAT]
    tmp = tempfile.mkdtemp(prefix="edbmerge_")
    out_path = os.path.join(tmp, "edb.json")
    fe.OUT = out_path
    fails = 0

    # ---- A 正常合并 ----------------------------------------------------------
    print("A 正常合并 + 前量缓冲")
    write_old(out_path, old_cats())
    CALLS.clear()
    fe.call_wind = make_wind({"S0029658": {
        "date": list(CUM.keys()), "value": [CUM[k] for k in CUM]}})
    rc = run_main(["--begin", "2025-09-30", "--end", "2025-12-31"])
    data = json.load(io.open(out_path, encoding="utf-8"))
    pts = data["categories"][0]["indicators"][0]["points"]
    got = dict(pts)
    fails += not check("退出码 0", rc in (None, 0), repr(rc))
    fails += not check("抓取起点带 45 天前量（q_begin=2025-08-16）",
                       CALLS and CALLS[0][1] == "2025-08-16", str(CALLS))
    fails += not check("缓冲段不入库（无 08-31 点）", "2025-08-31" not in got, str(sorted(got)))
    fails += not check("窗口首点差分正确：09-30=11629.8 而非累计 65834.79",
                       abs(got.get("2025-09-30", -1) - 11629.8) < 0.01, repr(got.get("2025-09-30")))
    fails += not check("12-31 = 9月后各月差分之和递推（96865.47-82520.32）",
                       abs(got.get("2025-12-31", -1) - 14345.15) < 0.01, repr(got.get("2025-12-31")))
    fails += not check("窗口外历史 4 点一个不丢",
                       all(d in got for d, _v in OLD_POINTS), str(sorted(got)))
    rng = data["categories"][0]["range"]
    fails += not check("分类 range 按真实点位（2024-11-30 起）",
                       rng["begin"] == "2024-11-30" and rng["end"] == "2025-12-31", json.dumps(rng))
    fails += not check("点数只进不删（8 点）", len(pts) == 8, str(len(pts)))

    # ---- B 合并异常 ----------------------------------------------------------
    print("B 旧文件损坏 → 不写盘")
    write_old(out_path, old_cats())
    bad = io.open(out_path, encoding="utf-8").read()[:-5]  # 截断成非法 JSON
    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write(bad)
    snapshot = io.open(out_path, "rb").read()
    fe.call_wind = make_wind({"S0029658": {
        "date": list(CUM.keys()), "value": [CUM[k] for k in CUM]}})
    rc = run_main(["--begin", "2025-09-30", "--end", "2025-12-31"])
    fails += not check("退出码 4", rc == 4, repr(rc))
    fails += not check("旧文件字节不变", io.open(out_path, "rb").read() == snapshot)

    # ---- B2 防线②的算术（点集回撤哨兵）------------------------------------
    # 注意：这一条**不可能**通过当前代码的正常调用触发——上面 A、C 两轮就是证据：
    # 只要旧文件读得出来，写盘点集必然是旧点集的超集。所以它是一道纯回归哨兵，
    # 兜的是"将来有人把合并逻辑改成覆盖写"。这里只对它的判据做算术验证，不去伪造触发。
    print("B2 点集回撤哨兵判据")
    _pk = fe._point_keys
    good = _pk(old_cats())
    trimmed = _pk(old_cats(points=OLD_POINTS[1:]))  # 假装有人删了 2024-11-30
    dropped = good - trimmed
    fails += not check("能算出被删的那个点", dropped == {("S0029658", "2024-11-30")}, str(dropped))
    fails += not check("并集方向不误报（新⊇旧时差集为空）", (trimmed - good) == set())

    # ---- C 整类空返回 -------------------------------------------------------
    print("C Wind 空返回 → 旧序列原样保留")
    write_old(out_path, old_cats())
    fe.call_wind = make_wind({})
    rc = run_main(["--begin", "2025-09-30", "--end", "2025-12-31"])
    data = json.load(io.open(out_path, encoding="utf-8"))
    pts = data["categories"][0]["indicators"][0]["points"]
    fails += not check("退出码 0（本轮没新数据不算失败）", rc in (None, 0), repr(rc))
    fails += not check("旧点 4 个全在", len(pts) == 4 and dict(pts)["2024-12-31"] == 15900.0, str(pts))

    # ---- D 增量窗口（2026-09-03 为 Wind 余额加的那半）-------------------
    print("D cat_begin 增量窗口 / --dry-run 不碰 Wind")
    hist = {"A": "2026-08-28", "B": "2026-07-31"}
    d0, e0 = "2025-09-03", "2026-09-03"
    fails += not check("有历史→取最落后的那个点",
                       fe.cat_begin(["A", "B"], hist, d0, e0, 120) == "2026-07-31",
                       fe.cat_begin(["A", "B"], hist, d0, e0, 120))
    fails += not check("无历史→回落默认窗口",
                       fe.cat_begin(["Z"], hist, d0, e0, 120) == d0)
    fails += not check("封顶 max_span_days（断更半年也只回溯 20 天）",
                       fe.cat_begin(["A"], {"A": "2026-01-01"}, d0, e0, 20) == "2026-08-14")
    fails += not check("封顶超默认窗口时仍以默认 365 天为限",
                       fe.cat_begin(["A"], {"A": "2020-01-01"}, d0, e0, 9999) == d0)

    calls = []
    fe.call_wind = lambda *a, **k: (calls.append(a), {})[1]
    before = io.open(out_path, encoding="utf-8").read()
    rc = run_main(["--dry-run", "--end", e0])
    fails += not check("--dry-run 一次 Wind 也不调", not calls, str(calls))
    fails += not check("--dry-run 不写盘", rc == 0 and
                       io.open(out_path, encoding="utf-8").read() == before, repr(rc))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s  失败项 %d" % ("全部通过" if not fails else "有失败", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
