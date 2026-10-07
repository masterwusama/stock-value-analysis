/* ---------------- 详情页渲染层 ----------------
 * stockLegacy.js 的渲染半边（2026-09-25 拆分后）：renderDetail 与各评分卡/图表/
 * 报表/事件模块的构建器、页面状态单例。评分计算全部来自 ./scoring.js（纯函数、
 * parity 资产）——本文件只管把数据变成 DOM，禁止改公式。
 * 公开 API 与拆分前一致（renderDetail/showDetail/state/…），调用方无感。
 */
import {
  BOND_10Y,
  equityOf,
  fmtMoney,
  fmtNum,
  fmtDate,
  fmtPct,
  recentDividends,
  cmpKey,
  annualRows,
  fiscalEndOf,
  sheetRowByDate,
  esc,
  valueAnalysis,
  weightedTotal,
  gradeOf,
  gradeText,
  fraudGradeOf,
  fraudGradeText,
  valueScores,
  fraudAnalysis,
  TRAP_W,
  TRAP_CUT,
  TRAP_BANDS,
  trapBandOf,
  trapScore,
  TRAP_VALUE_FMT,
  G_ITEMS,
  G_SUM_W,
  gGrowth,
  growthScore,
  G_VALUE_FMT,
  G_BANDS,
  gBandOf,
  V_ITEMS,
  V_SUM_W,
  vMcap,
  valueScore,
  V_VALUE_FMT,
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
  netCashFormula,
  priceReferences,
  scoreCalcText,
  stRiskOf,
  bmFeatures,
  R_FRAUD_GATE,
  R_TRAP_GATE,
  INTERIM_DIP_GATE
} from './scoring.js';

