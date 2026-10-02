<script setup>
/* 情绪日报页:盘后状态卡(可回看历史交易日) + 走势图 + 历史明细 + 明日预案 + 板块效应/候选池/黑名单。
 * 数据源 /api/sentiment/*,采集与口径见 docs/使用说明书.md「情绪日报」。
 * 候选池股票名链到详情页(#/stock/:code);走势图 echarts 四联格(涨停+炸板率/红盘率/R5/状态色带)
 * 共用一个 dataZoom,状态带就是每根顶满的色柱,缩放时与曲线天然同步。
 */
import { computed, onBeforeUnmount, onMounted, ref, nextTick } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'

const daily = ref(null)
const history = ref([])          // 升序(旧→新),来自 /sentiment/history
const error = ref('')
const loadMsg = ref('')          // 历史日期切换时的瞬时提示
const trendEl = ref(null)
let chart = null
let echarts = null   // ensureEcharts() 注入（window.echarts 同一份）

const TREND_DAYS = 120           // 走势卡取数窗口
const VISIBLE_DAYS = 60          // dataZoom 默认露出的天数(其余拖滚动条看)

onMounted(async () => {
  try {
    daily.value = await get('/sentiment/daily')
    history.value = await get('/sentiment/history', { limit: TREND_DAYS })
  } catch (e) {
    error.value = String(e.message || e)
  }
  if (daily.value && history.value.length) {
    await nextTick()
    // echarts 动态加载（1MB 不进页面关键路径）；失败则跳过建图（renderTrend 里
    // echarts 还是 null，硬跑会 null.init 抛未捕获异常），走势卡留占位高度
    try {
      echarts = await ensureEcharts()
      renderTrend()
      window.addEventListener('resize', onResize)
    } catch (e) { /* 走势卡空着：其余卡片照常 */ }
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) chart.dispose()
  chart = null
})
function onResize() { if (chart) chart.resize() }

const latestDate = computed(() => (history.value.length ? history.value[history.value.length - 1].date : null))
const isLatest = computed(() => !daily.value || !latestDate.value || daily.value.date === latestDate.value)

// 历史日期切换:±1 个交易日。history 只有 120 日窗口,翻出窗口时接口会给 404,提示里说明
async function goDay(step) {
  const idx = history.value.findIndex((h) => h.date === daily.value?.date)
  const next = history.value[idx + step]
  if (!next) { loadMsg.value = step < 0 ? '已在窗口最早一天(走势卡只留 120 日)' : '已是最新一期' }
  else await loadDate(next.date)
}
async function loadDate(d) {
  loadMsg.value = '加载 ' + d + ' …'
  try {
    daily.value = await get('/sentiment/daily', { date: d })
    loadMsg.value = ''
    window.scrollTo({ top: 0, behavior: 'smooth' })
  } catch (e) {
    loadMsg.value = '该日期无日报: ' + (e.message || e)
  }
}

const IND = [
  ['red_rate', '红盘率', v => v == null ? '-' : (v * 100).toFixed(0) + '%'],
  ['zt_count', '涨停', v => v ?? '-'],
  ['zha_rate', '炸板率', v => v == null ? '-' : (v * 100).toFixed(0) + '%'],
  ['dt_count', '跌停', v => v ?? '-'],
  ['yz_dt_count', '一字跌停', v => v ?? '-'],
  ['r5', 'R5(国证2000)', v => v == null ? '-' : (v * 100).toFixed(2) + '%'],
]

