<script setup>
/* 证券详情:数据经 /api 获取,渲染整体复用 legacy 链(stockLegacy.js 原样移植)
 * 容器 id 与原页面一致:renderDetail(d) 填充 #stock-detail-body,show() 切换 4 块
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { get } from '../api/client'
import '../assets/stock.css'

window.echarts = echarts

const route = useRoute()
const router = useRouter()
const error = ref('')
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
    const d = await get(`/securities/${encodeURIComponent(code)}`)
    if (!alive || seq !== renderSeq) return
    // 原页面事件层结构:renderEvents 消费 {events, holders},挂在 _events
    d._events = d.events || null
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
  } catch (e) {
    if (alive && seq === renderSeq) {
      error.value = e.message?.includes('404') ? `未找到证券 ${code}` : `加载失败：${e.message}`
    }
  } finally {
    if (seq === renderSeq) loading.value = false
  }
}

onMounted(() => render(route.params.code))
watch(() => route.params.code, (c) => c && render(c))
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
</style>
