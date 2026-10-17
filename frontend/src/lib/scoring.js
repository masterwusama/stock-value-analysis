/* ---------------- 评分模型（纯函数，零 DOM） ----------------
 * 从 stockLegacy.js 拆出（2026-09-25，行为零变化——搬运未改一行逻辑）：
 * 四派评分、造假/管理/周期/陷阱/成长/价值/推荐各轴、价格参考、净现金代入式文本、
 * ST 预警，以及它们共用的取数与格式化助手。这里是 JS 权威实现，与
 * backend/collector/scripts/scoring.py 做全量 parity（_score_check.py），
 * 改任何公式两侧同步、跑完比对再上线。
 *
 * 纪律：本文件禁止出现 document/window/getElementById——那是渲染层
 * （stockLegacy.js）的事；列表悬浮代入式只加载本文件（~60KB gz 而非全量 130KB）。
 */
  // 10年期国债收益率参考值（用于股债利差对比，需手动定期更新）
  var BOND_10Y = 0.017;

  /* ---------------- 工具函数 ---------------- */

  // 与 collector/scripts/equity.py 一致：有限数值，归母优先、总权益兜底，保留零和负值。

  // 与 collector/scripts/equity.py 一致：有限数值，归母优先、总权益兜底，保留零和负值。
  var PARENT_EQUITY_KEYS = ['归属于母公司股东权益合计', '归属于母公司股东的权益',
                          '归属于母公司股东权益', '归属于母公司所有者权益合计'];

  var TOTAL_EQUITY_KEYS = ['所有者权益(或股东权益)合计', '总权益', '股东权益合计'];

  function equityOf(row) {
    if (!row) return null;
    var keys = PARENT_EQUITY_KEYS.concat(TOTAL_EQUITY_KEYS);
    for (var i = 0; i < keys.length; i++) {
      var v = row[keys[i]];
      if (typeof v === 'number' && isFinite(v)) return v;
    }
    return null;
  }

  // 金额（元）→ "xx.x亿" / "xx万" / 原值

  // 金额（元）→ "xx.x亿" / "xx万" / 原值
  function fmtMoney(v) {
    if (v == null || isNaN(v)) return '-';
    var abs = Math.abs(v);
    if (abs >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (abs >= 1e4) return (v / 1e4).toFixed(2) + '万';
    return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  // 金额单位随市场：行情快照（现价/市值）是本币（A 人民币 / 港 HKD / 美 USD），
  // 而财报金额是原报表币种——美股美元，港股大多以人民币披露（如腾讯 2017 营收 2377.6 亿元），
  // 所以只有美股要把「亿元」改成「亿美元」、「元」改成「美元」，港股维持人民币口径不动。
  // 图表/对比表拿不到 d，统一从 renderDetail 开头挂上的 state.current 反查市场

  // 小数 → "12.34"
  function fmtNum(v) {
    if (v == null || isNaN(v)) return '-';
    return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  function fmtDate(s) {
    return s ? String(s).slice(0, 10) : '-';
  }

  // 最近一年分红记录（按派息日/公告日 ≥ 一年前，数据已按日期倒序）
  function recentDividends(d) {
    var cutoff = new Date();
    cutoff.setFullYear(cutoff.getFullYear() - 1);
    var cutoffStr = cutoff.toISOString().slice(0, 10);
    return (d.dividends || []).filter(function (r) {
      return (r.pay_date || r.announce_date || '') >= cutoffStr;
    });
  }

  /* ---------------- 价值分析：数据准备与工具函数 ---------------- */

  // 报表排序统一走三路比较。原来各处写成 `a[k] < b[k] ? -1 : 1`，相等时 cmp(a,b) 与 cmp(b,a)
  // 都是 1，违反反对称性 —— V8 的 TimSort 遇到这种比较器不保证保留输入序，而 scoring.py 用
  // Python 稳定排序 sorted() 一定保留。实测有 8 家公司存在重复报告日，两边取到的“最新一期”
  // 报表因此可能不是同一行，分数就对不上。相等必须显式返回 0。

  // 报表排序统一走三路比较。原来各处写成 `a[k] < b[k] ? -1 : 1`，相等时 cmp(a,b) 与 cmp(b,a)
  // 都是 1，违反反对称性 —— V8 的 TimSort 遇到这种比较器不保证保留输入序，而 scoring.py 用
  // Python 稳定排序 sorted() 一定保留。实测有 8 家公司存在重复报告日，两边取到的“最新一期”
  // 报表因此可能不是同一行，分数就对不上。相等必须显式返回 0。
  function cmpKey(ka, kb) { return ka < kb ? -1 : (ka > kb ? 1 : 0); }

  // indicators 中的年报序列（按报告期升序）

  // indicators 中的年报序列（按报告期升序）
  function annualRows(indicators) {
    return (indicators || [])
      .filter(function (r) { return String(r['报告期'] || '').indexOf('12-31') >= 0; })
      .sort(function (a, b) { return cmpKey(a['报告期'], b['报告期']); });
  }

  // 美股真实财年末：数据侧为对齐 A 股 schema 把报告期压成 12-31（见
  // collector/scripts/fetch_data.py 的 fiscal_year_end，年报选行全靠这个日期），
  // 原值随 extras 存在 财年截止 字段里。未重抓的老数据没这个字段，返回 null 即可。

  // 美股真实财年末：数据侧为对齐 A 股 schema 把报告期压成 12-31（见
  // collector/scripts/fetch_data.py 的 fiscal_year_end，年报选行全靠这个日期），
  // 原值随 extras 存在 财年截止 字段里。未重抓的老数据没这个字段，返回 null 即可。
  function fiscalEndOf(rows) {
    var best = null;
    (rows || []).forEach(function (r) {
      var f = String(r['财年截止'] || '');
      if (f.length === 10 && (!best || f > best)) best = f;
    });
    return best;
  }

  // 滚动 TTM 利润（利润表为累计口径）：最新累计 + 上年年报 - 上年同期累计；最新为年报时直接用年报数
  // field 可换成「扣非净利润」，让两个口径在同一 TTM 窗口下可比（经常性收益门槛用）

  // 滚动 TTM 利润（利润表为累计口径）：最新累计 + 上年年报 - 上年同期累计；最新为年报时直接用年报数
  // field 可换成「扣非净利润」，让两个口径在同一 TTM 窗口下可比（经常性收益门槛用）
  function ttmNetProfit(indicators, field) {
    var key = field || '净利润';
    var byDate = {}, latest = null, cur = null;
    (indicators || []).forEach(function (r) {
      var p = String(r['报告期'] || '').slice(0, 10);
      if (p.length === 10) {
        byDate[p] = r[key];
        if (latest == null || p > latest) { latest = p; cur = r[key]; }
      }
    });
    if (!latest) return null;
    var y = Number(latest.slice(0, 4)), m = latest.slice(5, 7);
    if (m === '12') return cur;  // 最新报告期为年报
    var prevAnn = byDate[String(y - 1) + '-12-31'];
    var prevSame = byDate[String(y - 1) + latest.slice(4, 10)];
    if (cur == null || prevAnn == null || prevSame == null) return null;
    return cur + prevAnn - prevSame;
  }

  // 财报股本优先（最新年报实收资本，港股退而取股本），与快照股本偏差 >5% 视为面值异常/口径不同时回退。
  // 仍不行时用归母权益/每股净资产反推（数据源口径、随财报更新）；
  // 港股“股本”常为面值总额（面值 0.1/0.01/0.001 等），再按常见面值反推股数。
  // 快照股本 mcap/price 随实时价抖动（含快照舍入/滞后），财报股本使每股量完全财报驱动

  // 财报股本优先（最新年报实收资本，港股退而取股本），与快照股本偏差 >5% 视为面值异常/口径不同时回退。
  // 仍不行时用归母权益/每股净资产反推（数据源口径、随财报更新）；
  // 港股“股本”常为面值总额（面值 0.1/0.01/0.001 等），再按常见面值反推股数。
  // 快照股本 mcap/price 随实时价抖动（含快照舍入/滞后），财报股本使每股量完全财报驱动
  function shareCount(balance, sharesFallback, bpsField) {
    var rows = (balance || []).filter(function (r) { return String(r['报告日'] || '').indexOf('12-31') >= 0; });
    var row = null, eq = null, capCn = null, capHk = null;
    if (rows.length) {
      rows.sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
      row = rows[rows.length - 1];
      capCn = row['实收资本(或股本)'];
      capHk = row['股本'];
      eq = equityOf(row);
    }
    if (capCn && sharesFallback && capCn / sharesFallback >= 0.95 && capCn / sharesFallback <= 1.05) return capCn;
    if (capHk && sharesFallback && capHk / sharesFallback >= 0.95 && capHk / sharesFallback <= 1.05) return capHk;
    // 权益/每股净资产反推（每股净资产=权益/股数，数据源算好的财报口径）；偏差 25% 内视为同口径
    if (bpsField && eq && sharesFallback) {
      var c2 = eq / bpsField;
      if (c2 / sharesFallback >= 0.75 && c2 / sharesFallback <= 1.25) return c2;
    }
    // 港股“股本”常为面值总额，按常见面值（0.1/0.01/0.001）反推股数；偏差 12% 内视为同口径
    if (capHk && sharesFallback) {
      var muls = [10, 100, 1000];
      for (var i = 0; i < muls.length; i++) {
        var c3 = capHk * muls[i];
        if (c3 / sharesFallback >= 0.88 && c3 / sharesFallback <= 1.12) return c3;
      }
    }
    return sharesFallback;
  }

  // indicators 最新报告期字段值（该期缺失时回退到上一期有值的）

  // indicators 最新报告期字段值（该期缺失时回退到上一期有值的）
  function latestField(indicators, field) {
    var best = null;
    (indicators || []).forEach(function (r) {
      var p = String(r['报告期'] || '').slice(0, 10);
      if (p.length === 10 && (best == null || p > best[0])) {
        var v = r[field];
        if (v != null) best = [p, v];
      }
    });
    return best ? best[1] : null;
  }

  // 基本每股收益字段（累计口径）滚动 TTM：最新累计 + 上年年报 - 上年同期累计；最新为年报时直接用年报值

  // 基本每股收益字段（累计口径）滚动 TTM：最新累计 + 上年年报 - 上年同期累计；最新为年报时直接用年报值
  function epsTtmField(indicators) {
    var byDate = {}, latest = null;
    (indicators || []).forEach(function (r) {
      var p = String(r['报告期'] || '').slice(0, 10);
      if (p.length === 10) byDate[p] = r['基本每股收益'];
    });
    var dates = Object.keys(byDate);
    if (!dates.length) return null;
    dates.sort();
    latest = dates[dates.length - 1];
    var cur = byDate[latest];
    if (cur == null) return null;
    var y = Number(latest.slice(0, 4)), m = latest.slice(5, 7);
    if (m === '12') return cur;
    var prevAnn = byDate[String(y - 1) + '-12-31'];
    var prevSame = byDate[String(y - 1) + latest.slice(4, 10)];
    if (prevAnn == null || prevSame == null) return null;
    return cur + prevAnn - prevSame;
  }

  // indicators 最新报告期是否年报。epsTtmField / ttmNetProfit 在年报期都直接
  // 返回当期值、不做滚动相减，所以这个判断等于「自算值是上一财年数，还是与快照 PE
  // 同为 TTM 口径的真滚动值」（对应 scoring.py _latest_period_is_annual）

  // indicators 最新报告期是否年报。epsTtmField / ttmNetProfit 在年报期都直接
  // 返回当期值、不做滚动相减，所以这个判断等于「自算值是上一财年数，还是与快照 PE
  // 同为 TTM 口径的真滚动值」（对应 scoring.py _latest_period_is_annual）
  function latestPeriodIsAnnual(indicators) {
    var periods = (indicators || [])
      .map(function (r) { return String(r['报告期'] || '').slice(0, 10); })
      .filter(function (p) { return p.length >= 10; });
    if (!periods.length) return false;
    return periods.sort()[periods.length - 1].slice(5, 7) === '12';
  }

  // 从三大报表列表中取指定报告日（YYYY-MM-DD）的行

  // 从三大报表列表中取指定报告日（YYYY-MM-DD）的行
  function sheetRowByDate(list, date) {
    list = list || [];
    for (var i = 0; i < list.length; i++) {
      if (String(list[i]['报告日']).slice(0, 10) === date) return list[i];
    }
    return null;
  }

  // 三大报表年报序列（报告日 12-31，升序）——用于历史趋势对比

  // 三大报表年报序列（报告日 12-31，升序）——用于历史趋势对比
  function annualBalanceRows(rows) {
    return (rows || [])
      .filter(function (r) { return String(r['报告日'] || '').indexOf('12-31') >= 0; })
      .sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
  }

  // 应收账款读取（港股报表科目为“应收帐款”，双科目兼容，优先 A 股口径）

  // 应收账款读取（港股报表科目为“应收帐款”，双科目兼容，优先 A 股口径）
  function arOf(row) {
    if (!row) return null;
    var v = row['应收账款'];
    return v != null ? v : row['应收帐款'];
  }

  // 复合增长率：cur 较 prev 跨越 years 年；任一端 ≤ 0 时比率开小数次方无实数解，返回 null

  // 复合增长率：cur 较 prev 跨越 years 年；任一端 ≤ 0 时比率开小数次方无实数解，返回 null
  function cagr(cur, prev, years) {
    if (cur == null || prev == null || prev <= 0 || cur <= 0 || !years) return null;
    return Math.pow(cur / prev, 1 / years) - 1;
  }

  // 某报告期年份的每股派息合计（元/股，含中期分红）

  // 某报告期年份的每股派息合计（元/股，含中期分红）
  function perShareDiv(dividends, year) {
    var total = 0, hit = false;
    (dividends || []).forEach(function (r) {
      var m = String(r.year || '').match(/^(\d{4})/);
      if (m && Number(m[1]) === year && r.bonus_per_10 != null) {
        total += r.bonus_per_10;
        hit = true;
      }
    });
    return hit ? total / 10 : null;
  }

  // 连续（现金）分红年数（从最新年份倒推）。只数真派过现金的年份（bonus_per_10 > 0）：
  // 送股不是现金回报，送股年不应顶替派现年守住「连续」。anchor（最新年报年，只在分红史
  // 全量可查的 A 股传）给定且最后现金派现年落在 anchor-2 之前 → 按断档清零：原实现从
  // 最后一次派现年倒推，对最近的削减分红天然失明。scoring.py 的 consecutive_div_years 同一条规则。

  // 连续（现金）分红年数（从最新年份倒推）。只数真派过现金的年份（bonus_per_10 > 0）：
  // 送股不是现金回报，送股年不应顶替派现年守住「连续」。anchor（最新年报年，只在分红史
  // 全量可查的 A 股传）给定且最后现金派现年落在 anchor-2 之前 → 按断档清零：原实现从
  // 最后一次派现年倒推，对最近的削减分红天然失明。scoring.py 的 consecutive_div_years 同一条规则。
  function consecutiveDivYears(dividends, anchor) {
    var years = {};
    (dividends || []).forEach(function (r) {
      var m = String(r.year || '').match(/^(\d{4})/);
      if (m && (r.bonus_per_10 || 0) > 0) years[Number(m[1])] = true;
    });
    var ys = Object.keys(years).map(Number).sort(function (a, b) { return b - a; });
    if (!ys.length) return 0;
    if (anchor != null && ys[0] < anchor - 1) return 0;
    var n = 1;
    for (var i = 1; i < ys.length; i++) {
      if (ys[i] === ys[i - 1] - 1) n++; else break;
    }
    return n;
  }

  function sum(list) {
    var s = 0, hit = false;
    list.forEach(function (v) { if (v != null) { s += v; hit = true; } });
    return hit ? s : null;
  }

  /* ---------------- 视图切换 ---------------- */

  // HTML 转义（事件明细表文本字段来自 Wind 原始数据，不可信，一律转义）
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // ⑥⑦ 卡片底部：Wind 事件优化说明（基础财报分 → 优化分 + 触发红旗）；仅当该代码在覆盖层时显示

  // 汇总全部价值分析计算（股息/现金流/杜邦/成长/体检），renderDetail 与各 render 共用
  function valueAnalysis(d) {
    var ind = d.indicators || [];
    var annual = annualRows(ind); // 年报，升序
    var cfList = (d.cashflow || []).slice().sort(function (a, b) {
      return cmpKey(a['报告日'], b['报告日']);
    });
    var baList = (d.balance || []).slice().sort(function (a, b) {
      return cmpKey(a['报告日'], b['报告日']);
    });
    var divs = d.dividends || [];
    var s = d.snapshot || {};
    var price = s.price;
    var last = annual[annual.length - 1];
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var lastYear = lastDate ? Number(lastDate.slice(0, 4)) : null;

    // ---- 股东回报 ----
    var perShare12m = null, hit12 = false;
    recentDividends(d).forEach(function (r) {
      if (r.bonus_per_10 != null) { perShare12m = (perShare12m || 0) + r.bonus_per_10; hit12 = true; }
    });
    perShare12m = hit12 ? perShare12m / 10 : null;
    var divYield = (price && perShare12m != null) ? perShare12m / price : null;
    var perShareY = lastYear != null ? perShareDiv(divs, lastYear) : null;
    var epsY = last ? last['基本每股收益'] : null;
    var payout = (perShareY != null && epsY != null && epsY > 0) ? perShareY / epsY : null;
    // 断档锚定只在 A 股传（分红史全量可查，停派是公开事实）；港美覆盖不全，见函数头
    // 港美股传宽锚（+2＝停派 ≥4 年才清零）：分红源覆盖不全，紧锚会把「没抓到」误判成
    // 「没派过」（scoring.py 同批，2026-09-28；此前港美股完全不锚）
    var divAnchor = lastYear != null ? (String(d.market || 'A') === 'A' ? lastYear : lastYear + 2) : null;
    var divConsecutive = consecutiveDivYears(divs, divAnchor);
    var cumPerShare = null, hitCum = false;
    divs.forEach(function (r) {
      if (r.bonus_per_10 != null) { cumPerShare = (cumPerShare || 0) + r.bonus_per_10; hitCum = true; }
    });
    cumPerShare = hitCum ? cumPerShare / 10 : null;

    // ---- 现金流质量（近 5 年年报）----
    var cfRows = [];
    for (var i = Math.max(0, annual.length - 5); i < annual.length; i++) {
      var date = String(annual[i]['报告期']).slice(0, 10);
      var c = sheetRowByDate(cfList, date);
      var net = annual[i]['净利润'];
      var revenue = annual[i]['营业总收入'];
      var ocf = c ? c['经营活动产生的现金流量净额'] : null;
      var capex = c ? c['购建固定资产、无形资产和其他长期资产所支付的现金'] : null;
      // 数据源偶发脏值（如个别年份数值异常大）：资本开支不可能超过营收 1.5 倍，超限置空
      if (capex != null && (capex < 0 || (revenue != null && capex > revenue * 1.5))) capex = null;
      var receive = c ? c['销售商品、提供劳务收到的现金'] : null;
      cfRows.push({
        year: date.slice(0, 4),
        net: net,
        ocf: ocf,
        ratio: (net != null && ocf != null && net > 0) ? ocf / net : null,
        capex: capex,
        fcf: (ocf != null && capex != null) ? ocf - capex : null,
        receive: receive,
        revenue: revenue,
        collect: (receive != null && revenue != null && revenue > 0) ? receive / revenue : null
      });
    }
    // 只累加同年 net 与 ocf 都有数的配对（与 Python value_analysis 一致）：两列各自求和时
    // 缺失年份不重合，分子分母来自不同年份集合，比值不对应任何一段真实经营期
    var ratioPairs = cfRows.filter(function (r) { return r.net != null && r.ocf != null; });
    var sumNet = sum(ratioPairs.map(function (r) { return r.net; }));
    var sumOcf = sum(ratioPairs.map(function (r) { return r.ocf; }));
    var ratio5 = (sumNet != null && sumNet > 0) ? sumOcf / sumNet : null;
    var ratioYears = ratioPairs.length;
    var fcf5 = sum(cfRows.map(function (r) { return r.fcf; }));
    var collects = cfRows.map(function (r) { return r.collect; }).filter(function (v) { return v != null; });
    var collectAvg = collects.length ? sum(collects) / collects.length : null;

    // ---- 杜邦分析（近 5 年年报）----
    var dupont = [];
    for (var j = Math.max(0, annual.length - 5); j < annual.length; j++) {
      var dDate = String(annual[j]['报告期']).slice(0, 10);
      var b = sheetRowByDate(baList, dDate);
      var bPrev = j > 0 ? sheetRowByDate(baList, String(annual[j - 1]['报告期']).slice(0, 10)) : null;
      var rev = annual[j]['营业总收入'];
      var net2 = annual[j]['净利润'];
      var assets = b ? b['资产总计'] : null;
      var assetsPrev = bPrev ? bPrev['资产总计'] : null;
      var assetsAvg = (assets != null && assetsPrev != null) ? (assets + assetsPrev) / 2 : assets;
      var equity = equityOf(b);
      var margin = (rev != null && net2 != null && rev > 0) ? net2 / rev : null;
      var turnover = (rev != null && assetsAvg != null && assetsAvg > 0) ? rev / assetsAvg : null;
      var leverage = (assets != null && equity != null && equity > 0) ? assets / equity : null;
      dupont.push({
        year: dDate.slice(0, 4),
        margin: margin,
        turnover: turnover,
        leverage: leverage,
        roe: (margin != null && turnover != null && leverage != null) ? margin * turnover * leverage : null,
        roeReported: annual[j]['净资产收益率']
      });
    }

    // ---- 成长性 & PEG ----
    // 基期必须真是 5 年前：早先直接取 annual[0]，窗口等于数据有多深就多深（实测 6911 家里
    // 97.2% 跨度≠5 年，7 年 4051 家、19 年 176 家），却挂着「CAGR(5年)」的标签，还喂给
    // 净利 5 年 CAGR（10% 阈值是按 5 年标定的）。取不晚于 lastYear-5 的最近年报
    // 作基期，历史不足 5 年时退回最早年报（与后端 scoring.py 同口径）。
    var revCagr5 = null, netCagr5 = null, revCagr3 = null, netCagr3 = null, growthNote = '年报数据';
    if (annual.length >= 2 && lastYear != null) {
      var base = null;
      for (var bi = annual.length - 2; bi >= 0; bi--) {
        if (Number(String(annual[bi]['报告期']).slice(0, 4)) <= lastYear - 5) { base = annual[bi]; break; }
      }
      if (base == null) base = annual[0];
      var baseYear = Number(String(base['报告期']).slice(0, 4));
      var span = lastYear - baseYear;
      if (span > 0) {
        revCagr5 = cagr(last['营业总收入'], base['营业总收入'], span);
        netCagr5 = cagr(last['净利润'], base['净利润'], span);
        var spanNote = span === 5 ? '' : (span > 5 ? '（5 年前年报缺档，取更早一期）' : '（年报不足 5 年，按实际跨度折算）');
        growthNote = '最新年报(' + lastYear + ') vs ' + baseYear + '，跨度 ' + span + ' 年' + spanNote;
        var a3 = null;
        for (var k = 0; k < annual.length; k++) {
          if (Number(String(annual[k]['报告期']).slice(0, 4)) === lastYear - 3) { a3 = annual[k]; break; }
        }
        if (a3) {
          revCagr3 = cagr(last['营业总收入'], a3['营业总收入'], 3);
          netCagr3 = cagr(last['净利润'], a3['净利润'], 3);
        }
      }
    }
    var peg = (s.pe_ttm != null && netCagr5 != null && netCagr5 > 0) ? s.pe_ttm / (netCagr5 * 100) : null;
    var pegText = peg == null
      ? (netCagr5 != null && netCagr5 <= 0 ? 'N/A(净利负增长)' : '-')
      : (peg < 1 ? fmtNum(peg) + ' 低估' : peg <= 2 ? fmtNum(peg) + ' 合理' : fmtNum(peg) + ' 偏贵');

    // ---- 价值体检清单 ----
    var roe3 = null;
    var roeVals = dupont.map(function (r) { return r.roeReported; }).filter(function (v) { return v != null; });
    if (roeVals.length) roe3 = sum(roeVals.slice(-3)) / Math.min(3, roeVals.length);
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    var cash = lastBa ? lastBa['货币资金'] : null;
    var debt = lastBa ? [lastBa['短期借款'], lastBa['一年内到期的非流动负债'], lastBa['长期借款'], lastBa['应付债券'], lastBa['租赁负债']] : [];
    var netCash = (cash != null && sum(debt) != null) ? cash - sum(debt) : null;
    var lastLb = last ? last['资产负债率'] : null;
    var lastCr = last ? last['流动比率'] : null;
    var lastMargin = last ? last['销售净利率'] : null;

    function check(label, std, valText, pass) {
      var st = pass === null ? 'na' : (pass ? 'pass' : 'fail');
      return { label: label, std: std, val: valText, pass: st };
    }
    var checks = [
      check('净资产收益率(近3年平均)', '≥ 15%', fmtPct(roe3), roe3 == null ? null : roe3 >= 0.15),
      check('销售净利率(最新年报)', '≥ 10%', fmtPct(lastMargin), lastMargin == null ? null : lastMargin >= 0.10),
      check('资产负债率(最新年报)', '< 60%', fmtPct(lastLb), lastLb == null ? null : lastLb < 0.60),
      check('流动比率(最新年报)', '≥ 1.5', fmtNum(lastCr), lastCr == null ? null : lastCr >= 1.5),
      check('5年累计净现比', '≥ 0.8', fmtNum(ratio5), ratio5 == null ? null : ratio5 >= 0.8),
      check('连续分红年数', '≥ 5 年', (divConsecutive || 0) + ' 年', divConsecutive >= 5),
      check('股息率(近12月)', '≥ 2%', fmtPct(divYield), divYield == null ? null : divYield >= 0.02),
      check('净利5年正增长', 'CAGR > 0', fmtPct(netCagr5), netCagr5 == null ? null : netCagr5 > 0),
      check('净现金(货币资金-有息负债)', '> 0', fmtMoney(netCash), netCash == null ? null : netCash > 0),
      check('5年累计自由现金流', '> 0', fmtMoney(fcf5), fcf5 == null ? null : fcf5 > 0)
    ];
    var passN = checks.filter(function (c) { return c.pass === 'pass'; }).length;
    var failN = checks.filter(function (c) { return c.pass === 'fail'; }).length;
    var checkSummary = '体检结果：通过 ' + passN + ' 项，未通过 ' + failN + ' 项，其余数据缺失。仅作量化参考，不构成投资建议。';

    // 每股派息图数据（近 6 年，含最新年报）
    var divChart = [];
    if (lastYear != null) {
      for (var y = lastYear; y > lastYear - 6 && y > 2000; y--) {
        var v = perShareDiv(divs, y);
        if (v != null) divChart.unshift({ year: y, value: v });
      }
    }

    return {
      divYield: divYield, spread: (divYield != null) ? divYield - BOND_10Y : null,
      payout: payout, divConsecutive: divConsecutive, cumPerShare: cumPerShare,
      cfRows: cfRows, ratio5: ratio5, ratioYears: ratioYears, fcf5: fcf5, collectAvg: collectAvg,
      dupont: dupont, revCagr5: revCagr5, netCagr5: netCagr5,
      revCagr3: revCagr3, netCagr3: netCagr3, pegText: pegText, growthNote: growthNote,
      checks: checks, checkSummary: checkSummary, divChart: divChart,
      // 评分基准报告期（最新年报期）：Python 侧由 compute_scores 写成 scores.reportDate 入库成
      // score_daily.report_date；本函数只供 Node 端 parity 比对取值（见 _score_check_node.js）
      annualDate: lastDate
    };
  }

  // 渲染价值分析各区块（体检表 + 现金流表 + 杜邦表）。两张图（每股派息/净现比）不在
  // 这里画——renderVaCharts 有「state.charts 为空即跳过」守卫，而 state.charts 由
  // renderCharts（①指标趋势，已改视口懒构建）填充；调用点跟着挪进 charts 懒构建器，
  // 否则懒构建前 runs 一遍被守卫吞掉，②的两张图永远出不来。

  // 线性得分：v ≤ a 取 ma；v ≥ b 取 mb；中间线性
  function lerpScore(v, a, b, ma, mb) {
    if (v == null || isNaN(v)) return null;
    if (v <= a) return ma;
    if (v >= b) return mb;
    return ma + (v - a) / (b - a) * (mb - ma);
  }

  // 定义域为正数的“越小越好”项（PE/PB 等，ma > mb）的符号护栏：v ≤ 0 是亏损/资不抵债的
  // “不达标”而非数据缺失，直接给最低分 mb；否则 lerpScore 的 v <= a 分支会把负值夹到满分端

  // 定义域为正数的“越小越好”项（PE/PB 等，ma > mb）的符号护栏：v ≤ 0 是亏损/资不抵债的
  // “不达标”而非数据缺失，直接给最低分 mb；否则 lerpScore 的 v <= a 分支会把负值夹到满分端
  function lerpScoreNonneg(v, a, b, ma, mb) {
    if (v == null || isNaN(v)) return null;
    return v <= 0 ? mb : lerpScore(v, a, b, ma, mb);
  }

  // 同比增长率：基期为负时用 |基期| 作分母，使亏损扩大→负、亏损收窄/扭亏→正；
  // 基期为正时与 cur/pre-1 完全等价，基期为 0 或缺失时无定义返回 null

  // 同比增长率：基期为负时用 |基期| 作分母，使亏损扩大→负、亏损收窄/扭亏→正；
  // 基期为正时与 cur/pre-1 完全等价，基期为 0 或缺失时无定义返回 null
  function yoyOf(cur, prev) {
    if (cur == null || prev == null || prev === 0) return null;
    return (cur - prev) / Math.abs(prev);
  }

  // 周期位置分 8 维权重：cycleAnalysis 阶段二与 cycleHistory 必须同尺度。
  // 合计 105 而非 100 —— 这正是旧代码要靠 Math.min(100, sum) 硬夹的原因，归一化后不再需要

  // 周期位置分 8 维权重：cycleAnalysis 阶段二与 cycleHistory 必须同尺度。
  // 合计 105 而非 100 —— 这正是旧代码要靠 Math.min(100, sum) 硬夹的原因，归一化后不再需要
  var CYCLE_POS_W = [25, 15, 15, 10, 10, 10, 10, 10];

  // scored = [[得分 or null, 该项满分权重]] → 按“可用项的有效满分”归一到 0~100。
  // 不能直接 sum(可用项)：造假分与周期位置分都是越低越好，缺一项就少扣一项，
  // 数据不全的公司反而显得更干净、更接近周期底部（实测 1620 家亏损公司缺造假分里
  // 25 分的净现比项）。归一化让缺项变成中性，而不是占便宜。

  // scored = [[得分 or null, 该项满分权重]] → 按“可用项的有效满分”归一到 0~100。
  // 不能直接 sum(可用项)：造假分与周期位置分都是越低越好，缺一项就少扣一项，
  // 数据不全的公司反而显得更干净、更接近周期底部（实测 1620 家亏损公司缺造假分里
  // 25 分的净现比项）。归一化让缺项变成中性，而不是占便宜。
  function weightedTotal(scored) {
    var wsum = 0, ssum2 = 0, any = false;
    for (var i = 0; i < scored.length; i++) {
      if (scored[i][0] == null) continue;
      any = true;
      ssum2 += scored[i][0];
      wsum += scored[i][1];
    }
    if (!any || wsum <= 0) return null;
    return Math.min(100, ssum2 / wsum * 100);
  }

  // 四派总分：正分项按可用满分归一（每项自带 max），扣分项原样相加，再夹到 ±「可评估权重」。
  // 不能直接 sum(所有项)（null 当 0 分）：缺一项等于白扣该项满分。施洛斯股息率项缺 44.1%、
  // 巴菲特净现比与净利 CAGR 两项各缺 23.8%/32.4%，港股美股字段本就稀疏，实测平均被压
  // 3~6 分而 A 股只 1~2 分，跨市场不可比。
  // 但也不能纯归一：银行/券商/保险缺的恰好是按业务模型本就不适用的偿债项（格防的流动比率
  // 与营运资本共 40 分权重、施洛斯的流动比率与市值/流动资产共 30 分权重），剩余项全满就是
  // 100 分，实测把格防前 15 里的 12 席、施洛斯前 15 里的 10 席换成金融股，真达标的公司被
  // 挤出去。夹到 ±可评估权重后：缺项仍中性，但最多只能拿到「桌上真正摆着的分」——覆盖度
  // 满 100 时与旧口径完全相同，四派前 15 名换手实测均为 0。
  // 下限那一侧同样需要：只评估到 35 分权重的公司若全为负分，纯归一会给 −42.9，跌破格防
  // −30 的设计下限。
  // 扣分项不参与归一：缺数据本就给 0，已中性；若按 |最低分| 也归一，施洛斯分母会变成 137，
  // 一家满分公司只能拿 73 分。
  // 扣分要在夹逼之后加：先加后夹等于「基础分顶到上限的公司扣不动」——施洛斯的 9 个扣分项
  // （合计最深 −37）实测对 55 家满分公司完全不生效，红旗分 60 与 0 的同分。

  // 四派总分：正分项按可用满分归一（每项自带 max），扣分项原样相加，再夹到 ±「可评估权重」。
  // 不能直接 sum(所有项)（null 当 0 分）：缺一项等于白扣该项满分。施洛斯股息率项缺 44.1%、
  // 巴菲特净现比与净利 CAGR 两项各缺 23.8%/32.4%，港股美股字段本就稀疏，实测平均被压
  // 3~6 分而 A 股只 1~2 分，跨市场不可比。
  // 但也不能纯归一：银行/券商/保险缺的恰好是按业务模型本就不适用的偿债项（格防的流动比率
  // 与营运资本共 40 分权重、施洛斯的流动比率与市值/流动资产共 30 分权重），剩余项全满就是
  // 100 分，实测把格防前 15 里的 12 席、施洛斯前 15 里的 10 席换成金融股，真达标的公司被
  // 挤出去。夹到 ±可评估权重后：缺项仍中性，但最多只能拿到「桌上真正摆着的分」——覆盖度
  // 满 100 时与旧口径完全相同，四派前 15 名换手实测均为 0。
  // 下限那一侧同样需要：只评估到 35 分权重的公司若全为负分，纯归一会给 −42.9，跌破格防
  // −30 的设计下限。
  // 扣分项不参与归一：缺数据本就给 0，已中性；若按 |最低分| 也归一，施洛斯分母会变成 137，
  // 一家满分公司只能拿 73 分。
  // 扣分要在夹逼之后加：先加后夹等于「基础分顶到上限的公司扣不动」——施洛斯的 9 个扣分项
  // （合计最深 −37）实测对 55 家满分公司完全不生效，红旗分 60 与 0 的同分。
  function schoolTotal(posItems, penItems) {
    var wsum = 0, acc = 0, full = 0;
    for (var i = 0; i < posItems.length; i++) {
      full += posItems[i].max;
      if (posItems[i].score == null) continue;
      acc += posItems[i].score;
      wsum += posItems[i].max;
    }
    if (wsum <= 0) return null;
    var pen = sum((penItems || []).map(function (x) { return x.score; })) || 0;
    // 覆盖度补偿只给正分：负分除以更小的分母等于让缺项放大坏消息，格防会因此跌破 −30 下限
    var total = acc / (acc > 0 ? wsum : full) * 100;
    return Math.max(-wsum, Math.min(wsum, total)) + pen;
  }

  // row 之前最多 3 个年报的资本开支（按 row 在年报序列中的实际位置开窗）。
  // 不能硬切 annualCf.slice(-4,-1)（默认现金流表末年即当年，报表错位就取错窗口），
  // 也不能裸用 indexOf（row 不在序列中时返回 -1，切片退化成“全部历史除最后一行”）

  // row 之前最多 3 个年报的资本开支（按 row 在年报序列中的实际位置开窗）。
  // 不能硬切 annualCf.slice(-4,-1)（默认现金流表末年即当年，报表错位就取错窗口），
  // 也不能裸用 indexOf（row 不在序列中时返回 -1，切片退化成“全部历史除最后一行”）
  function capexPrevOf(annualCf, row, key) {
    var ci = row == null ? -1 : annualCf.indexOf(row);
    if (ci < 0) return [];
    return annualCf.slice(Math.max(0, ci - 3), ci)
      .map(function (r) { return r[key]; }).filter(function (v) { return v != null; });
  }

  // 总分 → 等级（优秀/良好/一般/较差/数据不足）

  // 总分 → 等级（优秀/良好/一般/较差/数据不足）
  function gradeOf(total) {
    if (total == null) return 'na';
    if (total >= 80) return 'good';
    if (total >= 60) return 'mid';
    if (total >= 40) return 'low';
    return 'bad';
  }

  function gradeText(g) {
    return { good: '优秀', mid: '良好', low: '一般', bad: '较差', na: '数据不足' }[g];
  }

  // 造假风险等级（分数越高越可疑，与价值评分方向相反）：<20 低 / <40 中 / <60 较高 / ≥60 高

  // 造假风险等级（分数越高越可疑，与价值评分方向相反）：<20 低 / <40 中 / <60 较高 / ≥60 高
  function fraudGradeOf(total) {
    if (total == null) return 'na';
    if (total < 20) return 'good';
    if (total < 40) return 'mid';
    if (total < 60) return 'low';
    return 'bad';
  }

  function fraudGradeText(g) {
    return { good: '低', mid: '中', low: '较高', bad: '高', na: '数据不足' }[g];
  }

  // 评分项构造：match = 符合度（score/max，用于百分比与颜色；负分按 0% 显示）

  // 评分项构造：match = 符合度（score/max，用于百分比与颜色；负分按 0% 显示）
  function it(std, val, thr, max, score, inapp) {
    return {
      std: std, val: val, thr: thr, max: max, score: score,
      // inapp：驱动科目在本公司报表里结构性不存在（不适用），与「有这项但没数」（缺失）分开标注
      na: inapp === true && score == null,
      match: score == null ? null : Math.max(0, score) / max
    };
  }

  // 价格标签：买入价≤现价（进入买入区）标绿；卖出价≥现价（进入卖出区）标红

  // ---- 价格参考（买入/保守卖出/公允卖出）常量，评分项与参考价共用，防止两处口径漂移 ----
  // 低于一分钱的参考价一律视为「无」：没有任何市场按这个价位报价，而它当分母会把
  // 「折价率 1-现价/买价」吹到 1e20 量级。EPS 的滚动 TTM 是三项相减，留得住浮点零渣
  // （实测 3 家：比依股份 5.6e-17、安旭生物 5.6e-17、中科通达 1.7e-18）。
  var MIN_PRICE_REF = 0.01;
  // 三档都锚定各流派自己的估值阈值，不做任何反推：
  //   买点   = 账面派（格攻/施洛斯）取资产折价线；收益派（格防/巴菲特）取保守卖价 × 2/3 安全边际
  //   保守卖 = 该派核心估值锚：格攻 每股净流动资产、格防 15×EPS、施洛斯 每股净资产、巴菲特 公允PE×EPS
  //   公允卖 = 保守卖上浮：格攻/施洛斯 1.5 倍（正是价格项归零点）、格防 4/3 倍（PE 20，半分位）、
  //            巴菲特 1.3 倍（无对应评分项）
  // 两条限制：
  //   ① 收益派的 EPS 锚先用四道可信度门槛过滤（符号、隐含 PE、量级背离、经常性口径），过不了
  //      就没有锚，三档一起空；
  //   ② 账面派的三档里只有买点是「无条件目标价」，两档卖价只在该派估值射程（现价 ≤ SELL_BAND×
  //      资产锚）内给出——资产锚是清算底，对 99% 的市场它不构成卖出参考。
  // 曾用「二分反推使总分 ≥ 90 的最高价」，实测 6939 家里 56%~96% 的公司分数上限本就不足
  // 90，tgt = min(90, tMax) 把目标悄悄降级成公司自己的上限，产出的买价与 90 分再无关系；
  // 且买价中位数只有现价的 8%（格攻）~40%（格防），即要跌 60%~92% 才触发，不是参考价。

  // 三档都锚定各流派自己的估值阈值，不做任何反推：
  //   买点   = 账面派（格攻/施洛斯）取资产折价线；收益派（格防/巴菲特）取保守卖价 × 2/3 安全边际
  //   保守卖 = 该派核心估值锚：格攻 每股净流动资产、格防 15×EPS、施洛斯 每股净资产、巴菲特 公允PE×EPS
  //   公允卖 = 保守卖上浮：格攻/施洛斯 1.5 倍（正是价格项归零点）、格防 4/3 倍（PE 20，半分位）、
  //            巴菲特 1.3 倍（无对应评分项）
  // 两条限制：
  //   ① 收益派的 EPS 锚先用四道可信度门槛过滤（符号、隐含 PE、量级背离、经常性口径），过不了
  //      就没有锚，三档一起空；
  //   ② 账面派的三档里只有买点是「无条件目标价」，两档卖价只在该派估值射程（现价 ≤ SELL_BAND×
  //      资产锚）内给出——资产锚是清算底，对 99% 的市场它不构成卖出参考。
  // 曾用「二分反推使总分 ≥ 90 的最高价」，实测 6939 家里 56%~96% 的公司分数上限本就不足
  // 90，tgt = min(90, tMax) 把目标悄悄降级成公司自己的上限，产出的买价与 90 分再无关系；
  // 且买价中位数只有现价的 8%（格攻）~40%（格防），即要跌 60%~92% 才触发，不是参考价。
  var BUY_MARGIN = 2 / 3;      // 收益派（格防/巴菲特）买点相对公允倍数的安全边际


  var G_D_PE_FULL = 15;        // 格防：市盈率 ≤ 15 拿满 5 分，亦是保守卖价倍数
  // 本币 → 人民币的粗略汇率（按市场取）：只服务格防的规模硬门槛（100/50/30 亿当年按 A 股
  // 人民币定的，不折会把体量相当的港美股系统性降 1~2 档）。与 scoring.py 的 FX_TO_CNY 同值。

  // 本币 → 人民币的粗略汇率（按市场取）：只服务格防的规模硬门槛（100/50/30 亿当年按 A 股
  // 人民币定的，不折会把体量相当的港美股系统性降 1~2 档）。与 scoring.py 的 FX_TO_CNY 同值。
  var FX_TO_CNY = { A: 1.0, HK: 0.92, US: 7.15 };

  // 本币金额折成人民币当量（市场未知或未列时按人民币处理，即不折）

  // 本币金额折成人民币当量（市场未知或未列时按人民币处理，即不折）
  function toCny(v, market) {
    if (v == null) return null;
    var k = String(market || 'A');
    return v * (FX_TO_CNY[k] == null ? 1.0 : FX_TO_CNY[k]);
  }

  var S_PB_FULL = 0.75;        // 施洛斯：市净率 ≤ 0.75 拿满 25 分，亦是买点倍数
  // 收益锚可信度：符号相反只是其中一种坏法，量级差一个数量级同样是坏锚（一次性损益、
  // 股本口径错、年报窗口与快照 TTM 错配）。坏锚会占满「买入性价比」榜首：*ST华幸 买价
  // 55.81 对现价 1.08、金科股份 46.4 倍、BKNG 13.9 倍、和黄医药 买价 28.91 对现价 19.14。
  // 隐含 PE 下限取 3 倍：一家持续经营的公司不可能长期只值自己三年利润，市场按这个价位
  // 报价就是在说这笔盈利不可重复。现行流水线实测，下限从 1 倍提到 3 倍多拦 3 家：碧桂园
  // 1.54（债务重组收益）、RIGEL 2.42、天立国际 2.71。3~5 倍带内还有 32 家有锚公司，中国铁建
  // 3.76、中国中铁 3.96、新华保险 4.08 是低 PE 常态化的真深度价值，诺比侃 3.13、天能动力 3.27
  // 则是 EPS 字段与快照 PE 背离 8~9 倍的坏锚——那类归量级门槛（gate ③）管：年报窗口容忍从 10 倍
  // 收到 5 倍能多砍 17 家锚，但会连带 Merck、太古这类年报 EPS 与快照真实差 5~10 倍的转折公司。

  // 收益锚可信度：符号相反只是其中一种坏法，量级差一个数量级同样是坏锚（一次性损益、
  // 股本口径错、年报窗口与快照 TTM 错配）。坏锚会占满「买入性价比」榜首：*ST华幸 买价
  // 55.81 对现价 1.08、金科股份 46.4 倍、BKNG 13.9 倍、和黄医药 买价 28.91 对现价 19.14。
  // 隐含 PE 下限取 3 倍：一家持续经营的公司不可能长期只值自己三年利润，市场按这个价位
  // 报价就是在说这笔盈利不可重复。现行流水线实测，下限从 1 倍提到 3 倍多拦 3 家：碧桂园
  // 1.54（债务重组收益）、RIGEL 2.42、天立国际 2.71。3~5 倍带内还有 32 家有锚公司，中国铁建
  // 3.76、中国中铁 3.96、新华保险 4.08 是低 PE 常态化的真深度价值，诺比侃 3.13、天能动力 3.27
  // 则是 EPS 字段与快照 PE 背离 8~9 倍的坏锚——那类归量级门槛（gate ③）管：年报窗口容忍从 10 倍
  // 收到 5 倍能多砍 17 家锚，但会连带 Merck、太古这类年报 EPS 与快照真实差 5~10 倍的转折公司。
  var EPS_PE_MIN = 3;          // 隐含 PE（现价/EPS）低于 3 倍：这笔盈利不可重复，锚作废

  var EPS_MAG_TTM = 3;         // 自算值与快照同为 TTM 口径时，隐含 PE 允许的最大倍数差

  var EPS_MAG_ANNUAL = 10;     // 最新报告期是年报（自算值为上一财年）时放宽到 10 倍，只砍数量级背离
  // 经常性收益门槛：扣非后不足报告净利润一半，说明报告利润的大头来自非经常项目（处置资产、
  // 政府补助、债务重组收益），按扣非后的规模重定锚；扣非转负则该派没有可资本化的盈利，锚作废。
  // 只压缩不放大：扣非高于报告时维持报告口径，让锚停在偏保守一侧。
  // 现行流水线实测：4655 家有收益锚的公司里 297 家被压缩、3 家归零（安达科技、地纬智能、张裕Ａ）。
  // 两道覆盖边界：① 扣非是 A 股科目，港美股不披露、门槛不介入，那侧全靠隐含 PE 下限；
  // ② 券商保险的公允价值变动损益按准则不计入非经常性损益（中国人寿 TTM 扣非/报告 = 1.001），
  // 投资浮盈撑高 TTM 盈利的场景本门槛结构上抓不到。

  // 经常性收益门槛：扣非后不足报告净利润一半，说明报告利润的大头来自非经常项目（处置资产、
  // 政府补助、债务重组收益），按扣非后的规模重定锚；扣非转负则该派没有可资本化的盈利，锚作废。
  // 只压缩不放大：扣非高于报告时维持报告口径，让锚停在偏保守一侧。
  // 现行流水线实测：4655 家有收益锚的公司里 297 家被压缩、3 家归零（安达科技、地纬智能、张裕Ａ）。
  // 两道覆盖边界：① 扣非是 A 股科目，港美股不披露、门槛不介入，那侧全靠隐含 PE 下限；
  // ② 券商保险的公允价值变动损益按准则不计入非经常性损益（中国人寿 TTM 扣非/报告 = 1.001），
  // 投资浮盈撑高 TTM 盈利的场景本门槛结构上抓不到。
  var RECURRING_MIN_RATIO = 0.5;
  // 资产派（格攻/施洛斯）卖价射程：现价超过 2× 资产锚就不再给卖出参考。资产锚是清算底/
  // 账面底，健康持续经营的公司现价本就常年在其 5~10 倍处，把它当触发线等于对几乎整个市场亮红灯。
  // 实测（6939 家，有锚家数 → 其中现价≥保守卖价的比例）：
  //   格攻   4435 → 245 家，命中率 99.2% → 84.9%
  //   施洛斯 6809 → 2337 家，命中率 90.6% → 72.7%
  // 收益派不套射程（现价高于 15×EPS 或公允 PE 是有意义的信号），命中率仍是 83% 左右——
  // 市场确实长期站在格雷厄姆的 PE 15 线之上，这是口径本身的性质。
  // 取 2 倍是为了让账面派的公允卖价（1.5× 锚，即评分里价格项的归零点）留在射程内。

  // 资产派（格攻/施洛斯）卖价射程：现价超过 2× 资产锚就不再给卖出参考。资产锚是清算底/
  // 账面底，健康持续经营的公司现价本就常年在其 5~10 倍处，把它当触发线等于对几乎整个市场亮红灯。
  // 实测（6939 家，有锚家数 → 其中现价≥保守卖价的比例）：
  //   格攻   4435 → 245 家，命中率 99.2% → 84.9%
  //   施洛斯 6809 → 2337 家，命中率 90.6% → 72.7%
  // 收益派不套射程（现价高于 15×EPS 或公允 PE 是有意义的信号），命中率仍是 83% 左右——
  // 市场确实长期站在格雷厄姆的 PE 15 线之上，这是口径本身的性质。
  // 取 2 倍是为了让账面派的公允卖价（1.5× 锚，即评分里价格项的归零点）留在射程内。
  var SELL_BAND = 2;

  // 三大流派评分汇总（格雷厄姆进取/防御、施洛斯、巴菲特芒格），以最新年报为基础
  // 估值量一律取快照 market_cap/pe_ttm/pb（当期评分口径）。曾有 k（市值缩放）与
  // useFundamental（财报驱动每股量反推）两个参数，只服务已删除的买入价二分反推。

  // 三大流派评分汇总（格雷厄姆进取/防御、施洛斯、巴菲特芒格），以最新年报为基础
  // 估值量一律取快照 market_cap/pe_ttm/pb（当期评分口径）。曾有 k（市值缩放）与
  // useFundamental（财报驱动每股量反推）两个参数，只服务已删除的买入价二分反推。
  function valueScores(d, va) {
    var annual = annualRows(d.indicators || []);
    var last = annual[annual.length - 1];
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var lastYear = lastDate ? Number(lastDate.slice(0, 4)) : null;
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    var baAnnual = annualBalanceRows(d.balance);   // 三大报表年报序列（升序），下方共用
    var inAnnual = annualBalanceRows(d.income);
    var cfAnnual = annualBalanceRows(d.cashflow);
    // 「不适用」与「缺失」是两件事：银行/保险/券商不出分级资产负债表，压根没有「流动资产合计」
    // 这一行，不是这一期恰好没披露。判据只看数据——该科目在 ≥4 期年报里一次都没出现过，才判
    // 结构性不适用；按行业名套会误伤（实测 A 股 121 家「金融」里 22 家确实报这个科目）。
    // 只影响标注，不影响任何一项的得分与总分。
    function structAbsent(rows, key) {
      return rows.length >= 4 && !rows.some(function (r) { return r[key] != null; });
    }
    var noCA = structAbsent(baAnnual, '流动资产合计');
    var noCL = structAbsent(baAnnual, '流动负债合计');
    var noCash = structAbsent(baAnnual, '货币资金');
    var noGM = structAbsent(annual, '销售毛利率');
    var s = d.snapshot || {};
    var mcap = s.market_cap, pe = s.pe_ttm, pb = s.pb;
    // 快照缺 PB（腾讯行情不返回美股 PB）时用财报每股净资产补算：股价÷每股净资产，缺则归母权益/股本反推
    if (pb == null) {
      var bpsFb = latestField(d.indicators, '每股净资产');
      if (bpsFb == null && lastBa != null) {
        var eqFb = equityOf(lastBa);
        var shFb = shareCount(d.balance, (mcap != null && s.price != null && s.price > 0) ? mcap / s.price : null, null);
        if (eqFb != null && shFb) bpsFb = eqFb / shFb;
      }
      if (bpsFb != null && bpsFb > 0 && s.price != null && s.price > 0) pb = s.price / bpsFb;
    }
    var divConsecutive = va.divConsecutive || 0;
    var divYield = va.divYield;

    // ---- 基础量 ----
    // 资产负债表科目取**最新一期**（含季报，2026-09 起，批次 1），某科目季报缺则回退年报行——
    // 时点数越新越真，季报行覆盖率与年报持平（实测 98%+）；银行/外资口径结构性缺失两侧同缺。
    // 利润与披露比率（净利/负债率/毛利率）仍取年报，盈利状态单独走 TTM（见下）。
    // scoring.py 的 value_scores.bal 同一条规则。
    var latestBaVS = baList.length ? baList[baList.length - 1] : null;
    function balVS(key) {
      var v = (latestBaVS && latestBaVS[key] != null) ? latestBaVS[key] : (lastBa ? lastBa[key] : null);
      return v == null ? null : v;
    }
    function balDebtVS(bucket) {
      var v = latestBaVS ? debtKey(latestBaVS, bucket) : null;
      if (v == null && lastBa) v = debtKey(lastBa, bucket);
      return v == null ? null : v;
    }
    var ca = balVS('流动资产合计');      // 流动资产合计
    var cl = balVS('流动负债合计');      // 流动负债合计
    var tl = balVS('负债合计');          // 负债合计
    var assets = balVS('资产总计');      // 资产总计
    var cash = balVS('货币资金');        // 货币资金
    var stDebt = balDebtVS(DEBT_BUCKETS[0]);      // 短期借款
    var ltDebt = balDebtVS(DEBT_BUCKETS[2]);      // 长期借款
    var bond = balDebtVS(DEBT_BUCKETS[3]);        // 应付债券
    var due1y = balDebtVS(DEBT_BUCKETS[1]); // 一年内到期的长贷/债券/租赁重分类
    var lease = balDebtVS(DEBT_BUCKETS[4]);       // 租赁负债（新租赁准则表内化的分期付款）
    var intang = balVS('无形资产');      // 无形资产
    var goodwill = balVS('商誉');        // 商誉
    var netProfit = last ? last['净利润'] : null;
    // 负债率（批次 1 补）：改最新期 负债合计÷资产总计 现算，与同卡的流动比率同一张表；
    // 现算不出回退年报披露值。scoring.py 同一条规则
    var debtr = (tl != null && assets != null && assets > 0) ? tl / assets : (last ? last['资产负债率'] : null);
    var gMargin = last ? last['销售毛利率'] : null;
    var nMargin = last ? last['销售净利率'] : null;
    // 有息负债全口径：短借 + 一年内到期 + 长借 + 应付债券 + 租赁负债（字段缺失视为 0）
    var intDebt = sum([stDebt, due1y, ltDebt, bond, lease]);
    if (intDebt == null) intDebt = 0;
    // 盈利状态（批次 2）：滚动 TTM 净利润，缺则回退年报净利——「还赚不赚钱」回答到最新季
    var ttmNp = ttmNetProfit(d.indicators || [], '净利润');
    if (ttmNp == null) ttmNp = netProfit;
    var wc = (ca != null && cl != null) ? ca - cl : null;     // 营运资本
    // 长期有息负债全口径：一年内到期部分为重分类的长贷/债券，租赁负债计入（字段缺失视为 0）
    var ltd = sum([due1y, ltDebt, bond, lease]);
    if (ltd == null) ltd = 0;
    var curRatio = (ca != null && cl != null && cl > 0) ? ca / cl : null;
    var liqRatio = (ca != null && tl != null && tl > 0) ? ca / tl : null;
    var pepb = (pe != null && pb != null) ? pe * pb : null;

    // 近5年年报净利润（盈利稳定性）与近5年净利累计增长
    // posN 只数有数的行；有效行不足 5 整项判不动（null → schoolTotal 中性）：次新公司窗口
    // 不满或某年缺净利都不是「某年亏损」（scoring.py 同款，实测 199 家曾被压到 9/15 或 4/15）
    var net5 = annual.slice(-5).map(function (r) { return r['净利润']; });
    var valid5 = net5.filter(function (v) { return v != null; });
    var posN = valid5.filter(function (v) { return v > 0; }).length;
    // 基期取「不晚于 lastYear-5 的最近年报」（与 netCagr5 同一选基纪律），累计增长对齐
    // 0.33 阈值的 5 年标定；旧实现 annual.slice(-5) 首尾比实为 4 年增长（5 行只有 4 个间隔）
    var grow5 = null;
    if (annual.length >= 2 && last != null && lastYear != null && netProfit != null) {
      var gbase = null;
      for (var gi = annual.length - 2; gi >= 0; gi--) {
        if (Number(String(annual[gi]['报告期']).slice(0, 4)) <= lastYear - 5) { gbase = annual[gi]; break; }
      }
      if (!gbase) gbase = annual[0];
      var gbv = gbase['净利润'];
      if (gbv != null && gbv > 0) grow5 = netProfit / gbv - 1;
    }

    var basis = '评分基准：' + (lastYear ? lastYear + ' 年报' : '最新财报') +
      (s.time ? ' + ' + fmtDate(s.time) + ' 收盘价/市值' : '') +
      '；资产负债科目取最新一期财报（' + (latestBaVS ? String(latestBaVS['报告日']).slice(0, 10) : lastYear) +
      '，缺科目回退年报），盈利状态按 TTM；有息负债含一年内到期与租赁负债（全口径）';

    // （格攻/巴菲特两派 2026-10-06 整体移除——buy_validity v2 判决两线收益侧证据为负，
    //   历史判决表留档 docs/设计-四派买点信号回测.md；产品口径只剩格防/施洛斯两派。）

    // ---- 格雷厄姆 · 防御型烟蒂（《聪明的投资者》防御型标准）----
    // 严格性设计：规模为硬门槛（≥100亿满分）；关键安全项（流动比率/长期负债比/盈利稳定/增长）
    // 严重不达标时直接负分惩罚，而非仅给 0 分，避免“凑分式”达标
    function sizeScore(v) {
      if (v == null) return null;
      if (v >= 1e10) return 10;   // ≥100 亿
      if (v >= 5e9) return 6;     // 50~100 亿
      if (v >= 3e9) return 3;     // 30~50 亿
      return 0;                   // <30 亿（过小，防御型不参与）
    }
    function divScore10(years) {
      if (years >= 10) return 15;
      if (years >= 7) return 10;
      if (years >= 5) return 5;
      if (years >= 3) return 2;
      return 0;                   // <3 年分红史，防御型不给分
    }
    var ltdScore;
    if (wc == null) { ltdScore = null; }
    else if (wc <= 0) { ltdScore = -10; }  // 营运资本为负（流动负债>流动资产）危险信号
    else if (ltd <= wc) { ltdScore = 20; }
    else if (ltd <= wc * 1.5) { ltdScore = lerpScore(ltd / wc, 1, 1.5, 20, 5); }
    else { ltdScore = 0; }
    var gD = [
      it('企业规模（总资产）', fmtMoney(assets), '≥ 100 亿（人民币当量，本币按固定汇率折算）', 10, sizeScore(toCny(assets, d.market))),
      it('流动比率', fmtNum(curRatio), '≥ 2', 20, curRatio == null ? null
        : (curRatio >= 2 ? 20 : curRatio >= 1.5 ? lerpScore(curRatio, 1.5, 2, 5, 20) : curRatio >= 1 ? 5 : -10), noCA || noCL),
      it('长期有息负债 / 营运资本', (ltd == null ? '-' : fmtMoney(ltd)) + ' / ' + (wc == null ? '-' : fmtMoney(wc)), '长期负债 ≤ 营运资本', 20, ltdScore, noCA || noCL),
      it('盈利稳定（近5年净利为正）', valid5.length < 5 ? '-' : posN + '/5 年', '5 年全部为正', 15, valid5.length < 5 ? null : (posN >= 5 ? 15 : posN === 4 ? 9 : posN === 3 ? 4 : -5)),
      it('连续分红年数', (divConsecutive || 0) + ' 年', '≥ 10 年', 15, divScore10(divConsecutive || 0)),
      it('近5年净利累计增长', fmtPct(grow5), '≥ 33%', 10, grow5 == null ? null : (grow5 >= 0.33 ? 10 : grow5 >= 0 ? lerpScore(grow5, 0, 0.33, 0, 10) : -5)),
      it('市盈率（TTM）', pe == null ? '-' : (pe > 0 ? fmtNum(pe) : 'PE 为负（亏损）'), '≤ 15（亦是保守卖出参考倍数）', 5, lerpScoreNonneg(pe, G_D_PE_FULL, 25, 5, 0)),
      it('PE × PB', pepb == null ? '-' : ((pe > 0 && pb > 0) ? fmtNum(pepb) : 'PE/PB 为负'), '≤ 22.5', 5, pepb != null ? ((pe <= 0 || pb <= 0) ? 0 : (pepb <= 22.5 ? 5 : (pepb <= 45 ? lerpScore(pepb, 22.5, 45, 5, 0) : 0))) : null)
    ];
    // 其中流动比率/长期负债比/盈利稳定/增长四项可为负（最低各 −10/−10/−5/−5），
    // 归一分母仍是满分合计 100，故 −30 的下限不变
    var gDTotal = schoolTotal(gD);

    // ---- 施洛斯风险扣分（资产萎缩/减值结构/债务恶化/经营溃败的量化危险信号，仅负分）----
    // 有息负债全口径（与上方 intDebt 一致：短借+一年内到期+长借+债券+租赁，缺键当 0）
    // 时点类端点（批次 1）：最新一期行优先，权益/有息负债在季报缺时回退年报行
    var lastEq = equityOf(latestBaVS);
    if (lastEq == null) lastEq = equityOf(lastBa);
    var earliestEq = baAnnual.length >= 5 ? equityOf(baAnnual[0]) : null;
    var intDebtNow = intDebtOf(latestBaVS);
    if (intDebtNow == null) intDebtNow = lastBa ? intDebtOf(lastBa) : null;
    var intDebtEarliest = baAnnual.length >= 5 ? intDebtOf(baAnnual[0]) : null;
    // 近5年扣非亏损年数（annual 最后 5 行）
    var adjNet = annual.slice(-5).map(function (r) { return r['扣非净利润']; });
    var adjLossN = adjNet.filter(function (v) { return v != null && v < 0; }).length;
    var adjValid = adjNet.filter(function (v) { return v != null; }).length;
    // 应收账款/营收 3 年年报均值（位置对齐，缺失年忽略；港股“应收帐款”科目兼容）
    var ar3 = baAnnual.slice(-3).map(arOf);
    var rev3 = inAnnual.slice(-3).map(function (r) { return r['营业总收入']; });
    var ar3Sum = ar3.length === 3 ? sum(ar3) : null, rev3Sum = rev3.length === 3 ? sum(rev3) : null;
    var arRev3 = (ar3Sum != null && rev3Sum != null && rev3Sum > 0) ? ar3Sum / rev3Sum : null;
    // 近3年累计经营现金流 vs 累计利息费用
    var ocf3 = cfAnnual.slice(-3).map(function (r) { return r['经营活动产生的现金流量净额']; });
    var intExp3 = inAnnual.slice(-3).map(function (r) { return r['利息费用']; });
    var ocf3Sum = sum(ocf3), intExp3Sum = sum(intExp3);
    var ocfCovers = (ocf3Sum != null && intExp3Sum != null) ? ocf3Sum >= intExp3Sum : null;
    // 近5年累计经营现金流（区分扩张举债 vs 补亏举债）
    var ocf5Sum = sum(cfAnnual.slice(-5).map(function (r) { return r['经营活动产生的现金流量净额']; }));
    // 5 年趋势（最新 vs 最早年报，要求 ≥5 个年报）
    var spanOk = annual.length >= 5;
    var revNow = last ? last['营业总收入'] : null;
    var revEarliest = spanOk ? annual[0]['营业总收入'] : null;
    var gMarginNow = last ? last['销售毛利率'] : null;
    var gMarginEarliest = spanOk ? annual[0]['销售毛利率'] : null;
    var eqGrow = (lastEq != null && earliestEq != null && earliestEq > 0) ? lastEq / earliestEq - 1 : null;
    var intDebtGrow = (intDebtNow != null && intDebtEarliest != null && intDebtEarliest > 0) ? intDebtNow / intDebtEarliest - 1 : null;
    var revGrow = (revNow != null && revEarliest != null && revEarliest > 0) ? revNow / revEarliest - 1 : null;
    var gMarginDelta = (gMarginNow != null && gMarginEarliest != null) ? gMarginNow - gMarginEarliest : null;
    var gwIntSum = (goodwill != null ? goodwill : 0) + (intang != null ? intang : 0);
    // 负权益有两种，不能一律豁免：回购把权益打成负数而公司仍在赚钱（达美乐/HCA 这类）
    // 属正常资本结构；亏损导致的资不抵债、账上却还压着商誉无形，才是本项最该扣的情形
    // ——比值在负权益下算不出来，但实质是"无形压在已被抹平的权益基数上"，按最重档处理
    var eqDistress = lastEq != null && lastEq <= 0 && gwIntSum > 0 && !(ttmNp != null && ttmNp > 0);
    var inv = balVS('存货');
    // 9 个量化扣分项：危险信号触发负分（与正向分叠加），数据不足给 0 不误伤
    var riskItems = [
      it('净资产5年变动（归母权益）', eqGrow == null ? '-' : fmtPct(eqGrow), '≥ -20%（萎缩扣分）', 5,
        eqGrow == null ? 0 : (eqGrow <= -0.4 ? -5 : eqGrow <= -0.2 ? -3 : 0)),
      it('近5年扣非亏损年数', adjValid < 3 ? '-' : adjLossN + '/5 年', '≤ 1 年（扣非口径）', 5,
        adjValid < 3 ? 0 : (adjLossN >= 3 ? -5 : adjLossN === 2 ? -3 : 0)),
      it('(商誉+无形资产)/归母权益', (lastEq != null && lastEq > 0) ? fmtPct(gwIntSum / lastEq) : (eqDistress ? '资不抵债' : '-'), '≤ 30%（减值风险）', 4,
        (lastEq != null && lastEq > 0) ? (gwIntSum / lastEq > 0.6 ? -4 : gwIntSum / lastEq > 0.3 ? -2 : 0) : (eqDistress ? -4 : 0)),
      it('应收账款/营收（3年年报均值）', arRev3 == null ? '-' : fmtPct(arRev3), '≤ 40%（坏账风险）', 3,
        arRev3 == null ? 0 : (arRev3 > 0.6 ? -3 : arRev3 > 0.4 ? -1.5 : 0)),
      it('存货/总资产（最新年报）', (inv != null && assets != null && assets > 0) ? fmtPct(inv / assets) : '-', '≤ 35%（跌价风险）', 2,
        (inv != null && assets != null && assets > 0) ? (inv / assets > 0.5 ? -2 : inv / assets > 0.35 ? -1 : 0) : 0),
      it('有息负债5年变动', intDebtGrow == null ? '-' : fmtPct(intDebtGrow), '≤ 50%；翻倍且5年经营现金流为负重扣（补亏举债）', 6,
        intDebtGrow == null ? 0 : (intDebtGrow > 1 ? ((ocf5Sum != null && ocf5Sum < 0) ? -6 : -3) : intDebtGrow > 0.5 ? -2 : 0)),
      it('近3年经营现金流 vs 利息费用', (ocf3Sum == null || intExp3Sum == null) ? '-' : fmtMoney(ocf3Sum) + ' / ' + fmtMoney(intExp3Sum), '经营现金流 ≥ 利息费用', 4,
        ocfCovers === false ? -4 : 0),
      it('营收5年变动', revGrow == null ? '-' : fmtPct(revGrow), '≥ -20%（竞争地位）', 4,
        revGrow == null ? 0 : (revGrow <= -0.5 ? -4 : revGrow <= -0.2 ? -2 : 0)),
      it('毛利率5年变动', gMarginDelta == null ? '-' : fmtPct(gMarginDelta), '≥ -10pct（定价权）', 4,
        gMarginDelta == null ? 0 : (gMarginDelta <= -0.2 ? -4 : gMarginDelta <= -0.1 ? -2 : 0))
    ];

    // ---- 施洛斯烟蒂（资产折扣 + 低估值 + 低负债 + 股息）----
    var sItems = [
      it('市净率', pb == null ? '-' : (pb > 0 ? fmtNum(pb) : 'PB 为负（资不抵债）'), '≤ 0.75（资产折扣，亦是买入参考倍数）', 25, lerpScoreNonneg(pb, S_PB_FULL, 1.5, 25, 0)),
      it('市盈率（TTM）', pe == null ? '-' : (pe > 0 ? fmtNum(pe) : 'PE 为负（亏损）'), '≤ 10', 20, lerpScoreNonneg(pe, 10, 20, 20, 0)),
      it('流动资产/总负债', liqRatio == null ? '-' : fmtNum(liqRatio), '≥ 2', 20, lerpScore(liqRatio, 1, 2, 0, 20), noCA),
      it('股息率（近12月）', fmtPct(divYield), '≥ 3%', 15, lerpScore(divYield, 0, 0.03, 0, 15)),
      it('TTM 净利润', fmtMoney(ttmNp), '> 0', 10, ttmNp != null && ttmNp > 0 ? 10 : 0),
      it('市值 / 流动资产', (mcap == null ? '-' : fmtMoney(mcap)) + ' / ' + (ca == null ? '-' : fmtMoney(ca)), '市值 ≤ 流动资产', 10,
        (mcap != null && ca != null && ca > 0) ? (mcap <= ca ? 10 : lerpScore(mcap / ca, 1, 2, 10, 0)) : null, noCA)
    ];
    // 正分项归一 + 9 个扣分项原样相加：扣分项缺数据本就给 0，不参与归一（理由见 schoolTotal）
    var sTotal = schoolTotal(sItems, riskItems);

    // 有效满分/缺维数：不计分项（缺失或不适用）都不进满分，总分按有效满分折算，需向用户标注
    // （跨市场可比性）。缺失与不适用要分开报：前者是数据没到，后者是报表里就没这个科目。
    function effOf(arr) {
      var mx = 0, miss = 0, na = 0;
      arr.forEach(function (x) {
        if (x.score != null) mx += x.max;
        else if (x.na) na += 1;
        else miss += 1;
      });
      return { max: mx, miss: miss, na: na };
    }

    return {
      basis: basis,
      grahamDef: { title: '防御型烟蒂 · 防御型投资者标准', total: gDTotal, items: gD, eff: effOf(gD),
        note: '对应《聪明的投资者》第 14 章防御型投资者选股标准（规模/流动比率/长期负债/盈利稳定/分红历史/盈利增长/估值）。规模为硬门槛（总资产≥100亿），关键安全项（流动比率<1、营运资本为负、近5年过半亏损、净利负增长）直接负分惩罚。' },
      schloss: { title: '施洛斯烟蒂 · 资产折扣+低估值+低负债', total: sTotal, items: sItems.concat(riskItems), eff: effOf(sItems),
        note: '沃尔特·施洛斯风格：以低于净资产/流动资产的价格买入、负债极低、有股息，分散持有等待价值回归。风险扣分项为量化危险信号：净资产萎缩/扣非亏损、商誉无形与应收存货减值结构、有息负债攀升与利息覆盖不足、营收毛利率趋势溃败，数据不足不扣分；管理层掏空等无公开量化数据的信号未纳入。' }
    };
  }

  // ---- 财务报表造假可能性分析（Beneish M-Score 思路的量化红旗加权）----
  // 百分制：分数越高造假可能性越大；8 项红旗各按严重度(0~1)×权重计分，权重合计 100；
  // 数据不足该项计 0 不误伤；仅为量化筛查，不构成造假认定

  // ---- 财务报表造假可能性分析（Beneish M-Score 思路的量化红旗加权）----
  // 百分制：分数越高造假可能性越大；8 项红旗各按严重度(0~1)×权重计分，权重合计 100；
  // 数据不足该项计 0 不误伤；仅为量化筛查，不构成造假认定
  function fraudAnalysis(d) {
    var annual = annualRows(d.indicators || []);
    var last = annual.length ? annual[annual.length - 1] : null;
    var prev = annual.length >= 2 ? annual[annual.length - 2] : null;
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var prevDate = prev ? String(prev['报告期']).slice(0, 10) : null;
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var cfList = (d.cashflow || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    var prevBa = prevDate ? sheetRowByDate(baList, prevDate) : null;
    var lastCf = lastDate ? sheetRowByDate(cfList, lastDate) : null;
    var lastYear = lastDate ? lastDate.slice(0, 4) : null;

    var rev = last ? last['营业总收入'] : null;
    var revPrev = prev ? prev['营业总收入'] : null;
    var net = last ? last['净利润'] : null;
    var ocf = lastCf ? lastCf['经营活动产生的现金流量净额'] : null;
    var soldCash = lastCf ? lastCf['销售商品、提供劳务收到的现金'] : null;
    var assets = lastBa ? lastBa['资产总计'] : null;
    var ar = arOf(lastBa);
    var arPrev = arOf(prevBa);
    var inv = lastBa ? lastBa['存货'] : null;
    var invPrev = prevBa ? prevBa['存货'] : null;
    var otherAr = lastBa ? (lastBa['其他应收款'] != null ? lastBa['其他应收款'] : lastBa['其他应收款(合计)']) : null;
    // 商誉/无形双缺是「科目没取到」不是「没有软资产」：按 0 计会让该项(5分)永不亮灯，
    // 且 0 分折进归一化分母会稀释其余红旗——缺数据显得更干净（scoring.py 同款，实测
    // 256 家双缺、其中 235 家港美股）。单缺一科按另一科计。
    var gwV = lastBa ? lastBa['商誉'] : null;
    var itV = lastBa ? lastBa['无形资产'] : null;
    var soft = (gwV == null && itV == null) ? null : (gwV || 0) + (itV || 0);
    var gm = last ? last['销售毛利率'] : null;
    var gmPrev = prev ? prev['销售毛利率'] : null;

    // 近5年累计净现比（比单年稳健：累计经营现金流 ÷ 累计净利润）
    var sumNet = 0, sumOcf = 0, hitNO = false;
    annual.slice(-5).forEach(function (r) {
      var cf = sheetRowByDate(cfList, String(r['报告期']).slice(0, 10));
      var n = r['净利润'], o = cf ? cf['经营活动产生的现金流量净额'] : null;
      if (n != null && o != null) { sumNet += n; sumOcf += o; hitNO = true; }
    });
    var ratio5 = (hitNO && sumNet > 0) ? sumOcf / sumNet : null;

    function grow(cur, pre) { return (cur != null && pre != null && pre > 0) ? cur / pre - 1 : null; }
    var revGrow = grow(rev, revPrev);
    var arGap = (grow(ar, arPrev) != null && revGrow != null) ? grow(ar, arPrev) - revGrow : null;
    var invGap = (grow(inv, invPrev) != null && revGrow != null) ? grow(inv, invPrev) - revGrow : null;
    var gmDelta = (gm != null && gmPrev != null) ? gm - gmPrev : null;
    var tata = (net != null && ocf != null && assets != null && assets > 0) ? (net - ocf) / assets : null;
    var otherShare = (otherAr != null && assets != null && assets > 0) ? otherAr / assets : null;
    var softShare = (soft != null && assets != null && assets > 0) ? soft / assets : null;
    var collect = (soldCash != null && rev != null && rev > 0) ? soldCash / rev : null;

    // 严重度分段：v≤a→0；a~b→0~0.5；b~c→0.5~1；≥c→1（越高越可疑）
    function sev(v, a, b, c) {
      if (v == null) return null;
      if (v <= a) return 0;
      if (v >= c) return 1;
      if (v <= b) return (v - a) / (b - a) * 0.5;
      return 0.5 + (v - b) / (c - b) * 0.5;
    }
    function w(score, maxV) { return score == null ? null : score * maxV; }

    // 净现比：≥1 无红旗；0~1 线性升；≤0 满严重（利润无现金支撑）
    var s1 = ratio5 == null ? null : lerpScore(ratio5, 0, 1, 1, 0);
    // 收现比：≥100% 无红旗；60%~100% 线性升；≤60% 满严重
    var s8 = collect == null ? null : lerpScore(collect, 0.6, 1, 1, 0);
    // 毛利率上升才可疑（下降属经营问题）
    var s5 = gmDelta == null ? null : (gmDelta <= 0 ? 0 : sev(gmDelta, 0, 0.05, 0.10));

    var items = [
      it('5年累计净现比（经营现金流÷净利润）', ratio5 == null ? '-' : fmtNum(ratio5), '≥ 1（利润有现金支撑）', 25, w(s1, 25)),
      it('总应计比率（净利润−经营现金流）÷总资产', fmtPct(tata), '≤ 2%（应计越高越可疑）', 20, w(sev(tata, 0.02, 0.06, 0.10), 20)),
      it('应收账款增速 − 营收增速', fmtPct(arGap), '≤ 5pp（应收异常快于营收）', 15, w(sev(arGap, 0.05, 0.20, 0.40), 15)),
      it('存货增速 − 营收增速', fmtPct(invGap), '≤ 5pp（存货异常堆积）', 10, w(sev(invGap, 0.05, 0.25, 0.50), 10)),
      it('销售毛利率同比变动', gmDelta == null ? '-' : (gmDelta > 0 ? '+' : '') + (gmDelta * 100).toFixed(1) + 'pp', '上升≤0（逆势上升可疑）', 10, w(s5, 10)),
      it('其他应收款÷总资产（关联方占用）', fmtPct(otherShare), '≤ 2%', 10, w(sev(otherShare, 0.02, 0.05, 0.10), 10)),
      it('（商誉＋无形资产）÷总资产（资产偏软）', fmtPct(softShare), '≤ 10%', 5, w(sev(softShare, 0.10, 0.20, 0.35), 5)),
      it('销售收现比（销售收现÷营收）', fmtPct(collect), '≥ 100%', 5, w(s8, 5))
    ];
    // 归一化：本分越低越可疑，缺项若只从总分里少加一笔，等于数据不全反而“更干净”
    var total = weightedTotal(items.map(function (x) { return [x.score, x.max]; }));
    return {
      title: '财务报表造假可能性 · 量化红旗筛查',
      basis: '评分基准：' + (lastYear || '-') + ' 年报（同比项对比 ' + (prevDate ? prevDate.slice(0, 4) : '-') + ' 年报）',
      total: total == null ? null : Math.round(total * 10) / 10,
      items: items,
      note: '借鉴 Beneish M-Score 思路：将净现背离、高应计、应收/存货增速背离营收、毛利率逆势上升、其他应收款占用、资产偏软、收现不足等量化红旗按严重度加权为 0~100 分，分数越高造假可能性越大。总分按“可算出的项”归一，缺数据的项既不加罚也不免罚；本分为量化筛查信号，不构成对造假的认定，需结合审计意见、监管问询等定性信息综合判断。'
    };
  }

  /* ---------------- 价值陷阱分 T（0~100，分高＝坏消息堆得多） ----------------
   * 权重与阈值来自 backend/scripts/trap_validity.py 的实测：A 股 32,178 条「公司 × T 年」观测，
   * 信息集按「信号日当天真的公开了」截断，结局取信号日之后公开的第一份年报（上限 18 个月）。
   * 权重是该项坏侧相对其余的最强结局对数危险比 ln(lift)，标定表是同一次回测的五档实测发生率。
   *
   * 三处与四派刻意不同，改之前先读：
   * 1. 阈值是**固定绝对值**，不是当日横截面分位。按分位取的话，一家公司的分会被当天其他公司
   *    的涨跌改掉，存进按日入库的分数就不可复现。数值取回测当时的三分位边界后四舍五入。
   * 2. 归一化用「固定分母 Σ权重」，**故意不用** weightedTotal。那套归一让缺项变中性，而陷阱分
   *    最怕的正是「没查到」被读成「没毛病」——只查得动 1 项、而那 1 项恰好亮灯的公司，按可用项
   *    归一会拿满 100 分。这里缺项记 0、分母恒为满分母，覆盖度由 eff.evaluated 单独出。代价是
   *    覆盖不足会把分压低：7 项里只有商誉稀疏（最新年报未单列那一行，实测 5,553 家 A 股中 2,854 家
   *    判不动，占 14% 分母），压低幅度有界，且方向是「看不准就别声称干净」而非「看不准就更可疑」。
   * 3. 只覆盖 A 股：定增/回购源、扣非口径、减值科目都只在这一侧成立，港美股整列**不适用**
   *    （不适用不是缺失，见 eff.na）。
   */

  var TRAP_W = {
    ded_half: 1.63, gw_asset: 0.70, roe_delta: 0.69, fraud: 0.51,
    seo_dilu: 0.49, gm_delta: 0.48, ocfnp_med: 0.42
  };

  var TRAP_SUM_W = 4.92;                       // Σ TRAP_W，固定分母

  var TRAP_CUT = { roe_delta: -0.04, gm_delta: -0.03, ocfnp_med: 0.80 };
  // 回测五分位边界（C = Σ 权重×亮灯）与该档实测发生率，% —— 分数自己没有含义，这张表才有。
  // grade 是列表与详情共用的着色：档位单调变坏，故只往 bad 方向走，不再有「高分=好」那套反向。

  // 回测五分位边界（C = Σ 权重×亮灯）与该档实测发生率，% —— 分数自己没有含义，这张表才有。
  // grade 是列表与详情共用的着色：档位单调变坏，故只往 bad 方向走，不再有「高分=好」那套反向。
  var TRAP_BANDS = [
    { hi: 0.001, label: '档1 无证据', grade: 'good', loss: 3.66, imp5: 6.47, imp3: 13.48, divcut: 5.92, bvpsdn: 9.75 },
    { hi: 0.50, label: '档2 单点', grade: 'mid', loss: 4.87, imp5: 7.75, imp3: 16.18, divcut: 9.32, bvpsdn: 11.35 },
    { hi: 1.00, label: '档3 两点', grade: 'mid', loss: 6.27, imp5: 11.76, imp3: 20.17, divcut: 8.90, bvpsdn: 17.04 },
    { hi: 1.60, label: '档4 成串', grade: 'low', loss: 10.38, imp5: 17.90, imp3: 27.26, divcut: 13.25, bvpsdn: 22.88 },
    { hi: Infinity, label: '档5 叠加', grade: 'bad', loss: 28.70, imp5: 23.31, imp3: 35.88, divcut: 24.11, bvpsdn: 23.44 }
  ];

  function trapBandOf(c) {
    for (var i = 0; i < TRAP_BANDS.length; i++) {
      if (c <= TRAP_BANDS[i].hi) return TRAP_BANDS[i];
    }
    return TRAP_BANDS[TRAP_BANDS.length - 1];
  }

  // 近 5 期年报里那一项的中位数（不足 3 期不算——两期的中位数就是平均数，噪声当趋势）

  // 近 5 期年报里那一项的中位数（不足 3 期不算——两期的中位数就是平均数，噪声当趋势）
  function medOf(vals) {
    var got = vals.filter(function (v) { return v != null && isFinite(v); });
    if (got.length < 3) return null;
    got.sort(function (a, b) { return a - b; });
    var m = Math.floor(got.length / 2);
    return got.length % 2 ? got[m] : (got[m - 1] + got[m]) / 2;
  }

  function trapScore(d) {
    d = d || {};
    if ((d.market || 'A') !== 'A') {
      return { total: null, c: null, na: true,
        reason: '口径只覆盖 A 股（定增/回购源与扣非科目只在这一侧成立）',
        items: [], eff: { evaluated: 0, missing: 0, na: 7 } };
    }
    var annual = annualRows(d.indicators || []);
    var baAnnual = annualBalanceRows(d.balance);
    var cfAnnual = annualBalanceRows(d.cashflow);
    var win = annual.slice(-5);                       // 截至最新年报的最近 5 期
    var cur = annual.length ? win[win.length - 1] : null;
    if (!cur) return { total: null, c: null, na: false, reason: '无年报序列', items: [],
      eff: { evaluated: 0, missing: 7, na: 0 } };
    var hiY = Number(String(cur['报告期']).slice(0, 4));
    var loY = Number(String(win[0]['报告期']).slice(0, 4));
    var curBa = sheetRowByDate(baAnnual, String(cur['报告期']).slice(0, 10)) || {};
    var items = [], c = 0;

    function push(key, label, value, bad, why) {
      var it = { key: key, label: label, value: value, bad: bad, weight: TRAP_W[key] };
      if (bad === null) it.na = true;                 // 判不动：不进分子也不点亮
      else if (bad) c += TRAP_W[key];
      it.why = why;
      items.push(it);
    }

    // 1. 扣非不足报告净利一半（利润靠一次性收益撑）——本轮实测最强单项，5.08× 于转亏
    var ded = cur['扣非净利润'], net = cur['净利润'];
    push('ded_half', '扣非 < 0.5×报告净利', ded,
      (ded == null || !(net > 0)) ? null : (ded < 0.5 * net ? 1 : 0),
      net > 0 ? '扣非 ' + fmtMoney(ded) + ' vs 报告净利 ' + fmtMoney(net) : '报告净利非正，比值无意义');

    // 2. 商誉/总资产 ≥10%（减值弹药）
    var gw = curBa['商誉'], ta = curBa['资产总计'];
    push('gw_asset', '商誉/总资产 ≥ 10%', (gw != null && ta > 0) ? gw / ta : null,
      (gw == null || !(ta > 0)) ? null : (gw / ta >= 0.10 ? 1 : 0),
      gw == null ? '最新年报未单列商誉' : '商誉 ' + fmtMoney(gw) + ' / 总资产 ' + fmtMoney(ta));

    // 3~4. ROE 与毛利率「最新 − 近5年中位」：看减速，不看水平（水平归成长分）
    var roeMed = medOf(win.map(function (r) { return r['净资产收益率']; }));
    push('roe_delta', 'ROE 最新 − 近5年中位 ≤ −4pp',
      (roeMed == null || cur['净资产收益率'] == null) ? null : cur['净资产收益率'] - roeMed,
      (roeMed == null || cur['净资产收益率'] == null) ? null
        : (cur['净资产收益率'] - roeMed <= TRAP_CUT.roe_delta ? 1 : 0),
      '最新 ' + fmtPct(cur['净资产收益率']) + ' vs 中位 ' + (roeMed == null ? '-' : fmtPct(roeMed)));
    var gmMed = medOf(win.map(function (r) { return r['销售毛利率']; }));
    push('gm_delta', '毛利率 最新 − 近5年中位 ≤ −3pp',
      (gmMed == null || cur['销售毛利率'] == null) ? null : cur['销售毛利率'] - gmMed,
      (gmMed == null || cur['销售毛利率'] == null) ? null
        : (cur['销售毛利率'] - gmMed <= TRAP_CUT.gm_delta ? 1 : 0),
      '最新 ' + fmtPct(cur['销售毛利率']) + ' vs 中位 ' + (gmMed == null ? '-' : fmtPct(gmMed)));

    // 5. 净现比 5 年中位 ≤0.80（利润兑不出钱）。上档发生率同样偏高，故只取低侧作证据
    var ocfnp = medOf(win.map(function (r) {
      var cf = sheetRowByDate(cfAnnual, String(r['报告期']).slice(0, 10));
      var n = r['净利润'];
      return (cf && cf['经营活动产生的现金流量净额'] != null && n > 0)
        ? cf['经营活动产生的现金流量净额'] / n : null;
    }));
    push('ocfnp_med', '净现比 5 年中位 ≤ 0.80', ocfnp,
      ocfnp == null ? null : (ocfnp <= TRAP_CUT.ocfnp_med ? 1 : 0),
      ocfnp == null ? '可算年份不足 3 期' : '5 年中位 ' + fmtNum(ocfnp));

    // 6. 造假分 >50：与列表页门槛、刷池线同一个数，不另起口径
    var fa = fraudAnalysis(d);
    push('fraud', '造假分 > 50', fa.total,
      fa.total == null ? null : (fa.total > 50 ? 1 : 0),
      fa.total == null ? '指标行不足' : '红旗加权 ' + fa.total);

    // 7. 近 5 年定增摊薄 / 隐含股本。分母用 归母权益÷每股净资产 反推，不用「股本」列——
    //    送股转增会把那一列放大却不摊薄任何人的权益。只喂定增行：回购族没有一项进 T
    var acts = d.seo_actions;              // 没给这个字段 ≠ 给空数组：前者判不动，后者才是「近5年无定增」
    var eq = equityOf(curBa);
    var bps = cur['每股净资产'];
    var sh = (eq != null && bps > 0) ? eq / bps : null;
    var issued = 0, seen = false;
    (acts || []).forEach(function (r) {
      var ds = String(r.issue_date || r.listing_date || '').slice(0, 10);
      var yy = Number(ds.slice(0, 4));
      if (ds && yy >= loY && yy <= hiY && r.num) { issued += r.num; seen = true; }
    });
    var dilu = acts == null ? null : (seen ? (sh ? issued / sh : null) : 0);
    push('seo_dilu', '近5年定增新增股本 / 现股本', dilu,
      dilu == null ? null : (dilu > 0 ? 1 : 0),
      acts == null ? '未取到股本事件数据'
        : (seen ? '新增 ' + fmtMoney(issued) + ' 股 / 隐含股本 ' + (sh ? fmtMoney(sh) : '算不出')
          : '近5年无定增（A 股定增史全量可查，无事件是公开事实而非缺失）'));

    var ev = items.filter(function (x) { return x.bad !== null; }).length;
    return {
      na: false, c: c, total: ev ? Math.round(c / TRAP_SUM_W * 1000) / 10 : null,
      band: trapBandOf(c), items: items, basis: '评分基准：' + hiY + ' 年报（窗口 ' + loY + '~' + hiY + '）',
      eff: { evaluated: ev, missing: items.length - ev, na: 0 },
      note: '把「利润是撑出来的、资产里压着要减的、股本被摊过、回报在往下走」这几类各自亮灯的证据，'
        + '按回测出的对数危险比相加后折成 0~100。分母固定为全部 7 项，所以缺项只会压低分数、'
        + '不会加分——查不动的项按「没证据」计，分数低不等于没陷阱，先看抬头那行「可判 N/7 项」。'
    };
  }

  // 陷阱分项的「当前值」格式化：比率和差值是 0~1 的小数，金额是元，其余是百分制分

  // 陷阱分项的「当前值」格式化：比率和差值是 0~1 的小数，金额是元，其余是百分制分
  var TRAP_VALUE_FMT = {
    ded_half: fmtMoney, gw_asset: fmtPct, roe_delta: fmtPct, gm_delta: fmtPct,
    ocfnp_med: fmtNum, fraud: fmtNum, seo_dilu: fmtPct
  };

  // 价值陷阱分评分卡：分数只是七项证据的加权合计，看得懂要靠分项三态 + 档位实测发生率

  var G_ITEMS = [
    { key: 'np_g5', label: '净利 5 年年化增速', weight: 22, lo: -0.05, hi: 0.15 },
    { key: 'rev_g5', label: '营收 5 年年化增速', weight: 16, lo: -0.05, hi: 0.15 },
    { key: 'roe_med', label: 'ROE 近 5 年中位', weight: 16, lo: 0.05, hi: 0.18 },
    { key: 'bps_g5', label: '每股净资产 5 年年化增速', weight: 14, lo: -0.02, hi: 0.12 },
    { key: 'stab', label: '近 5 年净利负增长年数', weight: 14, lo: 3, hi: 0 },
    // accel 量的是两段增速之差，离散度由市场整体在减速还是加速主导：锚点按面板输入端 p10→p90
    // （实测 −0.74/+0.42）取整定的，写死 ±10% 会把 56% 的公司挤在零分上、一项退化成开关。
    { key: 'accel', label: '近 2 年年化 − 前 3 年年化', weight: 10, lo: -0.7, hi: 0.4 },
    { key: 'roe_trend', label: 'ROE 最新 − 近 5 年中位', weight: 8, lo: -0.05, hi: 0.05 }
  ];

  var G_SUM_W = 100;      // Σ G_ITEMS.weight，固定分母

  var G_CAP = 0.5;        // 增速 winsorize 边界 ±50%/年

  var G_MIN_PAIRS = 4;    // 5 年窗本该有 5 个同比间隔，缺 1 个仍算判得动

  // 年化增速，夹到 ±G_CAP。与既有的 cagr 刻意不同两处：基期为正而当期转负记成下界（那是真实
  // 的坏消息，不是「算不出」），以及上界夹逼（否则一次重组就能把整轴拉爆）。

  // 年化增速，夹到 ±G_CAP。与既有的 cagr 刻意不同两处：基期为正而当期转负记成下界（那是真实
  // 的坏消息，不是「算不出」），以及上界夹逼（否则一次重组就能把整轴拉爆）。
  function gGrowth(cur, prev, span) {
    if (cur == null || prev == null || !(span > 0) || !(prev > 0)) return null;
    if (cur <= 0) return -G_CAP;
    return Math.max(-G_CAP, Math.min(G_CAP, Math.pow(cur / prev, 1 / span) - 1));
  }

  function growthScore(d) {
    d = d || {};
    var annual = annualRows(d.indicators || []);
    var byYear = {};
    var cols = [['net', '净利润'], ['rev', '营业总收入'], ['roe', '净资产收益率'], ['bps', '每股净资产']];
    annual.forEach(function (r) {
      var y = Number(String(r['报告期']).slice(0, 4));
      if (!y) return;
      var slot = byYear[y] || (byYear[y] = {});
      cols.forEach(function (p) {
        var v = r[p[1]];
        if (typeof v === 'number' && isFinite(v)) slot[p[0]] = v;   // 同年后一行覆盖前一行，缺列留空
      });
    });
    var years = Object.keys(byYear).map(Number).sort(function (a, b) { return a - b; });
    if (years.length < 3) {
      return { total: null, reason: '公开年报不足 3 期', items: [], na: false,
        eff: { evaluated: 0, missing: G_ITEMS.length, na: 0 } };
    }
    var ay = years[years.length - 1], base = null;
    for (var i = years.length - 2; i >= 0; i--) {
      if (years[i] <= ay - 5) { base = years[i]; break; }
    }
    if (base == null) base = years[0];          // 上市晚，历史不足 5 年：按实际跨度年化
    var span = ay - base;
    var cur = byYear[ay], b = byYear[base];

    // ROE 中位取「基期之后到锚点」那几年（与回测同窗）；不足 3 期不算——两期的中位数就是平均数
    var roes = [];
    years.forEach(function (y) {
      if (y > base && byYear[y].roe != null) roes.push(byYear[y].roe);
    });
    var roeMed = medOf(roes);

    var neg = 0, pairs = 0;
    for (var y2 = base; y2 < ay; y2++) {
      var p = byYear[y2] ? byYear[y2].net : null;
      var q = byYear[y2 + 1] ? byYear[y2 + 1].net : null;
      if (p != null && q != null) { pairs++; if (q < p) neg++; }
    }
    var mid = byYear[ay - 2] ? byYear[ay - 2].net : null;
    var a1 = (span >= 3 && mid != null && b.net != null) ? gGrowth(cur.net, mid, 2) : null;
    var a2 = (span >= 3 && mid != null && b.net != null) ? gGrowth(mid, b.net, span - 2) : null;

    var raw = {
      np_g5: gGrowth(cur.net, b.net, span),
      rev_g5: gGrowth(cur.rev, b.rev, span),
      roe_med: roeMed,
      bps_g5: gGrowth(cur.bps, b.bps, span),
      stab: pairs >= G_MIN_PAIRS ? neg : null,
      accel: (a1 != null && a2 != null) ? a1 - a2 : null,
      roe_trend: (roeMed != null && cur.roe != null) ? cur.roe - roeMed : null
    };
    // 判不动的原因逐分项给：把「算不出」写成一句笼统的「数据不足」，使用者就没法区分
    // 「这家公司没披露」和「口径压根不适用」，而前者过两个月就会变。
    function growthWhy(col, name) {
      if (cur[col] == null) return '最新年报缺' + name;
      if (b[col] == null) return base + ' 年缺' + name;
      return base + ' 年' + name + '非正，年化增速无意义';
    }
    var naWhy = {
      np_g5: growthWhy('net', '净利润'),
      rev_g5: growthWhy('rev', '营业总收入'),
      bps_g5: growthWhy('bps', '每股净资产'),
      roe_med: 'ROE 可算年份不足 3 期',
      stab: '可测同比不足 ' + G_MIN_PAIRS + ' 组',
      accel: span < 3 ? '跨度不足 3 年，拆不出两段'
        : (mid == null ? (ay - 2) + ' 年缺净利润' : '前段或近段增速算不出（基期非正）'),
      roe_trend: roeMed == null ? 'ROE 中位算不出' : '最新年报缺 ROE'
    };
    var items = [], sum = 0, ev = 0;
    G_ITEMS.forEach(function (it) {
      var v = raw[it.key];
      var sc = v == null ? null : Math.max(0, Math.min(1, (v - it.lo) / (it.hi - it.lo)));
      if (v != null) { ev++; sum += it.weight * sc; }
      items.push({ key: it.key, label: it.label, value: v, sc: sc, weight: it.weight, na: v == null });
    });
    // 可判项的「口径」用与「当前值」同一个格式化器：锚点写成 -0.05~0.15 时，读者看着
    // 同行的 28.83% 还得自己换算量纲。判不动那侧没值可对，保留说明缺了哪一列的文案。
    items.forEach(function (x) {
      var it = G_ITEMS.filter(function (k) { return k.key === x.key; })[0];
      var f = G_ANCHOR_FMT[x.key] || fmtNum;
      x.why = x.na ? naWhy[x.key]
        : (it.hi > it.lo ? '锚点 ' + f(it.lo) + '~' + f(it.hi)
          : '越少越好，' + f(it.lo) + '→' + f(it.hi));
    });
    return {
      na: false, total: ev ? Math.round(sum / G_SUM_W * 1000) / 10 : null,
      items: items,
      basis: '评分基准：' + ay + ' 年报（基期 ' + base + '，跨度 ' + span + ' 年）',
      eff: { evaluated: ev, missing: G_ITEMS.length - ev, na: 0 },
      note: '把「过去五年长得多快、长得稳不稳、回报率水平与走向」七项量纲不同的证据，各自夹到 0~1 '
        + '后按先验权重折成 0~100。分母固定为 100 权重，缺项只压低分数不重新归一——可评估项数并列在'
        + '抬头，7 项齐全与只剩 3 项算得出的两家同样拿 60 分时，前者才是真的高增长。'
    };
  }

  // 成长分项的「当前值」格式化：三条增速与两条 ROE 量都是 0~1 的小数，负增长年数是整数

  // 成长分项的「当前值」格式化：三条增速与两条 ROE 量都是 0~1 的小数，负增长年数是整数
  var G_VALUE_FMT = {
    np_g5: fmtPct, rev_g5: fmtPct, bps_g5: fmtPct, accel: fmtPct,
    roe_med: fmtPct, roe_trend: fmtPct, stab: fmtNum
  };
  // 「口径」列的锚点用同一套单位；负增长年数是整数刻度，其余沿用上面的百分数量格式化器

  // 「口径」列的锚点用同一套单位；负增长年数是整数刻度，其余沿用上面的百分数量格式化器
  var G_ANCHOR_FMT = { stab: fmtNum };
  Object.keys(G_VALUE_FMT).forEach(function (k) {
    if (!G_ANCHOR_FMT[k]) G_ANCHOR_FMT[k] = G_VALUE_FMT[k];
  });
  // 回测五分位切点（A 股面板 21,926 条「公司 × 信号年」观测）与该档实测的未来增速中位数、
  // 净利转负率。档位是按**回测当时**的分位切的，当期分布若整体漂移，占比会变而切点不动——
  // 这张表回答的是「这个分数在历史上接着长出多少」，不是「你排第几」。

  // 回测五分位切点（A 股面板 21,926 条「公司 × 信号年」观测）与该档实测的未来增速中位数、
  // 净利转负率。档位是按**回测当时**的分位切的，当期分布若整体漂移，占比会变而切点不动——
  // 这张表回答的是「这个分数在历史上接着长出多少」，不是「你排第几」。
  var G_BANDS = [
    { hi: 16.0, label: '档1 增长最弱', grade: 'bad', s2: -9.2, s3: -13.4, neg2: 45.2 },
    { hi: 28.3, label: '档2 偏低', grade: 'low', s2: -10.6, s3: -8.9, neg2: 28.7 },
    { hi: 47.7, label: '档3 中位', grade: 'mid', s2: -8.9, s3: -8.6, neg2: 23.6 },
    { hi: 69.0, label: '档4 偏高', grade: 'mid', s2: -4.2, s3: -2.9, neg2: 13.1 },
    { hi: Infinity, label: '档5 最强', grade: 'good', s2: -2.0, s3: 0.4, neg2: 8.7 }
  ];

  function gBandOf(total) {
    for (var i = 0; i < G_BANDS.length; i++) {
      if (total <= G_BANDS[i].hi) return G_BANDS[i];
    }
    return G_BANDS[G_BANDS.length - 1];
  }

  var V_ITEMS = [
    { key: 'edge_tbv', label: 'ln(有形账面价值 ÷ 市值)', weight: 30, lo: -1.5, hi: 0.5 },
    { key: 'ep', label: '盈利收益率（TTM 扣非 ÷ 市值）', weight: 25, lo: 0, hi: 0.10 },
    { key: 'cash_yld', label: '现金股息率（近 3 年均值）', weight: 10, lo: 0, hi: 0.05 },
    { key: 'roe_med5', label: 'ROE 近 5 年中位', weight: 14, lo: 0, hi: 0.20 },
    { key: 'debt_rev', label: '资产负债率', weight: 10, lo: 0.90, hi: 0.30 },
    { key: 'ocfnp', label: '净现比（近 5 年配对求和）', weight: 8, lo: 0.8, hi: 2.5 },
    { key: 'return_cash', label: '近 5 年派现年数占比', weight: 3, lo: 0, hi: 1 }
  ];

  var V_SUM_W = 100;        // Σ V_ITEMS.weight，固定分母

  var V_EDGE_FLOOR = -9;    // 有形账面价值 ≤0 时 ln 无定义：记成一个必然夹到 0 分档的下界

  function vNum(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }

  // V 的市值：本批次行情优先，没给到可用市值才退回深抓快照。0 与负数市值不是「便宜」，
  // 是行情行坏了，与缺值同处理。collector/scripts/scoring.py 的 _v_mcap 是同一条规则。

  // V 的市值：本批次行情优先，没给到可用市值才退回深抓快照。0 与负数市值不是「便宜」，
  // 是行情行坏了，与缺值同处理。collector/scripts/scoring.py 的 _v_mcap 是同一条规则。
  function vMcap(q) {
    var v = vNum((q || {}).market_cap);
    return (v != null && v > 0) ? v : null;
  }

  function valueScore(d) {
    d = d || {};
    var byYear = {};
    function slot(y) { return byYear[y] || (byYear[y] = {}); }
    function put(o, k, v) { var n = vNum(v); if (n != null) o[k] = n; }   // 同年后一行覆盖前一行，缺列留空
    function yearOf(s) { var y = Number(String(s || '').slice(0, 4)); return y || null; }
    annualRows(d.indicators || []).forEach(function (r) {
      var y = yearOf(r['报告期']);
      if (!y) return;
      var s = slot(y);
      put(s, 'net', r['净利润']); put(s, 'ded', r['扣非净利润']);
      put(s, 'roe', r['净资产收益率']); put(s, 'bps', r['每股净资产']);
      put(s, 'debt_r', r['资产负债率']);
    });
    annualBalanceRows(d.balance).forEach(function (r) {
      var y = yearOf(r['报告日']);
      if (!y) return;
      var s = slot(y);
      var eq = equityOf(r);
      if (eq != null) s.eq = eq;
      put(s, 'ta', r['资产总计']); put(s, 'tl', r['负债合计']);
      put(s, 'gw', r['商誉']); put(s, 'intang', r['无形资产']);
    });
    annualBalanceRows(d.cashflow).forEach(function (r) {
      var y = yearOf(r['报告日']);
      if (!y) return;
      put(slot(y), 'ocf', r['经营活动产生的现金流量净额']);
    });
    var years = Object.keys(byYear).map(Number).sort(function (a, b) { return a - b; });
    if (years.length < 3) {
      return { na: false, total: null, reason: '公开年报不足 3 期', items: [],
        eff: { evaluated: 0, missing: V_ITEMS.length, na: 0 } };
    }
    var ay = years[years.length - 1], win = years.slice(-5), cur = byYear[ay];
    var mcap = vMcap(d.batch_quote);
    if (mcap == null) mcap = vMcap(d.snapshot);
    // 派现年表：归属年 → 每 10 股现金红利合计，只数真给了钱的行（送股不派现不算回过钱）
    var divAmt = {}, divSeen = false;
    (d.dividends || []).forEach(function (r) {
      var y = yearOf(r.year), b = vNum(r.bonus_per_10);
      if (!y || y < 1990 || !(b > 0)) return;
      divAmt[y] = (divAmt[y] || 0) + b;
      divSeen = true;
    });
    // 隐含股本：归母权益 ÷ 每股净资产，与陷阱分同一招——「股本」列会被送转股放大。
    // 美股年报没有每股净资产那一行，所以它的股息率算不出（判不动），账面折扣照算。
    var sh = (cur.eq != null && cur.bps > 0) ? cur.eq / cur.bps : null;
    var tbv = cur.eq == null ? null : cur.eq - (cur.gw || 0) - (cur.intang || 0);
    // ep 的分子改滚动 TTM（扣非优先，缺则净利）：年报口径要等下一次年报才反映「年报之后
    // 中报塌方」（600866 实例）。最新指标行是年报时 TTM==年报值、分数不动，只有存在晚于
    // 最新年报的 interim 才分化。scoring.py 的 value_score 同一条规则（判据不动、只升级输入端）。
    var earn = ttmNetProfit(d.indicators || [], '扣非净利润');
    if (earn == null) earn = ttmNetProfit(d.indicators || [], '净利润');
    var raw = {};
    if (mcap != null) {
      if (tbv != null) raw.edge_tbv = tbv <= 0 ? V_EDGE_FLOOR : Math.log(tbv / mcap);
      if (earn != null) raw.ep = Math.max(0, earn) / mcap;
      if (sh != null) {
        var paid = 0;
        for (var y3 = ay - 3; y3 <= ay - 1; y3++) paid += divAmt[y3] || 0;
        raw.cash_yld = (paid / 10) * sh / 3 / mcap;
      }
    }
    var roes = [];
    win.forEach(function (y) { if (byYear[y].roe != null) roes.push(byYear[y].roe); });
    raw.roe_med5 = medOf(roes);
    raw.debt_rev = cur.debt_r != null ? cur.debt_r
      : ((cur.ta && cur.tl != null) ? cur.tl / cur.ta : null);   // 指标行缺该行时用负债/资产补
    var sn = 0, so = 0, np = 0;
    win.forEach(function (y) {
      var r = byYear[y];
      if (r.net != null && r.ocf != null) { np++; sn += r.net; so += r.ocf; }
    });
    if (np >= 3 && sn > 0) raw.ocfnp = so / sn;
    // A 股分红史全量可查，「近五年没派现」是公开事实（0 分档）；港美股只有查得派过才算得出，
    // 查不到分不清「没分」与「这侧的分红源没覆盖」，判不动。
    if (d.market === 'A' || divSeen) {
      var gave = 0;
      for (var y5 = ay - 5; y5 <= ay - 1; y5++) if (divAmt[y5]) gave++;
      raw.return_cash = Math.min(1, gave / 5);
    }
    var naWhy = {
      edge_tbv: mcap == null ? '快照取不到市值'
        : (cur.eq == null ? '最新年报缺归母权益（含键名变体）' : '有形账面价值算不出'),
      ep: mcap == null ? '快照取不到市值' : '最新年报缺净利润与扣非净利润',
      cash_yld: mcap == null ? '快照取不到市值'
        : (sh == null ? '每股净资产或股本取不到，反推不出派息总额' : '派现额算不出'),
      roe_med5: 'ROE 可算年份不足 3 期',
      debt_rev: '指标行缺资产负债率，且负债合计/资产总计也凑不出',
      ocfnp: np < 3 ? '净利润与经营现金流同年可算不足 3 期' : '五年净利合计非正，比值无意义',
      return_cash: '非 A 股且查无派现记录（这一侧的分红史不全量可查，没记录不等于没分过）'
    };
    var items = [], sum = 0, ev = 0;
    V_ITEMS.forEach(function (it) {
      var v = vNum(raw[it.key]);
      var sc = v == null ? null : Math.max(0, Math.min(1, (v - it.lo) / (it.hi - it.lo)));
      if (v != null) { ev++; sum += it.weight * sc; }
      items.push({ key: it.key, label: it.label, value: v, sc: sc,
        weight: it.weight, na: v == null });
    });
    items.forEach(function (x) {
      var it = V_ITEMS.filter(function (k) { return k.key === x.key; })[0];
      var f = V_ANCHOR_FMT[x.key] || fmtNum;
      x.why = x.na ? naWhy[x.key]
        : (it.hi > it.lo ? '锚点 ' + f(it.lo) + '~' + f(it.hi)
          : '越低越好，' + f(it.lo) + '→' + f(it.hi));
    });
    return {
      na: false, total: ev ? Math.round(sum / V_SUM_W * 1000) / 10 : null,
      items: items,
      basis: '评分基准：' + ay + ' 年报（窗口 ' + win[0] + '~' + ay + '）· 市值取行情快照',
      eff: { evaluated: ev, missing: V_ITEMS.length - ev, na: 0 },
      note: '把「相对账面有多便宜、相对价格赚回来多少、赚到的钱兑不兑得出经营现金流、权益结构稳不稳」'
        + '七项量纲不同的证据各自夹到 0~1 后按先验权重折成 0~100。权重是先验声明、不对这段历史拟合，'
        + '分母固定 100 权重所以缺项只压低分数——便宜那 65 分要吃到市值才算得出，只看总分不看'
        + '「可判 N/7 项」会把一家只算得动三项的公司读成低估值。'
    };
  }

  // 价值分项的「当前值」格式化：账面折扣是对数比值（无量纲），其余按各自量纲给单位

  // 价值分项的「当前值」格式化：账面折扣是对数比值（无量纲），其余按各自量纲给单位
  var V_VALUE_FMT = {
    edge_tbv: fmtNum, ep: fmtPct, cash_yld: fmtPct, roe_med5: fmtPct,
    debt_rev: fmtPct, ocfnp: fmtNum, return_cash: fmtPct
  };
  // edge_tbv 的锚点是对数不是百分数，return_cash 是年数占比；其余沿用当前值的格式化器

  // edge_tbv 的锚点是对数不是百分数，return_cash 是年数占比；其余沿用当前值的格式化器
  var V_ANCHOR_FMT = {
    edge_tbv: fmtNum, ocfnp: fmtNum,
    ep: fmtPct, cash_yld: fmtPct, roe_med5: fmtPct, debt_rev: fmtPct, return_cash: fmtPct
  };
  // 档位切点与两个率取自 v_validity 那份 A 股面板（2021~2023 信号年、16,373 条「公司 × 信号年」）
  // 的 V 总分五分位。⚠ 那张面板的价格三项是「今天的市值 × 当年的年报」，今天的市值里已经装着
  // 信号日之后的一切，所以这两个率不是预测——它们只回答「档位形状对不对」（同一批观测里，
  // 真正有预测效度的是不含价格那半边：C+D 块五分位对转亏率 25.5%→5.8%，见 §甲-2）。
  // 落差几乎全在档1 与其余各档之间：档2~档5 的转亏率 9.2%~11.3% 已不分上下，减值率仍拉得开。

  // 档位切点与两个率取自 v_validity 那份 A 股面板（2021~2023 信号年、16,373 条「公司 × 信号年」）
  // 的 V 总分五分位。⚠ 那张面板的价格三项是「今天的市值 × 当年的年报」，今天的市值里已经装着
  // 信号日之后的一切，所以这两个率不是预测——它们只回答「档位形状对不对」（同一批观测里，
  // 真正有预测效度的是不含价格那半边：C+D 块五分位对转亏率 25.5%→5.8%，见 §甲-2）。
  // 落差几乎全在档1 与其余各档之间：档2~档5 的转亏率 9.2%~11.3% 已不分上下，减值率仍拉得开。
  var V_BANDS = [
    { hi: 19.3, label: '档1 价值最低', grade: 'bad', loss: 24.2, imp5: 33.7 },
    { hi: 26.7, label: '档2 偏低', grade: 'low', loss: 11.3, imp5: 11.7 },
    { hi: 34.0, label: '档3 中位', grade: 'mid', loss: 9.2, imp5: 8.7 },
    { hi: 45.9, label: '档4 偏高', grade: 'mid', loss: 10.1, imp5: 7.2 },
    { hi: Infinity, label: '档5 价值最高', grade: 'good', loss: 7.9, imp5: 8.6 }
  ];

  function vBandOf(total) {
    for (var i = 0; i < V_BANDS.length; i++) {
      if (total <= V_BANDS[i].hi) return V_BANDS[i];
    }
    return V_BANDS[V_BANDS.length - 1];
  }

  var R_W_VALUE = 0.6, R_FRAUD_GATE = 40, R_TRAP_GATE = 20;

  function recommendScore(vTotal, gTotal, fraud, trap, dip) {
    // 陷阱线 2026-10-17 起标注不拦分：>20 的 R 照打、gate 记 'trap'（scoring.py 同步同改）。
    // 命中顺序恒为 fraud→trap→interim；组合里有拦截项时 R=None，'trap' 只作附记。
    var hit = [];
    if (fraud != null && fraud > R_FRAUD_GATE) hit.push('fraud');
    var trapHit = trap != null && trap > R_TRAP_GATE;
    if (trapHit) hit.push('trap');
    if (dip != null && dip <= INTERIM_DIP_GATE) hit.push('interim');
    if (hit.indexOf('fraud') >= 0 || hit.indexOf('interim') >= 0) return { total: null, gate: hit.join('+') };
    if (vTotal == null || gTotal == null) return { total: null, gate: 'nodata' };
    return { total: Math.round((R_W_VALUE * vTotal + (1 - R_W_VALUE) * gTotal) * 10) / 10,
             gate: trapHit ? 'trap' : 'pass' };
  }

  // 中报恶化 dip：最新 interim（Q1/H1/Q3）的扣非同比（缺则净利同比）。评分轴只吃年报，
  // 这里是「年报正常、之后中报变脸」盲区的探针（600866 星湖科技 2026H1 扣非 −93% 而 R 停在
  // 82.3 的实例）。只在最新一期不是年报时才有值；同比分母用 |基期|。
  // scoring.py 的 interim_dip_yoy 同一条规则。

  // 中报恶化 dip：最新 interim（Q1/H1/Q3）的扣非同比（缺则净利同比）。评分轴只吃年报，
  // 这里是「年报正常、之后中报变脸」盲区的探针（600866 星湖科技 2026H1 扣非 −93% 而 R 停在
  // 82.3 的实例）。只在最新一期不是年报时才有值；同比分母用 |基期|。
  // scoring.py 的 interim_dip_yoy 同一条规则。
  var INTERIM_DIP_NOTE = -0.30, INTERIM_DIP_GATE = -0.70;   // 门槛阈值来历：scripts/dip_validity.py

  function interimDipYoy(indicators) {
    var byDate = {};
    (indicators || []).forEach(function (r) {
      var p = String(r['报告期'] || '').slice(0, 10);
      if (p.length === 10) byDate[p] = r;
    });
    var keys = Object.keys(byDate);
    if (!keys.length) return null;
    var latest = keys.sort()[keys.length - 1];
    if (latest.slice(5, 7) === '12') return null;   // 最新一期就是年报：无更新消息
    var prevKey = (Number(latest.slice(0, 4)) - 1) + latest.slice(4);
    if (!byDate[prevKey]) return null;
    var cur = byDate[latest]['扣非净利润'], pv = byDate[prevKey]['扣非净利润'];
    if (cur == null || pv == null) {
      cur = byDate[latest]['净利润']; pv = byDate[prevKey]['净利润'];
    }
    if (cur == null || pv == null || pv === 0) return null;
    return (cur - pv) / Math.abs(pv);
  }

  // 切点与两率来自 r_validity 面板 R_full（含价格、前视）五分位——与 V_BANDS 同一条纪律：
  // 判决书在无价格版 R_q（门槛内五分位 → 其后转亏率 16.0%→2.4%、减值≥5% 26.8%→3.3%，单调、
  // 逐年 3/3），这张表只看形状。改切点要与列表页 R_BAND_CUTS 同步。

  // 切点与两率来自 r_validity 面板 R_full（含价格、前视）五分位——与 V_BANDS 同一条纪律：
  // 判决书在无价格版 R_q（门槛内五分位 → 其后转亏率 16.0%→2.4%、减值≥5% 26.8%→3.3%，单调、
  // 逐年 3/3），这张表只看形状。改切点要与列表页 R_BAND_CUTS 同步。
  var R_BANDS = [
    { hi: 24, label: '档1 最差', grade: 'bad', loss: 11.9, imp5: 24.4 },
    { hi: 36, label: '档2 偏低', grade: 'low', loss: 8.2, imp5: 6.7 },
    { hi: 46, label: '档3 中位', grade: 'mid', loss: 5.5, imp5: 5.4 },
    { hi: 56, label: '档4 偏高', grade: 'mid', loss: 3.3, imp5: 3.8 },
    { hi: Infinity, label: '档5 最高', grade: 'good', loss: 3.4, imp5: 6.0 }
  ];

  function rBandOf(total) {
    for (var i = 0; i < R_BANDS.length; i++) {
      if (total <= R_BANDS[i].hi) return R_BANDS[i];
    }
    return R_BANDS[R_BANDS.length - 1];
  }

  var R_GATE_TEXT = {
    fraud: '造假红旗分 > 40（报表内部的量化背离过线）',
    trap: '价值陷阱分 > 20（坏消息证据堆过「单点」档——2026-10-17 起标注不拦分：R 照打并挂 ⚠ 风险标注）',
    interim: '中报恶化：最新一期季报/半年报的扣非同比 ≤ −70%（回测转亏 lift 7.8×，见 dip_validity）',
    nodata: '门槛过了，但 V/G 至少一条判不动（公开年报不足 3 期）'
  };

  // ---- 管理层管理水平评分（融合 DEA 投入产出效率思想的 8 维百分制加权）----
  // 分数越高管理水平越好（与价值评分同向）；数据不足该项不计分不误伤。
  // 纯 DEA 相对效率依赖同行业样本且为黑盒无法逐项展示，故采用其“投入→产出”效率内核的透明加权替代。
  function managementAnalysis(d) {
    var annual = annualRows(d.indicators || []);
    var last = annual.length ? annual[annual.length - 1] : null;
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var lastYear = lastDate ? lastDate.slice(0, 4) : null;
    var incList = (d.income || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var cfList = (d.cashflow || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var lastInc = lastDate ? sheetRowByDate(incList, lastDate) : null;
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;

    var rev = last ? last['营业总收入'] : (lastInc ? lastInc['营业总收入'] : null);
    var sellExp = lastInc ? lastInc['销售费用'] : null;
    var admExp = lastInc ? lastInc['管理费用'] : null;
    var finExp = lastInc ? lastInc['财务费用'] : null;
    var assets = lastBa ? lastBa['资产总计'] : null;
    var ar = arOf(lastBa);
    var inv = lastBa ? lastBa['存货'] : null;
    // 资本回报改「近 5 年披露 ROE 中位，不足 3 期判不动」：单年披露值在薄/负权益处会
    // 炸到 ±几千 pp（见 valueScores 处同款注释，巴菲特项早已改中位），旧实现 52 家单年
    // >100% 的公司直接拿满 20/20。逐年先取「净资产收益率」、缺则「-摊薄」（scoring.py 同款）
    var roeList = annual.slice(-5).map(function (r) {
      return r['净资产收益率'] != null ? r['净资产收益率'] : r['净资产收益率-摊薄'];
    }).filter(function (v) { return v != null; });
    var roe = null;
    if (roeList.length >= 3) {
      var roeSv = roeList.slice().sort(function (a, b) { return a - b; });
      var roeMid = Math.floor(roeSv.length / 2);
      roe = roeSv.length % 2 ? roeSv[roeMid] : (roeSv[roeMid - 1] + roeSv[roeMid]) / 2;
    }
    var eps = last ? last['基本每股收益'] : null;

    // 1. 三费率（销售＋管理＋财务费用）÷营收，越低越好（费用纪律/代理成本控制）；
    // 三费科目全缺（港股/美股报表口径）时回退替代科目近似计算：美股“营业费用”（销售+管理+研发合计）、
    // 港股“销售及分销费用”，避免整维缺失，口径为近似值（与三费合计不完全可比）。
    // 只有财务费用时不算三费率：财务费用为净收益（利息收入>支出）会把费率压成负数，
    // 而本项“越低越好”，负值直接拿满 15 分（实测友邦保险 feeRatio=-0.0031 → 15/15），
    // 等于用一个与费用纪律无关的科目发满分。此时按三费全缺处理，交给替代科目兜底。
    var feeSum = (sellExp != null || admExp != null)
      ? (sellExp || 0) + (admExp || 0) + (finExp || 0) : null;
    var feeLabel = '费用纪律：三费率（销售＋管理＋财务费用）÷营收';
    if (feeSum == null && lastInc != null) {
      feeSum = lastInc['营业费用'] != null ? lastInc['营业费用'] : lastInc['销售及分销费用'];
      if (feeSum != null) feeLabel = '费用纪律：费用÷营收（营业费用/销售及分销费用近似口径）';
    }
    var feeRatio = (feeSum != null && rev != null && rev > 0) ? feeSum / rev : null;

    // 2. 总资产周转率 = 营收 ÷ 总资产（资产运营效率）
    var turnover = (rev != null && assets != null && assets > 0) ? rev / assets : null;

    // 4. 营收约 5 年 CAGR（成长质量；取不晚于 lastYear-5 的最近年报作基期，缺则用最早年报）
    var revCagr = null;
    if (annual.length >= 2 && lastYear != null) {
      var base = null;
      for (var i = annual.length - 2; i >= 0; i--) {
        var yy = Number(String(annual[i]['报告期']).slice(0, 4));
        if (yy <= Number(lastYear) - 5) { base = annual[i]; break; }
      }
      if (!base) base = annual[0];
      var span = Number(lastYear) - Number(String(base['报告期']).slice(0, 4));
      if (span > 0) revCagr = cagr(rev, base['营业总收入'], span);
    }

    // 5. 营运资金占用（应收＋存货）÷营收，越低越好（回款与库存管理）
    var wc = (ar != null && inv != null && rev != null && rev > 0) ? (ar + inv) / rev : null;

    // 6. 近 5 年累计净现比（累计经营现金流 ÷ 累计净利润，现金流质量）
    var sumNet = 0, sumOcf = 0, hit = false;
    annual.slice(-5).forEach(function (r) {
      var cfRow = sheetRowByDate(cfList, String(r['报告期']).slice(0, 10));
      var n = r['净利润'], o = cfRow ? cfRow['经营活动产生的现金流量净额'] : null;
      if (n != null && o != null) { sumNet += n; sumOcf += o; hit = true; }
    });
    var cashRatio = (hit && sumNet > 0) ? sumOcf / sumNet : null;

    // 7. 现金分红率 = 最近归属年每股派现合计 ÷ 最近年报每股收益（与 valueAnalysis 同口径）；
    // >150% 视为口径不可比置空。旧实现取「最近一条记录」，单次中期分红会把分红率砍到几分之一
    var payout = null;
    var perShareY = lastYear != null ? perShareDiv(d.dividends || [], Number(lastYear)) : null;
    if (perShareY != null && eps != null && eps > 0) {
      payout = perShareY / eps;
      if (payout > 1.5) payout = null;
    }

    // 第 8 项「治理诚信=造假分反向」已删（2026-09-28，与 scoring.py 同批）：与造假分列
    // 同一信号两面，两列并排会被误读为独立印证；诚信归造假分列与 gate，本分纯看经营效率
    var items = [
      it(feeLabel, fmtPct(feeRatio), '≤ 10%', 15, lerpScore(feeRatio, 0.10, 0.30, 15, 0)),
      it('资产运营：总资产周转率（营收÷总资产）', turnover == null ? '-' : fmtNum(turnover), '≥ 1.0', 10, lerpScore(turnover, 0.2, 1.0, 0, 10)),
      it('资本回报：净资产收益率（近5年中位）', fmtPct(roe), '≥ 15%', 20, lerpScore(roe, 0, 0.15, 0, 20)),
      it('成长质量：营收约5年CAGR', fmtPct(revCagr), '≥ 10%', 10, lerpScore(revCagr, 0, 0.10, 0, 10)),
      it('营运资金：（应收＋存货）÷营收', fmtPct(wc), '≤ 15%', 10, lerpScore(wc, 0.15, 0.45, 10, 0)),
      it('现金流质量：近5年累计净现比（经营现金流÷净利润）', cashRatio == null ? '-' : fmtNum(cashRatio), '≥ 1.0', 15, lerpScore(cashRatio, 0, 1.0, 0, 15)),
      it('股东回报：现金分红率（每股分红÷每股收益）', fmtPct(payout), '≥ 50%', 10, lerpScore(payout, 0, 0.50, 0, 10))
    ];
    // 归一化：本分越高越好，缺项若只是少几笔加分，数据不全的公司就被系统性压低
    var total = weightedTotal(items.map(function (x) { return [x.score, x.max]; }));
    return {
      title: '管理层管理水平 · 投入产出效率量化',
      basis: '评分基准：' + (lastYear || '-') + ' 年报（比率类按年报口径，成长/现金流按近5年累计，资本回报取近5年ROE中位，分红取归属年合计）',
      total: total == null ? null : Math.round(total * 10) / 10,
      items: items,
      note: '借鉴 DEA 数据包络分析的“投入→产出”效率思想，将管理层能力拆为 7 个可量化维度加权 0~100 分：费用纪律/资产周转/资本回报/成长质量/营运资金/现金流质量/股东回报，分数越高管理水平越好（「治理诚信」项 2026-09-28 删除——它与造假分列同源）。纯 DEA 相对效率依赖同行业大样本且为黑盒、无法逐项展示，故采用其效率内核的透明加权替代；总分按“可算出的项”归一，缺数据的项不会把公司整体压低。本分为量化参考，需结合公司治理结构、股权激励、管理层履历等定性信息综合判断。'
    };
  }

  // 管理层评分卡（与 scoreCard 同构，等级方向高分=好=绿；符合度列 = 得分/权重）

  // ---- 周期性行业判定 + 周期位置评分（0~100，分数越低越接近周期底部）----
  // 两阶段：①周期强度 0~100（净利波动40 + 深度下滑频率35 + 毛利率波动25），≥40 判为周期性；
  // ②周期性公司才打周期位置分（利润/毛利率/营收位置 + 同比动能 + 现金流 + 库存/资本开支周期 + 单季环比）。
  function cycleAnalysis(d) {
    var annual = annualRows(d.indicators || []);
    var cfList = (d.cashflow || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var last = annual.length ? annual[annual.length - 1] : null;
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var lastYear = lastDate ? Number(lastDate.slice(0, 4)) : null;

    // ---- 阶段一：周期强度判定（样本标准差，窗口取近 8 年年报）----
    var w8 = annual.slice(-8);
    var nets = w8.map(function (r) { return r['净利润']; }).filter(function (v) { return v != null; });
    var gms = w8.map(function (r) { return r['销售毛利率']; }).filter(function (v) { return v != null; });
    function sd(arr) {
      if (arr.length < 2) return null;
      var m = arr.reduce(function (s, x) { return s + x; }, 0) / arr.length;
      var v = arr.reduce(function (s, x) { return s + (x - m) * (x - m); }, 0) / (arr.length - 1);
      return Math.sqrt(v);
    }
    // 1a 净利变异系数 = 标准差 ÷ |均值|（均值取绝对值防近零放大；全亏取各年绝对值均值）
    var cvNet = null;
    if (nets.length >= 3) {
      var meanNet = nets.reduce(function (s, x) { return s + x; }, 0) / nets.length;
      var denom = Math.abs(meanNet);
      if (denom === 0) denom = nets.reduce(function (s, x) { return s + Math.abs(x); }, 0) / nets.length;
      if (denom > 0) cvNet = sd(nets) / denom;
    }
    // 1b 利润深度下滑频率：年度净利同比 ≤ -30% 的年数（同比自算，与报表口径一致）。
    // 窗口与 1a/1c 同为近 8 年（函数头声明的口径）：旧实现数全史，上市越久攒够 2 次
    // 深跌的机会越多——实测 145 家的「周期性」标签因此翻转（全数偏向更周期）。
    // 此处只在基期为正时计算：“下滑 30%”对盈利基数才有意义；若把亏损扩大也算进来，
    // 尚未盈利的生物医药/新经济公司会被误判为周期性行业（实测 166 家误判）。
    // 阶段二“利润动能”用 yoyOf（基期为负按 |基期|）——那里问的是“是否在离开底部”，亏损收窄正是信号。
    var drops = 0, hitDrop = false;
    for (var ci = Math.max(1, annual.length - 8); ci < annual.length; ci++) {
      var nCur = annual[ci]['净利润'], nPre = annual[ci - 1]['净利润'];
      var yoy = (nCur != null && nPre != null && nPre > 0) ? nCur / nPre - 1 : null;
      if (yoy != null) { hitDrop = true; if (yoy <= -0.3) drops++; }
    }
    // 1c 毛利率波动 = 年度毛利率标准差（价格驱动型周期行业毛利率大起大落）
    var gmSd = gms.length >= 3 ? sd(gms) : null;

    var cItems = [
      it('净利润波动：年度净利变异系数（标准差÷|均值|）', cvNet == null ? '-' : fmtNum(cvNet), '≥ 1.2 强周期', 40, lerpScore(cvNet, 0.3, 1.2, 0, 40)),
      it('利润深度下滑：年度净利同比≤-30% 的年数', hitDrop ? drops + ' 年' : '-', '≥ 2 年', 35, hitDrop ? lerpScore(drops, 0, 2, 0, 35) : null),
      it('毛利率波动：年度毛利率标准差', gmSd == null ? '-' : (gmSd * 100).toFixed(1) + ' pct', '≥ 10 pct', 25, lerpScore(gmSd, 0.03, 0.10, 0, 25))
    ];
    var cAvail = cItems.filter(function (x) { return x.score != null; });
    var cyc = cAvail.length ? Math.min(100, cAvail.reduce(function (s, x) { return s + x.score; }, 0)) : null;
    cyc = cyc == null ? null : Math.round(cyc * 10) / 10;
    var cyclical = cyc != null && cyc >= 40;

    if (!cyclical) {
      return {
        cyclical: false, cyclicalScore: cyc, cyclicalItems: cItems, total: null, items: [],
        title: '周期位置 · 周期性行业判定与底部概率量化',
        basis: cyc == null ? '样本不足：年报数据少于 3 期，无法判定周期性' :
          '周期强度 ' + fmtNum(cyc) + ' < 40，判定为非周期性/弱周期行业',
        note: '周期强度由近 8 年年报的净利润变异系数（40 分）+ 利润深度下滑频率（35 分）+ 毛利率波动（25 分）构成；≥ 40 判为周期性行业才进行周期位置打分。已知局限：判定基于约 8 年财务样本，近 8 年处于单边景气期的典型周期股（如上行期的资源股）波动特征不明显会被判为“非周期”，若窗口恰好覆盖单边行情也可能误判，需结合行业属性（如公用事业/消费/医药通常弱周期，钢铁/有色/化工/航运/造纸通常强周期）复核。'
      };
    }

    // ---- 阶段二：周期位置评分（分数越低越接近周期底部）----
    function pctOf(v, arr) {
      var vs = arr.filter(function (x) { return x != null; });
      if (v == null || vs.length < 2) return null;
      var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
      return mx === mn ? 0.5 : (v - mn) / (mx - mn);
    }
    var netLast = last ? last['净利润'] : null;
    var revLast = last ? last['营业总收入'] : null;
    var gmLast = last ? last['销售毛利率'] : null;
    // 2a/2c/2d 利润/毛利率/营收在近 8 年区间的位置（越接近最低越接近底部）
    var netPct = pctOf(netLast, nets);
    var gmPct = pctOf(gmLast, gms);
    var revs = w8.map(function (r) { return r['营业总收入']; });
    var revPct = pctOf(revLast, revs);
    // 2b 最新年报净利同比（深负=底部区域，过热=远离底部）
    // 必须用最新年报自身同比：上年净利缺失/为 0 时置空，不能回退到历史同比序列末位（那是数年前的值）
    var netYoy = annual.length >= 2 ? yoyOf(netLast, annual[annual.length - 2]['净利润']) : null;
    // 2e 最新年报净现比（底部常伴随现金流恶化）
    var lastCf = lastDate ? sheetRowByDate(cfList, lastDate) : null;
    var ocfLast = lastCf ? lastCf['经营活动产生的现金流量净额'] : null;
    var ncr = (netLast != null && ocfLast != null && netLast > 0) ? ocfLast / netLast : null;
    // 2f 存货同比（去库存 → 接近底部）
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    var prevDate = annual.length >= 2 ? String(annual[annual.length - 2]['报告期']).slice(0, 10) : null;
    var prevBa = prevDate ? sheetRowByDate(baList, prevDate) : null;
    var invNow = lastBa ? lastBa['存货'] : null;
    var invPrev = prevBa ? prevBa['存货'] : null;
    var invGrow = (invNow != null && invPrev != null && invPrev > 0) ? invNow / invPrev - 1 : null;
    // 2g 资本开支强度 = 当年购建支出 ÷ 近 3 年均值（收缩 → 供给出清接近底部）
    var CAPEX_K = '购建固定资产、无形资产和其他长期资产所支付的现金';
    var annualCf = cfList.filter(function (r) { return String(r['报告日'] || '').slice(5) === '12-31'; });
    var capexNow = lastCf ? lastCf[CAPEX_K] : null;
    var capexPrev = capexPrevOf(annualCf, lastCf, CAPEX_K);
    var capexRatio = null;
    if (capexNow != null && capexPrev.length >= 2) {
      var capexAvg = capexPrev.reduce(function (s, x) { return s + x; }, 0) / capexPrev.length;
      if (capexAvg > 0) capexRatio = capexNow / capexAvg;
    }
    // 2h 最新单季营收环比（仍在回落 → 未到底；环比回升 → 开始离开底部）
    var qRows = (d.indicators || []).filter(function (r) { return String(r['报告期'] || '').slice(5) !== '12-31'; })
      .sort(function (a, b) { return cmpKey(String(a['报告期']), String(b['报告期'])); });
    // 单季同比（2026-09-28 由环比改同比）：环比被季节差主导（白酒 Q1 备货 Q1 环比深负≠探底），
    // 同比对齐去年同季季节性相消；锚点放宽到 -30%/+10%（同比量级比环比宽）
    var qByDate = {};
    qRows.forEach(function (r) { qByDate[String(r['报告期'] || '').slice(0, 10)] = r['营业总收入_单季']; });
    var qoq = null;
    (function () {
      var dates = Object.keys(qByDate).sort();
      if (!dates.length) return;
      var qd = dates[dates.length - 1];
      var qp = (Number(qd.slice(0, 4)) - 1) + qd.slice(4);
      if (qByDate[qd] != null && qByDate[qp] != null && qByDate[qp] > 0) qoq = qByDate[qd] / qByDate[qp] - 1;
    })();

    var items = [
      it('利润位置：最新年报净利在近' + w8.length + '年区间的位置', netLast == null ? '-' : fmtMoney(netLast), '接近最低分→底部', 25, netPct == null ? null : netPct * 25),
      it('利润动能：最新年报净利同比', netYoy == null ? '-' : fmtPct(netYoy), '≤ -50% 底部', 15, lerpScore(netYoy, -0.50, 0.30, 0, 15)),
      it('毛利率位置：最新年报毛利率在近' + w8.length + '年区间的位置', gmLast == null ? '-' : fmtPct(gmLast), '接近最低分→底部', 15, gmPct == null ? null : gmPct * 15),
      it('营收位置：最新年报营收在近' + w8.length + '年区间的位置', revLast == null ? '-' : fmtMoney(revLast), '接近最低分→底部', 10, revPct == null ? null : revPct * 10),
      it('现金流压力：最新年报净现比（经营现金流÷净利润）', ncr == null ? '-' : fmtNum(ncr), '≤ 0 底部', 10, lerpScore(ncr, 0, 1.2, 0, 10)),
      it('库存周期：存货同比（去库存→接近底部）', invGrow == null ? '-' : fmtPct(invGrow), '≤ -10% 去库存', 10, lerpScore(invGrow, -0.10, 0.20, 0, 10)),
      it('资本开支周期：当年购建支出÷近3年均值（收缩→出清）', capexRatio == null ? '-' : fmtNum(capexRatio), '≤ 0.7 收缩', 10, lerpScore(capexRatio, 0.7, 1.3, 0, 10)),
      it('单季同比：最新单季营收同比（回升→离开底部）', qoq == null ? '-' : fmtPct(qoq), '≤ -30% 仍在探底', 10, lerpScore(qoq, -0.30, 0.10, 0, 10))
    ];
    // 归一化：本分越低越接近底部，缺项若只是少扣一笔，数据不全的公司就被判成“更接近底部”。
    // 顺带去掉旧代码的 Math.min(100, sum) 硬夹 —— 8 维权重合计 105，归一化后天然不越界
    var total = weightedTotal(items.map(function (x) { return [x.score, x.max]; }));
    return {
      cyclical: true, cyclicalScore: cyc, cyclicalItems: cItems,
      total: total == null ? null : Math.round(total * 10) / 10,
      items: items,
      title: '周期位置 · 周期性行业判定与底部概率量化',
      basis: '周期强度 ' + fmtNum(cyc) + '（≥ 40 判为周期性）；位置评分基准：' + (lastYear || '-') + ' 年报 + 最新单季',
      note: '两阶段量化：①周期强度（净利变异系数 40 + 深度下滑频率 35 + 毛利率波动 25，≥ 40 判为周期性，非周期性不打分）；②周期位置 0~100，分数越低越接近周期底部（利润/毛利率/营收处于历史低位、同比深负、现金流承压、去库存、资本开支收缩、单季仍在回落均为底部特征）。已知局限：位置分按“可算出的维度”归一，缺数据的维度既不加罚也不免罚，维度很少时该分只反映已测得的部分；基于约 8 年样本的相对位置，近 8 年单边景气期的典型周期股会被判为“非周期”；无历史市价分位数据故未纳入估值维度；周期位置低≠立即反转，需结合行业供需与产能数据确认，仅供研究参考。'
    };
  }

  // 周期位置等级（分数越低越接近底部=机会=绿，方向同造假分）与文案

  // 周期位置等级（分数越低越接近底部=机会=绿，方向同造假分）与文案
  function cycleGradeText(g) {
    return { good: '底部区域', mid: '磨底过渡', low: '周期中段', bad: '景气偏高', na: '不适用' }[g];
  }

  // 周期评分卡：非周期性公司显示判定依据表；周期性公司另加周期强度表 + 8 维位置表（低分=绿）

  // ---- 年度周期位置分回溯 + 趋势状态（上行/反转/筑底/下行）----
  // 逐年回溯：对每个年报年，以该年为窗口末尾取最近 8 年年报，用与 cycleAnalysis 阶段二相同的 8 维逻辑打分；
  // 单季环比逐年参与：历史年用该年自身单季营收环比，末年用全局最新单季环比（与当期总分口径对齐），故各年均为满 8 维、同口径可比。
  function cycleHistory(d) {
    var annual = annualRows(d.indicators || []);
    var cfList = (d.cashflow || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var CAPEX_K = '购建固定资产、无形资产和其他长期资产所支付的现金';
    var annualCf = cfList.filter(function (r) { return String(r['报告日'] || '').slice(5) === '12-31'; });
    var qRows = (d.indicators || []).filter(function (r) { return String(r['报告期'] || '').slice(5) !== '12-31'; })
      .sort(function (a, b) { return cmpKey(String(a['报告期']), String(b['报告期'])); });
    var qByDate = {};
    qRows.forEach(function (r) { qByDate[String(r['报告期'] || '').slice(0, 10)] = r['营业总收入_单季']; });
    var qoq = null;
    (function () {
      var dates = Object.keys(qByDate).sort();
      if (!dates.length) return;
      var qd = dates[dates.length - 1];
      var qp = (Number(qd.slice(0, 4)) - 1) + qd.slice(4);
      if (qByDate[qd] != null && qByDate[qp] != null && qByDate[qp] > 0) qoq = qByDate[qd] / qByDate[qp] - 1;
    })();
    // 逐年单季同比（与 cycleAnalysis 同批改）：历史年取该年最后一个单季 vs 去年同季
    function yearQoq(yr) {
      var dates = Object.keys(qByDate).filter(function (k) { return k.slice(0, 4) === String(yr); }).sort();
      if (!dates.length) return null;
      var qd = dates[dates.length - 1];
      var qp = (Number(qd.slice(0, 4)) - 1) + qd.slice(4);
      if (qByDate[qd] != null && qByDate[qp] != null && qByDate[qp] > 0) {
        return qByDate[qd] / qByDate[qp] - 1;
      }
      return null;
    }
    function pctOf(v, arr) {
      var vs = arr.filter(function (x) { return x != null; });
      if (v == null || vs.length < 2) return null;
      var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
      return mx === mn ? 0.5 : (v - mn) / (mx - mn);
    }
    var out = [];
    for (var i = 2; i < annual.length; i++) {  // 需至少 3 年窗口且同比可算（i≥2）
      var row = annual[i];
      var year = Number(String(row['报告期']).slice(0, 4));
      var win = annual.slice(Math.max(0, i - 7), i + 1);
      var nets = win.map(function (r) { return r['净利润']; });
      var gms = win.map(function (r) { return r['销售毛利率']; });
      var revs = win.map(function (r) { return r['营业总收入']; });
      var net = row['净利润'], prev = annual[i - 1]['净利润'];
      var yoy = yoyOf(net, prev);
      var date = String(row['报告期']).slice(0, 10);
      var cf = sheetRowByDate(cfList, date);
      var ba = sheetRowByDate(baList, date);
      var baPrev = sheetRowByDate(baList, String(annual[i - 1]['报告期']).slice(0, 10));
      var ocf = cf ? cf['经营活动产生的现金流量净额'] : null;
      var ncr = (net != null && ocf != null && net > 0) ? ocf / net : null;
      var invNow = ba ? ba['存货'] : null;
      var invPrev = baPrev ? baPrev['存货'] : null;
      var invGrow = (invNow != null && invPrev != null && invPrev > 0) ? invNow / invPrev - 1 : null;
      var capexNow = cf ? cf[CAPEX_K] : null;
      var capexPrev = capexPrevOf(annualCf, cf, CAPEX_K);
      var capexRatio = null;
      if (capexNow != null && capexPrev.length >= 2) {
        var capexAvg = capexPrev.reduce(function (s, x) { return s + x; }, 0) / capexPrev.length;
        if (capexAvg > 0) capexRatio = capexNow / capexAvg;
      }
      // 单季环比：末年用全局最新单季环比（与当期总分一致），历史年用该年自身单季环比（满 8 维）
      var qoqI = (i === annual.length - 1) ? qoq : yearQoq(year);
      var scores = [
        pctOf(net, nets) == null ? null : pctOf(net, nets) * 25,
        lerpScore(yoy, -0.50, 0.30, 0, 15),
        pctOf(row['销售毛利率'], gms) == null ? null : pctOf(row['销售毛利率'], gms) * 15,
        pctOf(row['营业总收入'], revs) == null ? null : pctOf(row['营业总收入'], revs) * 10,
        lerpScore(ncr, 0, 1.2, 0, 10),
        lerpScore(invGrow, -0.10, 0.20, 0, 10),
        lerpScore(capexRatio, 0.7, 1.3, 0, 10),
        lerpScore(qoqI, -0.30, 0.10, 0, 10)
      ];
      // 与阶段二同口径归一化：各年可用维度数不同，不归一就没法逐年比（cycleTrendOf 正是逐年比）
      var tot = weightedTotal(scores.map(function (s, j) { return [s, CYCLE_POS_W[j]]; }));
      var sc = tot == null ? null : Math.round(tot * 10) / 10;
      out.push({ year: year, score: sc });
    }
    return out;
  }

  // 趋势状态（基于逐年回溯分，最新年相对上一年）：
  // 反转=上一年还在底部区（≤30）且最新分明显回升；上行=持续回升；筑底=低位（≤40）横盘；下行=分数走低（基本面恶化）

  // 趋势状态（基于逐年回溯分，最新年相对上一年）：
  // 反转=上一年还在底部区（≤30）且最新分明显回升；上行=持续回升；筑底=低位（≤40）横盘；下行=分数走低（基本面恶化）
  function cycleTrendOf(hist) {
    var h = (hist || []).filter(function (x) { return x.score != null; });
    if (!h.length) return null;
    var cur = h[h.length - 1], prev = null;
    // 必须取“上一年”而非“上一条有分的年”：中间年缺分时两者跨年，会把两年前的分当成上一年
    for (var i = h.length - 2; i >= 0; i--) {
      if (h[i].year === cur.year - 1) { prev = h[i]; break; }
    }
    if (prev == null) return null;
    var d1 = cur.score - prev.score;
    if (d1 > 5) return prev.score <= 30 ? 'rev' : 'up';
    if (d1 < -5) return 'down';
    if (cur.score <= 40) return 'flat';
    return d1 >= 0 ? 'up' : 'down';
  }

  // 详情页历年周期位置分趋势图（仅周期性公司且 ≥2 个有效年份才显示；曲线下探=接近底部）

  // 类现金加权口径（与 scoring.py 一致）：分项键 → [展示名, 折算系数, 附注字段]
  // 附注拆得出「定期存款」与「受限货币资金」时，货币资金与其他流动资产都只按可用部分折算，
  // 故展示名跟着第三项字段是否命中变；附注为空的分项不参与折算也不展示。
  var NET_CASH_W = [
    ['avail', '货币资金', 1, 'restricted'],
    ['fin', '交易性金融资产', 1],
    ['notes', '应收票据', 0.4],
    ['otherNonDep', '其他流动资产', 0.3, 'termDeposit'],
    ['termDeposit', '定期存款', 1]
  ];

  // 净现金分子分项：dep（定期存款）与 rst（受限货币资金）来自财报附注，附注没命中的公司
  // 两个都是 null，avail / otherNonDep 就退回原科目本身，式子与旧口径逐位相同。
  // dep 还要夹在「其他流动资产」当期值以内：附注常比资产负债表早一期（三季报无附注），
  // 上一期的存款到这一期可能已到期，超过科目就按科目上限夹住、科目算不出就整项弃用，宁少不错。

  // 净现金分子分项：dep（定期存款）与 rst（受限货币资金）来自财报附注，附注没命中的公司
  // 两个都是 null，avail / otherNonDep 就退回原科目本身，式子与旧口径逐位相同。
  // dep 还要夹在「其他流动资产」当期值以内：附注常比资产负债表早一期（三季报无附注），
  // 上一期的存款到这一期可能已到期，超过科目就按科目上限夹住、科目算不出就整项弃用，宁少不错。
  function netCashParts(o) {
    var dep = o.dep;
    if (dep != null) dep = (o.other == null) ? null : Math.min(dep, o.other);
    return {
      avail: (o.cash != null && o.rst != null) ? Math.max(0, o.cash - o.rst) : o.cash,
      fin: o.fin,
      notes: o.notes,
      otherNonDep: (o.other != null && dep != null) ? Math.max(0, o.other - dep) : o.other,
      termDeposit: dep
    };
  }

  // 附注 → 报告日不晚于 day 的最近一份（附注只有 06-30 与 12-31 两个报告日，而最新一期
  // 资产负债常常是三季报，故按「上一次披露的构成」用）；返回 [报告日, 值] 或 null

  // 附注 → 报告日不晚于 day 的最近一份（附注只有 06-30 与 12-31 两个报告日，而最新一期
  // 资产负债常常是三季报，故按「上一次披露的构成」用）；返回 [报告日, 值] 或 null
  function noteLatest(notes, day) {
    if (!notes || typeof notes !== 'object' || !day) return null;
    var best = null;
    Object.keys(notes).forEach(function (k) {
      var v = notes[k];
      if (k.length < 10 || !v || typeof v !== 'object') return;
      var d10 = k.slice(0, 10);
      if (d10 <= day && (best == null || d10 > best[0])) best = [d10, v];
    });
    return best;
  }

  // 有息负债五桶的市场别名（2026-09 实测键名全集）：美股「长期负债(本期部分)」＝一年内到期
  // 长债、「资本租赁债务(流动)/(非流动)」＝融资租赁；港股「已发行债券」＝应付债券。缺这些
  // 别名时港美股净现金(扣有息)系统性偏高（MSFT 漏算 260 亿美元级）。桶内语义：canonical
  // 在则用之（434 家租赁负债与融资/资本租赁拆分键并存的行一律按 canonical 全额，避免双计），
  // 不在才取别名之和。scoring.py 的 DEBT_BUCKETS/_debt_key 同一条规则。

  // 有息负债五桶的市场别名（2026-09 实测键名全集）：美股「长期负债(本期部分)」＝一年内到期
  // 长债、「资本租赁债务(流动)/(非流动)」＝融资租赁；港股「已发行债券」＝应付债券。缺这些
  // 别名时港美股净现金(扣有息)系统性偏高（MSFT 漏算 260 亿美元级）。桶内语义：canonical
  // 在则用之（434 家租赁负债与融资/资本租赁拆分键并存的行一律按 canonical 全额，避免双计），
  // 不在才取别名之和。scoring.py 的 DEBT_BUCKETS/_debt_key 同一条规则。
  var DEBT_BUCKETS = [
    ['短期借款', ['短期借款']],
    ['一年内到期的非流动负债', ['一年内到期的非流动负债', '长期负债(本期部分)']],
    ['长期借款', ['长期借款', '长期债务']],
    ['应付债券', ['应付债券', '已发行债券']],
    ['租赁负债', ['租赁负债', '融资租赁负债(流动)', '融资租赁负债(非流动)', '资本租赁债务(流动)', '资本租赁债务(非流动)']]
  ];

  function debtKey(row, bucket) {
    if (!row) return null;
    if (row[bucket[0]] != null) return row[bucket[0]];
    var total = 0, hit = false;
    bucket[1].forEach(function (k) {
      if (row[k] != null) { total += row[k]; hit = true; }
    });
    return hit ? total : null;
  }

  // 有息负债全口径：五桶别名读取后合计，缺桶当 0；行缺 → null。scoring.py 的 _int_debt 同一条规则

  // 有息负债全口径：五桶别名读取后合计，缺桶当 0；行缺 → null。scoring.py 的 _int_debt 同一条规则
  function intDebtOf(row) {
    if (!row) return null;
    var v = sum(DEBT_BUCKETS.map(function (b) { return debtKey(row, b); }));
    return v == null ? 0 : v;
  }

  // 加权类现金：可用货币资金×1.0 ＋ 交易性金融资产×1.0 ＋ 应收票据×0.4
  // ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0（附注闭合才采信、存款夹在科目内、
  // 受限按 0 折）。行缺或货币资金缺 → null；其余科目缺按 0 折入。
  // priceReferences（最新一期）与 valueScores 格攻净现金（年报行）共用这一条折算。
  // scoring.py 的 _weighted_cash 同一条规则。

  // 加权类现金：可用货币资金×1.0 ＋ 交易性金融资产×1.0 ＋ 应收票据×0.4
  // ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0（附注闭合才采信、存款夹在科目内、
  // 受限按 0 折）。行缺或货币资金缺 → null；其余科目缺按 0 折入。
  // priceReferences（最新一期）与 valueScores 格攻净现金（年报行）共用这一条折算。
  // scoring.py 的 _weighted_cash 同一条规则。
  function weightedCashOf(baRow, note) {
    if (!baRow || baRow['货币资金'] == null) return null;
    var p = netCashParts({
      cash: baRow['货币资金'], fin: baRow['交易性金融资产'],
      notes: baRow['应收票据'], other: baRow['其他流动资产'],
      dep: note ? note.termDeposit : null, rst: note ? note.restrictedCash : null
    });
    function wgt(v, k) { return v != null ? v * k : 0; }
    return wgt(p.avail, 1) + wgt(p.fin, 1) + wgt(p.notes, 0.4) + wgt(p.otherNonDep, 0.3) + wgt(p.termDeposit, 1);
  }

  // 净现金/市值 代入计算式（多行文本，桌面 title 与移动端点击浮层共用）；无明细返回空串

  // 净现金/市值 代入计算式（多行文本，桌面 title 与移动端点击浮层共用）；无明细返回空串
  function netCashFormula(refs) {
    var c = refs && refs.netCashCalc;
    if (!c || !c.mcap) return '';
    var p = netCashParts({ cash: c.cash, fin: c.fin, notes: c.notes,
                           other: c.otherCA, dep: c.termDeposit, rst: c.restricted });
    var wSum = 0, items = [];
    NET_CASH_W.forEach(function (it) {
      var v = p[it[0]];
      if (v == null) return;               // 缺失科目不参与折算也不展示
      wSum += v * it[2];
      var label = it[1] + (it[3] && c[it[3]] != null ? '(' + (it[3] === 'restricted' ? '扣受限' : '非存款') + ')' : '');
      items.push(label + fmtMoney(v) + '×' + it[2]);
    });
    var lines = [
      '净现金/市值 ＝（' + items.join(' ＋ ') + ' − 负债合计' + fmtMoney(c.tl) + '）÷ 总市值' + fmtMoney(c.mcap),
      '＝ (' + fmtMoney(wSum) + ' − ' + fmtMoney(c.tl) + ') ÷ ' + fmtMoney(c.mcap) +
        ' ＝ ' + ((wSum - c.tl) / c.mcap * 100).toFixed(1) + '%'
    ];
    if (c.report) lines.push('资产负债表：' + c.report);
    // 分项标签带了「(扣受限)」「(非存款)」，这里给出拆出的那个数与它是哪一期披露的
    var split = [];
    if (c.termDeposit != null) split.push('定期存款' + fmtMoney(c.termDeposit) + '（自其他流动资产拆出）');
    if (c.restricted != null) split.push('受限货币资金' + fmtMoney(c.restricted) + '（自货币资金扣出）');
    if (split.length) {
      lines.push('附注拆分：' + split.join(' ＋ ') + (c.noteReport ? '，附注 ' + c.noteReport : ''));
    }
    return lines.join('\n');
  }

  // 巴菲特合理市盈率 = 净利5年CAGR×100，夹在 [8, 25]；无数据取 15

  // 两派买入/保守卖出/公允卖出价格参考（对应 scoring.py price_references）
  // netCashRatio = 净现金/市值（最近一期财报 加权类现金−负债合计 ÷ 快照总市值）
  function priceReferences(d, va) {
    var s = d.snapshot || {};
    var price0 = s.price, mcap0 = s.market_cap, pe0 = s.pe_ttm, pb0 = s.pb;
    var none = { buy: null, sellCons: null, sellFair: null };
    if (price0 == null || price0 <= 0) {
      return { netCashRatio: null, wCash: null, intDebt: null, netCashW: null, netCashB: null, netCashCalc: null, grahamDef: none, schloss: none };
    }
    // ---- 基础量（最新年报资产负债表）----
    var annual = annualRows(d.indicators || []);
    var last = annual[annual.length - 1];
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    // 批次 1（2026-09）：账面口径的资产负债科目取最新一期（含季报），缺则回退评分基准
    // 年报行——与施洛斯账面锚（最新期每股净资产）对齐。scoring.py 的 price_references 同一条规则。
    var latestBaPR = baList.length ? baList[baList.length - 1] : null;
    var lastEq = equityOf(latestBaPR);
    if (lastEq == null) lastEq = equityOf(lastBa);
    // 每股净资产优先用指标字段（数据源按财报算好、随财报更新，与实时价无关），
    // 避免快照 pb/pe 舍入与 mcap 滞后导致参考价随行情漂移（财务无变化时参考价应不变）
    var bps = latestField(d.indicators, '每股净资产');
    // 股本优先用财报实收资本（最新年报），快照 mcap/price 会随实时价抖动（快照舍入/滞后）
    var shares = shareCount(d.balance, mcap0 != null ? mcap0 / price0 : null, bps);
    if (bps == null && lastEq != null && shares) bps = lastEq / shares;
    if (bps == null && pb0 != null && pb0 > 0) bps = price0 / pb0;
    var epsTtm = epsTtmField(d.indicators);
    if (epsTtm == null) {
      var ttmNet = ttmNetProfit(d.indicators);
      if (ttmNet != null && shares) epsTtm = ttmNet / shares;
    }
    if (epsTtm == null && pe0 != null && pe0 > 0) epsTtm = price0 / pe0;
    // ---- EPS 锚可信度：坏锚会让收益派三档价一起错，还会霸榜「买入性价比」排序 ----
    var isAnnual = latestPeriodIsAnnual(d.indicators);
    // ① 符号相反且同为 TTM 口径 → 至少一边错。最新报告期是年报时 epsTtmField 直接
    // 返回上一财年 EPS，与 TTM 快照本就不同窗口，符号相反是真实的一次性减值/转折
    // （实测 65 家：GILD 上一财年 +6.84 对 TTM 隐含 -2.65、COIN +4.85 对 -3.92），保留。
    // 中间期时自算值就是 cur+上年年报-上年同期 的真 TTM，实测 37 家全是累计相减在零附近
    // 的残差（招商蛇口 0.06+0.08-0.14=0 对 pe0=+565、山东墨龙 0.0001 对 pe0=-2558）。
    if (epsTtm != null && pe0 != null && pe0 !== 0 && !isAnnual
        && (epsTtm > 0) !== (pe0 > 0)) {
      epsTtm = null;
    }
    // ② 隐含 PE（现价/EPS）低于 EPS_PE_MIN 倍：一家持续经营的公司不可能长期只值自己三年
    // 利润，市场按这个价位报价就是在说这笔盈利不可重复（下限 1→3 倍多拦的 3 家见常量注释）。
    // 旧值 1 倍时只拦得住金科股份一家单位/口径错（快照 PE 与自算 EPS 同给 0.36）。
    if (epsTtm != null && epsTtm > 0 && price0 / epsTtm < EPS_PE_MIN) epsTtm = null;
    // ③ 量级背离：同 TTM 口径差 3 倍以上、年报窗口差 10 倍以上（实测 7 + 8 家）。
    // 年报窗口放宽是因为真实业绩可以一年翻几倍，但 10 倍以上的背离全是坏锚
    // （和黄医药 buy 28.91 对现价 19.14、BKNG 隐含 2497.8 倍、中国中冶 0.03 倍）。
    if (epsTtm != null && epsTtm > 0 && pe0 != null && pe0 > 0) {
      var mag = isAnnual ? EPS_MAG_ANNUAL : EPS_MAG_TTM;
      if (!((1 / mag) <= (price0 / epsTtm) / pe0 && (price0 / epsTtm) / pe0 <= mag)) epsTtm = null;
    }
    // ④ 经常性收益：报告利润的大头来自非经常项目时，按扣非后的规模重定锚。实测 4655 家有收益锚
    // 的公司里 297 家被压缩、3 家归零（安达科技、地纬智能、张裕Ａ——扣非转负，报表盈利靠处置与
    // 补助撑起）。只压缩不放大：扣非高于报告时维持报告口径，让锚停在偏保守一侧。
    // 抓不到的两类写在常量注释里：港美股不披露扣非，券商保险的公允价值变动损益不计入非经常。
    if (epsTtm != null && epsTtm > 0) {
      var ttmNet0 = ttmNetProfit(d.indicators || []);
      var adjNet = ttmNetProfit(d.indicators || [], '扣非净利润');
      if (ttmNet0 != null && adjNet != null && ttmNet0 > 0 && adjNet < RECURRING_MIN_RATIO * ttmNet0) {
        epsTtm = adjNet > 0 ? epsTtm * adjNet / ttmNet0 : null;
      }
    }
    // 净现金/市值：最近一期财报（加权类现金 − 负债合计）÷ 快照总市值；
    // 类现金保守折算：可用货币资金×1.0 ＋ 交易性金融资产×1.0 ＋ 应收票据×0.4
    //                ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0；
    // 定期存款与受限货币资金来自财报附注（PDF 解析），附注缺失时两者为空、式子退回旧口径；
    // 分子随财报更新（含季报），分母随行情快照，缺失科目按 0 折入
    var lastBaAll = baList[baList.length - 1];
    function gv(key) {
      var v = lastBaAll ? lastBaAll[key] : null;
      return (typeof v === 'number') ? v : null;
    }
    var cashV = gv('货币资金');
    var finV = gv('交易性金融资产');
    var notesV = gv('应收票据');
    var otherV = gv('其他流动资产');
    var tlLatest = gv('负债合计');
    var baDay = String((lastBaAll || {})['报告日'] || '').slice(0, 10);
    var noteFound = lastBaAll ? noteLatest(d.notes, baDay) : null;
    var noteVal = noteFound ? noteFound[1] : {};
    var depV = (typeof noteVal.termDeposit === 'number') ? noteVal.termDeposit : null;
    var rstV = (typeof noteVal.restrictedCash === 'number') ? noteVal.restrictedCash : null;
    var ncParts = netCashParts({ cash: cashV, fin: finV, notes: notesV, other: otherV, dep: depV, rst: rstV });
    function wgt(v, k) { return v != null ? v * k : 0; }
    // 货币资金缺失（银行/外资口径行）→ null，与 scoring.py _weighted_cash 的锚点一致
    var weightedCash = (cashV == null) ? null
      : wgt(ncParts.avail, 1) + wgt(ncParts.fin, 1) + wgt(ncParts.notes, 0.4) + wgt(ncParts.otherNonDep, 0.3) + wgt(ncParts.termDeposit, 1);
    // 净现金三件套（列表三列用，本币）：加权类现金、有息负债、净现金=加权−有息。
    // 与 netCashRatio（减全部负债的宽口径比值）并存，口径差见说明书 §2.1。
    var intDebtLatest = lastBaAll ? intDebtOf(lastBaAll) : null;
    var hasCore = cashV != null && tlLatest != null && mcap0;
    var netCashRatio = hasCore ? (weightedCash - tlLatest) / mcap0 : null;
    var netCashCalc = hasCore ? {
      cash: cashV, fin: finV, notes: notesV, otherCA: otherV,
      tl: tlLatest, mcap: mcap0,
      report: baDay || null,
      termDeposit: ncParts.termDeposit, restricted: rstV,
      noteReport: noteFound ? noteFound[0] : null
    } : null;
    // 低于一分钱（含负值与浮点零渣）的参考价一律视为无
    function ref(v) { return (v != null && v >= MIN_PRICE_REF) ? v : null; }
    // 资产派卖价只在估值射程内给出：现价已在 anchor 的 SELL_BAND 倍之外，说明该派的资产口径
    // 对这只股票没有「该卖」的意见（它从来不是这只股票的估值方法），留空。
    // 现价无快照的情形已在函数开头整体返回空，这里不必再判。
    function band(anchor) { return (anchor == null || price0 <= SELL_BAND * anchor) ? anchor : null; }
    var sRef = ref(bps);
    // TTM 每股亏损（≤0）时基于 EPS 的估值锚无意义，锚位与买入价一并置空（避免负价/误导价）
    var epsOk = epsTtm != null && epsTtm > 0;
    var gDCons = epsOk ? ref(G_D_PE_FULL * epsTtm) : null;
    var sCons = band(sRef);
    // 公允卖价跟着保守卖价同生同灭：公允是保守的 ≥1.3 倍，保守过了一分钱下限
    // 公允必然也过；但若各自独立套下限，会在保守差一点、公允刚过点时只留半档，
    // 而卖点筛选要求「同时 ≥ 保守与公允」，半档等于把这家公司永久排除。
    // 买点不挂卖点：锚为空才没有买点；资产派「出射程」只抹掉两档卖价，买点作为目标价照旧给出。
    return {
      netCashRatio: netCashRatio,
      wCash: weightedCash,
      intDebt: intDebtLatest,
      netCashW: (weightedCash != null) ? weightedCash - intDebtLatest : null,
      // 宽口径金额 = 加权类现金 − 负债合计（列表「净现金(减全部负债)」列用，本币亿）
      netCashB: (weightedCash != null && tlLatest != null) ? weightedCash - tlLatest : null,
      netCashCalc: netCashCalc,
      grahamDef: {
        buy: (gDCons != null) ? ref(BUY_MARGIN * gDCons) : null,
        sellCons: gDCons,
        sellFair: (gDCons != null) ? 20 * epsTtm : null
      },
      schloss: {
        buy: (sRef != null) ? ref(S_PB_FULL * sRef) : null,
        sellCons: sCons,
        sellFair: (sCons != null) ? 1.5 * sCons : null
      }
    };
  }

  // 渲染两派评分卡（格防御、施洛斯）

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '-';
    return (v * 100).toFixed(2) + '%';
  }

  function scoreCalcText(d, which) {
    var L = [];
    function pushStd(items) {
      (items || []).forEach(function (x) {
        if (x.na) { L.push('· ' + x.std + '：报表无此科目（不适用），不计分也不计入有效满分'); return; }
        L.push('· ' + x.std + '：' + (x.val == null ? '-' : x.val) + '（阈值 ' + x.thr + '）→ ' +
          (x.score == null ? '缺数据，不计分' : fmtNum(x.score) + ' / ' + x.max + ' 分'));
      });
    }
    function bandLabel(bands, total) {
      for (var i = 0; i < bands.length; i++) if (total <= bands[i].hi) return bands[i].label;
      return bands[bands.length - 1].label;
    }

    if (which === 'fraud' || which === 'mgmt') {
      var fm = which === 'fraud' ? fraudAnalysis(d) : managementAnalysis(d);
      L.push((which === 'fraud' ? '造假分（红旗加权，越高越可疑）' : '管理分（越高越好）') +
        '：' + (fm.total == null ? '-' : fmtNum(fm.total)));
      L.push(fm.basis);
      pushStd(fm.items);
      return L.join('\n');
    }

    if (which === 'cycle') {
      var ca = cycleAnalysis(d);
      L.push('阶段一 · 周期强度 ' + (ca.cyclicalScore == null ? '-' : fmtNum(ca.cyclicalScore)) +
        '（≥ 40 判为周期性；非周期性不打位置分）');
      pushStd(ca.cyclicalItems);
      if (ca.cyclical) {
        L.push('阶段二 · 周期位置：' + (ca.total == null ? '-' : fmtNum(ca.total)) + '（越低越接近周期底部）');
        L.push(ca.basis);
        pushStd(ca.items);
      }
      return L.join('\n');
    }

    if (which === 'trap') {
      var tp = trapScore(d);
      L.push('价值陷阱分 T：' + (tp.total == null ? (tp.na ? '不适用' : '判不动') : fmtNum(tp.total) + '（' + tp.band.label + '）'));
      if (tp.reason) { L.push(tp.reason); return L.join('\n'); }
      L.push(tp.basis + '　证据合计 C = ' + fmtNum(tp.c) + '，可判 ' + tp.eff.evaluated + '/7 项（缺项按「没证据」计 0，不给分也不加罚）');
      (tp.items || []).forEach(function (x) {
        var tf = TRAP_VALUE_FMT[x.key] || fmtNum;
        var st = x.bad === null ? '判不动计 0' : (x.bad ? '亮灯计 ' + x.weight : '未触发计 0');
        L.push('· ' + x.label + '：' + (x.value == null ? '-' : tf(x.value)) + '，' + st + '（' + x.why + '）');
      });
      return L.join('\n');
    }

    if (which === 'growth' || which === 'value') {
      var gv = which === 'growth' ? growthScore(d) : valueScore(d);
      var gvf = which === 'growth' ? G_VALUE_FMT : V_VALUE_FMT;
      var gvb = which === 'growth' ? G_BANDS : V_BANDS;
      var nm = which === 'growth' ? '成长综合分 G' : '价值综合分 V';
      if (gv.total == null && gv.reason) { L.push(nm + '：算不出（' + gv.reason + '，七项全部判不动）'); return L.join('\n'); }
      L.push(nm + '：' + (gv.total == null ? '-' : fmtNum(gv.total) + '（' + bandLabel(gvb, gv.total) + '）'));
      L.push(gv.basis + '　可判 ' + gv.eff.evaluated + '/7 项（分母固定 100 权重，缺项只压低分数、不重新归一）');
      (gv.items || []).forEach(function (x) {
        var gf = gvf[x.key] || fmtNum;
        if (x.na || x.sc == null) {
          L.push('· ' + x.label + '：判不动，不计分' + (x.why ? '（' + x.why + '）' : ''));
          return;
        }
        L.push('· ' + x.label + '：' + (x.value == null ? '-' : gf(x.value)) + ' → 符合 ' +
          Math.round(x.sc * 100) + '% × 权重 ' + x.weight + ' = ' + fmtNum(x.weight * x.sc) + ' 分' +
          (x.why ? '（' + x.why + '）' : ''));
      });
      return L.join('\n');
    }

    if (which === 'recommend') {
      var rv = valueScore(d), rg = growthScore(d);
      var rf = fraudAnalysis(d).total, rt = trapScore(d);
      var dip = interimDipYoy(d.indicators || []);
      var rRec = recommendScore(rv.total, rg.total, rf, rt.total, dip);
      L.push('综合推荐分 R = 0.6 × V + 0.4 × G（门槛外的合成，权重先验不拟合）');
      L.push('输入：V ' + (rv.total == null ? '-' : fmtNum(rv.total)) + ' · G ' + (rg.total == null ? '-' : fmtNum(rg.total)) +
        ' · 造假 ' + (rf == null ? '-' : fmtNum(rf)) + ' · 陷阱 ' + (rt.total == null ? (rt.na ? '不适用' : '判不动') : fmtNum(rt.total)) +
        ' · 中报 dip ' + (dip == null ? '判不动' : (dip * 100).toFixed(0) + '%'));
      L.push('拦截门槛：造假 ≤ 40 ' + (rf == null ? '（判不动放行）' : rf <= R_FRAUD_GATE ? '✓' : '✗') +
        ' · 中报 dip > −70% ' + (dip == null ? '（判不动放行）' : dip > INTERIM_DIP_GATE ? '✓' : '✗'));
      L.push('陷阱标注线 ≤ 20：' + (rt.total == null ? '（不适用）' : rt.total <= R_TRAP_GATE
        ? '✓ 干净' : '⚠ 命中——标注不拦分，R 照打并挂风险标注'));
      if (rRec.total == null) {
        L.push('结论：无 R（' + (R_GATE_TEXT[rRec.gate] || rRec.gate) + '）——门槛外是「不过资格线」，不是 0 分');
      } else {
        L.push('＝ 0.6×' + fmtNum(rv.total) + ' + 0.4×' + fmtNum(rg.total) + ' = ' + fmtNum(rRec.total) +
          '（' + bandLabel(R_BANDS, rRec.total) + '）');
        if (rRec.gate === 'trap') {
          L.push('⚠ 价值陷阱标注线命中（陷阱分 ' + fmtNum(rt.total) + (rt.band ? ' · ' + rt.band.label : '') +
            '）：便宜可能是周期底部或价值陷阱，读分时把陷阱分一节的档位与发生率一并看。');
        }
      }
      if (dip != null && dip <= INTERIM_DIP_NOTE) {
        L.push('⚠ 中报恶化：最新一期扣非（缺则净利）同比 ' + (dip * 100).toFixed(0) + '%。评分轴只吃年报，年报之后的恶化不进任何分。');
      }
      return L.join('\n');
    }

    // 四派：sc.basis 共享一条，逐派 eff（缺失/不适用）注在基准行尾
    var va = valueAnalysis(d);
    var sc = valueScores(d, va);
    var one = sc[which];
    if (!one) return '';
    var eff = one.eff || {};
    L.push(one.title + '：' + (one.total == null ? '-' : fmtNum(one.total) + ' / ' + (eff.max || 100)));
    L.push(sc.basis + ((eff.miss || eff.na) ? '；⚠ ' +
      [eff.miss ? eff.miss + ' 项数据缺失未计分' : '', eff.na ? eff.na + ' 项报表无此科目（不适用）' : '']
        .filter(Boolean).join('，') + '，按有效满分 ' + eff.max + '/100 折算' : ''));
    pushStd(one.items);
    return L.join('\n');
  }

  /* ---------------- ST/退市风险预警（scoring.py _st_risk 的 JS 侧，同一条规则） ----------------
   * 徽标与筛选说明用的现算入口（列表行上的 st_risk/st_flags 是入库版，同一个函数在
   * refresh 时算的）。旗与等级的来历见 scoring.py 对应注释块；改任一边先改完两边再跑比对。
   */

  var ST_FIN_KW = ['金融', '银行', '保险', '证券', '货币金融', '资本市场'];

  function stBoardLine(code) {
    if (code.slice(0, 3) === '688' || code.slice(0, 3) === '689' || code.slice(0, 2) === '30') return 1e8;
    if (['43', '82', '83', '87', '88', '92'].indexOf(code.slice(0, 2)) >= 0) return 5e7;
    return 3e8;
  }

  function stRiskOf(d) {
    if ((d.market || 'A') !== 'A') return { risk: null, flags: '' };
    var annual = annualRows(d.indicators || []);
    if (!annual.length) return { risk: null, flags: '' };
    var last = annual[annual.length - 1];
    var profs = [last['净利润'], last['扣非净利润']].filter(function (x) { return x != null; });
    var prof = profs.length ? Math.min.apply(null, profs) : null;
    var rev = last['营业总收入'];
    var info = d.info || {};
    var code = String(d.code || '');
    var flags = [], exempt = false;
    if (prof != null && prof < 0 && rev != null) {
      var line = stBoardLine(code);
      if (rev < line) {
        // 豁免窗内的双创未盈利：旗名带 _ex 后缀、等级降关注（与 scoring._st_risk 同款）
        var ex = '';
        if (code.slice(0, 3) === '688' || code.slice(0, 3) === '689' || code.slice(0, 2) === '30') {
          var ly = parseInt(String(info['上市日期'] || '').slice(0, 4), 10);
          if (ly && parseInt(String(last['报告期'] || '').slice(0, 4), 10) - ly < 4) ex = '_ex';
        }
        flags.push('loss_rev' + ex);
        exempt = !!ex;
      } else if (rev < 2 * line) {
        flags.push('loss_rev_near');
      }
    }
    var dr = last['资产负债率'];
    var ind = String(info['行业'] || '');
    if (dr != null && dr > 0.9 && !ST_FIN_KW.some(function (k) { return ind.indexOf(k) >= 0; })) {
      flags.push('debt90');
    }
    // 现价：本批行情优先（refresh 注入 batch_quote，与 V 的市值同一份），退回深抓快照
    var price = (d.batch_quote || {}).price;
    if (price == null) price = (d.snapshot || {}).price;
    if (price != null && price > 0 && price <= 1.2) flags.push('price_low');
    if (!flags.length) return { risk: 0, flags: '' };
    var hit = flags.indexOf('loss_rev') >= 0 || flags.indexOf('loss_rev_ex') >= 0;
    return { risk: (hit && !exempt) ? 2 : 1, flags: flags.join(',') };
  }

  /* ---------------- 商业模式特征（bm 标签唯一 JS 实现） ----------------
   * 与 scoring.compute_scores 同一规则：轻资产自供 light = 3 年合计 capex÷经营现金流
   * ≤0.6（窗口任一年缺数退单年口径）；定价权 pricing = 毛利率较 5 年前年报降幅 ≥−3pp；
   * 原料缺失 → null（判不动）。zygc 芯片与 _score_check parity 都从这里取——
   * 2026-09-30 收敛前 parity 校验的是校验器内联抄本，生产芯片那份零校验。
   * ratio/calibre/gmDelta 供芯片 tip 文案，parity 不比对。 */
  function bmFeatures(d) {
    var cfList = (d.cashflow || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var annual = annualRows(d.indicators || []);
    var last = annual.length ? annual[annual.length - 1] : null;
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var annDates = cfList.filter(function (r) { return String(r['报告日'] || '').slice(5) === '12-31'; })
      .map(function (r) { return String(r['报告日']).slice(0, 10); });
    var last3 = annDates.slice(-3);
    var cxs = [], ocfs = [];
    last3.forEach(function (yd) {
      var r = sheetRowByDate(cfList, yd);
      if (r) {
        var c = r['购建固定资产、无形资产和其他长期资产所支付的现金'];
        var o = r['经营活动产生的现金流量净额'];
        if (c != null && o != null) { cxs.push(c); ocfs.push(o); }
      }
    });
    var light = null, ratio = null, calibre = '3年合计';
    if (last3.length >= 3 && cxs.length >= 3 && ocfs.reduce(function (a, b) { return a + b; }, 0) > 0) {
      ratio = cxs.reduce(function (a, b) { return a + b; }, 0) / ocfs.reduce(function (a, b) { return a + b; }, 0);
      light = ratio <= 0.6;
    } else {
      calibre = '单年';
      var lastCf = lastDate ? sheetRowByDate(cfList, lastDate) : null;
      var cx = lastCf ? lastCf['购建固定资产、无形资产和其他长期资产所支付的现金'] : null;
      var ocf = lastCf ? lastCf['经营活动产生的现金流量净额'] : null;
      if (cx != null && ocf != null && ocf > 0) { ratio = cx / ocf; light = ratio <= 0.6; }
    }
    var gmNow = last ? last['销售毛利率'] : null;
    var gmOld = null;
    if (lastDate) {
      var oldY = String(Number(lastDate.slice(0, 4) || 0) - 5);
      for (var gi = 0; gi < annual.length; gi++) {
        if (String(annual[gi]['报告期']).slice(0, 4) === oldY) { gmOld = annual[gi]['销售毛利率']; break; }
      }
    }
    return {
      light: light, pricing: (gmNow != null && gmOld != null) ? (gmNow - gmOld >= -0.03) : null,
      ratio: ratio, calibre: calibre,
      gmDelta: (gmNow != null && gmOld != null) ? gmNow - gmOld : null
    };
  }

  /* ---------------- 启动(已移交 Vue 组件) ---------------- */

export {
  BOND_10Y,
  PARENT_EQUITY_KEYS,
  TOTAL_EQUITY_KEYS,
  equityOf,
  fmtMoney,
  fmtNum,
  fmtDate,
  fmtPct,
  recentDividends,
  cmpKey,
  annualRows,
  fiscalEndOf,
  ttmNetProfit,
  shareCount,
  latestField,
  epsTtmField,
  latestPeriodIsAnnual,
  sheetRowByDate,
  annualBalanceRows,
  arOf,
  cagr,
  perShareDiv,
  consecutiveDivYears,
  sum,
  esc,
  valueAnalysis,
  lerpScore,
  lerpScoreNonneg,
  yoyOf,
  CYCLE_POS_W,
  weightedTotal,
  schoolTotal,
  capexPrevOf,
  gradeOf,
  gradeText,
  fraudGradeOf,
  fraudGradeText,
  it,
  MIN_PRICE_REF,
  BUY_MARGIN,
  G_D_PE_FULL,
  FX_TO_CNY,
  toCny,
  S_PB_FULL,
  EPS_PE_MIN,
  EPS_MAG_TTM,
  EPS_MAG_ANNUAL,
  RECURRING_MIN_RATIO,
  SELL_BAND,
  valueScores,
  fraudAnalysis,
  TRAP_W,
  TRAP_SUM_W,
  TRAP_CUT,
  TRAP_BANDS,
  trapBandOf,
  medOf,
  trapScore,
  TRAP_VALUE_FMT,
  G_ITEMS,
  G_SUM_W,
  G_CAP,
  G_MIN_PAIRS,
  gGrowth,
  growthScore,
  G_VALUE_FMT,
  G_ANCHOR_FMT,
  G_BANDS,
  gBandOf,
  V_ITEMS,
  V_SUM_W,
  V_EDGE_FLOOR,
  vNum,
  vMcap,
  valueScore,
  V_VALUE_FMT,
  V_ANCHOR_FMT,
  V_BANDS,
  vBandOf,
  R_W_VALUE,
  recommendScore,
  INTERIM_DIP_NOTE,
  interimDipYoy,
  R_BANDS,
  rBandOf,
  R_GATE_TEXT,
  managementAnalysis,
  cycleAnalysis,
  cycleGradeText,
  cycleHistory,
  cycleTrendOf,
  NET_CASH_W,
  netCashParts,
  noteLatest,
  DEBT_BUCKETS,
  debtKey,
  intDebtOf,
  weightedCashOf,
  netCashFormula,
  priceReferences,
  scoreCalcText,
  ST_FIN_KW,
  stBoardLine,
  stRiskOf,
  bmFeatures,
  R_FRAUD_GATE,
  R_TRAP_GATE,
  INTERIM_DIP_GATE,
};
