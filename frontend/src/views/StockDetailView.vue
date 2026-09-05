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
let legacy = null

async function ensureLegacy() {
  if (!legacy) legacy = await import('../lib/stockLegacy.js')
  return legacy
}

async function render(code) {
  error.value = ''
  const el = document.getElementById('stock-detail-body')
  if (el) el.innerHTML = ''
  try {
    const d = await get(`/securities/${encodeURIComponent(code)}`)
    // 原页面事件层结构:renderEvents 消费 {events, holders},挂在 _events
    d._events = d.events || null
    const L = await ensureLegacy()
    // Wind 事件覆盖层接管:API scores.wind 即原 events/index.json byCode 条目全量
    // (⑥⑦优化脚注 + ⑨事件总览芯片读 state.eventOverlay[d.code])
    if (d.scores && d.scores.wind) {
      L.state.eventOverlay = { [d.code]: d.scores.wind }
    }
    L.state.overlayLoaded = true
    await nextTick()
    L.renderDetail(d)
  } catch (e) {
    error.value = e.message?.includes('404') ? `未找到证券 ${code}` : `加载失败：${e.message}`
  }
}

onMounted(() => render(route.params.code))
watch(() => route.params.code, (c) => c && render(c))
onBeforeUnmount(() => {
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
    <!-- legacy show() 切换的四块容器(id 与原页面一致) -->
    <div id="stock-loading" style="display:none">加载中…</div>
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
