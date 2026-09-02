<script setup>
/* 模拟组合页:三策略页签 + 汇总 + 持仓(桌面表格/移动卡片) + 调仓记录
 * 逻辑自 stock-data/portfolio/assets/portfolio.js 1:1 平移;数据源改 /api/portfolio*(实时市值/盈亏)
 */
import { computed, onMounted, ref } from 'vue'
import { get } from '../api/client'
import '../assets/portfolio.css'

const KEYS = ['schloss', 'grahamDef', 'buffett']
const COLS = [
  { key: 'name', label: '标的' },
  { key: 'cost', label: '成本价' },
  { key: 'shares', label: '持股数' },
  { key: 'value', label: '市值' },
  { key: 'weight', label: '持仓比例' },
  { key: 'price', label: '现价' },
  { key: 'pnl_pct', label: '盈亏比例' },
  { key: 'pnl', label: '盈亏额' },
  { key: 'days', label: '持仓天数' },
]

const pf = ref(null)
const trades = ref(null)
const error = ref('')
const key = ref('schloss')
const sortBy = ref({ key: null, dir: -1 })

onMounted(async () => {
  try {
    const [p, t] = await Promise.all([get('/portfolio'), get('/portfolio/trades')])
    pf.value = p
    trades.value = t
  } catch (e) {
    error.value = `持仓数据加载失败：${e.message}`
  }
})

function fmt(n, d) {
  if (n == null || isNaN(n)) return '-'
  return Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d })
}
const cls = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat')
const sign = (v, d) => (v > 0 ? '+' : '') + fmt(v, d)

const strategy = computed(() => (pf.value ? pf.value.strategies[key.value] : null))

// 持仓排序:无排序键保持引擎原始序;文本列(标的)按拼音,其余数值
const positions = computed(() => {
  const s = strategy.value
  if (!s) return []
  const pos = s.positions || []
  const k = sortBy.value.key
  if (!k) return pos.slice()
  const dir = sortBy.value.dir
  const val = (p) => (k === 'weight' ? p.value / s.nav : p[k])
  return pos.slice().sort((a, b) => {
    if (k === 'name') return String(a.name).localeCompare(String(b.name), 'zh-CN') * dir
    return ((val(a) || 0) - (val(b) || 0)) * dir
  })
})

// 表头点击排序:同列切换升降,换列数值列默认降序、文本列默认升序
function clickSort(k) {
  if (sortBy.value.key === k) {
    sortBy.value = { key: k, dir: sortBy.value.dir * -1 }
  } else {
    sortBy.value = { key: k, dir: k === 'name' ? 1 : -1 }
  }
}
const caret = (k) => (sortBy.value.key === k ? (sortBy.value.dir < 0 ? ' ▼' : ' ▲') : '')

// 调仓记录(该策略,倒序)
const tradeRows = computed(() => ((trades.value || {})[key.value] || []).slice().reverse())
</script>

