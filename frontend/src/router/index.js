import { createRouter, createWebHashHistory } from 'vue-router'
import { healStaleChunk } from '../lib/chunkRecovery.js'

const routes = [
  { path: '/', name: 'list', component: () => import('../views/StockListView.vue') },
  { path: '/pick', name: 'pick', component: () => import('../views/PickView.vue') },
  { path: '/stock/:code', name: 'detail', component: () => import('../views/StockDetailView.vue') },
  { path: '/agro', name: 'agro', component: () => import('../views/AgroView.vue') },
  { path: '/commodity', name: 'commodity', component: () => import('../views/CommodityView.vue') },
  { path: '/forecast', name: 'forecast', component: () => import('../views/ForecastView.vue') },
  { path: '/sentiment', name: 'sentiment', component: () => import('../views/SentimentView.vue') },
  { path: '/etf', name: 'etf', component: () => import('../views/EtfView.vue') },
  { path: '/research', name: 'research', component: () => import('../views/ResearchView.vue') },
  { path: '/research/:slug', name: 'research-report', component: () => import('../views/ResearchView.vue') },
]

const router = createRouter({ history: createWebHashHistory(), routes })

// 路由懒分块 404（跨部署旧页签）自愈，正则与防抖同 echarts 链路共用 chunkRecovery
router.onError((error, to) => {
  healStaleChunk(error, to?.fullPath)
})

export default router
