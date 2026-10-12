<script setup>
// 掘金台:全库 A股/港股的「值得买」决策看板。
// 数据 /api/pick/stars(强推+统计+情绪) /cloud(散点) /board(清单),三者共享
// 后端进程内缓存,并发拉取无额外成本。口径详见页面底部说明与 app/api/pick.py 头注。
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'
import { MOBILE_QUERY } from '../lib/useMediaQuery'

const router = useRouter()

const meta = ref(null)
const stats = ref(null)
const stars = ref([])
const cloud = ref([])
const board = ref({ total: 0, items: [] })
const industries = ref([])
const loading = ref(true)
const boardLoading = ref(false)
const error = ref('')

// —— 筛选状态(hash query 同步,刷新/分享可恢复) ——
const market = ref('')
const tier = ref('')
const industry = ref('')
const keyword = ref('')
const sort = ref('pick')
const order = ref('desc')
const page = ref(1)
const PAGE_SIZE = 50

// 排除行业:默认把银行整体排除(A股行业字典叫「货币金融服务」、港股叫「银行」)。
// hash 有 ex 参数(哪怕空)以 hash 为准,否则落默认——用户主动清空后刷新不会"复活"。
const BANK_INDUSTRIES = ['货币金融服务', '银行']
const exIndustries = ref([...BANK_INDUSTRIES])

const TIER_META = {
  strong: { label: '强推', cls: 't-strong' },
  buy: { label: '值得买', cls: 't-buy' },
  watch: { label: '观察', cls: 't-watch' },
  na: { label: '未评级', cls: 't-na' },
  avoid: { label: '回避', cls: 't-avoid' },
}
const TIER_TABS = [
  { key: '', label: '全部' },
  { key: 'strong', label: '强推' },
  { key: 'buy', label: '值得买' },
  { key: 'watch', label: '观察' },
  { key: 'avoid', label: '回避' },
]
const SORTS = [
  { key: 'pick', label: '入手分' },
  { key: 'recommend', label: '推荐分R' },
  { key: 'discount', label: '安全边际' },
  { key: 'div_yield', label: '股息率' },
  { key: 'pb_pctile', label: 'PB分位' },
  { key: 'fraud', label: '造假分' },
  { key: 'market_cap', label: '市值' },
]
const MARKET_TABS = [
  { key: '', label: '全部' },
  { key: 'A', label: 'A股' },
  { key: 'HK', label: '港股' },
]

const totalAll = computed(() => {
  if (!stats.value) return 0
  const scope = market.value ? { [market.value]: stats.value[market.value] } : stats.value
  return Object.values(scope).reduce((a, m) => a + (m ? Object.values(m).reduce((x, y) => x + y, 0) : 0), 0)
})
const tierStat = (t) => {
  if (!stats.value) return 0
  if (market.value) return stats.value[market.value]?.[t] || 0
  return (stats.value.A?.[t] || 0) + (stats.value.HK?.[t] || 0)
}
const mktTotal = (m) => {
  if (!stats.value?.[m]) return 0
  return Object.values(stats.value[m]).reduce((a, b) => a + b, 0)
}

