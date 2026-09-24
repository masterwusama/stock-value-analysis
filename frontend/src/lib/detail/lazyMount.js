/* 详情页懒挂载器：占位壳 → 进入视口前 rootMargin 构建（一次性）。
 * 从 renderDetail 闭包里的 buildLazy 泛化（2026-09-25 模块化重构）：
 * - IO 回调跟渲染帧走，后台页签/省电模式会迟到 → 视口内的壳 register 时同步直建，
 *   其余交观察器；再加滚动节流兜底（部分 WebView 的 IO 不可靠），三保险。
 * - forceBuild(key) 给锚点跳转用：目标还没建就先建再滚，不跳空壳。
 * - 单例式自清理：全部建完自动摘监听；destroy() 供页面重渲染/卸载时整体回收。
 */
export function createLazyMount(opts) {
  const rootMargin = (opts && opts.rootMargin) || '200px 0px'
  const built = new Set()          // key 集（含构建失败——失败不重试，占位保留）
  const els = new Map()            // key → { el, build }
  let io = null
  let scrollFn = null
  let scrollT = null

  function runBuild(key) {
    const rec = els.get(key)
    if (!rec || built.has(key)) return
    built.add(key)
    try {
      rec.build(rec.el)
    } catch (e) { /* 单模块失败不连坐：占位文案保留，控制台可见 */ }
    rec.el.style.minHeight = ''
    els.delete(key)
    // 不在此处自毁：注册是逐个进行的，首项直建后 els 瞬时为空会误杀后续模块的
    // 观察器（实测 charts 之后全部 pending）。scroll 监听留到外部 destroy() 再摘。
  }

  function inBand(el) {
    const r = el.getBoundingClientRect()
    return r.top < window.innerHeight + 200 && r.bottom > -200
  }

  function sweep() {
    for (const [key, rec] of Array.from(els)) {
      if (inBand(rec.el)) runBuild(key)
    }
  }

  const api = {
    register(el, key, build) {
      if (built.has(key)) return
      el.style.minHeight = el.style.minHeight || '80px'
      els.set(key, { el, build })
      if (inBand(el)) { runBuild(key); return }
      if (io) io.observe(el)
    },
    forceBuild(key) { if (els.has(key)) runBuild(key) },
    destroy() {
      if (io) { io.disconnect(); io = null }
      if (scrollFn) { window.removeEventListener('scroll', scrollFn); scrollFn = null }
      if (scrollT) { clearTimeout(scrollT); scrollT = null }
    },
    pending() { return Array.from(els.keys()) },
  }

  if ('IntersectionObserver' in window) {
    io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return
        const key = en.target.dataset.lazy
        if (key) runBuild(key)
        io.unobserve(en.target)
      })
    }, { rootMargin })
  }
  scrollFn = () => {
    if (scrollT) return
    scrollT = setTimeout(() => {
      scrollT = null
      if (els.size) sweep()
    }, 200)
  }
  window.addEventListener('scroll', scrollFn, { passive: true })
  return api
}
