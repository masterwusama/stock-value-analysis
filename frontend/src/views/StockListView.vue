<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '../api/client'

const router = useRouter()

const COLS = [
  { key: 'code', label: '代码/名称', l: true },
  { key: null, label: '行业', l: true, noSort: true },
  { key: 'price', label: '现价', noSort: true },
  { key: null, label: '涨跌', noSort: true },
  { key: 'pe_ttm', label: 'PE(TTM)' },
  { key: 'pb', label: 'PB' },
  { key: 'market_cap', label: '市值(亿)' },
  { key: 'score_graham_agg', label: '格进取' },
  { key: 'score_graham_def', label: '格防御' },
  { key: 'score_schloss', label: '施洛斯' },
  { key: 'score_buffett', label: '巴菲特' },
  { key: 'fraud', label: '造假' },
  { key: 'mgmt', label: '管理' },
  { key: 'cycle', label: '周期' },
  { key: 'fair_liq', label: '清算' },
  { key: 'net_cash_ratio', label: '净现金/市值' },
  // 价格参考合并列:每流派一列,竖排 买→保守/公允(同原站 listCells)
  { key: 'grahamAgg', label: '格进取 买/保/公', ref: true },
  { key: 'grahamDef', label: '格防御 买/保/公', ref: true },
  { key: 'schloss', label: '施洛斯 买/保/公', ref: true },
  { key: 'buffett', label: '巴菲特 买/保/公', ref: true },
]

const market = ref('')
const keyword = ref('')
const kwDebounced = ref('')
const sort = ref('market_cap')
const order = ref('desc')
const page = ref(1)
const pageSize = 50

const data = ref(null)
const loading = ref(false)
const error = ref('')

// 筛选(语义同原版):造假≤/管理≥/买点多选×折扣%/卖点多选(须同时达保守与公允);空值=不限
const SCHOOLS = [['grahamAgg', '格进取'], ['grahamDef', '格防御'], ['schloss', '施洛斯'], ['buffett', '巴菲特']]
const flt = reactive({ fraudMax: '', mgmtMin: '', buys: [], discount: '', sells: [] })

function toggleFlt(arr, key, on) {
  const i = arr.indexOf(key)
  if (on && i < 0) arr.push(key)
  if (!on && i >= 0) arr.splice(i, 1)
}
function resetFlt() {
  Object.assign(flt, { fraudMax: '', mgmtMin: '', buys: [], discount: '', sells: [] })
  applyFlt()
}
function applyFlt() {
  if (page.value !== 1) page.value = 1  // watch 会触发 load;首页时需手动
  else load()
}
const fltCount = () =>
  (flt.fraudMax !== '' ? 1 : 0) + (flt.mgmtMin !== '' ? 1 : 0) +
  (flt.buys.length ? 1 : 0) + (flt.sells.length ? 1 : 0)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await get('/securities', {
      market: market.value, keyword: kwDebounced.value,
      fraud_max: flt.fraudMax === '' ? null : flt.fraudMax,
      mgmt_min: flt.mgmtMin === '' ? null : flt.mgmtMin,
      buys: flt.buys.length ? flt.buys.join(',') : null,
      sells: flt.sells.length ? flt.sells.join(',') : null,
      discount: (flt.discount !== '' && flt.buys.length) ? flt.discount : null,
      sort: sort.value, order: order.value, page: page.value, page_size: pageSize,
    })
  } catch (e) {
    error.value = `加载失败：${e.message}`
  } finally {
    loading.value = false
  }
}

function setSort(key) {
  if (!key) return
  if (sort.value === key) {
    order.value = order.value === 'desc' ? 'asc' : 'desc'
  } else {
    sort.value = key
    order.value = 'desc'
  }
  page.value = 1
}

watch([market, sort, order, page], load)
watch(keyword, (v) => {
  clearTimeout(setSort._t)
  setSort._t = setTimeout(() => { kwDebounced.value = v.trim(); page.value = 1 }, 300)
})

onMounted(load)