// —— 数据装载 ——
let boardSeq = 0
async function loadBoard() {
  const seq = ++boardSeq
  boardLoading.value = true
  try {
    const d = await get('/pick/board', {
      market: market.value, tier: tier.value, industry: industry.value,
      ex_industry: exIndustries.value.join(','),
      keyword: keyword.value, sort: sort.value, order: order.value,
      page: page.value, page_size: PAGE_SIZE,
    })
    if (seq !== boardSeq) return
    board.value = d
  } catch (e) {
    if (seq === boardSeq) error.value = e.message
  } finally {
    if (seq === boardSeq) boardLoading.value = false
  }
}
async function loadStars() {
  const d = await get('/pick/stars', {
    limit: 12, market: market.value, ex_industry: exIndustries.value.join(','),
  })
  meta.value = d.meta
  stats.value = d.stats
  stars.value = d.stars
}
async function loadCloud() {
  cloud.value = await get('/pick/cloud', { market: market.value, ex_industry: exIndustries.value.join(',') })
  await nextTick()
  drawCloud()
}
async function loadIndustries() {
  const indRes = await get('/securities/industries', { market: market.value || undefined }).catch(() => [])
  industries.value = (Array.isArray(indRes) ? indRes : []).map((i) => i.industry).filter(Boolean)
}
// 市场切换:全页联动(统计 chips / 强推卡 / 散点 / 行业字典 / 清单),hash 同步
async function setMarket(m) {
  if (market.value === m) return
  market.value = m
  industry.value = ''
  page.value = 1
  syncHash()
  await Promise.all([loadStars(), loadCloud()]).catch((e) => { error.value = e.message })
  loadIndustries()
  loadBoard()
}
function syncHash() {
  const q = new URLSearchParams()
  for (const [k, v] of [['market', market.value], ['tier', tier.value], ['industry', industry.value],
    ['ex', exIndustries.value.join(',')], ['kw', keyword.value],
    ['sort', sort.value], ['order', order.value], ['page', page.value > 1 ? page.value : '']]) {
    if (v !== '' && v != null) q.set(k, v)
  }
  const qs = q.toString()
  history.replaceState(null, '', `#/pick${qs ? '?' + qs : ''}`)
}
// 排除行业变更:统计/强推卡/散点/清单全页联动
async function applyEx() {
  page.value = 1
  syncHash()
  await Promise.all([loadStars(), loadCloud()]).catch((e) => { error.value = e.message })
  loadBoard()
}
function removeEx(name) {
  exIndustries.value = exIndustries.value.filter((i) => i !== name)
  applyEx()
}
// 排除面板:面板内只改草稿,「完成」才生效(避免逐项勾选时连环打接口)
const exPanel = ref(false)
const exSearch = ref('')
const pendingEx = ref([])
const exChoices = computed(() => {
  const kw = exSearch.value.trim()
  return industries.value.filter((i) => !kw || i.includes(kw))
})
function openExPanel() {
  pendingEx.value = [...exIndustries.value]
  exSearch.value = ''
  exPanel.value = true
}
function toggleEx(i) {
  pendingEx.value = pendingEx.value.includes(i)
    ? pendingEx.value.filter((x) => x !== i)
    : [...pendingEx.value, i]
}
function confirmEx() {
  exIndustries.value = [...pendingEx.value]
  exPanel.value = false
  applyEx()
}
function setFilter(fn) {
  fn()
  page.value = 1
  syncHash()
  loadBoard()
}
function setSort(key) {
  if (sort.value === key) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else { sort.value = key; order.value = key === 'pb_pctile' ? 'asc' : 'desc' }
  setFilter(() => {})
}

let kwTimer = null
function onKeyword() {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => setFilter(() => {}), 350)
}

// —— 散点云 ——
// 模板 ref 必须是 ref() 才能接到 <div ref="chartEl">,普通 let 永远是 null
const chartEl = ref(null)
let chart = null
function pickColor(t) {
  return t === 'strong' ? '#c0392b' : t === 'buy' ? '#2d6cdf' : '#9aa4b2'
}
async function drawCloud() {
  if (!chartEl.value) return
  try { await ensureEcharts() } catch { return }
  const echarts = window.echarts
  if (!echarts) return
  if (!chart) chart = echarts.init(chartEl.value)
  const pts = cloud.value.filter((p) => p.pick != null)
  const size = (p) => {
    const cap = p.market_cap || 0
    return Math.max(5, Math.min(24, Math.sqrt(cap / 1e8) * 1.1))
  }
  chart.setOption({
    grid: { left: 48, right: 20, top: 30, bottom: 46 },
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const d = p.data
        const disc = d.marg == null ? '—' : `安全边际 ${d.marg.toFixed(0)}%`
        return `<b>${d.name}</b> ${d.code}<br/>${TIER_META[d.tier].label} · 入手分 ${d.value[1]}<br/>${disc}`
      },
    },
    xAxis: {
      name: '安全边际 %', nameLocation: 'middle', nameGap: 26,
      min: -10, max: 70,
      splitLine: { show: true, lineStyle: { color: '#eef1f5' } },
      axisLabel: { formatter: '{value}' },
    },
    yAxis: { name: '入手分', max: 100, min: 30, splitLine: { show: true, lineStyle: { color: '#eef1f5' } } },
    series: [{
      type: 'scatter',
      data: pts.map((p) => ({
        name: p.name,
        value: [Math.max(-10, Math.min(70, -(p.discount ?? 1) * 100)), p.pick],
        marg: p.discount == null ? null : -p.discount * 100,
        symbolSize: size(p),
        itemStyle: { color: pickColor(p.tier), opacity: p.tier === 'watch' ? 0.35 : 0.78 },
        code: p.code, tier: p.tier,
      })),
      markLine: {
        silent: true, symbol: 'none', animation: false,
        lineStyle: { type: 'dashed', color: '#c0392b', opacity: 0.5 },
        label: { formatter: '强推线', position: 'insideEndTop', fontSize: 10, color: '#c0392b' },
        data: [{ yAxis: 78 }],
      },
    }],
    // 港股菱形/A股圆点由 series 级 symbol 区分不了,统一圆点,悬浮可见代码即可
  })
  chart.resize()
}
function onCloudClick(params) {
  if (params.data?.code) router.push(`/stock/${params.data.code}`)
}

