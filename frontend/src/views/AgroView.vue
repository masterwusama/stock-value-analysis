<script setup>
/* 农化制品价格页:DOM 骨架与原 agro-price/index.html 一致,
 * 渲染复用 legacy 链(agroLegacy.js),数据源 /api/agro/products。
 * 2026-10-27 起聚焦农化制品:行业 EDB 五板块(电解铝/航运/轮胎/地产/钢铁)
 * 整体移除(视图/接口/采集链),中农立华周报双入口修复+茚虫威/噁草酮接入。
 */
import { nextTick, onBeforeUnmount, onMounted } from 'vue'
import { ensureEcharts } from '../lib/echartsLoader'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'
import '../assets/agro.css'

const isMobile = useMediaQuery(MOBILE_QUERY)

let mods = null
// 卸载竞态：onMounted 里两个 await import 期间切走路由时，onBeforeUnmount 见到的
// mods 还是 null 会直接 return，随后迟到的 init() 却照常执行——resize 监听挂上后
// 无人摘除，每次进出 /agro 累积一对。alive 挡住迟到 init，teardown 里的 dead 标志
// 再挡住 init 内部在途的 fetch 回调。
let alive = true

onMounted(async () => {
  await nextTick()
  // echarts 后台加载（不阻塞骨架与数据请求）；本页图表主体是农化价格曲线，
  // legacy 渲染层读 window.echarts——ensureEcharts 完成前若已到画图步，等它
  const [agro] = await Promise.all([
    import('../lib/agroLegacy.js'),
    ensureEcharts(),
  ])
  if (!alive) return
  mods = { agro }
  agro.init() // 拉 /api/agro/products 渲染
})

onBeforeUnmount(() => {
  alive = false
  if (!mods) return
  const c = mods.agro.agroState.chart
  if (c) { try { c.dispose() } catch (e) { /* 已释放 */ } mods.agro.agroState.chart = null }
  // 光 dispose 不够：resize 监听还挂在 window 上，路由切走后每次窗口变化都会
  // 去 resize 已释放的实例，反复来回切会越积越多
  mods.agro.teardown?.()
})
</script>

<template>
  <div id="agro-app">

    <!-- 农化制品视图 -->
    <div id="agro-view">
      <div id="agro-loading" class="agro-hint">数据加载中...</div>
      <div id="agro-error" class="agro-hint" style="display:none"></div>

      <div id="agro-main" style="display:none">

        <!-- 公司营收结构卡 -->
        <div class="agro-revenue">
          <div class="agro-revenue-head">
            <span class="agro-company">广信股份（603599）</span>
            <span class="agro-revenue-title">2025 年营收结构 · 主营产品价格跟踪</span>
          </div>
          <div class="agro-revenue-bar" id="agro-revenue-bar"></div>
          <div class="agro-revenue-note">产品价格 → 出厂价/毛利率 → 业绩（年报口径：杀菌剂毛利率波动主要系多菌灵价格变动所致）。茚虫威/噁草酮为 2026H1 新投产线，周报自 2025 年初起有连续报价</div>
        </div>

        <!-- 分类筛选 -->
        <div class="agro-tabs" id="agro-tabs">
          <button class="agro-tab active" data-cat="全部">全部</button>
          <button class="agro-tab" data-cat="杀菌剂">杀菌剂</button>
          <button class="agro-tab" data-cat="除草剂">除草剂</button>
          <button class="agro-tab" data-cat="杀虫剂">杀虫剂</button>
          <button class="agro-tab" data-cat="中间体">中间体</button>
        </div>

        <!-- 产品卡片 -->
        <div class="agro-cards" id="agro-cards"></div>

        <!-- 走势图 -->
        <div class="agro-chart-wrap">
          <div class="agro-chart-head">
            <span id="agro-chart-title">价格走势</span>
            <span class="agro-chart-tools">
              <label class="agro-check"><input type="checkbox" id="agro-normalize"> 归一化对比（基准=100）</label>
              <select id="agro-range">
                <option value="1y">近 1 年</option>
                <option value="3y">近 3 年</option>
                <option value="all" selected>全部</option>
              </select>
            </span>
          </div>
          <!-- 内联高度任何 CSS 都盖不住，只能在这里绑定；手机端降到 300px 免得一图占满整屏 -->
          <div id="agro-chart" :style="{ width: '100%', height: isMobile ? '300px' : '420px' }"></div>
        </div>

        <div class="agro-foot" id="agro-foot"></div>
      </div>
    </div>

  </div>
</template>
