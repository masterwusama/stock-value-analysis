<script setup>
import { onMounted } from 'vue'
import { prefetchEcharts } from './lib/echartsLoader'

// 任一页首屏挂载后空闲预热 echarts(1MB chunk):不阻塞本页,只让下一个
// 图表页免付下载+解析(十个视图七个画图,预热命中率很高)
onMounted(prefetchEcharts)
</script>

<template>
  <header class="topbar">
    <router-link to="/" class="brand">价值分析</router-link>
    <nav aria-label="主导航">
      <router-link to="/">证券列表</router-link>
      <router-link to="/pick">掘金台</router-link>
      <router-link to="/agro">农化制品</router-link>
      <router-link to="/commodity">大宗商品</router-link>
      <router-link to="/forecast">业绩预测</router-link>
      <router-link to="/sentiment">情绪日报</router-link>
      <router-link to="/etf">ETF评估</router-link>
      <router-link to="/research">投研库</router-link>
    </nav>
  </header>
  <main class="page">
    <router-view />
  </main>
</template>
