// echarts 单例加载器：全站唯一的 echarts 入口。
// 为什么不静态 import：echarts+zrender 是 1MB（gzip 346KB）的大块，静态导入会把它
// 拖进「进入页面」的关键路径——详情页点进去先解析 1MB 才开始拉数据（移动端 1~3 秒），
// 而详情页的图表本就是懒模块、农价/情绪页也只在挂载后才画图，echarts 晚到无妨。
// window.echarts 契约保留：legacy 渲染层（stockLegacy/agroLegacy/SentimentView 的
// 图表初始化）继续读全局 echarts，只是由这里负责在需要前注入。
import { healStaleChunk } from './chunkRecovery.js'

let pending = null

export function ensureEcharts() {
  if (!pending) {
    pending = import('echarts')
      .then((m) => {
        window.echarts = m
        return m
      })
      .catch((e) => {
        // 失败不缓存：原来 rejected promise 被永久留在 pending 里，一次瞬时网络
        // 错误就让本页签 echarts 再也装不上（图表区整页留空直到手动刷新）
        pending = null
        // 跨部署的旧页签：echarts 分块 hash 已失效，刷新一次换新构建（防抖内不重复刷）
        healStaleChunk(e)
        throw e
      })
  }
  return pending
}

// 空闲预热:十个视图七个要画图,首屏挂载后趁空闲把 chunk 拉好,用户点进任一
// 图表页时不再付「下载 1MB + 解析」的关键路径开销。与 stockLegacy 的 warm 同款
// 节奏;后台页签 idle 不触发由 timeout 兜底。
export function prefetchEcharts() {
  const warm = () => { ensureEcharts().catch(() => {}) }
  if (typeof requestIdleCallback === 'function') requestIdleCallback(warm, { timeout: 4000 })
  else setTimeout(warm, 2000)
}
