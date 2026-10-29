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
// 全部可选价格序列(价格+成本+价差拍平),供主图切换
const allPrices = computed(() => {
  const out = []
  for (const seg of target.value?.segments || []) {
    for (const p of seg.prices || []) out.push({ ...p, seg: seg.product })
    for (const p of seg.costs || []) out.push({ ...p, seg: seg.product, isCost: true })
    for (const p of seg.spreads || []) out.push({ ...p, seg: seg.product, isSpread: true })
  }
  return out
})
const byCode = computed(() =>
  Object.fromEntries(allPrices.value.map(p => [p.code, p])))

const num = (v, d = 1) => (v == null ? '-' : (+v).toFixed(d))
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down')
const pct = (v) => (v == null ? '-' : (v >= 0 ? '+' : '') + (+v).toFixed(1) + '%')
const scnCN = { low: '悲观', base: '基准', high: '乐观' }
const scnTag = { low: 'fa-arrow-down', base: 'fa-circle', high: 'fa-arrow-up' }
const projKeys = ['revenue_yi', 'gross_margin_pct', 'non_gaap_yi', 'attributable_yi']
const projHead = { revenue_yi: '营收(亿)', gross_margin_pct: '毛利率%', non_gaap_yi: '扣非(亿)', attributable_yi: '归母(亿)' }
const q4Baseline = computed(() => {
  const m = target.value?.model
  if (!m || !m.q4) return ''
  if (m.baseline_label) return m.baseline_label
  return m.q4.mode === '增量桥' ? 'Q3归母/扣非' : 'Q3营收8.59亿/GM37.29%'
})
const periodShort = computed(() => {
  const lbl = target.value?.model?.period_label || ''
  if (lbl.includes('H2')) return 'H2'
  if (lbl.includes('Q3')) return 'Q3'   // 三季报未披露标的:预测目标=Q3(下一份未出财报)
  return 'Q4'
})
// A/H 市场标准化:徽标 + 口径条(H股=半年报制/无扣非;A股=季报制/归母+扣非)
const mktLabel = (m) => (m === 'HK' ? 'H' : m === 'A+H' ? 'A+H' : 'A')
const mktCls = (m) => (m === 'HK' ? 'hk' : m === 'A+H' ? 'ah' : 'a')
const mktStrip = computed(() => {
  const m = target.value?.market
  if (m === 'HK') return '港股上市 · 半年报制(无季报,年报次年3月) · 人民币 · 仅归母——H股不披露扣非'
  if (m === 'A+H') return 'A+H两地上市 · 预测走A股季报口径 · 人民币 · 归母+扣非'
  return 'A股上市 · 季报制 · 人民币 · 归母+扣非'
})
const fmtRange = (arr) => (arr || []).map(v => num(v)).join(' / ')
const fy26Yoy = computed(() => target.value?.model?.fy26?.yoy_pct || null)
const fy27Vals = computed(() => {
  const f = target.value?.model?.fy27
  return f ? (f.attributable_yi || f.non_gaap_yi) : null
})
const fy27Yoy = computed(() => target.value?.model?.fy27?.yoy_pct || null)
const fy27IsNonGaap = computed(() => {
  const f = target.value?.model?.fy27
  return !!f && !f.attributable_yi && !!f.non_gaap_yi
})

function pickPrice(p) { curCode.value = p.code }

// 移动端分部表:三张表(价格/成本/价差)拍平成序列卡列表用的分组描述
function segTables(seg) {
  return [
    { title: "价格序列", rows: seg.prices || [], kind: "price" },
    { title: "成本序列(原料/能源)", rows: seg.costs || [], kind: "cost" },
    { title: "价差(产品−原料,简单差)", rows: seg.spreads || [], kind: "spread" },
  ].filter(t => t.rows.length)
}

