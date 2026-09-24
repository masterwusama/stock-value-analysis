/* ⑫/⑬ 股本事件（定增/回购，东财全市场快照；港美股不采整节不出）。
 * 编号跟随 events 有无（无 Wind 事件时本节顶 ⑫ 的位子）——由注册表编排。
 */
import { esc, fmtMoney, fmtNum, fmtDate } from '../../scoring.js'

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

export { hasActions }

export default {
  key: 'actions', title: '股本事件 · 定增与回购', lazy: true,
  has: (d) => hasActions(d.actions),
  build: (d) => renderActions(d.actions),
}
