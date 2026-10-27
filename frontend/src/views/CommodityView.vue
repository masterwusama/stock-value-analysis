<script setup>
/* 大宗商品·黄金供需页(P0 数据层):价格矩阵/持仓库存/官方储备/宏观因子四组序列,
 * 指标卡(最新值+近4周变动+52周分位)点击切换主图,支持组内归一化对比。
 * 数据源 /api/commodity/gold(读时合成统计,口径见 app/api/commodity.py 头注)。
 * 定位:研究看板不是投资建议;因子记分卡与供需平衡表是 P2,先看数再谈模型。
 */
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'

const isMobile = useMediaQuery(MOBILE_QUERY)
const data = ref(null)
const error = ref('')
const loading = ref(false)
const curCat = ref('')
const curCode = ref('')      // 当前主图序列;空=组内归一化对比
const range = ref('3y')
const normalize = ref(true)  // 主图模式:组内归一化(基准=区间起点100) vs 单序列绝对值

const chartEl = ref(null)
let chart = null
let echarts = null

const RANGE_DAYS = { '1y': 365, '3y': 365 * 3, '5y': 365 * 5, all: 0 }

onMounted(async () => {
  loading.value = true
  try {
    const d = await get('/commodity/gold')
    data.value = d
    curCat.value = d.cats?.[0] || ''
    curCode.value = d.series?.[0]?.code || ''
    echarts = await ensureEcharts()
    await nextTick()
    render()
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    loading.value = false
  }
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) chart.dispose()
  chart = null
})
function onResize() { if (chart) chart.resize() }

// ---- 展示格式化 ----
const num = (v, d = 2) => (v == null ? '-' : (+v).toFixed(d))
const sign = (v, d = 2) => (v == null ? '-' : (v >= 0 ? '+' : '') + (+v).toFixed(d))
const cls = (v) => (v == null ? '' : v >= 0 ? 'up' : 'down')

const series = computed(() => data.value?.series || [])
const catSeries = computed(() => series.value.filter((s) => s.cat === curCat.value))
const staleList = computed(() => series.value.filter((s) => s.stats?.stale))
const byCode = computed(() => Object.fromEntries(series.value.map((s) => [s.code, s])))
// 头条卡固定取核心序列(跨分类);缺序列时该卡不渲染
const HEAD = [
  { code: 'XAU_USD', note: '伦敦金现货' },
  { code: 'SGE_AU9999', note: '上海金 Au99.99' },
  { code: 'SGE_PREMIUM', note: 'SGE 溢价率(中国需求温度计)' },
  { code: 'GOLD_SILVER', note: '金银比(52周分位见卡)' },
  { code: 'SPDR_TON', note: 'SPDR 持仓·金融需求' },
  { code: 'COMEX_INV', note: 'COMEX 库存·可交割' },
  { code: 'CN_GOLD_WAN', note: '中国官方储备(万盎司,月)' },
  { code: 'DFII10', note: '10Y 实际利率·核心定价因子' },
]
const headCards = computed(() =>
  HEAD.map((h) => ({ ...h, s: byCode.value[h.code] })).filter((x) => x.s?.stats?.latest != null))

function pick(s) { curCode.value = s.code; normalize.value = false }
function pickCat(c) {
  curCat.value = c
  normalize.value = true
  curCode.value = catSeries.value[0]?.code || ''
}

function windowPoints(s) {
  const days = RANGE_DAYS[range.value]
  if (!days) return s.points
  const cut = new Date(Date.now() - days * 86400e3).toISOString().slice(0, 10)
  return s.points.filter((p) => p[0] >= cut)
}

function render() {
  if (!chartEl.value || !echarts) return
  if (!chart) chart = echarts.init(chartEl.value)
  const base = { textStyle: { fontFamily: 'inherit' }, grid: { left: 56, right: 18, top: 34, bottom: 48 } }
  if (normalize.value) {
    const list = catSeries.value.map((s) => ({ s, pts: windowPoints(s) })).filter((x) => x.pts.length)
    if (!list.length) { chart.clear(); return }
    const dates = [...new Set(list.flatMap((x) => x.pts.map((p) => p[0])))].sort()
    const disc = new Map(dates.map((d, i) => [d, i]))
    chart.setOption({
      ...base,
      title: { text: `${curCat.value} · 归一化对比(区间起点=100)`, left: 0, top: 0, textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'axis', confine: true, valueFormatter: (v) => (v == null ? '-' : (+v).toFixed(1)) },
      legend: { top: 6, type: 'scroll', icon: 'roundRect', itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11 } },
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
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
      title: { text: `${s.name}(${s.unit || ''}) · ${s.source || ''}`, left: 0, top: 0, textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'axis', confine: true },
      xAxis: { type: 'category', data: pts.map((p) => p[0]), boundaryGap: false, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 8 }],
      series: [{ name: s.label, type: 'line', showSymbol: false,
                 lineStyle: { width: 1.6, color: '#2d6cdf' }, itemStyle: { color: '#2d6cdf' },
                 areaStyle: { opacity: 0.06, color: '#2d6cdf' },
                 data: pts.map((p) => p[1]) }],
    }, true)
  }
}

watch([curCat, curCode, range, normalize], () => nextTick(render))
</script>

