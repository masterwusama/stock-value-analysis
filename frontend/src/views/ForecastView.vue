<script setup>
/* 业绩预测页:标的(紫金/广信)分部量价成本表 + 指引完成度 + 价格QTD环比 + ±10%敏感度。
 * 数据源 /api/forecast(读时合成,口径见 app/api/forecast.py 头注)。
 * 定位:研究工具——量/成本为披露滞后数据,价格为库内最新,时点差是误差主来源;
 * 广信为敏感性推演标的(thesis 声明),不做绝对利润预测。
 */
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'

const isMobile = useMediaQuery(MOBILE_QUERY)
const data = ref(null)
const error = ref('')
const curSid = ref('')
const chartEl = ref(null)
let chart = null
let echarts = null
const curCode = ref('')  // 主图序列(code),空=不画

onMounted(async () => {
  try {
    const d = await get('/forecast')
    data.value = d
    curSid.value = d.targets?.[0]?.sid || ''
    echarts = await ensureEcharts()
    // ensureEcharts 完成时 watch(curCode) 的首轮 render 可能已因 echarts 未就绪
    // 被跳过,且 curCode 不再变化不会重触发——这里补一次
    await nextTick()
    render()
  } catch (e) {
    error.value = String(e.message || e)
  }
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) chart.dispose()
  chart = null
})
function onResize() { if (chart) chart.resize() }

const target = computed(() =>
  (data.value?.targets || []).find(t => t.sid === curSid.value) || null)
// 全部可选价格序列(分部内拍平),供主图切换
const allPrices = computed(() => {
  const out = []
  for (const seg of target.value?.segments || [])
    for (const p of seg.prices || [])
      out.push({ ...p, seg: seg.product })
  return out
})
const byCode = computed(() =>
  Object.fromEntries(allPrices.value.map(p => [p.code, p])))

const num = (v, d = 1) => (v == null ? '-' : (+v).toFixed(d))
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down')
const pct = (v) => (v == null ? '-' : (v >= 0 ? '+' : '') + (+v).toFixed(1) + '%')

function pickPrice(p) { curCode.value = p.code }

function render() {
  if (!chartEl.value || !echarts || !curCode.value) return
  const p = byCode.value[curCode.value]
  if (!p) return
  if (!chart) chart = echarts.init(chartEl.value)
  const pts = p.points || []
  chart.setOption({
    textStyle: { fontFamily: 'inherit' },
    grid: { left: 60, right: 16, top: 30, bottom: 44 },
    title: { text: `${p.name}(${p.unit})`, left: 0, top: 0, textStyle: { fontSize: 13 } },
    tooltip: { trigger: 'axis', confine: true },
    xAxis: { type: 'category', data: pts.map(x => x[0]), boundaryGap: false, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 15, bottom: 6 }],
    series: [{ type: 'line', showSymbol: false, lineStyle: { width: 1.5, color: '#2d6cdf' },
               itemStyle: { color: '#2d6cdf' }, areaStyle: { opacity: 0.06 },
               data: pts.map(x => x[1]) }],
  }, true)
}
watch([curSid, curCode], () => nextTick(render))
watch(() => allPrices.value.length, (n) => {
  if (n && !curCode.value) curCode.value = allPrices.value[0]?.code || ''
})
</script>

