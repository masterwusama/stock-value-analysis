<script setup>
/* 大宗商品·供需页(黄金/白银双商品,2026-10-27 二期泛化)。首屏只留「四件套」
 * (各商品 4 张大卡)+核心双轴图,其余序列收进折叠的「全部指标」区——信息密度
 * 做减法,四件套回答"看哪个"。数据源 /api/commodity/{gold,silver}(读时合成,
 * app/api/commodity.py;释义 desc 与导读 guide 由 API 注入)。
 * 核心图:黄金=金价 vs 实际利率(负相关);白银=银价 vs 金银比(镜像跷跷板,
 * 数据自含——白银端点无金价序列,金银比即相对强弱)。
 * 定位:研究看板不是投资建议;因子记分卡与供需平衡表是 P2,先看数再谈模型。
 */
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'
import { useMediaQuery } from '../lib/useMediaQuery'

// 移动断点与 CSS .m 类统一 640px(MOBILE_QUERY 全局常量是 600px,本页自带);
// 样式用 .m class 而非 @media——桌面注入 class 即可验证移动布局
const isMobile = useMediaQuery('(max-width: 640px)')
const cur = ref('gold')    // 当前商品 gold | silver
const data = ref(null)
const error = ref('')
const loading = ref(false)
const expanded = ref(false)   // 「全部指标」折叠区(默认收起,首屏减负)
const curCat = ref('')
const curCode = ref('')       // 完整区主图序列;空=组内归一化对比
const range = ref('3y')       // 时间范围:核心图与完整图共用
const normalize = ref(true)   // 完整区主图:归一化(起点=100) vs 单序列绝对值

const coreEl = ref(null)
const chartEl = ref(null)
let coreChart = null
let chart = null
let echarts = null

const RANGE_DAYS = { '1y': 365, '3y': 365 * 3, '5y': 365 * 5, all: 0 }

// 商品切换条(模板循环;新商品在这里加一行)
const CMDS = [
  { key: 'gold', name: '黄金' }, { key: 'silver', name: '白银' },
  { key: 'copper', name: '铜' }, { key: 'aluminum', name: '铝' },
  { key: 'lithium', name: '碳酸锂' }, { key: 'crudeoil', name: '原油' },
  { key: 'ferrous', name: '黑色系' }, { key: 'agri', name: '农产品' },
]

