/* ① 内子模块：关键指标趋势三图（季/年视图切换）。懒构建——echarts 经
 * window.__ensureEcharts 异步就位后才画（由 stockLegacy 的派发层处理）。
 */
import { fmtDate, fiscalEndOf } from '../../scoring.js'
import { state, mqMobile } from '../state.js'
import { yiUnit } from '../format.js'

var $ = function (id) { return document.getElementById(id); }

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
        // renderDetail 在宿主 stockLegacy 闭包里，反向 import 会成环——宿主渲染时把
        // 自己挂到 state.rebuildDetail（见 stockLegacy renderDetail 开头），这里只回调
        if ($('stock-detail-body') && typeof state.rebuildDetail === 'function') {
          state.rebuildDetail(state.current);
        }
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

export { renderCharts, bindViewToggle, unbindResize };
