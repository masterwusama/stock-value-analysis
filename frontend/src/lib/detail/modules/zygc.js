/* ① 内子模块：主营构成（东财 F10 三维度）+ 商业模式特征芯片。
 * zygc 数据不在详情首开包（/{code}/zygc 懒取，d._loadZygc 由视图注入）；
 * 无数据/拉失败给占位文案，芯片按财报可算项独立成行。
 */
import { esc, fmtNum, bmFeatures } from '../../scoring.js'

  function bmChips(d, zygc) {
    var chips = [];
    // 判定规则在 scoring.bmFeatures（与列表筛选入库列同一实现，parity 覆盖），
    // 这里只管把结果画成芯片：ratio/calibre/gmDelta 供 tip 文案
    var bm = bmFeatures(d);
    if (bm.ratio != null) {
      chips.push({ key: 'light', label: '轻资产自供', on: bm.light === true, backed: true,
        tip: '近3年 capex 合计÷经营现金流合计 = ' + (bm.ratio * 100).toFixed(0) + '%（' + bm.calibre + '口径，阈值 ≤60%）。回测（A 股 21,926 观测，bm_validity 3 年口径重跑）：亮灯组其后转亏率 8.1% vs 其余 13.6%（lift 0.60）' });
    }
    if (bm.gmDelta != null) {
      chips.push({ key: 'pricing', label: '定价权', on: bm.pricing === true, backed: true,
        tip: '毛利率较 5 年前 ' + (bm.gmDelta >= 0 ? '+' : '') + (bm.gmDelta * 100).toFixed(1) + 'pp（阈值 ≥−3pp）。回测：亮灯组其后转亏率 10.2% vs 其余 12.2%（lift 0.83）' });
    }
    if (zygc && zygc.sections && zygc.sections.length) {
      var prod = null;
      zygc.sections.forEach(function (s) { if (s.type === 2) prod = s; });
      if (!prod) zygc.sections.forEach(function (s) { if (s.type === 1) prod = s; });
      if (prod && prod.rows.length) {
        var t1 = prod.rows[0];
        if (t1.ratio != null && t1.ratio >= 0.7)
          chips.push({ key: 'focus', label: '主营集中 ' + (t1.ratio * 100).toFixed(0) + '%', backed: false,
            tip: '首项「' + t1.name + '」占营收 ' + (t1.ratio * 100).toFixed(1) + '%（观察项，只陈列事实）' });
        else if (t1.ratio != null && t1.ratio <= 0.4)
          chips.push({ key: 'disp', label: '主营分散 ' + (t1.ratio * 100).toFixed(0) + '%', backed: false,
            tip: '首项「' + t1.name + '」仅占营收 ' + (t1.ratio * 100).toFixed(1) + '%（观察项）' });
      }
      var gms = [];
      if (prod) prod.rows.forEach(function (r) { if (r.gm != null) gms.push(r.gm); });
      if (gms.length >= 2) {
        var spread = (Math.max.apply(null, gms) - Math.min.apply(null, gms)) * 100;
        if (spread >= 20)
          chips.push({ key: 'gmsplit', label: '业务毛利差 ' + spread.toFixed(0) + 'pp', backed: false,
            tip: '分部毛利率极差 ' + spread.toFixed(1) + 'pp（观察项：各业务盈利质量分层）' });
      }
      var reg = null;
      zygc.sections.forEach(function (s) { if (s.type === 3) reg = s; });
      if (reg) {
        var ov = 0;
        reg.rows.forEach(function (r) {
          if (r.name.indexOf('境外') >= 0 || r.name.indexOf('海外') >= 0 || r.name.indexOf('出口') >= 0) ov += (r.ratio || 0);
        });
        if (ov >= 0.3)
          chips.push({ key: 'overseas', label: '出海 ' + (ov * 100).toFixed(0) + '%', backed: false,
            tip: '境外收入占比 ' + (ov * 100).toFixed(1) + '%（观察项）' });
      }
    }
    var tops = (zygc && zygc.historyTop1) || [];
    var names = [];
    tops.forEach(function (h) { if (names.indexOf(h.name) < 0) names.push(h.name); });
    if (names.length >= 2)
      chips.push({ key: 'pivot', label: '主业变更', backed: false,
        tip: '近 ' + tops.length + ' 期报告首项：' + names.join(' → ') + '（观察项）' });
    return chips;
  }

function buildZygcHtml(zg) {
  if (!zg || !zg.sections || !zg.sections.length) {
    return '<p class="empty">无主营构成数据（不披露或未采集）。</p>';
  }
  var out = '<div class="zygc-grid">';
  zg.sections.forEach(function (sec) {
    out += '<div class="zygc-tbl"><h4>' + sec.label + '</h4>' +
      '<table class="stock-compare"><thead><tr><th>构成项</th><th>收入(亿)</th><th>占比</th><th>毛利率</th></tr></thead><tbody>';
    sec.rows.forEach(function (r) {
      out += '<tr><td class="zygc-name" title="' + esc(r.name) + '">' + esc(r.name) + '</td>' +
        '<td class="v">' + (r.income == null ? '-' : fmtNum(r.income / 1e8)) + '</td>' +
        '<td class="v">' + (r.ratio == null ? '-' : (r.ratio * 100).toFixed(1) + '%') + '</td>' +
        '<td class="v">' + (r.gm == null ? '-' : (r.gm * 100).toFixed(1) + '%') + '</td></tr>';
    });
    out += '</tbody></table></div>';
  });
  return out + '</div>' +
    '<p class="score-note">来源：东方财富 F10 主营构成，按公司披露口径分维度（缺哪个维度就是不披露，不是没有）。' +
    '毛利率为东财披露的分部毛利率；「其他(补充)」类行是公司兜底项，占比小可忽略。</p>';
}

function buildChipsHtml(d, zg) {
  var bmRow = bmChips(d, zg);
  return bmRow.length
    ? '<div class="bm-chips"><span class="bm-cap">商业模式特征</span>' +
      bmRow.map(function (c) {
        var cls = c.backed ? (c.on ? 'bm-on' : 'bm-off') : 'bm-obs';
        var mark = c.backed ? (c.on ? '✓' : '✗') : '•';
        return '<span class="bm-chip ' + cls + '" title="' + esc(c.tip) + '">' + mark + ' ' + c.label + '</span>';
      }).join('') +
      '<span class="bm-note">✓/✗＝回测背书标签（bm_validity）；•＝观察项</span></div>'
    : '';
}

export default {
  key: 'zygc', title: '主营业务构成', lazy: true,
  has: () => true,
  shell: () => '<h3>主营业务构成<span class="s-ccy-note">（东财 F10，滚动到本区自动加载）</span></h3>' +
    '<div data-lazy-body="zygc"><p class="empty">下滑加载…</p></div>' +
    '<div data-lazy-body="bmchips"></div>',
  build: (d, _ctx, host) => {
    var zygcHost = host.querySelector('[data-lazy-body="zygc"]');
    var chipsHost = host.querySelector('[data-lazy-body="bmchips"]');
    var done = function (zg) {
      if (zygcHost) zygcHost.innerHTML = buildZygcHtml(zg);
      if (chipsHost) chipsHost.innerHTML = buildChipsHtml(d, zg);
    };
    if (d.zygc) done(d.zygc);
    else if (typeof d._loadZygc === 'function') d._loadZygc().then(done).catch(function () { done(null); });
    else done(null);
  },
}
