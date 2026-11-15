<script setup>
/* 世界局势推演(2026-11-15)。独立页 #/geo:局势温度计+活跃事件+情景树+
 * 敏感度矩阵+历史剧本+应验度追踪。数据三源三种供法——温度计读时合成
 * (/api/world-geo/overview,复用序列 8+跨商品派生 GEO_ 2),人工档直读
 * (events/scenarios/matrix)。纪律:不做复合评分(黄金记分卡判死教训),
 * 温度计是多个单一事实并排;情景概率是主观估计·非模型输出,页面显性标注;
 * 应验度是事实核对不是评分;sector/stock 面不做自动核对(业务结构决定
 * 敏感度),标人工。
 */
import { computed, onBeforeUnmount, onMounted, nextTick, ref, watch } from 'vue'
import { get } from '../api/client'
import { ensureEcharts } from '../lib/echartsLoader'
import { useMediaQuery } from '../lib/useMediaQuery'

const isMobile = useMediaQuery('(max-width: 640px)')
const ov = ref(null)      // /overview 温度计
const evs = ref(null)     // /events 事件+应验度
const scn = ref(null)     // /scenarios 情景树
const mtx = ref(null)     // /matrix 矩阵+剧本
const error = ref('')
const loading = ref(false)

const mEl = ref(null)     // 热力图容器
let mChart = null
let echarts = null

// ---- 展示格式化(与 CommodityView 同款) ----
const num = (v, d = 2) => (v == null ? '-' : (+v).toFixed(d))
const sign = (v, d = 1) => (v == null ? '-' : (v >= 0 ? '+' : '') + (+v).toFixed(d))
const cls = (v) => (v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '')

// 方向徽章:direction 是对资产「价格」的方向(A股语义:利多红/利空绿)
const DIR = {
  bullish: { t: '↑ 利多', c: 'dir-up' },
  bearish: { t: '↓ 利空', c: 'dir-down' },
  neutral: { t: '→ 中性', c: 'dir-flat' },
}
// 应验度徽章:事实核对三态+未到期/人工/缺数(中性色,不用涨跌色)
const VERDICT = {
  confirmed: { t: '✓ 应验', c: 'v-ok' },
  refuted: { t: '✗ 证伪', c: 'v-bad' },
  flat: { t: '→ 平', c: 'v-flat' },
  pending: { t: '⏳ 未到期', c: 'v-wait' },
  manual: { t: '人工核对', c: 'v-wait' },
  no_data: { t: '缺数', c: 'v-wait' },
  insufficient: { t: '数据不足', c: 'v-wait' },
}
function vBadge(a) {
  const v = a.verify || {}
  const base = VERDICT[v.status] || VERDICT.manual
  if (v.status === 'done') {
    return { ...base, tip: `窗口 ${v.window_start}~${v.window_end} 实际 ${sign(v.chg_pct)}%` }
  }
  return { ...base, tip: v.note || '' }
}

// 形态对照卡:典型温度计组合→局面判别的静态人工文案(不做算法判别——
// 组合判别留给人,这里只给「指纹手册」)
const PATTERNS = [
  { name: '地缘避险局', cond: 'GPR 高分位 + 金油比高分位 + 铜金比走低',
    read: '地缘冲突定价中:油含风险溢价、金含避险溢价、铜(需求)承压。对照剧本:俄乌/霍尔木兹。关注溢价回吐的顺序(保费→运价→油价)与停火分支。' },
  { name: '流动性恐慌局', cond: '美元↑ 与 金↑ 同涨 + VIX 急升',
    read: '美元荒:一切资产被抛换现金,金先跌后涨(两段式)。对照剧本:2020-03/2023-03。金融稳定事件的指纹是利率与铜金比先动,不是油。' },
  { name: '衰退定价局', cond: '铜金比历史低位 + 金油比偏高 + 实际利率下行',
    read: '增长资产被抛、避险与能源被抢。铜金比跳水是经典衰退指纹(2023-03)。此局商品普跌但金独强。' },
  { name: '海运紧张局', cond: '布伦特-WTI 走阔 + 油运运价类代理走高',
    read: '制裁/航线风险集中在美国以外海运市场。布伦特-WTI 价差是「全球 vs 美国」紧张度的直接比价,配合航运通道类事件读。' },
  { name: '通胀交易局', cond: '油铜比走高(油先动) + 实际利率上行',
    read: '供给冲击主导:油涨铜稳=通胀担忧;油稳铜跌=需求衰退。油铜比走高的方向语义看分母谁先动。' },
  { name: '局势平静期', cond: 'GPR 中低位 + 全部比价中位 + VIX < 20',
    read: '溢价层归零,商品回归各自供需定价——看各商品模块的量价库存数据(成本/库存/仓单),世界局势维度暂不给边际信息。' },
]

