/* 详情渲染层共享单例：页面状态与移动端判定。子模块文件（detail/modules/*）与
 * stockLegacy.js 都从这取——避免模块文件反向 import stockLegacy 造成循环。
 * state 的字段语义见 stockLegacy 原注释；charts 存 echarts 实例供卸载时统一 dispose。
 */
  var state = { current: null, charts: [], view: 'year', details: {},
    // 事件覆盖层（events/index.json byCode）：由 fetchOverlayOnce 拉取，或 StockDetailView 从接口数据直接注入
    eventOverlay: null, overlayLoaded: false };

  // 移动端断点（与 CSS @media max-width:600px 保持一致）：详情长列表折叠 + 图表 grid 收紧
  var mqMobile = window.matchMedia('(max-width: 600px)');

  // 10年期国债收益率参考值（用于股债利差对比，需手动定期更新）

export { state, mqMobile };
