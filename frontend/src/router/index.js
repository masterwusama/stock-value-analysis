import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'list', component: () => import('../views/StockListView.vue') },
  { path: '/stock/:code', name: 'detail', component: () => import('../views/StockDetailView.vue') },
  { path: '/agro', name: 'agro', component: () => import('../views/AgroView.vue') },
  { path: '/sentiment', name: 'sentiment', component: () => import('../views/SentimentView.vue') },
]

const router = createRouter({ history: createWebHashHistory(), routes })

// 懒加载分块失败自愈：页面开着跨过一次前端部署时，vite 已把旧 hash 的路由分块删掉，
// 点进详情这类懒加载路由会 404，表现是「点了没反应」。捕获后整页刷新到目标地址——
// index.html 是 no-cache 的，刷新即拿到新 hash 的入口，页面自愈不用用户懂原因。
router.onError((error, to) => {
  const msg = String(error?.message || error)
  if (/importing module|Failed to fetch dynamically|Loading chunk|error loading dynamically/.test(msg) && to) {
    window.location.assign(`#${to.fullPath}`)
    window.location.reload()
  }
})

export default router
