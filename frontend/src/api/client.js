// 轻量 API 客户端:统一拼 /api 前缀与查询串
const BASE = '/api'

export async function get(path, params = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v)
  }
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ''}`
  const res = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`${path} HTTP ${res.status}`)
  return res.json()
}