<template>
  <div class="cmd">
    <div v-if="error" class="err">加载失败:{{ error }}<template v-if="loading"> …重试中</template></div>
    <div v-else-if="!data" class="empty">数据加载中…</div>

    <template v-if="data">
      <!-- 新鲜度红条:任一序列超频段阈值(学习 etf 页教训,断链要自己说话) -->
      <p v-if="staleList.length" class="stale-warn">
        ⚠ {{ staleList.length }} 条序列陈旧:{{ staleList.map((s) => s.label).join('、') }}
        ——采集链疑似断档(单序列源故障会降级留旧数据)。检查:
        `python -m collector.run gold` 手动补跑,job 日志看哪个源失败。
      </p>

      <!-- 头卡 -->
      <section class="card">
        <div class="title-row">
          <h3>黄金 · 供需与定价因子看板</h3>
          <span class="sub">数据截至 {{ data.updated_at }} · 免费源(新浪/SGE/东财/FRED/GPR) · 研究看板,非投资建议</span>
        </div>
        <div class="kpis">
          <div v-for="h in headCards" :key="h.code" class="kpi" :class="{ on: curCode === h.code }"
               @click="pick(h.s)">
            <div class="k-label">{{ h.s.label }}<i class="k-note">{{ h.note }}</i></div>
            <div class="k-val">{{ num(h.s.stats.latest, h.code === 'CN_GOLD_WAN' ? 0 : 2) }}<em class="k-unit">{{ h.s.unit }}</em></div>
            <div class="k-row">
              <span :class="cls(h.s.stats.chg_4w)">4周 {{ sign(h.s.stats.chg_4w, h.code === 'CN_GOLD_WAN' ? 0 : 1) }}</span>
              <span class="pct" :title="'52周区间 ' + num(h.s.stats.lo_52w) + ' ~ ' + num(h.s.stats.hi_52w)">
                52周分位 {{ num((h.s.stats.pct_52w ?? 0) * 100, 0) }}%
                <i class="pbar"><b :style="{ width: ((h.s.stats.pct_52w ?? 0) * 100).toFixed(0) + '%' }" /></i>
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- 分类切换 + 图模式 -->
      <div class="toolbar tabs">
        <button v-for="c in data.cats" :key="c" type="button" class="tab"
                :class="{ on: c === curCat }" @click="pickCat(c)">{{ c }}</button>
        <span class="tools">
          <label class="norm"><input type="checkbox" v-model="normalize"> 归一化对比(当前分类)</label>
          <select v-model="range">
            <option value="1y">近 1 年</option>
            <option value="3y">近 3 年</option>
            <option value="5y">近 5 年</option>
            <option value="all">全部</option>
          </select>
        </span>
      </div>

      <section class="card">
        <div ref="chartEl" class="chart" :style="{ height: isMobile ? '300px' : '420px' }"></div>
        <p class="dim tip">点指标卡切换单序列;勾「归一化对比」叠加当前分类全部序列(起点=100,只比相对涨跌)。</p>
      </section>

      <!-- 指标卡矩阵 -->
      <section v-for="c in data.cats" :key="c" class="card">
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
            <div class="i-foot">{{ s.name }} · {{ s.source }}</div>
          </div>
        </div>
      </section>

      <p class="dim foot">
        口径:伦敦金现货替代已收费的 LBMA 定盘价;SPDR 隐含金价=总价值/持仓(实测与 COMEX 价差&lt;0.1%);
        SGE 溢价率=SGE/(伦敦金×中间价折算)-1,负值=内盘折价。CFTC 为商业持仓口径(非商业细项 P1 接 CFTC 官网)。
        供需平衡表(WGC 季度)与因子记分卡属 P2,先看数再谈模型(设计文档:docs/设计-大宗商品供需模块-黄金.md)。
      </p>
    </template>
  </div>
</template>

<style scoped>
.cmd { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h3 { margin: 0; font-size: 15px; }
.card .sub { font-weight: normal; color: var(--sub, #6b7280); font-size: 12px; margin-left: 8px; }
.title-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; margin-bottom: 12px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); font-size: 12px; }
.stale-warn { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px;
  border-radius: 8px; font-size: 13px; line-height: 1.7; margin-bottom: 12px; }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }

.kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
.kpi { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 10px 12px; cursor: pointer;
  background: var(--bg-soft, #f8fafd); transition: border-color .15s; }
.kpi:hover { border-color: var(--accent-line, #c4d7f6); }
.kpi.on { border-color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe); }
.k-label { font-size: 13px; font-weight: 600; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.k-note { font-style: normal; font-weight: normal; color: var(--sub, #9ca3af); font-size: 11px; }
.k-val { font-size: 20px; font-weight: 700; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.k-unit { font-style: normal; font-size: 11px; font-weight: normal; color: var(--sub, #9ca3af); margin-left: 4px; }
.k-row { display: flex; justify-content: space-between; align-items: center; font-size: 12px;
  font-variant-numeric: tabular-nums; gap: 8px; flex-wrap: wrap; }
.pct { color: var(--sub, #6b7280); display: inline-flex; align-items: center; gap: 5px; }
.pbar { width: 46px; height: 5px; border-radius: 3px; background: var(--line-soft, #e5e9f2); overflow: hidden; }
.pbar b { display: block; height: 100%; background: var(--accent, #2d6cdf); opacity: .55; }

.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tabs { flex-wrap: wrap; }
.tab { font-size: 13px; padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line, #e5e7eb);
  background: var(--card, #fff); cursor: pointer; color: var(--txt, #374151); }
.tab.on { border-color: var(--accent, #2d6cdf); color: var(--accent, #2d6cdf); background: #eff6ff; font-weight: 600; }
.tools { margin-left: auto; display: inline-flex; align-items: center; gap: 10px; font-size: 12px; color: var(--sub, #6b7280); }
.tools select { font-size: 12px; padding: 3px 6px; border-radius: 6px; border: 1px solid var(--line, #e5e9f2); }
.norm { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
.chart { width: 100%; }
.tip { margin: 8px 2px 0; }

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
.i-foot { font-size: 10.5px; color: var(--sub, #9ca3af); margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.foot { line-height: 1.8; padding: 0 4px 12px; }

@media (max-width: 640px) {
  .kpis { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .tools { margin-left: 0; }
}
</style>
