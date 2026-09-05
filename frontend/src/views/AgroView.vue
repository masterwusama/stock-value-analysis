<script setup>
/* 农价/行业EDB量价页:DOM 骨架与原 agro-price/index.html 一致,
 * 渲染整体复用 legacy 链(agroLegacy.js + edbLegacy.js),数据源 /api/agro/*
 */
import { nextTick, onBeforeUnmount, onMounted } from 'vue'
import * as echarts from 'echarts'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'
import '../assets/agro.css'

window.echarts = echarts
const isMobile = useMediaQuery(MOBILE_QUERY)
let mods = null

onMounted(async () => {
  await nextTick()
  const agro = await import('../lib/agroLegacy.js')
  const edb = await import('../lib/edbLegacy.js')
  mods = { agro, edb }
  agro.init() // 拉 /api/agro/products 渲染农化视图
  edb.init()  // 绑定行业切换栏(EDB 数据懒加载)
})

onBeforeUnmount(() => {
  if (!mods) return
  const c = mods.agro.agroState.chart
  if (c) { try { c.dispose() } catch (e) { /* 已释放 */ } mods.agro.agroState.chart = null }
  mods.edb.edbState.instances.forEach((x) => { try { x.dispose() } catch (e) { /* 已释放 */ } })
  mods.edb.edbState.instances = []
  // 光 dispose 不够：两个模块的 resize 监听还挂在 window 上，路由切走后
  // 每次窗口变化都会去 resize 已释放的实例，反复来回切会越积越多
  mods.agro.teardown?.()
  mods.edb.teardown?.()
})
</script>

<template>
  <div id="agro-app">

    <!-- 行业切换栏 -->
    <div class="edb-switch" id="edb-switch">
      <div class="edb-segs">
        <button class="edb-seg active" data-view="agro">农化制品</button>
        <button class="edb-seg" data-view="auto">汽车</button>
        <button class="edb-seg" data-view="alu">电解铝</button>
        <button class="edb-seg" data-view="shipping">航运</button>
        <button class="edb-seg" data-view="tire">轮胎橡胶</button>
        <button class="edb-seg" data-view="realestate">地产链</button>
        <button class="edb-seg" data-view="coal">煤炭</button>
        <button class="edb-seg" data-view="steel">钢铁</button>
      </div>
      <span class="edb-switch-note">行业 EDB 量价跟踪 · 数据源 万得 Wind（周/月聚合）</span>
    </div>

    <!-- ========== 农化制品视图 ========== -->
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
          <div class="agro-revenue-note">产品价格 → 出厂价/毛利率 → 业绩（年报口径：杀菌剂毛利率波动主要系多菌灵价格变动所致）</div>
        </div>

        <!-- 分类筛选 -->
        <div class="agro-tabs" id="agro-tabs">
          <button class="agro-tab active" data-cat="全部">全部</button>
          <button class="agro-tab" data-cat="杀菌剂">杀菌剂</button>
          <button class="agro-tab" data-cat="除草剂">除草剂</button>
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

    <!-- ========== EDB 量价视图 ========== -->
    <div id="edb-view" style="display:none">
      <div id="edb-loading" class="agro-hint" style="display:none">EDB 数据加载中...</div>
      <div id="edb-error" class="agro-hint" style="display:none"></div>

      <div id="edb-body" style="display:none">
        <div class="edb-cat-head" id="edb-cat-head"></div>

        <!-- KPI 指标卡行 -->
        <div class="edb-kpis" id="edb-kpis"></div>

        <!-- 相对走势总览 -->
        <div class="edb-panel">
          <div class="edb-panel-head">
            <span class="edb-panel-title">相对走势总览</span>
            <span class="edb-panel-sub">各指标区间起点归一为 100，仅比较相对涨跌幅（单位不同不可直接比高低）</span>
          </div>
          <div id="edb-chart-overview" class="edb-chart" :style="{ height: isMobile ? '300px' : '420px' }"></div>
        </div>

        <!-- 多维度分类图表（动态生成） -->
        <div id="edb-dim-charts"></div>

        <div class="agro-foot" id="edb-foot"></div>
      </div>
    </div>

  </div>
</template>
