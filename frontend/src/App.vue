<script setup>
</script>

<template>
  <header class="topbar">
    <router-link to="/" class="brand">价值分析</router-link>
    <nav aria-label="主导航">
      <router-link to="/">证券列表</router-link>
      <router-link to="/pick">掘金台</router-link>
      <router-link to="/agro">农价·量价</router-link>
      <router-link to="/sentiment">情绪日报</router-link>
      <router-link to="/research">投研库</router-link>
    </nav>
  </header>
  <main class="page">
    <router-view />
  </main>
</template>