// 详情模块注册表（detail/modules/*）：加新模块 = 新建一个文件 + 这里注册一行，
// 编号/导航/懒加载由 renderDetail 统一编排。子模块（zygc）只注册壳，不占 nav。
import evMod from './detail/modules/events.js';
import acMod from './detail/modules/actions.js';
import zygcMod from './detail/modules/zygc.js';
import { evHasAny } from './detail/modules/events.js';
import { hasActions } from './detail/modules/actions.js';
import { createLazyMount } from './detail/lazyMount.js';
import { ensureDetailChrome, retargetDetailSpy } from './detail/chrome.js';
import { state, mqMobile } from './detail/state.js';
import { renderCharts, bindViewToggle, unbindResize } from './detail/modules/charts.js';
import { renderCompare, bindComparePicks } from './detail/modules/compare.js';
import { buildList } from './detail/modules/lists.js';
import { detailMarket, fmtCap, cls, yiUnit, yuanUnit, kv, sheetTab } from './detail/format.js';

  var DATA_BASE = './data/';

  var $ = function (id) { return document.getElementById(id); };

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

  // 商业模式特征芯片（Phase 2）：轻资产/定价权为 bm_validity 过线标签（回测背书，tooltip
  // 带实测 lift），主营集中/出海/主业变更/分部毛利差为观察项（只陈列事实，不判好坏）。
  // 全部客户端现算，输入 = 财报 + d.zygc，零接口新增。scoring.py 不含此逻辑（不进评分）。

  // ---- SECTION_DEFS：详情各节的单一事实源（顺序＝视觉顺序＝编号顺序） ----
  // 加新节 = 在这里插一行（html 为 section 内部内容；评分卡节给 card 即可，
  // 壳与填充 id 约定 stock-score-<key>）。①② 因内容依赖 ctx 在 renderDetail 内联，
  // 但仍在此登记 nav 顺序——编号/锚点永不散落两处。
  var SECTION_DEFS = [
    { key: 'basic', no: '①', navTitle: '基础财务信息' },
    { key: 'value', no: '②', navTitle: '通用价值标准' },
    { key: 'graham', no: '③', navTitle: '格雷厄姆烟蒂', title: '格雷厄姆烟蒂标准评判',
      html: '<div class="score-grid"><div class="score-card" id="stock-score-graham-def"></div></div>' },
    { key: 'schloss', no: '④', navTitle: '施洛斯烟蒂', title: '施洛斯烟蒂标准评判', card: 'stock-score-schloss' },
    { key: 'fraud', no: '⑤', navTitle: '造假风险', title: '财务报表造假可能性分析', card: 'stock-score-fraud' },
    { key: 'mgmt', no: '⑥', navTitle: '管理水平', title: '管理层管理水平评分', card: 'stock-score-mgmt' },
    { key: 'cycle', no: '⑦', navTitle: '周期位置', title: '周期位置 · 周期性行业判定', card: 'stock-score-cycle',
      tail: '<div class="stock-chart-block" id="stock-cycle-chart-block" style="display:none"><h4>历年财报周期位置评分趋势（分数越低越接近周期底部）</h4>' +
        '<div class="stock-chart" id="stock-chart-cycle"></div>' +
        '<p class="stock-chart-note">逐年回溯：以各年报年为窗口末尾取最近 8 年年报，按与当期相同的 8 维逻辑打分；单季环比逐年参与（历史年用该年自身单季营收环比，末年用最新单季环比），各年均为满 8 维、同口径可比。</p></div>' },
    { key: 'value-score', no: '⑧', navTitle: '价值综合分', title: '价值综合分 · 便宜与质量的证据', card: 'stock-score-value' },
    { key: 'recommend', no: 'R', navTitle: '综合推荐', title: '综合推荐分 · 门槛内的价值×成长合成', card: 'stock-score-recommend' },
    { key: 'growth', no: '⑨', navTitle: '成长综合分', title: '成长综合分 · 过去五年的增长证据', card: 'stock-score-growth' },
    { key: 'trap', no: '⑩', navTitle: '价值陷阱分', title: '价值陷阱分 · 坏消息证据', card: 'stock-score-trap' },
  ];

  // 空态格悬停说明（判不动＝原料缺失/前提不满足，不计入该项得分）：各卡表格统一
  var NA_TIP = ' title="原料缺失或不满足计算前提，判不动＝不计入该项得分（详见本卡说明）"';

  // 总览条 chips：占位 –，队列算出后 fillStrip 点亮（档位色点 + 悬停档位说明）
  function stripChip(letter, name, target) {
    return '<a class="strip-chip" data-scroll="' + target + '" href="#' + target + '" ' +
      'title="' + name + '分（计算中…）">' +
      '<em>' + letter + '</em><span>' + name + '</span>' +
      '<b id="strip-' + letter.toLowerCase() + '">–</b>' +
      '<i class="dot va-grade-na"></i></a>';
  }

  function fillStrip(letter, total, grade, tip) {
    var b = $('strip-' + letter.toLowerCase());
    if (!b) return;
    b.textContent = total == null ? '判不动' : fmtNum(total);
    var chip = b.parentElement;
    var dot = chip && chip.querySelector('.dot');
    if (dot) dot.className = 'dot va-grade-' + grade;
    if (chip && tip) chip.setAttribute('title', tip);
  }

  function renderDetail(d) {
    state.current = d;
    // 跨断点整体重建由 charts.js 的 resize 监听触发；renderDetail 在本闭包内，
    // 子模块反向 import 会成环，故经 state 单例回挂（state.js 头注释即为此约定）
    state.rebuildDetail = renderDetail;
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
    // 评分基准报告期（score_daily.report_date，随详情包下发）：长页里分数挂在各节，
    // 「这份分是按哪期财报算的」放头部才不会被当成最新季报的结论；非日历财年
    // （NVDA 1 月末、新鸿基 6 月末）同时点明财年截止，12-31 只是对齐标签
    var rd = ((d.scores || {}).reportDate || '');
    var fe = fiscalEndOf(d.indicators || []);
    var repBase = rd ? ('<span class="s-meta s-report">评分基准 ' + String(rd).slice(0, 10) +
      ' 财报' + (fe && String(fe).slice(5) !== '12-31' ? '（财年截至 ' + fe + '，12-31 为对齐标签）' : '') +
      '</span>') : '';
    html += '<div class="stock-header">' +
      '<h2>' + d.name + '</h2>' +
      '<span class="s-badge s-badge-' + (d.market || 'A') + '">' + marketName + '</span>' +
      '<span class="s-code">' + d.code + '</span>' +
      '<span class="s-price ' + cls(chg) + '">' + fmtNum(s.price) + '</span>' +
      '<span class="s-ccy">' + ccy + '</span>' +
      '<span class="s-meta ' + cls(chg) + '">' +
      (chg == null ? '-' : (chg > 0 ? '+' : '') + (chg * 100).toFixed(2) + '%') + '</span>' +
      repBase +
      '<span class="s-meta s-updated">更新于 ' + fmtDate(s.time || d.updated_at) + '</span>' +
      '</div>';

    // 评分总览条：五个结论分一眼可见（R 卡埋在页面中部、T/F 更深，长页扫视拿不到
    // 结论）。占位「–」随渐进任务队列逐个点亮（fa→F，vv/gg→V/G，recommend→R，
    // trap→T）；点按跳对应节，档位色点复用 va-grade-* 配色
    html += '<div class="score-strip" id="score-strip">' +
      stripChip('R', '综合', 'sec-recommend') +
      stripChip('V', '价值', 'sec-value-score') +
      stripChip('G', '成长', 'sec-growth') +
      stripChip('T', '陷阱', 'sec-trap') +
      stripChip('F', '造假', 'sec-fraud') +
      '</div>';

    // Wind 事件覆盖层当前代码条目（⑤⑥优化说明 + ⑪事件模块共用）；无事件数据（港美股/未抓公司）为 null
    var ovD = (state.eventOverlay && state.eventOverlay[d.code]) ? state.eventOverlay[d.code] : null;
    // 尾部模块注册表：⑪ 事件（有 Wind 明细才出）→ ⑫ 股本事件。
    // navTitle/no 在这里定（模块文件不知道自己前面的编号），has 门控在模块里
    var tailMods = [Object.assign({ navTitle: '事件与股东' }, evMod)];
    if (evMod.has(d)) {
      tailMods.push(Object.assign({ no: '⑫', navTitle: '股本事件' }, acMod));
    } else {
      tailMods.push(Object.assign({ no: '⑪', navTitle: '股本事件' }, acMod));
    }
    var hasEvents = evMod.has(d);
    var hasAct = acMod.has(d);
    // 各模块锚点导航（点击平滑滚动，避免与 #/code 路由冲突）：①-⑩ 恒在，⑪ ⑫ 按有无数据出
    html += '<nav class="va-nav" aria-label="详情模块导航">' +
      SECTION_DEFS.concat(tailMods).filter(function (m) {
        return m.navTitle && (!m.has || m.has(d));   // 无 has＝恒出（①②与评分卡节）
      }).map(function (m) {
        return '<a href="#sec-' + m.key + '" data-scroll="sec-' + m.key + '">' + m.no + ' ' + m.navTitle + '</a>';
      }).join('') +
      '<i class="va-progress" aria-hidden="true"></i></nav>';   // 阅读进度条（chrome.js 驱动）

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
          if (o.pct != null) parts.push(k.toUpperCase() + ' 样本 ' + (o.days == null ? '-' : o.days));
        });
        var tip = '市盈率(TTM)／市净率／市销率(TTM) 各自近十年分位（外源' + (v.source || 'Wind')
          + '口径，0~100，越低说明当前估值在自己十年里越靠下）；'
          + '分位窗口、样本起点与亏损期处理均随外源（样本数在东财源下是“正值样本数”，Wind 下是交易日数），'
          + '本系统只做展示与筛选，不进四派评分与买卖点参考价。'
          + '置空表示外源无值或已被判为不可信（如市盈率为负、外源日期过旧）。'
          + (v.date ? ' 外源观测日 ' + v.date + '。' : '')
          + (parts.length ? ' ' + parts.join('，') + '。' : '');
        return '<span title="' + tip + '">' + one('pe') + ' / ' + one('pb') + ' / ' + one('ps') + '</span>';
      })()) +
      kv('当前股息率', fmtPct(va.divYield)) +
      divBox +
      '</div></div>';

    // 主营构成 + 商业模式芯片：懒模块（滚动到才渲染）。zygc 已移出首开包
    //（/securities/{code}/zygc 懒取），数据方注入 d._loadZygc；无数据也占位
    html += '<div class="stock-section" data-lazy="zygc" style="min-height:96px">' + zygcMod.shell() + '</div>';

    // （价值分析五大区块：价值体检/股东回报/现金流质量/杜邦分析/成长性已移至模块二）

    // 指标趋势图（按指标分 3 个独立图表；支持季/年视图切换）——懒模块：
    // 壳与图容器先进 DOM，滚动到才 renderCharts + 绑视图切换（手机上 echarts init 不便宜）
    var indCount = (d.indicators || []).length;
    html += '<div class="stock-section" data-lazy="charts" style="min-height:300px"><div class="stock-section-head">' +
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

    // 财务对比（年报/季报，任意两个报告期可对比）——懒模块：表体滚动到才构建
    html += '<div class="stock-section" data-lazy="compare" style="min-height:140px"><div class="stock-section-head">' +
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
      '<table class="stock-table" id="stock-sheet-body"><tr><td class="k">点上方「利润表 / 资产负债表 / 现金流量表」标签加载报表</td></tr></table></div>';

    // 报表延后构建：首次点任一报表标签时才整表构建（含标签绑定）。
    // 主包里季报裁到最近 12 期（移动端首开瘦身，见 securities._prune_interims）；
    // 数据方给了 d._loadStatements（Vue 层注入的完整报表史懒取）就先拉全量再建表，
    // 期次下拉才回得去老季度。拉失败也照常建（表里是裁剪后的期次，功能不缺）。
    // ⚠ 绑定必须在 innerHTML 注入之后跑（bindSheetLazy 的调用点在下方赋值行后）——
    // 之前写在注入前，querySelectorAll 找不到还没进 DOM 的按钮，报表标签整排死键
    //（dbb7b79 渐进式渲染改造引入、2026-09-23 被「报表点不开」暴露）。
    var sheetBuilt = false;
    function bindSheetLazy() {
      document.querySelectorAll('.stock-tabs button[data-sheet]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          if (sheetBuilt) return;
          sheetBuilt = true;
          btn.classList.add('active');
          var buildSheet = function () {
            initSheet(d);
            var want = btn.dataset.sheet;
            if (want && want !== 'income') {
              var b = document.querySelector('.stock-tabs button[data-sheet="' + want + '"]');
              if (b) b.click();
            }
          };
          if (typeof d._loadStatements === 'function') {
            var body0 = $('stock-sheet-body');
            if (body0) body0.innerHTML = '<tr><td class="k">正在加载完整报表历史…</td></tr>';
            d._loadStatements().then(function () { buildSheet(); })
              .catch(function () { buildSheet(); });
          } else buildSheet();
        });
      });
    }

    // 分红历史（全量；移动端默认前 5 条，其余折叠）——懒模块：行列表滚动到才渲染
    var divs = d.dividends || [];
    html += '<div class="stock-section" data-lazy="divs" style="min-height:56px">' +
      '<h3>分红历史（' + divs.length + ' 条）</h3><div data-lazy-body="divs"><p class="empty">下滑加载…</p></div></div>';

    // 定期报告（移动端默认前 5 份，其余折叠）——懒模块
    var reports = d.reports || [];
    html += '<div class="stock-section" data-lazy="reports" style="min-height:56px">' +
      '<h3>定期报告（' + reports.length + ' 份）</h3><div data-lazy-body="reports"><p class="empty">下滑加载…</p></div></div>';
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

    // ---- 评分卡各节（③~⑩ + R）：壳由 SECTION_DEFS 生成，内容经下方任务队列填充 ----
    SECTION_DEFS.forEach(function (m) {
      if (!m.title) return;   // ①② 在上面内联构建（依赖 ctx），这里只出评分卡节
      html += '<section id="sec-' + m.key + '" class="stock-section va-module">' +
        '<h2 class="va-module-title"><span>' + m.no + '</span>' + m.title + '</h2>' +
        (m.html || '<div class="score-card" id="' + m.card + '"></div>') +
        (m.tail || '') + '</section>';
    });


    // ---- 模块十二：公司事件与股东结构（仅当有 Wind 事件明细时展示；港美股/未抓公司自动隐藏）----
    // 尾部整节模块统一编排：has 门控、编号标题、懒壳全部由注册表描述
    tailMods.forEach(function (m) {
      if (!m.has(d)) return;
      html += '<section id="sec-' + m.key + '" class="stock-section va-module" data-lazy="' + m.key + '" style="min-height:80px">' +
        '<h2 class="va-module-title"><span>' + m.no + '</span>' + m.title + '</h2>' +
        '<div data-lazy-body="' + m.key + '"><p class="empty">下滑加载…</p></div></section>';
    });

    $('stock-detail-body').innerHTML = html;
    // 报表标签绑定必须在按钮进 DOM 之后（见上方 bindSheetLazy 注释）
    bindSheetLazy();

    // ---- 懒模块：占位壳 → 进入视口前 200px 构建（滚动到哪建到哪，手机首屏只建评分卡）----
    // buildLazy 可被锚点跳转强制调用（bindVaNav）：跳到未构建的目标时先建再滚，不跳空壳
    var lazyDone = {};
    function buildLazy(key) {
      if (lazyDone[key]) return;
      lazyDone[key] = true;
      var host = function (name) { return document.querySelector('[data-lazy-body="' + name + '"]'); };
      try {
        if (key === 'charts') {
          // echarts 动态加载（见 echartsLoader）：图表模块本就在视口外，1MB 的
          // 下载解析与阅读评分卡的时间重叠；ensure 失败也照常渲染（图表区留空）
          var go = function () {
            renderCharts(d.indicators || []);
            renderVaCharts(va);          // ②的两张图依赖 state.charts（见 renderValueAnalysis 注释）
            bindViewToggle();
          };
          if (typeof window.__ensureEcharts === 'function') {
            window.__ensureEcharts().then(go).catch(function () { try { go(); } catch (e2) {} });
          } else go();
        } else if (key === 'compare') {
          renderCompare(d);
          bindComparePicks();
        } else if (key === 'divs' || key === 'reports') {
          buildList(d, key, host(key));
          bindMoreButtons();
        } else {
          // 注册表模块（zygc 子块 / ⑪⑫ 整节）：build 接 (d, ctx, hostEl)，宿主节点
          // 注入与绑定写在各模块文件里
          var mod = { zygc: zygcMod, events: evMod, actions: acMod }[key];
          var shellEl = document.querySelector('#stock-detail-body [data-lazy="' + key + '"]');
          if (mod && shellEl) {
            // 约定：build 返回 html 字符串 → 派发处注入 [data-lazy-body]；返回
            // undefined → 模块自行注入（如 zygc 有两个宿主）。两种都支持。
            var out = mod.build(d, { ovD: ovD }, shellEl);
            var bodyHost = shellEl.querySelector('[data-lazy-body="' + key + '"]');
            if (out != null && bodyHost) bodyHost.innerHTML = out;
          }
        }
      } catch (e) { /* 单模块构建失败不影响其余：占位文案保留 */ }
      var shell = document.querySelector('#stock-detail-body [data-lazy="' + key + '"]');
      if (shell) shell.style.minHeight = '';
    }

    // 分红/定期报告行列表（从同步 html 挪来；折叠口径不变）

    // 主营构成 + 商业模式芯片（zygc 懒取；失败/无数据给占位文案，芯片按可算项独立成行）

    // 懒挂载统一走 createLazyMount（IO + 视口内同步直建 + 滚动节流兜底三保险，
    // 见 detail/lazyMount.js）：重渲染时先 destroy 旧实例（buildLazy 仍保留为
    // 注册表派发的构建函数本体，锚点跳转经 lazy.forceBuild 强制构建）
    if (state.lazyMount) state.lazyMount.destroy();
    var lazyMount = createLazyMount();
    state.lazyMount = lazyMount;
    document.querySelectorAll('#stock-detail-body [data-lazy]').forEach(function (el) {
      lazyMount.register(el, el.dataset.lazy, function (hostEl) { buildLazy(hostEl.dataset.lazy); });
    });

    // 长页 chrome：scroll-spy 重挂（section 集合随渲染变化），监听器/回顶单例只绑一次
    ensureDetailChrome();
    retargetDetailSpy();
    if (!('IntersectionObserver' in window)) {
      ['charts', 'compare', 'divs', 'reports', 'zygc', 'events', 'actions'].forEach(function (k) { lazyMount.forceBuild(k); });
    }

    // 移动端读法折叠：score-note 常占卡片 40% 高度，窄屏默认收起、点按展开
    // （桌面不受影响：折叠样式只在 ≤600px 媒体查询里生效）
    if (window.innerWidth <= 600) {
      $('stock-detail-body').querySelectorAll('.score-note').forEach(function (n) {
        n.addEventListener('click', function () { n.classList.toggle('open'); });
      });
    }
    bindVaNav(function (k) { lazyMount.forceBuild(k); });

    // ---- 渐进式渲染：重活逐个让出主线程，手机上页面先可用、模块依次点亮 ----
    // 顺序即视觉顺序（图表 → 价值/评分卡 → 造假/管理/周期 → 综合分/陷阱）；
    // 三大报表延后到首次点开标签页才构建（initSheet 是加载期最重的同步构建之一，
    // 且报表标签页默认并不展开）。桌面同样走这条队列——每个任务之间只让出一帧。
    var fa = null, ma = null, ca = null, vv = null, gg = null, tpD = null, dip = null;
    var tasks = [
      // 趋势图表与财务对比已改视口懒构建（charts/compare 两个懒模块），不占任务帧；
      // 队列只跑首屏评分内容：价值/评分卡 → 造假/管理/周期 → 综合分/陷阱
      function () { renderValueAnalysis(va); renderScores(sc); },
      function () {
        fa = fraudAnalysis(d);
        var el = $('stock-score-fraud');
        if (el) el.innerHTML = fraudCard(fa, ovD);
        var fg = fa && fa.total != null ? fraudGradeOf(fa.total) : 'na';
        fillStrip('F', fa ? fa.total : null, fg,
          fa && fa.total != null
            ? '造假风险分 ' + fmtNum(fa.total) + ' · ' + fraudGradeText(fg)
            : '造假风险分判不动');
      },
      function () {
        ma = managementAnalysis(d);
        var el = $('stock-score-mgmt');
        if (el) el.innerHTML = managementCard(ma, ovD);
      },
      function () {
        ca = cycleAnalysis(d);
        var el = $('stock-score-cycle');
        if (el) el.innerHTML = cycleCard(ca);
        renderCycleChart(d, ca);
      },
      function () {
        vv = valueScore(d); gg = growthScore(d);
        var ve = $('stock-score-value'), ge = $('stock-score-growth');
        if (ve) ve.innerHTML = valueCard(vv);
        if (ge) ge.innerHTML = growthCard(gg);
        var vb = vv.total == null ? null : vBandOf(vv.total);
        var gb = gg.total == null ? null : gBandOf(gg.total);
        fillStrip('V', vv.total, vb ? vb.grade : 'na',
          vb ? '价值综合分 ' + fmtNum(vv.total) + ' · ' + vb.label : '价值综合分判不动');
        fillStrip('G', gg.total, gb ? gb.grade : 'na',
          gb ? '成长综合分 ' + fmtNum(gg.total) + ' · ' + gb.label : '成长综合分判不动');
      },
      function () {
        // R 综合推荐分：同一份现算输入（V/G/造假/陷阱/中报恶化）合成，与列表页入库分同一套公式
        tpD = trapScore(d) || {};
        dip = interimDipYoy(d.indicators || []);
        var rec = recommendScore(vv.total, gg.total, fa ? fa.total : null, tpD.total, dip);
        var el = $('stock-score-recommend');
        if (el) el.innerHTML = recommendCard(rec, vv.total, gg.total, dip, d.market);
        var rb = rec && rec.total == null ? null : (rec ? rBandOf(rec.total) : null);
        fillStrip('R', rec ? rec.total : null, rb ? rb.grade : 'na',
          rb ? '综合推荐分 ' + fmtNum(rec.total) + ' · ' + rb.label +
            (rec.gate ? '（门槛外：' + R_GATE_TEXT[rec.gate] + '）' : '')
            : (rec && rec.gate ? '门槛外不算分：' + (R_GATE_TEXT[rec.gate] || rec.gate) : '综合推荐分判不动'));
      },
      function () {
        var el = $('stock-score-trap');
        // 复用 R 步已算的 tpD：trapScore 内部还带一遍 fraudAnalysis，重算纯属白烧
        if (el) el.innerHTML = trapCard(tpD);
        fillStrip('T', tpD.total == null ? null : tpD.total,
          tpD.band ? tpD.band.grade : 'na',
          tpD.band ? '价值陷阱分 ' + fmtNum(tpD.total) + ' · ' + tpD.band.label : '价值陷阱分判不动');
        bindMoreButtons();
      },
    ];
    (function next() {
      var fn = tasks.shift();
      if (!fn) return;
      try {
        fn();
      } catch (e) {
        // 单任务抛错只跳过该格:不能让整条队列停摆,后面的评分卡全空且无提示
        if (window.console && console.error) console.error('[detail task]', e);
      }
      setTimeout(next, 0);
    })();
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

  function renderValueAnalysis(va) {
    renderHealth(va);
    renderCfTable(va);
    renderDuPont(va);
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

  function trapCard(ts) {
    var g = ts.total == null ? 'na' : ts.band.grade;
    var head = '<div class="score-card-head"><h4>价值陷阱分 T</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>陷阱分</span><b>' +
      (ts.total == null ? '不适用' : fmtNum(ts.total)) + '</b><i>' +
      (ts.total == null ? (ts.na ? '不适用' : '判不动') : ts.band.label) + '</i></div></div>';
    if (ts.reason) {
      return head + '<p class="score-note">' + ts.reason + '。</p>';
    }
    var rows = ts.items.map(function (x) {
      var cls = x.bad === null ? 'sc-na' : x.bad ? 'sc-bad' : 'sc-good';
      var txt = x.bad === null ? '判不动' : x.bad ? '亮灯' : '未触发';
      var fmt = TRAP_VALUE_FMT[x.key] || fmtNum;
      return '<tr><td>' + x.label + '</td>' +
        '<td class="v">' + (x.value == null ? '-' : fmt(x.value)) + '</td>' +
        '<td class="v" style="text-align:left">' + x.why + '</td>' +
        '<td class="v">' + x.weight + '</td>' +
        '<td class="v ' + cls + '"' + (x.bad === null ? NA_TIP : '') + '>' + txt + '</td></tr>';
    }).join('');
    var bands = TRAP_BANDS.map(function (b, i) {
      var on = i === TRAP_BANDS.indexOf(ts.band);
      var lo = i === 0 ? 0 : TRAP_BANDS[i - 1].hi;
      return '<tr' + (on ? ' class="cmp-group"' : '') + '><td>' + b.label + '</td>' +
        '<td class="v">' + (b.hi === Infinity ? '> ' + lo : '≤ ' + b.hi) + '</td>' +
        '<td class="v"><b>' + b.loss + '%</b></td>' +
        '<td class="v">' + b.imp5 + '%</td><td class="v">' + b.imp3 + '%</td>' +
        '<td class="v">' + b.divcut + '%</td><td class="v">' + b.bvpsdn + '%</td></tr>';
    }).join('');
    return head +
      '<p class="score-basis">' + ts.basis + '　证据合计 C = ' + fmtNum(ts.c) +
      '，可判 ' + ts.eff.evaluated + '/7 项（缺项按「没证据」计，不给分也不加罚）</p>' +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>证据项</th><th>当前值</th><th>依据</th><th>权重</th><th>判定</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">档位实测发生率（A 股 32,178 条「公司 × T 年」回测，结局取信号日之后公开的第一份年报）</h4>' +
      '<table class="stock-compare">' +
      '<thead><tr><th>档位</th><th>C 上界</th><th>转亏</th><th>减值≥净资产5%</th><th>减值≥净资产3%</th>' +
      '<th>分红中断</th><th>每股净资产降≥10%</th></tr></thead>' +
      '<tbody>' + bands + '</tbody></table></div>' +
      '<p class="score-note">' + ts.note + '</p>';
  }

  /* ---------------- 成长综合分 G（0~100，分高＝过去五年更能长） ----------------
   * 权重与锚点来自 backend/scripts/g_validity.py 的实测：A 股 21,926 条「公司 × 信号年」观测，
   * 信息集按「信号日当天真的公开了」截断，结局取锚点之后公开的真实净利增速（平滑分母口径）。
   * 三条判定线全过才出厂：甲 五分位单调 + ρ≥0.05 + 转负率不升、乙 与四派 max|ρ|≤0.85、丙 覆盖与
   * 打满/打零分布。权重是先验声明后按判别效度复核的，不是对那段历史拟合出来的。
   *
   * 三处与四派刻意不同，改之前先读：
   * 1. 锚点是**固定绝对阈值**，不是当日横截面分位。按分位取的话，一家公司的分会被当天其他公司
   *    的涨跌改掉，存进按日入库的分数就不可复现，改一家还会牵动其余六千家。
   * 2. 归一化用「固定分母 ΣW=100」，**故意不用** weightedTotal：缺项只压低分数、不参与归一，
   *    否则「只算得动一项且恰好拿满」的公司会顶到榜首。覆盖度由 eff.evaluated 单独出。
   * 3. 增速一律先夹到 ±50%/年（gGrowth），基期非正则压根算不出 → 判不动。基期必须真是 5 年前：
   *    取不晚于「锚点年−5」的最近一期，历史不足 5 年才退回最早一期；直接取序列首行等于把 3 年增速当 5 年卖。
   * 全市场适用（不像陷阱分只覆盖 A 股）：七项只读年报的四列（净利/营收/ROE/每股净资产），各市场
   * 都有；覆盖度差异由 evaluated 出——实测 A 股平均 6.58/7 项、美股只有 4.99/7。
   */

  function growthCard(gs) {
    var band = gs.total == null ? null : gBandOf(gs.total);
    var g = band ? band.grade : 'na';
    var head = '<div class="score-card-head"><h4>成长综合分 G</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>成长分</span><b>' +
      (gs.total == null ? '算不出' : fmtNum(gs.total)) + '</b><i>' +
      (band ? band.label : '年报不足 3 期') + '</i></div></div>';
    if (gs.reason) return head + '<p class="score-note">' + gs.reason + '，七项全部判不动。</p>';
    var rows = gs.items.map(function (x) {
      var cls = x.na ? 'sc-na' : x.sc >= 0.8 ? 'sc-good' : x.sc >= 0.4 ? 'sc-mid' : 'sc-low';
      var fmt = G_VALUE_FMT[x.key] || fmtNum;
      return '<tr><td>' + x.label + '</td>' +
        '<td class="v">' + (x.value == null ? '-' : fmt(x.value)) + '</td>' +
        '<td class="v ' + cls + '"' + (x.na ? NA_TIP : '') + '>' + (x.na ? '判不动' : (x.sc * 100).toFixed(0) + '%') + '</td>' +
        '<td class="v">' + x.weight + '</td>' +
        '<td class="v"><b>' + (x.na ? '-' : fmtNum(x.weight * x.sc)) + '</b></td>' +
        '<td class="v" style="text-align:left">' + x.why + '</td></tr>';
    }).join('');
    var bands = G_BANDS.map(function (b, i) {
      var on = b === band;
      var lo = i === 0 ? 0 : G_BANDS[i - 1].hi;
      return '<tr' + (on ? ' class="cmp-group"' : '') + '><td>' + b.label + '</td>' +
        '<td class="v">' + (b.hi === Infinity ? '> ' + lo : lo + ' ~ ' + b.hi) + '</td>' +
        '<td class="v"><b>' + b.s2 + '%</b></td><td class="v">' + b.s3 + '%</td>' +
        '<td class="v">' + b.neg2 + '%</td></tr>';
    }).join('');
    return head +
      '<p class="score-basis">' + gs.basis + '　可判 ' + gs.eff.evaluated + '/7 项' +
      '（分母固定为 100 权重，缺项只压低分数、不重新归一）</p>' +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>分项</th><th>当前值</th><th>符合度</th><th>权重</th><th>贡献分</th><th>口径</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">档位实测结局（A 股 21,926 条「公司 × 信号年」回测，' +
      '信息集按公告日重建，结局为锚点之后公开的真实净利增速）</h4>' +
      '<table class="stock-compare">' +
      '<thead><tr><th>档位</th><th>G 区间</th><th>未来 2 年年化</th><th>未来 3 年年化</th>' +
      '<th>净利转负率</th></tr></thead><tbody>' + bands + '</tbody></table></div>' +
      '<p class="score-note">' + gs.note + ' 档位两列都是未来真实增速的中位数：观测锚点在 2021~2023，' +
      '其后两年正逢 A 股净利普遍下修，所以各档中位数普遍为负——读的是档与档之间的落差，' +
      '不是某一档的绝对值。档1 与档2 在 2 年期上不分上下（差 1.4pp，在分位噪声内），落差主要出现在' +
      ' 3 年期与转负率上——这条轴能拉开好坏两端，拉不动的正是中间那一大片。</p>';
  }

  /* ---------------- 价值综合分 V（便宜 + 质量，先验加权） ----------------
   * 分项、权重与锚点冻结在 backend/scripts/v_validity.py 的定稿指表上——那份脚本是这条轴的出厂
   * 检验（四条验收线的实测数打在它的文件头），这里只是线上实现；要动任何一项先回去重跑。
   * 三条纪律与成长分同形：
   * 1. 锚点是固定绝对阈值，不按当日横截面分位重排，否则一家公司的分会被当天其他公司的涨跌改掉。
   * 2. 分母恒为 ΣW=100，缺项不进分子也不重新归一（按可用项归一会让「只算得动一项且恰好拿满」的
   *    公司顶到榜首）；覆盖度由 eff.evaluated 单独出。
   * 3. 「算得出而为负」记 0 分档（有形账面价值为负、利润为负、五年一股没回过钱），
   *    「科目取不到」才判不动——把前者记成判不动，等于让最没有安全边际的公司因为算不出而不被扣分。
   * 便宜那三项（30+25+10＝65 分）吃市值（取法见 vMcap：本批行情优先、退回深抓快照），质量那三项与回报
   * 那一项不吃；回测只证明了不吃价格的那半边有判别效度（C+D 块五分位对其后转亏率 25.5%→5.8%），
   * 便宜块本库量不了（没有历史市值）。
   */

  function valueCard(vr) {
    var band = vr.total == null ? null : vBandOf(vr.total);
    var g = band ? band.grade : 'na';
    var head = '<div class="score-card-head"><h4>价值综合分 V</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>价值分</span><b>' +
      (vr.total == null ? '算不出' : fmtNum(vr.total)) + '</b><i>' +
      (band ? band.label : (vr.reason || '分项全部判不动')) + '</i></div></div>';
    if (vr.reason) return head + '<p class="score-note">' + vr.reason + '，七项全部判不动。</p>';
    var rows = vr.items.map(function (x) {
      var cls = x.na ? 'sc-na' : x.sc >= 0.8 ? 'sc-good' : x.sc >= 0.4 ? 'sc-mid' : 'sc-low';
      var fmt = V_VALUE_FMT[x.key] || fmtNum;
      return '<tr><td>' + x.label + '</td>' +
        '<td class="v">' + (x.value == null ? '-' : fmt(x.value)) + '</td>' +
        '<td class="v ' + cls + '"' + (x.na ? NA_TIP : '') + '>' + (x.na ? '判不动' : (x.sc * 100).toFixed(0) + '%') + '</td>' +
        '<td class="v">' + x.weight + '</td>' +
        '<td class="v"><b>' + (x.na ? '-' : fmtNum(x.weight * x.sc)) + '</b></td>' +
        '<td class="v" style="text-align:left">' + x.why + '</td></tr>';
    }).join('');
    var bands = V_BANDS.map(function (b, i) {
      var on = b === band;
      var lo = i === 0 ? 0 : V_BANDS[i - 1].hi;
      return '<tr' + (on ? ' class="cmp-group"' : '') + '><td>' + b.label + '</td>' +
        '<td class="v">' + (b.hi === Infinity ? '> ' + lo : lo + ' ~ ' + b.hi) + '</td>' +
        '<td class="v"><b>' + b.loss + '%</b></td><td class="v">' + b.imp5 + '%</td></tr>';
    }).join('');
    return head +
      '<p class="score-basis">' + vr.basis + '　可判 ' + vr.eff.evaluated + '/7 项' +
      '（分母固定为 100 权重，缺项只压低分数、不重新归一）</p>' +
      '<div class="stock-compare-wrap"><table class="stock-compare">' +
      '<thead><tr><th>分项</th><th>当前值</th><th>符合度</th><th>权重</th><th>贡献分</th><th>口径</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">档位结局对照（A 股 16,373 条「公司 × 信号年」，' +
      '信号年 2021~2023，结局为该期之后公开的真实年报）</h4>' +
      '<table class="stock-compare">' +
      '<thead><tr><th>档位</th><th>V 区间</th><th>其后转亏率</th><th>其后减值≥5%净资产率</th></tr></thead>' +
      '<tbody>' + bands + '</tbody></table></div>' +
      '<p class="score-note">' + vr.note + ' 这张对照表不是预测：面板上的便宜那 65 分用的是今天的市值' +
      '乘当年的年报，今天的市值里已经装着信号日之后的一切消息，所以它只证明档位形状没反（分数越高、' +
      '其后变坏的越少）。真正的预测证据在不含价格的那 35 分上——同一批观测里，质量与回报两项的五分位' +
      '对「其后转亏率」是 25.5% → 15.0% → 10.6% → 7.3% → 5.8%、对「减值≥5%净资产率」是 37.3% → 3.8%。' +
      '对照表里的落差则几乎全在档1 与其余各档之间（24.2% 对 8~11%），档2~档5 在转亏率上已不分上下：' +
      '这一列擅长挑出「既不便宜又赚不到钱」的那一批，拉不动中间那一大片。</p>';
  }

  /* ---------------- 综合推荐分 R（门槛外的 0.6×V + 0.4×G） ----------------
   * 对应 scoring.py 的 recommend_score，出厂检验 backend/scripts/r_validity.py 三线全过
   * （甲 单调判别 / 乙 不弱于成分 / 丙 门槛剔得更坏，数字见 scoring.py 文件头）。
   * 门槛外的公司没有 R：列表 `-` 是「不过资格线」，不是 0 分。
   */

  function recommendCard(r, vTot, gTot, dip, market) {
    var band = r.total == null ? null : rBandOf(r.total);
    var g = band ? band.grade : 'na';
    var head = '<div class="score-card-head"><h4>综合推荐分 R</h4>' +
      '<div class="score-circle va-grade-' + g + '"><span>综合</span><b>' +
      (r.total == null ? '无分' : fmtNum(r.total)) + '</b><i>' +
      (band ? band.label : '门槛未过') + '</i></div></div>';
    var basis = '<p class="score-basis">R = 0.6 × 价值综合分 V + 0.4 × 成长综合分 G' +
      '（本标的：V ' + (vTot == null ? '-' : fmtNum(vTot)) + ' · G ' + (gTot == null ? '-' : fmtNum(gTot)) +
      '）；发分前置三道门槛——造假 ≤ 40 · 陷阱 ≤ 20 · 中报恶化 dip ≤ −70%，判不动的门槛输入按' +
      '「无证据」放行（港美股陷阱整列不适用、扣非缺失侧 dip 判不动，靠这条拿得到 R）。' +
      (market && market !== 'A'
        ? '<br><b style="color:#c2571a">⚠ 港美股注意：三道门槛在本市场实际只剩「造假 ≤ 40」一道' +
          '（陷阱口径只在 A 股成立、扣非科目港美股不披露）——本列在此市场的排雷保护弱于 A 股，' +
          '读分时请把造假分与 ⚑ 硬门槛一并看。</b>'
        : '') +
      '门槛外的公司整格 `-`，那是「不过资格线」，不是 0 分。</p>';
    // 中报恶化警示：评分轴只吃年报，这里把年报盲区亮出来（不改分，600866 式实例）
    if (dip != null && dip <= INTERIM_DIP_NOTE) {
      basis += '<p class="score-basis" style="color:#c2571a">⚠ 中报恶化：最新一期季报/半年报的扣非（缺则净利）同比 ' +
        (dip * 100).toFixed(0) + '%。评分只读年报（基准 ' +
        '一期晚于上面所有分数），年报之后的经营恶化不进任何分——要等下一次年报披露后的深抓才会反映。' +
        '读这一页时请把本行的分数视为「上一个年报年度的画像」。</p>';
    }
    if (r.total == null) {
      return head + basis + '<p class="score-note">本标的无 R：' +
        (R_GATE_TEXT[r.gate] || r.gate) + '。</p>';
    }
    var bands = R_BANDS.map(function (b, i) {
      var on = b === band;
      var lo = i === 0 ? 0 : R_BANDS[i - 1].hi;
      return '<tr' + (on ? ' class="cmp-group"' : '') + '><td>' + b.label + '</td>' +
        '<td class="v">' + (b.hi === Infinity ? '> ' + lo : lo + ' ~ ' + b.hi) + '</td>' +
        '<td class="v"><b>' + b.loss + '%</b></td><td class="v">' + b.imp5 + '%</td></tr>';
    }).join('');
    return head + basis +
      '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">档位结局对照（A 股 8,955 条门槛内「公司 × 信号年」，' +
      '信号年 2021~2023，结局为该期之后公开的真实年报）</h4>' +
      '<table class="stock-compare">' +
      '<thead><tr><th>档位</th><th>R 区间</th><th>其后转亏率</th><th>其后减值≥5%净资产率</th></tr></thead>' +
      '<tbody>' + bands + '</tbody></table></div>' +
      '<p class="score-note">权重 0.6/0.4 是先验声明不拟合（价值取向给量便宜+质量的 V 六成）。这张对照表' +
      '不是预测：面板上 R 的便宜那部分配的是今天的市值，装着信号日之后的一切消息——它只证明档位形状没反。' +
      '真正的判决书在无价格版上：同一批门槛内观测按「0.6×V质量块+0.4×G」五分位，其后转亏率' +
      '16.0% → 8.2% → 4.9% → 2.9% → 2.4%、减值≥5%率 26.8% → 6.6% → 5.9% → 4.0% → 3.3%，' +
      '逐年 3/3 同向；门槛剔除的人群转亏率 19.3%（过门 5.8%）。' +
      '收益侧也实测过（scripts/rr_validity.py，同一面板配 hfq 日线）：结论是负向——R_q 高分组' +
      '其后 2 年超额收益跑输低分组 10.1pp（V 质量块 −6.7、G −11.1，2022~2024 质量/成长风格杀跌），' +
      '所以 R 是排雷与选质的入口，不是买点：它挑「其后不容易变坏」的公司，不挑「其后涨得多」的。</p>';
  }

  // 造假分析评分卡（与 scoreCard 同构但等级方向相反：分低=安全=绿）；ov 为 Wind 事件覆盖层条目，有则并列基础分+事件明细+优化分

  function fraudCard(fa, ov) {    var g = fraudGradeOf(fa.total);
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

  /* ---------------- ⑧ 公司事件与股东结构（一次性 Wind 明细） ---------------- */

  // 事件明细是否含任意非空数据（决定 ⑦ 后是否插入 ⑧ 模块）

  function cycleCard(ca) {
    function dimRows(items) {
      return items.map(function (x) {
        var mCls = x.match == null ? 'sc-na' : x.match >= 0.99 ? 'sc-good' : x.match >= 0.5 ? 'sc-mid' : 'sc-low';
        var mTxt = x.match == null ? '-' : (x.match * 100).toFixed(0) + '%';
        return '<tr><td>' + x.std + '</td><td class="v">' + x.val + '</td><td class="v">' + x.thr + '</td>' +
          '<td class="v ' + mCls + '"' + (x.match == null ? NA_TIP : '') + '>' + mTxt + '</td>' +
          '<td class="v"' + (x.score == null ? NA_TIP : '') + '><b>' + (x.score == null ? '-' : fmtNum(x.score)) + '</b> / ' + x.max + '</td></tr>';
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
      h += '<div class="stock-compare-wrap"><h4 style="margin:8px 0 4px">阶段二：周期位置评分' +
        (ca.total == null ? '' : ' — 当前 <b class="cy-total">' + fmtNum(ca.total) + ' 分</b>') +
        '（分数越低越接近周期底部）</h4>' +
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

  function renderCycleChart(d, ca) {
    var block = $('stock-cycle-chart-block');
    var el = $('stock-chart-cycle');
    if (!block || !el) return;
    if (!ca.cyclical) { block.style.display = 'none'; return; }
    var hist = cycleHistory(d).filter(function (x) { return x.score != null; });
    if (hist.length < 2) { block.style.display = 'none'; return; }
    block.style.display = '';
    // echarts 全站动态加载（cd074e6）后，评分卡渐进队列先于脚本就位：这里等加载器
    // 再画。老写法 undefined 就 display:none 且无人再调，周期图首次进详情必消失
    var draw = function () {
      if (typeof echarts === 'undefined' || $('stock-cycle-chart-block') !== block) {
        // 等待期页面已重渲染/离开：旧 el 已分离，画上去只会泄漏实例
        block.style.display = 'none';
        return;
      }
      var chart = echarts.init(el);
      state.charts.push(chart);
      chart.setOption({
      grid: mqMobile.matches ? { left: 38, right: 14, top: 28, bottom: 26 } : { left: 44, right: 18, top: 32, bottom: 30 },
      tooltip: { trigger: 'axis', valueFormatter: function (v) { return v == null ? '-' : v + ' 分'; } },
      // 年份多时（回溯 8 年+）相邻标签在 1180px 下间隙已 <8px：hideOverlap 自动隔年
      xAxis: { type: 'category', data: hist.map(function (x) { return x.year; }),
               axisLabel: { hideOverlap: true, fontSize: 11 } },
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
    };
    if (typeof echarts === 'undefined') {
      if (typeof window.__ensureEcharts === 'function') {
        window.__ensureEcharts().then(draw).catch(function () { block.style.display = 'none'; });
      } else {
        block.style.display = 'none';
      }
      return;
    }
    draw();
  }

  // ---- 价格参考（买入/保守卖出/公允卖出）----
  // 三档都锚定各流派评分项自己的阈值（常量见 valueScores 上方），不做任何反推，
  // 因此参考价只随财报与估值快照变动，不随「当前总分能不能凑到某个数」漂移。

  // 类现金加权口径（与 scoring.py 一致）：分项键 → [展示名, 折算系数, 附注字段]
  // 附注拆得出「定期存款」与「受限货币资金」时，货币资金与其他流动资产都只按可用部分折算，
  // 故展示名跟着第三项字段是否命中变；附注为空的分项不参与折算也不展示。

  function renderScores(sc) {
    var curPrice = ((sc.d || {}).snapshot || {}).price;
    var refs = sc.priceRefs || {};
    var cards = [
      ['stock-score-graham-def', sc.grahamDef, refs.grahamDef],
      ['stock-score-schloss', sc.schloss, refs.schloss]
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

  function bindVaNav(buildLazy) {
    // 总览条 chips 与导航锚点同一套跳转语义（data-scroll → forceBuild + 平滑滚动）
    document.querySelectorAll('.va-nav a, a.strip-chip').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        var el = $(a.getAttribute('data-scroll'));
        if (!el) return;
        // 目标在懒模块里且还没构建：先同步构建再滚，不跳空壳。目标本身是懒壳
        //（⑪⑫ 整节）与目标内含懒壳（① 里的图表/对比/分红/报告/主营）都要覆盖
        var targets = el.matches('[data-lazy]') ? [el]
          : Array.prototype.slice.call(el.querySelectorAll('[data-lazy]'));
        targets.forEach(function (lz) { if (buildLazy) buildLazy(lz.dataset.lazy); });
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

  /* ---------------- 列表页悬浮代入式（多行纯文本） ----------------
   * 与详情页评分卡同一批现算函数（fraudAnalysis/managementAnalysis/cycleAnalysis/trapScore/
   * growthScore/valueScore/recommendScore/valueScores），这里只把卡面压成多行文本——
   * 数值口径不允许在这里出现第二套。which ∈ grahamDef/schloss（格攻/巴菲特 2026-10-06 移除）
   * fraud/mgmt/cycle/trap/growth/value/recommend。列表页动态 import 本库后按格调用。
   */

export { renderDetail, showDetail, state, unbindResize,
  valueAnalysis, valueScores, priceReferences, netCashFormula, equityOf,
  cycleAnalysis, cycleHistory, cycleTrendOf, fraudAnalysis, managementAnalysis,
  trapScore, trapBandOf, TRAP_W, TRAP_CUT, TRAP_BANDS,
  growthScore, G_ITEMS, G_SUM_W, valueScore, V_ITEMS, V_SUM_W,
  recommendScore, R_W_VALUE, R_FRAUD_GATE, R_TRAP_GATE, R_BANDS,
  interimDipYoy, INTERIM_DIP_NOTE, INTERIM_DIP_GATE,
  fmtMoney, fmtNum, fmtPct, recentDividends,
  scoreCalcText, stRiskOf, bmFeatures };