// 按商品定制:标题/四件套/核心图双轴与解读
const META = {
  gold: {
    title: '黄金 · 四个关键数',
    core: [
      { code: 'XAU_USD', tag: '价格', note: '国际金价基准' },
      { code: 'DFII10', tag: '定价锚', note: '持金机会成本,负相关' },
      { code: 'SPDR_TON', tag: '资金', note: '最大黄金ETF·西方资金' },
      { code: 'GOLD_SILVER', tag: '估值', note: '金银比·跨商品比价' },
    ],
    chart: { left: 'XAU_USD', right: 'DFII10', title: '核心图:金价 vs 10Y 实际利率(负相关是常态)',
             leftName: '伦敦金(左轴·美元/盎司)', rightName: '10Y实际利率(右轴·%)',
             tip: '📖 正常态是两线反向(利率降→持金成本降→金涨)。两线同向走=出现避险、央行购金等额外力量,值得深究。' },
  },
  silver: {
    title: '白银 · 四个关键数',
    core: [
      { code: 'XAG_USD', tag: '价格', note: '国际银价基准' },
      { code: 'AG_GSR', tag: '估值', note: '金银比·第一估值锚' },
      { code: 'SLV_TON', tag: '资金', note: '最大白银ETF·西方资金' },
      { code: 'PS_MAIN', tag: '工业', note: '多晶硅·光伏排产温度计' },
    ],
    chart: { left: 'XAG_USD', right: 'AG_GSR', title: '核心图:银价 vs 金银比(镜像跷跷板)',
             leftName: '伦敦银(左轴·美元/盎司)', rightName: '金银比(右轴·倍)',
             tip: '📖 正常态是两线镜像(银涨→金银比压缩=银跑赢金)。金银比走阔=银跑输金,常出现在恐慌期(白银先被抛售)。' },
  },
  aluminum: {
    title: '铝 · 四个关键数',
    core: [
      { code: 'AHD_MAIN', tag: '价格', note: '国际铝价基准·LME三月' },
      { code: 'AO0_CLOSE', tag: '成本', note: '氧化铝·冶炼原料锚' },
      { code: 'AL_PMI', tag: '需求', note: '制造业PMI·50是荣枯线' },
      { code: 'AL_SHFE_LME', tag: '比价', note: '沪伦比·内盘溢价' },
    ],
    chart: { left: 'AL0_CLOSE', right: 'AO0_CLOSE', title: '核心图:沪铝 vs 氧化铝(成本传导)',
             leftName: '沪铝(左轴·元/吨)', rightName: '氧化铝(右轴·元/吨)',
             tip: '📖 氧化铝是电解铝的直接原料(1.93 吨产 1 吨)。氧化铝相对涨得快=冶炼利润被压缩;沪铝独立走强=需求或供给约束驱动。' },
  },
  lithium: {
    title: '碳酸锂 · 四个关键数',
    core: [
      { code: 'LC0_CLOSE', tag: '价格', note: '广期所主力·元/吨' },
      { code: 'EV_ETF', tag: '需求', note: '新能车ETF·终端景气' },
      { code: 'LC_INV', tag: '库存', note: '广期所仓单·挤仓变量' },
      { code: 'LC0_OI', tag: '仓位', note: '持仓量·套保热度' },
    ],
    chart: { left: 'LC0_CLOSE', right: 'EV_ETF', title: '核心图:碳酸锂 vs 新能车ETF(需求同向/背离)',
             leftName: '碳酸锂(左轴·元/吨)', rightName: '新能车ETF(右轴·元)',
             tip: '📖 常态同向(需求驱动)。需求涨而锂价跌=供给大量释放(2022-2024 的主矛盾);价格独立走强=供给收缩或挤仓。' },
  },
  crudeoil: {
    title: '原油 · 四个关键数',
    core: [
      { code: 'CL_MAIN', tag: '价格', note: 'WTI原油·美元/桶' },
      { code: 'BRENT_WTI', tag: '比价', note: '布伦特-WTI·全球紧张度' },
      { code: 'CFTC_OIL_COMM', tag: '仓位', note: 'CFTC商业净持仓' },
      { code: 'CRACK_HO', tag: '炼制', note: '取暖油/原油·成品油强弱' },
    ],
    chart: { left: 'CL_MAIN', right: 'OIL_DTWEXBGS', title: '核心图:WTI vs 美元指数(负相关是常态)',
             leftName: 'WTI原油(左轴·美元/桶)', rightName: '美元宽口径(右轴·点)',
             tip: '📖 正常态是两线反向(美元弱→油价涨)。两线同向走=供给冲击主导(减产/制裁),美元不是主线。' },
  },
  ferrous: {
    title: '黑色系 · 四个关键数',
    core: [
      { code: 'RB0_CLOSE', tag: '价格', note: '螺纹钢·建筑业长材' },
      { code: 'I0_CLOSE', tag: '成本', note: '铁矿石·1.6吨/吨钢' },
      { code: 'MILL_MARGIN', tag: '利润', note: '钢厂盘面利润·>0有得赚' },
      { code: 'HC_RB_SPREAD', tag: '结构', note: '卷螺差·制造vs建筑' },
    ],
    chart: { left: 'RB0_CLOSE', right: 'I0_CLOSE', title: '核心图:螺纹钢 vs 铁矿石(成本挤压)',
             leftName: '螺纹钢(左轴·元/吨)', rightName: '铁矿石(右轴·元/吨)',
             tip: '📖 常态是钢价与矿价同向(需求驱动成本)。铁矿独立走强=矿端供给事件(飓风/安检),钢厂利润被挤压;螺纹领涨=终端需求定价。' },
  },
  agri: {
    title: '农产品 · 四个关键数',
    core: [
      { code: 'LH0_CLOSE', tag: '价格', note: '生猪·元/公斤÷1000' },
      { code: 'HOG_FEED', tag: '比价', note: '猪粮比·6是预警线' },
      { code: 'M0_CLOSE', tag: '饲料', note: '豆粕·蛋白成本' },
      { code: 'C0_CLOSE', tag: '粮价', note: '玉米·饲料锚' },
    ],
    chart: { left: 'LH0_CLOSE', right: 'C0_CLOSE', title: '核心图:生猪 vs 玉米(养殖成本传导)',
             leftName: '生猪(左轴·元/吨)', rightName: '玉米(右轴·元/吨)',
             tip: '📖 猪价与粮价同向温和=成本传导正常。粮价独立上涨而猪价不涨=养殖利润被挤压(自产能出清);猪价独立暴涨暴跌=猪周期本身(产能调节滞后 10 个月)。' },
  },
  copper: {
    title: '铜 · 四个关键数',
    core: [
      { code: 'CAD_MAIN', tag: '价格', note: '国际铜价基准·LME三月' },
      { code: 'CN_PMI', tag: '需求', note: '制造业PMI·50是荣枯线' },
      { code: 'CFTC_CU_NC', tag: '仓位', note: '基金净持仓·拥挤度' },
      { code: 'CU_GOLD', tag: '估值', note: '铜金比·周期vs避险' },
    ],
    chart: { left: 'CAD_MAIN', right: 'CU_DTWEXBGS', title: '核心图:铜价 vs 美元指数(负相关是常态)',
             leftName: 'LME铜(左轴·美元/吨)', rightName: '美元宽口径(右轴·点)',
             tip: '📖 正常态是两线反向(美元弱→铜的美元计价成本降→需求增)。两线同向走=中国需求主导行情,美元不是主线。' },
  },
}
const meta = computed(() => META[cur.value] || META.gold)

