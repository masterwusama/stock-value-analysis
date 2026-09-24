/* ⑫ 公司事件与股东结构（Wind 事件明细；港美股/未抓公司整节不出）。
 * 自包含构建器（2026-09-25 从 stockLegacy.js 迁出）：build 返回 html，注入与
 * 事件绑定写在同一文件——「绑在注入前」这类 bug 在结构上不可能发生。
 */
import { esc, fmtMoney, fmtNum, fmtPct, fmtDate } from '../../scoring.js'

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

export { evHasAny }

export default {
  key: 'events', no: '⑫', title: '公司事件与股东结构', lazy: true,
  has: (d) => evHasAny(d._events),
  build: (d, ctx) => renderEvents(d._events, ctx.ovD),
}
