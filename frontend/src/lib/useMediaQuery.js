import { ref, onBeforeUnmount } from 'vue'

// 断点值须与 stock.css / agro.css 的 @media(max-width:600px) 及 stockLegacy.js 的 mqMobile 三处同步
export const MOBILE_QUERY = '(max-width: 600px)'

export function useMediaQuery(query) {
  const mq = window.matchMedia(query)
  const matches = ref(mq.matches)
  const onChange = (e) => { matches.value = e.matches }
  mq.addEventListener('change', onChange)
  onBeforeUnmount(() => mq.removeEventListener('change', onChange))
  return matches
}