async function load(c) {
  loading.value = true
  error.value = ''
  try {
    const d = await get('/commodity/' + c)
    data.value = d
    curCat.value = d.cats?.[0] || ''
    curCode.value = d.series?.[0]?.code || ''
    await nextTick()
    renderCore()
    render()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
}
function switchCmd(c) { if (c !== cur.value) { cur.value = c; load(c) } }

onMounted(async () => {
  echarts = await ensureEcharts()
  await load(cur.value)
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (coreChart) coreChart.dispose()
  if (chart) chart.dispose()
  coreChart = chart = null
})
function onResize() { if (coreChart) coreChart.resize(); if (chart) chart.resize() }

// ---- 展示格式化 ----
const num = (v, d = 2) => (v == null ? '-' : (+v).toFixed(d))
const sign = (v, d = 2) => (v == null ? '-' : (v >= 0 ? '+' : '') + (+v).toFixed(d))
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down')

const series = computed(() => data.value?.series || [])
const catSeries = computed(() => series.value.filter((s) => s.cat === curCat.value))
const staleList = computed(() => series.value.filter((s) => s.stats?.stale))
const byCode = computed(() => Object.fromEntries(series.value.map((s) => [s.code, s])))
const coreCards = computed(() =>
  meta.value.core.map((h) => ({ ...h, s: byCode.value[h.code] })).filter((x) => x.s?.stats?.latest != null))

function windowPoints(s) {
  const days = RANGE_DAYS[range.value]
  if (!days) return s.points
  const cut = new Date(Date.now() - days * 86400e3).toISOString().slice(0, 10)
  return s.points.filter((p) => p[0] >= cut)
}

// 核心卡点击:展开完整区并定位到该序列(联动,点卡必有反馈)
function pickCore(s) { expanded.value = true; normalize.value = false; curCode.value = s.code }
function pick(s) { curCode.value = s.code; normalize.value = false }
function pickCat(c) {
  curCat.value = c
  normalize.value = true
  curCode.value = catSeries.value[0]?.code || ''
}

// ---- 核心图:双轴双线(配置在 META[商品].chart) ----
function renderCore() {
  if (!coreEl.value || !echarts) return
  if (!coreChart) coreChart = echarts.init(coreEl.value)
  const cfg = meta.value.chart
  const L = byCode.value[cfg.left]
  const R = byCode.value[cfg.right]
  if (!L && !R) { coreChart.clear(); return }
  const dates = [...new Set([
    ...(L ? windowPoints(L).map((p) => p[0]) : []),
    ...(R ? windowPoints(R).map((p) => p[0]) : []),
  ])].sort()
  const disc = new Map(dates.map((d, i) => [d, i]))
  const mk = (s, name, color) => {
    if (!s) return null
    const m = new Map(windowPoints(s).map((p) => [p[0], p[1]]))
    return {
      name, type: 'line', showSymbol: false, connectNulls: true,
      lineStyle: { width: 1.6, color }, itemStyle: { color },
      data: dates.map((d) => (m.has(d) ? [disc.get(d), m.get(d)] : [disc.get(d), null])),
    }
  }
  const ss = [mk(L, cfg.leftName, '#c9a227'), mk(R, cfg.rightName, '#2d6cdf')].filter(Boolean)
  const m = isMobile.value
  coreChart.setOption({
    textStyle: { fontFamily: 'inherit' },
    grid: { left: m ? 42 : 60, right: m ? 40 : 56, top: m ? 38 : 40, bottom: m ? 44 : 48 },
    title: { text: cfg.title, left: 0, top: 0, textStyle: { fontSize: m ? 12 : 13 } },
    tooltip: { trigger: 'axis', confine: true, textStyle: { fontSize: m ? 10 : 12 } },
    legend: { top: 6, right: 0, icon: 'roundRect', itemWidth: m ? 10 : 14, itemHeight: 8,
              textStyle: { fontSize: m ? 9.5 : 11 } },
    xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: m ? 8 : 10 } },
    yAxis: [
      { type: 'value', scale: true, axisLabel: { fontSize: m ? 8 : 10 }, splitLine: { lineStyle: { opacity: 0.35 } } },
      { type: 'value', scale: true, axisLabel: { fontSize: m ? 8 : 10 }, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: m ? 14 : 16, bottom: 6 }],
    series: ss.map((x, i) => ({ ...x, yAxisIndex: i })),
  }, true)
}

