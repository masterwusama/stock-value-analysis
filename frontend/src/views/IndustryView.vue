<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '../api/client'
import { aliasOf } from '../lib/industryAlias'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'

const router = useRouter()
const isMobile = useMediaQuery(MOBILE_QUERY)
const loading = ref(true)
const error = ref('')
const meta = ref(null)
const rows = ref([])

const DENSITY_TIP = '在买点内家数 / 该派有买点参考价的家数(无锚者不计入分母——多数成长股没有资产/盈利锚)'
const NOTE_TIP = 'PB/PE 中位仅计正值;PB 分位为每股一行最新观测(含停更陈旧行),数据日期见页脚。'

// school 列显示 hit/anchor,排序按密度(占比);其余列直接按字段排
const COLS = [
  { key: 'industry', label: '行业', text: true, stick: true },
  { key: 'n', label: '家数' },
  { key: 'med_pb', label: 'PB中位', d: 2 },
  { key: 'med_pe', label: 'PE中位', d: 1 },
  { key: 'med_pb_pct', label: 'PB分位中位', d: 1 },
  { key: 'd_ga', label: '格攻买点', school: 'graham_agg', tip: DENSITY_TIP },
  { key: 'd_gd', label: '格防买点', school: 'graham_def', tip: DENSITY_TIP },
  { key: 'd_sc', label: '施洛斯买点', school: 'schloss', tip: DENSITY_TIP },
  { key: 'd_bf', label: '巴菲特买点', school: 'buffett', tip: DENSITY_TIP },
  { key: 'gate_pass', label: '过门槛', tip: '硬门槛(造假/陷阱/立案等)全过的家数' },
  { key: 'st_high', label: 'ST高危', tip: 'ST 风险分 >0 的家数' },
  { key: 'med_fraud', label: '造假中位', d: 1 },
  { key: 'med_mgmt', label: '管理中位', d: 1 },
  { key: 'med_cycle', label: '周期中位', d: 1, tip: '仅周期股子集(cyclical=1)的中位,非周期行业为空' },
  { key: 'med_div_pct', label: '股息中位%', d: 2 },
]

const sortKey = ref('d_sc')
const sortDir = ref('desc')

function val(row, key) {
  const col = COLS.find((c) => c.key === key)
  if (key === 'industry') return row.industry
  if (col && col.school) {
    const b = row.buy[col.school]
    return b && b.n_anchor ? b.hit / b.n_anchor : null
  }
  if (key === 'med_div_pct') return row.med_div == null ? null : row.med_div * 100
  return row[key]
}

const sorted = computed(() => {
  const col = COLS.find((c) => c.key === sortKey.value)
  const dir = sortDir.value === 'desc' ? -1 : 1
  return [...rows.value].sort((a, b) => {
    const va = val(a, sortKey.value)
    const vb = val(b, sortKey.value)
    if (va == null && vb == null) return 0
    if (va == null) return 1 // 空值永远垫底,与方向无关
    if (vb == null) return -1
    if (col && col.text) return va.localeCompare(vb, 'zh') * dir
    return (va - vb) * dir
  })
})

function setSort(key) {
  const col = COLS.find((c) => c.key === key)
  if (!col) return
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 'desc' ? 'asc' : 'desc'
  } else {
    sortKey.value = key
    sortDir.value = col.text ? 'asc' : 'desc'
  }
}

function drill(row) {
  // 列表页 onMounted 已支持 ?ind= 恢复(hash 状态持久化的既有键),此处零改动对接
  router.push({ path: '/', query: { ind: row.industry } })
}

const fmt = (n, d = 0) => (n == null ? '-' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }))