const TONE_CN = { strong: '极强', warm: '回暖', flat: '普通', weak: '极弱' }
const TONE_COLOR = { strong: '#dc2626', warm: '#ea580c', flat: '#d1d5db', weak: '#16a34a' }
// 温度/极端度(API 读时计算,collector/scripts/sentiment_score.py 同源锚点)
const RANK_LABELS = { r5: 'R5', red: '红盘', zt: '涨停', short: '空头', zha: '炸板' }
const rankPct = (r) => r == null ? '-' : Math.round(r * 100) + '%'
// 踩线说明的主导触发指标:极强=正向分位≥80/反向≤20,极弱镜像(与后端 dominant_of 同规则)
function dominant(score, state) {
  const pos = ['r5', 'red', 'zt'], neg = ['short', 'zha']
  const hot = (k, r) => state === '极强'
    ? (pos.includes(k) ? r >= 0.8 : r <= 0.2)
    : (pos.includes(k) ? r <= 0.2 : r >= 0.8)
  return Object.entries(score?.ranks || {})
    .filter(([k, r]) => (pos.includes(k) || neg.includes(k)) && hot(k, r))
    .map(([k, r]) => RANK_LABELS[k] + ' ' + rankPct(r))
}
function fmtAmount(wan) {
  if (wan == null) return '-'
  return wan >= 10000 ? (wan / 10000).toFixed(1) + '亿' : Math.round(wan).toLocaleString() + '万'
}
const pct1 = (v) => v == null ? null : +(v * 100).toFixed(1)

// ---- 走势图:四联格共用一根 x(交易日)与同一个 dataZoom ----
function renderTrend() {
  if (!trendEl.value || !history.value.length) return
  const days = history.value
  const dates = days.map((d) => d.date)
  const zt = days.map((d) => d.zt_count)
  const zha = days.map((d) => pct1(d.zha_rate))
  const red = days.map((d) => pct1(d.red_rate))
  const t = days.map((d) => d.t)   // 温度 0-100(R5 保留在 tooltip/状态卡/历史表)
  const startPct = days.length > VISIBLE_DAYS ? (1 - VISIBLE_DAYS / days.length) * 100 : 0
  const hiddenX = { xAxis: { show: false } }
  chart = echarts.init(trendEl.value)
  chart.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#334155' } },
      formatter: (params) => {
        const i = params[0]?.dataIndex ?? 0
        const d = days[i]
        if (!d) return ''
        const pc = (v) => v == null ? '-' : (v * 100).toFixed(1) + '%'
        return `<b>${d.date} ${d.state}${d.trap ? ' ⚠陷阱日' : ''}</b><br/>` +
          `涨停 ${d.zt_count ?? '-'} · 炸板率 ${pc(d.zha_rate)}<br/>` +
          `红盘率 ${pc(d.red_rate)} · 跌停 ${d.dt_count ?? '-'}(一字 ${d.yz_dt_count ?? '-'})<br/>` +
          `温度 ${d.t ?? '-'}/100 · R5 ${d.r5 == null ? '-' : (d.r5 * 100).toFixed(2) + '%'}`
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 46, right: 46, top: 24, height: '30%' },    // 涨停家数 + 炸板率
      { left: 46, right: 46, top: '48%', height: '16%' }, // 红盘率
      { left: 46, right: 46, top: '70%', height: '14%' }, // R5
      { left: 46, right: 46, top: '87%', height: 12 },    // 状态色带
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, ...hiddenX },
      { type: 'category', data: dates, gridIndex: 1, ...hiddenX },
      { type: 'category', data: dates, gridIndex: 2, ...hiddenX },
      { type: 'category', data: dates, gridIndex: 3, axisTick: { show: false },
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: '#6b7280', fontSize: 10,
          formatter: (v) => v.slice(5).replace('-', '/') } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, name: '涨停', nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
      { type: 'value', gridIndex: 0, name: '炸板%', position: 'right', min: 0, max: 100,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { show: false } },
      { type: 'value', gridIndex: 1, min: 0, max: 100, name: '红盘%', splitNumber: 2,
        nameTextStyle: { color: '#9ca3af', fontSize: 10 },
        axisLabel: { color: '#6b7280', fontSize: 10 }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
      { type: 'value', gridIndex: 2, min: 0, max: 100, name: '温度', splitNumber: 2,
        nameTextStyle: { color: '#9ca3af', fontSize: 10, align: 'right' },
        axisLabel: { color: '#6b7280', fontSize: 10, formatter: (v) => v.toFixed(0) },
        splitLine: { lineStyle: { color: '#f1f5f9' } } },
      { type: 'value', gridIndex: 3, min: 0, max: 1, show: false },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2, 3], start: startPct, end: 100, zoomOnMouseWheel: false, moveOnMouseWheel: true },
      { type: 'slider', xAxisIndex: [0, 1, 2, 3], start: startPct, end: 100, bottom: 2, height: 16,
        borderColor: '#e5e7eb', fillerColor: 'rgba(37,99,235,0.08)' },
    ],
    series: [
      { name: '涨停家数', type: 'bar', xAxisIndex: 0, yAxisIndex: 0, data: zt,
        itemStyle: { color: 'rgba(220,38,38,0.75)' }, barMaxWidth: 10 },
      { name: '炸板率%', type: 'line', xAxisIndex: 0, yAxisIndex: 1, data: zha,
        symbol: 'none', lineStyle: { color: '#ea580c', width: 1.5 } },
      { name: '红盘率%', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: red,
        symbol: 'none', lineStyle: { color: '#059669', width: 1.5 },
        areaStyle: { color: 'rgba(5,150,105,0.08)' } },
      { name: '温度T', type: 'line', xAxisIndex: 2, yAxisIndex: 3, data: t,
        symbol: 'none', connectNulls: true, lineStyle: { color: '#2563eb', width: 1.5 },
        // 20/80 虚线=极弱/极强带边界(T 与状态机并行,档内高低在曲线上直接可读)
        markLine: { silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#cbd5e1', type: 'dashed', width: 1 },
          data: [{ yAxis: 20 }, { yAxis: 80 }] } },
      { // 状态色带:每根顶满的色柱,颜色即当日档位;缩放/悬浮与上面三格天然同步
        name: '状态', type: 'bar', xAxisIndex: 3, yAxisIndex: 4, barWidth: '100%',
        data: days.map((d) => ({ value: 1, itemStyle: { color: TONE_COLOR[d.tone] || '#d1d5db' } })),
        silent: false, cursor: 'pointer',
        onClick: (p) => loadDate(days[p.dataIndex].date),
      },
    ],
  })
}

