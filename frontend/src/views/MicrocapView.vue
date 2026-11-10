<script setup>
// 小微盘投机池:微盘/小票两档 × 资金热度/热门题材/涨停生态 × 排雷记分卡。
// /api/microcap/overview(环境+日历+热榜) /board(清单)共享后端进程缓存,并发拉取。
// 定位是投机选股工具:热钱在炒什么(资金/题材/涨停梯队)是主菜单,排雷是亮灯
// 风控层——亮灯不拦截,🔴+高热度的「雷上加热」要自己看清楚。口径详见底部说明
// 与 app/api/microcap.py 头注。
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '../api/client'
import { MOBILE_QUERY } from '../lib/useMediaQuery'

const router = useRouter()

const overview = ref(null)
const board = ref({ total: 0, items: [] })
const industries = ref([])
const loading = ref(true)
const boardLoading = ref(false)
const error = ref('')

// —— 筛选状态(hash query 同步) ——
const pool = ref('')          // ''=两池 / micro / small
const band = ref('')          // ''=全部 / hot / warm / cool
const risk = ref('')          // ''=全部 / 3 / 2 / 1 / 0
const concept = ref('')
const industry = ref('')
const keyword = ref('')
const sort = ref('heat')
const order = ref('desc')
const page = ref(1)
const PAGE_SIZE = 50

const POOL_TABS = [
  { key: '', label: '两池' },
  { key: 'micro', label: '微盘<25亿' },
  { key: 'small', label: '小票25~60亿' },
]
const BAND_TABS = [
  { key: '', label: '全部热度' },
  { key: 'hot', label: '火爆≥70' },
  { key: 'warm', label: '温热50~70' },
  { key: 'cool', label: '冷<50' },
]
const RISK_TABS = [
  { key: '', label: '全部', cls: '' },
  { key: '3', label: '🔴 确定', cls: 'r3' },
  { key: '2', label: '🟠 高危', cls: 'r2' },
  { key: '1', label: '🟡 关注', cls: 'r1' },
  { key: '0', label: '🟢 安全', cls: 'r0' },
]
const SORTS = [
  { key: 'heat', label: '热度' },
  { key: 'inflow_t', label: '主力净流入' },
  { key: 'streak', label: '连板' },
  { key: 'turn_rate', label: '换手率' },
  { key: 'r5', label: '5日涨幅' },
  { key: 'r20', label: '20日涨幅' },
  { key: 'chg_pct', label: '今日涨幅' },
  { key: 'market_cap', label: '市值' },
  { key: 'rev_line_ratio', label: '营收/退市线' },
  { key: 'discount', label: '安全边际' },
  { key: 'recommend', label: '推荐分R' },
]

const RISK_META = {
  3: { label: '确定', cls: 'rb-3', tip: '已戴帽或年报已触线——披露后必然 ST/退市流程' },
  2: { label: '高危', cls: 'rb-2', tip: '审计非标/立案/逼近营收线或面值/市值线' },
  1: { label: '关注', cls: 'rb-1', tip: '亏损/高负债/连续扣非亏/期报转亏等下滑信号' },
  0: { label: '安全', cls: 'rb-0', tip: '无任何排雷旗' },
}

// —— 数据装载 ——
let boardSeq = 0
async function loadBoard() {
  const seq = ++boardSeq
  boardLoading.value = true
  try {
    const d = await get('/microcap/board', {
      pool: pool.value || undefined, band: band.value || undefined,
      risk: risk.value || undefined, concept: concept.value || undefined,
      industry: industry.value || undefined, keyword: keyword.value || undefined,
      sort: sort.value, order: order.value, page: page.value, page_size: PAGE_SIZE,
    })
    if (seq !== boardSeq) return
    board.value = d
  } catch (e) {
    if (seq === boardSeq) error.value = e.message
  } finally {
    if (seq === boardSeq) boardLoading.value = false
  }
}
async function loadOverview() {
  overview.value = await get('/microcap/overview')
}
async function loadIndustries() {
  const indRes = await get('/securities/industries', { market: 'A' }).catch(() => [])
  industries.value = (Array.isArray(indRes) ? indRes : []).map((i) => i.industry).filter(Boolean)
}
function setFilter(fn) {
  fn()
  page.value = 1
  syncHash()
  loadBoard()
}
function setSortToggle(key) {
  if (sort.value === key) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else { sort.value = key; order.value = 'desc' }
  setFilter(() => {})
}
function setConcept(name) {
  setFilter(() => { concept.value = concept.value === name ? '' : name })
}
function syncHash() {
  const q = new URLSearchParams()
  for (const [k, v] of [['pool', pool.value], ['band', band.value], ['risk', risk.value],
    ['concept', concept.value], ['industry', industry.value], ['kw', keyword.value],
    ['sort', sort.value], ['order', order.value], ['page', page.value > 1 ? page.value : '']]) {
    if (v !== '' && v != null) q.set(k, v)
  }
  const qs = q.toString()
  history.replaceState(null, '', `#/microcap${qs ? '?' + qs : ''}`)
}