const fmt = (n, d = 2) => n == null ? '-' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })
const score2 = (n) => n == null ? '-' : (n * 100).toFixed(1) + '%'
const pct = (n) => n == null ? '-' : (n > 0 ? '+' : '') + (n * 100).toFixed(2) + '%'
const yi = (n) => n == null ? '-' : (n / 1e8).toFixed(1)
const score = (n) => n == null ? '-' : n.toFixed(1)
// 价格参考列(原版语义):现价≤买价绿;现价≥保守/公允卖价红;公允缺失时留空不占位
// 字段名 = 前缀与流派键都转 snake:sell_cons_graham_agg 等
const snake = (v) => v.replace(/([A-Z])/g, '_$1').toLowerCase()
const REF_LABELS = { grahamAgg: '格·进取', grahamDef: '格·防御', schloss: '施洛斯', buffett: '巴菲特' }
const refKey = (k, kind) => snake(kind) + '_' + snake(k)
const refBuy = (s, k) => s[refKey(k, 'buy')]
const refCons = (s, k) => s[refKey(k, 'sellCons')]
const refFair = (s, k) => s[refKey(k, 'sellFair')]
const refTitle = (s, k) => {
  const f = (v) => v == null ? '-' : fmt(v)
  return `${REF_LABELS[k]}：买 ${f(refBuy(s, k))} / 保卖 ${f(refCons(s, k))} / 公卖 ${f(refFair(s, k))}`
}
const cls = (n) => n > 0 ? 'up' : n < 0 ? 'down' : 'flat'
const MARKET_NAME = { A: 'A股', HK: '港股', US: '美股' }
const totalPages = () => data.value ? Math.max(1, Math.ceil(data.value.total / pageSize)) : 1
</script>

