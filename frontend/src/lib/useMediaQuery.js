import { ref, onBeforeUnmount } from 'vue'

// 断点值须与 stock.css / agro.css 的 @media(max-width:600px) 及 stockLegacy.js 的 mqMobile 三处同步
export const MOBILE_QUERY = '(max-width: 600px)'

export function useMediaQuery(query) {
  const mq = window.matchMedia(query)
  const matches = ref(mq.matches)
  // change 事件之外再挂 resize 兜底：部分环境（如嵌入式 WebView）媒体查询状态翻转时
  // 不保证派发 change，resize 重读一次 matches，代价是一次布尔赋值
  const sync = () => { matches.value = mq.matches }
  const onChange = (e) => { matches.value = e.matches }
  mq.addEventListener('change', onChange)
  window.addEventListener('resize', sync)
  onBeforeUnmount(() => {
    mq.removeEventListener('change', onChange)
    window.removeEventListener('resize', sync)
  })
  return matches
}
