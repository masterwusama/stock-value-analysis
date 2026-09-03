import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'list', component: () => import('../views/StockListView.vue') },
  { path: '/stock/:code', name: 'detail', component: () => import('../views/StockDetailView.vue') },
  { path: '/agro', name: 'agro', component: () => import('../views/AgroView.vue') },
]

export default createRouter({ history: createWebHashHistory(), routes })
