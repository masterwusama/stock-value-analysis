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
const plan = ref(null)
const board = ref({ total: 0, items: [] })
const industries = ref([])
const loading = ref(true)
const boardLoading = ref(false)
const error = ref('')

// —— 筛选状态(hash query 同步) ——
const pool = ref('')          // ''=两池 / micro / small
const band = ref('')          // ''=全部 / hot / warm / cool
const risk = ref('')          // ''=全部 / 3 / 2 / 1 / 0
const preset = ref('')        // ''=无 / zt 今日涨停 / broke 断板观察 / turn 扭亏摘帽候 / trade 可上手
const specBand = ref('')      // ''=全部 / trade / watch / rest 投机分档
const concept = ref('')
const industry = ref('')
const keyword = ref('')
const sort = ref('spec')
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
  { key: 'spec', label: '投机分' },
  { key: 'danger', label: '危险度' },
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

const ARCH_META = { board: '连板型', low: '低位启动', base: '基础' }
const VERDICT = {
  ok: ['达标', 'up'], hold: ['持平', 'flat'], weak: ['走弱', 'warn'],
  half: ['破5日线', 'warn'], stop: ['止损', 'down'], promo: ['晋级', 'up'],
  dt: ['跌停', 'down'], na: ['出池', 'flat'],
}
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
      zt: preset.value === 'zt' || undefined,
      broke: preset.value === 'broke' || undefined,
      turn: preset.value === 'turn' || undefined,
      tradeable: preset.value === 'trade' || undefined,
      spec_band: specBand.value || undefined,
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
async function loadPlan() {
  plan.value = await get('/microcap/plan')
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
function setPreset(p) {
  setFilter(() => { preset.value = preset.value === p ? '' : p })
}
function syncHash() {
  const q = new URLSearchParams()
  for (const [k, v] of [['pool', pool.value], ['band', band.value], ['risk', risk.value],
    ['preset', preset.value], ['sb', specBand.value], ['concept', concept.value],
    ['industry', industry.value], ['kw', keyword.value], ['sort', sort.value],
    ['order', order.value], ['page', page.value > 1 ? page.value : '']]) {
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
// 营收/线着色是「危险度」语义不是涨跌方向:<1 触线=红(危险),1~2 边缘=琥珀
const lineClass = (r) => r == null ? '' : r < 1 ? 'line-hit' : r < 2 ? 'warn-amber' : ''
const turnPctileTxt = (it) => it.turn_pctile == null ? '—' : `${Math.round(it.turn_pctile * 100)}%`
const ztTxt = (it) => {
  if (it.streak) return (it.zt_yesterday ? '晋级↑' : '') + `${it.streak}板`
  if (it.zt_yesterday) return '断板↓'
  if (it.dt_today) return '跌停'
  if (it.zha_flag) return '炸板⚠'
  if (it.zt_5d > 0) return `近5日${it.zt_5d}次`
  return ''
}
const ztClass = (it) => it.streak ? 'zt-hot'
  : it.zt_yesterday ? 'warn-amber'
  : it.dt_today ? 'down' : it.zha_flag ? 'warn-amber' : 'flat'
const sealTxt = (it) => it.seal_amt == null ? '' : yi(it.seal_amt)
const flowMissing = computed(() => overview.value && !overview.value.heat?.flow_ok)
// 分布校准偏离:实际红盘率 - 分布预期(百分数),>0=强于分布(赚钱效应超统计)
const calibDev = computed(() => {
  const c = overview.value?.battle?.calibration
  if (!c || c.expect_red == null || c.actual_red == null) return 0
  return (c.actual_red - c.expect_red) * 100
})
const calibText = computed(() => {
  const d = calibDev.value
  if (!overview.value?.battle?.calibration?.actual_red) return ''
  if (d >= 5) return '——强于分布(赚钱效应超统计)'
  if (d <= -5) return '——弱于分布(亏钱效应超统计)'
  return '——符合分布'
})
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
  if (q.get('preset')) preset.value = q.get('preset')
  if (q.get('sb')) specBand.value = q.get('sb')
  if (q.get('concept')) concept.value = q.get('concept')
  if (q.get('industry')) industry.value = q.get('industry')
  if (q.get('kw')) keyword.value = q.get('kw')
  if (q.get('sort')) sort.value = q.get('sort')
  if (q.get('order')) order.value = q.get('order')
  if (q.get('page')) page.value = Math.max(1, Number(q.get('page')) || 1)

  try {
    await Promise.all([loadOverview(), loadPlan(), loadIndustries(), loadBoard()])
    loading.value = false
  } catch (e) {
    error.value = e.message
    loading.value = false
  }
})
// 初值必须当场求值:只挂 change 监听的话,移动端直接加载(从不缩放)会永远渲染桌面表格
const isMobile = ref(window.matchMedia(MOBILE_QUERY).matches)
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
          <span class="spec-sep"></span>
          <button class="chip c-spec1" :class="{ on: specBand === 'trade' }" title="投机推荐分≥65(热度×(100−危险度)/100)" @click="setFilter(() => specBand = specBand === 'trade' ? '' : 'trade')">
            🎯 可上手 <b>{{ overview.spec?.trade ?? '—' }}</b>
          </button>
          <button class="chip c-spec2" :class="{ on: specBand === 'watch' }" title="投机推荐分 45~65" @click="setFilter(() => specBand = specBand === 'watch' ? '' : 'watch')">
            观察 <b>{{ overview.spec?.watch ?? '—' }}</b>
          </button>
          <span class="spec-sep"></span>
          <span class="chip c-dgr" :title="`池内危险度中位数(≈随机买入一只短期大幅回撤的概率);分值口径见底部说明`">
            池内危险度中位 <b>{{ overview.spec?.danger_median ?? '—' }}</b>
          </span>
        </div>
      </div>

      <!-- ── 投机作战面板 ── -->
      <div class="card battle-card">
        <div class="battle-grid">
          <div class="bt-block">
            <div class="bt-title">今日打法 <span class="bt-sub">按情绪状态的历史校准</span></div>
            <div class="bt-row">
              状态 <b :class="(overview.env?.t ?? 50) >= 60 ? 'zt-hot' : 'flat'">{{ overview.env?.state ?? '—' }}</b>
              <span v-if="overview.env?.t != null"> · 温度 {{ overview.env.t }}</span>
            </div>
            <div class="bt-hint" v-if="overview.battle?.premium_hint">
              该状态下涨停股次日竞价 <b>{{ overview.battle.premium_hint }}</b>
            </div>
            <div class="bt-trap" v-if="overview.battle?.trap">
              ⚠ 陷阱日：昨日极弱今日普通——该形态次日仅 +0.55%/胜率50%，全局禁隔夜
            </div>
            <div class="bt-hint" v-if="overview.battle?.calibration?.expect_red != null">
              分布校准：池内中位 {{ pctNum(overview.battle.calibration.median_chg) }} → 预期红盘
              ~{{ Math.round(overview.battle.calibration.expect_red * 100) }}%，实际
              <b :class="calibDev >= 0 ? 'up' : 'down'">{{ overview.battle.calibration.actual_red != null ? Math.round(overview.battle.calibration.actual_red * 100) + '%' : '—' }}</b>
              {{ calibText }}
            </div>
          </div>
          <div class="bt-block">
            <div class="bt-title">月历 <span class="bt-sub">2 5 11玩大票，其余玩小票；做反=极强全错过长征全赶上</span></div>
            <div class="bt-row">
              {{ overview.battle?.regime?.month }} 月
              <b class="regime-tag" :class="overview.battle?.regime?.small_ok === true ? 'rg-ok' : overview.battle?.regime?.small_ok === false ? 'rg-bad' : 'rg-mid'">{{ overview.battle?.regime?.label ?? '—' }}</b>
            </div>
            <div class="bt-hint">{{ overview.battle?.regime?.advice }}</div>
          </div>
          <div class="bt-block">
            <div class="bt-title">池内昨日涨停 → 今日 <span class="bt-sub">打板赚钱效应</span></div>
            <div class="bt-row">
              <b>{{ overview.battle?.zt_yd?.n ?? '—' }}</b> 只
              · 均涨 <b :class="chgClass(overview.battle?.zt_yd?.avg_chg)">{{ pctNum(overview.battle?.zt_yd?.avg_chg) }}</b>
              · 红盘 <b>{{ overview.battle?.zt_yd?.red_rate != null ? Math.round(overview.battle.zt_yd.red_rate * 100) + '%' : '—' }}</b>
              · 晋级 <b class="zt-hot">{{ overview.battle?.zt_yd?.promo ?? '—' }}</b>
            </div>
            <div class="bt-hint">红盘率 ≥60% 赚钱效应在可打板；≤40% 退潮期只低吸不追高</div>
          </div>
          <div class="bt-block">
            <div class="bt-title">连板梯队 <span class="bt-sub">全市场 {{ overview.battle?.ladder?.total ?? 0 }} 家涨停 · 近{{ overview.battle?.ladder?.trail?.length ?? 0 }}日高度轨迹</span></div>
            <div class="ladder-row">
              <span v-for="(cnt, lv) in overview.battle?.ladder?.dist || {}" :key="lv" class="lv-chip"
                :class="{ top: +lv === overview.battle?.ladder?.max }">{{ lv }}板×{{ cnt }}</span>
              <span v-if="!overview.battle?.ladder?.total" class="bt-hint">今日榜单未出</span>
            </div>
            <div v-if="overview.battle?.ladder?.trail?.length" class="trail-row" title="近15个榜单日的全市场最高连板数——空间龙高度图(80课):周期龙目标=超越前周期高度">
              <span v-for="(h, i) in overview.battle.ladder.trail" :key="i" class="trail-bar">
                <i class="tb-col" :class="{ top: h === overview.battle.ladder.max }" :style="{ height: (h * 7 + 3) + 'px' }"></i>
                <em>{{ h }}</em>
              </span>
            </div>
            <div class="bt-hint">
              <template v-if="overview.battle?.ladder?.cycle_note">{{ overview.battle.ladder.cycle_note }}。 </template>
              <template v-else>最高 {{ overview.battle?.ladder?.max ?? '—' }} 板——空间板的身位参照，高位断板=题材退潮信号。 </template>
              <template v-if="overview.battle?.dt_side?.n">
                · 跌停 {{ overview.battle.dt_side.n }}（一字 {{ overview.battle.dt_side.oneword }}）
              </template>
            </div>
            <div class="bt-trap" v-if="overview.battle?.dt_side?.level === 2">
              ⚠ 极弱判定：一字跌停 ≥5 家（答疑口径：早盘 5 个以上开盘一字跌停=极弱）——负螺旋黑洞，防守等一字梯队打开
            </div>
            <div class="bt-trap warn2" v-else-if="overview.battle?.dt_side?.level === 1">
              ⚠ 负螺旋预警：一字跌停 ≥3 家——一字梯队开始带全场补跌，梯队打开=正螺旋反弹起点
            </div>
          </div>
        </div>
      </div>

      <!-- ── 收盘复盘 → 明日预案 ── -->
      <div class="card plan-card">
        <div class="plan-head">
          <div>
            <div class="sec-title">收盘复盘 → 明日预案
              <span class="sec-sub">昨日可上手逐只复盘 · 今日可上手逐只应对卡(加仓/低吸/持有/减半/清仓)</span>
            </div>
            <div class="plan-hint">{{ plan?.header?.position_hint }}{{ plan?.header?.trap ? ' · ⚠陷阱日禁隔夜' : '' }}</div>
          </div>
        </div>

        <!-- 复盘条 -->
        <div class="review-strip" v-if="plan?.review?.available">
          <b>{{ plan.review.date }}</b> 可上手 {{ plan.review.stats.n }} 只 →
          今均涨 <b :class="chgClass(plan.review.stats.avg_chg)">{{ pctNum(plan.review.stats.avg_chg) }}</b>
          · 红盘 <b>{{ Math.round((plan.review.stats.red_rate ?? 0) * 100) }}%</b>
          · 破5日线 <b class="warn-amber">{{ plan.review.stats.broken5 }}</b>
          · 止损触发 <b class="down">{{ plan.review.stats.stopped }}</b>
          <details class="review-detail">
            <summary>逐只</summary>
            <div v-for="r in plan.review.items" :key="r.code" class="review-row">
              <span class="cd">{{ r.code }}</span><b>{{ r.name }}</b>
              <span :class="chgClass(r.chg)">{{ r.chg != null ? pctNum(r.chg) : '—' }}</span>
              <span class="rv-verdict" :class="'v-' + (VERDICT[r.verdict]?.[1] || 'flat')">{{ VERDICT[r.verdict]?.[0] || r.verdict }}</span>
              <span class="rv-note">{{ r.note }}</span>
            </div>
          </details>
        </div>
        <div class="review-strip" v-else>
          <span class="flat">{{ plan?.review?.note || '复盘数据加载中…' }}</span>
        </div>

        <!-- 预案卡网格 -->
        <div class="plan-grid" v-if="plan?.plans?.length">
          <div v-for="p in plan.plans" :key="p.code" class="plan-item" @click="router.push(`/stock/${p.code}`)">
            <div class="pi-head">
              <b>{{ p.name }}</b><span class="cd">{{ p.code }}</span>
              <span class="pi-spec">投{{ p.spec?.toFixed(0) }} 险{{ p.danger }}</span>
              <span class="arch-tag" :class="'ar-' + p.archetype">{{ ARCH_META[p.archetype] }}</span>
              <span v-if="p.streak" class="zt-hot">{{ p.streak }}板</span>
            </div>
            <div class="pi-levels" :title="'价位锚点:5日/10日线为动态生命线,前低=恐慌位,前高(不含今日)=压力,涨停价按板块幅度'">
              <span>5日 <b>{{ p.levels.ma5?.toFixed(2) ?? '—' }}</b></span>
              <span>10日 <b>{{ p.levels.ma10?.toFixed(2) ?? '—' }}</b></span>
              <span>前低 <b class="down">{{ p.levels.lo20?.toFixed(2) ?? '—' }}</b></span>
              <span>前高 <b class="warn-amber">{{ p.levels.hi20_ex?.toFixed(2) ?? '—' }}</b></span>
              <span>涨停 <b class="zt-hot">{{ p.levels.limit_up?.toFixed(2) ?? '—' }}</b></span>
              <span v-if="p.levels.turn_ma5 != null" :title="`量能锚:今日换手 ${p.levels.turn_today?.toFixed(1) ?? '—'}% vs 5日均 ${p.levels.turn_ma5}%——加仓指引的缩量/放量线由此而来`">
                量能 今<b>{{ p.levels.turn_today?.toFixed(1) ?? '—' }}</b>/均<b>{{ p.levels.turn_ma5 }}</b>
                <i class="q-line">缩&lt;{{ p.levels.shrink }} 放&gt;{{ p.levels.surge }}</i>
              </span>
            </div>
            <div class="pi-sizing" :title="'加仓纪律:分批建仓,每笔独立止损——加仓部分错了只出加仓部分,不动底仓'">{{ p.sizing }}</div>
            <div class="pi-moves">
              <div v-for="m in p.moves" :key="m.t" class="mv-row">
                <span class="mv-t">{{ m.t }}</span>
                <span class="mv-a" :class="'act-' + m.k">{{ m.x }}</span>
              </div>
            </div>
            <div class="pi-risk">{{ p.risk_note }}</div>
          </div>
        </div>
        <div v-else class="review-strip"><span class="flat">今日无「可上手」标的——空仓也是仓位,等信号。</span></div>

        <details class="cheat-box">
          <summary>短线场景速查表(通用)</summary>
          <table class="grid cheat-table">
            <thead><tr><th class="l">场景</th><th class="l">应对</th><th>出处</th></tr></thead>
            <tbody>
              <tr v-for="c in plan?.cheatsheet || []" :key="c.t">
                <td class="l">{{ c.t }}</td><td class="l">{{ c.x }}</td><td class="cd">{{ c.src }}</td>
              </tr>
            </tbody>
          </table>
        </details>
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
        <div class="preset-row">
          <span class="preset-label">快捷</span>
          <button class="chip" :class="{ on: preset === 'zt' }" title="今日涨停池内的票(含晋级),打板/接力视角" @click="setPreset('zt')">今日涨停</button>
          <button class="chip" :class="{ on: preset === 'broke' }" title="昨日涨停今日未再板——94课口径:2板封不死,次日正股多低开~8%;强承接低吸 or 出货规避,看当日涨幅分辨" @click="setPreset('broke')">断板观察</button>
          <button class="chip" :class="{ on: preset === 'turn' }" title="年报亏损或已ST,最新披露期净利润转正——ST即摘帽博弈候选,非ST即困境反转候选(业绩面信号,非交易所认定)" @click="setPreset('turn')">扭亏/摘帽候</button>
          <button class="chip" :class="{ on: preset === 'trade' }" title="投机推荐分≥65:热度×(100−危险度)/100,热度够且短期回撤折损小——进攻首选档" @click="setPreset('trade')">可上手</button>
          <button v-if="preset" class="chip" @click="setPreset(preset)">清除</button>
        </div>
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
              <th :class="{ on: sort === 'spec' }" title="投机推荐分=热度×(100−危险度)/100;≥65可上手,45~65观察,<45看戏。危险度≈随机买入短期大幅回撤的概率" @click="setSortToggle('spec')">投机{{ sort === 'spec' ? (order === 'desc' ? '↓' : '↑') : '' }}</th>
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
                  <span v-if="s.turn_profit" class="turn-badge" title="年报亏损或已ST,最新披露期净利润已转正——ST 即摘帽博弈候选,非 ST 即困境反转候选(业绩面信号,非交易所认定)">扭亏🔥</span>
                </div>
                <div v-if="s.hot_concepts.length" class="concept-tags">
                  <span v-for="c in s.hot_concepts.slice(0, 2)" :key="c" class="ctag"
                    :class="{ lead: s.concept_lead?.includes(c) }"
                    :title="s.concept_lead?.includes(c) ? c + ' 的池内身位最高(连板/热度)' : ''">{{ (s.concept_lead?.includes(c) ? '👑' : '') + c }}</span>
                  <span v-if="s.hot_concepts.length > 2" class="ctag more" :title="s.hot_concepts.join(' · ')">+{{ s.hot_concepts.length - 2 }}</span>
                </div>
              </td>
              <td><span class="risk-badge" :class="RISK_META[s.risk_level].cls" :title="riskTitle(s)">{{ RISK_META[s.risk_level].label }}</span></td>
              <td>
                <div class="spec-cell">
                  <b :class="s.spec >= 65 ? 'spec-hi' : s.spec == null ? 'flat' : ''">{{ s.spec?.toFixed(0) ?? '—' }}</b>
                  <em class="dgr" :class="s.danger >= 45 ? 'dgr-hi' : s.danger >= 25 ? 'dgr-mid' : 'dgr-lo'"
                    :title="(s.danger_labels || []).join(' · ') || '无危险旗'">险{{ s.danger }}</em>
                </div>
              </td>
              <td>
                <div class="heat-cell">
                  <b :class="s.heat >= 70 ? 'zt-hot' : ''">{{ s.heat?.toFixed(0) ?? '—' }}</b>
                  <i v-if="s.heat != null" class="hbar"><u :style="{ width: s.heat + '%' }"></u></i>
                </div>
              </td>
              <td class="l">
                <span v-if="ztTxt(s)" :class="ztClass(s)">{{ ztTxt(s) }}</span>
                <span v-if="s.seal_amt" class="seal"
                  :title="`封板资金 ${sealTxt(s)} · 占流通市值 ${s.seal_strength != null ? (s.seal_strength * 100).toFixed(1) + '%' : '—'}（封单大≠强,看占盘子比例）`">
                  封{{ sealTxt(s) }}<template v-if="s.seal_strength != null">/{{ (s.seal_strength * 100).toFixed(1) }}%</template>
                </span>
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
              <span v-if="s.turn_profit" class="turn-badge">扭亏🔥</span>
              <span class="risk-badge" :class="RISK_META[s.risk_level].cls">{{ RISK_META[s.risk_level].label }}</span>
              <span class="m-pick">{{ s.spec?.toFixed(0) ?? '—' }}投 <i class="m-dgr">险{{ s.danger }}</i></span>
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
          <b>作战面板四问四答</b>:①今天什么打法——按大盘情绪状态的历史校准(极强日涨停股次日竞价 +3.6%/胜率73%,
          炸板股 -1.6% 应回避,极弱转普通的「陷阱日」禁隔夜);②今天在年历的哪一段——12个月大环境各不相同,
          极弱月(1/4/10)防守、极强月(2/5/11)小票主升、长征月(6-9)只主线核心、12月只能博弈;
          「2 5 11 玩大票(容量),其余玩小票,做反=极强全错过长征全赶上」;分布校准=池内中位涨幅按正态分布
          应有的红盘率 vs 实际(±5pp 内符合分布);③今天打板赚不赚钱——池内昨日涨停股今日均涨/红盘率/晋级数,
          红盘率 ≥60% 赚钱效应在、≤40% 退潮只低吸;④身位在哪——全市场连板梯队,最高板是空间参照,
          高位断板=题材退潮信号;跌停侧看一字梯队(≥3 家=负螺旋:大A 一字带 5000B 补跌,梯队打开=反弹起点)。
          <b>快捷预设</b>:今日涨停(打板/接力)、断板观察(强承接低吸 or 出货规避,看当日涨幅分辨)、
          扭亏/摘帽候(年报亏损或已 ST、最新披露期转正——摘帽博弈/困境反转的正主题,业绩面信号非交易所认定)。
        </p>
        <p>
          <b>池子</b>:总市值切档,微盘&lt;25 亿、小票 25~60 亿;排北交所、剔上市不满 1 年次新。
          <b>投机推荐分 = 热度 ×(100−危险度)/100</b>(≥65 可上手🎯 / 45~65 观察 / &lt;45 看戏):
          热度是进攻(想吃到的肉),危险度是折损(吃肉路上被埋的概率)——「又冷又安全」不得高分,
          「又热又危险」被重折。危险度(0~95,≈随机买入一只短期大幅回撤的概率)=今日跌停20/
          昨日炸板15(实测次日-1.6%)/断板10(次日多低开~8%)/高位连板≥5板25/追高(20日+50%→18)/
          排雷级3·2·1→30·15·6/流通&lt;10亿闪崩10/换手分位98%+6/热度拥挤5,全部命中累加封顶;
          每股的危险旗悬浮在「险」字上。<b>热度分</b>(0~100)只是排序器:主力净流入强度(今日/流通市值 0~30、5日 0~15)、
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
          交易损耗提醒（第1课）：一次满仓往返 ≈ 0.1~0.32%（佣金+印花税+盘口冲击），账号寿命只有约
          300~500 次满仓交易——出手有效率用乘法衰减（20%胜率出手2次仅4%全赢），管住手比找票难。
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
/* 涨跌色走全站令牌(红涨绿跌),不再本页硬编码——与列表页/掘金台同一根色管 */
.up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--sub-deep, #8a94a6); }
.warn-amber { color: #b8860b; }
.line-hit { color: var(--up); font-weight: 600; }   /* 营收触线:红色危险,勿与涨跌混淆 */
.zt-hot { color: #c0392b; font-weight: 600; }
.marg-pos { color: #c0392b; font-weight: 600; }
.flow-warn { background: #fff7e6; color: #8a6d1a; border: 1px solid #f0dcb0; border-radius: 8px; padding: 6px 10px; font-size: 12.5px; }
.stat-chips { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.spec-sep { width: 1px; height: 18px; background: #dfe3e9; margin: 0 2px; }
.chip.c-spec1.on { background: #f4eefd; border-color: #b9a3e8; color: #6c3fc5; font-weight: 600; }
.chip.c-spec2.on { background: #eef7fa; border-color: #a3cbe8; color: #2a6fa8; font-weight: 600; }
.chip.c-dgr { background: #f7f8fa; color: #5a6472; cursor: default; }
.spec-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; }
.spec-hi { color: #6c3fc5; }
.dgr { font-style: normal; font-size: 10.5px; border-radius: 3px; padding: 0 4px; }
.dgr-lo { color: #1e7e34; background: #edf7ee; }
.dgr-mid { color: #b8860b; background: #fff7e6; }
.dgr-hi { color: #a93226; background: #fdeeee; font-weight: 600; }
.m-dgr { font-style: normal; font-size: 11px; color: #8a94a6; }
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
.ctag.lead { background: #fdf6ec; color: #b05e10; font-weight: 600; }
.mine-hot { font-size: 10.5px; color: #fff; background: #c0392b; border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
.turn-badge { font-size: 10.5px; color: #0e7a5f; background: #e6f5f0; border: 1px solid #b8e0d3; border-radius: 4px; padding: 1px 5px; white-space: nowrap; }
/* 作战面板:四块并排(打法/月历/赚钱效应/梯队),窄屏纵叠 */
.battle-card { display: block; }
.battle-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.regime-tag { display: inline-block; margin-left: 8px; border-radius: 4px; padding: 1px 8px; font-size: 12px; }
.regime-tag.rg-ok { background: #edf7ee; color: #1e7e34; }
.regime-tag.rg-bad { background: #fdeeee; color: #a93226; }
.regime-tag.rg-mid { background: #fdf3e7; color: #b05e10; }
.bt-block { min-width: 0; }
.bt-title { font-size: 13.5px; font-weight: 600; margin-bottom: 6px; }
.bt-sub { font-weight: 400; color: #8a94a6; font-size: 11.5px; margin-left: 6px; }
.bt-row { font-size: 13px; color: #5a6472; }
.bt-hint { font-size: 11.5px; color: #8a94a6; margin-top: 5px; }
.bt-trap { font-size: 11.5px; color: #a93226; background: #fdeeee; border-radius: 6px; padding: 4px 8px; margin-top: 5px; }
.ladder-row { display: flex; gap: 5px; flex-wrap: wrap; }
.lv-chip { font-size: 11.5px; color: #5a6472; background: #f4f5f7; border-radius: 4px; padding: 2px 7px; }
.lv-chip.top { color: #c0392b; background: #fdeeee; font-weight: 700; }
/* 高度轨迹迷你图:近15榜各日最高连板数 */
.trail-row { display: flex; align-items: flex-end; gap: 3px; margin-top: 7px; height: 60px; }
.trail-bar { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; width: 16px; }
.trail-bar .tb-col { display: block; width: 10px; background: #dfe6f2; border-radius: 2px 2px 0 0; }
.trail-bar .tb-col.top { background: #c0392b; }
.trail-bar em { font-style: normal; font-size: 9.5px; color: #8a94a6; margin-top: 2px; }
.bt-trap.warn2 { background: #fff7e6; color: #b8860b; }
/* —— 复盘·预案卡 —— */
.plan-card { display: flex; flex-direction: column; gap: 10px; }
.plan-hint { font-size: 12px; color: #8a94a6; margin-top: 3px; }
.review-strip { font-size: 12.5px; color: #5a6472; background: #f7f8fa; border-radius: 8px; padding: 7px 10px; }
.review-strip b { margin: 0 2px; }
.review-detail { margin-top: 4px; }
.review-detail summary { cursor: pointer; color: #2d6cdf; font-size: 12px; }
.review-row { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 12px; border-top: 1px dashed #e8ebf0; }
.rv-verdict { font-weight: 600; }
.rv-verdict.v-up { color: var(--up); } .rv-verdict.v-down { color: var(--down); }
.rv-verdict.v-warn { color: #b8860b; } .rv-verdict.v-flat { color: #8a94a6; }
.rv-note { color: #8a94a6; font-size: 11.5px; }
.plan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 10px; }
.plan-item { border: 1px solid #e8ebf0; border-radius: 10px; padding: 9px 11px; cursor: pointer; background: #fff; }
.plan-item:hover { border-color: #b9a3e8; }
.pi-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pi-spec { font-size: 11px; color: #6c3fc5; background: #f4eefd; border-radius: 4px; padding: 1px 5px; }
.arch-tag { font-size: 10.5px; border-radius: 4px; padding: 1px 5px; }
.arch-tag.ar-board { background: #fdeeee; color: #a93226; }
.arch-tag.ar-low { background: #edf7ee; color: #1e7e34; }
.arch-tag.ar-base { background: #f4f5f7; color: #5a6472; }
.pi-levels { display: flex; gap: 10px; flex-wrap: wrap; font-size: 11.5px; color: #8a94a6; margin: 6px 0; }
.pi-levels b { color: #5a6472; font-weight: 600; }
.pi-sizing { font-size: 11px; color: #6c3fc5; background: #f8f5fe; border-radius: 5px; padding: 3px 7px; margin-bottom: 5px; }
.q-line { font-style: normal; color: #8a94a6; font-size: 10.5px; margin-left: 3px; }
.pi-moves { display: flex; flex-direction: column; gap: 3px; }
.mv-row { display: flex; gap: 8px; font-size: 11.8px; line-height: 1.45; }
.mv-t { flex: 0 0 128px; color: #5a6472; }
.mv-a { flex: 1; color: #3c4552; }
.mv-a.act-add, .mv-a.act-dip { color: var(--up); font-weight: 600; }
.mv-a.act-half, .mv-a.act-cut, .mv-a.act-clear, .mv-a.act-stop { color: var(--down); font-weight: 600; }
.mv-a.act-hold { color: #5a6472; }
.mv-a.act-wait { color: #b8860b; }
.pi-risk { margin-top: 6px; font-size: 11px; color: #8a94a6; border-top: 1px dashed #e8ebf0; padding-top: 5px; }
.cheat-box summary { cursor: pointer; font-size: 12.5px; color: #2d6cdf; }
.cheat-table { font-size: 12px; margin-top: 8px; }
/* 快捷预设行 */
.preset-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.preset-label { font-size: 12px; color: #8a94a6; }
.preset-row .chip.on { background: #f4eefd; border-color: #b9a3e8; color: #6c3fc5; font-weight: 600; }
/* 热度列迷你条 */
.heat-cell { display: flex; flex-direction: column; gap: 3px; min-width: 46px; }
.hbar { display: block; height: 3px; background: #eef0f3; border-radius: 2px; overflow: hidden; }
.hbar u { display: block; height: 100%; background: linear-gradient(90deg, #e67e22, #c0392b); }
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

/* —— 移动端(600px 断点,与全站一致):横滑条防折行堆叠 ——
   概念面板 15 板若一板一行会把正文顶出三屏,是最优先要收的一块 */
@media (max-width: 600px) {
  .head-title { font-size: 17px; }
  .head-row { gap: 8px; }
  /* 作战面板纵叠:三问各成块,梯队横滑 */
  .battle-grid { grid-template-columns: 1fr; gap: 10px; }
  .ladder-row {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
  }
  .ladder-row::-webkit-scrollbar { display: none; }
  .lv-chip { flex: none; }
  /* 预案卡纵叠;复盘条横滑 */
  .plan-grid { grid-template-columns: 1fr; }
  .review-strip { white-space: nowrap; overflow-x: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .review-strip::-webkit-scrollbar { display: none; }
  .mv-t { flex-basis: 104px; }
  /* 快捷预设单行横滑 */
  .preset-row {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
    margin-inline: -11px;
    padding-inline: 11px;
  }
  .preset-row::-webkit-scrollbar { display: none; }
  .preset-row .chip { flex: none; white-space: nowrap; }
  /* 环境行:五个指标一条横滑,不折行 */
  .env-row {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
    margin-inline: -11px;
    padding-inline: 11px;
  }
  .env-row::-webkit-scrollbar { display: none; }
  .env-item { flex: none; white-space: nowrap; }
  /* 风险分布 chips 单行横滑 */
  .stat-chips {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
    margin-inline: -11px;
    padding-inline: 11px;
  }
  .stat-chips::-webkit-scrollbar { display: none; }
  .stat-chips .chip { flex: none; padding: 5px 12px; min-height: 34px; }
  /* 概念面板单行横滑;窄条里去涨幅列(悬浮有全量),副标题藏起不占两行 */
  .concept-row {
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
    margin-inline: -11px;
    padding-inline: 11px;
  }
  .concept-row::-webkit-scrollbar { display: none; }
  .concept-chip { flex: none; padding: 6px 9px; }
  .concept-chip .cc-chg { display: none; }
  .concept-card .sec-sub { display: none; }
  /* 工具条:两组 tab 各自整行横滑,下拉与搜索分享一行 */
  .list-card .toolbar { gap: 8px; }
  .list-card .toolbar .tabs {
    flex: 1 1 100%;
    overflow-x: auto;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
  }
  .list-card .toolbar .tabs::-webkit-scrollbar { display: none; }
  .list-card .toolbar .tabs .tab { flex: none; white-space: nowrap; }
  .list-card .toolbar .sel { flex: 1 1 auto; min-width: 0; min-height: 34px; }
  .list-card .toolbar input { flex: 1 1 120px; min-width: 0; min-height: 34px; }
  /* 已筛概念条不满宽 */
  .concept-active { max-width: 100%; }
}
</style>
