<script setup>
/* 证券详情:数据经 /api 获取,渲染整体复用 legacy 链(stockLegacy.js 原样移植)
 * 容器 id 与原页面一致:renderDetail(d) 填充 #stock-detail-body,show() 切换 4 块
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get } from '../api/client'
import { getDetail } from '../lib/detailCache'
import { ensureEcharts } from '../lib/echartsLoader'
import '../assets/stock.css'

// 投研库入口卡(v2):该代码有深度研报时,在 legacy 详情上方给一张结论卡。
// 覆盖数据来自 /api/research/list(9 份级别的小请求,失败静默——研报是增强不是依赖)
const researchCard = ref(null)
async function loadResearch(code) {
  researchCard.value = null
  try {
    const rs = await get('/research/list')
    researchCard.value = rs.find((r) => r.code === code) || null
  } catch (e) { /* 列表接口异常时详情页照常 */ }
}

// echarts 不进关键路径：详情的图表全是懒模块（滚动到才建），legacy 的 charts 构建器
// 经 window.__ensureEcharts 拿到 Promise 后再 renderCharts。这里挂载即后台预热，
// 与首屏评分卡的阅读时间重叠——1MB 的 echarts 下载解析不再挡「点进详情」
window.__ensureEcharts = ensureEcharts
ensureEcharts()

const route = useRoute()
const router = useRouter()
const error = ref('')
// 详情加载中：数据包已瘦身（季报裁剪+gzip≈27KB）+ 列表悬浮/点按预取命中时零等待，
// 剩余等待主要是手机端的解析与渲染——保留可见状态兜底
const loading = ref(false)
let legacy = null

// 与列表页 loadSeq 同款守卫：watch(code) 触发新 render 后，晚到的旧响应直接丢弃，
// 否则快速切换股票时旧公司的详情会盖掉新公司。alive 兜另一头——卸载后迟到的
// 响应不渲染也不碰模块级 state（stockLegacy 是单例，卸载时 legacy 可能还没 import 完）。
let renderSeq = 0
let alive = true

async function ensureLegacy() {
  if (!legacy) legacy = await import('../lib/stockLegacy.js')
  return legacy
}

async function render(code) {
  const seq = ++renderSeq
  error.value = ''
  loading.value = true
  const el = document.getElementById('stock-detail-body')
  if (el) el.innerHTML = ''
  try {
    const d = await getDetail(code)
    if (!alive || seq !== renderSeq) return
    // 原页面事件层结构:renderEvents 消费 {events, holders},挂在 _events
    d._events = d.events || null
    // 主包季报裁到最近 12 期（移动端瘦身）：报表标签页首次点开时懒取完整历史，
    // 拉回来的全量三表就地并回这份数据（期次下拉回得去老季度）
    d._loadStatements = async () => {
      const full = await get(`/securities/${encodeURIComponent(code)}/statements`)
      if (alive && seq === renderSeq) {
        d.income = full.income; d.balance = full.balance; d.cashflow = full.cashflow
      }
      return full
    }
    // 主营构成懒取：zygc 已移出首开包，模块滚动到才拉
    d._loadZygc = async () => await get(`/securities/${encodeURIComponent(code)}/zygc`)
    const L = await ensureLegacy()
    if (!alive || seq !== renderSeq) return
    // Wind 事件覆盖层接管:API scores.wind 即原 events/index.json byCode 条目全量
    // (⑥⑦优化脚注 + ⑨事件总览芯片读 state.eventOverlay[d.code])
    if (d.scores && d.scores.wind) {
      L.state.eventOverlay = { [d.code]: d.scores.wind }
    }
    L.state.overlayLoaded = true
    await nextTick()
    if (!alive || seq !== renderSeq) return
    L.renderDetail(d)
    loading.value = false
  } catch (e) {
    if (alive && seq === renderSeq) {
      error.value = e.message?.includes('404') ? `未找到证券 ${code}` : `加载失败：${e.message}`
    }
  } finally {
    if (seq === renderSeq) loading.value = false
  }
}

onMounted(() => { render(route.params.code); loadResearch(route.params.code) })
watch(() => route.params.code, (c) => { if (c) { render(c); loadResearch(c) } })
onBeforeUnmount(() => {
  alive = false
  renderSeq++ // 使在途请求全部失效
  if (legacy) {
    legacy.unbindResize()
    legacy.state.charts.forEach((c) => { try { c.dispose() } catch (e) { /* 已释放 */ } })
    legacy.state.charts = []
  }
})
</script>