// ---- 事件分组:active 在前,resolved/结构性靠后 ----
const activeEvents = computed(() => (evs.value?.events || []).filter((e) => e.status === 'active'))
const structuralEvents = computed(() =>
  (evs.value?.events || []).filter((e) => e.status === 'active' && e.is_structural))
const activeNormal = computed(() => activeEvents.value.filter((e) => !e.is_structural))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [o, e, s, m] = await Promise.all([
      get('/world-geo/overview'), get('/world-geo/events'),
      get('/world-geo/scenarios'), get('/world-geo/matrix'),
    ])
    ov.value = o; evs.value = e; scn.value = s; mtx.value = m
    await nextTick()
    renderMatrix()
  } catch (e2) {
    error.value = String(e2.message || e2)
  } finally {
    loading.value = false
  }
}

// ---- 敏感度矩阵热力图:value=d*s ∈ [-3,3],红=利多(A股语义) 绿=利空 ----
function renderMatrix() {
  if (!mEl.value || !echarts || !mtx.value) return
  if (!mChart) mChart = echarts.init(mEl.value)
  const m = isMobile.value
  const cats = mtx.value.categories || []
  const rows = mtx.value.rows || []
  const data = []
  rows.forEach((r, ri) => {
    (r.cells || []).forEach((cell, ci) => {
      data.push([ci, ri, (cell.d || 0) * (cell.s || 0)])
    })
  })
  // 移动端固定画布宽放进横向滚动容器,桌面自适应
  if (m && mEl.value) mEl.value.style.width = '780px'
  if (!m && mEl.value) mEl.value.style.width = '100%'
  mChart.resize()
  mChart.setOption({
    textStyle: { fontFamily: 'inherit' },
    tooltip: {
      confine: true, textStyle: { fontSize: m ? 10 : 12 },
      formatter: (p) => {
        const r = rows[p.value[1]], c = (r.cells || [])[p.value[0]] || {}
        const dTxt = c.d > 0 ? '利多' : c.d < 0 ? '利空' : '中性/不适用'
        return `<b>${r.name} × ${cats[p.value[0]]}</b><br/>${dTxt} · 强度 ${c.s || 0}<br/>${c.note || ''}`
      },
    },
    grid: { left: m ? 96 : 120, right: 14, top: 8, bottom: m ? 56 : 60 },
    xAxis: { type: 'category', data: cats,
             axisLabel: { fontSize: m ? 9 : 11, rotate: m ? 45 : 30, interval: 0 },
             splitArea: { show: true } },
    yAxis: { type: 'category', data: rows.map((r) => r.name),
             axisLabel: { fontSize: m ? 9 : 11 }, splitArea: { show: true } },
    visualMap: {
      min: -3, max: 3, calculable: false, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: m ? 10 : 14, itemHeight: m ? 60 : 90,
      text: ['利多', '利空'], textStyle: { fontSize: m ? 9 : 11 },
      inRange: { color: ['#16a34a', '#eef2f7', '#dc2626'] },
    },
    series: [{
      type: 'heatmap', data,
      label: { show: false },
      itemStyle: { borderColor: '#fff', borderWidth: m ? 1 : 2 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,.3)' } },
    }],
  }, true)
}

function onResize() { if (mChart) mChart.resize() }
watch(isMobile, () => nextTick(renderMatrix))

onMounted(async () => {
  echarts = await ensureEcharts()
  await load()
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (mChart) mChart.dispose()
  mChart = null
})
</script>

