// 详情 JSON 共享缓存：列表悬浮代入式、移动端点按预取、详情页三方共用同一份/同一个在途
// Promise。详情数据一天一刷（16:05 批次），当天内「悬浮过再点进」零等待；LRU 24 家与
// 列表页旧实现同参。缓存的是 Promise——并发点同一家不会发第二个请求。
// TTL 4 小时：跨过 16:05 批次还开着的页面不再拿旧行情现算 V（悬浮代入式与缓存命中
// 的详情页都会用快照里的市值，隔夜的快照算出来的 V 会与列表入库分对不上口径）。
import { get } from '../api/client'

const MAX = 24
const TTL_MS = 4 * 3600 * 1000
const cache = new Map()   // code → { at: 毫秒, p: Promise<json> }（命中即挪队尾，触顶淘汰最旧）

export function prefetchDetail(code) {
  const hit = cache.get(code)
  if (hit) {
    cache.delete(code)
    if (Date.now() - hit.at < TTL_MS) {
      cache.set(code, hit)
      return hit.p
    }
    cache.delete(code)     // 过期：直接淘汰走重取
  }
  const p = get(`/securities/${encodeURIComponent(code)}`).then((j) => {
    if (cache.size >= MAX) {
      const oldest = cache.keys().next().value
      if (oldest !== undefined && oldest !== code) cache.delete(oldest)
    }
    return j
  }).catch((e) => {
    cache.delete(code)   // 失败不占坑：下次重试
    throw e
  })
  cache.set(code, { at: Date.now(), p })
  return p
}

export const getDetail = prefetchDetail