// ---- 完整区图(归一化/单序列) ----
function render() {
  if (!chartEl.value || !echarts || !expanded.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const m = isMobile.value
  const base = { textStyle: { fontFamily: 'inherit' },
                 grid: { left: m ? 42 : 56, right: m ? 12 : 18, top: 34, bottom: m ? 44 : 48 } }
  if (normalize.value) {
    const list = catSeries.value.map((s) => ({ s, pts: windowPoints(s) })).filter((x) => x.pts.length)
    if (!list.length) { chart.clear(); return }
    const dates = [...new Set(list.flatMap((x) => x.pts.map((p) => p[0])))].sort()
    const disc = new Map(dates.map((d, i) => [d, i]))
    chart.setOption({
      ...base,
      title: { text: `${curCat.value} · 归一化对比(区间起点=100)`, left: 0, top: 0, textStyle: { fontSize: m ? 12 : 13 } },
      tooltip: { trigger: 'axis', confine: true, textStyle: { fontSize: m ? 10 : 12 }, valueFormatter: (v) => (v == null ? '-' : (+v).toFixed(1)) },
      legend: { top: 6, type: 'scroll', icon: 'roundRect', itemWidth: m ? 10 : 14, itemHeight: 8, textStyle: { fontSize: m ? 9.5 : 11 } },
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: m ? 8 : 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: m ? 8 : 10 } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: m ? 14 : 16, bottom: 6 }],
      series: list.map(({ s, pts }) => {
        const v0 = pts[0][1]
        return {
          name: s.label, type: 'line', showSymbol: false, connectNulls: true,
          lineStyle: { width: 1.4 },
          data: pts.map((p) => [disc.get(p[0]), +(p[1] / v0 * 100).toFixed(2)]),
        }
      }),
    }, true)
  } else {
    const s = byCode.value[curCode.value]
    if (!s) { chart.clear(); return }
    const pts = windowPoints(s)
    chart.setOption({
      ...base,
      title: { text: `${s.name}(${s.unit || ''}) · ${s.source || ''}`, left: 0, top: 0, textStyle: { fontSize: m ? 11 : 13 } },
      tooltip: { trigger: 'axis', confine: true, textStyle: { fontSize: m ? 10 : 12 } },
      xAxis: { type: 'category', data: pts.map((p) => p[0]), boundaryGap: false, axisLabel: { fontSize: m ? 8 : 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: m ? 8 : 10 } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: m ? 14 : 16, bottom: 6 }],
      series: [{ name: s.label, type: 'line', showSymbol: false,
                 lineStyle: { width: 1.6, color: '#2d6cdf' }, itemStyle: { color: '#2d6cdf' },
                 areaStyle: { opacity: 0.06, color: '#2d6cdf' },
                 data: pts.map((p) => p[1]) }],
    }, true)
  }
}