<template>
  <div class="card">
    <div class="toolbar">
      <div class="tabs">
        <button v-for="t in [['', '全部'], ['A', 'A股'], ['HK', '港股'], ['US', '美股']]" :key="t[0]"
                class="tab" :class="{ active: market === t[0] }"
                @click="market = t[0]; page = 1">{{ t[1] }}</button>
      </div>
      <input v-model="keyword" placeholder="搜索代码 / 名称" />
      <span v-if="data" style="color: var(--sub)">共 {{ data.total }} 只 · 快照 {{ data.trade_date }}</span>
    </div>

    <div class="flts">
      <label class="num" title="财报造假可能性(0-100,越高越可疑),只保留 ≤ 该分的公司">造假≤
        <input v-model="flt.fraudMax" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="管理层水平(0-100,越高越好),只保留 ≥ 该分的公司">管理≥
        <input v-model="flt.mgmtMin" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <span class="t" title="多选需同时满足:现价 ≤ 买价 × 折扣%">买点</span>
      <label v-for="[k, lab] in SCHOOLS" :key="'b' + k" class="cb">
        <input type="checkbox" :checked="flt.buys.includes(k)"
               @change="toggleFlt(flt.buys, k, $event.target.checked); applyFlt()">{{ lab }}</label>
      <label class="num disc" title="买点门槛 × 折扣%,如填 80 要求现价 ≤ 买价×80%,填 120 放宽到买价×120%;仅勾选买点后可用,留空等同 100%">打折
        <input v-model="flt.discount" type="number" min="0" max="500" step="1" placeholder="100"
               :disabled="!flt.buys.length" @change="applyFlt">%</label>
      <span class="t" title="多选需同时满足:现价须同时 ≥ 保守卖价与公允卖价">卖点</span>
      <label v-for="[k, lab] in SCHOOLS" :key="'s' + k" class="cb">
        <input type="checkbox" :checked="flt.sells.includes(k)"
               @change="toggleFlt(flt.sells, k, $event.target.checked); applyFlt()">{{ lab }}</label>
      <button type="button" class="rst" @click="resetFlt">重置筛选{{ fltCount() ? `(${fltCount()})` : '' }}</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="loading && !data" class="loading">加载中…</div>

    <div v-if="data && !data.items.length && !loading" class="loading">无符合筛选条件的公司</div>
    <div v-else-if="data" style="overflow-x: auto">
      <table class="grid">
        <thead>
          <tr>
            <th v-for="c in COLS" :key="c.label" :class="{ l: c.l }"
                @click="c.key && setSort(c.key)">
              {{ c.label }}<template v-if="c.key && sort === c.key">{{ order === 'desc' ? ' ▼' : ' ▲' }}</template>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in data?.items" :key="s.sid" @click="router.push(`/stock/${s.code}`)">
            <td class="l"><b>{{ s.name }}</b> <span class="badge">{{ MARKET_NAME[s.market] }}</span> {{ s.code }}</td>
            <td class="l">{{ s.industry || '-' }}</td>
            <td>{{ fmt(s.price) }}</td>
            <td :class="cls(s.change_pct)">{{ pct(s.change_pct) }}</td>
            <td>{{ fmt(s.pe_ttm) }}</td>
            <td>{{ fmt(s.pb) }}</td>
            <td>{{ yi(s.market_cap) }}</td>
            <td>{{ score(s.score_graham_agg) }}</td>
            <td>{{ score(s.score_graham_def) }}</td>
            <td>{{ score(s.score_schloss) }}</td>
            <td>{{ score(s.score_buffett) }}</td>
            <td>{{ score(s.fraud) }}</td>
            <td>{{ score(s.mgmt) }}</td>
            <td>{{ score(s.cycle) }}</td>
            <td :class="{ 'r-hit': s.fair_liq != null && s.price != null && s.price <= s.fair_liq }"
                title="公允清算价值估算：(流动资产合计-负债合计)/股本">{{ fmt(s.fair_liq) }}</td>
            <td :class="{ 'r-hit': s.net_cash_ratio != null && s.net_cash_ratio >= 1 }"
                title="净现金/市值（最近一期财报），≥100% 表示扣除全部负债后的类现金仍高于市值">{{ score2(s.net_cash_ratio) }}</td>
            <td v-for="c in COLS.filter(x => x.ref)" :key="c.key" class="c-ref" :title="refTitle(s, c.key)">
              <span class="rf-buy" :class="{ 'r-hit': refBuy(s, c.key) != null && s.price != null && s.price <= refBuy(s, c.key) }">{{ fmt(refBuy(s, c.key)) }}</span>
              <span class="rf-sell">
                <span :class="{ 'r-hit-s': refCons(s, c.key) != null && s.price != null && s.price >= refCons(s, c.key) }">{{ fmt(refCons(s, c.key)) }}</span>
                <span :class="{ 'r-hit-s': refFair(s, c.key) != null && s.price != null && s.price >= refFair(s, c.key) }">{{ refFair(s, c.key) == null ? '' : fmt(refFair(s, c.key)) }}</span>
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <button :disabled="page <= 1" @click="page--">上一页</button>
      <span>{{ page }} / {{ totalPages() }}</span>
      <button :disabled="page >= totalPages()" @click="page++">下一页</button>
    </div>
  </div>
</template>

<style scoped>
.flts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  padding: 10px 0 12px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--sub);
}
.flts .t { font-weight: 600; color: var(--txt); margin-left: 6px; }
.flts .num { display: inline-flex; align-items: center; gap: 4px; }
.flts .num input {
  width: 62px;
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--bg);
  color: var(--txt);
}
.flts .num input:disabled { opacity: 0.45; }
.flts .cb { display: inline-flex; align-items: center; gap: 3px; cursor: pointer; }
.flts .disc { margin-left: 2px; }
.flts .rst {
  margin-left: auto;
  padding: 3px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--card);
  color: var(--txt);
  cursor: pointer;
  font-size: 13px;
}
.flts .rst:hover { border-color: var(--accent); color: var(--accent); }
/* 价格参考合并列(移植原站 .c-ref 竖排样式) */
.c-ref { white-space: nowrap; }
.c-ref .rf-buy { display: block; }
.c-ref .rf-sell { display: flex; flex-direction: column; font-size: 11px; line-height: 1.3; opacity: 0.78; }
.r-hit { color: #0a7d3c; font-weight: 600; }
.r-hit-s { color: #c0392b; }
</style>
