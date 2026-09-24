// echarts 单例加载器：全站唯一的 echarts 入口。
// 为什么不静态 import：echarts+zrender 是 1MB（gzip 346KB）的大块，静态导入会把它
// 拖进「进入页面」的关键路径——详情页点进去先解析 1MB 才开始拉数据（移动端 1~3 秒），
// 而详情页的图表本就是懒模块、农价/情绪页也只在挂载后才画图，echarts 晚到无妨。
// window.echarts 契约保留：legacy 渲染层（stockLegacy/agroLegacy/SentimentView 的
// 图表初始化）继续读全局 echarts，只是由这里负责在需要前注入。
let pending = null

export function ensureEcharts() {
  if (!pending) {
    pending = import('echarts').then((m) => {
      window.echarts = m
      return m
    })
  }
  return pending
}
