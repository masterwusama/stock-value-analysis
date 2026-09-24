/* ① 内子模块：分红历史 / 定期报告 行列表。懒构建；移动端默认前 5 条折叠。
 */
import { fmtDate } from '../../scoring.js'
import { state, mqMobile } from '../state.js'

export function buildList(d, key, hostEl) {
  var fold = mqMobile.matches && !state.detailExpanded;
  var out = '';
  if (key === 'divs') {
    (d.dividends || []).forEach(function (r, i) {
      var extra = r.pay_date ? '派息日 ' + fmtDate(r.pay_date) : '';
      out += '<div class="stock-list-item' + (fold && i >= 5 ? ' d-more' : '') + '">' +
        '<span class="d-year">' + (r.year || '-') + '</span>' +
        '<span class="stock-badge">' + (r.type || '') + '</span>' +
        '<span class="d-desc">' + (r.description || '') + '</span>' +
        (extra ? '<span class="d-date">' + extra + '</span>' : '') + '</div>';
    });
  } else {
    (d.reports || []).forEach(function (r, i) {
      var audit = (r.audit_firm || r.audit_opinion)
        ? '<span class="d-audit">审计：' + (r.audit_firm || '—') + (r.audit_opinion ? ' · ' + r.audit_opinion : '') + '</span>' : '';
      out += '<div class="stock-list-item' + (fold && i >= 5 ? ' d-more' : '') + '">' +
        '<span class="stock-badge">' + r.category + '</span>' +
        '<span class="d-year">' + r.title + '</span>' +
        '<span class="d-date">' + fmtDate(r.date) + '</span>' + audit +
        '<a href="' + r.pdf_url + '" target="_blank" rel="noopener">PDF 原文</a>' +
        '<a href="' + r.detail_url + '" target="_blank" rel="noopener">详情</a></div>';
    });
  }
  var n = key === 'divs' ? (d.dividends || []).length : (d.reports || []).length;
  if (fold && n > 5) {
    out += '<button type="button" class="d-more-btn" id="' + (key === 'divs' ? 'stock-divs-more' : 'stock-reports-more') +
      '">展开全部 ' + n + ' ' + (key === 'divs' ? '条' : '份') + '</button>';
  }
  if (hostEl) hostEl.innerHTML = out;
}

export default {
  key: 'lists', lazy: true, has: () => true,
};
