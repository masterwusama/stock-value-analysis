/* 展示小件共享层：格式化与 html 小构建器，stockLegacy 与 detail/modules/* 共用。
 * （esc/fmtNum 等数值格式化属 scoring.js——这里是「带样式/单位语境」的展示件。）
 * detailMarket 读 state.current——从 state.js 取。
 */
import { state } from './state.js'
import { fmtMoney } from '../scoring.js'

  function detailMarket() { return (state.current && state.current.market) || 'A'; }

  function yiUnit() { return detailMarket() === 'US' ? '亿美元' : '亿元'; }

  function yuanUnit() { return detailMarket() === 'US' ? '美元' : '元'; }
  // 市值本币后缀（拼在 fmtMoney 的「亿」后面）：A 股留空，与列表页 USD/HKD 角标同一套语义

  function fmtCap(v) {
    if (v == null || isNaN(v)) return '-';
    return fmtMoney(v) + (detailMarket() === 'US' ? '美元' : detailMarket() === 'HK' ? '港元' : '');
  }

  // 小数 → "12.34"

  function cls(v) {
    if (v == null || isNaN(v) || v === 0) return 'flat';
    return v > 0 ? 'up' : 'down';
  }

  // 最近一年分红记录（按派息日/公告日 ≥ 一年前，数据已按日期倒序）

  function kv(k, v) {
    return '<div class="kv"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>';
  }

  function sheetTab(key, label) {
    return '<button data-sheet="' + key + '">' + label + '</button>';
  }

  /* ---------------- 指标趋势图（3 个独立图表，支持季/年视图切换） ---------------- */

  // 具名监听 + 守卫：早先这里是 window.onresize 赋值，会直接覆盖农化/EDB 模块挂的 resize 处理

export { detailMarket, fmtCap, cls, yiUnit, yuanUnit, kv, sheetTab };