<template>
  <div class="wgeo" :class="{ m: isMobile }">
    <div v-if="error" class="err">加载失败:{{ error }}</div>
    <div v-else-if="loading" class="empty">数据加载中…</div>

    <template v-if="ov">
      <!-- 页头:纪律声明 -->
      <section class="card head">
        <h2>世界局势推演</h2>
        <p class="sub">
          局势温度计 × 活跃事件 × 情景树 × 敏感度矩阵 × 历史剧本 × 应验度追踪。
          研究看板,非投资建议;情景概率是<b>主观估计·非模型输出</b>;不做复合评分
          (温度计是多个单一事实并排,组合判别留给人);应验度是事后事实核对不是预测评分。
        </p>
      </section>

      <!-- ① 温度计:10 卡 -->
      <section class="card">
        <div class="title-row">
          <h3>① 局势温度计</h3>
          <span class="sub">数据截至 {{ ov.updated_at }} · 读时合成(复用序列 8 + 跨商品派生 2)</span>
        </div>
        <div class="thermo-grid">
          <div v-for="t in ov.thermo" :key="t.code" class="t-card"
               :class="{ dead: t.stats.stale }">
            <div class="t-head">
              <span class="t-label">{{ t.label }}</span>
              <span class="t-freq">{{ t.freq === 'month' ? '月' : '日' }}</span>
              <span v-if="t.stats.stale" class="t-stale">陈旧{{ t.stats.stale_days }}天</span>
            </div>
            <div class="t-val">{{ num(t.stats.latest) }}<em>{{ t.unit }}</em></div>
            <div class="t-pcts">
              <span class="pct" :title="'近 52 周分位'">52周 {{ num((t.stats.pct_52w ?? 0) * 100, 0) }}%
                <i class="pbar"><b :style="{ width: ((t.stats.pct_52w ?? 0) * 100).toFixed(0) + '%' }" /></i></span>
              <span class="pct" :title="'全史分位(数据起点 ' + (t.stats.begin || '-') + ')'">全史 {{ num((t.stats.pct_all ?? 0) * 100, 0) }}%
                <i class="pbar alt"><b :style="{ width: ((t.stats.pct_all ?? 0) * 100).toFixed(0) + '%' }" /></i></span>
              <span class="t-chg" :class="cls(t.stats.chg_1m)">1月 {{ sign(t.stats.chg_1m) }}</span>
            </div>
            <div class="t-note">{{ t.note }}</div>
          </div>
        </div>
        <p class="dim tip">📖 {{ ov.method }}</p>
      </section>

      <!-- ② 形态对照:组合指纹手册(静态人工文案) -->
      <section class="card">
        <div class="title-row">
          <h3>② 形态对照手册</h3>
          <span class="sub">典型温度计组合 → 局面判别(人工指纹,不算法判别)</span>
        </div>
        <div class="pattern-grid">
          <div v-for="p in PATTERNS" :key="p.name" class="p-card">
            <div class="p-name">{{ p.name }}</div>
            <div class="p-cond">{{ p.cond }}</div>
            <div class="p-read">{{ p.read }}</div>
          </div>
        </div>
      </section>

      <!-- ③ 活跃事件时间线 -->
      <section class="card">
        <div class="title-row">
          <h3>③ 活跃事件</h3>
          <span class="sub">{{ evs.updated }} 更新 · 方向=对资产价格的方向(航空在停火中是利多) · 强度 1-3</span>
        </div>
        <div v-for="ev in activeNormal" :key="ev.id" class="ev-card">
          <div class="ev-head">
            <span class="ev-cat">{{ ev.category }}</span>
            <b class="ev-title">{{ ev.title }}</b>
            <span class="ev-date">{{ ev.date }}</span>
            <span class="ev-region">{{ ev.region }}</span>
            <span v-if="ev.verify_summary.done" class="ev-vsum"
                  :title="'可自动核对的资产中应验/证伪/平'">
              应验 {{ ev.verify_summary.confirmed }}/{{ ev.verify_summary.done }}
              <template v-if="ev.verify_summary.refuted">· 证伪 {{ ev.verify_summary.refuted }}</template>
            </span>
          </div>
          <p class="ev-sum">{{ ev.summary }}</p>
          <div class="ev-wrap">
            <table class="ev-tbl">
              <thead><tr>
                <th>资产</th><th>方向</th><th title="传导强度 1-3(3=主导变量)">强度</th>
                <th>传导渠道</th><th>时间窗</th><th>应验度</th>
              </tr></thead>
              <tbody>
                <tr v-for="(a, ai) in ev.assets" :key="ai">
                  <td>
                    <span class="a-type" :data-t="a.type">{{ a.type === 'commodity' ? '商品' : a.type === 'fx' ? '汇率' : a.type === 'sector' ? '板块' : '个股' }}</span>
                    {{ a.ref }}
                  </td>
                  <td><span class="dir" :class="DIR[a.direction]?.c">{{ DIR[a.direction]?.t || a.direction }}</span></td>
                  <td>{{ '●'.repeat(a.strength || 0) || '·' }}</td>
                  <td class="a-chan">{{ a.channel }}</td>
                  <td class="a-hz">{{ a.horizon }}</td>
                  <td><span class="v-badge" :class="vBadge(a).c" :title="vBadge(a).tip">{{ vBadge(a).t }}</span>
                    <span v-if="a.verify?.chg_pct != null" class="v-chg">{{ sign(a.verify.chg_pct) }}%</span></td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="ev-exp"><b>预期:</b>{{ ev.expectation?.notes }}(核对窗 {{ ev.expectation?.window_days }} 天)</p>
          <p v-if="ev.evidence?.length" class="ev-evi"><b>依据:</b>{{ ev.evidence.join(';') }}</p>
          <p v-if="ev.playbook_ref" class="ev-pb">📜 历史剧本:{{ ev.playbook_ref }}(见 ⑤)</p>
        </div>

        <!-- 结构性趋势单独一组(无到期概念) -->
        <div v-if="structuralEvents.length" class="ev-struct">
          <h4>结构性趋势(无到期日,不进应验度核对)</h4>
          <div v-for="ev in structuralEvents" :key="ev.id" class="ev-card">
            <div class="ev-head">
              <span class="ev-cat">{{ ev.category }}</span>
              <b class="ev-title">{{ ev.title }}</b>
              <span class="ev-date">{{ ev.date }} 起</span>
            </div>
            <p class="ev-sum">{{ ev.summary }}</p>
            <div class="ev-wrap">
              <table class="ev-tbl">
                <thead><tr><th>资产</th><th>方向</th><th>强度</th><th>传导渠道</th><th>性质</th></tr></thead>
                <tbody>
                  <tr v-for="(a, ai) in ev.assets" :key="ai">
                    <td><span class="a-type" data-t="commodity">商品</span>{{ a.ref }}</td>
                    <td><span class="dir" :class="DIR[a.direction]?.c">{{ DIR[a.direction]?.t }}</span></td>
                    <td>{{ '●'.repeat(a.strength || 0) || '·' }}</td>
                    <td class="a-chan">{{ a.channel }}</td>
                    <td class="a-hz">structural</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <p class="dim tip">📖 {{ evs.verify_note }}{{ evs.note }}</p>
      </section>

      <!-- ④ 情景树 -->
      <section v-for="set in scn.sets" :key="set.set_id" class="card">
        <div class="title-row">
          <h3>④ 情景树 · {{ set.title }}</h3>
          <span class="sub">{{ set.updated }} 更新 · 概率是主观估计·非模型输出</span>
        </div>
        <p class="guide">{{ set.base_note }}</p>
        <div class="scn-grid">
          <div v-for="s in set.scenarios" :key="s.name" class="scn-card"
               :class="{ main: s.probability >= 0.5 }">
            <div class="scn-prob">{{ (s.probability * 100).toFixed(0) }}<em>%</em></div>
            <div class="scn-name">{{ s.name }}</div>
            <div class="scn-note">{{ s.prob_note }}</div>
            <p class="scn-nar">{{ s.narrative }}</p>
            <div v-for="(a, ai) in s.assets" :key="ai" class="scn-asset">
              <span class="dir" :class="DIR[a.direction]?.c">{{ DIR[a.direction]?.t }}</span>
              <b>{{ a.ref }}</b>
              <span class="scn-mag">{{ a.magnitude }}</span>
              <span class="scn-chan">{{ a.channel }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ⑤ 敏感度矩阵热力图 -->
      <section class="card">
        <div class="title-row">
          <h3>⑤ 敏感度矩阵</h3>
          <span class="sub">{{ mtx.updated }} 更新 · 资产×事件类别的方向先验(典型形态,非回测产物)</span>
        </div>
        <div class="mx-scroll">
          <div ref="mEl" class="mx-chart" :style="{ height: isMobile ? '620px' : '640px' }"></div>
        </div>
        <p class="dim tip">📖 {{ mtx.note }}</p>
      </section>

      <!-- ⑥ 历史剧本库 -->
      <section class="card">
        <div class="title-row">
          <h3>⑥ 历史剧本库</h3>
          <span class="sub">新事件按类别匹配剧本,对照传导路径-峰值反应-持续期-消退形态</span>
        </div>
        <details v-for="pb in mtx.playbooks" :key="pb.id" class="pb">
          <summary>
            <span class="pb-date">{{ pb.date }}</span>
            <b>{{ pb.title }}</b>
            <span class="pb-cat">{{ pb.category }}</span>
          </summary>
          <div class="pb-body">
            <p><b>触发:</b>{{ pb.trigger }}</p>
            <ol class="pb-chains">
              <li v-for="(c, ci) in pb.chains" :key="ci">
                <b>{{ c.step }}</b> — {{ c.detail }}
              </li>
            </ol>
            <table class="pb-tbl">
              <thead><tr><th>资产</th><th>峰值反应</th><th>备注</th></tr></thead>
              <tbody>
                <tr v-for="(a, ai) in pb.assets" :key="ai">
                  <td>{{ a.ref }}</td><td>{{ a.peak_move }}</td><td class="pb-note">{{ a.note }}</td>
                </tr>
              </tbody>
            </table>
            <p><b>持续期:</b>{{ pb.duration }} · <b>消退:</b>{{ pb.fade_note }}</p>
            <p class="pb-lesson"><b>💡 方法论:</b>{{ pb.lesson }}</p>
          </div>
        </details>
      </section>

      <p class="dim foot">
        口径:温度计 10 指标中 8 个复用商品模块已入库序列(GPR/VIXCLS/金银比/铜金比/布伦特-WTI/
        美元宽/实际利率/人民币中间价),金油比与油铜比为跨商品派生
        (fetch_worldgeo.py 读兄弟模块 JSON 按日内连接相除,GEO_ 前缀,07:55 殿后任务);
        GEO_OIL_CU 数据起点 2016(新浪外盘 CAD 源),全史分位覆盖不了完整周期,读数看 begin。
        事件/情景/矩阵/剧本为人工结构化档(commodity-worldgeo/data/ 下四个 JSON),
        维护口径见各文件 note。应验度自动核对仅覆盖商品与汇率面(代理序列映射见
        world_geo.py _VERIFY_CODE);sector/stock 面敏感度取决于业务结构(现货 vs 长约/内贸),
        人工核对。免费源边界:温度计全部来自既有免费采集链,本模块零新增网络依赖。
      </p>
    </template>
  </div>
</template>

<style scoped>
.wgeo { max-width: 1100px; margin: 0 auto; }
.card { background: var(--card, #fff); border: 1px solid var(--line, #e5e9f2); border-radius: var(--r-m, 12px);
  padding: 14px 16px; margin-bottom: 14px; box-shadow: var(--shadow-s, 0 1px 2px rgba(20,30,55,.05)); }
.card h2 { margin: 0 0 4px; font-size: 18px; }
.card h3 { margin: 0 0 4px; font-size: 15px; }
.card h4 { margin: 12px 0 8px; font-size: 13px; color: var(--sub, #6b7280); }
.card .sub { font-weight: normal; color: var(--sub, #6b7280); font-size: 12px; margin-left: 8px; }
.title-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; margin-bottom: 12px; }
.head .sub { margin-left: 0; display: block; line-height: 1.8; }
.head .sub b { color: #b45309; }
.guide { font-size: 12.5px; line-height: 1.8; color: var(--txt-2, #4b5563); background: var(--bg-soft, #f8fafd);
  border: 1px dashed var(--line, #e5e9f2); border-radius: 8px; padding: 8px 12px; margin: 0 0 12px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: var(--sub, #9ca3af); font-size: 13px; }
.dim { color: var(--sub, #9ca3af); font-size: 12px; }
.tip { margin: 8px 2px 0; line-height: 1.8; }
.foot { line-height: 1.8; padding: 8px 4px 0; }
.up { color: var(--up, #dc2626); } .down { color: var(--down, #16a34a); }

/* ---- 温度计 ---- */
.thermo-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
.t-card { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 10px 12px;
  background: var(--bg-soft, #f8fafd); }
.t-card.dead { opacity: .72; }
.t-head { display: flex; align-items: center; gap: 5px; }
.t-label { font-size: 13px; font-weight: 600; }
.t-freq { font-size: 10px; color: var(--sub, #9ca3af); border: 1px solid var(--line, #e5e9f2);
  border-radius: 4px; padding: 0 4px; }
.t-stale { font-size: 10px; color: #b91c1c; background: #fef2f2; border-radius: 4px; padding: 0 4px; }
.t-val { font-size: 22px; font-weight: 700; margin: 6px 0 4px; font-variant-numeric: tabular-nums; }
.t-val em { font-style: normal; font-size: 10px; font-weight: normal; color: var(--sub, #9ca3af); margin-left: 3px; }
.t-pcts { display: flex; flex-direction: column; gap: 3px; font-size: 11px;
  color: var(--sub, #6b7280); font-variant-numeric: tabular-nums; }
.pct { display: inline-flex; align-items: center; gap: 5px; }
.pbar { width: 52px; height: 5px; border-radius: 3px; background: var(--line-soft, #e5e9f2); overflow: hidden;
  display: inline-block; }
.pbar b { display: block; height: 100%; background: var(--accent, #2d6cdf); opacity: .55; }
.pbar.alt b { background: #c9a227; }
.t-chg { font-size: 11px; }
.t-note { font-size: 10.5px; color: var(--sub, #6b7280); line-height: 1.5; margin-top: 6px;
  border-top: 1px dashed var(--line, #e5e9f2); padding-top: 5px; }

/* ---- 形态对照 ---- */
.pattern-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.p-card { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 10px 12px; }
.p-name { font-size: 13px; font-weight: 700; color: var(--accent, #2d6cdf); }
.p-cond { font-size: 11px; color: #b45309; background: #fffbeb; border-radius: 4px; padding: 2px 6px;
  display: inline-block; margin: 5px 0; }
.p-read { font-size: 11.5px; color: var(--txt-2, #4b5563); line-height: 1.6; }

/* ---- 事件卡 ---- */
.ev-card { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 12px 14px;
  margin-bottom: 12px; background: var(--card, #fff); }
.ev-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ev-cat { font-size: 10.5px; color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe);
  border-radius: 4px; padding: 1px 6px; white-space: nowrap; }
.ev-title { font-size: 14px; }
.ev-date { font-size: 11px; color: var(--sub, #9ca3af); font-variant-numeric: tabular-nums; }
.ev-region { font-size: 10.5px; color: var(--sub, #9ca3af); border: 1px solid var(--line, #e5e9f2);
  border-radius: 4px; padding: 0 4px; }
.ev-vsum { font-size: 11px; color: #166534; background: #f0fdf4; border: 1px solid #bbf7d0;
  border-radius: 999px; padding: 1px 8px; margin-left: auto; }
.ev-sum { font-size: 12.5px; line-height: 1.7; color: var(--txt-2, #4b5563); margin: 8px 0; }
.ev-wrap { overflow-x: auto; }
.ev-tbl { border-collapse: collapse; width: 100%; font-size: 12px; font-variant-numeric: tabular-nums; }
.ev-tbl th { text-align: left; color: var(--sub, #6b7280); font-weight: 500; font-size: 11px;
  padding: 4px 8px; border-bottom: 1px solid var(--line, #e5e9f2); white-space: nowrap; }
.ev-tbl td { padding: 5px 8px; border-bottom: 1px solid var(--line-soft, #f0f2f7); vertical-align: top; }
.a-type { display: inline-block; font-size: 10px; border: 1px solid var(--line, #e5e9f2);
  border-radius: 4px; padding: 0 4px; margin-right: 5px; color: var(--sub, #6b7280); }
.a-chan { max-width: 330px; white-space: normal; font-size: 11.5px; color: var(--txt-2, #4b5563); }
.a-hz { white-space: nowrap; color: var(--sub, #6b7280); font-size: 11px; }
.dir { font-weight: 600; white-space: nowrap; }
.dir-up { color: var(--up, #dc2626); }
.dir-down { color: var(--down, #16a34a); }
.dir-flat { color: var(--sub, #9ca3af); }
.v-badge { display: inline-block; font-size: 10.5px; border-radius: 999px; padding: 1px 8px;
  border: 1px solid var(--line, #e5e9f2); white-space: nowrap; }
.v-ok { color: #166534; background: #f0fdf4; border-color: #bbf7d0; }
.v-bad { color: #991b1b; background: #fef2f2; border-color: #fecaca; }
.v-flat { color: var(--sub, #6b7280); }
.v-wait { color: var(--sub, #9ca3af); }
.v-chg { margin-left: 5px; font-size: 11px; font-variant-numeric: tabular-nums; }
.ev-exp, .ev-evi, .ev-pb { font-size: 11.5px; color: var(--txt-2, #4b5563); line-height: 1.7; margin: 6px 0 0; }
.ev-pb { color: #92400e; }
.ev-struct h4 { border-top: 1px dashed var(--line, #e5e9f2); padding-top: 10px; }

/* ---- 情景树 ---- */
.scn-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.scn-card { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; padding: 12px 14px; }
.scn-card.main { border-color: var(--accent, #2d6cdf); background: var(--accent-soft, #f4f8ff); }
.scn-prob { font-size: 30px; font-weight: 700; color: var(--accent, #2d6cdf); line-height: 1; }
.scn-prob em { font-style: normal; font-size: 13px; }
.scn-name { font-size: 14px; font-weight: 700; margin-top: 4px; }
.scn-note { font-size: 10px; color: #b45309; margin-top: 2px; }
.scn-nar { font-size: 11.5px; color: var(--txt-2, #4b5563); line-height: 1.6; margin: 8px 0;
  border-top: 1px dashed var(--line, #e5e9f2); padding-top: 8px; }
.scn-asset { font-size: 11.5px; line-height: 1.6; margin-bottom: 7px; }
.scn-asset b { margin: 0 4px; }
.scn-mag { font-variant-numeric: tabular-nums; }
.scn-chan { display: block; color: var(--sub, #6b7280); font-size: 10.5px; }

/* ---- 矩阵 ---- */
.mx-scroll { overflow-x: auto; }
.mx-chart { width: 100%; }

/* ---- 剧本 ---- */
.pb { border: 1px solid var(--line, #e5e9f2); border-radius: 10px; margin-bottom: 8px; }
.pb summary { cursor: pointer; padding: 10px 14px; font-size: 13px; display: flex;
  align-items: center; gap: 8px; flex-wrap: wrap; list-style: none; }
.pb summary::-webkit-details-marker { display: none; }
.pb summary::before { content: '▸'; color: var(--sub, #9ca3af); }
.pb[open] summary::before { content: '▾'; }
.pb-date { font-size: 11px; color: var(--sub, #9ca3af); font-variant-numeric: tabular-nums; }
.pb-cat { font-size: 10.5px; color: var(--accent, #2d6cdf); background: var(--accent-soft, #eaf1fe);
  border-radius: 4px; padding: 1px 6px; margin-left: auto; }
.pb-body { padding: 0 14px 12px; font-size: 12px; line-height: 1.7; color: var(--txt-2, #4b5563); }
.pb-chains { margin: 8px 0; padding-left: 20px; }
.pb-chains li { margin-bottom: 4px; }
.pb-tbl { border-collapse: collapse; width: 100%; font-size: 11.5px; margin: 8px 0;
  font-variant-numeric: tabular-nums; }
.pb-tbl th { text-align: left; color: var(--sub, #6b7280); font-weight: 500; padding: 4px 8px;
  border-bottom: 1px solid var(--line, #e5e9f2); }
.pb-tbl td { padding: 4px 8px; border-bottom: 1px solid var(--line-soft, #f0f2f7); vertical-align: top; }
.pb-note { color: var(--sub, #6b7280); }
.pb-lesson { color: #92400e; background: #fffbeb; border-radius: 6px; padding: 8px 10px; }

/* ---- 移动端(≤640px,.m 类单点维护) ---- */
.m .card { padding: 10px 12px; border-radius: 10px; }
.m .card h2 { font-size: 16px; }
.m .card h3 { font-size: 14px; }
.m .title-row .sub { font-size: 11px; margin-left: 0; width: 100%; }
.m .thermo-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
.m .t-val { font-size: 18px; }
.m .pbar { width: 38px; }
.m .pattern-grid { grid-template-columns: 1fr; }
.m .scn-grid { grid-template-columns: 1fr; }
.m .ev-title { font-size: 13px; }
.m .a-chan { max-width: none; }
.m .ev-tbl { font-size: 11px; }
.m .mx-chart { width: 780px; }
.m .foot { font-size: 11px; }
</style>