<template>
  <div class="fc">
    <div v-if="error" class="err">加载失败:{{ error }}</div>
    <div v-else-if="!data" class="empty">数据加载中…</div>

    <template v-if="target">
      <div class="toolbar tabs">
        <button v-for="t in data.targets" :key="t.sid" type="button" class="tab"
                :class="{ on: t.sid === curSid }" @click="curSid = t.sid">
          {{ t.name }}<span class="code">{{ t.sid }}</span>
        </button>
        <span class="sub head-sub">业绩预测 · 研究工具非投资建议 · 量/成本为披露值,价格为库内最新</span>
      </div>

      <!-- 头卡:论点+期间速览+指引完成度 -->
      <section class="card">
        <div class="thesis">{{ target.thesis }}</div>
        <p class="dim note-line">{{ target.period_note }}</p>
        <div v-if="target.guidance_2026" class="guide">
          <div class="guide-title">2026 指引完成度(Q1-3 实际 / 全年指引)</div>
          <div class="guide-grid">
            <div v-for="(g, prod) in target.guidance_2026" :key="prod" class="g-item">
              <div class="g-name">{{ prod }}<i v-if="g.note" class="g-note">{{ g.note }}</i></div>
              <div class="g-bar"><b :style="{ width: Math.min(100, (g.q1_3 || 0) / g.qty * 100).toFixed(0) + '%' }" /></div>
              <div class="g-vals">
                <span class="g-q">{{ g.q1_3 != null ? g.q1_3 : '—' }} / {{ g.qty }} {{ g.unit }}</span>
                <span v-if="g.q1_3" :class="cls((g.q1_3 / g.qty) - 0.75)">{{ num((g.q1_3 / g.qty) * 100, 0) }}%</span>
              </div>
            </div>
          </div>
          <p class="dim" v-if="target.guidance_source">{{ target.guidance_source }}</p>
        </div>
      </section>

      <!-- 分部表 -->
      <section v-for="seg in target.segments" :key="seg.product" class="card">
        <h3>{{ seg.product }}</h3>
        <div v-if="seg.note" class="dim note-line">{{ seg.note }}</div>
        <table class="grid" v-if="seg.prices.length">
          <thead>
            <tr><th class="stick">价格序列</th><th>单位</th><th>最新</th><th>QTD均</th><th>上季均</th><th>环比</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in seg.prices" :key="p.code" :class="{ on: curCode === p.code }" @click="pickPrice(p)">
              <td class="stick">{{ p.name }}</td>
              <td>{{ p.unit }}</td>
              <td>{{ num(p.stats.latest, p.unit === '元/克' ? 1 : 0) }} <i class="dim">({{ p.stats.latest_date }})</i></td>
              <td>{{ num(p.stats.qtd, 0) }} <i class="dim">{{ p.stats.quarter }}</i></td>
              <td>{{ num(p.stats.prev_q, 0) }}</td>
              <td :class="cls(p.stats.qoq)">{{ pct(p.stats.qoq) }}</td>
              <td class="dim">看图→</td>
            </tr>
          </tbody>
        </table>
        <div class="meta">
          <span v-if="seg.volume.latest.qty != null" class="chip">
            量:{{ seg.volume.latest.qty }} {{ seg.volume.latest.unit || '' }}
            <i class="dim">{{ seg.volume.latest.period }}{{ seg.volume.latest.yoy ? ' · ' + seg.volume.latest.yoy : '' }}</i>
          </span>
          <span v-if="seg.volume.latest.avg_price != null" class="chip">
            公告均价:{{ seg.volume.latest.avg_price }} 元/吨 <i class="dim">(口径基准)</i>
          </span>
          <span v-if="seg.unit_cost.latest.cost != null" class="chip v">
            单位成本:{{ seg.unit_cost.latest.cost }} {{ seg.unit_cost.latest.unit }}
            <i class="dim">{{ seg.unit_cost.latest.period }}{{ seg.unit_cost.latest.qoq ? ' · 环比' + seg.unit_cost.latest.qoq : '' }}</i>
          </span>
          <span v-if="seg.sensitivity_10pct != null" class="chip warn" title="±10%价格 → 年化毛利变动(亿,权益100%简化)">
            ±10%价 → 毛利 ±{{ seg.sensitivity_10pct }} 亿
          </span>
          <span v-for="cd in seg.cost_drivers" :key="cd.name" class="chip bad">{{ cd.name }} {{ cd.change }}</span>
          <span v-for="se in seg.spread_extra" :key="se.name" class="chip bad">{{ se.name }}({{ se.unit }})</span>
          <span v-if="seg.params.quasi_cash_yi" class="chip v">
            类现金 {{ seg.params.quasi_cash_yi }} 亿 · Q3理财收益 {{ seg.params.q3_wealth_income_yi }} 亿
          </span>
        </div>
        <p v-if="seg.sensitivity_note" class="dim">{{ seg.sensitivity_note }}</p>
      </section>

      <!-- 主图 -->
      <section class="card" v-if="curCode">
        <div ref="chartEl" class="chart" :style="{ height: isMobile ? '280px' : '380px' }"></div>
        <p class="dim tip">点上方任一价格行切换序列;QTD=本季至今均值(读时合成)。</p>
      </section>

      <!-- 观察哨 -->
      <section class="card">
        <h3>观察哨</h3>
        <ul class="watch">
          <li v-for="(w, i) in target.watchlist" :key="i">{{ w }}</li>
        </ul>
        <p v-if="target.equity_notes" class="dim">{{ target.equity_notes }}</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.fc { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h3 { margin: 0 0 8px; font-size: 15px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); font-size: 12px; }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }
.note-line { margin: 4px 0 8px; line-height: 1.6; }
.thesis { font-size: 14px; font-weight: 600; line-height: 1.7; }
.tabs { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tab { font-size: 13px; padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt, #374151); }
.tab.on { border-color: var(--accent, #2d6cdf); color: var(--accent, #2d6cdf); background: #eff6ff; font-weight: 600; }
.tab .code { color: var(--sub, #9ca3af); font-size: 12px; margin-left: 4px; font-weight: normal; }
.head-sub { margin-left: auto; }
.guide { margin-top: 10px; }
.guide-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.guide-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
.g-item { border: 1px solid var(--line, #e5e9f2); border-radius: 9px; padding: 8px 10px; }
.g-name { font-size: 12.5px; font-weight: 600; }
.g-note { font-style: normal; font-weight: normal; color: var(--sub); font-size: 11px; margin-left: 4px; }
.g-bar { height: 6px; border-radius: 3px; background: var(--line-soft, #e5e9f2); overflow: hidden; margin: 6px 0 4px; }
.g-bar b { display: block; height: 100%; background: var(--accent, #2d6cdf); opacity: .6; }
.g-vals { display: flex; justify-content: space-between; font-size: 12px; font-variant-numeric: tabular-nums; }
table.grid { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0; }
table.grid th, table.grid td { padding: 6px 8px; border-bottom: 1px solid var(--line-soft, #eef1f7); text-align: right; white-space: nowrap; }
table.grid th { color: var(--sub); font-weight: 500; background: var(--card); }
table.grid td.stick, table.grid th.stick { text-align: left; position: sticky; left: 0; background: var(--card); }
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--bg-soft, #f8fafd); }
tbody tr.on { background: var(--accent-soft, #eaf1fe); }
.meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.chip { font-size: 12px; background: var(--bg-soft, #f6f8fc); border-radius: 999px; padding: 3px 10px;
  display: inline-flex; align-items: center; gap: 4px; font-variant-numeric: tabular-nums; }
.chip.v { background: #eff6ff; color: #1e40af; }
.chip.warn { background: #fffbeb; color: #92400e; }
.chip.bad { background: #fef2f2; color: #991b1b; }
.chart { width: 100%; }
.tip { margin: 6px 2px 0; }
.watch { margin: 4px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.9; }
</style>