// 历史明细的着色复用档位色;点行跳当天
function goRow(d) { loadDate(d) }
</script>

<template>
  <div class="sent">
    <div v-if="error" class="err">加载失败:{{ error }}(服务端可能还没跑过 fetch_sentiment)</div>

    <template v-if="daily">
      <!-- ① 状态卡(◀ ▷ 翻历史交易日;非最新时整页都是那天的数据) -->
      <section class="card head" :class="'tone-' + daily.tone">
        <div class="badge">{{ daily.state }}</div>
        <div class="head-meta">
          <div class="date">
            {{ daily.date }}<span class="st">{{ daily.state }}</span>
            <span v-if="daily.prev_state" class="prev">· 昨日 {{ daily.prev_state }}</span>
            <span v-if="!isLatest" class="hist-flag">历史查看中</span>
          </div>
          <div v-if="daily.score" class="score">
            <span v-if="daily.score.extreme != null" class="se" :class="'bd-' + daily.tone">
              极端度 {{ daily.score.extreme }}/100 · {{ daily.score.band }}</span>
            <span v-else class="se">温度 {{ daily.score.t }}/100</span>
            <span v-if="daily.score.streak >= 2" class="stk">{{ daily.state }} · 第{{ daily.score.streak }}日</span>
            <span v-if="daily.score.ranks" class="ranks">
              <template v-for="(v, k) in daily.score.ranks" :key="k">{{ RANK_LABELS[k] || k }} {{ rankPct(v) }}&ensp;</template>
            </span>
          </div>
          <div class="nav">
            <button type="button" @click="goDay(-1)" :title="'查看前一交易日'">◀ 前一日</button>
            <button v-if="!isLatest" type="button" class="latest" @click="loadDate(latestDate)">回到最新</button>
            <button type="button" @click="goDay(1)" :disabled="isLatest" title="查看后一交易日">后一日 ▶</button>
          </div>
          <div class="inds">
            <span v-for="[k, label, fmt] in IND" :key="k" class="ind">
              <em>{{ label }}</em>{{ fmt(daily[k]) }}
            </span>
          </div>
        </div>
      </section>
      <p v-if="loadMsg" class="loadmsg">{{ loadMsg }}</p>
      <p v-if="daily.trap" class="trap">
        ⚠ 陷阱日:昨日极弱、今日转普通——该形态次日涨停溢价实测 +0.55%/胜率 50%,全局禁止隔夜。
      </p>
      <p v-if="daily.score && daily.score.band === '踩线'" class="trap">
        ⚠ 踩线{{ daily.state }}:极端度 {{ daily.score.extreme }} 未达 80——主导
        {{ dominant(daily.score, daily.state).join('、') || '—' }};单指标驱动、综合温度未跟上,操作按普通档仓位对待。
      </p>
      <p v-if="daily.score && daily.score.divergence" class="trap">
        ⚠ 温度与档位背离:综合温度 {{ daily.score.t }} 已达{{ daily.score.divergence === 'high' ? '极强' : '极弱' }}带但状态机未触发——多指标同向而均未过线,关注次日确认。
      </p>

      <!-- ② 走势:近 120 个交易日,四联格 + 缩放;状态带点柱跳当日 -->
      <section v-if="history.length" class="card">
        <h3>走势<span class="sub">近 {{ history.length }} 个交易日 · 默认露最近 {{ VISIBLE_DAYS }} 日,滚动条/滚轮平移;点状态色带或历史明细行可回看当日</span></h3>
        <div ref="trendEl" class="trend"></div>
        <p class="legend">
          <i class="bg-strong"></i>极强 <i class="bg-warm"></i>回暖 <i class="bg-flat"></i>普通 <i class="bg-weak"></i>极弱
          <span class="lg-note">红柱=涨停家数 橙线=炸板率 绿线=红盘率 蓝线=温度T(虚线 20/80=极弱/极强带)</span>
        </p>
      </section>

      <!-- ③ 明日预案 -->
      <section class="card">
        <h3>明日预案(转移概率,2024-01~2026-09 校准)</h3>
        <div v-for="(p, k) in daily.transitions" :key="k" class="trow">
          <span class="tname">{{ k }}</span>
          <span class="tbar"><i :style="{ width: (p * 100) + '%' }"></i></span>
          <span class="tpct">{{ Math.round(p * 100) }}%</span>
        </div>
      </section>

      <!-- ④ 细分板块效应 -->
      <section class="card">
        <h3>细分板块效应<span class="sub">东财行业,同行业涨停≥3家 · 比大类细一档(医药→化学制药/医疗服务/医疗器械)</span></h3>
        <table v-if="(daily.sectors_fine || []).length" class="tbl">
          <thead><tr><th>细分行业</th><th class="num">涨停家数</th><th class="num">占涨停%</th><th class="num">最高连板</th><th class="num">封单合计(亿)</th></tr></thead>
          <tbody>
            <tr v-for="s in daily.sectors_fine" :key="s.industry">
              <td>{{ s.industry }}</td>
              <td class="num">{{ s.limit_count }}</td>
              <td class="num">{{ s.share_of_zt }}%</td>
              <td class="num">{{ s.max_streak ?? '-' }}</td>
              <td class="num">{{ s.seal_yi ?? '-' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">当日无细分板块聚集。</p>
      </section>

      <!-- ⑤ 大类板块效应 -->
      <section class="card">
        <h3>大类板块效应<span class="sub">证监会行业,涨停≥5家且占该行业成分≥2% · 次日溢价档 {{ daily.report.premium_hint || '-' }}</span></h3>
        <table v-if="(daily.sectors || []).length" class="tbl">
          <thead><tr><th>行业</th><th class="num">涨停家数</th><th class="num">占行业%</th><th class="num">成交额</th></tr></thead>
          <tbody>
            <tr v-for="s in daily.sectors" :key="s.industry">
              <td>{{ s.industry }}</td>
              <td class="num">{{ s.limit_count }}</td>
              <td class="num">{{ s.share_of_industry }}%</td>
              <td class="num">{{ fmtAmount(s.amount_yi * 10000) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">当日无板块效应行业(散点行情,谨慎追高)。</p>
      </section>

      <!-- ⑥ 候选池 -->
      <section class="card">
        <h3>候选池<span class="sub">两类板块效应行业内自然涨停 · 预期竞价溢价 {{ daily.report.premium_hint || '-' }}</span></h3>
        <table v-if="(daily.report.candidates || []).length" class="tbl">
          <thead><tr><th>代码/名称</th><th>大类行业</th><th>细分行业</th><th class="num">连板</th><th class="num">收盘</th><th class="num">涨跌幅</th><th class="num">成交额</th><th class="num">流通市值(亿)</th></tr></thead>
          <tbody>
            <tr v-for="c in daily.report.candidates" :key="c.code">
              <td><router-link :to="'/stock/' + c.code" class="code-link">{{ c.code }} {{ c.name }}</router-link></td>
              <td>{{ c.industry }}</td>
              <td>{{ c.fine_industry || '-' }}</td>
              <td class="num">{{ c.streak ?? '-' }}</td>
              <td class="num">{{ c.price }}</td>
              <td class="num">{{ c.chg_pct }}%</td>
              <td class="num">{{ fmtAmount(c.amount_wan) }}</td>
              <td class="num">{{ c.float_mcap_yi }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">无(当日无板块效应或全是一字板)。</p>
      </section>

      <!-- ⑦ 黑名单 / 跌停观察 -->
      <section class="card">
        <h3>炸板黑名单<span class="sub">次日无条件禁入,实测次日溢价 -1.6% · 共 {{ daily.zha_count }} 只</span></h3>
        <p class="chips">
          <router-link v-for="b in (daily.report.blacklist || []).slice(0, 20)" :key="b.code"
                       :to="'/stock/' + b.code" class="chip chip-bad">{{ b.name }}</router-link>
        </p>
        <h3>跌停观察<span class="sub">一字跌停 {{ daily.yz_dt_count }} 只,关注打开(掏肛需质地与板块预期配合)</span></h3>
        <p class="chips">
          <router-link v-for="w in (daily.report.watch_dt || [])" :key="w.code"
                       :to="'/stock/' + w.code" class="chip chip-watch">{{ w.name }}</router-link>
          <span v-if="!(daily.report.watch_dt || []).length" class="empty">当日无跌停。</span>
        </p>
      </section>

      <!-- ⑧ 历史明细(点行回看当日) -->
      <section v-if="history.length" class="card">
        <details>
          <summary>历史明细<span class="sub">近 {{ history.length }} 个交易日,点任意行回看当天的完整日报</span></summary>
          <div class="tbl-wrap">
            <table class="tbl">
              <thead><tr>
                <th>日期</th><th>状态</th><th class="num">红盘率</th><th class="num">涨停</th>
                <th class="num">炸板率</th><th class="num">跌停</th><th class="num">一字</th>
                <th class="num">R5</th><th class="num">温度</th><th></th>
              </tr></thead>
              <tbody>
                <tr v-for="h in [...history].reverse()" :key="h.date" class="hrow"
                    :class="{ cur: h.date === daily.date }" @click="goRow(h.date)">
                  <td>{{ h.date }}</td>
                  <td><span class="dot" :style="{ background: TONE_COLOR[h.tone] }"></span>{{ h.state }}<span v-if="h.streak >= 2" class="stkk">·{{ h.streak }}</span></td>
                  <td class="num">{{ pct1(h.red_rate) ?? '-' }}%</td>
                  <td class="num">{{ h.zt_count ?? '-' }}</td>
                  <td class="num">{{ pct1(h.zha_rate) ?? '-' }}%</td>
                  <td class="num">{{ h.dt_count ?? '-' }}</td>
                  <td class="num">{{ h.yz_dt_count ?? '-' }}</td>
                  <td class="num">{{ h.r5 == null ? '-' : (h.r5 * 100).toFixed(2) + '%' }}</td>
                  <td class="num">{{ h.t ?? '-' }}</td>
                  <td class="num">{{ h.trap ? '⚠陷阱' : '' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </section>

      <p class="note">口径:全市场 A 股收盘快照(剔除停牌),涨停按交易所涨停价判定;状态阈值与转移矩阵为
        2024-01~2026-09 校准,积累满 60 交易日后换滚动分位。历史回看:走势卡留 120 个交易日,
        更早的期次随数据积累仍在 /api/sentiment/history 里(limit 上限 500)。采集:交易日 15:35 fetch_sentiment。
        温度 T/极端度/连续天数为 API 读时计算(锚点 static-v1,满 60 交易日后切滚动 250 日分位),
        状态机阈值不受影响。</p>
    </template>
    <p v-else-if="!error" class="empty">加载中…</p>
  </div>
</template>

<style scoped>
.sent { max-width: 1100px; margin: 0 auto; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.card .sub { font-weight: normal; color: #6b7280; font-size: 12px; margin-left: 8px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }

.head { display: flex; align-items: center; gap: 16px; }
.badge { font-size: 30px; font-weight: 700; color: #fff; background: #6b7280; border-radius: 10px; padding: 10px 18px; }
.tone-strong .badge { background: #dc2626; }  /* A股红涨 */
.tone-warm .badge { background: #ea580c; }
.tone-flat .badge { background: #6b7280; }
.tone-weak .badge { background: #16a34a; }
.head-meta .date { font-size: 16px; font-weight: 600; }
.head-meta .st { font-size: 12px; color: #6b7280; font-weight: normal; margin-left: 6px; }
.head-meta .prev { color: #6b7280; font-weight: normal; font-size: 13px; margin-left: 6px; }
.hist-flag { font-size: 12px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a;
  border-radius: 999px; padding: 1px 8px; margin-left: 8px; font-weight: normal; }
.nav { margin-top: 6px; display: flex; gap: 6px; }
.nav button { font-size: 12px; padding: 3px 10px; border: 1px solid #e5e7eb; background: #fff;
  color: #374151; border-radius: 6px; cursor: pointer; }
.nav button:hover:not(:disabled) { border-color: #2563eb; color: #2563eb; }
.nav button:disabled { opacity: 0.45; cursor: default; }
.nav .latest { border-color: #bfdbfe; background: #eff6ff; color: #1d4ed8; }
.inds { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 6px; font-size: 13px; }
.inds em { font-style: normal; color: #6b7280; margin-right: 4px; }

.score { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; }
.score .se { font-weight: 700; font-size: 18px; }
.score .se.bd-strong { color: #dc2626; }
.score .se.bd-weak { color: #16a34a; }
.score .stk { font-size: 12px; color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe;
  border-radius: 999px; padding: 1px 8px; }
.score .ranks { font-size: 12px; color: #6b7280; }
.stkk { color: #9ca3af; font-size: 12px; margin-left: 2px; }

.loadmsg { color: #6b7280; font-size: 12px; margin: 0 0 10px; }
.trap { color: #92400e; background: #fffbeb; border: 1px solid #fde68a; padding: 8px 14px; border-radius: 8px; }

.trend { width: 100%; height: 380px; }
.legend { font-size: 12px; color: #6b7280; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 10px; }
.legend .lg-note { margin-left: 12px; color: #9ca3af; }
.bg-strong { background: #dc2626; } .bg-warm { background: #ea580c; }
.bg-flat { background: #d1d5db; } .bg-weak { background: #16a34a; }

.trow { display: flex; align-items: center; gap: 10px; margin: 6px 0; font-size: 13px; }
.tname { width: 3em; text-align: right; }
.tbar { flex: 1; background: #f3f4f6; border-radius: 4px; height: 14px; overflow: hidden; }
.tbar i { display: block; height: 100%; background: #2563eb; }
.tpct { width: 3em; }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 5px 8px; border-bottom: 1px solid #f1f5f9; text-align: left; }
.tbl th { color: #6b7280; font-weight: 500; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.code-link { color: #2563eb; text-decoration: none; }
.code-link:hover { text-decoration: underline; }
/* 历史明细:整行可点,当天行高亮 */
details > summary { cursor: pointer; font-size: 15px; font-weight: 600; margin-bottom: 8px; }
details > summary .sub { font-weight: normal; }
.tbl-wrap { max-height: 420px; overflow-y: auto; }
.hrow { cursor: pointer; }
.hrow:hover { background: #f8fafc; }
.hrow.cur { background: #eff6ff; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; }
.chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; text-decoration: none; }
.chip-bad { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.chip-watch { background: #f0f9ff; color: #075985; border: 1px solid #bae6fd; }

.note { color: #9ca3af; font-size: 12px; line-height: 1.6; }
.empty { color: #9ca3af; font-size: 13px; }
@media (max-width: 600px) {
  .head { flex-direction: column; align-items: flex-start; }
  .tbl { font-size: 12px; }
  .trend { height: 320px; }
}
</style>
