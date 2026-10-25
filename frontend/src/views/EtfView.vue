<script setup>
/* ETF 综合评估页:成份股联动推荐分 + 跟踪指数估值(PE/PB/股息率,序列累积中) +
 * 调仓跟踪 + 综合评分(读时合成,口径见后端 app/api/etf.py 头注)。
 * 数据源 /api/etf/{list,detail};估值序列自采集器上线日起逐日累积,
 * 满 30 个样本分位与估值分自动纳入——冷启动期总分按剩余权重归一并明示。
 */
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'

const list = ref([])
const detail = ref(null)
const error = ref('')
const loading = ref(false)
const curCode = ref('')
const chartEl = ref(null)
let chart = null
let echarts = null

function parseHash() {
  const q = new URLSearchParams((location.hash.split('?')[1] || ''))
  return q.get('code') || ''
}
function syncHash() {
  const base = '#/etf'
  history.replaceState(null, '', curCode.value ? `${base}?code=${curCode.value}` : base)
}

let seq = 0
async function loadDetail(code) {
  const my = ++seq
  loading.value = true
  error.value = ''
  try {
    const d = await get('/etf/detail', { code })
    if (my !== seq) return            // 竞态守卫:旧响应不覆盖新请求
    detail.value = d
    curCode.value = code
    syncHash()
    await nextTick()
    renderChart()
  } catch (e) {
    if (my === seq) error.value = String(e.message || e)
  } finally {
    if (my === seq) loading.value = false
  }
}

onMounted(async () => {
  try {
    const res = await get('/etf/list')
    list.value = res.items || []
    const want = parseHash()
    const first = list.value.find((i) => i.code === want) || list.value[0]
    if (first) await loadDetail(first.code)
    else error.value = '采集名单为空(先跑 python -m collector.run etf)'
  } catch (e) {
    error.value = String(e.message || e)
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) chart.dispose()
  chart = null
})
function onResize() { if (chart) chart.resize() }

watch(curCode, () => { /* hash 已在 loadDetail 里同步 */ })

// ---- 展示格式化 ----
const TIER_CN = { strong: '强推', buy: '买入', watch: '观察', na: '不定', avoid: '回避' }
const TIER_CLS = { strong: 't-strong', buy: 't-buy', watch: 't-watch', na: 't-na', avoid: 't-avoid' }
const pct = (v, d = 2) => (v == null ? '-' : v.toFixed(d) + '%')
const num1 = (v) => (v == null ? '-' : (+v).toFixed(1))
const num2 = (v) => (v == null ? '-' : (+v).toFixed(2))

const d = computed(() => detail.value || {})
const score = computed(() => d.value.score || {})
const spot = computed(() => d.value.spot || {})
const holdings = computed(() => d.value.holdings || {})
const valuation = computed(() => d.value.valuation || {})
const changes = computed(() => d.value.changes || null)

const pctile = computed(() => valuation.value.pctile || {})
const pePct = computed(() => pctile.value.pe?.pct)
const pbPct = computed(() => pctile.value.pb?.pct)
// 双口径:指数权重(ETF 实际持仓) vs 市值权重(对表 Wind/恒指官方)
const fmtVal = (main, mcap) => (main == null ? '-' : num2(main) + (mcap == null ? '' : ' / ' + num2(mcap)))

// 分层权重条:strong/buy/watch/na/avoid 五段
const TIER_ORDER = ['strong', 'buy', 'watch', 'na', 'avoid']
const TIER_BAR_COLOR = { strong: '#dc2626', buy: '#ea580c', watch: '#2563eb', na: '#cbd5e1', avoid: '#16a34a' }
const tierBar = computed(() => {
  const tw = holdings.value.tier_weight || {}
  return TIER_ORDER.map((k) => ({ key: k, w: tw[k] || 0 })).filter((x) => x.w > 0)
})
const tierBarTotal = computed(() => tierBar.value.reduce((s, x) => s + x.w, 0) || 1)

// 评分构成:三段柱 + 修正项
const scoreBar = computed(() => {
  const p = score.value.parts || {}
  return [
    { k: 'constituent', label: '成份分', v: p.constituent, max: 50, color: '#2563eb' },
    { k: 'valuation', label: '估值分', v: p.valuation, max: 30, color: '#059669' },
    { k: 'dividend', label: '股息分', v: p.dividend, max: 20, color: '#ea580c' },
  ]
})