let kwTimer = null
function onKeyword() {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => setFilter(() => {}), 350)
}

// —— 展示辅助 ——
const yi = (v) => v == null ? '—' : `${(v / 1e8).toFixed(v >= 1e9 ? 1 : 2)}亿`
const pctS = (v) => v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
const pctNum = (v) => v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`   // 后端已是百分数
const chgClass = (v) => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat'
const lineClass = (r) => r == null ? '' : r < 1 ? 'down' : r < 2 ? 'warn-amber' : ''
const turnPctileTxt = (it) => it.turn_pctile == null ? '—' : `${Math.round(it.turn_pctile * 100)}%`
const ztTxt = (it) => {
  if (it.streak) return `${it.streak}板`
  if (it.dt_today) return '跌停'
  if (it.zha_flag) return '炸板⚠'
  if (it.zt_5d > 0) return `近5日${it.zt_5d}次`
  return ''
}
const ztClass = (it) => it.streak ? 'zt-hot' : it.dt_today ? 'down' : it.zha_flag ? 'warn-amber' : 'flat'
const sealTxt = (it) => it.seal_amt == null ? '' : yi(it.seal_amt)
const flowMissing = computed(() => overview.value && !overview.value.heat?.flow_ok)
const calLabel = computed(() => {
  const c = overview.value?.calendar
  if (!c) return ''
  if (c.state === 'revealing') return `${c.label}披露 ${Math.round((c.progress || 0) * 100)}%`
  const last = c.last_label ? `${c.last_label}已收${c.last_progress != null ? Math.round(c.last_progress * 100) + '%' : ''}` : ''
  const next = c.next_label ? `${c.next_label}还有${c.days_to_next}天` : ''
  return [last, next].filter(Boolean).join(' · ')
})
const riskTitle = (it) => {
  const fin = it.annual_rev != null
    ? `年报营收 ${yi(it.annual_rev)}(退市线 ${yi(it.rev_line)}) · 净利 ${yi(it.annual_np)} · 负债率 ${it.debt_ratio != null ? Math.round(it.debt_ratio * 100) + '%' : '—'}`
    : '年报财务缺'
  return `${(it.risk_labels || []).join(' · ') || '无排雷旗'}\n${fin}`
}
const heatOnMine = (it) => it.risk_level >= 2 && (it.heat || 0) >= 70

onMounted(async () => {
  const mq = window.matchMedia(MOBILE_QUERY)
  const onMq = (e) => { isMobile.value = e.matches }
  mq.addEventListener?.('change', onMq)

  const q = new URLSearchParams(location.hash.split('?')[1] || '')
  if (q.get('pool')) pool.value = q.get('pool')
  if (q.get('band')) band.value = q.get('band')
  if (q.get('risk')) risk.value = q.get('risk')
  if (q.get('concept')) concept.value = q.get('concept')
  if (q.get('industry')) industry.value = q.get('industry')
  if (q.get('kw')) keyword.value = q.get('kw')
  if (q.get('sort')) sort.value = q.get('sort')
  if (q.get('order')) order.value = q.get('order')
  if (q.get('page')) page.value = Math.max(1, Number(q.get('page')) || 1)

  try {
    await Promise.all([loadOverview(), loadIndustries(), loadBoard()])
    loading.value = false
  } catch (e) {
    error.value = e.message
    loading.value = false
  }
})
const isMobile = ref(false)
onBeforeUnmount(() => { clearTimeout(kwTimer) })
</script>

<template>
  <div class="mc-page">
    <div v-if="loading" class="loading">正在合成小微盘池…(首次约 4 秒)</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <template v-else>
      <!-- ── 头部:环境横幅 + 池子/风险分布 ── -->
      <div class="card head-card">
        <div class="head-row">
          <div>
            <h2 class="head-title">小微盘 · 投机雷达</h2>
            <div class="head-sub">
              微盘 {{ overview.pools.micro }} · 小票 {{ overview.pools.small }} 只
              · 评分 {{ overview.trade_date }} · 行情 {{ overview.quote_date }}
              <template v-if="overview.heat?.date"> · 资金流 {{ overview.heat.date }}</template>
            </div>
          </div>
          <div v-if="overview.env?.state" class="senti"
            :class="'st-' + ((overview.env.t ?? 50) >= 60 ? 'hot' : (overview.env.t ?? 50) <= 30 ? 'cold' : 'mid')">
            大盘情绪 <b>{{ overview.env.state }}</b>
            <span v-if="overview.env.t != null" class="senti-t">温度 {{ overview.env.t }}</span>
          </div>
          <div class="cal-pill" :class="overview.calendar?.state === 'revealing' ? 'cal-hot' : 'cal-ok'"
            :title="overview.calendar?.state === 'revealing' ? '披露窗内:雷还没炸完,小微盘整体承压' : '雷暴出清窗口:小微盘历史超额时段'">
            {{ overview.calendar?.state === 'revealing' ? '⚡ 雷暴进行中' : '✅ 出清窗口' }}
            <span class="cal-sub">{{ calLabel }}</span>
          </div>
        </div>
        <div class="env-row">
          <span v-if="overview.env?.r5 != null" class="env-item" title="国证2000指数近5日涨幅——小票风格锚">国证2000 5日 <b :class="chgClass(overview.env.r5)">{{ pctNum(overview.env.r5 * 100) }}</b></span>
          <span v-if="overview.env?.zt_count != null" class="env-item" title="全市场涨停家数(东财涨停池口径)">全市场涨停 <b>{{ overview.env.zt_count }}</b></span>
          <span class="env-item" title="池内今日红盘占比/平均涨幅/涨停家数">池内红盘 <b :class="chgClass(overview.env?.pool_effect?.red_rate ? 1 : 0)">{{ overview.env?.pool_effect?.red_rate != null ? Math.round(overview.env.pool_effect.red_rate * 100) + '%' : '—' }}</b></span>
          <span class="env-item">池内均涨 <b :class="chgClass(overview.env?.pool_effect?.avg_chg)">{{ pctNum(overview.env?.pool_effect?.avg_chg) }}</b></span>
          <span class="env-item">池内涨停 <b class="zt-hot">{{ overview.env?.pool_effect?.zt_count ?? '—' }}</b></span>
        </div>
        <div v-if="flowMissing" class="flow-warn">⚠ 资金流源未更新——热度分与资金列只含涨停/动量/换手因子,题材面板无净流入</div>
        <div class="stat-chips">
          <button class="chip c-r3" :class="{ on: risk === '3' }" @click="setFilter(() => risk = risk === '3' ? '' : '3')">
            🔴 确定 <b>{{ overview.risk_dist['3'] }}</b>
          </button>
          <button class="chip c-r2" :class="{ on: risk === '2' }" @click="setFilter(() => risk = risk === '2' ? '' : '2')">
            🟠 高危 <b>{{ overview.risk_dist['2'] }}</b>
          </button>
          <button class="chip c-r1" :class="{ on: risk === '1' }" @click="setFilter(() => risk = risk === '1' ? '' : '1')">
            🟡 关注 <b>{{ overview.risk_dist['1'] }}</b>
          </button>
          <button class="chip c-r0" :class="{ on: risk === '0' }" @click="setFilter(() => risk = risk === '0' ? '' : '0')">
            🟢 安全 <b>{{ overview.risk_dist['0'] }}</b>
          </button>
          <button class="chip c-all" :class="{ on: risk === '' }" @click="setFilter(() => risk = '')">
            全部 <b>{{ overview.pools.micro + overview.pools.small }}</b>
          </button>
        </div>
      </div>

      <!-- ── 热门概念面板 ── -->
      <div class="card concept-card">
        <div class="sec-title">热门概念题材
          <span class="sec-sub">东财概念板块按今日主力净流入排序 · 点板名筛池内成分 · 池内 N 只=已建映射的成分数</span>
        </div>
        <div v-if="overview.heat?.boards?.length" class="concept-row">
          <button v-for="b in overview.heat.boards" :key="b.name" class="concept-chip"
            :class="{ on: concept === b.name }" :title="`今日净流入 ${yi(b.inflow)} · 涨幅 ${pctNum(b.chg_pct)}${b.top_stock ? ' · 领涨 ' + b.top_stock : ''}`"
            @click="setConcept(b.name)">
            <b>{{ b.name }}</b>
            <span class="cc-inflow" :class="chgClass(b.inflow)">{{ yi(b.inflow) }}</span>
            <span class="cc-chg" :class="chgClass(b.chg_pct)">{{ pctNum(b.chg_pct) }}</span>
            <span class="cc-count" :class="{ unknown: !b.members_known }">{{ b.members_known ? `池内${b.pool_count}只` : '成分?' }}</span>
          </button>
        </div>
        <div v-else class="flow-warn">概念板块排行未更新(采集侧 heat job 未跑或挂了)</div>
      </div>

      <!-- ── 清单 ── -->
      <div class="card list-card">
        <div class="toolbar">
          <div class="tabs">
            <button v-for="t in POOL_TABS" :key="t.key" class="tab" :class="{ active: pool === t.key }"
              @click="setFilter(() => pool = t.key)">{{ t.label }}</button>
          </div>
          <div class="tabs">
            <button v-for="t in BAND_TABS" :key="t.key" class="tab" :class="{ active: band === t.key }"
              @click="setFilter(() => band = t.key)">{{ t.label }}</button>
          </div>
          <select class="sel" :value="sort" @change="setFilter(() => sort = $event.target.value)">
            <option v-for="o in SORTS" :key="o.key" :value="o.key">按{{ o.label }}</option>
          </select>
          <select class="sel" :value="industry" @change="setFilter(() => industry = $event.target.value)">
            <option value="">全部行业</option>
            <option v-for="i in industries" :key="i" :value="i">{{ i }}</option>
          </select>
          <input v-model="keyword" placeholder="代码/名称" @input="onKeyword" />
        </div>
        <div v-if="concept" class="concept-active">
          已筛概念 <b>{{ concept }}</b><i class="ex-x" @click="setConcept(concept)">×</i>
        </div>

        <!-- 桌面表格 -->
        <table v-if="!isMobile" class="grid mc-table">
          <thead>
            <tr>
              <th class="l stick">标的</th>
              <th title="排雷记分卡:确定/高危/关注/安全,悬浮看旗子与年报财务">排雷</th>
              <th :class="{ on: sort === 'heat' }" @click="setSortToggle('heat')">热度{{ sort === 'heat' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th class="l" title="今日涨停生态:连板数/封单资金;近5日涨停次数;炸板警示(实测次日-1.6%)">涨停生态</th>
              <th :class="{ on: sort === 'inflow_t' }" title="今日主力净流入(额/占流通市值)" @click="setSortToggle('inflow_t')">主力净流入{{ sort === 'inflow_t' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'turn_rate' }" title="当日换手率(自身近120日分位)" @click="setSortToggle('turn_rate')">换手{{ sort === 'turn_rate' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'r5' }" title="5日/20日区间涨幅" @click="setSortToggle('r5')">5日{{ sort === 'r5' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'r20' }" @click="setSortToggle('r20')">20日{{ sort === 'r20' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th>现价</th>
              <th :class="{ on: sort === 'market_cap' }" @click="setSortToggle('market_cap')">流通市值{{ sort === 'market_cap' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th :class="{ on: sort === 'rev_line_ratio' }" title="最新年报营收 ÷ 该板退市营收线(主板3亿/双创1亿);<1 触线,<2 边缘" @click="setSortToggle('rev_line_ratio')">营收/线{{ sort === 'rev_line_ratio' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
              <th title="库内推荐分R(价值0.6+成长0.4) · 现价较最优买点折价 · 股息率">R/边际</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in board.items" :key="s.sid" @click="router.push(`/stock/${s.code}`)">
              <td class="l stick">
                <div class="nm-row">
                  <b>{{ s.name }}</b>
                  <span class="cd">{{ s.code }}</span>
                  <span v-if="heatOnMine(s)" class="mine-hot" title="雷区+高热度:摘帽/重组预期的炒作重地,波动极大,务必看清旗子">雷上加热🔥</span>
                </div>
                <div v-if="s.hot_concepts.length" class="concept-tags">
                  <span v-for="c in s.hot_concepts.slice(0, 2)" :key="c" class="ctag">{{ c }}</span>
                  <span v-if="s.hot_concepts.length > 2" class="ctag more" :title="s.hot_concepts.join(' · ')">+{{ s.hot_concepts.length - 2 }}</span>
                </div>
              </td>
              <td><span class="risk-badge" :class="RISK_META[s.risk_level].cls" :title="riskTitle(s)">{{ RISK_META[s.risk_level].label }}</span></td>
              <td><b :class="s.heat >= 70 ? 'zt-hot' : ''">{{ s.heat?.toFixed(0) ?? '—' }}</b></td>
              <td class="l">
                <span v-if="ztTxt(s)" :class="ztClass(s)">{{ ztTxt(s) }}</span>
                <span v-if="s.seal_amt" class="seal" title="封板资金">封{{ sealTxt(s) }}</span>
                <span v-if="!ztTxt(s) && !s.seal_amt" class="flat">—</span>
              </td>
              <td :title="s.inflow_strength != null ? `占流通市值 ${(s.inflow_strength * 100).toFixed(1)}%` : ''">
                <span v-if="s.inflow_t != null" :class="chgClass(s.inflow_t)">{{ yi(s.inflow_t) }}</span>
                <span v-else class="flat">—</span>
              </td>
              <td :title="s.turn_pctile != null ? `自身分位 ${Math.round(s.turn_pctile * 100)}%` : ''">
                {{ s.turn_rate != null ? s.turn_rate.toFixed(1) + '%' : '—' }}
              </td>
              <td :class="chgClass(s.r5)">{{ pctS(s.r5) }}</td>
              <td :class="chgClass(s.r20)">{{ pctS(s.r20) }}</td>
              <td>{{ s.price?.toFixed(2) ?? '—' }} <span class="cd" :class="chgClass(s.chg_pct)">{{ pctNum(s.chg_pct) }}</span></td>
              <td>{{ yi(s.float_mcap) }}</td>
              <td :class="lineClass(s.rev_line_ratio)" :title="s.annual_rev != null ? `年报营收 ${yi(s.annual_rev)} · 线 ${yi(s.rev_line)} · 年报净利 ${yi(s.annual_np)}` : ''">
                {{ s.rev_line_ratio?.toFixed(2) ?? '—' }}
              </td>
              <td :title="`价值 ${s.value?.toFixed(0) ?? '—'} · 成长 ${s.growth?.toFixed(0) ?? '—'} · 股息 ${s.div_yield != null ? (s.div_yield * 100).toFixed(1) + '%' : '—'}`">
                {{ s.recommend?.toFixed(0) ?? '—' }} <span :class="s.discount != null && s.discount < 0 ? 'marg-pos' : 'flat'">{{ s.discount != null ? pctS(s.discount) : '—' }}</span>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- 移动端卡片 -->
        <div v-else class="m-list">
          <div v-for="s in board.items" :key="s.sid" class="m-item" @click="router.push(`/stock/${s.code}`)">
            <div class="m-row1">
              <b>{{ s.name }}</b><span class="cd">{{ s.code }}</span>
              <span class="risk-badge" :class="RISK_META[s.risk_level].cls">{{ RISK_META[s.risk_level].label }}</span>
              <span class="m-pick">{{ s.heat?.toFixed(0) ?? '—' }}热</span>
            </div>
            <div class="m-row2">
              <span v-if="ztTxt(s)" :class="ztClass(s)">{{ ztTxt(s) }}</span>
              <span v-if="s.inflow_t != null" :class="chgClass(s.inflow_t)">净{{ yi(s.inflow_t) }}</span>
              <span :class="chgClass(s.r5)">5日{{ pctS(s.r5) }}</span>
              <span>{{ yi(s.float_mcap) }}</span>
            </div>
            <div v-if="s.risk_labels.length || s.hot_concepts.length" class="m-row3">
              <span v-for="r in s.risk_labels.slice(0, 2)" :key="r" class="risk-flag">{{ r }}</span>
              <span v-for="c in s.hot_concepts.slice(0, 1)" :key="c" class="ctag">{{ c }}</span>
            </div>
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
          <b>池子</b>:总市值切档,微盘&lt;25 亿、小票 25~60 亿;排北交所、剔上市不满 1 年次新。
          <b>热度分</b>(0~100)只是排序器:主力净流入强度(今日/流通市值 0~30、5日 0~15)、
          换手自身分位 0~15、连板数 0~20、5日/20日动量各 0~10、热门概念 +10;
          缺数因子按可用权重归一,资金流源未更新时分数只含行情因子(不冒 0)。
        </p>
        <p>
          <b>排雷记分卡</b>(亮灯不拦截,亏损/戴帽的票常正是投机标的,但要看清拿的是什么刀):
          🔴 确定 = 简称已 ST/*ST、年报亏损且营收低于退市线(主板3亿/双创1亿,披露后必然戴帽)、年报资不抵债;
          🟠 高危 = 审计非标、证监会立案、亏损且营收不足线2倍、20日均价&lt;1.5元、市值逼近退市线(主板5亿/双创3亿+20%)、最新期净资产转负;
          🟡 关注 = 负债率&gt;90%、年报亏损、连续两年扣非亏、最新期转亏;
          双创上市不满 4 个完整会计年度的未盈利豁免窗触线降为关注。
          雷区旗子+高热度同时命中会标「雷上加热」。
        </p>
        <p class="note-fine">
          涨停生态来自东财涨停池(连板数/封板资金为交易所口径;炸板警示含黑名单,实测次日溢价 -1.6%);
          概念映射只覆盖热榜前 15 板(成分 7 天复用,新进榜当日补);换手分位窗=近 120 交易日(表内现约 43 日,随数据加深自动受益);
          「营收/线」用营业总收入代替扣除后营收(口径偏宽松=宁漏勿错);市值退市线用单日快照+20% 提前量近似连续 20 日口径;
          每交易日 16:45 heat 任务更新资金流/概念(sentiment 16:30 榜单池)。
          本页是投机环境合成,不构成投资建议。
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.mc-page { display: flex; flex-direction: column; gap: 14px; }
.head-card { display: flex; flex-direction: column; gap: 10px; }
.head-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.head-title { margin: 0; font-size: 18px; }
.head-sub { color: var(--muted, #8a94a6); font-size: 12.5px; margin-top: 3px; }
.senti { padding: 6px 12px; border-radius: 999px; font-size: 13px; border: 1px solid transparent; }
.senti.st-hot { background: #fdeeee; color: #c0392b; border-color: #f3c3c3; }
.senti.st-cold { background: #eef3fd; color: #2d6cdf; border-color: #c3d2f3; }
.senti.st-mid { background: #f4f5f7; color: #5a6472; border-color: #dfe3e9; }
.senti-t { margin-left: 6px; opacity: 0.8; }
.cal-pill { padding: 6px 12px; border-radius: 999px; font-size: 13px; border: 1px solid transparent; }
.cal-pill.cal-ok { background: #edf7ee; color: #1e7e34; border-color: #c3e6cb; }
.cal-pill.cal-hot { background: #fff7e6; color: #b8860b; border-color: #f0dcb0; }
.cal-sub { margin-left: 6px; opacity: 0.85; font-size: 12px; }
.env-row { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12.5px; color: var(--muted, #8a94a6); }
.env-item b { margin-left: 4px; font-size: 13px; }
.up { color: #c0392b; } .down { color: #1e7e34; } .flat { color: #8a94a6; }
.warn-amber { color: #b8860b; }
.zt-hot { color: #c0392b; font-weight: 600; }
.marg-pos { color: #c0392b; font-weight: 600; }
.flow-warn { background: #fff7e6; color: #8a6d1a; border: 1px solid #f0dcb0; border-radius: 8px; padding: 6px 10px; font-size: 12.5px; }
.stat-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { border: 1px solid #dfe3e9; background: #fff; border-radius: 999px; padding: 5px 12px; font-size: 13px; cursor: pointer; color: #5a6472; }
.chip.on { font-weight: 600; }
.chip.c-r3.on { background: #fdeeee; border-color: #e4a3a3; color: #a93226; }
.chip.c-r2.on { background: #fdf3e7; border-color: #ecc99b; color: #b05e10; }
.chip.c-r1.on { background: #fdfae7; border-color: #e6d78f; color: #8a7a10; }
.chip.c-r0.on { background: #edf7ee; border-color: #a8d5ad; color: #1e7e34; }
.chip.c-all.on { background: #eef3fd; border-color: #a3bde8; color: #2d6cdf; }
.concept-card { display: flex; flex-direction: column; gap: 8px; }
.sec-title { font-size: 14px; font-weight: 600; }
.sec-sub { font-weight: 400; color: var(--muted, #8a94a6); font-size: 12px; margin-left: 8px; }
.concept-row { display: flex; gap: 8px; flex-wrap: wrap; }
.concept-chip { display: inline-flex; align-items: center; gap: 8px; border: 1px solid #dfe3e9; background: #fff; border-radius: 10px; padding: 6px 10px; font-size: 12.5px; cursor: pointer; color: #5a6472; }
.concept-chip.on { border-color: #2d6cdf; background: #eef3fd; color: #2d6cdf; font-weight: 600; }
.cc-inflow { font-weight: 600; }
.cc-chg, .cc-count { color: #8a94a6; font-size: 11.5px; }
.cc-count.unknown { color: #b8860b; }
.concept-active { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: #2d6cdf; background: #eef3fd; border-radius: 8px; padding: 5px 10px; width: fit-content; }
.ex-x { cursor: pointer; font-style: normal; padding: 0 4px; font-size: 14px; }
.concept-tags { margin-top: 2px; display: flex; gap: 4px; }
.ctag { font-size: 10.5px; color: #2d6cdf; background: #eef3fd; border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.ctag.more { color: #8a94a6; background: #f4f5f7; }
.mine-hot { font-size: 10.5px; color: #fff; background: #c0392b; border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.mc-table { font-size: 12.5px; }
.mc-table th { white-space: nowrap; font-weight: 600; cursor: pointer; }
.mc-table th.on { color: #2d6cdf; }
.mc-table td { white-space: nowrap; }
.nm-row { display: flex; align-items: center; gap: 6px; }
.cd { color: #8a94a6; font-size: 11.5px; }
.seal { margin-left: 4px; font-size: 11px; color: #8a94a6; }
.risk-badge { display: inline-block; font-size: 11px; border-radius: 4px; padding: 2px 7px; white-space: nowrap; }
.rb-3 { background: #fdeeee; color: #a93226; font-weight: 600; }
.rb-2 { background: #fdf3e7; color: #b05e10; font-weight: 600; }
.rb-1 { background: #fdfae7; color: #8a7a10; }
.rb-0 { background: #f4f5f7; color: #5a6472; }
.risk-flag { display: inline-block; font-size: 11px; color: #b05e10; background: #fdf3e7; border-radius: 4px; padding: 1px 6px; margin-right: 4px; }
.m-list { display: flex; flex-direction: column; gap: 8px; }
.m-item { border: 1px solid #eef0f3; border-radius: 10px; padding: 8px 10px; }
.m-row1 { display: flex; align-items: center; gap: 8px; }
.m-pick { margin-left: auto; font-weight: 600; color: #5a6472; }
.m-row2 { display: flex; gap: 12px; font-size: 12px; margin-top: 4px; color: #5a6472; }
.m-row3 { margin-top: 4px; display: flex; gap: 4px; flex-wrap: wrap; }
</style>
