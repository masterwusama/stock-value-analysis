// 懒 chunk 加载失败自愈：页面开着跨过一次前端部署/本地重建时，vite 已把旧 hash
// 的分块删掉（本地 build 清空 dist；云端整包覆盖也不保证旧 hash 齐全），动态
// import 404 的表现是「点了没反应」或「图表区留空壳」。整页刷新一次拿当前构建
// —— index.html 是 no-cache 的，刷新即换新入口，页面自愈不用用户懂原因。
// 10 秒防抖（sessionStorage 'va_chunk_heal'）：部署出一半（新 hash 也 404）会
// 连环触发，窗口内只自愈一次，第二次把错误留给调用方/控制台可见。
export function healStaleChunk(err, targetPath) {
  const msg = String((err && err.message) || err || '')
  if (!/importing module|Failed to fetch dynamically|Loading chunk|error loading dynamically/i.test(msg)) return false
  try {
    const last = Number(sessionStorage.getItem('va_chunk_heal') || 0)
    if (Date.now() - last < 10000) return false
    sessionStorage.setItem('va_chunk_heal', String(Date.now()))
  } catch (e) { /* 无痕模式写不进：宁可冒险也不放弃自愈 */ }
  if (targetPath) window.location.assign(`#${targetPath}`)
  window.location.reload()
  return true
}