// ---- 成份股表:表头点击排序(数值列;null 恒垫底,与掘金台同口径) ----
const SORT_GET = {
  weight: (h) => h.weight, recommend: (h) => h.recommend,
  value: (h) => h.value, growth: (h) => h.growth,
  trap: (h) => h.trap, fraud: (h) => h.fraud,
  div_yield: (h) => h.div_yield,
}
const sortKey = ref('weight')
const sortDir = ref('desc')
function toggleSort(k) {
  if (!SORT_GET[k]) return
  if (sortKey.value === k) sortDir.value = sortDir.value === 'desc' ? 'asc' : 'desc'
  else { sortKey.value = k; sortDir.value = 'desc' }
}
const sortedRows = computed(() => {
  const rows = holdings.value.rows || []
  const get = SORT_GET[sortKey.value]
  if (!get) return rows
  const dir = sortDir.value === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const va = get(a), vb = get(b)
    if (va == null) return vb == null ? 0 : 1     // null 垫底,与方向无关
    if (vb == null) return -1
    return va === vb ? 0 : (va > vb ? 1 : -1) * dir
  })
})

// ---- 估值图:上格 PE+股息率,下格 PB+指数点位(点位为底衬,浅灰) ----
function renderChart() {
  if (!chartEl.value) return
  const v = valuation.value
  if (!v.dates || !v.dates.length) return
  if (!echarts) {
    ensureEcharts().then((e) => {
      echarts = e
      window.addEventListener('resize', onResize)
      renderChart()
    }).catch(() => { /* 图卡空着,其余卡片照常 */ })
    return
  }
  const closeMap = new Map((v.close_series || []).map(([d0, c]) => [d0, c]))
  // x 轴:点位史全量日(有)否则估值日;估值点按日期对齐落位
  const dates = (v.close_series || []).length
    ? (v.close_series || []).map(([d0]) => d0)
    : v.dates
  const pe = v.dates.map((x, i) => [x, v.pe_series[i]])
  const pb = v.dates.map((x, i) => [x, v.pb_series[i]])
  const peM = v.dates.map((x, i) => [x, (v.pe_mcap_series || [])[i]])
  const pbM = v.dates.map((x, i) => [x, (v.pb_mcap_series || [])[i]])
  const dv = v.dates.map((x, i) => [x, v.dv_series[i]])
  const close = dates.map((x) => closeMap.has(x) ? [x, closeMap.get(x)] : [x, null])
  // 默认聚焦估值序列近 250 日(点位史更长,滚动条看全程)
  const n = dates.length
  const start = n > 260 ? (1 - 250 / n) * 100 : 0
  const hiddenX = { xAxis: { show: false } }
  if (chart) chart.dispose()
  chart = echarts.init(chartEl.value)
  chart.setOption({
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross', label: { backgroundColor: '#334155' } } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 46, right: 46, top: 26, height: '34%' },
      { left: 46, right: 46, top: '58%', height: '30%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, ...hiddenX },
      { type: 'category', data: dates, gridIndex: 1, axisTick: { show: false },
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: '#6b7280', fontSize: 10, formatter: (x) => x.slice(2).replace('-', '/') } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: 'PE', scale: true,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
      { type: 'value', gridIndex: 0, name: '股息%', position: 'right', scale: true,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { show: false } },
      { type: 'value', gridIndex: 1, name: 'PB', scale: true,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
      { type: 'value', gridIndex: 1, name: '点位', position: 'right', scale: true,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start, end: 100, zoomOnMouseWheel: false, moveOnMouseWheel: true },
      { type: 'slider', xAxisIndex: [0, 1], start, end: 100, bottom: 2, height: 16,
        borderColor: '#e5e7eb', fillerColor: 'rgba(37,99,235,0.08)' },
    ],
    series: [
      { name: 'PE(持仓)', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: pe,
        symbol: 'circle', symbolSize: 3, connectNulls: true, lineStyle: { color: '#dc2626', width: 1.5 } },
      { name: 'PE(Wind口径)', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: peM,
        symbol: 'none', connectNulls: true, lineStyle: { color: '#fCA5A5', width: 1.2, type: 'dashed' } },
      { name: '股息率%', type: 'line', xAxisIndex: 0, yAxisIndex: 1, data: dv,
        symbol: 'none', connectNulls: true, lineStyle: { color: '#ea580c', width: 1.5, type: 'dashed' } },
      { name: 'PB(持仓)', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: pb,
        symbol: 'none', connectNulls: true, lineStyle: { color: '#2563eb', width: 1.5 } },
      { name: 'PB(Wind口径)', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: pbM,
        symbol: 'none', connectNulls: true, lineStyle: { color: '#93c5fd', width: 1.2, type: 'dashed' } },
      { name: '指数点位', type: 'line', xAxisIndex: 1, yAxisIndex: 3, data: close,
        symbol: 'none', connectNulls: true,
        lineStyle: { color: '#cbd5e1', width: 1 }, itemStyle: { color: '#cbd5e1' } },
    ],
  })
}
</script>

<template>
  <div class="etf">
    <div v-if="error && !detail" class="err">加载失败:{{ error }}</div>

    <template v-if="list.length">
      <!-- ① ETF 切换 + 头卡 -->
      <div class="toolbar tabs">
        <button v-for="it in list" :key="it.code" type="button" class="tab"
                :class="{ on: it.code === curCode }" @click="loadDetail(it.code)">
          {{ it.name }}<span class="code">{{ it.code }}</span>
        </button>
      </div>

      <div v-if="loading && !detail" class="empty">加载中…</div>

      <template v-if="detail">
        <section class="card head" :class="TIER_CLS[score.tier] || 't-na'">
          <div class="score-badge">
            <em>{{ score.total ?? '-' }}</em>
            <span>{{ TIER_CN[score.tier] || '不定' }}</span>
          </div>
          <div class="head-meta">
            <div class="title">
              {{ d.name }}<span class="code">{{ d.code }}</span>
              <span class="idx">跟踪 {{ d.index_name || '-' }}</span>
            </div>
            <div class="chips">
              <span class="chip">现价 {{ num2(spot.price) }}<em :class="spot.change_pct >= 0 ? 'up' : 'down'">{{ spot.change_pct >= 0 ? '+' : '' }}{{ pct(spot.change_pct) }}</em></span>
              <span class="chip">规模 {{ num1(spot.scale_yi) }} 亿</span>
              <span class="chip" v-if="spot.premium_pct != null">折溢价 <em :class="spot.premium_pct >= 0 ? 'up' : 'down'">{{ pct(spot.premium_pct) }}</em><i class="sub">(对 {{ spot.nav_date }} 净值 {{ num2(spot.nav) }})</i></span>
              <span class="chip">52周 {{ num2(spot.week52_high) }} / {{ num2(spot.week52_low) }}</span>
              <span class="chip" v-if="d.fees?.mgmt">管理费 {{ d.fees.mgmt }}</span>
              <span class="chip" v-if="d.list_date">上市 {{ d.list_date }}</span>
            </div>
            <div class="chips val-chips">
              <span class="chip v" title="PE = Σ权重/Σ(权重/个股PE):整体法,按 ETF 实际持仓权重">PE <em>{{ num2(valuation.pe) }}</em><i class="sub">持仓口径</i><em v-if="pePct != null">分位 {{ num1(pePct) }}</em></span>
              <span class="chip v" title="PE = Σ市值/Σ(市值/个股PE):市值权重整体法,与 Wind/恒指官方读数同口径">PE <em>{{ num2(valuation.pe_mcap) }}</em><i class="sub">Wind口径</i></span>
              <span class="chip v" title="PB = Σ权重/Σ(权重/个股PB),持仓口径">PB <em>{{ num2(valuation.pb) }}</em><i class="sub">持仓口径</i><em v-if="pbPct != null">分位 {{ num1(pbPct) }}</em></span>
              <span class="chip v" title="PB = Σ市值/Σ(市值/个股PB),与 Wind 同口径">PB <em>{{ num2(valuation.pb_mcap) }}</em><i class="sub">Wind口径</i></span>
              <span class="chip v" title="股息率 = Σ(权重×个股近1年股息率/价格)">股息率 {{ pct(valuation.dv) }}</span>
              <span class="chip v warn" v-if="valuation.sample_days < 30">
                估值序列累积中 {{ valuation.sample_days }}/30 日,分位暂缺
              </span>
              <span class="chip v" v-else>估值样本 {{ valuation.sample_days }} 日</span>
              <span class="chip v" v-if="holdings.source === 'f10'">持仓为季报前十口径(覆盖率 {{ ((holdings.weight_covered || 0) * 100).toFixed(1) }}%)</span>
            </div>
          </div>
        </section>

        <!-- ② 综合评分构成 + 理由/风险 -->
        <section class="card">
          <h3>综合评分构成<span class="sub">成份 50 / 估值 30 / 股息 20,缺项按剩余权重归一;修正项 {{ num1(score.adjust) }}</span></h3>
          <div class="score-parts">
            <div v-for="p in scoreBar" :key="p.k" class="sp" :title="p.v == null ? '暂缺' : p.v + '/' + p.max">
              <em>{{ p.label }}</em>
              <div class="spbar"><i :style="{ width: (p.v == null ? 0 : p.v / p.max * 100) + '%', background: p.color }"></i></div>
              <b>{{ p.v ?? '暂缺' }}</b>
            </div>
          </div>
          <div class="rr">
            <ul v-if="(score.reasons || []).length" class="ok-list">
              <li v-for="(r, i) in score.reasons" :key="i">✓ {{ r }}</li>
            </ul>
            <ul v-if="(score.risks || []).length" class="risk-list">
              <li v-for="(r, i) in score.risks" :key="i">⚠ {{ r }}</li>
            </ul>
          </div>
        </section>

        <!-- ②.5 行业分布 -->
        <section class="card" v-if="(holdings.industry_dist || []).length">
          <h3>行业分布<span class="sub">持仓权重聚合{{ holdings.source === 'f10' ? '(仅前十口径)' : '' }} · 行业口径:{{ holdings.source === 'official' ? '国证官方' : '证券库' }}</span></h3>
          <div class="ind-rows">
            <div v-for="x in holdings.industry_dist" :key="x.name" class="ind-row" :title="x.name + ' ' + (x.weight * 100).toFixed(1) + '% · ' + x.count + ' 只'">
              <em>{{ x.name }}</em>
              <div class="indbar"><i :style="{ width: (x.weight / holdings.industry_dist[0].weight * 100) + '%' }"></i></div>
              <b>{{ (x.weight * 100).toFixed(1) }}%</b>
              <span class="cnt">{{ x.count }} 只</span>
            </div>
          </div>
        </section>

        <!-- ③ 跟踪指数估值序列 -->
        <section class="card">
          <h3>跟踪指数估值<span class="sub">{{ valuation.basis === 'official' ? (d.index_family === 'csi' ? '中证官方权重' : '国证官方权重') : valuation.basis === 'pcf' ? '申赎清单全量权重' : '季报前十权重' }} · 双口径整体法 · 样本逐日累积</span></h3>
          <div v-if="valuation.dates?.length" ref="chartEl" class="chart"></div>
          <p v-else class="empty">估值序列尚未生成(跑一次 python -m collector.run etf)</p>
          <p class="legend">
            <i class="lg-pe"></i>PE 持仓口径 <i class="lg-pem"></i>PE Wind口径
            <i class="lg-dv"></i>股息率 <i class="lg-pb"></i>PB 持仓口径 <i class="lg-pbm"></i>PB Wind口径
            <i class="lg-close"></i>指数点位
            <span class="lg-note">持仓口径=Σ权重/Σ(权重/个股PE),你实际买到组合的估值;Wind口径=Σ市值/Σ(市值/个股PE),与 Wind/恒指官方读数对表</span>
          </p>
        </section>

        <!-- ④ 成份股联动推荐分 -->
        <section class="card">
          <h3>成份股联动<span class="sub">{{ holdings.period }} · {{ holdings.count }} 只 · 覆盖权重 {{ ((holdings.weight_covered || 0) * 100).toFixed(1) }}% · 加权推荐分 {{ num1(holdings.r_weighted) }}(评分覆盖 {{ ((holdings.score_coverage || 0) * 100).toFixed(0) }}%)</span></h3>
          <div class="tierbar" v-if="tierBar.length">
            <span v-for="x in tierBar" :key="x.key" class="seg" :style="{ width: (x.w / tierBarTotal * 100) + '%', background: TIER_BAR_COLOR[x.key] }"
                  :title="TIER_CN[x.key] + ' ' + (x.w * 100).toFixed(1) + '%'">{{ x.w / tierBarTotal > 0.12 ? TIER_CN[x.key] : '' }}</span>
          </div>
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr>
                <th class="num sortable" @click="toggleSort('weight')">权重<i v-if="sortKey==='weight'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th>代码/名称</th><th>行业</th>
                <th class="num sortable" @click="toggleSort('recommend')">推荐分<i v-if="sortKey==='recommend'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th class="num sortable" @click="toggleSort('value')">V<i v-if="sortKey==='value'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th class="num sortable" @click="toggleSort('growth')">G<i v-if="sortKey==='growth'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th class="num sortable" @click="toggleSort('trap')">陷阱<i v-if="sortKey==='trap'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th class="num sortable" @click="toggleSort('fraud')">造假<i v-if="sortKey==='fraud'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th class="num sortable" @click="toggleSort('div_yield')">股息率<i v-if="sortKey==='div_yield'" class="arr">{{ sortDir==='desc' ? '▼' : '▲' }}</i></th>
                <th>分层</th><th>旗</th>
              </tr></thead>
              <tbody>
                <tr v-for="h in sortedRows" :key="h.code">
                  <td class="num">{{ (h.weight * 100).toFixed(2) }}%</td>
                  <td><router-link :to="'/stock/' + h.code" class="code-link">{{ h.name }}<span class="mkt">{{ h.market }}</span></router-link></td>
                  <td class="l">{{ h.industry || '-' }}</td>
                  <td class="num" :class="{ dim: h.recommend == null }">{{ num1(h.recommend) }}</td>
                  <td class="num dim">{{ num1(h.value) }}</td>
                  <td class="num dim">{{ num1(h.growth) }}</td>
                  <td class="num" :class="{ bad: (h.trap || 0) > 20 }">{{ num1(h.trap) }}</td>
                  <td class="num" :class="{ bad: (h.fraud || 0) > 25 }">{{ num1(h.fraud) }}</td>
                  <td class="num dim">{{ h.div_yield == null ? '-' : (h.div_yield * 100).toFixed(1) + '%' }}</td>
                  <td><span class="tier" :class="TIER_CLS[h.tier]">{{ TIER_CN[h.tier] }}</span></td>
                  <td><span v-if="h.gate_flags?.length || h.st_risk === 2" class="flag">⚠ {{ h.gate_flags.join('/') || 'ST高危' }}</span><span v-else class="dim">-</span></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ⑤ 调仓跟踪 -->
        <section class="card" v-if="changes">
          <h3>调仓跟踪<span class="sub">{{ changes.kind === 'month' ? '官方月度快照' : changes.kind === 'pcf' ? '申赎清单月度' : '季报持仓' }} {{ changes.from }} → {{ changes.to }}</span></h3>
          <div class="chg">
            <p v-if="changes.ins.length"><em class="in">调入 {{ changes.ins.length }}</em>
              <span v-for="x in changes.ins" :key="x.code" class="chip chg-chip in">
                <router-link :to="'/stock/' + x.code" class="code-link">{{ x.name }}</router-link> {{ ((x.weight || 0) * 100).toFixed(2) }}%
              </span>
            </p>
            <p v-if="changes.outs.length"><em class="out">调出 {{ changes.outs.length }}</em>
              <span v-for="x in changes.outs" :key="x.code" class="chip chg-chip out">
                <router-link :to="'/stock/' + x.code" class="code-link">{{ x.name }}</router-link> {{ ((x.weight || 0) * 100).toFixed(2) }}%
              </span>
            </p>
            <p v-if="!changes.ins.length && !changes.outs.length" class="empty">本期无调入调出。</p>
          </div>
          <details v-if="changes.wchg?.length">
            <summary>权重变动 {{ changes.wchg.length }} 只</summary>
            <div class="tbl-scroll">
              <table class="tbl">
                <thead><tr><th>代码/名称</th><th class="num">旧权重</th><th class="num">新权重</th><th class="num">变动</th></tr></thead>
                <tbody>
                  <tr v-for="x in changes.wchg" :key="x.code">
                    <td><router-link :to="'/stock/' + x.code" class="code-link">{{ x.name }}</router-link></td>
                    <td class="num">{{ ((x.from || 0) * 100).toFixed(2) }}%</td>
                    <td class="num">{{ ((x.to || 0) * 100).toFixed(2) }}%</td>
                    <td class="num" :class="{ up: (x.to || 0) > (x.from || 0), down: (x.to || 0) < (x.from || 0) }">
                      {{ (((x.to || 0) - (x.from || 0)) * 100).toFixed(2) }}pp
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </section>

        <!-- ⑥ 公告(源接入中) -->
        <section class="card">
          <h3>基金公告</h3>
          <p class="empty">公告源接入中:东财/交易所公开接口暂不覆盖 ETF 公告面;规模、净值、分红变化已由日更的规模/折溢价序列与调仓跟踪覆盖。后续接入巨潮/交易所披露后本卡自动点亮。</p>
        </section>

        <p class="note">口径说明(个股原料均为腾讯行情的 PE-TTM 与 PB,剔除亏损/负净资产样本后取正值):
          <br>① <b>持仓口径 pe/pb</b>:算法=Σ权重 ÷ Σ(权重÷个股PE)。直观读法——把 100 元按 ETF 的实际权重拆着买满成份股,每只投入"权重元"、对应盈利为"权重÷PE 元",加总即整篮子买到的合计盈利,100÷合计盈利就是整篮子的合并市盈率(等价于「篮子总市值÷篮子总盈利」的整体法,只是"投入比例"用指数权重)。权重来源:{{ holdings.source === 'pcf' ? '招商申赎清单各券金额占比' : d.index_family === 'csi' ? '中证官网月度成份权重' : holdings.source === 'official' ? '国证官方月度成份权重' : '季报前十权重' }}。这是「你实际买到的组合」的估值,综合评分的估值分与分位均用此口径。
          <br>② <b>Wind口径 pe_mcap/pb_mcap</b>:算法=Σ总市值 ÷ Σ(总市值÷个股PE)。与①是同一个整体法公式,唯一差别是把每只股票的"投入"从指数权重换成它的总市值——相当于无视指数的权重设计(上限/低波调整),只按公司体量大小合并。此口径与 Wind/恒指官方读数一致(2026-10-26 实测:159263 该口径 9.02/0.85,Wind 显示 9.01/0.85),用于对表验证。设了权重上限的指数(如红利低波)两口径会差 20% 上下:低 PE 央企被指数规则压权重、但市值巨大,是差异主因,属正常现象。
          <br>③ <b>股息率 dv</b>=Σ(权重×个股近一年股息率),指数权重加权。
          <br>另:折溢价=现价÷最新披露单位净值−1(静态);分层阈值与掘金台一致(≥78 强推/≥65 买入/≥45 观察);国证名录官方 PB 快照因口径离群已停用,序列统一自算。数据更新:每日 17:35 etf job。</p>
      </template>
    </template>
    <p v-else-if="!error" class="empty">加载中…</p>
  </div>
</template>

<style scoped>
.etf { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.card .sub { font-weight: normal; color: var(--sub, #6b7280); font-size: 12px; margin-left: 8px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }
.bad { color: #b45309; }

.tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tab { font-size: 13px; padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt, #374151); }
.tab.on { border-color: var(--accent, #2d6cdf); color: var(--accent, #2d6cdf); background: #eff6ff; font-weight: 600; }
.tab .code, .title .code { color: var(--sub, #9ca3af); font-size: 12px; margin-left: 4px; font-weight: normal; }

.head { display: flex; align-items: center; gap: 16px; }
.score-badge { text-align: center; color: #fff; border-radius: 12px; padding: 10px 18px; min-width: 84px;
  background: #6b7280; box-shadow: var(--shadow-m, 0 4px 14px rgba(20,30,55,.16)); }
.score-badge em { display: block; font-style: normal; font-size: 30px; font-weight: 700; line-height: 1.1; }
.score-badge span { font-size: 12px; letter-spacing: 2px; }
.head.t-strong .score-badge { background: #dc2626; }
.head.t-buy .score-badge { background: #ea580c; }
.head.t-watch .score-badge { background: #2563eb; }
.head-meta { flex: 1; min-width: 0; }
.title { font-size: 16px; font-weight: 600; }
.title .idx { color: var(--sub, #6b7280); font-weight: normal; font-size: 13px; margin-left: 10px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { font-size: 12px; background: var(--bg-soft, #f6f8fc); border-radius: 999px; padding: 3px 10px;
  display: inline-flex; align-items: center; gap: 4px; font-variant-numeric: tabular-nums; }
.chip .sub { color: var(--sub, #9ca3af); font-style: normal; font-size: 11px; }
.chip.v { background: #eff6ff; color: #1e40af; }
.chip.v em { font-style: normal; color: #2563eb; font-weight: 600; }
.chip.v.warn { background: #fffbeb; color: #92400e; }

.score-parts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.sp { display: grid; grid-template-columns: 3.2em 1fr 2.6em; align-items: center; gap: 8px; font-size: 12px; }
.sp em { font-style: normal; color: var(--sub, #6b7280); }
.spbar { height: 10px; background: var(--bg-soft, #f3f4f6); border-radius: 5px; overflow: hidden; }
.spbar i { display: block; height: 100%; border-radius: 5px; }
.sp b { font-variant-numeric: tabular-nums; text-align: right; }
.rr { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.rr ul { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.8; }
.ok-list li { color: #374151; }
.risk-list li { color: #92400e; }

.ind-rows { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 28px; }
.ind-row { display: grid; grid-template-columns: 6.5em 1fr 3.6em 3em; align-items: center; gap: 8px; font-size: 12px; }
.ind-row em { font-style: normal; color: var(--txt, #374151); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.indbar { height: 9px; background: var(--bg-soft, #f3f4f6); border-radius: 5px; overflow: hidden; }
.indbar i { display: block; height: 100%; border-radius: 5px; background: var(--accent, #2d6cdf); opacity: 0.85; }
.ind-row b { font-variant-numeric: tabular-nums; text-align: right; }
.ind-row .cnt { color: var(--sub, #9ca3af); text-align: right; }

.chart { width: 100%; height: 360px; }
.legend { font-size: 12px; color: var(--sub, #6b7280); margin: 8px 0 0; line-height: 1.8; }
.legend i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; margin: 0 4px 0 10px; vertical-align: middle; }
.legend .lg-note { display: block; margin-left: 0; color: var(--sub, #9ca3af); font-size: 11px; }
.lg-pe { background: #dc2626; } .lg-pem { background: #fca5a5; }
.lg-dv { background: #ea580c; } .lg-pb { background: #2563eb; }
.lg-pbm { background: #93c5fd; } .lg-close { background: #cbd5e1; }

.tierbar { display: flex; height: 18px; border-radius: 6px; overflow: hidden; margin-bottom: 10px; font-size: 11px; color: #fff; }
.tierbar .seg { display: flex; align-items: center; justify-content: center; white-space: nowrap; overflow: hidden; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 6px 10px; border-bottom: 1px solid #f1f5f9; text-align: left; white-space: nowrap; }
.tbl th { color: var(--sub, #6b7280); font-weight: 500; position: sticky; top: 0; background: var(--card, #fff); }
.tbl th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
.tbl th.sortable:hover { color: var(--accent, #2d6cdf); }
.tbl th .arr { font-style: normal; font-size: 10px; margin-left: 2px; color: var(--accent, #2d6cdf); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .l { max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.tbl-wrap { max-height: 480px; overflow: auto; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
.tbl-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
.code-link { color: var(--accent, #2563eb); text-decoration: none; }
.code-link:hover { text-decoration: underline; }
.mkt { color: var(--sub, #9ca3af); font-size: 11px; margin-left: 4px; }
.tier { font-size: 11px; padding: 1px 8px; border-radius: 999px; }
.t-strong { background: #fef2f2; color: #b91c1c; }
.t-buy { background: #fff7ed; color: #c2410c; }
.t-watch { background: #eff6ff; color: #1d4ed8; }
.t-na { background: #f3f4f6; color: #6b7280; }
.t-avoid { background: #f0fdf4; color: #15803d; }
.flag { color: #b45309; font-size: 12px; }

.chg p { margin: 4px 0; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.chg em { font-style: normal; font-size: 12px; font-weight: 600; min-width: 5em; }
.chg em.in { color: #dc2626; } .chg em.out { color: #16a34a; }
.chg-chip { font-size: 12px; padding: 2px 10px; border-radius: 999px; }
.chg-chip.in { background: #fef2f2; border: 1px solid #fecaca; }
.chg-chip.out { background: #f0fdf4; border: 1px solid #bbf7d0; }
details > summary { cursor: pointer; font-size: 13px; font-weight: 600; margin: 10px 0 8px; }

.note { color: var(--sub, #9ca3af); font-size: 12px; line-height: 1.7; }

@media (max-width: 600px) {
  .head { flex-direction: column; align-items: stretch; gap: 10px; }
  .score-badge { align-self: flex-start; display: flex; align-items: baseline; gap: 8px; padding: 8px 14px; }
  .score-badge em { font-size: 24px; }
  .score-parts { grid-template-columns: 1fr; }
  .rr { grid-template-columns: 1fr; }
  .ind-rows { grid-template-columns: 1fr; }
  .chart { height: 300px; }
  .tbl { font-size: 12px; }
  .card { padding: 12px 11px; }
}
</style>