const isMobile = ref(window.matchMedia(MOBILE_QUERY).matches)
let mq = null
function onResize() { chart?.resize() }
function fmtCap(v, cur) {
  if (v == null) return '—'
  const yi = v / 1e8
  return `${yi.toFixed(yi >= 100 ? 0 : 1)}亿${cur === 'HKD' ? '港' : ''}`
}
function fmtPct(v, digits = 1) {
  return v == null ? '—' : `${(v * 100).toFixed(digits)}%`
}
function pctClass(v) {
  if (v == null) return 'flat'
  return v < -0.01 ? 'marg-pos' : v > 0.2 ? 'down' : 'flat'
}
function pctText(v) {
  if (v == null) return '—'
  const a = Math.abs(v * 100).toFixed(0)
  return v < 0 ? `低${a}%` : `高${a}%`
}

onMounted(async () => {
  mq = window.matchMedia(MOBILE_QUERY)
  const onMq = (e) => { isMobile.value = e.matches }
  mq.addEventListener?.('change', onMq)
  window.addEventListener('resize', onResize)

  // hash 恢复
  const q = new URLSearchParams(location.hash.split('?')[1] || '')
  if (q.get('market')) market.value = q.get('market')
  if (q.get('tier')) tier.value = q.get('tier')
  if (q.get('industry')) industry.value = q.get('industry')
  if (q.has('ex')) exIndustries.value = q.get('ex') ? q.get('ex').split(',').filter(Boolean) : []
  if (q.get('kw')) keyword.value = q.get('kw')
  if (q.get('sort')) { sort.value = q.get('sort') }
  if (q.get('order')) order.value = q.get('order')
  if (q.get('page')) page.value = Math.max(1, Number(q.get('page')) || 1)

  try {
    await loadStars()
    await loadIndustries()
    await loadCloud()
    chart?.on('click', onCloudClick)
    loading.value = false
    await loadBoard()
  } catch (e) {
    error.value = e.message
    loading.value = false
  }
})
onBeforeUnmount(() => {
  clearTimeout(kwTimer)
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="pick-page">
    <div v-if="loading" class="loading">正在合成全市场推荐…(首次约 2 秒)</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <template v-else>
      <!-- ── 头部:一句话结论 + 数据口径 + 情绪 ── -->
      <div class="card head-card">
        <div class="head-row">
          <div>
            <h2 class="head-title">掘金台 · 今日值得买</h2>
            <div class="head-sub">
              覆盖 A股 {{ mktTotal('A') }} · 港股 {{ mktTotal('HK') }} 只
              · 评分日 {{ meta?.trade_date }} · 行情 {{ meta?.quote_date }}
            </div>
          </div>
          <div v-if="meta?.sentiment" class="senti" :class="'st-' + (meta.sentiment.t >= 60 ? 'hot' : meta.sentiment.t <= 30 ? 'cold' : 'mid')">
            大盘情绪 <b>{{ meta.sentiment.state }}</b>
            <span v-if="meta.sentiment.t != null" class="senti-t">温度 {{ meta.sentiment.t }}</span>
          </div>
        </div>
        <div class="mkt-row">
          <div class="tabs">
            <button v-for="t in MARKET_TABS" :key="t.key" class="tab" :class="{ active: market === t.key }"
              @click="setMarket(t.key)">{{ t.label }}</button>
          </div>
          <span class="mkt-hint">{{ market === 'A' ? '仅 A 股' : market === 'HK' ? '仅港股' : 'A股 + 港股' }}</span>
          <div class="ex-wrap">
            <span class="ex-label">排除行业</span>
            <span v-for="name in exIndustries" :key="name" class="ex-chip">
              {{ name }}<i class="ex-x" title="取消排除" @click="removeEx(name)">×</i>
            </span>
            <button class="ex-add" @click="openExPanel">+ 添加</button>
          </div>
        </div>
        <div class="stat-chips">
          <button class="chip c-strong" :class="{ on: tier === 'strong' }" @click="setFilter(() => tier = 'strong')">
            强推 <b>{{ tierStat('strong') }}</b>
          </button>
          <button class="chip c-buy" :class="{ on: tier === 'buy' }" @click="setFilter(() => tier = 'buy')">
            值得买 <b>{{ tierStat('buy') }}</b>
          </button>
          <button class="chip c-watch" :class="{ on: tier === 'watch' }" @click="setFilter(() => tier = 'watch')">
            观察 <b>{{ tierStat('watch') }}</b>
          </button>
          <button class="chip c-na" :class="{ on: tier === '' }" @click="setFilter(() => tier = '')">
            全部 <b>{{ totalAll }}</b>
          </button>
          <button class="chip c-avoid" :class="{ on: tier === 'avoid' }" @click="setFilter(() => tier = 'avoid')">
            回避 <b>{{ tierStat('avoid') }}</b>
          </button>
        </div>
      </div>

      <!-- ── 强推卡片区 ── -->
      <div v-if="stars.length" class="stars-sec">
        <div class="sec-title">★ 强推名单 <span class="sec-sub">入手分前列 · 安全边际 / PB 分位 / 股息至少一项过硬 · 点击卡片看详情</span></div>
        <div class="stars-grid">
          <div v-for="s in stars" :key="s.sid" class="pick-card" @click="router.push(`/stock/${s.code}`)">
            <div class="pc-head">
              <span class="pc-name">{{ s.name }}</span>
              <router-link v-if="s.research" :to="'/research/' + s.research.slug" class="rs-flag"
                :title="s.research.title + '(结论:' + s.research.conclusion + ')'" @click.stop>研</router-link>
              <span class="pc-code">{{ s.code }}</span>
              <span class="badge">{{ s.market === 'HK' ? '港股' : 'A股' }}</span>
            </div>
            <div class="pc-score-row">
              <span class="pc-pick">{{ Math.round(s.pick) }}</span>
              <span class="pc-pick-label">入手分</span>
              <span class="pc-price">
                <b>{{ s.price?.toFixed(2) }}</b>
                <span :class="s.change_pct >= 0 ? 'up' : 'down'">{{ s.change_pct == null ? '' : (s.change_pct * 100).toFixed(2) + '%' }}</span>
              </span>
              <span class="tier-badge" :class="TIER_META[s.tier].cls">{{ TIER_META[s.tier].label }}</span>
            </div>
            <div class="pc-metrics">
              <div class="pm"><i>安全边际</i><b :class="s.discount < -0.01 ? 'marg-pos' : 'flat'">{{ s.discount == null ? '—' : pctText(s.discount) }}</b></div>
              <div class="pm"><i>PB十年分位</i><b>{{ s.pb_pctile == null ? '—' : s.pb_pctile.toFixed(0) + '%' }}</b></div>
              <div class="pm"><i>股息率</i><b>{{ fmtPct(s.div_yield) }}</b></div>
              <div class="pm"><i>推荐分R</i><b>{{ s.recommend?.toFixed(0) ?? '—' }}</b></div>
            </div>
            <div class="pc-reasons" :title="s.reasons.join(' · ')">{{ s.reasons.slice(0, 3).join(' · ') }}</div>
            <div v-if="s.risks.length" class="pc-risks"><span v-for="r in s.risks" :key="r" class="risk-flag">{{ r }}</span></div>
          </div>
        </div>
      </div>
      <div v-else class="card wip">当前无强推标的(市场整体偏贵时会自动收缩)</div>

      <!-- ── 散点云 ── -->
      <div class="card cloud-card">
        <div class="sec-title">估值-评分地图 <span class="sec-sub">越靠右安全边际越深,越靠上越值得买 · 红点=强推 蓝点=值得买 灰点=观察 · 点点跳详情</span></div>
        <div ref="chartEl" class="cloud-chart"></div>
      </div>

      <!-- ── 全量清单 ── -->
      <div class="card list-card">
        <div class="toolbar">
          <div class="tabs">
            <button v-for="t in MARKET_TABS" :key="t.key" class="tab" :class="{ active: market === t.key }"
              @click="setMarket(t.key)">{{ t.label }}</button>
          </div>
          <div class="tabs">
            <button v-for="t in TIER_TABS" :key="t.key" class="tab" :class="{ active: tier === t.key }"
              @click="setFilter(() => tier = t.key)">{{ t.label }}</button>
          </div>
          <select class="sel" :value="sort" @change="setFilter(() => sort = $event.target.value)">
            <option v-for="o in SORTS" :key="o.key" :value="o.key">按{{ o.label }}</option>
          </select>
          <select v-if="industries.length" class="sel" :value="industry" @change="setFilter(() => industry = $event.target.value)">
            <option value="">全部行业</option>
            <option v-for="i in industries" :key="i" :value="i">{{ i }}</option>
          </select>
          <input v-model="keyword" placeholder="代码/名称" @input="onKeyword" />
        </div>

        <!-- 桌面表格 -->
        <table v-if="!isMobile" class="grid pick-table">
          <thead>
            <tr>
              <th class="l stick">标的</th>
              <th>评级</th>
              <th :class="{ on: sort === 'pick' }" @click="setSort('pick')">入手分{{ sort === 'pick' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'recommend' }" @click="setSort('recommend')">R{{ sort === 'recommend' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'discount' }" title="现价相对最优买点" @click="setSort('discount')">安全边际{{ sort === 'discount' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'pb_pctile' }" @click="setSort('pb_pctile')">PB分位{{ sort === 'pb_pctile' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'div_yield' }" @click="setSort('div_yield')">股息{{ sort === 'div_yield' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'fraud' }" @click="setSort('fraud')">造假{{ sort === 'fraud' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th class="l">风险 / 理由</th>
              <th :class="{ on: sort === 'market_cap' }" @click="setSort('market_cap')">市值{{ sort === 'market_cap' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(s, i) in board.items" :key="s.sid" @click="router.push(`/stock/${s.code}`)">
              <td class="l stick">
                <div class="nm-row">
                  <span class="rank">{{ (page - 1) * PAGE_SIZE + i + 1 }}</span>
                  <b>{{ s.name }}</b>
                  <router-link v-if="s.research" :to="'/research/' + s.research.slug" class="rs-flag" @click.stop>研</router-link>
                  <span class="cd">{{ s.code }}</span>
                  <span class="badge">{{ s.market === 'HK' ? '港' : 'A' }}</span>
                </div>
              </td>
              <td><span class="tier-badge" :class="TIER_META[s.tier].cls">{{ TIER_META[s.tier].label }}</span></td>
              <td><b :class="{ 'pick-hi': s.tier === 'strong' }">{{ s.pick?.toFixed(0) ?? '—' }}</b></td>
              <td :title="`价值 ${s.value?.toFixed(0) ?? '—'} · 成长 ${s.growth?.toFixed(0) ?? '—'}${s.recommend_gate ? ' · 门槛:' + s.recommend_gate : ''}`">{{ s.recommend?.toFixed(0) ?? '—' }}</td>
              <td :class="pctClass(s.discount)">{{ pctText(s.discount) }}</td>
              <td :class="s.pb_pctile != null && s.pb_pctile <= 25 ? 'marg-pos' : ''">{{ s.pb_pctile?.toFixed(0) ?? '—' }}{{ s.pb_pctile != null ? '%' : '' }}</td>
              <td>{{ fmtPct(s.div_yield) }}</td>
              <td :class="s.fraud > 25 ? 'down' : 'flat'">{{ s.fraud?.toFixed(0) ?? '—' }}</td>
              <td class="l why-cell">
                <span v-for="r in s.risks.slice(0, 2)" :key="r" class="risk-flag">{{ r }}</span>
                <span v-if="!s.risks.length && s.reasons.length" class="why-reason" :title="s.reasons.join(' · ')">{{ s.reasons[0] }}</span>
              </td>
              <td>{{ fmtCap(s.market_cap, s.currency) }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 移动端卡片 -->
        <div v-else class="m-list">
          <div v-for="s in board.items" :key="s.sid" class="m-item" @click="router.push(`/stock/${s.code}`)">
            <div class="m-row1">
              <b>{{ s.name }}</b><span class="cd">{{ s.code }}</span>
              <span class="tier-badge" :class="TIER_META[s.tier].cls">{{ TIER_META[s.tier].label }}</span>
              <span class="m-pick">{{ s.pick?.toFixed(0) ?? '—' }}分</span>
            </div>
            <div class="m-row2">
              <span :class="pctClass(s.discount)">边际{{ pctText(s.discount) }}</span>
              <span>PB{{ s.pb_pctile?.toFixed(0) ?? '—' }}%</span>
              <span>息{{ fmtPct(s.div_yield) }}</span>
              <span>{{ fmtCap(s.market_cap, s.currency) }}</span>
            </div>
            <div v-if="s.risks.length" class="m-row3"><span v-for="r in s.risks.slice(0, 2)" :key="r" class="risk-flag">{{ r }}</span></div>
          </div>
        </div>

        <div class="pager">
          <span>{{ boardLoading ? '加载中…' : `共 ${board.total} 只` }}</span>
          <button :disabled="page <= 1" @click="setFilter(() => page--)">上一页</button>
          <span>{{ page }} / {{ Math.max(1, Math.ceil(board.total / PAGE_SIZE)) }}</span>
          <button :disabled="page >= Math.ceil(board.total / PAGE_SIZE)" @click="setFilter(() => page++)">下一页</button>
        </div>
      </div>

      <!-- ── 口径说明 ── -->
      <div class="card note-card">
        <b>怎么读这块看板</b>
        <p>
          <b>入手分</b>(0~100)= 综合推荐分 R(价值 0.6 + 成长 0.4,已被造假/陷阱/中报恶化门槛把守)
          叠加「当下值不值得下手」:安全边际(现价低于施洛斯/格雷厄姆买点的幅度)、PB 十年分位、股息率、净现金垫子;
          造假与陷阱灰带、ST 关注旗、中报变脸、年报滞后逐项扣分。分数只回答「现在买它有多划算」,不预测涨跌。
        </p>
        <p>
          <b>分层</b>:强推 = 入手分≥78 且安全边际/PB 分位/股息至少一项过硬,无关注旗、无中报恶化、年报不滞后、市值≥30 亿(人民币当量);
          值得买 = 入手分≥65;观察 = ≥45;回避 = 硬门槛(审计非标/风险警示/立案/资不抵债)或 ST 高危或造假分&gt;40 —— 不参与打分;
          无推荐分的深折价烟蒂(施洛斯分≥70)单走烟蒂通道,封顶 65 分。
        </p>
        <p class="note-fine">
          默认排除银行(A股行业字典记「货币金融服务」、港股记「银行」,两处一并排除,可点 chips × 取消);
          数据 {{ meta?.trade_date }} 评分 · {{ meta?.quote_date }} 行情;PB 分位取近十年(观测 &gt;45 天不用);
          港股无陷阱分与 ST 旗(不适用)。本看板是既有评分体系的决策合成,不构成投资建议。
        </p>
      </div>
    </template>

    <Teleport to="body">
      <div v-if="exPanel" class="ex-mask" @click.self="exPanel = false">
        <div class="ex-panel">
          <div class="ex-head">
            <b>排除行业</b>
            <button class="ex-close" @click="exPanel = false">×</button>
          </div>
          <div class="ex-sub">勾中的行业整体从推荐里去掉(两市场一起生效);无行业标注的标的不受影响</div>
          <input v-model="exSearch" class="ex-search" placeholder="搜索行业…" />
          <div class="ex-list">
            <label v-for="i in exChoices" :key="i" class="ex-item">
              <input type="checkbox" :checked="pendingEx.includes(i)" @change="toggleEx(i)" />
              <span>{{ i }}</span>
            </label>
            <div v-if="!exChoices.length" class="loading">无匹配行业</div>
          </div>
          <div class="ex-foot">
            <button class="ex-btn" :disabled="!pendingEx.length" @click="pendingEx = []">清空</button>
            <button class="ex-btn ex-ok" @click="confirmEx">完成</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.head-card { margin-bottom: 14px; }
.head-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.head-title { margin: 0; font-size: 20px; }
.head-sub { color: var(--sub); font-size: 12px; margin-top: 2px; }
.senti { font-size: 13px; color: var(--sub); padding: 4px 12px; border-radius: 16px; border: 1px solid var(--line); }
.senti b { margin: 0 4px; }
.senti-t { opacity: 0.8; }
.st-hot { background: #fdeeee; border-color: #f2c6c6; }
.st-cold { background: #e9f5ef; border-color: #bfe3d0; }

.stat-chips { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.mkt-row { display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.mkt-row .tabs { gap: 6px; }
.mkt-hint { font-size: 12px; color: var(--sub); }
.ex-wrap { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.ex-label { font-size: 12px; color: var(--sub); }
.ex-chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; padding: 2px 8px;
  background: #fdeeee; color: #c0392b; border: 1px solid #f2c6c6; border-radius: 12px; }
.ex-x { font-style: normal; cursor: pointer; font-weight: 700; opacity: 0.7; }
.ex-x:hover { opacity: 1; }
.ex-add { border: 1px dashed var(--line); background: var(--card); border-radius: 12px;
  padding: 2px 10px; font-size: 12px; color: var(--sub); cursor: pointer; }
.ex-add:hover { border-color: var(--accent); color: var(--accent); }

.ex-mask { position: fixed; inset: 0; background: rgba(43, 52, 65, 0.35); z-index: 100;
  display: flex; align-items: center; justify-content: center; }
.ex-panel { background: var(--card); border-radius: 10px; width: min(420px, 92vw);
  max-height: 78vh; display: flex; flex-direction: column; padding: 16px; }
.ex-head { display: flex; align-items: center; justify-content: space-between; }
.ex-close { border: none; background: none; font-size: 20px; cursor: pointer; color: var(--sub); line-height: 1; }
.ex-sub { font-size: 12px; color: var(--sub); margin: 4px 0 10px; }
.ex-search { padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; font-size: 14px; margin-bottom: 8px; }
.ex-list { overflow-y: auto; min-height: 120px; border: 1px solid var(--line); border-radius: 6px; padding: 4px 0; }
.ex-item { display: flex; align-items: center; gap: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.ex-item:hover { background: #f0f5ff; }
.ex-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.ex-btn { padding: 6px 16px; border: 1px solid var(--line); border-radius: 6px; background: var(--card); cursor: pointer; }
.ex-btn:disabled { opacity: 40%; cursor: default; }
.ex-ok { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip {
  border: 1px solid var(--line); background: var(--card); border-radius: 16px;
  padding: 5px 14px; cursor: pointer; font-size: 13px; color: var(--sub);
}
.chip b { margin-left: 4px; font-size: 15px; }
.c-strong b { color: #c0392b; }
.c-buy b { color: var(--accent); }
.c-watch b { color: #6b7686; }
.c-avoid b { color: #9aa4b2; }
.chip.on { border-color: var(--accent); background: #eef4ff; color: var(--accent); }
.chip.on b { color: var(--accent); }

.sec-title { font-weight: 700; margin-bottom: 10px; font-size: 15px; }
.sec-sub { font-weight: 400; font-size: 12px; color: var(--sub); margin-left: 8px; }
.stars-sec { margin-bottom: 14px; }
.stars-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; }
.pick-card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; cursor: pointer; transition: box-shadow 0.15s, transform 0.15s;
}
.pick-card:hover { box-shadow: 0 4px 16px rgba(45, 108, 223, 0.13); transform: translateY(-1px); }
.pc-head { display: flex; align-items: center; gap: 6px; }
.pc-name { font-size: 15px; font-weight: 700; }
.pc-code { color: var(--sub); font-size: 12px; margin-left: auto; }
.pc-score-row { display: flex; align-items: baseline; gap: 6px; margin: 6px 0 4px; }
.pc-pick { font-size: 30px; font-weight: 800; color: #c0392b; line-height: 1; }
.pc-pick-label { color: var(--sub); font-size: 12px; }
.pc-price { margin-left: 10px; font-size: 13px; display: flex; gap: 6px; align-items: baseline; }
.tier-badge { font-size: 11px; padding: 1px 8px; border-radius: 10px; margin-left: auto; white-space: nowrap; }
.t-strong { background: #fdeeee; color: #c0392b; font-weight: 700; }
.t-buy { background: #e8f0fe; color: #2d6cdf; font-weight: 700; }
.t-watch { background: #f0f2f5; color: #6b7686; }
.t-na { background: #f7f8fa; color: #b0b6c0; }
.t-avoid { background: #ececec; color: #8a94a6; }
.pc-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin: 8px 0; }
.pm { text-align: center; background: #f7f9fc; border-radius: 6px; padding: 4px 2px; }
.pm i { display: block; font-style: normal; font-size: 10px; color: var(--sub); }
.pm b { font-size: 13px; }
.marg-pos { color: #2d6cdf; font-weight: 600; }
.pc-reasons { font-size: 12px; color: var(--txt); opacity: 0.85; line-height: 1.5; }
.pc-risks { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.risk-flag { font-size: 10px; padding: 0 6px; border-radius: 8px; background: #fdf6e7; color: #b7791f; border: 1px solid #f0e0bb; white-space: nowrap; }

.cloud-card { margin-bottom: 14px; }
.cloud-chart { height: 340px; width: 100%; }

.list-card { margin-bottom: 14px; }
.sel { padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--card); font-size: 13px; }
.pick-table th.on { color: var(--accent); font-weight: 700; }
.pick-table .rank { color: var(--sub); font-size: 11px; display: inline-block; width: 22px; }
.nm-row { display: flex; align-items: center; gap: 6px; }
.cd { color: var(--sub); font-size: 12px; }
.rs-flag { font-size: 10px; padding: 0 4px; border: 1px solid var(--accent); border-radius: 4px; color: var(--accent); }
.pick-hi { color: #c0392b; }
.why-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.why-reason { font-size: 12px; color: var(--sub); }

.m-list { display: grid; gap: 8px; }
.m-item { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; cursor: pointer; }
.m-row1 { display: flex; align-items: center; gap: 8px; }
.m-pick { margin-left: auto; font-weight: 800; color: #c0392b; font-size: 16px; }
.m-row2 { display: flex; gap: 14px; margin-top: 4px; color: var(--sub); font-size: 12px; flex-wrap: wrap; }
.m-row3 { margin-top: 4px; display: flex; gap: 4px; flex-wrap: wrap; }

.note-card { font-size: 12px; color: var(--sub); line-height: 1.8; }
.note-card b { color: var(--txt); }
.note-card p { margin: 4px 0; }
.note-fine { opacity: 0.75; }

@media (max-width: 600px) {
  .stars-grid { grid-template-columns: 1fr; }
  .cloud-chart { height: 260px; }
  .pc-metrics { grid-template-columns: repeat(4, 1fr); gap: 3px; }
  .pc-pick { font-size: 26px; }
  .head-title { font-size: 17px; }
  .stat-chips .chip { padding: 4px 10px; }
}
</style>