watch(range, () => nextTick(() => { renderCore(); render() }))
watch([curCat, curCode, normalize], () => nextTick(render))
watch(expanded, (v) => { if (v) nextTick(() => { render(); chart && chart.resize() }) })
// 端切换(旋转屏/拖窗口):图参数随端重渲染,尺寸由 onResize 兜
watch(isMobile, () => nextTick(() => { renderCore(); render() }))
</script>

<template>
  <div class="cmd" :class="{ m: isMobile }">
    <div v-if="error" class="err">加载失败:{{ error }}<template v-if="loading"> …重试中</template></div>
    <div v-else-if="!data" class="empty">数据加载中…</div>

    <template v-if="data">
      <!-- 新鲜度红条:任一序列超频段阈值(学习 etf 页教训,断链要自己说话) -->
      <p v-if="staleList.length" class="stale-warn">
        ⚠ {{ staleList.length }} 条序列陈旧:{{ staleList.map((s) => s.label).join('、') }}
        ——采集链疑似断档(单序列源故障会降级留旧数据)。检查:
        `python -m collector.run {{ cur }}` 手动补跑,job 日志看哪个源失败。
      </p>

      <!-- 首屏:商品切换 + 四件套大卡 -->
      <section class="card">
        <div class="title-row">
          <h3>{{ meta.title }}</h3>
          <span class="sub">数据截至 {{ data.updated_at }} · 免费源 · 研究看板,非投资建议</span>
          <span class="tools">
            <select v-model="range">
              <option value="1y">近 1 年</option>
              <option value="3y">近 3 年</option>
              <option value="5y">近 5 年</option>
              <option value="all">全部</option>
            </select>
          </span>
        </div>
        <!-- 商品切换独立一行:八个商品在标题行放不下;移动端横向滚动不换行 -->
        <div class="seg-bar">
          <button v-for="c in CMDS" :key="c.key" type="button" class="seg-btn"
                  :class="{ on: cur === c.key }" @click="switchCmd(c.key)">{{ c.name }}</button>
        </div>
        <p v-if="data.guide" class="guide">💡 {{ data.guide }}</p>
        <div class="kpis">
          <div v-for="h in coreCards" :key="h.code" class="kpi" @click="pickCore(h.s)">
            <div class="k-tag">{{ h.tag }}</div>
            <div class="k-label">{{ h.s.label }}</div>
            <div class="k-val">{{ num(h.s.stats.latest, 2) }}<em class="k-unit">{{ h.s.unit }}</em></div>
            <div class="k-row">
              <span :class="cls(h.s.stats.chg_4w)">4周 {{ sign(h.s.stats.chg_4w, 1) }}</span>
              <span class="pct" :title="'52周区间 ' + num(h.s.stats.lo_52w) + ' ~ ' + num(h.s.stats.hi_52w)">
                52周分位 {{ num((h.s.stats.pct_52w ?? 0) * 100, 0) }}%
                <i class="pbar"><b :style="{ width: ((h.s.stats.pct_52w ?? 0) * 100).toFixed(0) + '%' }" /></i>
              </span>
            </div>
            <div class="k-desc">{{ h.s.desc || h.note }}</div>
          </div>
        </div>
      </section>

      <!-- 首屏:核心图 -->
      <section class="card">
        <div ref="coreEl" class="chart" :style="{ height: isMobile ? '280px' : '400px' }"></div>
        <p class="dim tip">{{ meta.chart.tip }}</p>
      </section>

      <!-- 全部指标:默认折叠,首屏减负 -->
      <section class="card fold">
        <button type="button" class="fold-btn" @click="expanded = !expanded">
          {{ expanded ? '▲ 收起全部指标' : `▼ 展开全部 ${series.length} 个指标(价格/持仓/工业需求/宏观因子)` }}
        </button>
        <template v-if="expanded">
          <div class="toolbar tabs">
            <button v-for="c in data.cats" :key="c" type="button" class="tab"
                    :class="{ on: c === curCat }" @click="pickCat(c)">{{ c }}</button>
            <span class="tools">
              <label class="norm"><input type="checkbox" v-model="normalize"> 归一化对比(当前分类)</label>
            </span>
          </div>
          <div ref="chartEl" class="chart" :style="{ height: isMobile ? '300px' : '420px' }"></div>
          <p v-if="!normalize && byCode[curCode]?.desc" class="dim tip">📖 {{ byCode[curCode].desc }}</p>
          <p class="dim tip">点指标卡切换单序列;勾「归一化对比」叠加当前分类全部序列(起点=100,只比相对涨跌)。</p>

          <div v-for="c in data.cats" :key="c" class="ind-group">
            <h3>{{ c }}<span class="sub">点击卡片看走势</span></h3>
            <div class="ind-rows">
              <div v-for="s in series.filter((x) => x.cat === c)" :key="s.code" class="ind"
                   :class="{ on: curCode === s.code, dead: s.stats.stale }" @click="pick(s)">
                <div class="i-head">
                  <span class="i-name">{{ s.label }}</span>
                  <span class="i-freq">{{ s.freq }}</span>
                  <span v-if="s.stats.stale" class="i-stale">陈旧{{ s.stats.stale_days }}天</span>
                </div>
                <div class="i-body">
                  <span class="i-val">{{ num(s.stats.latest) }}<em>{{ s.unit }}</em></span>
                  <span class="i-chg" :class="cls(s.stats.chg_4w)">4周 {{ sign(s.stats.chg_4w, 1) }}</span>
                  <span class="i-chg" :class="cls(s.stats.chg_1y)">1年 {{ sign(s.stats.chg_1y, 0) }}</span>
                </div>
                <div class="i-desc" v-if="s.desc">{{ s.desc }}</div>
                <div class="i-foot" :title="s.name + ' · ' + (s.source || '')">{{ s.name }} · {{ s.source }}</div>
              </div>
            </div>
          </div>

          <p class="dim foot">
            口径:伦敦金/银现货替代已收费的 LBMA 定盘价;ETF 隐含价=总价值/持仓(与盘面价差应<1%);
            SGE 溢价率=内盘/(国际价×中间价折算)-1。CFTC 非商业走官网 legacy 口径。
            白银工业需求代理:多晶硅(广期所 PS)/光伏ETF/沪锌铅(伴生矿)。
            铜:LME/COMEX 库存无免费结构化源,仅有沪铜滚动近 3 月(诚实弱项);
            铜金比=LME铜(美元/吨)÷伦敦金(美元/盎司),沪伦比>1=内盘溢价。
            铝:LME 库存同无免费源、CFTC 铝流动性极低不采(定价权在 LME/SHFE);
            氧化铝=电解铝直接原料(1.93 吨产 1 吨,广期所 2023-06 上市)。
            碳酸锂:现货(SMM/百川)与锂矿成本线无免费源不采;期货 2023-07 上市历史短,
            需求代理=新能车/电池ETF(新浪)。
            原油:EIA 周度库存与贝克休斯钻机数免费源已停更(2025-09/2024-02)不采记 P2;
            取暖油比价为无量纲比值(HO/CL),裂解价差绝对值需单位校准故用比值。
            黑色系:钢厂盘面利润=螺纹−1.6×铁矿−0.5×焦煤(吨钢配比口径);普氏与港口库存无免费源。
            农产品:猪粮比为期货口径近似官方(生猪/玉米);能繁母猪存栏无免费源;生猪期货 2021 上市历史短。
            供需平衡表(WGC/Silver Institute/USGS)与因子记分卡属 P2,先看数再谈模型
            (设计文档:docs/设计-大宗商品供需模块-黄金.md / -白银.md)。
          </p>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.cmd { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h3 { margin: 0 0 4px; font-size: 15px; }
.card .sub { font-weight: normal; color: var(--sub, #6b7280); font-size: 12px; margin-left: 8px; }
.title-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; margin-bottom: 12px; }
.guide { font-size: 12.5px; line-height: 1.8; color: var(--txt-2, #4b5563); background: var(--bg-soft, #f8fafd);
  border: 1px dashed var(--line, #e5e9f2); border-radius: 8px; padding: 8px 12px; margin: 0 0 12px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); font-size: 12px; }
.stale-warn { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px;
  border-radius: 8px; font-size: 13px; line-height: 1.7; margin-bottom: 12px; }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }

/* 商品切换条:独立一行,桌面自动换行,移动端横向滚动 */
.seg-bar { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
.seg-btn { font-size: 13px; padding: 5px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt-2, #4b5563); }
.seg-btn.on { border-color: var(--accent, #2d6cdf); color: #fff; background: var(--accent, #2d6cdf); font-weight: 600; }

/* ---- 移动端(≤640px,isMobile 注入 .m 类;桌面注入同名 class 可直接验证) ---- */
.m .card { padding: 10px 12px; border-radius: 10px; }
.m .card h3 { font-size: 14px; }
.m .title-row { align-items: flex-start; }
.m .title-row .sub { font-size: 11px; margin-left: 0; width: 100%; order: 3; }
.m .title-row .tools { margin-left: 0; }
.m .seg-bar { flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
  scrollbar-width: none; margin-bottom: 10px; }
.m .seg-bar::-webkit-scrollbar { display: none; }
.m .seg-btn { flex: 0 0 auto; padding: 4px 12px; font-size: 12.5px; white-space: nowrap; }
.m .guide { font-size: 12px; padding: 7px 10px; line-height: 1.7; }
.m .kpis { grid-template-columns: repeat(2, 1fr); gap: 8px; }
.m .kpi { padding: 10px 11px; }
.m .k-val { font-size: clamp(17px, 5.6vw, 21px); margin: 4px 0 3px; }
.m .k-row { font-size: 11px; }
.m .k-desc { font-size: 11px; }
.m .pbar { width: 38px; }
.m .tip { font-size: 11px; line-height: 1.6; }
.m .fold-btn { font-size: 12px; padding: 8px 0; }
.m .tab { font-size: 12px; padding: 5px 10px; }
.m .ind-rows { grid-template-columns: 1fr; }
.m .foot { font-size: 11px; }
.m .stale-warn { font-size: 12px; }

/* 四件套:更少更大,一眼扫完 */
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.kpi { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 12px 14px; cursor: pointer;
  background: var(--bg-soft, #f8fafd); transition: border-color .15s; }
.kpi:hover { border-color: var(--accent-line, #c4d7f6); }
.k-tag { font-size: 10.5px; color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe);
  border-radius: 4px; padding: 1px 6px; display: inline-block; margin-bottom: 4px; }
.k-label { font-size: 14px; font-weight: 600; }
.k-val { font-size: 26px; font-weight: 700; margin: 6px 0 4px; font-variant-numeric: tabular-nums; }
.k-unit { font-style: normal; font-size: 11px; font-weight: normal; color: var(--sub, #9ca3af); margin-left: 4px; }
.k-row { display: flex; justify-content: space-between; align-items: center; font-size: 12px;
  font-variant-numeric: tabular-nums; gap: 8px; flex-wrap: wrap; }
.pct { color: var(--sub, #6b7280); display: inline-flex; align-items: center; gap: 5px; }
.pbar { width: 46px; height: 5px; border-radius: 3px; background: var(--line-soft, #e5e9f2); overflow: hidden; }
.pbar b { display: block; height: 100%; background: var(--accent, #2d6cdf); opacity: .55; }
.k-desc { font-size: 11.5px; color: var(--sub, #6b7280); line-height: 1.5; margin-top: 6px;
  border-top: 1px dashed var(--line, #e5e9f2); padding-top: 5px; }

.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tabs { flex-wrap: wrap; }
.tab { font-size: 13px; padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt, #374151); }
.tab.on { border-color: var(--accent, #2d6cdf); color: var(--accent, #2d6cdf); background: #eff6ff; font-weight: 600; }
.tools { margin-left: auto; display: inline-flex; align-items: center; gap: 10px; font-size: 12px; color: var(--sub, #6b7280); }
.title-row .tools { margin-left: auto; }
.tools select { font-size: 12px; padding: 3px 6px; border-radius: 6px; border: 1px solid var(--line, #e5e9f2); }
.norm { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
.chart { width: 100%; }
.tip { margin: 8px 2px 0; }

.fold-btn { width: 100%; font-size: 13px; padding: 9px 0; border-radius: 8px; cursor: pointer;
  border: 1px dashed var(--line, #d5dbe8); background: var(--bg-soft, #f8fafd); color: var(--txt-2, #4b5563); }
.fold-btn:hover { border-color: var(--accent-line, #c4d7f6); color: var(--accent, #2d6cdf); }

.ind-group { margin-top: 14px; }
.ind-rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 8px; margin-top: 10px; }
.ind { border: 1px solid var(--line, #e5e9f2); border-radius: 9px; padding: 8px 10px; cursor: pointer;
  transition: border-color .15s; }
.ind:hover { border-color: var(--accent-line, #c4d7f6); }
.ind.on { border-color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe); }
.ind.dead { opacity: .72; }
.i-head { display: flex; align-items: center; gap: 6px; }
.i-name { font-size: 13px; font-weight: 600; }
.i-freq { font-size: 10px; color: var(--sub, #9ca3af); border: 1px solid var(--line, #e5e9f2);
  border-radius: 4px; padding: 0 4px; }
.i-stale { font-size: 10px; color: #b91c1c; background: #fef2f2; border-radius: 4px; padding: 0 4px; }
.i-body { display: flex; align-items: baseline; gap: 10px; margin-top: 3px; flex-wrap: wrap;
  font-variant-numeric: tabular-nums; }
.i-val { font-size: 15px; font-weight: 700; }
.i-val em { font-style: normal; font-size: 10px; color: var(--sub, #9ca3af); margin-left: 3px; }
.i-chg { font-size: 11px; }
.i-desc { font-size: 11px; color: var(--sub, #6b7280); line-height: 1.5; margin-top: 3px; }
.i-foot { font-size: 10.5px; color: var(--sub, #9ca3af); margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.foot { line-height: 1.8; padding: 8px 4px 0; }
/* 移动规则统一走 .m 类(见上),不再用 @media——断点由 useMediaQuery(640px) 单点维护 */
</style>
