/* 价值分析详情渲染（自 masterwusama.github.io/stock-data/assets/stock.js 整体移植）
 * 改造点:去 IIFE 改 ESM;数据加载改由 Vue 组件经 /api 提供;删除 hash 路由顶层绑定。
 * 计算与渲染函数保持原逻辑不变,StockDetailView 提供同名 DOM 容器后调用 renderDetail(d)。
 */

  var DATA_BASE = './data/';

  var $ = function (id) { return document.getElementById(id); };
  var state = { current: null, charts: [], view: 'year', details: {},
    // 事件覆盖层（events/index.json byCode）：由 fetchOverlayOnce 拉取，或 StockDetailView 从接口数据直接注入
    eventOverlay: null, overlayLoaded: false };

  // 移动端断点（与 CSS @media max-width:600px 保持一致）：详情长列表折叠 + 图表 grid 收紧
  var mqMobile = window.matchMedia('(max-width: 600px)');

  // 10年期国债收益率参考值（用于股债利差对比，需手动定期更新）
  var BOND_10Y = 0.017;

  /* ---------------- 工具函数 ---------------- */

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
  function detailMarket() { return (state.current && state.current.market) || 'A'; }
  function yiUnit() { return detailMarket() === 'US' ? '亿美元' : '亿元'; }
  function yuanUnit() { return detailMarket() === 'US' ? '美元' : '元'; }
  // 市值本币后缀（拼在 fmtMoney 的「亿」后面）：A 股留空，与列表页 USD/HKD 角标同一套语义
  function fmtCap(v) {
    if (v == null || isNaN(v)) return '-';
    return fmtMoney(v) + (detailMarket() === 'US' ? '美元' : detailMarket() === 'HK' ? '港元' : '');
  }

  // 小数 → "12.34"
  function fmtNum(v) {
    if (v == null || isNaN(v)) return '-';
    return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  function fmtDate(s) {
    return s ? String(s).slice(0, 10) : '-';
  }

  function cls(v) {
    if (v == null || isNaN(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
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
  function cmpKey(ka, kb) { return ka < kb ? -1 : (ka > kb ? 1 : 0); }

  // indicators 中的年报序列（按报告期升序）
  function annualRows(indicators) {
    return (indicators || [])
      .filter(function (r) { return String(r['报告期'] || '').indexOf('12-31') >= 0; })
      .sort(function (a, b) { return cmpKey(a['报告期'], b['报告期']); });
  }

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
  function shareCount(balance, sharesFallback, bpsField) {
    var rows = (balance || []).filter(function (r) { return String(r['报告日'] || '').indexOf('12-31') >= 0; });
    var row = null, eq = null, capCn = null, capHk = null;
    if (rows.length) {
      rows.sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
      row = rows[rows.length - 1];
      capCn = row['实收资本(或股本)'];
      capHk = row['股本'];
      eq = row['归属于母公司股东权益合计'] != null ? row['归属于母公司股东权益合计'] : row['所有者权益(或股东权益)合计'];
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
  function latestPeriodIsAnnual(indicators) {
    var periods = (indicators || [])
      .map(function (r) { return String(r['报告期'] || '').slice(0, 10); })
      .filter(function (p) { return p.length >= 10; });
    if (!periods.length) return false;
    return periods.sort()[periods.length - 1].slice(5, 7) === '12';
  }

  // 从三大报表列表中取指定报告日（YYYY-MM-DD）的行
  function sheetRowByDate(list, date) {
    list = list || [];
    for (var i = 0; i < list.length; i++) {
      if (String(list[i]['报告日']).slice(0, 10) === date) return list[i];
    }
    return null;
  }

  // 三大报表年报序列（报告日 12-31，升序）——用于历史趋势对比
  function annualBalanceRows(rows) {
    return (rows || [])
      .filter(function (r) { return String(r['报告日'] || '').indexOf('12-31') >= 0; })
      .sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
  }

  // 应收账款读取（港股报表科目为“应收帐款”，双科目兼容，优先 A 股口径）
  function arOf(row) {
    if (!row) return null;
    var v = row['应收账款'];
    return v != null ? v : row['应收帐款'];
  }

  // 复合增长率：cur 较 prev 跨越 years 年；任一端 ≤ 0 时比率开小数次方无实数解，返回 null
  function cagr(cur, prev, years) {
    if (cur == null || prev == null || prev <= 0 || cur <= 0 || !years) return null;
    return Math.pow(cur / prev, 1 / years) - 1;
  }

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

  // 连续分红年数（从最新年份倒推）
  function consecutiveDivYears(dividends) {
    var years = {};
    (dividends || []).forEach(function (r) {
      var m = String(r.year || '').match(/^(\d{4})/);
      if (m) years[Number(m[1])] = true;
    });
    var ys = Object.keys(years).map(Number).sort(function (a, b) { return b - a; });
    if (!ys.length) return 0;
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

  function show(id) {
    ['stock-loading', 'stock-error', 'stock-list', 'stock-detail'].forEach(function (n) {
      $(n).style.display = n === id ? '' : 'none';
    });
  }

  function fail(msg) {
    $('stock-error').textContent = msg;
    show('stock-error');
  }

  // [legacy] hash 路由绑定已移除:导航由 vue-router 接管

  /* ---------------- Wind 事件增强分 helper ---------------- */

  // 基础分叠加事件 delta 后钉到 0~100；基础分缺失时原样返回（无基可加）
  function applyDelta(base, delta) {
    if (base == null) return null;
    if (!delta) return base;
    return Math.max(0, Math.min(100, base + delta));
  }

  // HTML 转义（事件明细表文本字段来自 Wind 原始数据，不可信，一律转义）
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // ⑥⑦ 卡片底部：Wind 事件优化说明（基础财报分 → 优化分 + 触发红旗）；仅当该代码在覆盖层时显示
  function eventEnhFooter(ov, base, kind) {
    if (!ov) return '';
    var delta = (kind === 'fraud' ? ov.fraudDelta : ov.mgmtDelta) || 0;
    var opt = applyDelta(base, delta);
    // 造假分越低越好（delta<0 为改善），管理分越高越好（delta>0 为改善）
    var good = kind === 'fraud' ? delta < 0 : delta > 0;
    var bad = kind === 'fraud' ? delta > 0 : delta < 0;
    var dCls = delta === 0 ? 'ev-flat' : (good ? 'ev-good' : (bad ? 'ev-bad' : 'ev-flat'));
    var sign = delta > 0 ? '+' : '';
    var h = '<div class="ev-enh">' +
      '<div class="ev-enh-line">Wind 事件优化：基础财报分 <b>' + (base == null ? '-' : fmtNum(base)) + '</b> ' +
      '<span class="' + dCls + '">' + (delta === 0 ? '±0' : sign + fmtNum(delta)) + '</span> → 优化分 <b>' +
      (opt == null ? '-' : fmtNum(opt)) + '</b>' +
      '<span class="ev-enh-hint">（' + (kind === 'fraud' ? '越低越好' : '越高越好') + '）</span></div>';
    var flags = ov.flags || [];
    if (flags.length) {
      h += '<div class="ev-flags">' + flags.map(function (f) {
        return '<span class="ev-flag">' + esc(f) + '</span>';
      }).join('') + '</div>';
    } else {
      h += '<div class="ev-flags"><span class="ev-flag ev-flag-none">无事件增量，与基础财报分一致</span></div>';
    }
    h += '<div class="ev-enh-note">事件信号来自一次性 Wind 抓取（增减持/并购重组/违规处罚/司法诉讼/ST/股东结构），仅本地留存不随每日更新；基础评分内核不变，本行只在“事件增强分”口径下生效。</div>';
    return h + '</div>';
  }

  /* ---------------- 公司详情 ---------------- */

  // 拉单家事件明细（events/<code>.json）；404 / 失败 → null（静默，无事件模块不显示）
  function fetchEventData(code) {
    return fetch(DATA_BASE + 'events/' + code + '.json?t=' + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // 确保事件覆盖层已加载（详情页按需自取，不依赖任何列表预载）；用 overlayLoaded 避免反复请求
  function fetchOverlayOnce() {
    if (state.overlayLoaded) return Promise.resolve(state.eventOverlay);
    return fetch(DATA_BASE + 'events/index.json?t=' + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (ov) { state.eventOverlay = (ov && ov.byCode) || null; state.overlayLoaded = true; return state.eventOverlay; })
      .catch(function () { state.overlayLoaded = true; return null; });
  }

  function showDetail(code) {
    var cached = state.details[code];
    // 已预载公司数据且事件明细也拉过则直接复用；否则补拉事件与覆盖层（本地小文件，不耗 Wind）
    if (cached && cached._evDone) { renderDetail(cached); return; }
    show('stock-loading');
    var pc = cached ? Promise.resolve(cached) :
      fetch(DATA_BASE + 'companies/' + code + '.json')
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .catch(function () { return null; });
    Promise.all([pc, fetchOverlayOnce()])
      .then(function (res) {
        var d = res[0];
        if (!d) { fail('公司数据加载失败：' + code); return null; }
        // 覆盖层有该代码事件条目才拉单家明细（避免港美股/未抓公司每次 404 控制台报错）
        var overlay = res[1];
        return (overlay && overlay[code])
          ? fetchEventData(code).then(function (evs) { return [d, evs]; })
          : [d, null];
      })
      .then(function (pair) {
        if (!pair) return;
        var d = pair[0];
        d._events = pair[1];
        d._evDone = true;
        if (!state.details[code]) state.details[code] = d;
        renderDetail(d);
      });
  }

  function renderDetail(d) {
    state.current = d;
    // 详情页 DOM 重建前释放旧图表实例
    state.charts.forEach(function (c) { try { c.dispose(); } catch (e) {} });
    state.charts = [];
    show('stock-detail');
    var s = d.snapshot || {};
    var chg = s.change_pct;

    var html = '';

    // 头部（市场徽标 + 货币单位：US→USD / HK→HKD / A→CNY）
    var marketName = d.market === 'US' ? '美股' : d.market === 'HK' ? '港股' : 'A股';
    var ccy = d.market === 'US' ? 'USD' : d.market === 'HK' ? 'HKD' : 'CNY';
    html += '<div class="stock-header">' +
      '<h2>' + d.name + '</h2>' +
      '<span class="s-badge s-badge-' + (d.market || 'A') + '">' + marketName + '</span>' +
      '<span class="s-code">' + d.code + '</span>' +
      '<span class="s-price ' + cls(chg) + '">' + fmtNum(s.price) + '</span>' +
      '<span class="s-ccy">' + ccy + '</span>' +
      '<span class="s-meta ' + cls(chg) + '">' +
      (chg == null ? '-' : (chg > 0 ? '+' : '') + (chg * 100).toFixed(2) + '%') + '</span>' +
      '<span class="s-meta">更新于 ' + fmtDate(s.time || d.updated_at) + '</span>' +
      '</div>';

    // Wind 事件覆盖层当前代码条目（⑥⑦优化说明 + ⑨事件模块共用）；无事件数据（港美股/未抓公司）为 null
    var ovD = (state.eventOverlay && state.eventOverlay[d.code]) ? state.eventOverlay[d.code] : null;
    var hasEvents = evHasAny(d._events);
    var hasAct = hasActions(d.actions);
    // ⑨ 只在有 Wind 事件明细时出，股本事件跟着顶上空缺的那个号，免得出现 ⑧ → ⑩ 的跳号
    var actNo = hasEvents ? '⑩' : '⑨';

    // 各模块锚点导航（点击平滑滚动，避免与 #/code 路由冲突）：①-⑧ 恒在，⑨ ⑩ 按有无数据出
    html += '<nav class="va-nav" aria-label="详情模块导航">' +
      '<a href="#sec-basic" data-scroll="sec-basic">① 基础财务信息</a>' +
      '<a href="#sec-value" data-scroll="sec-value">② 通用价值标准</a>' +
      '<a href="#sec-graham" data-scroll="sec-graham">③ 格雷厄姆烟蒂</a>' +
      '<a href="#sec-schloss" data-scroll="sec-schloss">④ 施洛斯烟蒂</a>' +
      '<a href="#sec-buffett" data-scroll="sec-buffett">⑤ 巴菲特芒格</a>' +
      '<a href="#sec-fraud" data-scroll="sec-fraud">⑥ 造假风险</a>' +
      '<a href="#sec-mgmt" data-scroll="sec-mgmt">⑦ 管理水平</a>' +
      '<a href="#sec-cycle" data-scroll="sec-cycle">⑧ 周期位置</a>' +
      (hasEvents ? '<a href="#sec-events" data-scroll="sec-events">⑨ 事件与股东</a>' : '') +
      (hasAct ? '<a href="#sec-actions" data-scroll="sec-actions">' + actNo + ' 股本事件</a>' : '') +
      '</nav>';

    // ---- 模块一：基础财务信息（估值快照/趋势图/财务对比/报表/分红/定期报告）----
    html += '<section id="sec-basic" class="stock-section va-module"><h2 class="va-module-title"><span>①</span>基础财务信息</h2>';

    // 估值快照
    var recDivs = recentDividends(d);
    var va = valueAnalysis(d); // 价值分析计算（股息/现金流/杜邦/成长/体检）
    var sc = valueScores(d, va); // 三大流派评分（格雷厄姆/施洛斯/巴菲特）
    sc.d = d;
    // 价格参考单独挂载（只依赖财报与快照，不回调 valueScores）
    sc.priceRefs = priceReferences(d, va);
    var divBox = '<div class="kv kv-div"><div class="k">近一年分红</div><div class="v">' + recDivs.length + ' 条</div>' +
      '<div class="div-list">' + (recDivs.length
        ? recDivs.map(function (r) {
          return '<span class="div-item">' + (r.year ? r.year + ' ' : '') + (r.description || '') + '</span>';
        }).join('')
        : '<span class="div-item">暂无</span>') +
      '</div></div>';
    html += '<div class="stock-section"><div class="stock-snapshot">' +
      kv('市盈率(TTM)', fmtNum(s.pe_ttm)) +
      kv('市净率', fmtNum(s.pb)) +
      kv('总市值', fmtCap(s.market_cap)) +
      kv('流通市值', fmtCap(s.float_market_cap)) +
      kv('公允清算价值', '<span title="(流动资产合计-负债合计)/财报股本，格雷厄姆清算口径，随财报更新">' +
        (sc.priceRefs && sc.priceRefs.fairLiq != null ? fmtNum(sc.priceRefs.fairLiq) : '-') + '</span>') +
      kv('净现金/市值', (function () {
        var r = sc.priceRefs || {};
        var tip = netCashFormula(r) ||
          '净现金/市值（最近一期财报 加权类现金 − 负债合计 ÷ 快照总市值），≥100% 表示扣除全部负债后的类现金仍高于市值';
        return '<span title="' + tip + '">' + fmtPct(r.netCashRatio) + '</span>';
      })()) +
      // 估值分位只是"当前估值在自己十年历史里的位置"，窗口/样本/亏损期是否计入全由外源定，
      // 故这一格纯展示，既不参与上面的四派评分，也不进参考价。
      kv('估值分位(10年)', (function () {
        var v = d.valuationPctile;
        if (!v) return '-';
        var one = function (k) {
          var o = v[k] || {};
          return k.toUpperCase() + ' ' + (o.pct == null ? '-' : Number(o.pct).toFixed(1) + '%');
        };
        var parts = [];
        ['pe', 'pb', 'ps'].forEach(function (k) {
          var o = v[k] || {};
          if (o.pct != null) parts.push(k.toUpperCase() + ' 样本 ' + (o.days == null ? '-' : o.days) + ' 个交易日');
        });
        var tip = '市盈率(TTM)／市净率／市销率(TTM) 各自近十年分位（外源 Wind 口径，0~100，越低说明当前估值在自己十年里越靠下）；'
          + '分位窗口、样本起点与亏损期处理均随外源，本系统只做展示与筛选，不进四派评分与买卖点参考价。'
          + '置空表示外源无值或已被判为不可信（如市盈率为负、外源日期过旧）。'
          + (v.date ? ' 外源观测日 ' + v.date + '。' : '')
          + (parts.length ? ' ' + parts.join('，') + '。' : '');
        return '<span title="' + tip + '">' + one('pe') + ' / ' + one('pb') + ' / ' + one('ps') + '</span>';
      })()) +
      kv('当前股息率', fmtPct(va.divYield)) +
      divBox +
      '</div></div>';

    // （价值分析五大区块：价值体检/股东回报/现金流质量/杜邦分析/成长性已移至模块二）

    // 指标趋势图（按指标分 3 个独立图表；支持季/年视图切换）
    var indCount = (d.indicators || []).length;
    html += '<div class="stock-section"><div class="stock-section-head">' +
      '<h3 id="stock-chart-title">关键指标趋势（近 ' + indCount + ' 期）</h3>' +
      '<div class="stock-view-toggle">' +
      '<button data-view="quarter">季</button>' +
      '<button data-view="year" class="active">年</button>' +
      '</div></div>' +
      '<div class="stock-chart-block"><h4 id="stock-chart-revenue-title">营业总收入 & 净利润（单季，' + yiUnit() + '）</h4><div class="stock-chart" id="stock-chart-revenue"></div></div>' +
      '<div class="stock-chart-block"><h4 id="stock-chart-margin-title">销售毛利率 & 销售净利率（报告期口径）</h4><div class="stock-chart" id="stock-chart-margin"></div></div>' +
      '<div class="stock-chart-block"><h4 id="stock-chart-roe-title">净资产收益率（各期累计）</h4><div class="stock-chart" id="stock-chart-roe"></div></div>' +
      '<p class="stock-chart-note" id="stock-chart-note">季度口径：单季值 = 本期累计 - 上期累计（一季报为当季值）；ROE 为报告期累计值' +
      // 财年不是日历年的公司（NVDA 1 月末、MSFT 6 月末、AAPL 9 月末）图上标的是对齐用的
      // 12-31，不点明就会被当成日历年数据去跟同行比（时点最多差半年）
      (function () {
        var fe = fiscalEndOf(d.indicators);
        return fe && fe.slice(5) !== '12-31'
          ? '；本公司财年截至 ' + fe + '，下面的 12-31 只是对齐日历年的报告期标签' : '';
      })() + '</p></div>';

    // 财务对比（年报/季报，任意两个报告期可对比）
    html += '<div class="stock-section"><div class="stock-section-head">' +
      '<h3>财务对比</h3>' +
      '<div class="stock-compare-pick">' +
      '<select id="stock-compare-a"></select>' +
      '<span>对比</span>' +
      '<select id="stock-compare-b"></select>' +
      '</div></div>' +
      '<div class="stock-compare-wrap"><table class="stock-compare" id="stock-compare-body"></table></div></div>';

    // 三大报表（金额单位随市场：A/港股人民币元，美股美元）
    html += '<div class="stock-section"><h3>财务报表' +
      '<span class="s-ccy-note">（金额单位：' + (d.market === 'US' ? '美元' : '人民币元') + '）</span></h3>' +
      '<div class="stock-tabs">' +
      sheetTab('income', '利润表') + sheetTab('balance', '资产负债表') + sheetTab('cashflow', '现金流量表') +
      '<select id="stock-period"></select></div>' +
      '<table class="stock-table" id="stock-sheet-body"></table></div>';

    // 分红历史（全量；移动端默认前 5 条，其余折叠）
    var divs = d.dividends || [];
    var foldDivs = mqMobile.matches && !state.detailExpanded;
    html += '<div class="stock-section"><h3>分红历史（' + divs.length + ' 条）</h3>';
    divs.forEach(function (r, i) {
      var desc = r.description || '';
      var extra = '';
      if (r.pay_date) extra += '派息日 ' + fmtDate(r.pay_date);
      html += '<div class="stock-list-item' + (foldDivs && i >= 5 ? ' d-more' : '') + '">' +
        '<span class="d-year">' + (r.year || '-') + '</span>' +
        '<span class="stock-badge">' + (r.type || '') + '</span>' +
        '<span class="d-desc">' + desc + '</span>' +
        (extra ? '<span class="d-date">' + extra + '</span>' : '') +
        '</div>';
    });
    if (foldDivs && divs.length > 5) {
      html += '<button type="button" class="d-more-btn" id="stock-divs-more">展开全部 ' + divs.length + ' 条</button>';
    }
    html += '</div>';

    // 定期报告（移动端默认前 5 份，其余折叠）
    var reports = d.reports || [];
    var foldReps = mqMobile.matches && !state.detailExpanded;
    html += '<div class="stock-section"><h3>定期报告（' + reports.length + ' 份）</h3>';
    reports.forEach(function (r, i) {
      var audit = '';
      // 审计信息：年报/半年报附事务所与意见类型（季报不审计，无该字段）
      if (r.audit_firm || r.audit_opinion) {
        audit = '<span class="d-audit">审计：' + (r.audit_firm || '—') +
          (r.audit_opinion ? ' · ' + r.audit_opinion : '') + '</span>';
      }
      html += '<div class="stock-list-item' + (foldReps && i >= 5 ? ' d-more' : '') + '">' +
        '<span class="stock-badge">' + r.category + '</span>' +
        '<span class="d-year">' + r.title + '</span>' +
        '<span class="d-date">' + fmtDate(r.date) + '</span>' +
        audit +
        '<a href="' + r.pdf_url + '" target="_blank" rel="noopener">PDF 原文</a>' +
        '<a href="' + r.detail_url + '" target="_blank" rel="noopener">详情</a>' +
        '</div>';
    });
    if (foldReps && reports.length > 5) {
      html += '<button type="button" class="d-more-btn" id="stock-reports-more">展开全部 ' + reports.length + ' 份</button>';
    }
    html += '</div>';
    html += '</section>'; // 模块一结束

    // ---- 模块二：通用价值标准（体检/股东回报/现金流/杜邦/成长）----
    html += '<section id="sec-value" class="stock-section va-module"><h2 class="va-module-title"><span>②</span>通用价值标准</h2>';

    // 价值体检清单（Pass/Fail 一眼定位风险点）
    html += '<div class="stock-section"><h3>价值体检</h3>' +
      '<div class="stock-compare-wrap"><table class="stock-compare va-health"><tbody id="stock-health-body"></tbody></table>' +
      '<p class="stock-chart-note">' + va.checkSummary + '</p></div></div>';

    // 股东回报：股息率/分红率/连续分红/累计派息 + 每股分红趋势
    html += '<div class="stock-section"><div class="stock-section-head">' +
      '<h3>股东回报</h3></div>' +
      '<div class="stock-snapshot va-snapshot">' +
      kv('股息率(近12月)', fmtPct(va.divYield)) +
      kv('10年国债收益率', fmtPct(BOND_10Y)) +
      kv('股债利差', fmtPct(va.spread)) +
      kv('分红率(最新年报)', fmtPct(va.payout)) +
      kv('连续分红年数', va.divConsecutive ? va.divConsecutive + ' 年' : '-') +
      kv('累计每股派息', va.cumPerShare == null ? '-' : fmtNum(va.cumPerShare) + ' 元') +
      '</div>' +
      '<div class="stock-chart-block"><h4>近 ' + va.divChart.length + ' 年每股派息（元/股）</h4><div class="stock-chart" id="stock-chart-dividend"></div></div></div>';

    // 现金流质量：净现比/自由现金流/收现比
    html += '<div class="stock-section"><div class="stock-section-head"><h3>现金流质量</h3></div>' +
      '<div class="stock-snapshot va-snapshot">' +
      kv('5年累计净现比', (va.ratio5 == null ? '-' : fmtNum(va.ratio5) + '（' + (va.ratioYears || 0) + '/5 年配对）')) +
      kv('5年累计自由现金流', fmtMoney(va.fcf5)) +
      kv('近5年收现比均值', va.collectAvg == null ? '-' : fmtNum(va.collectAvg)) +
      '</div>' +
      '<div class="stock-compare-wrap"><table class="stock-compare"><thead><tr>' +
      '<th>年度</th><th>净利润(亿)</th><th>经营现金流(亿)</th><th>净现比</th><th>资本开支(亿)</th><th>自由现金流(亿)</th><th>收现比</th></tr></thead>' +
      '<tbody id="stock-cf-body"></tbody></table></div>' +
      '<div class="stock-chart-block"><h4>净现比（年报，>1 说明利润有真金白银支撑）</h4><div class="stock-chart" id="stock-chart-netcash"></div></div></div>';

    // 杜邦分析：ROE = 净利率 × 总资产周转率 × 权益乘数
    html += '<div class="stock-section"><h3>杜邦分析</h3>' +
      '<div class="stock-compare-wrap"><table class="stock-compare"><thead><tr>' +
      '<th>年度</th><th>净利率</th><th>总资产周转率</th><th>权益乘数</th><th>ROE(拆解)</th><th>ROE(披露)</th></tr></thead>' +
      '<tbody id="stock-dupont-body"></tbody></table></div>' +
      '<p class="stock-chart-note">ROE 拆解 = 净利率 × 总资产周转率 × 权益乘数（期末口径）；披露 ROE 为同花顺报告期口径，两者略有差异属正常。</p></div>';

    // 成长性 & 估值匹配
    html += '<div class="stock-section"><div class="stock-section-head"><h3>成长性与估值匹配</h3></div>' +
      '<div class="stock-snapshot va-snapshot">' +
      kv('营收 CAGR(5年)', fmtPct(va.revCagr5)) +
      kv('净利 CAGR(5年)', fmtPct(va.netCagr5)) +
      kv('营收 CAGR(3年)', fmtPct(va.revCagr3)) +
      kv('净利 CAGR(3年)', fmtPct(va.netCagr3)) +
      kv('PEG', va.pegText) +
      '</div>' +
      '<p class="stock-chart-note">CAGR 基于 ' + va.growthNote + '；PEG = PE(TTM) / 净利5年CAGR，&lt;1 低估、1~2 合理、&gt;2 偏贵。</p></div>';
    html += '</section>'; // 模块二结束

    // ---- 模块三：格雷厄姆烟蒂标准评判（进取型 + 防御型两张评分卡）----
    html += '<section id="sec-graham" class="stock-section va-module"><h2 class="va-module-title"><span>③</span>格雷厄姆烟蒂标准评判</h2>' +
      '<div class="score-grid">' +
      '<div class="score-card" id="stock-score-graham-agg"></div>' +
      '<div class="score-card" id="stock-score-graham-def"></div>' +
      '</div></section>';

    // ---- 模块四：施洛斯烟蒂标准评判 ----
    html += '<section id="sec-schloss" class="stock-section va-module"><h2 class="va-module-title"><span>④</span>施洛斯烟蒂标准评判</h2>' +
      '<div class="score-card" id="stock-score-schloss"></div></section>';

    // ---- 模块五：巴菲特芒格价值标准评判 ----
    html += '<section id="sec-buffett" class="stock-section va-module"><h2 class="va-module-title"><span>⑤</span>巴菲特芒格价值标准评判</h2>' +
      '<div class="score-card" id="stock-score-buffett"></div></section>';

    // ---- 模块六：财务报表造假可能性分析（量化红旗筛查，百分制越高风险越大）----
    html += '<section id="sec-fraud" class="stock-section va-module"><h2 class="va-module-title"><span>⑥</span>财务报表造假可能性分析</h2>' +
      '<div class="score-card" id="stock-score-fraud"></div></section>';

    // ---- 模块七：管理层管理水平评分（8 维百分制加权，越高越好）----
    html += '<section id="sec-mgmt" class="stock-section va-module"><h2 class="va-module-title"><span>⑦</span>管理层管理水平评分</h2>' +
      '<div class="score-card" id="stock-score-mgmt"></div></section>';

    // ---- 模块八：周期位置（周期性判定 + 底部概率，分数越低越接近底部，非周期不打分）----
    html += '<section id="sec-cycle" class="stock-section va-module"><h2 class="va-module-title"><span>⑧</span>周期位置 · 周期性行业判定</h2>' +
      '<div class="score-card" id="stock-score-cycle"></div>' +
      '<div class="stock-chart-block" id="stock-cycle-chart-block" style="display:none"><h4>历年财报周期位置评分趋势（分数越低越接近周期底部）</h4>' +
      '<div class="stock-chart" id="stock-chart-cycle"></div>' +
      '<p class="stock-chart-note">逐年回溯：以各年报年为窗口末尾取最近 8 年年报，按与当期相同的 8 维逻辑打分；单季环比逐年参与（历史年用该年自身单季营收环比，末年用最新单季环比），各年均为满 8 维、同口径可比。</p></div></section>';

    // ---- 模块九：公司事件与股东结构（仅当有 Wind 事件明细时展示；港美股/未抓公司自动隐藏）----
    if (hasEvents) {
      html += '<section id="sec-events" class="stock-section va-module"><h2 class="va-module-title"><span>⑨</span>公司事件与股东结构</h2>' +
        renderEvents(d._events, ovD) + '</section>';
    }

    // ---- 模块十：股本事件（定增 / 回购，东财全市场快照；港美股不采，整节不出）----
    // 编号跟着 ⑨ 的有无走：没有 Wind 事件明细时 ⑨ 这个位子空着，由本节顶上
    if (hasAct) {
      html += '<section id="sec-actions" class="stock-section va-module"><h2 class="va-module-title"><span>' + actNo + '</span>股本事件 · 定增与回购</h2>' +
        renderActions(d.actions) + '</section>';
    }

    $('stock-detail-body').innerHTML = html;
    bindViewToggle();
    bindComparePicks();
    bindVaNav();
    renderCharts(d.indicators || []);
    renderCompare(d);
    initSheet(d);
    renderValueAnalysis(va);
    renderScores(sc);
    var fa = fraudAnalysis(d);
    var fraudEl = $('stock-score-fraud');
    if (fraudEl) fraudEl.innerHTML = fraudCard(fa, ovD);
    var ma = managementAnalysis(d);
    var mgmtEl = $('stock-score-mgmt');
    if (mgmtEl) mgmtEl.innerHTML = managementCard(ma, ovD);
    var ca = cycleAnalysis(d);
    var cycleEl = $('stock-score-cycle');
    if (cycleEl) cycleEl.innerHTML = cycleCard(ca);
    renderCycleChart(d, ca);
    bindMoreButtons();
  }

  // 移动端“展开全部”按钮：分红/定期报告各在其所属 section 内展开折叠项
  function bindMoreButtons() {
    var bind = function (id) {
      var btn = $(id);
      if (!btn) return;
      btn.addEventListener('click', function () {
        state.detailExpanded = true;
        btn.parentNode.querySelectorAll('.d-more').forEach(function (el) { el.classList.remove('d-more'); });
        btn.parentNode.removeChild(btn);
      });
    };
    bind('stock-divs-more');
    bind('stock-reports-more');
  }

  /* ---------------- 价值分析：核心计算与渲染 ---------------- */

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
    var divConsecutive = consecutiveDivYears(divs);
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
      var equity = b ? (b['归属于母公司股东权益合计'] != null ? b['归属于母公司股东权益合计'] : b['所有者权益(或股东权益)合计']) : null;
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
    // fairPe 与巴菲特净利成长项（10% 阈值是按 5 年标定的）。取不晚于 lastYear-5 的最近年报
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

  // 渲染价值分析各区块（体检表 + 现金流表 + 杜邦表 + 2 张图）
  function renderValueAnalysis(va) {
    renderHealth(va);
    renderCfTable(va);
    renderDuPont(va);
    renderVaCharts(va);
  }

  function renderHealth(va) {
    var body = $('stock-health-body');
    if (!body) return;
    var html = va.checks.map(function (c) {
      var badge = c.pass === 'pass' ? '<span class="va-badge va-pass">通过</span>'
        : c.pass === 'fail' ? '<span class="va-badge va-fail">未通过</span>'
        : '<span class="va-badge va-na">数据不足</span>';
      return '<tr><td class="k">' + c.label + '</td><td class="v">' + c.val + '</td>' +
        '<td class="v">' + c.std + '</td><td class="v">' + badge + '</td></tr>';
    }).join('');
    body.innerHTML = html;
  }

  function renderCfTable(va) {
    var body = $('stock-cf-body');
    if (!body) return;
    body.innerHTML = va.cfRows.map(function (r) {
      return '<tr><td>' + r.year + '</td>' +
        '<td>' + fmtMoney(r.net) + '</td>' +
        '<td>' + fmtMoney(r.ocf) + '</td>' +
        '<td>' + fmtNum(r.ratio) + '</td>' +
        '<td>' + fmtMoney(r.capex) + '</td>' +
        '<td>' + fmtMoney(r.fcf) + '</td>' +
        '<td>' + fmtNum(r.collect) + '</td></tr>';
    }).join('');
  }

  function renderDuPont(va) {
    var body = $('stock-dupont-body');
    if (!body) return;
    body.innerHTML = va.dupont.map(function (r) {
      return '<tr><td>' + r.year + '</td>' +
        '<td>' + fmtPct(r.margin) + '</td>' +
        '<td>' + fmtNum(r.turnover) + '</td>' +
        '<td>' + fmtNum(r.leverage) + '</td>' +
        '<td>' + fmtPct(r.roe) + '</td>' +
        '<td>' + fmtPct(r.roeReported) + '</td></tr>';
    }).join('');
  }

  /* ---------------- 三大流派价值评分（满分 100，基准=最新年报 + 最新市值） ---------------- */

  // 线性得分：v ≤ a 取 ma；v ≥ b 取 mb；中间线性
  function lerpScore(v, a, b, ma, mb) {
    if (v == null || isNaN(v)) return null;
    if (v <= a) return ma;
    if (v >= b) return mb;
    return ma + (v - a) / (b - a) * (mb - ma);
  }

  // 定义域为正数的“越小越好”项（PE/PB 等，ma > mb）的符号护栏：v ≤ 0 是亏损/资不抵债的
  // “不达标”而非数据缺失，直接给最低分 mb；否则 lerpScore 的 v <= a 分支会把负值夹到满分端
  function lerpScoreNonneg(v, a, b, ma, mb) {
    if (v == null || isNaN(v)) return null;
    return v <= 0 ? mb : lerpScore(v, a, b, ma, mb);
  }

  // 同比增长率：基期为负时用 |基期| 作分母，使亏损扩大→负、亏损收窄/扭亏→正；
  // 基期为正时与 cur/pre-1 完全等价，基期为 0 或缺失时无定义返回 null
  function yoyOf(cur, prev) {
    if (cur == null || prev == null || prev === 0) return null;
    return (cur - prev) / Math.abs(prev);
  }

  // 周期位置分 8 维权重：cycleAnalysis 阶段二与 cycleHistory 必须同尺度。
  // 合计 105 而非 100 —— 这正是旧代码要靠 Math.min(100, sum) 硬夹的原因，归一化后不再需要
  var CYCLE_POS_W = [25, 15, 15, 10, 10, 10, 10, 10];

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
  function capexPrevOf(annualCf, row, key) {
    var ci = row == null ? -1 : annualCf.indexOf(row);
    if (ci < 0) return [];
    return annualCf.slice(Math.max(0, ci - 3), ci)
      .map(function (r) { return r[key]; }).filter(function (v) { return v != null; });
  }

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
  function it(std, val, thr, max, score, inapp) {
    return {
      std: std, val: val, thr: thr, max: max, score: score,
      // inapp：驱动科目在本公司报表里结构性不存在（不适用），与「有这项但没数」（缺失）分开标注
      na: inapp === true && score == null,
      match: score == null ? null : Math.max(0, score) / max
    };
  }

  // 价格标签：买入价≤现价（进入买入区）标绿；卖出价≥现价（进入卖出区）标红
  function _priceTag(label, p, curPrice, kind, tip) {
    var cls = '';
    if (p != null && curPrice != null) {
      if (kind === 'buy' && curPrice <= p) cls = ' sp-hit';
      if (kind === 'sell' && curPrice >= p) cls = ' sp-hit';
    }
    return '<span class="sp' + cls + '" title="' + tip + '"><i>' + label + '</i><b>' +
      (p == null ? '-' : fmtNum(p)) + '</b></span>';
  }

  // 评分卡 HTML：标题 + 总分圆徽 + 价格参考行 + 标准明细表（标准/当前值/参考阈值/符合度/得分）+ 备注
  function scoreCard(title, basis, total, items, note, priceRefs, curPrice) {
    var g = gradeOf(total);
    var rows = items.map(function (x) {
      var mCls = x.match == null ? 'sc-na' : x.match >= 0.99 ? 'sc-good' : x.match >= 0.5 ? 'sc-mid' : 'sc-low';
      var mTxt = x.match == null ? '-' : (x.match * 100).toFixed(0) + '%';
      var stTxt = x.na ? '不适用' : (x.score == null ? '-' : fmtNum(x.score));
      return '<tr' + (x.na ? ' class="sc-inapp"' : '') + ' title="' +
        (x.na ? '该公司报表里没有这一科目（不是数据缺失），不计分也不计入有效满分' : '') + '">' +
        '<td>' + x.std + '</td><td class="v">' + (x.na ? '报表无此科目' : x.val) + '</td>' +
        '<td class="v">' + x.thr + '</td>' +
        '<td class="v ' + mCls + '">' + mTxt + '</td>' +
        '<td class="v"><b>' + stTxt + '</b> / ' + x.max + '</td></tr>';
    }).join('');
    var refs = priceRefs || {};
    var pricesHtml = '<div class="score-prices">' +
      _priceTag('买入参考', refs.buy, curPrice, 'buy', '该流派估值锚打折后的价格（账面派为资产折价线，收益派为保守卖价×2/3 安全边际）；现价不高于此价时进入买入参考区。账面派这格是目标价、与现价无关，只要有锚就给') +
      _priceTag('保守卖出', refs.sellCons, curPrice, 'sell', '核心估值锚位；现价不低于此价时进入保守卖出参考区。空值两种情形：收益派＝每股盈利锚不可信（亏损或口径异常），账面派＝现价已在资产锚 2 倍之外，清算/账面口径对这只股票没有「该卖」的意见') +
      _priceTag('公允卖出', refs.sellFair, curPrice, 'sell', '估值锚位上浮后的价格；现价不低于此价时进入公允卖出参考区。空值与保守卖出同生同灭') +
      '</div>';
    return '<div class="score-card-head"><h4>' + title + '</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>总分</span><b>' + (total == null ? '-' : fmtNum(total)) + '</b><i>' + gradeText(g) + '</i></div></div>' +
      '<p class="score-basis">' + basis + '</p>' +
      pricesHtml +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>评判标准</th><th>当前值</th><th>参考阈值</th><th>符合度</th><th>得分</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      (note ? '<p class="score-note">' + note + '</p>' : '');
  }

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
  var BUY_MARGIN = 2 / 3;      // 收益派（格防/巴菲特）买点相对公允倍数的安全边际
  var G_A_PNCAV_FULL = 0.67;   // 格攻：市值/净流动资产 ≤ 0.67 拿满 30 分，亦是买点倍数
  var G_D_PE_FULL = 15;        // 格防：市盈率 ≤ 15 拿满 5 分，亦是保守卖价倍数
  // 本币 → 人民币的粗略汇率（按市场取）：只服务格防的规模硬门槛（100/50/30 亿当年按 A 股
  // 人民币定的，不折会把体量相当的港美股系统性降 1~2 档）。与 scoring.py 的 FX_TO_CNY 同值。
  var FX_TO_CNY = { A: 1.0, HK: 0.92, US: 7.15 };

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
  var RECURRING_MIN_RATIO = 0.5;
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
        var eqFb = lastBa['归属于母公司股东权益合计'] != null ? lastBa['归属于母公司股东权益合计'] : lastBa['所有者权益(或股东权益)合计'];
        var shFb = shareCount(d.balance, (mcap != null && s.price != null && s.price > 0) ? mcap / s.price : null, null);
        if (eqFb != null && shFb) bpsFb = eqFb / shFb;
      }
      if (bpsFb != null && bpsFb > 0 && s.price != null && s.price > 0) pb = s.price / bpsFb;
    }
    var divConsecutive = va.divConsecutive || 0;
    var divYield = va.divYield;

    // ---- 基础量（最新年报）----
    var ca = lastBa ? lastBa['流动资产合计'] : null;      // 流动资产合计
    var cl = lastBa ? lastBa['流动负债合计'] : null;      // 流动负债合计
    var tl = lastBa ? lastBa['负债合计'] : null;          // 负债合计
    var assets = lastBa ? lastBa['资产总计'] : null;      // 资产总计
    var cash = lastBa ? lastBa['货币资金'] : null;        // 货币资金
    var stDebt = lastBa ? lastBa['短期借款'] : null;      // 短期借款
    var ltDebt = lastBa ? lastBa['长期借款'] : null;      // 长期借款
    var bond = lastBa ? lastBa['应付债券'] : null;        // 应付债券
    var due1y = lastBa ? lastBa['一年内到期的非流动负债'] : null; // 一年内到期的长贷/债券/租赁重分类
    var lease = lastBa ? lastBa['租赁负债'] : null;       // 租赁负债（新租赁准则表内化的分期付款）
    var intang = lastBa ? lastBa['无形资产'] : null;      // 无形资产
    var goodwill = lastBa ? lastBa['商誉'] : null;        // 商誉
    var netProfit = last ? last['净利润'] : null;
    var debtr = last ? last['资产负债率'] : null;
    var gMargin = last ? last['销售毛利率'] : null;
    var nMargin = last ? last['销售净利率'] : null;
    // 有息负债全口径：短借 + 一年内到期 + 长借 + 应付债券 + 租赁负债（字段缺失视为 0）
    var intDebt = sum([stDebt, due1y, ltDebt, bond, lease]);
    if (intDebt == null) intDebt = 0;
    var netCash = (cash != null && intDebt != null) ? cash - intDebt : null; // 净现金
    var ncav = (ca != null && tl != null) ? ca - tl : null;   // 净流动资产 NCAV
    var wc = (ca != null && cl != null) ? ca - cl : null;     // 营运资本
    // 长期有息负债全口径：一年内到期部分为重分类的长贷/债券，租赁负债计入（字段缺失视为 0）
    var ltd = sum([due1y, ltDebt, bond, lease]);
    if (ltd == null) ltd = 0;
    var curRatio = (ca != null && cl != null && cl > 0) ? ca / cl : null;
    var liqRatio = (ca != null && tl != null && tl > 0) ? ca / tl : null;
    var pncav = (mcap != null && ncav != null && ncav > 0) ? mcap / ncav : null;
    var pnetcash = (mcap != null && netCash != null && netCash > 0) ? mcap / netCash : null;
    // 净现金/NCAV 为负时属于“不达标”而非“数据缺失”：当前值文本说明 + 得 0 分
    var pncavVal = ncav == null ? '-' : (ncav > 0 ? (pncav == null ? '-' : fmtNum(pncav) + '×') : 'NCAV 为负');
    var pnetcashVal = netCash == null ? '-' : (netCash > 0 ? (pnetcash == null ? '-' : fmtNum(pnetcash) + '×') : '净现金为负');
    var pepb = (pe != null && pb != null) ? pe * pb : null;
    var intangShare = (intang != null && assets != null && assets > 0) ? intang / assets : null;
    var goodwillShare = (goodwill != null && assets != null && assets > 0) ? goodwill / assets : null;

    // 近5年年报净利润（盈利稳定性）与近5年净利累计增长
    var net5 = annual.slice(-5).map(function (r) { return r['净利润']; });
    var posN = net5.filter(function (v) { return v != null && v > 0; }).length;
    var grow5 = (net5.length >= 2 && net5[0] != null && net5[net5.length - 1] != null && net5[0] > 0)
      ? net5[net5.length - 1] / net5[0] - 1 : null;

    // ---- ROE 近 5 年年报序列：水平取中位数，持续性取「达标（≥10%）年数占比」----
    // 不用均值：披露口径的 ROE 在薄权益/负权益处会炸到 ±几千个百分点（实测 155 家有单年 >100%，
    // 美股 90 家最集中），均值与中位数在这份数据上的相关系数只有 0.2486，6912 家里 402 家
    // （5.8%）两种算法会跨过 15% 这条线，且错的方向随机：Home Depot 均值 −4357%（某年权益为负）
    // 而中位 +385%，被误判 0 分；O'Reilly 均值 984% 而中位 −168%，白拿满分。中位数只取中间那一年，
    // 炸群的一年进不了统计量。早先这两个 ROE 项（护城河 4 + 盈利质量 25）同用这一个均值，
    // 实测相关 0.9801，等于 29/100 押在同一个数上——现拆成「水平」与「持续」两个量。
    var roeVals = va.dupont.map(function (r) { return r.roeReported; }).filter(function (v) { return v != null; });
    var nRoe = roeVals.length, roeMed5 = null, roeOkFrac = null, roeOkYears = null;
    if (nRoe) {
      // 必须带数值比较器：JS 默认按字符串排序，[0.4, 2, 15] 会排成 [0.4, 15, 2]
      var roeSorted = roeVals.slice().sort(function (a, b) { return a - b; });
      var rMid = Math.floor(nRoe / 2);
      roeMed5 = nRoe % 2 ? roeSorted[rMid] : (roeSorted[rMid - 1] + roeSorted[rMid]) / 2;
    }
    // 不足 3 年无从谈「持续」，按缺失处理（缺数据项在归一化时当中性，不虚给也不白扣）
    if (nRoe >= 3) {
      roeOkYears = roeVals.filter(function (v) { return v >= 0.10; }).length;
      roeOkFrac = roeOkYears / nRoe;
    }

    var basis = '评分基准：' + (lastYear ? lastYear + ' 年报' : '最新财报') +
      (s.time ? ' + ' + fmtDate(s.time) + ' 收盘价/市值' : '') +
      '；有息负债含一年内到期与租赁负债（全口径）';

    // ---- 格雷厄姆 · 进取型烟蒂（net-net 净流动资产折价）----
    var gA = [
      it('价格/净流动资产（市值/NCAV）', pncavVal, '≤ 0.67×（2/3 净流动资产，亦是买入参考倍数）', 30, ncav == null ? null : (ncav > 0 ? lerpScore(pncav, G_A_PNCAV_FULL, 1.5, 30, 0) : 0), noCA),
      it('价格/净现金（市值/现金-有息负债）', pnetcashVal, '≤ 1×', 20, netCash == null ? null : (netCash > 0 ? lerpScore(pnetcash, 1, 2, 20, 0) : 0), noCash),
      it('流动资产/总负债', liqRatio == null ? '-' : fmtNum(liqRatio), '≥ 2（资产覆盖债务）', 20, lerpScore(liqRatio, 1, 2, 0, 20), noCA),
      it('最新年报净利润', fmtMoney(netProfit), '> 0（清算缓冲）', 15, netProfit != null && netProfit > 0 ? 15 : 0),
      it('资产负债率', fmtPct(debtr), '≤ 60%', 10, lerpScore(debtr, 0.6, 0.8, 10, 0)),
      it('连续分红年数', (divConsecutive || 0) + ' 年', '≥ 3 年', 5, divConsecutive >= 3 ? 5 : divConsecutive >= 1 ? 2.5 : 0)
    ];
    var gATotal = schoolTotal(gA);

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
      it('盈利稳定（近5年净利为正）', posN + '/5 年', '5 年全部为正', 15, posN >= 5 ? 15 : posN === 4 ? 9 : posN === 3 ? 4 : -5),
      it('连续分红年数', (divConsecutive || 0) + ' 年', '≥ 10 年', 15, divScore10(divConsecutive || 0)),
      it('近5年净利累计增长', fmtPct(grow5), '≥ 33%', 10, grow5 == null ? null : (grow5 >= 0.33 ? 10 : grow5 >= 0 ? lerpScore(grow5, 0, 0.33, 0, 10) : -5)),
      it('市盈率（TTM）', pe == null ? '-' : (pe > 0 ? fmtNum(pe) : 'PE 为负（亏损）'), '≤ 15（亦是保守卖出参考倍数）', 5, lerpScoreNonneg(pe, G_D_PE_FULL, 25, 5, 0)),
      it('PE × PB', pepb == null ? '-' : ((pe > 0 && pb > 0) ? fmtNum(pepb) : 'PE/PB 为负'), '≤ 22.5', 5, pepb != null ? ((pe <= 0 || pb <= 0) ? 0 : (pepb <= 22.5 ? 5 : (pepb <= 45 ? lerpScore(pepb, 22.5, 45, 5, 0) : 0))) : null)
    ];
    // 其中流动比率/长期负债比/盈利稳定/增长四项可为负（最低各 −10/−10/−5/−5），
    // 归一分母仍是满分合计 100，故 −30 的下限不变
    var gDTotal = schoolTotal(gD);

    // ---- 施洛斯风险扣分（资产萎缩/减值结构/债务恶化/经营溃败的量化危险信号，仅负分）----
    // 归母权益（优先归母，缺则全部权益）
    function eqOf(row) {
      if (!row) return null;
      var v = row['归属于母公司股东权益合计'];
      return v != null ? v : row['所有者权益(或股东权益)合计'];
    }
    // 有息负债全口径（与上方 intDebt 一致：短借+一年内到期+长借+债券+租赁，缺键当 0）
    function intDebtOf(row) {
      if (!row) return null;
      var v = sum([row['短期借款'], row['一年内到期的非流动负债'], row['长期借款'], row['应付债券'], row['租赁负债']]);
      return v == null ? 0 : v;
    }
    var lastEq = eqOf(lastBa);
    var earliestEq = baAnnual.length >= 5 ? eqOf(baAnnual[0]) : null;
    var intDebtNow = lastBa ? intDebtOf(lastBa) : null;
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
    var eqDistress = lastEq != null && lastEq <= 0 && gwIntSum > 0 && !(netProfit != null && netProfit > 0);
    var inv = lastBa ? lastBa['存货'] : null;
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
      it('最新年报净利润', fmtMoney(netProfit), '> 0', 10, netProfit != null && netProfit > 0 ? 10 : 0),
      it('市值 / 流动资产', (mcap == null ? '-' : fmtMoney(mcap)) + ' / ' + (ca == null ? '-' : fmtMoney(ca)), '市值 ≤ 流动资产', 10,
        (mcap != null && ca != null && ca > 0) ? (mcap <= ca ? 10 : lerpScore(mcap / ca, 1, 2, 10, 0)) : null, noCA)
    ];
    // 正分项归一 + 9 个扣分项原样相加：扣分项缺数据本就给 0，不参与归一（理由见 schoolTotal）
    var sTotal = schoolTotal(sItems, riskItems);

    // ---- 巴菲特芒格（优质企业 + 护城河）----
    var moatItems = [
      it('销售毛利率', fmtPct(gMargin), '≥ 40%（定价权迹象）', 5, lerpScore(gMargin, 0.2, 0.4, 0, 5), noGM),
      it('近5年 ROE ≥ 10% 达标年数 · 护城河', roeOkFrac == null ? '-' : roeOkYears + '/' + nRoe + ' 年', '5/5 年达标（2/5 起给分）', 4,
        lerpScore(roeOkFrac, 0.4, 1.0, 0, 4)),
      // 不含商誉：商誉/总资产 ≥10% 组其后大额减值（≥净资产5%）率 29.01% vs 其余 12.28%（lift 2.36、
      // z=+20.7、四档单调），无形 ≥10% 组 13.73% vs 13.22%（lift 1.04，分不开）——判据见 backend/scripts/fraud_validity.py B 节
      it('无形资产 / 总资产', fmtPct(intangShare), '≥ 10%（品牌/专利/特许权）', 3,
        lerpScore(intangShare, 0, 0.1, 0, 3)),
      it('连续分红且分红率 ≤ 70%', (divConsecutive || 0) + ' 年 / ' + fmtPct(va.payout), '≥ 5 年且 ≤ 70%', 3,
        divConsecutive >= 5 ? (va.payout != null && va.payout <= 0.7 ? 3 : 1.5) : 0)
    ];
    var bItems = [
      it('ROE（近5年中位数）· 盈利质量', fmtPct(roeMed5), '≥ 15%（10% 起给分）', 25, lerpScore(roeMed5, 0.10, 0.15, 0, 25)),
      it('销售净利率（最新年报）', fmtPct(nMargin), '≥ 10%', 15, lerpScore(nMargin, 0.05, 0.10, 0, 15)),
      it('资产负债率', fmtPct(debtr), '≤ 50%', 15, lerpScore(debtr, 0.5, 0.75, 15, 0)),
      it('5年累计净现比', fmtNum(va.ratio5) + (va.ratioYears === 5 ? '' : '（' + (va.ratioYears || 0) + '/5 年）'), '≥ 1', 15, lerpScore(va.ratio5, 0.5, 1, 0, 15)),
      it('净利润 5 年 CAGR', fmtPct(va.netCagr5), '≥ 10%', 15, lerpScore(va.netCagr5, 0, 0.1, 0, 15))
    ];
    // 缺得最多的是净现比（23.8%）与净利 5 年 CAGR（32.4%），各 15 分，早先一律白扣
    var bTotal = schoolTotal(bItems.concat(moatItems));

    // 护城河备注：无形资产/商誉明细 + 特许经营（定价权）证据说明
    var moatNote = '';
    if (intang != null || goodwill != null) {
      moatNote = '无形资产 ' + fmtMoney(intang) + '（占总资产 ' + fmtPct(intangShare) + '），商誉 ' + fmtMoney(goodwill) + '（占 ' + fmtPct(goodwillShare) + '）。'
        + '本项只按无形资产计量：商誉占比 ≥10% 的公司其后出现大额减值（≥ 净资产 5%）的比例是其余的 2.36 倍（z=20.7），'
        + '而无形资产看不出这个关系（1.04 倍），所以商誉在这里是风险信号、不计入护城河得分。';
      if (gMargin != null && gMargin >= 0.4 && roeMed5 != null && roeMed5 >= 0.15) {
        moatNote += '高毛利率（≥40%）+ 高 ROE（近5年中位 ≥15%）组合通常意味着品牌溢价或特许经营（定价权）等护城河，是无形资产创造超额回报的量化证据；若该特征为行业通性（如医药/软件），则更多体现行业属性而非个体优势，需结合行业地位判断。';
      } else if (gMargin != null && gMargin >= 0.4 || roeMed5 != null && roeMed5 >= 0.15) {
        moatNote += '毛利率或 ROE 单项突出，特许经营/品牌优势的证据不完全，需结合行业地位判断其可持续性。';
      } else {
        moatNote += '毛利率与 ROE 均未达强护城河量化线（40%/15%），暂未见品牌溢价或特许经营定价权证据。';
      }
      if (goodwillShare != null && goodwillShare > 0.2) moatNote += '商誉占比偏高（>20%），若增长依赖并购需警惕商誉减值风险。';
      if (intangShare != null && intangShare > 0.3) moatNote += '无形资产占比较高（>30%），注意区分专利/特许经营权与土地使用权，前者才是定价权来源。';
    } else {
      moatNote = '最新年报未披露无形资产/商誉明细，无法量化评估特许经营资产。';
    }
    // 本卡两行都有 ROE，但测的是两件事（原先同用一个均值，实测相关 0.9801，
    // 等于 29/100 押在同一个数上）。备注要说清两行各自看什么，免得看上去像重复录入。
    moatNote += 'ROE 在本卡有两行，看的不是同一件事：护城河项（4 分）看持续，取近 5 年中 ROE ≥ 10% 的'
      + '达标年数（5/5 年达标才满分）；盈利质量项（25 分）看水平，取近 5 年 ROE 中位数（≥ 15% 满分，'
      + '10% 起给分）。中位数只取中间那一年，某一年因权益变薄而畸高畸低都进不了统计量。';

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
      grahamAgg: { title: '进取型烟蒂 · net-net（低于净流动资产买入）', total: gATotal, items: gA, eff: effOf(gA),
        note: '格雷厄姆 net-net 思路：以低于净流动资产（流动资产-全部负债）2/3 的价格买入，赚取清算价值与市价之差。得分越高代表越接近“捡烟蒂”状态。' },
      grahamDef: { title: '防御型烟蒂 · 防御型投资者标准', total: gDTotal, items: gD, eff: effOf(gD),
        note: '对应《聪明的投资者》第 14 章防御型投资者选股标准（规模/流动比率/长期负债/盈利稳定/分红历史/盈利增长/估值）。规模为硬门槛（总资产≥100亿），关键安全项（流动比率<1、营运资本为负、近5年过半亏损、净利负增长）直接负分惩罚，比进取型更严格。' },
      schloss: { title: '施洛斯烟蒂 · 资产折扣+低估值+低负债', total: sTotal, items: sItems.concat(riskItems), eff: effOf(sItems),
        note: '沃尔特·施洛斯风格：以低于净资产/流动资产的价格买入、负债极低、有股息，分散持有等待价值回归。风险扣分项为量化危险信号：净资产萎缩/扣非亏损、商誉无形与应收存货减值结构、有息负债攀升与利息覆盖不足、营收毛利率趋势溃败，数据不足不扣分；管理层掏空等无公开量化数据的信号未纳入。' },
      buffett: { title: '巴菲特芒格 · 优质企业合理价格+护城河', total: bTotal, items: bItems.concat(moatItems), eff: effOf(bItems.concat(moatItems)),
        note: moatNote }
    };
  }

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
    var soft = lastBa ? ((lastBa['商誉'] || 0) + (lastBa['无形资产'] || 0)) : null;
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

  // 造假分析评分卡（与 scoreCard 同构但等级方向相反：分低=安全=绿）；ov 为 Wind 事件覆盖层条目，有则并列基础分+事件明细+优化分
  function fraudCard(fa, ov) {
    var g = fraudGradeOf(fa.total);
    var rows = fa.items.map(function (x) {
      var mCls = x.match == null ? 'sc-na' : x.match <= 0.01 ? 'sc-good' : x.match < 0.5 ? 'sc-mid' : x.match < 0.99 ? 'sc-low' : 'sc-bad';
      var mTxt = x.match == null ? '-' : (x.match * 100).toFixed(0) + '%';
      return '<tr><td>' + x.std + '</td><td class="v">' + x.val + '</td><td class="v">' + x.thr + '</td>' +
        '<td class="v ' + mCls + '">' + mTxt + '</td>' +
        '<td class="v"><b>' + (x.score == null ? '-' : fmtNum(x.score)) + '</b> / ' + x.max + '</td></tr>';
    }).join('');
    return '<div class="score-card-head"><h4>' + fa.title + '</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>造假分</span><b>' + (fa.total == null ? '-' : fmtNum(fa.total)) + '</b><i>' + fraudGradeText(g) + '</i></div></div>' +
      '<p class="score-basis">' + fa.basis + '</p>' +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>红旗指标</th><th>当前值</th><th>安全阈值</th><th>严重度</th><th>得分</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<p class="score-note">' + fa.note + '</p>' + eventEnhFooter(ov, fa.total, 'fraud');
  }

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
    var roe = last ? (last['净资产收益率'] != null ? last['净资产收益率'] : last['净资产收益率-摊薄']) : null;
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

    // 7. 现金分红率 = 最近一次每股分红 ÷ 最近年报每股收益（股东回报意愿）；异常高（>150%）视为口径不可比置空
    var payout = null;
    var divs = (d.dividends || []).filter(function (x) { return x.bonus_per_10 != null && x.bonus_per_10 > 0; });
    if (divs.length && eps != null && eps > 0) {
      payout = (divs[0].bonus_per_10 / 10) / eps;
      if (payout > 1.5) payout = null;
    }

    // 8. 治理与诚信（造假风险反向）：造假分越低越诚信，管理越透明
    var fraud = null;
    try { fraud = fraudAnalysis(d).total; } catch (e) { /* 造假分缺失不影响其余维度 */ }

    var items = [
      it(feeLabel, fmtPct(feeRatio), '≤ 10%', 15, lerpScore(feeRatio, 0.10, 0.30, 15, 0)),
      it('资产运营：总资产周转率（营收÷总资产）', turnover == null ? '-' : fmtNum(turnover), '≥ 1.0', 10, lerpScore(turnover, 0.2, 1.0, 0, 10)),
      it('资本回报：净资产收益率（年报）', fmtPct(roe), '≥ 15%', 20, lerpScore(roe, 0, 0.15, 0, 20)),
      it('成长质量：营收约5年CAGR', fmtPct(revCagr), '≥ 10%', 10, lerpScore(revCagr, 0, 0.10, 0, 10)),
      it('营运资金：（应收＋存货）÷营收', fmtPct(wc), '≤ 15%', 10, lerpScore(wc, 0.15, 0.45, 10, 0)),
      it('现金流质量：近5年累计净现比（经营现金流÷净利润）', cashRatio == null ? '-' : fmtNum(cashRatio), '≥ 1.0', 15, lerpScore(cashRatio, 0, 1.0, 0, 15)),
      it('股东回报：现金分红率（每股分红÷每股收益）', fmtPct(payout), '≥ 50%', 10, lerpScore(payout, 0, 0.50, 0, 10)),
      it('治理诚信：财报造假风险反向（100−造假分）', fraud == null ? '-' : fmtNum(fraud), '造假分 0（最诚信）', 10, fraud == null ? null : lerpScore(fraud, 0, 100, 10, 0))
    ];
    // 归一化：本分越高越好，缺项若只是少几笔加分，数据不全的公司就被系统性压低
    var total = weightedTotal(items.map(function (x) { return [x.score, x.max]; }));
    return {
      title: '管理层管理水平 · 投入产出效率量化',
      basis: '评分基准：' + (lastYear || '-') + ' 年报（比率类按年报口径，成长/现金流按近5年累计，分红取最近一次）',
      total: total == null ? null : Math.round(total * 10) / 10,
      items: items,
      note: '借鉴 DEA 数据包络分析的“投入→产出”效率思想，将管理层能力拆为 8 个可量化维度加权 0~100 分：费用纪律/资产周转/资本回报/成长质量/营运资金/现金流质量/股东回报/治理诚信，分数越高管理水平越好。纯 DEA 相对效率依赖同行业大样本且为黑盒、无法逐项展示，故采用其效率内核的透明加权替代；总分按“可算出的项”归一，缺数据的项不会把公司整体压低。本分为量化参考，需结合公司治理结构、股权激励、管理层履历等定性信息综合判断。'
    };
  }

  // 管理层评分卡（与 scoreCard 同构，等级方向高分=好=绿；符合度列 = 得分/权重）
  function managementCard(ma, ov) {
    var g = gradeOf(ma.total);
    var effMax = 0, missN = 0;
    ma.items.forEach(function (x) { if (x.score != null) effMax += x.max; else missN += 1; });
    var rows = ma.items.map(function (x) {
      var mCls = x.match == null ? 'sc-na' : x.match >= 0.99 ? 'sc-good' : x.match >= 0.5 ? 'sc-mid' : 'sc-low';
      var mTxt = x.match == null ? '-' : (x.match * 100).toFixed(0) + '%';
      return '<tr><td>' + x.std + '</td><td class="v">' + x.val + '</td><td class="v">' + x.thr + '</td>' +
        '<td class="v ' + mCls + '">' + mTxt + '</td>' +
        '<td class="v"><b>' + (x.score == null ? '-' : fmtNum(x.score)) + '</b> / ' + x.max + '</td></tr>';
    }).join('');
    return '<div class="score-card-head"><h4>' + ma.title + '</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>管理分</span><b>' + (ma.total == null ? '-' : fmtNum(ma.total)) + '</b><i>' + gradeText(g) + '</i></div></div>' +
      '<p class="score-basis">' + ma.basis + (missN ? '；⚠ ' + missN + ' 项数据缺失未计分，本卡按有效满分 ' + effMax + '/100 折算' : '') + '</p>' +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>评判维度</th><th>当前值</th><th>参考阈值</th><th>符合度</th><th>得分</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<p class="score-note">' + ma.note + '</p>' + eventEnhFooter(ov, ma.total, 'mgmt');
  }

  /* ---------------- ⑨ 公司事件与股东结构（一次性 Wind 明细） ---------------- */

  // 事件明细是否含任意非空数据（决定 ⑧ 后是否插入 ⑨ 模块）
  function evHasAny(evs) {
    if (!evs) return false;
    var e = evs.events || {}, h = evs.holders || {}, k;
    for (k in e) if (e[k] && e[k].length) return true;
    for (k in h) if (h[k] && h[k].length) return true;
    return false;
  }

  // 从一行记录里按列名优先级取值（列名带前缀且各表不一）。
  // 先精确后子串：防“最新一期机构持股比例”被子串匹配抢到含它的“…合计”列
  function pickVal(rec, subs) {
    var i, k, v;
    for (i = 0; i < subs.length; i++) {
      v = rec[subs[i]];
      if (v !== null && v !== undefined && v !== '') return v;
    }
    for (i = 0; i < subs.length; i++) {
      for (k in rec) {
        if (k.indexOf(subs[i]) >= 0) {
          v = rec[k];
          if (v !== null && v !== undefined && v !== '') return v;
        }
      }
    }
    return '';
  }

  // 十大/流通股东表为“最新 vs 上期”成对展开，按名次去重并排序
  function dedupeRank(recs) {
    var seen = {}, out = [];
    (recs || []).forEach(function (r) {
      var rk = pickVal(r, ['名次']);
      var key = rk !== '' ? 'r' + rk : JSON.stringify(r);
      if (!seen[key]) { seen[key] = 1; out.push(r); }
    });
    out.sort(function (a, b) {
      return (Number(pickVal(a, ['名次'])) || 99) - (Number(pickVal(b, ['名次'])) || 99);
    });
    return out;
  }

  // 通用事件子表：cols=[{label, keys:[子串] | get:fn(rec)->html(需自行转义), cls}]；超出 cap 折叠提示
  function evTable(title, recs, cols, opts) {
    if (!recs || !recs.length) return '';
    opts = opts || {};
    var cap = opts.cap || 12;
    var shown = recs.slice(0, cap);
    var h = '<div class="ev-block"><h4>' + title + '<span class="ev-n">' + recs.length + ' 条</span></h4>' +
      '<div class="stock-compare-wrap"><table class="stock-compare ev-tbl"><thead><tr>' +
      cols.map(function (c) { return '<th>' + c.label + '</th>'; }).join('') + '</tr></thead><tbody>';
    shown.forEach(function (r) {
      h += '<tr>' + cols.map(function (c) {
        var v = c.get ? c.get(r) : esc(pickVal(r, c.keys));
        return '<td class="' + (c.cls || '') + '">' + (v === '' || v == null ? '-' : v) + '</td>';
      }).join('') + '</tr>';
    });
    h += '</tbody></table></div>';
    if (recs.length > cap) h += '<p class="ev-more">仅显示前 ' + cap + ' 条（共 ' + recs.length + ' 条）</p>';
    h += '</div>';
    return h;
  }

  // 概览横幅：把覆盖层里的关键事件事实浓缩成可点读的标签（缺项不显示）
  function evOverview(ov, evs) {
    var chips = [];
    if (ov) {
      if (ov.penaltyCount) chips.push('<span class="ev-chip ev-chip-bad">违规处罚 ' + ov.penaltyCount + ' 条</span>');
      if (ov.defendantLawsuit) chips.push('<span class="ev-chip ev-chip-bad">被告涉案 ' + fmtNum(ov.defendantLawsuit) + ' 万</span>');
      if (ov.st) chips.push('<span class="ev-chip ev-chip-bad">ST / 风险警示</span>');
      if (ov.reduceFlag) chips.push('<span class="ev-chip ev-chip-bad">大股东/董监高减持</span>');
      if (ov.unlockRatio != null) chips.push('<span class="ev-chip' + (ov.unlockRatio >= 20 ? ' ev-chip-warn' : '') + '">未来解禁占比 ' + fmtNum(ov.unlockRatio) + '%</span>');
      if (ov.instHold != null) chips.push('<span class="ev-chip ev-chip-good">机构持股合计 ' + fmtNum(ov.instHold) + '%</span>');
    }
    var ac = ((evs.holders || {}).actual_controller || [])[0];
    if (ac) {
      var nm = pickVal(ac, ['实际控制人名称', '疑似实际控制人', '实际控制人']);
      if (nm) chips.push('<span class="ev-chip">实际控制人：' + esc(nm) + '</span>');
    }
    return chips.length ? '<div class="ev-chips">' + chips.join('') + '</div>' : '';
  }

  // ⑨ 主体：事件明细五表 + 股东结构（前十大/机构/实控人/解禁）
  function renderEvents(evs, ov) {
    if (!evHasAny(evs)) return '';
    var e = evs.events || {}, hd = evs.holders || {};
    var short = evs.name || '';
    var h = '<div class="va-events">';
    h += '<p class="ev-src">数据源：一次性 Wind 结构化事件与股东数据（抓取于 ' + esc(fmtDate(evs.fetched_at)) + '），不随每日行情/财报更新。</p>';
    h += evOverview(ov, evs);

    // 增减持（仅保留有“方向”值的记录，误路由的十大变动表无方向列自动排除）
    var inc = (e.increase_hold || []).filter(function (r) { return pickVal(r, ['方向']) !== ''; });
    h += evTable('重要股东增减持（近 1 年）', inc, [
      { label: '股东', keys: ['增减持股东姓名', '股东姓名'] },
      { label: '方向', get: function (r) { var d = pickVal(r, ['方向']); return '<span class="' + (/减|卖/.test(d) ? 'ev-bad' : 'ev-good') + '">' + esc(d) + '</span>'; } },
      { label: '数量', keys: ['增减持数量'] },
      { label: '变动后持股', keys: ['增减持后股东持股'] },
      { label: '报告期', keys: ['报告期'] }
    ]);

    h += evTable('并购重组', e.ma, [
      { label: '标题', keys: ['并购事件标题', '标题'] },
      { label: '标的方', keys: ['标的方名称'] },
      { label: '出让方', keys: ['出让方名称'] },
      { label: '类型', keys: ['并购类型'] },
      { label: '最新进度', keys: ['最新进度'] },
      { label: '披露日', keys: ['最新披露日'] }
    ]);

    h += evTable('违规处罚', e.penalty, [
      { label: '发生日期', keys: ['发生日期'] },
      { label: '违规行为', keys: ['违规行为'] },
      { label: '原因类型', keys: ['违规原因类型'] },
      { label: '决定机构', keys: ['决定机构'] },
      { label: '罚款金额', keys: ['罚款金额'] }
    ], { cap: 12 });

    // 司法诉讼：本公司作为被告的行高亮（简称子串匹配）
    h += evTable('司法诉讼', e.lawsuit, [
      { label: '案件名称', keys: ['案件名称'] },
      { label: '类型', keys: ['诉讼仲裁类型', '类型'] },
      { label: '原告', keys: ['原告方'] },
      { label: '被告', cls: 'ev-def', get: function (r) { var d = pickVal(r, ['被告方']); return short && String(d).indexOf(short) >= 0 ? '<b class="ev-bad">' + esc(d) + '</b>' : esc(d); } },
      { label: '涉案金额(万元)', cls: 'ev-num', keys: ['涉案金额'] }
    ], { cap: 12 });

    h += evTable('ST / 风险警示变动', e.st_change, [
      { label: '日期', keys: ['日期', '变动日'] },
      { label: '类型', keys: ['类型', '状态'] },
      { label: '说明', keys: ['实施', '原因', '说明', '类型'] }
    ]);

    // 前十大股东（优先十大股东，回落流通股东）
    var top = dedupeRank((hd.top10 && hd.top10.length) ? hd.top10 : hd.top10_float);
    h += evTable('前十大股东', top, [
      { label: '名次', keys: ['名次'] },
      { label: '股东名称', keys: ['最新一期十大股东名称', '最新一期前十大流通股东名称', '股东名称'] },
      { label: '持股比例', cls: 'ev-num', keys: ['最新一期十大股东持股比例', '最新一期前十大流通股东持股比例', '持股比例'] },
      { label: '持股数量', cls: 'ev-num', keys: ['最新一期十大股东持股数量', '最新一期前十大流通股东持股数量', '持股数量'] },
      { label: '较上期变动', cls: 'ev-num', keys: ['股东持股数量变动'] }
    ], { cap: 10 });

    // 机构持股（去名次重后取前几家；合计比例见概览）
    var inst = dedupeRank(hd.institutions);
    h += evTable('机构持股（前几家）', inst, [
      { label: '名次', keys: ['名次'] },
      { label: '机构名称', keys: ['最新一期机构股东名称', '机构股东名称'] },
      { label: '持股比例', cls: 'ev-num', keys: ['最新一期机构持股比例', '机构持股比例'] },
      { label: '持股数量', cls: 'ev-num', keys: ['最新一期机构持股数量', '机构持股数量'] },
      { label: '较上期变动', cls: 'ev-num', keys: ['机构持股数量变动', '机构持股比例变动'] }
    ], { cap: 10 });

    h += evTable('限售解禁', hd.unlock, [
      { label: '解禁日期', keys: ['解禁日期', '日期'] },
      { label: '解禁数量', cls: 'ev-num', keys: ['解禁数量', '数量'] },
      { label: '占总股本/流通股比例', cls: 'ev-num', keys: ['比例'] },
      { label: '股东名称', keys: ['股东名称', '名称'] }
    ]);

    h += '</div>';
    return h;
  }

  /* ---------------- ⑩ 股本事件：定增 / 回购（东财 datacenter 全市场快照） ---------------- */

  // 三笔里任一笔有值就出这一节；港美股与源侧无记录的公司 actions 整体为 null，整节不出
  function hasActions(a) {
    return !!a && !!(a.seo || a.buy_latest || a.buy_done);
  }

  // 只有一笔的表：摊成十列的横表没人看得完，改成一列标签一列值的竖排
  function acKv(pairs) {
    var h = '<div class="stock-compare-wrap"><table class="stock-compare ac-tbl"><tbody>';
    pairs.forEach(function (p) {
      h += '<tr><th>' + p[0] + '</th><td class="' + (p[2] || '') + '">'
        + (p[1] == null || p[1] === '' ? '-' : p[1]) + '</td></tr>';
    });
    return h + '</tbody></table></div>';
  }

  function acRange(lo, hi, f) {
    if (hi == null) return lo == null ? '-' : '≥' + f(lo);
    if (lo == null) return '≤' + f(hi);
    return lo === hi ? f(hi) : f(lo) + '~' + f(hi);
  }

  // 定增一笔 → 竖表（way/lockin/target 等附注字段由接口在详情里额外给出）
  function acSeoBlock(v) {
    if (!v) return '';
    var shares = function (b, a2) {
      if (b == null && a2 == null) return null;
      return (b == null ? '-' : fmtMoney(b)) + ' → ' + (a2 == null ? '-' : fmtMoney(a2)) + ' 股';
    };
    return '<div class="ev-block"><h4>最近一次定向增发</h4>' +
      acKv([
        ['发行日（缺失取上市日）', esc(fmtDate(v.date))],
        ['发行价（元/股）', '<b>' + fmtNum(v.price) + '</b>'],
        ['发行量', fmtNum(v.num) + ' 股'],
        ['募集资金', fmtNum(v.amount) + ' 元'],
        ['发行方式', esc(v.way)],
        ['新增股份锁定期', esc(v.lockin)],
        ['发行前后股本', shares(v.share_before, v.share_after)],
        ['认购价 / 发行前收盘价', v.apply_price == null && v.price_before == null
          ? null : fmtNum(v.apply_price) + ' / ' + fmtNum(v.price_before)],
        ['发行市场', esc(v.market)],
        ['发行对象', esc(v.target), 'ac-long']
      ]) + '</div>';
  }

  function acBuyRows(a) {
    var latest = a.buy_latest, done = a.buy_done, rows = [];
    function mk(tag, b, dateKind) {
      return {
        tag: tag, date: b.date, dateKind: dateKind, progress: b.progress,
        price: fmtNum(b.price), priceKind: b.price == null ? '缺失' : b.price_actual ? '成交均价' : '方案上限',
        low: b.done_price_low, high: b.done_price_high,
        num: (b.num_actual ? fmtNum(b.num) : acRange(b.num_lo, b.num, fmtNum)) + ' 股',
        numKind: b.num_actual ? '累计成交' : '计划',
        amount: (b.amount_actual ? fmtNum(b.amount) : acRange(b.amount_lo, b.amount, fmtNum)) + ' 元',
        amountKind: b.amount_actual ? '累计成交' : '计划',
        cx: b.cancel_type || '待核', purpose: b.purpose,
        evidence: b.evidence, objective: b.objective
      };
    }
    if (latest && done && done.src_id === latest.src_id) return [mk('最新且已完成', done, '完成日')];
    if (latest) rows.push(mk('最新公告', latest, '公告日'));
    if (done) rows.push(mk('最近已完成', done, '完成日'));
    return rows;
  }

  function acBuyBlock(a) {
    var rows = acBuyRows(a);
    if (!rows.length) return '';
    var h = evTable('最近一次回购', rows, [
      { label: '笔', cls: 'ac-tag', get: function (r) { return esc(r.tag); } },
      { label: '进度', get: function (r) { return esc(r.progress || '-'); } },
      { label: '日期', get: function (r) { return esc(fmtDate(r.date)) + '<i class="ac-dk">' + r.dateKind + '</i>'; } },
      { label: '价格(元/股)', cls: 'ev-num', get: function (r) {
        return '<b>' + r.price + '</b><i class="ac-dk">' + r.priceKind + '</i>'
          + (r.low != null && r.high != null
            ? '<i class="ac-dk">成交区间 ' + fmtNum(r.low) + '~' + fmtNum(r.high) + '</i>' : '');
      } },
      { label: '数量', cls: 'ev-num', get: function (r) { return esc(r.num) + '<i class="ac-dk">' + r.numKind + '</i>'; } },
      { label: '金额', cls: 'ev-num', get: function (r) { return esc(r.amount) + '<i class="ac-dk">' + r.amountKind + '</i>'; } },
      { label: '是否注销（用途）', get: function (r) { return esc(r.cx); } },
      { label: '用途', get: function (r) { return esc(r.purpose || '-'); } }
    ], { cap: 2 });
    rows.forEach(function (r) {
      h += '<p class="ac-note">' + esc(r.tag) + '·判据：' + esc(r.evidence || '未取得明确的处置判据，需核对公告') + '</p>';
      if (r.objective) h += '<details class="ac-note"><summary>' + esc(r.tag) + '·方案原文</summary><p>' + esc(r.objective) + '</p></details>';
    });
    return h;
  }

  function renderActions(a) {
    if (!hasActions(a)) return '';
    var h = '<div class="va-actions">';
    h += '<p class="ev-src">来源：东方财富，仅 A 股；历史价格未复权，仅展示，不参与评分和买卖点。'
      + '定增只指非公开发行，公开增发与配股不计。价、量、金额各自优先取累计成交值，缺项显示计划值。'
      + '发行日缺失取上市日；公告日缺失取决议日；完成日缺失取公告日。'
      + '「注销/非注销/待核」按公告中的股份用途分类，不代表已办理注销；回购完成不等于注销完成。</p>';
    h += a.seo ? acSeoBlock(a.seo) : '<p class="ac-note">定增：-（未收录记录）</p>';
    h += acBuyBlock(a) || '<p class="ac-note">回购：-（未收录记录）</p>';
    return h + '</div>';
  }

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
    // 1b 利润深度下滑频率：年度净利同比 ≤ -30% 的年数（同比自算，与报表口径一致）
    // 此处只在基期为正时计算：“下滑 30%”对盈利基数才有意义；若把亏损扩大也算进来，
    // 尚未盈利的生物医药/新经济公司会被误判为周期性行业（实测 166 家误判）。
    // 阶段二“利润动能”用 yoyOf（基期为负按 |基期|）——那里问的是“是否在离开底部”，亏损收窄正是信号。
    var drops = 0, hitDrop = false;
    for (var ci = 1; ci < annual.length; ci++) {
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
    var qrev = qRows.map(function (r) { return r['营业总收入_单季']; });
    var qoq = (qrev.length >= 2 && qrev[qrev.length - 2] != null && qrev[qrev.length - 2] > 0 && qrev[qrev.length - 1] != null)
      ? qrev[qrev.length - 1] / qrev[qrev.length - 2] - 1 : null;

    var items = [
      it('利润位置：最新年报净利在近' + w8.length + '年区间的位置', netLast == null ? '-' : fmtMoney(netLast), '接近最低分→底部', 25, netPct == null ? null : netPct * 25),
      it('利润动能：最新年报净利同比', netYoy == null ? '-' : fmtPct(netYoy), '≤ -50% 底部', 15, lerpScore(netYoy, -0.50, 0.30, 0, 15)),
      it('毛利率位置：最新年报毛利率在近' + w8.length + '年区间的位置', gmLast == null ? '-' : fmtPct(gmLast), '接近最低分→底部', 15, gmPct == null ? null : gmPct * 15),
      it('营收位置：最新年报营收在近' + w8.length + '年区间的位置', revLast == null ? '-' : fmtMoney(revLast), '接近最低分→底部', 10, revPct == null ? null : revPct * 10),
      it('现金流压力：最新年报净现比（经营现金流÷净利润）', ncr == null ? '-' : fmtNum(ncr), '≤ 0 底部', 10, lerpScore(ncr, 0, 1.2, 0, 10)),
      it('库存周期：存货同比（去库存→接近底部）', invGrow == null ? '-' : fmtPct(invGrow), '≤ -10% 去库存', 10, lerpScore(invGrow, -0.10, 0.20, 0, 10)),
      it('资本开支周期：当年购建支出÷近3年均值（收缩→出清）', capexRatio == null ? '-' : fmtNum(capexRatio), '≤ 0.7 收缩', 10, lerpScore(capexRatio, 0.7, 1.3, 0, 10)),
      it('单季环比：最新单季营收环比（回升→离开底部）', qoq == null ? '-' : fmtPct(qoq), '≤ -10% 仍在探底', 10, lerpScore(qoq, -0.10, 0.05, 0, 10))
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
  function cycleGradeText(g) {
    return { good: '底部区域', mid: '磨底过渡', low: '周期中段', bad: '景气偏高', na: '不适用' }[g];
  }

  // 周期评分卡：非周期性公司显示判定依据表；周期性公司另加周期强度表 + 8 维位置表（低分=绿）
  function cycleCard(ca) {
    function dimRows(items) {
      return items.map(function (x) {
        var mCls = x.match == null ? 'sc-na' : x.match >= 0.99 ? 'sc-good' : x.match >= 0.5 ? 'sc-mid' : 'sc-low';
        var mTxt = x.match == null ? '-' : (x.match * 100).toFixed(0) + '%';
        return '<tr><td>' + x.std + '</td><td class="v">' + x.val + '</td><td class="v">' + x.thr + '</td>' +
          '<td class="v ' + mCls + '">' + mTxt + '</td>' +
          '<td class="v"><b>' + (x.score == null ? '-' : fmtNum(x.score)) + '</b> / ' + x.max + '</td></tr>';
      }).join('');
    }
    var head;
    if (!ca.cyclical) {
      head = '<div class="score-circle va-grade-na"><span>周期</span><b>非周期</b><i>不适用</i></div>';
    } else {
      var g = fraudGradeOf(ca.total);
      head = '<div class="score-circle va-grade-' + g + '"><span>周期位</span><b>' + (ca.total == null ? '-' : fmtNum(ca.total)) + '</b><i>' + cycleGradeText(g) + '</i></div>';
    }
    var tableHead = '<thead><tr><th>指标</th><th>当前值</th><th>参考阈值</th><th>符合度</th><th>得分</th></tr></thead>';
    var h = '<div class="score-card-head"><h4>' + ca.title + '</h4>' + head + '</div>' +
      '<p class="score-basis">' + ca.basis + '</p>';
    if (ca.cyclical) {
      h += '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">阶段一：周期强度判定（' + (ca.cyclicalScore == null ? '-' : fmtNum(ca.cyclicalScore)) + ' / 40 即判为周期性）</h4>' +
        '<table class="stock-compare">' + tableHead + '<tbody>' + dimRows(ca.cyclicalItems) + '</tbody></table></div>';
    }
    if (ca.items.length) {
      h += '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">阶段二：周期位置评分（分数越低越接近周期底部）</h4>' +
        '<table class="stock-compare">' + tableHead + '<tbody>' + dimRows(ca.items) + '</tbody></table></div>';
    } else {
      h += '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">阶段一：周期强度判定明细</h4>' +
        '<table class="stock-compare">' + tableHead + '<tbody>' + dimRows(ca.cyclicalItems) + '</tbody></table></div>';
    }
    return h + '<p class="score-note">' + ca.note + '</p>';
  }

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
    var qrev = qRows.map(function (r) { return r['营业总收入_单季']; });
    var qoq = (qrev.length >= 2 && qrev[qrev.length - 2] != null && qrev[qrev.length - 2] > 0 && qrev[qrev.length - 1] != null)
      ? qrev[qrev.length - 1] / qrev[qrev.length - 2] - 1 : null;
    // 按年归档单季营收（升序，保留 null），供逐年环比：各年取该年最后两个单季（Q3/Q2）
    var interimByYear = {};
    qRows.forEach(function (r) {
      var yr = String(r['报告期'] || '').slice(0, 4);
      (interimByYear[yr] = interimByYear[yr] || []).push(r['营业总收入_单季']);
    });
    function yearQoq(yr) {
      var vals = interimByYear[String(yr)] || [];
      if (vals.length >= 2 && vals[vals.length - 2] != null && vals[vals.length - 2] > 0 && vals[vals.length - 1] != null) {
        return vals[vals.length - 1] / vals[vals.length - 2] - 1;
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
        lerpScore(qoqI, -0.10, 0.05, 0, 10)
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
  function renderCycleChart(d, ca) {
    var block = $('stock-cycle-chart-block');
    var el = $('stock-chart-cycle');
    if (!block || !el) return;
    if (!ca.cyclical || typeof echarts === 'undefined') { block.style.display = 'none'; return; }
    var hist = cycleHistory(d).filter(function (x) { return x.score != null; });
    if (hist.length < 2) { block.style.display = 'none'; return; }
    block.style.display = '';
    var chart = echarts.init(el);
    state.charts.push(chart);
    chart.setOption({
      grid: mqMobile.matches ? { left: 38, right: 14, top: 28, bottom: 26 } : { left: 44, right: 18, top: 32, bottom: 30 },
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return v == null ? '-' : v + ' 分'; } },
      xAxis: { type: 'category', data: hist.map(function (x) { return x.year; }) },
      yAxis: { type: 'value', min: 0, max: 100, name: '周期位置分', splitLine: { lineStyle: { type: 'dashed' } } },
      series: [{
        name: '周期位置分', type: 'line', data: hist.map(function (x) { return x.score; }),
        smooth: false, symbol: 'circle', symbolSize: 8,
        label: { show: true, position: 'top', fontSize: 10 },
        itemStyle: { color: '#b07a10' }, lineStyle: { width: 2 },
        markLine: { symbol: 'none', silent: true, label: { formatter: '底部区 ≤ 30', position: 'insideEndTop', fontSize: 10 },
          lineStyle: { color: '#1e7e44', type: 'dashed' }, data: [{ yAxis: 30 }] },
        markArea: { silent: true, itemStyle: { color: 'rgba(30,126,68,0.08)' }, data: [[{ yAxis: 0 }, { yAxis: 30 }]] }
      }]
    });
  }

  // ---- 价格参考（买入/保守卖出/公允卖出）----
  // 三档都锚定各流派评分项自己的阈值（常量见 valueScores 上方），不做任何反推，
  // 因此参考价只随财报与估值快照变动，不随「当前总分能不能凑到某个数」漂移。

  // 类现金加权口径（与 scoring.py 一致）：分项键 → [展示名, 折算系数, 附注字段]
  // 附注拆得出「定期存款」与「受限货币资金」时，货币资金与其他流动资产都只按可用部分折算，
  // 故展示名跟着第三项字段是否命中变；附注为空的分项不参与折算也不展示。
  var NET_CASH_W = [
    ['avail', '货币资金', 1, 'restricted'],
    ['fin', '交易性金融资产', 0.7],
    ['notes', '应收票据', 0.4],
    ['otherNonDep', '其他流动资产', 0.3, 'termDeposit'],
    ['termDeposit', '定期存款', 1]
  ];

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
  function fairPe(netCagr5) {
    if (netCagr5 == null) return 15;
    return Math.max(8, Math.min(25, netCagr5 * 100));
  }

  // 公允清算价值 + 四大流派买入/保守卖出/公允卖出价格参考（对应 scoring.py price_references）
  // fairLiq = 每股公允清算价值（流动资产合计-负债合计）/财报股本，格雷厄姆清算口径
  // netCashRatio = 净现金/市值（最近一期财报 加权类现金−负债合计 ÷ 快照总市值）
  function priceReferences(d, va) {
    var s = d.snapshot || {};
    var price0 = s.price, mcap0 = s.market_cap, pe0 = s.pe_ttm, pb0 = s.pb;
    var none = { fairLiq: null, buy: null, sellCons: null, sellFair: null };
    if (price0 == null || price0 <= 0) {
      return { fairLiq: null, netCashRatio: null, netCashCalc: null, grahamAgg: none, grahamDef: none, schloss: none, buffett: none };
    }
    // ---- 基础量（最新年报资产负债表）----
    var annual = annualRows(d.indicators || []);
    var last = annual[annual.length - 1];
    var lastDate = last ? String(last['报告期']).slice(0, 10) : null;
    var baList = (d.balance || []).slice().sort(function (a, b) { return cmpKey(a['报告日'], b['报告日']); });
    var lastBa = lastDate ? sheetRowByDate(baList, lastDate) : null;
    var ca = lastBa ? lastBa['流动资产合计'] : null;
    var tl = lastBa ? lastBa['负债合计'] : null;
    var ncav = (ca != null && tl != null) ? ca - tl : null;
    var lastEq = lastBa ? (lastBa['归属于母公司股东权益合计'] != null ? lastBa['归属于母公司股东权益合计'] : lastBa['所有者权益(或股东权益)合计']) : null;
    // 每股净资产优先用指标字段（数据源按财报算好、随财报更新，与实时价无关），
    // 避免快照 pb/pe 舍入与 mcap 滞后导致参考价随行情漂移（财务无变化时参考价应不变）
    var bps = latestField(d.indicators, '每股净资产');
    // 股本优先用财报实收资本（最新年报），快照 mcap/price 会随实时价抖动（快照舍入/滞后）
    var shares = shareCount(d.balance, mcap0 != null ? mcap0 / price0 : null, bps);
    var ncavPs = (ncav != null && shares) ? ncav / shares : null;   // 每股净流动资产
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
    // 类现金保守折算：可用货币资金×1.0 ＋ 交易性金融资产×0.7 ＋ 应收票据×0.4
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
    var weightedCash = wgt(ncParts.avail, 1) + wgt(ncParts.fin, 0.7) + wgt(ncParts.notes, 0.4) + wgt(ncParts.otherNonDep, 0.3) + wgt(ncParts.termDeposit, 1);
    var hasCore = cashV != null && tlLatest != null && mcap0;
    var netCashRatio = hasCore ? (weightedCash - tlLatest) / mcap0 : null;
    var netCashCalc = hasCore ? {
      cash: cashV, fin: finV, notes: notesV, otherCA: otherV,
      tl: tlLatest, mcap: mcap0,
      report: baDay || null,
      termDeposit: ncParts.termDeposit, restricted: rstV,
      noteReport: noteFound ? noteFound[0] : null
    } : null;
    var fpe = fairPe(va.netCagr5);
    // 低于一分钱（含负值与浮点零渣）的参考价一律视为无
    function ref(v) { return (v != null && v >= MIN_PRICE_REF) ? v : null; }
    // 资产派卖价只在估值射程内给出：现价已在 anchor 的 SELL_BAND 倍之外，说明该派的资产口径
    // 对这只股票没有「该卖」的意见（它从来不是这只股票的估值方法），留空。
    // 现价无快照的情形已在函数开头整体返回空，这里不必再判。
    function band(anchor) { return (anchor == null || price0 <= SELL_BAND * anchor) ? anchor : null; }
    var ncavRef = ref(ncavPs);
    var sRef = ref(bps);
    // TTM 每股亏损（≤0）时基于 EPS 的估值锚无意义，锚位与买入价一并置空（避免负价/误导价）
    var epsOk = epsTtm != null && epsTtm > 0;
    var gACons = band(ncavRef);
    var gDCons = epsOk ? ref(G_D_PE_FULL * epsTtm) : null;
    var sCons = band(sRef);
    var bCons = epsOk ? ref(fpe * epsTtm) : null;
    // 公允卖价跟着保守卖价同生同灭：每派公允都是保守的 ≥1.3 倍，保守过了一分钱下限
    // 公允必然也过；但若各自独立套下限，会在保守差一点、公允刚过点时只留半档，
    // 而卖点筛选要求「同时 ≥ 保守与公允」，半档等于把这家公司永久排除。
    // 买点不挂卖点：锚为空才没有买点；资产派「出射程」只抹掉两档卖价，买点作为目标价照旧给出。
    return {
      fairLiq: ncavRef,
      netCashRatio: netCashRatio,
      netCashCalc: netCashCalc,
      grahamAgg: {
        buy: (ncavRef != null) ? ref(G_A_PNCAV_FULL * ncavRef) : null,
        sellCons: gACons,
        sellFair: (gACons != null) ? 1.5 * gACons : null
      },
      grahamDef: {
        buy: (gDCons != null) ? ref(BUY_MARGIN * gDCons) : null,
        sellCons: gDCons,
        sellFair: (gDCons != null) ? 20 * epsTtm : null
      },
      schloss: {
        buy: (sRef != null) ? ref(S_PB_FULL * sRef) : null,
        sellCons: sCons,
        sellFair: (sCons != null) ? 1.5 * sCons : null
      },
      buffett: {
        buy: (bCons != null) ? ref(BUY_MARGIN * bCons) : null,
        sellCons: bCons,
        sellFair: (bCons != null) ? fpe * epsTtm * 1.3 : null
      }
    };
  }

  // 渲染四大评分卡（格雷厄姆进取/防御、施洛斯、巴菲特芒格）
  function renderScores(sc) {
    var curPrice = ((sc.d || {}).snapshot || {}).price;
    var refs = sc.priceRefs || {};
    var cards = [
      ['stock-score-graham-agg', sc.grahamAgg, refs.grahamAgg],
      ['stock-score-graham-def', sc.grahamDef, refs.grahamDef],
      ['stock-score-schloss', sc.schloss, refs.schloss],
      ['stock-score-buffett', sc.buffett, refs.buffett]
    ];
    cards.forEach(function (trio) {
      var el = $(trio[0]);
      // 缺维标注：不计分项要说明是「数据没到」还是「报表结构里没这个科目」，
      // 并标注有效满分——总分不是同口径 100 分制（跨市场可比性）
      var eff = trio[1].eff;
      var gap = eff && (eff.miss || eff.na) ? '；⚠ ' + [
        eff.miss ? eff.miss + ' 项数据缺失未计分' : '',
        eff.na ? eff.na + ' 项该公司报表无此科目（不适用，非缺数据）' : ''
      ].filter(Boolean).join('，') + '，本卡按有效满分 ' + eff.max + '/100 折算' : '';
      var basis = sc.basis + gap;
      if (el) el.innerHTML = scoreCard(trio[1].title, basis, trio[1].total, trio[1].items, trio[1].note, trio[2], curPrice);
    });
  }

  // 模块锚点导航：平滑滚动（避免与 #/code 路由冲突）
  function bindVaNav() {
    document.querySelectorAll('.va-nav a').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        var el = $(a.getAttribute('data-scroll'));
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  // 每股派息柱状图 + 净现比柱状图（renderDetail 时重建实例，随 state.charts 统一销毁）
  function renderVaCharts(va) {
    if (typeof echarts === 'undefined') return;
    if (!state.charts.length) return; // 图表区未初始化时也不建（正常流程 charts 已由 renderCharts 建立）

    var divData = va.divChart.map(function (r) { return r.value; });
    var divYears = va.divChart.map(function (r) { return String(r.year); });
    var ch1 = echarts.init($('stock-chart-dividend'));
    state.charts.push(ch1);
    ch1.setOption({
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return v == null ? '-' : v + ' 元'; } },
      grid: mqMobile.matches ? { left: 40, right: 10, top: 22, bottom: 26 } : { left: 46, right: 16, top: 24, bottom: 30 },
      xAxis: { type: 'category', data: divYears, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', axisLabel: { fontSize: 11, formatter: function (v) { return v + ' 元'; } } },
      series: [{ type: 'bar', barMaxWidth: 28, itemStyle: { color: '#61c0a8' },
        data: divData, label: { show: true, position: 'top', fontSize: 10, formatter: function (p) { return p.value; } } }]
    });

    var cf5 = va.cfRows;
    var ch2 = echarts.init($('stock-chart-netcash'));
    state.charts.push(ch2);
    ch2.setOption({
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return v == null ? '-' : v; } },
      grid: mqMobile.matches ? { left: 40, right: 10, top: 22, bottom: 26 } : { left: 46, right: 16, top: 24, bottom: 30 },
      xAxis: { type: 'category', data: cf5.map(function (r) { return r.year; }), axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', axisLabel: { fontSize: 11 } },
      series: [{
        name: '净现比', type: 'bar', barMaxWidth: 28,
        itemStyle: { color: '#5b8ff9' },
        data: cf5.map(function (r) { return r.ratio; }),
        markLine: { silent: true, symbol: 'none', lineStyle: { color: '#e8684a', type: 'dashed' },
          data: [{ yAxis: 1, label: { formatter: '净现比=1', fontSize: 10 } }] }
      }]
    });
  }

  function fmtPct(v) {
    if (v == null || isNaN(v)) return '-';
    return (v * 100).toFixed(2) + '%';
  }

  function kv(k, v) {
    return '<div class="kv"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>';
  }

  function sheetTab(key, label) {
    return '<button data-sheet="' + key + '">' + label + '</button>';
  }

  /* ---------------- 指标趋势图（3 个独立图表，支持季/年视图切换） ---------------- */

  // 具名监听 + 守卫：早先这里是 window.onresize 赋值，会直接覆盖农化/EDB 模块挂的 resize 处理
  var resizeBound = false;
  var wasNarrow = null;

  function resizeStockCharts() {
    state.charts.forEach(function (c) { try { c.resize(); } catch (e) {} });
  }

  function onStockResize() {
    // 推进宏任务：图表容器高度与折叠标记都要等这一轮 DOM 落地后再量
    setTimeout(function () {
      if (!resizeBound || !state.current) return;
      var n = mqMobile.matches;
      // grid 边距与移动端折叠结构都是渲染期烘死的，跨断点必须整体重建，单靠 resize 会留下桌面边距
      if (n !== wasNarrow) {
        wasNarrow = n;
        if ($('stock-detail-body')) renderDetail(state.current);
        return;
      }
      resizeStockCharts();
    }, 0);
  }

  // 详情路由卸载后容器消失，必须摘掉监听（StockDetailView 的 onBeforeUnmount 调用）
  function unbindResize() {
    if (!resizeBound) return;
    resizeBound = false;
    window.removeEventListener('resize', onStockResize);
  }

  function renderCharts(indicators) {
    if (typeof echarts === 'undefined') {
      $('stock-chart-revenue').innerHTML = '<p class="stock-hint">图表库加载失败（ECharts CDN 不可用）</p>';
      return;
    }
    var isYear = state.view === 'year';
    var rows = indicators.slice().reverse(); // 升序排列
    // 年视图仅保留年报（12-31）报告期
    var data = isYear
      ? rows.filter(function (r) { return String(r['报告期']).indexOf('12-31') >= 0; })
      : rows;
    var dates = data.map(function (r) { return fmtDate(r['报告期']); });

    // 标题与注释随视图切换
    $('stock-chart-title').textContent = isYear
      ? '关键指标趋势（年度口径）'
      : '关键指标趋势（近 ' + indicators.length + ' 期）';
    $('stock-chart-revenue-title').textContent = '营业总收入 & 净利润（' + (isYear ? '全年' : '单季') + '，' + yiUnit() + '）';
    $('stock-chart-margin-title').textContent = '销售毛利率 & 销售净利率（' + (isYear ? '年度' : '报告期') + '口径）';
    $('stock-chart-roe-title').textContent = '净资产收益率（' + (isYear ? '年度' : '各期累计') + '）';
    $('stock-chart-note').textContent = isYear
      ? '年度口径：柱为全年累计值，折线为年度同比（右轴 %）'
      : '季度口径：柱为单季值（本期累计 - 上期累计），折线为同比/环比（右轴 %，虚线=环比）；ROE 为报告期累计值';

    // 首次渲染创建实例，切换视图时复用并整体替换 option
    if (!state.charts.length) {
      ['stock-chart-revenue', 'stock-chart-margin', 'stock-chart-roe'].forEach(function (id) {
        state.charts.push(echarts.init($(id)));
      });
      wasNarrow = mqMobile.matches;
      if (!resizeBound) { resizeBound = true; window.addEventListener('resize', onStockResize); }
    }

    // 通用配置：图例 + 缩放条 + 双 Y 轴（左=指标值，右=增长率 %）
    // 默认显示窗口：年度=最近 3 年，季度=最近 8 个季度（可拖动缩放条查看全部）
    var zoomN = isYear ? 3 : 8;
    function baseOption(legendData, yName, yFormatter) {
      return {
        tooltip: {
          trigger: 'axis',
          valueFormatter: function (v) { return v == null ? '-' : (yFormatter ? yFormatter(v) : v); }
        },
        legend: { data: legendData, top: 0 },
        // bottom 两端都留 46：dataZoom 滑块要占位；移动端只收紧左右与顶部
        grid: mqMobile.matches ? { left: 46, right: 44, top: 30, bottom: 46 }
          : { left: 60, right: 56, top: 34, bottom: 46 },
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 11 } },
        yAxis: [
          { type: 'value', name: yName, axisLabel: { fontSize: 11, formatter: yFormatter } },
          {
            type: 'value', name: '增长率',
            axisLabel: { fontSize: 11, formatter: fmtPctAxis },
            splitLine: { show: false }
          }
        ],
        dataZoom: [
          { type: 'inside', startValue: data.length - zoomN, endValue: data.length - 1 },
          { type: 'slider', height: 14, bottom: 6, startValue: data.length - zoomN, endValue: data.length - 1 }
        ],
        series: []
      };
    }

    // 金额系列（亿元柱状；年视图取累计字段，季视图取单季字段）
    function barYiSeries(name, key, color) {
      return {
        name: name, type: 'bar', barMaxWidth: 22,
        itemStyle: { color: color },
        data: data.map(function (r) {
          return r[key] == null ? null : +(r[key] / 1e8).toFixed(2);
        })
      };
    }

    // 比率系列（% 折线，服务端已是小数）
    function pctLineSeries(name, key, color) {
      return {
        name: name, type: 'line', smooth: true,
        itemStyle: { color: color },
        data: data.map(function (r) {
          return r[key] == null ? null : +(r[key] * 100).toFixed(2);
        })
      };
    }

    function fmtPctAxis(v) { return v + '%'; }

    // 增长率折线（右轴 %）：同比=隔 N 期，环比=隔 1 期；虚线为环比
    function growthLine(name, field, color, step, dashed) {
      return {
        name: name, type: 'line', smooth: true, yAxisIndex: 1,
        symbolSize: 4,
        itemStyle: { color: color },
        lineStyle: { color: color, width: 1.5, type: dashed ? 'dashed' : 'solid' },
        data: data.map(function (r, i) {
          if (i < step) return null;
          var cur = r[field], prev = data[i - step][field];
          if (cur == null || prev == null || prev === 0) return null;
          return +((cur - prev) / Math.abs(prev) * 100).toFixed(1);
        })
      };
    }

    // 图 1：营业总收入 & 净利润（柱）+ 同比/环比增长率（折线，右轴 %）
    var revKey = isYear ? '营业总收入' : '营业总收入_单季';
    var netKey = isYear ? '净利润' : '净利润_单季';
    var opt1Legend = isYear
      ? ['营业总收入', '净利润', '营收同比', '净利同比']
      : ['营业总收入', '净利润', '营收同比', '净利同比', '营收环比', '净利环比'];
    var opt1 = baseOption(opt1Legend, yiUnit());
    opt1.series = [
      barYiSeries('营业总收入', revKey, '#5b8ff9'),
      barYiSeries('净利润', netKey, '#61c0a8'),
      growthLine('营收同比', revKey, '#5b8ff9', isYear ? 1 : 4, false),
      growthLine('净利同比', netKey, '#61c0a8', isYear ? 1 : 4, false)
    ];
    if (!isYear) {
      opt1.series.push(growthLine('营收环比', revKey, '#5b8ff9', 1, true));
      opt1.series.push(growthLine('净利环比', netKey, '#61c0a8', 1, true));
    }
    state.charts[0].setOption(opt1, true);

    // 图 2：销售毛利率 & 销售净利率
    var opt2 = baseOption(['销售毛利率', '销售净利率'], '%', fmtPctAxis);
    opt2.series = [
      pctLineSeries('销售毛利率', '销售毛利率', '#f6bd16'),
      pctLineSeries('销售净利率', '销售净利率', '#e8684a')
    ];
    state.charts[1].setOption(opt2, true);

    // 图 3：净资产收益率
    var opt3 = baseOption(['净资产收益率'], '%', fmtPctAxis);
    opt3.series = [pctLineSeries('净资产收益率', '净资产收益率', '#5b8ff9')];
    state.charts[2].setOption(opt3, true);
  }

  /* ---------------- 季/年视图切换 ---------------- */

  function bindViewToggle() {
    var btns = document.querySelectorAll('.stock-view-toggle button');
    btns.forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.classList.contains('active')) return;
        btns.forEach(function (x) { x.classList.toggle('active', x === b); });
        state.view = b.dataset.view;
        if (state.current) renderCharts(state.current.indicators || []);
      });
    });
  }

  /* ---------------- 财务对比（年报/季报，任意两个报告期） ---------------- */

  // 对比指标配置（type: amount=亿元相对变化, pct=百分点差, ratio/yuan=绝对差, days=天数差）
  // keySingle: 金额类在季报对比时改用单季字段（与图表季视图口径一致）
  // src: 数据来源（缺省 indicators；income/balance/cashflow 三大报表按报告日关联）
  //      金额类在季报对比时自动单季化（本期累计 - 上期累计），single:false 为时点值不单季化
  var ANNUAL_METRICS = [
    { group: '规模与成长', key: '营业总收入', keySingle: '营业总收入_单季', label: '营业总收入', type: 'amount' },
    { group: '规模与成长', key: '净利润', keySingle: '净利润_单季', label: '净利润', type: 'amount' },
    { group: '规模与成长', key: '扣非净利润', label: '扣非净利润', type: 'amount' },
    { group: '成长能力', key: '营业总收入同比增长率', label: '营收同比增长率', type: 'pct' },
    { group: '成长能力', key: '净利润同比增长率', label: '净利同比增长率', type: 'pct' },
    { group: '成长能力', key: '扣非净利润同比增长率', label: '扣非净利同比增长率', type: 'pct' },
    { group: '盈利能力', key: '销售毛利率', label: '销售毛利率', type: 'pct' },
    { group: '盈利能力', key: '销售净利率', label: '销售净利率', type: 'pct' },
    { group: '盈利能力', key: '净资产收益率', label: '净资产收益率', type: 'pct' },
    { group: '盈利能力', key: '净资产收益率-摊薄', label: '净资产收益率(摊薄)', type: 'pct' },
    { group: '偿债能力', key: '资产负债率', label: '资产负债率', type: 'pct' },
    { group: '偿债能力', key: '产权比率', label: '产权比率', type: 'ratio' },
    { group: '偿债能力', key: '流动比率', label: '流动比率', type: 'ratio' },
    { group: '偿债能力', key: '速动比率', label: '速动比率', type: 'ratio' },
    { group: '偿债能力', key: '保守速动比率', label: '保守速动比率', type: 'ratio' },
    { group: '费用与利润', src: 'income', key: '营业总成本', label: '营业总成本', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '营业成本', label: '营业成本', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '销售费用', label: '销售费用', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '管理费用', label: '管理费用', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '财务费用', label: '财务费用', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '研发费用', label: '研发费用', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '营业利润', label: '营业利润', type: 'amount' },
    { group: '费用与利润', src: 'income', key: '利润总额', label: '利润总额', type: 'amount' },
    { group: '资产与负债', src: 'balance', key: '资产总计', label: '资产总计', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '负债合计', label: '负债合计', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '所有者权益(或股东权益)合计', label: '所有者权益合计', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '归属于母公司股东权益合计', label: '归母股东权益', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '货币资金', label: '货币资金', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '存货', label: '存货', type: 'amount', single: false },
    { group: '资产与负债', src: 'balance', key: '应收账款', label: '应收账款', type: 'amount', single: false },
    { group: '现金流量', src: 'cashflow', key: '经营活动产生的现金流量净额', label: '经营现金流净额', type: 'amount' },
    { group: '现金流量', src: 'cashflow', key: '投资活动产生的现金流量净额', label: '投资现金流净额', type: 'amount' },
    { group: '现金流量', src: 'cashflow', key: '筹资活动产生的现金流量净额', label: '筹资现金流净额', type: 'amount' },
    { group: '现金流量', src: 'cashflow', key: '销售商品、提供劳务收到的现金', label: '销售商品收到现金', type: 'amount' },
    { group: '现金流量', src: 'cashflow', key: '期末现金及现金等价物余额', label: '期末现金及等价物', type: 'amount', single: false },
    { group: '每股与营运', key: '基本每股收益', label: '基本每股收益', type: 'yuan' },
    { group: '每股与营运', key: '每股净资产', label: '每股净资产', type: 'yuan' },
    { group: '每股与营运', key: '每股经营现金流', label: '每股经营现金流', type: 'yuan' },
    { group: '每股与营运', key: '每股未分配利润', label: '每股未分配利润', type: 'yuan' },
    { group: '每股与营运', key: '每股资本公积金', label: '每股资本公积金', type: 'yuan' },
    { group: '每股与营运', key: '存货周转率', label: '存货周转率', type: 'ratio' },
    { group: '每股与营运', key: '存货周转天数', label: '存货周转天数', type: 'days' },
    { group: '每股与营运', key: '应收账款周转天数', label: '应收账款周转天数', type: 'days' },
    { group: '每股与营运', key: '营业周期', label: '营业周期', type: 'days' }
  ];

  function periodLabel(p) {
    var names = { '03': '一季报', '06': '半年报', '09': '三季报', '12': '年报' };
    return String(p).slice(0, 4) + (names[String(p).slice(5, 7)] || '');
  }

  function bindComparePicks() {
    var selA = $('stock-compare-a');
    var selB = $('stock-compare-b');
    if (!selA || !selB) return;
    var refresh = function () {
      if (selA.value && selA.value === selB.value) return; // 两选同一报告期时忽略
      if (state.current) renderCompare(state.current);
    };
    selA.onchange = refresh;
    selB.onchange = refresh;
  }

  function renderCompare(d) {
    var rows = ((d && d.indicators) || []).slice(); // 保持倒序（最新报告期在前）
    if (!rows.length) return;
    var selA = $('stock-compare-a');
    var selB = $('stock-compare-b');
    if (!selA || !selB) return;

    // 三大报表按报告日升序（金额项单季化：本期累计 - 上期累计）
    var rpt = {};
    ['income', 'balance', 'cashflow'].forEach(function (sec) {
      rpt[sec] = ((d[sec] || []).slice()).sort(function (x, y) {
        return cmpKey(x['报告日'], y['报告日']);
      });
    });
    var indBy = {};
    rows.forEach(function (r) { indBy[r['报告期']] = r; });

    // 首次填充报告期下拉（按年份分组），默认最新年报 vs 上一年报
    if (!selA.options.length) {
      var lastYear = null, ogA = null, ogB = null;
      rows.forEach(function (r) {
        var y = String(r['报告期']).slice(0, 4);
        if (y !== lastYear) {
          lastYear = y;
          ogA = document.createElement('optgroup');
          ogA.label = y + ' 年';
          selA.appendChild(ogA);
          ogB = document.createElement('optgroup');
          ogB.label = y + ' 年';
          selB.appendChild(ogB);
        }
        ogA.appendChild(new Option(periodLabel(r['报告期']), r['报告期']));
        ogB.appendChild(new Option(periodLabel(r['报告期']), r['报告期']));
      });
      var annual = rows.filter(function (r) { return String(r['报告期']).indexOf('12-31') >= 0; });
      selA.value = annual[0] ? annual[0]['报告期'] : rows[0]['报告期'];
      selB.value = annual[1] ? annual[1]['报告期'] : selA.value;
    }

    var a = selA.value, b = selB.value;
    // 两期均为年报时用累计值（全年），否则用单季值（与图表季视图口径一致）
    var isAnnualCmp = a.indexOf('12-31') >= 0 && b.indexOf('12-31') >= 0;

    // 取数：indicators 直接取值；报表按报告日关联，非年报对比时金额类单季化（时点值除外）
    function valOf(period, m) {
      if (m.src && m.src !== 'indicator') {
        var list = rpt[m.src];
        for (var i = 0; i < list.length; i++) {
          if (list[i]['报告日'] === period) {
            var v = list[i][m.key];
            if (v == null) return null;
            if (m.type === 'amount' && !isAnnualCmp && m.single !== false) {
              // 一季报累计即当季值，无需差分；其余季度单季 = 本期累计 - 上期累计
              if (String(period).slice(5, 7) !== '03' && i > 0) {
                var pv = list[i - 1][m.key];
                return pv == null ? null : v - pv;
              }
              return v;
            }
            return v;
          }
        }
        return null;
      }
      var row = indBy[period];
      if (!row) return null;
      var k = (m.type === 'amount' && !isAnnualCmp && m.keySingle) ? m.keySingle : m.key;
      return row[k] == null ? null : row[k];
    }

    var html = '<thead><tr><th>指标</th><th>' + periodLabel(a) + '</th><th>' + periodLabel(b) +
      '</th><th>变化</th></tr></thead><tbody>';

    var curGroup = null, gIdx = -1;
    // 移动端默认只展开前 2 组核心指标（规模成长/成长能力），其余折叠；桌面端全量；展开过则记住
    var fold = mqMobile.matches && !state.compareExpanded;
    ANNUAL_METRICS.forEach(function (m) {
      if (m.group !== curGroup) {
        curGroup = m.group;
        gIdx++;
        html += '<tr class="cmp-group' + (fold && gIdx >= 2 ? ' cmp-more' : '') + '"><td colspan="4">' + m.group + '</td></tr>';
      }
      var va = valOf(a, m), vb = valOf(b, m);
      html += '<tr' + (fold && gIdx >= 2 ? ' class="cmp-more"' : '') + '><td>' + m.label + '</td>' +
        '<td>' + fmtMetric(va, m) + '</td>' +
        '<td>' + fmtMetric(vb, m) + '</td>' +
        '<td>' + fmtChange(va, vb, m) + '</td></tr>';
    });
    html += '</tbody>';
    if (fold) html += '<button type="button" class="cmp-more-btn" id="stock-compare-more">展开全部指标</button>';

    $('stock-compare-body').innerHTML = html;
    var moreBtn = $('stock-compare-more');
    if (moreBtn) {
      moreBtn.addEventListener('click', function () {
        state.compareExpanded = true;
        $('stock-compare-body').querySelectorAll('.cmp-more').forEach(function (tr) { tr.classList.remove('cmp-more'); });
        moreBtn.parentNode.removeChild(moreBtn);
      });
    }
  }

  function fmtMetric(v, m) {
    if (v == null) return '-';
    if (m.type === 'amount') return (v / 1e8).toFixed(1) + '亿';
    if (m.type === 'pct') return (v * 100).toFixed(1) + '%';
    if (m.type === 'days') return (+v).toFixed(1) + '天';
    return (+v).toFixed(2); // ratio / yuan
  }

  // 变化列：A 相对 B（红涨绿跌；比率用 pp，倍数/每股/天数用绝对差）
  function fmtChange(va, vb, m) {
    if (va == null || vb == null || vb === 0) return '-';
    var d, cls, txt;
    if (m.type === 'amount') {
      d = (va - vb) / Math.abs(vb) * 100;
      txt = (d >= 0 ? '+' : '') + d.toFixed(1) + '%';
    } else if (m.type === 'pct') {
      d = (va - vb) * 100;
      txt = (d >= 0 ? '+' : '') + d.toFixed(1) + 'pp';
    } else if (m.type === 'days') {
      d = va - vb;
      txt = (d >= 0 ? '+' : '') + d.toFixed(1) + '天';
    } else if (m.type === 'yuan') {
      d = va - vb;
      txt = (d >= 0 ? '+' : '') + d.toFixed(2) + yuanUnit();
    } else {
      d = va - vb;
      txt = (d >= 0 ? '+' : '') + d.toFixed(2);
    }
    cls = d >= 0 ? 'up' : 'down';
    return '<span class="cmp-' + cls + '">' + txt + '</span>';
  }

  /* ---------------- 三大报表 ---------------- */

  function initSheet(d) {
    var body = $('stock-sheet-body');
    var periodSel = $('stock-period');
    var key = 'income';

    var buttons = document.querySelectorAll('.stock-tabs button[data-sheet]');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        buttons.forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        key = btn.dataset.sheet;
        fillSheet(d, key, periodSel.value);
      });
    });

    var rows = d[key] || [];
    var periods = rows.map(function (r) { return r['报告日']; });
    periodSel.innerHTML = periods
      .map(function (p) { return '<option>' + fmtDate(p) + '</option>'; })
      .join('');
    periodSel.onchange = function () { fillSheet(d, key, periodSel.value); };

    if (buttons.length) buttons[0].classList.add('active');
    fillSheet(d, key, periods[0]);
  }

  function fillSheet(d, key, period) {
    var rows = (d[key] || []).filter(function (r) { return fmtDate(r['报告日']) === period; });
    if (!rows.length) {
      $('stock-sheet-body').innerHTML = '<tr><td>暂无数据</td></tr>';
      return;
    }
    var row = rows[0];
    var html = Object.keys(row)
      .filter(function (k) { return !['报告日', '公告日期', '数据源', '是否审计', '币种', '类型', '更新日期'].includes(k); })
      .map(function (k) {
        var v = row[k];
        return '<tr><td class="k">' + k + '</td><td class="v">' + fmtMoney(v) + '</td></tr>';
      })
      .join('');
    $('stock-sheet-body').innerHTML = html;
  }

  /* ---------------- 启动(已移交 Vue 组件) ---------------- */

  export { renderDetail, showDetail, state, valueAnalysis, valueScores, priceReferences,
    netCashFormula,
    cycleAnalysis, cycleHistory, cycleTrendOf, fraudAnalysis, managementAnalysis, fmtMoney, fmtNum, fmtPct, recentDividends,
    unbindResize };