function render() {
  if (!chartEl.value || !echarts || !curCode.value) return
  const p = byCode.value[curCode.value]
  if (!p) return
  if (!chart) chart = echarts.init(chartEl.value)
  const pts = p.points || []
  const m = isMobile.value
  chart.setOption({
    textStyle: { fontFamily: 'inherit' },
    grid: { left: m ? 44 : 60, right: 16, top: 30, bottom: m ? 50 : 44 },
    title: { text: `${p.name}(${p.unit})`, left: 0, top: 0, textStyle: { fontSize: m ? 12 : 13 } },
    tooltip: { trigger: 'axis', confine: true },
    xAxis: { type: 'category', data: pts.map(x => x[0]), boundaryGap: false, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
    // 移动端滑杆加高到 18px:15px 的拖把手指点不中
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: m ? 18 : 15, bottom: m ? 4 : 6 }],
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
          <i class="mkt" :class="mktCls(t.market)">{{ mktLabel(t.market) }}</i>{{ t.name }}<span class="code">{{ t.sid }}</span>
        </button>
      </div>
      <!-- 口径说明独立成行(移动端 tabs 单行横滑,塞不进按钮行) -->
      <p class="sub head-sub">业绩预测 · 研究工具非投资建议 · 量/成本为披露值,价格为库内最新</p>

      <!-- 头卡:市场口径条+论点+期间速览+指引完成度 -->
      <section class="card">
        <div class="mkt-strip"><i class="mkt" :class="mktCls(target.market)">{{ mktLabel(target.market) }}</i><span>{{ mktStrip }}</span></div>
        <div class="thesis">{{ target.thesis }}</div>
        <p class="dim note-line">{{ target.period_note }}</p>
        <div v-if="target.guidance_2026" class="guide">
          <div class="guide-title">2026 指引完成度({{ periodShort === 'Q4' ? 'Q1-3 实际' : 'H1 实际' }} / 全年指引)</div>
          <div class="guide-grid">
            <div v-for="(g, prod) in target.guidance_2026" :key="prod" class="g-item" :class="{ fact: g.fact }">
              <div class="g-name">{{ prod }}<i v-if="g.note" class="g-note">{{ g.note }}</i></div>
              <div v-if="g.fact" class="g-vals">
                <span class="g-q">{{ g.qty }} {{ g.unit }}</span><span class="g-wait">事实口径</span>
              </div>
              <template v-else>
                <div class="g-bar" :class="{ pending: g.q1_3 == null }"><b :style="{ width: Math.min(100, (g.q1_3 || 0) / g.qty * 100).toFixed(0) + '%' }" /></div>
                <div class="g-vals">
                  <span class="g-q">{{ g.q1_3 != null ? g.q1_3 : '—' }} / {{ g.qty }} {{ g.unit }}</span>
                  <span v-if="g.q1_3 != null" :class="cls((g.q1_3 / g.qty) - (periodShort === 'Q4' ? 0.75 : 0.5))">{{ num((g.q1_3 / g.qty) * 100, 0) }}%</span>
                  <span v-else class="g-wait">待披露</span>
                </div>
              </template>
            </div>
          </div>
          <p class="dim" v-if="target.guidance_source">{{ target.guidance_source }}</p>
        </div>
      </section>

      <!-- 动态预估(置顶) -->
      <section v-if="target.model && target.model.q4" class="card proj">
        <div class="proj-head">
          <h3>动态预估</h3>
          <span class="sub">{{ target.model.period_label || '2026Q4E' }} · 三情景 · {{ periodShort === 'Q3' ? '价格已实现' : '每日随价格滚动' }}</span>
          <span v-if="target.model.confidence" class="conf" :class="'conf-' + (target.model.confidence.level || '')"
                :title="'依据:\n· ' + (target.model.confidence.drivers || []).join('\n· ') + '\n\n疑点:\n· ' + (target.model.confidence.doubts || []).join('\n· ')">
            把握 {{ target.model.confidence.score }}<em>/100·{{ target.model.confidence.level }}</em>
          </span>
          <span class="sub proj-clock">{{ periodShort === 'Q3' ? 'Q3 已收官 · 价格全部已实现' : `Q4 已过 ${target.model.q4_window.days_gone}/${target.model.q4_window.days_total} 天` }} · 传导 {{ target.model.conduit }}</span>
        </div>

        <div class="scn-cards">
          <div v-for="p in target.model.q4.proj" :key="p.scenario" class="scn-card" :class="p.scenario">
            <div class="scn-name">{{ scnCN[p.scenario] || p.scenario }}</div>
            <div class="scn-val">{{ num(p.attributable_yi) }}<em>亿</em></div>
            <div class="scn-cap">归母净利({{ periodShort }}'26E)</div>
            <div class="scn-cmp">
              <span :class="cls(p.qoq_pct)" title="对比上一报告期实际(基线)">环比 {{ pct(p.qoq_pct) }}</span>
              <span v-if="p.yoy_pct != null" :class="cls(p.yoy_pct)" :title="target.model.yoy_note || ''">同比 {{ pct(p.yoy_pct) }}</span>
            </div>
            <div class="scn-extra">
              <span v-if="p.revenue_yi != null">营收 {{ num(p.revenue_yi) }}亿</span>
              <span v-if="p.gross_margin_pct != null">毛利 {{ num(p.gross_margin_pct) }}%</span>
              <span v-if="p.non_gaap_yi != null">扣非 {{ num(p.non_gaap_yi) }}亿</span>
            </div>
          </div>
        </div>
        <p class="dim scn-note">基线 {{ q4Baseline }}<template v-if="target.model.yoy_note"> · 同比基数:{{ target.model.yoy_note }}</template></p>

        <div class="fy-grid">
          <div class="fy-item" v-if="target.model.fy26">
            <div class="fy-name">{{ target.model.fy26.label || 'FY2026E' }} · 归母</div>
            <div class="fy-val">{{ fmtRange(target.model.fy26.attributable_yi) }}<em>亿</em></div>
            <div class="fy-cmp" v-if="fy26Yoy">同比 <span :class="cls(fy26Yoy[1])">{{ pct(fy26Yoy[1]) }}</span><span class="dim">(基准档)</span></div>
            <div class="fy-cmp dim" v-else-if="target.model.fy26_note" style="font-size:11px">{{ target.model.fy26_note }}</div>
          </div>
          <div class="fy-item" v-if="fy27Vals">
            <div class="fy-name">FY2027E · {{ fy27IsNonGaap ? '扣非' : '归母' }}</div>
            <div class="fy-val">{{ fmtRange(fy27Vals) }}<em>亿</em></div>
            <div class="fy-cmp" v-if="fy27Yoy">同比 <span :class="cls(fy27Yoy[1])">{{ pct(fy27Yoy[1]) }}</span><span class="dim">(基准档)</span></div>
          </div>
        </div>

        <div class="proj-detail">
          <div v-if="target.model.q4.mode === '增量桥'" class="dim">
            分部毛利变动(亿): <span v-for="s in target.model.q4.segments" :key="s.product" class="chip">
              {{ s.product.slice(0,12) }} {{ s.delta_yi ? s.delta_yi.join('/') : (s.vol_note||'').slice(0,20) }}</span>
            <span v-if="target.model.q4.impairment_yi" class="chip bad">减值 {{ target.model.q4.impairment_yi.join('/') }}亿</span>
          </div>
          <div v-else class="dim">
            <span v-for="s in target.model.q4.segments" :key="s.product" class="chip">
              {{ s.product.slice(0,12) }} GM {{ s.q3_gm_pct }}%→{{ s.q4_gm_pct.join('/') }}%</span>
            <span class="chip bad">非经:补偿 {{ (target.model.q4.non_op||{}).compensation_yi || [] }}亿 + 理财 {{ (target.model.q4.non_op||{}).wealth_yi || [] }}亿</span>
          </div>
          <p v-if="target.model.q4.note" class="dim">{{ target.model.q4.note }}</p>
          <p v-if="target.model.q4.opex_note" class="dim">{{ target.model.q4.opex_note }}</p>
          <ul v-if="target.model.assumptions && target.model.assumptions.length" class="watch compact">
            <li v-for="a in target.model.assumptions" :key="a.name">{{ a.name }}: {{ a.contrib_yi.join('/') }} 亿 —— {{ a.driver }}</li>
          </ul>
          <p v-if="target.model.fy27 && target.model.fy27.crosscheck" class="dim">交叉校验: {{ target.model.fy27.crosscheck }}</p>
          <p v-if="target.model.fy27 && target.model.fy27.crosscheck_fy26" class="dim">FY26 对照: {{ target.model.fy27.crosscheck_fy26 }}</p>
          <p v-if="target.model.fy27 && target.model.fy27.note" class="dim">{{ target.model.fy27.note }}</p>
        </div>
      </section>

      <!-- 分部表(桌面) / 序列卡(移动:7列表格在 375px 必然横向溢出) -->
      <section v-for="seg in target.segments" :key="seg.product" class="card">
        <h3>{{ seg.product }}</h3>
        <div v-if="seg.note" class="dim note-line">{{ seg.note }}</div>
        <div v-if="isMobile" class="mrows">
          <template v-for="tbl in segTables(seg)" :key="tbl.kind">
            <div class="m-tbl-title" :class="tbl.kind">{{ tbl.title }}</div>
            <button v-for="p in tbl.rows" :key="tbl.kind + p.code" type="button"
                    class="mrow" :class="{ on: curCode === p.code }" @click="pickPrice(p)">
              <span class="mrow-head">
                <b class="mrow-name">{{ p.name }}</b>
                <span class="mrow-latest">{{ num(p.stats.latest, p.unit === '元/克' ? 1 : 0) }} <i class="dim">{{ p.unit }}</i></span>
              </span>
              <span class="mrow-sub">最新 {{ p.stats.latest_date }}<template v-if="p.freq === 'week'"> · 周频</template> · QTD {{ num(p.stats.qtd, 0) }} · 上季 {{ num(p.stats.prev_q, 0) }} · 环比 <b :class="cls(p.stats.qoq)">{{ pct(p.stats.qoq) }}</b></span>
            </button>
          </template>
        </div>
        <template v-else>
        <table class="grid" v-if="seg.prices.length">
          <thead>
            <tr><th class="stick">价格序列</th><th>单位</th><th>最新</th><th>QTD均</th><th>上季均</th><th>环比</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in seg.prices" :key="p.code" :class="{ on: curCode === p.code }" @click="pickPrice(p)">
              <td class="stick">{{ p.name }}</td>
              <td>{{ p.unit }}</td>
              <td>{{ num(p.stats.latest, p.unit === '元/克' ? 1 : 0) }} <i class="dim">({{ p.stats.latest_date }}<template v-if="p.freq === 'week'">·周频</template>)</i></td>
              <td>{{ num(p.stats.qtd, 0) }} <i class="dim">{{ p.stats.quarter }}</i></td>
              <td>{{ num(p.stats.prev_q, 0) }}</td>
              <td :class="cls(p.stats.qoq)">{{ pct(p.stats.qoq) }}</td>
              <td class="dim">看图→</td>
            </tr>
          </tbody>
        </table>
        <table class="grid cost" v-if="seg.costs && seg.costs.length">
          <thead>
            <tr><th class="stick">成本序列(原料/能源)</th><th>单位</th><th>最新</th><th>QTD均</th><th>上季均</th><th>环比</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in seg.costs" :key="'c-' + p.code" :class="{ on: curCode === p.code }" @click="pickPrice(p)">
              <td class="stick">{{ p.name }}</td>
              <td>{{ p.unit }}</td>
              <td>{{ num(p.stats.latest, 0) }} <i class="dim">({{ p.stats.latest_date }})</i></td>
              <td>{{ num(p.stats.qtd, 0) }}</td>
              <td>{{ num(p.stats.prev_q, 0) }}</td>
              <td :class="cls(p.stats.qoq)">{{ pct(p.stats.qoq) }}</td>
              <td class="dim">看图→</td>
            </tr>
          </tbody>
        </table>
        <table class="grid spread" v-if="seg.spreads && seg.spreads.length">
          <thead>
            <tr><th class="stick">价差(产品-原料,简单差)</th><th>最新</th><th>QTD均</th><th>上季均</th><th>环比</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="p in seg.spreads" :key="'s-' + p.code" :class="{ on: curCode === p.code }" @click="pickPrice(p)">
              <td class="stick">{{ p.name }}</td>
              <td>{{ num(p.stats.latest, 0) }}</td>
              <td>{{ num(p.stats.qtd, 0) }} <i class="dim">{{ p.stats.quarter }}</i></td>
              <td>{{ num(p.stats.prev_q, 0) }}</td>
              <td :class="cls(p.stats.qoq)">{{ pct(p.stats.qoq) }}</td>
              <td class="dim">看图→</td>
            </tr>
          </tbody>
        </table>
        </template>
        <div class="meta">
          <span v-if="seg.unit_margin != null" class="chip v" title="QTD均价 − 最新披露单位成本(单位一致才可减;成本披露滞后一个报告期)">
            单位毛利 {{ seg.unit_margin }} {{ (seg.unit_cost.latest.unit || '').replace('元/','') }}<i class="dim">({{ seg.unit_cost.latest.period }}成本)</i>
          </span>
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
/* 移动端横滑容器(tabs)用负 margin 拉满屏宽,取整会把页面撑出 1px 横向滚动;
   clip 单轴裁切(可与另一轴 visible 组合),横滑容器内部滚动不受影响 */
@media (max-width: 600px) { .fc { overflow-x: clip; } }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h3 { margin: 0 0 8px; font-size: 15px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); font-size: 12px; }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }
.note-line { margin: 4px 0 8px; line-height: 1.6; }
.thesis { font-size: 14px; font-weight: 600; line-height: 1.7; }
.tabs { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.tab { font-size: 13px; padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt, #374151); white-space: nowrap; }
.tab.on { border-color: var(--accent, #2d6cdf); color: var(--accent, #2d6cdf); background: #eff6ff; font-weight: 600; }
.tab .code { color: var(--sub, #9ca3af); font-size: 12px; margin-left: 4px; font-weight: normal; }
/* 口径说明独立成行(原在 tabs 行内右侧;移动端 tabs 单行横滑后塞不下) */
.head-sub { margin: 0 2px 12px; text-align: right; }
.guide { margin-top: 10px; }
.guide-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.guide-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
.g-item { border: 1px solid var(--line, #e5e9f2); border-radius: 9px; padding: 8px 10px; }
.g-name { font-size: 12.5px; font-weight: 600; }
.g-note { font-style: normal; font-weight: normal; color: var(--sub); font-size: 11px; margin-left: 4px; }
.g-bar { height: 6px; border-radius: 3px; background: var(--line-soft, #e5e9f2); overflow: hidden; margin: 6px 0 4px; }
.g-bar b { display: block; height: 100%; background: var(--accent, #2d6cdf); opacity: .6; }
.g-bar.pending { background: repeating-linear-gradient(45deg, var(--line-soft, #e5e9f2) 0 6px, transparent 6px 12px); }
.g-wait { font-size: 11px; color: var(--muted, #8a93a6); }
.g-item.fact .g-vals { justify-content: flex-start; gap: 8px; }
.g-vals { display: flex; justify-content: space-between; font-size: 12px; font-variant-numeric: tabular-nums; }
table.grid { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0; }
table.grid th, table.grid td { padding: 6px 8px; border-bottom: 1px solid var(--line-soft, #eef1f7); text-align: right; white-space: nowrap; }
table.grid th { color: var(--sub); font-weight: 500; background: var(--card); }
table.grid td.stick, table.grid th.stick { text-align: left; position: sticky; left: 0; background: var(--card); }
table.grid.cost tbody tr td.stick { color: #92400e; }
table.grid.spread tbody tr td.stick { color: #1e40af; font-weight: 600; }
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

/* A/H 市场徽标与口径条(H股琥珀/A股蓝/A+H紫,标准化区分) */
.mkt { display: inline-block; font-size: 10px; font-weight: 700; line-height: 1; padding: 3px 5px; border-radius: 4px; margin-right: 6px; vertical-align: 1px; font-style: normal; }
.mkt.hk { background: #fef3c7; color: #b45309; border: 1px solid #fcd34d; }
.mkt.a { background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }
.mkt.ah { background: #f5f3ff; color: #6d28d9; border: 1px solid #ddd6fe; }
.mkt-strip { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--sub, #6b7280); margin-bottom: 8px; }
.mkt-strip .mkt { margin-right: 0; font-size: 11px; padding: 3px 6px; }
.tip { margin: 6px 2px 0; }
.proj-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.proj-head h3 { margin: 0; font-size: 15px; }
.proj-clock { margin-left: auto; }
/* 精确度自评徽标:高=绿/中高=蓝/中=琥珀/低=红灰,hover title 展示依据与疑点 */
.conf { font-size: 13px; font-weight: 700; padding: 2px 9px; border-radius: 999px; white-space: nowrap; font-variant-numeric: tabular-nums; cursor: help; }
.conf em { font-style: normal; font-size: 11px; font-weight: normal; opacity: .75; }
.conf-高 { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.conf-中高 { background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }
.conf-中 { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.conf-低 { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.scn-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 10px; }
.scn-card { border: 1px solid var(--line, #e5e9f2); border-radius: 12px; padding: 12px 10px 10px; text-align: center; }
.scn-card.base { border-color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe); box-shadow: 0 2px 10px rgba(45,108,223,.12); }
.scn-card.low { border-color: rgba(22,163,74,.4); }
.scn-card.high { border-color: rgba(220,38,38,.4); }
.scn-name { font-size: 12px; color: var(--sub, #6b7280); letter-spacing: 3px; }
.scn-val { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; line-height: 1.15; }
.scn-val em { font-style: normal; font-size: 13px; color: var(--sub); margin-left: 2px; font-weight: normal; }
.scn-cap { font-size: 11px; color: var(--sub); }
.scn-cmp { display: flex; justify-content: center; gap: 14px; font-size: 12.5px; margin-top: 6px; font-variant-numeric: tabular-nums; }
.scn-extra { font-size: 11.5px; color: var(--sub); margin-top: 5px; display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.scn-note { margin: 8px 2px 0; }
.fy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; margin-top: 12px; }
.fy-item { border: 1px dashed var(--accent-line, #c4d7f6); border-radius: 10px; padding: 9px 12px; }
.fy-name { font-size: 12px; color: var(--sub); }
.fy-val { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 2px; }
.fy-val em { font-style: normal; font-size: 12px; color: var(--sub); font-weight: normal; }
.fy-cmp { font-size: 12px; }
.proj-detail { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line, #e5e9f2); }

/* —— 移动端:三张分部表合成序列卡(7列 nowrap 表格在 375px 宽 548px+,撑破卡片) —— */
.m-tbl-title { font-size: 11.5px; color: var(--sub, #6b7280); margin: 10px 0 5px; font-weight: 600; }
.m-tbl-title:first-child { margin-top: 2px; }
.m-tbl-title.cost { color: #92400e; }      /* 与桌面成本表 stick 列同色 */
.m-tbl-title.spread { color: #1e40af; }    /* 与桌面价差表 stick 列同色 */
.mrow { display: flex; flex-direction: column; gap: 3px; width: 100%; text-align: left;
  border: 1px solid var(--line, #e5e9f2); border-radius: 10px; background: var(--card, #fff);
  padding: 9px 12px; margin-bottom: 6px; cursor: pointer; font: inherit; color: inherit; }
.mrow.on { background: var(--accent-soft, #eaf1fe); border-color: var(--accent, #2d6cdf); }
.mrow-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.mrow-name { font-size: 13px; font-weight: 600; min-width: 0; }
.mrow-latest { font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mrow-latest .dim { font-style: normal; }
.mrow-sub { font-size: 11.5px; color: var(--sub, #6b7280); font-variant-numeric: tabular-nums; line-height: 1.5; }
.mrow-sub b { font-weight: 600; }

/* 断点与全站 MOBILE_QUERY(600px)对齐;此前 640 是本页孤例 */
@media (max-width: 600px) {
  .card { padding: 12px 11px; }
  /* 标的 tabs 单行横滑(五个标的换行堆两层错落);口径说明整行左对齐 */
  .tabs { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch; margin-inline: -11px; padding-inline: 11px; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { flex: none; min-height: 36px; }
  .head-sub { text-align: left; margin: 0 1px 10px; }
  .scn-cards { grid-template-columns: 1fr; }
  .scn-card.base { order: -1; }
  .scn-val { font-size: 26px; }
  /* 窄屏行内元素换行后加行距,消除视觉重叠感(环比/同比、营收/毛利/扣非两行) */
  .scn-cmp { gap: 6px 10px; flex-wrap: wrap; }
  .scn-extra { row-gap: 3px; }
  .guide-grid { grid-template-columns: 1fr; }
  .fy-grid { grid-template-columns: 1fr; }
  .mkt-strip { align-items: flex-start; }
}
.watch { margin: 4px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.9; }
.watch.compact { font-size: 12px; line-height: 1.7; }
.proj-block { margin-top: 12px; }
.proj-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.fy-row { display: flex; gap: 22px; flex-wrap: wrap; font-size: 13.5px; }
.fy-row b { font-variant-numeric: tabular-nums; }
tr.base td { background: var(--accent-soft, #eaf1fe); }
</style>