<template>
  <div class="detail-root">
    <div class="detail-bar">
      <button type="button" class="back-btn" @click="router.back()">← 返回列表</button>
    </div>
    <div v-if="error" class="error">{{ error }}</div>
    <!-- 研报入口卡:结论/区间/摘要,点击进阅读器 -->
    <router-link v-if="researchCard" :to="'/research/' + researchCard.slug" class="res-card">
      <span class="rc-badge" :class="'rc-' + (researchCard.conclusion === '回避' ? 'sell' : researchCard.conclusion === '观察' ? 'hold' : 'buy')">{{ researchCard.conclusion }}</span>
      <span class="rc-title">深度研报:{{ researchCard.title }}</span>
      <span class="rc-val" v-if="researchCard.val_low != null">估值 {{ researchCard.val_low }}~{{ researchCard.val_high }} · 基准价 {{ researchCard.price_ref }}</span>
      <span class="rc-go">阅读全文 →</span>
    </router-link>
    <div v-if="loading && !error" class="detail-loading">加载中…（首次加载含全部分析模块，约需数秒）</div>
    <!-- legacy show() 切换的四块容器(id 与原页面一致)；loading 由 Vue 接管 v-show -->
    <div id="stock-loading" v-show="loading">加载中…</div>
    <div id="stock-error" class="error" style="display:none"></div>
    <div id="stock-list" style="display:none"></div>
    <div id="stock-detail" style="display:none">
      <div id="stock-detail-body"></div>
    </div>
  </div>
</template>

<style scoped>
.detail-bar { margin-bottom: 12px; }
.back-btn {
  padding: 6px 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card);
  cursor: pointer;
  font-size: 14px;
  color: var(--txt);
}
.back-btn:hover { border-color: var(--accent); color: var(--accent); }

/* 手机端放大返回按钮的点击区；桌面保持原尺寸不变 */
@media (max-width: 600px) {
  .back-btn { padding: 8px 16px; font-size: 15px; min-height: 36px; }
}

.detail-loading {
  padding: 10px 14px;
  font-size: 13px;
  color: #3f639c;
  background: #f3f7fd;
  border-left: 3px solid #5585d4;
  border-radius: 0 8px 8px 0;
}

/* 研报入口卡:横条链接卡,与 legacy 评分卡同视觉层 */
.res-card { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  background: #fff; border: 1px solid #dbe7f5; border-left: 3px solid #2563eb;
  border-radius: 8px; padding: 10px 16px; margin-bottom: 12px;
  text-decoration: none; box-shadow: 0 1px 3px rgba(16,24,40,0.05); }
.res-card:hover { border-color: #2563eb; }
.rc-badge { font-size: 12px; font-weight: 600; border-radius: 999px; padding: 1px 10px; flex-shrink: 0; }
.rc-buy { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
.rc-sell { background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }
.rc-hold { background: #f9fafb; color: #6b7280; border: 1px solid #e5e7eb; }
.rc-title { color: #1f2937; font-size: 14px; font-weight: 600; }
.rc-val { color: #1d4ed8; font-size: 13px; }
.rc-go { color: #2563eb; font-size: 13px; margin-left: auto; }
</style>

<!-- 详情页版式整理（2026-09）。legacy 注入的 DOM 不带 scoped 属性，故用非 scoped 块、
     选择器全部挂在 .detail-root 下防泄漏；样式本体在 assets/stock.css，这里只做覆盖。 -->
<style>
/* 限宽居中：宽屏下 26 列快照、7 列评分表、通栏注释墙全部被拉散是「乱」的主源 */
.detail-root { max-width: 1180px; margin: 0 auto; }

/* 注释/读法段落面板化：通栏灰字墙 → 带左边线的脚注块，与数据表视觉分层 */
.detail-root .score-note {
  background: #f7f9fc;
  border-left: 3px solid #c9d6ea;
  border-radius: 0 8px 8px 0;
  padding: 10px 12px;
  color: #6b7686;
}
.detail-root .score-basis { color: #7c8698; }

/* 评分卡：轻投影提层级，卡与灰底分离开 */
.detail-root .score-card { box-shadow: 0 1px 3px rgba(16, 24, 40, 0.05); }

/* 评分表：斑马纹读行不串位（cmp-group 高亮行与 sticky 首列同步换底） */
.detail-root .stock-compare th,
.detail-root .stock-compare td { padding: 7px 12px; }
.detail-root .stock-compare tbody tr:nth-child(even) td { background: #fafbfd; }
.detail-root .stock-compare tbody tr:nth-child(even) td:first-child { background: #fafbfd; }
.detail-root .stock-compare tbody tr:hover td { background: #f0f5ff; }
.detail-root .stock-compare tbody tr:hover td:first-child { background: #f0f5ff; }

/* 顶部锚点导航芯片收紧：13 枚芯片少占一行 */
.detail-root .va-nav a { font-size: 12px; padding: 3px 10px; }
.detail-root .va-nav { gap: 5px; }
</style>
