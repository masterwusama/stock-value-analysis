<script setup>
</script>

<template>
  <header class="topbar">
    <router-link to="/" class="brand">价值分析</router-link>
    <nav>
      <router-link to="/">证券列表</router-link>
      <router-link to="/portfolio">模拟组合</router-link>
      <router-link to="/agro">农价·量价</router-link>
    </nav>
  </header>
  <main class="page">
    <router-view />
  </main>
</template>