<template>
  <div>
    <div v-if="error" class="error">{{ error }}</div>
    <div v-else-if="!pf" class="pf-hint">加载中…</div>

    <template v-else-if="strategy">
      <div class="pf-header">
        <div class="pf-title">模拟组合
          <span class="pf-asof">行情截至 {{ strategy.as_of || '-' }}</span>
        </div>
        <div class="pf-sub">三策略组合模拟持仓(价值投资引擎每日结算);现价/市值/盈亏按最新行情实时计算</div>
      </div>

      <div class="pf-tabs">
        <div v-for="k in KEYS" :key="k"
             class="pf-tab" :class="{ active: k === key }"
             @click="key = k">{{ pf.strategies[k] && pf.strategies[k].label }}</div>
      </div>

      <div class="pf-summary">
        <div class="pf-stat">
          <div class="k">总资产</div>
          <div class="v">{{ fmt(strategy.nav) }} 元</div>
          <div class="s">初始 {{ fmt(strategy.init_cap, 0) }} · 现金 {{ fmt(strategy.cash) }}</div>
        </div>
        <div class="pf-stat">
          <div class="k">持仓整体盈亏</div>
          <div class="v" :class="cls(strategy.total_pnl)">{{ sign(strategy.total_pnl) }}</div>
          <div class="s" :class="cls(strategy.total_pnl_pct)">{{ sign(strategy.total_pnl_pct) }}%</div>
        </div>
        <div class="pf-stat">
          <div class="k">当日盈亏</div>
          <div class="v" :class="cls(strategy.day_pnl)">{{ sign(strategy.day_pnl) }}</div>
          <div class="s" :class="cls(strategy.day_pnl_pct)">{{ sign(strategy.day_pnl_pct) }}%</div>
        </div>
        <div class="pf-stat">
          <div class="k">总持仓比例</div>
          <div class="v">{{ fmt(strategy.position_pct, 1) }}%</div>
          <div class="s">持仓 {{ (strategy.positions || []).length }} 只</div>
        </div>
      </div>

      <div class="pf-positions">
        <table class="pf-table">
          <thead>
            <tr>
              <th v-for="c in COLS" :key="c.key"
                  class="pf-th-sort" :class="{ 'pf-th-on': sortBy.key === c.key }"
                  title="点击排序" @click="clickSort(c.key)">{{ c.label }}{{ caret(c.key) }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!positions.length">
              <td :colspan="COLS.length" style="text-align:center;color:#8a94a6">当前空仓（等待买点出现）</td>
            </tr>
            <tr v-for="p in positions" :key="p.code">
              <td><b>{{ p.name }}</b> <span style="color:#8a94a6">{{ p.code }}</span></td>
              <td>{{ fmt(p.cost) }}</td>
              <td>{{ p.shares.toLocaleString('zh-CN') }}</td>
              <td>{{ fmt(p.value) }}</td>
              <td>{{ fmt(p.value / strategy.nav * 100, 1) }}%</td>
              <td>{{ fmt(p.price) }}</td>
              <td :class="cls(p.pnl_pct)">{{ sign(p.pnl_pct) }}%</td>
              <td :class="cls(p.pnl)">{{ sign(p.pnl) }}</td>
              <td>{{ p.days }} 天</td>
            </tr>
          </tbody>
        </table>
        <div v-if="!positions.length" class="pf-empty">当前空仓（等待买点出现）</div>
        <div v-for="p in positions" :key="'c' + p.code" class="pf-card">
          <div class="pf-card-top">
            <div><span class="pf-name">{{ p.name }}</span><span class="pf-code">{{ p.code }}</span></div>
            <div class="pf-pnl-big" :class="cls(p.pnl_pct)">{{ sign(p.pnl_pct) }}%</div>
          </div>
          <div class="pf-grid">
            <div><div class="k">成本价</div><div class="v">{{ fmt(p.cost) }}</div></div>
            <div><div class="k">现价</div><div class="v">{{ fmt(p.price) }}</div></div>
            <div><div class="k">持股数</div><div class="v">{{ p.shares.toLocaleString('zh-CN') }}</div></div>
            <div><div class="k">市值</div><div class="v">{{ fmt(p.value) }}</div></div>
            <div><div class="k">持仓比例</div><div class="v">{{ fmt(p.value / strategy.nav * 100, 1) }}%</div></div>
            <div><div class="k">盈亏额</div><div class="v" :class="cls(p.pnl)">{{ sign(p.pnl) }}</div></div>
            <div><div class="k">持仓天数</div><div class="v">{{ p.days }} 天</div></div>
          </div>
        </div>
      </div>

      <details class="pf-trades" open>
        <summary>调仓记录（{{ strategy.label }}，共 {{ tradeRows.length }} 笔）</summary>
        <div v-if="!tradeRows.length" class="pf-trades-empty">暂无调仓记录</div>
        <div v-for="(t, i) in tradeRows" :key="i" class="pf-trade-row">
          <span class="pf-trade-meta">{{ t.date }}</span>
          <span class="pf-side" :class="t.side === 'buy' ? 'up' : t.side === 'dividend' ? 'flat' : 'down'">
            {{ t.side === 'buy' ? '买入' : t.side === 'dividend' ? '分红' : '卖出' }}
          </span>
          <span><b>{{ t.name }}</b> <span class="pf-trade-meta">{{ t.code }}</span></span>
          <span v-if="t.side === 'dividend'">
            {{ t.amount > 0 ? '现金分红入账' : '转增股 ' + (t.shares || 0).toLocaleString('zh-CN') + ' 股' }}
          </span>
          <span v-else>{{ fmt(t.price) }} 元 × {{ t.shares.toLocaleString('zh-CN') }} 股</span>
          <span>{{ fmt(t.amount) }} 元</span>
          <span class="pf-trade-meta">{{ t.reason || '' }}</span>
        </div>
      </details>
    </template>
  </div>
</template>