onMounted(async () => {
  try {
    const d = await get('/industries/overview')
    rows.value = d.rows
    meta.value = d.meta
  } catch (e) {
    error.value = String(e?.message || e)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2>行业视角</h2>
      <span v-if="meta" class="sub">证监会大类 · {{ meta.n_stocks }} 只 A 股 · {{ meta.date }}
        <span class="tip" :title="NOTE_TIP">ⓘ</span></span>
    </div>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error" class="error">{{ error }}</div>

    <template v-else>
      <!-- 桌面:排序表 -->
      <div v-if="!isMobile" class="card tablewrap">
        <table>
          <thead>
            <tr>
              <th v-for="c in COLS" :key="c.key" :class="{ sortable: true, stick: c.stick }"
                  :title="c.tip || ((c.school ? DENSITY_TIP : ''))"
                  @click="setSort(c.key)">
                {{ c.label }}<span class="arrow" v-if="sortKey === c.key">{{ sortDir === 'desc' ? '▼' : '▲' }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in sorted" :key="r.industry" class="row" @click="drill(r)">
              <td class="stick" :title="r.industry">
                <span class="name">{{ aliasOf(r.industry) }}</span>
                <span class="cnt">{{ r.n }}家</span>
              </td>
              <td class="num">{{ fmt(r.n) }}</td>
              <td class="num">{{ fmt(r.med_pb, 2) }}</td>
              <td class="num">{{ fmt(r.med_pe, 1) }}</td>
              <td class="num pct" :class="r.med_pb_pct != null && r.med_pb_pct < 20 ? 'low' : ''">{{ fmt(r.med_pb_pct, 1) }}</td>
              <td v-for="c in COLS.filter((x) => x.school)" :key="c.key" class="num">
                <span v-if="r.buy[c.school].n_anchor" :class="{ hot: r.buy[c.school].hit / r.buy[c.school].n_anchor >= 0.2 }">
                  {{ r.buy[c.school].hit }}/{{ r.buy[c.school].n_anchor }}</span>
                <span v-else class="sub">-</span>
              </td>
              <td class="num">{{ fmt(r.gate_pass) }}</td>
              <td class="num" :class="{ warn: r.st_high > 0 }">{{ fmt(r.st_high) }}</td>
              <td class="num">{{ fmt(r.med_fraud, 1) }}</td>
              <td class="num">{{ fmt(r.med_mgmt, 1) }}</td>
              <td class="num">{{ fmt(r.med_cycle, 1) }}</td>
              <td class="num">{{ fmt(r.med_div == null ? null : r.med_div * 100, 2) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 窄屏:卡片 -->
      <div v-else class="cards">
        <div v-for="r in sorted" :key="r.industry" class="card mcard" @click="drill(r)">
          <div class="mhead">
            <span class="name">{{ aliasOf(r.industry) }}</span>
            <span class="cnt">{{ r.n }}家</span>
          </div>
          <div class="mline">
            <span>PB {{ fmt(r.med_pb, 2) }}</span>
            <span>分位 {{ fmt(r.med_pb_pct, 1) }}</span>
            <span>施洛斯 {{ r.buy.schloss.n_anchor ? r.buy.schloss.hit + '/' + r.buy.schloss.n_anchor : '-' }}</span>
          </div>
          <div class="mline sub">
            <span>门槛 {{ fmt(r.gate_pass) }}</span>
            <span>ST {{ fmt(r.st_high) }}</span>
            <span>管理 {{ fmt(r.med_mgmt, 0) }}</span>
            <span>造假 {{ fmt(r.med_fraud, 0) }}</span>
          </div>
        </div>
      </div>
      <p class="foot sub" v-if="meta">{{ meta.anchor_note }}<template
        v-if="meta.pb_pctile_date"> PB 分位数据日期 {{ meta.pb_pctile_date }}。</template></p>
    </template>
  </div>
</template>

<style scoped>
h2 { margin: 0; font-size: 16px; }
.sub { color: var(--sub); }
.toolbar .sub { font-size: 12px; }
.tip { cursor: help; }
.tablewrap { overflow-x: auto; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { position: sticky; top: 0; background: var(--card); color: var(--sub); font-weight: 500;
     cursor: pointer; user-select: none; z-index: 1; }
th:hover { color: var(--accent); }
.arrow { font-size: 10px; margin-left: 2px; }
td.stick, th.stick { position: sticky; left: 0; background: var(--card); z-index: 2; }
th.stick { z-index: 3; }
.row { cursor: pointer; }
.row:hover { background: #f5f7fb; }
.name { font-weight: 600; }
.cnt { margin-left: 6px; font-size: 11px; color: var(--sub); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.pct.low { color: var(--accent); font-weight: 600; }
.hot { color: var(--up); font-weight: 600; }
.warn { color: var(--up); }
.cards { display: flex; flex-direction: column; gap: 8px; }
.mcard { padding: 10px 12px; cursor: pointer; }
.mhead { display: flex; justify-content: space-between; align-items: baseline; }
.mline { display: flex; gap: 14px; margin-top: 6px; font-size: 13px; font-variant-numeric: tabular-nums; }
.foot { font-size: 11px; margin-top: 10px; }
</style>
