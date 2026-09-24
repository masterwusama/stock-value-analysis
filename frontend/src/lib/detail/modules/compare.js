/* ① 内子模块：财务对比（任意两报告期逐指标对比）。懒构建。
 */
import { annualRows, cmpKey } from '../../scoring.js'
import { state, mqMobile } from '../state.js'
import { cls, yuanUnit } from '../format.js'

var $ = function (id) { return document.getElementById(id); };

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

export { renderCompare, bindComparePicks };
