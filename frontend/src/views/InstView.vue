<script setup>
/* 机构持仓页(社保及外资,2026-11-18):十大流通股东口径的社保/QFII/保险/养老持仓跟踪。
 * 数据 /api/inst/{,holding,stock/{code},org/{holder}};读时合成,前端零状态。
 * 口径横幅三条(与后端 api/inst.py 头注同源):前十大=进不了前十系统性缺席;
 * 季报滞后≤3个月,每个数字带报告期 as-of;战略持股=近似标签(占流通比≥5% 且
 * 近4期连续不变),默认视图可一键过滤。研究工具非投资建议。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { get } from '../api/client'

const ov = ref(null)
const error = ref('')
const tab = ref('ov')            // ov | ssf | qfii | stock
// ---- 明细列表(社保/QFII 两 tab 共用一套状态,type 跟随 tab)----
const listType = computed(() => (tab.value === 'qfii' ? 'QFII' : '社保'))
const period = ref('')
const changeF = ref('all')
const q = ref('')
const exclStrat = ref(false)
const sortKey = ref('mktcap')
const order = ref('desc')
const page = ref(1)
const rows = ref([])
const total = ref(0)
const listLoading = ref(false)
let listSeq = 0
// ---- 单股 ----
const stockCode = ref('')
const stockData = ref(null)
const stockLoading = ref(false)
let stockSeq = 0
// ---- 组合视图(列表内点击机构名展开)----
const org = ref(null)
const orgLoading = ref(false)

function parseHash() {
  const qs = new URLSearchParams((location.hash.split('?')[1] || ''))
  return { code: qs.get('code') || '', tab: qs.get('tab') || '' }
}
function syncHash() {
  const p = new URLSearchParams()
  if (tab.value === 'stock' && stockCode.value) p.set('code', stockCode.value)
  if (tab.value !== 'ov') p.set('tab', tab.value)
  history.replaceState(null, '', '#/inst' + (p.toString() ? '?' + p : ''))
}

async function loadOverview() {
  try {
    ov.value = await get('/inst')
    if (!period.value && ov.value.period) {
      period.value = ov.value.period
    }
  } catch (e) {
    error.value = String(e.message || e)
  }
}

async function loadList() {
  const my = ++listSeq
  listLoading.value = true
  org.value = null
  try {
    const d = await get('/inst/holding', {
      type: listType.value, period: period.value, change: changeF.value,
      q: q.value, exclude_strategic: exclStrat.value ? 'true' : '',
      sort: sortKey.value, order: order.value, page: page.value, size: 50,
    })
    if (my !== listSeq) return
    rows.value = d.rows || []
    total.value = d.total || 0
  } catch (e) {
    if (my === listSeq) error.value = String(e.message || e)
  } finally {
    if (my === listSeq) listLoading.value = false
  }
}

async function loadStock(code) {
  const my = ++stockSeq
  stockLoading.value = true
  try {
    const d = await get(`/inst/stock/${encodeURIComponent(code)}`)
    if (my !== stockSeq) return
    stockData.value = d
    stockCode.value = code
    syncHash()
  } catch (e) {
    if (my === stockSeq) error.value = String(e.message || e)
  } finally {
    if (my === stockSeq) stockLoading.value = false
  }
}

async function loadOrg(h) {
  orgLoading.value = true
  try {
    org.value = await get(`/inst/org/${encodeURIComponent(h)}`)
  } catch (e) {
    error.value = String(e.message || e)
  } finally {
    orgLoading.value = false
  }
}

function goStock(code) {
  tab.value = 'stock'
  loadStock(code)
}
function sortBy(k) {
  if (sortKey.value === k) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else { sortKey.value = k; order.value = 'desc' }
  page.value = 1
}
const SORT_LABEL = { mktcap: '持仓市值', holdnum: '持股数', free_ratio: '占流通比', qoq: '环比pp', code: '代码' }

const pages = computed(() => Math.max(1, Math.ceil(total.value / 50)))
const o = computed(() => ov.value || {})
const typeCards = computed(() => {
  const t = o.value.types || {}
  return ['社保', 'QFII', '保险', '养老'].map((k) => ({ key: k, ...(t[k] || {}) }))
})

watch(tab, (t) => {
  syncHash()
  if (t === 'ssf' || t === 'qfii') { page.value = 1; loadList() }
})
watch([period, changeF, exclStrat], () => { page.value = 1; loadList() })
watch(q, () => { page.value = 1; loadList() })  // 输入即查:表短,后端 LIKE 足够

onMounted(async () => {
  const h = parseHash()
  await loadOverview()
  if (h.tab === 'stock' && h.code) {
    tab.value = 'stock'
    loadStock(h.code)
  } else if (h.tab === 'ssf' || h.tab === 'qfii') {
    tab.value = h.tab
  }
})
onBeforeUnmount(() => {})

// ---- 格式化 ----
const yi = (v, d = 2) => (v == null ? '-' : (+v).toLocaleString('zh-CN', { maximumFractionDigits: d }))
const wan = (v) => {
  if (v == null) return '-'
  return v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿股' : (v / 1e4).toFixed(1) + ' 万股'
}
const pct1 = (v, d = 2) => (v == null ? '-' : (+v).toFixed(d) + '%')
const pp = (v) => (v == null ? '-' : (+v >= 0 ? '+' : '') + (+v).toFixed(2) + 'pp')
const CHG_CLS = { 新进: 'chg-new', 增持: 'chg-add', 减持: 'chg-cut', 不变: 'chg-flat' }
const GATE_CN = { pass: '', trap: '⚠陷阱', fraud: '⚠造假', cycletop: '⚠周期顶', nodata: '缺数' }
function recText(r) { return r.recommend == null ? '-' : (+r.recommend).toFixed(1) }
function gateNote(g) { return GATE_CN[(g || '').split('+')[0]] || '' }
</script>

<template>
  <div class="inst">
    <div v-if="error" class="err">加载失败:{{ error }}</div>

    <div class="toolbar">
      <div class="tabs">
        <button type="button" class="tab" :class="{ active: tab === 'ov' }" @click="tab = 'ov'">总览</button>
        <button type="button" class="tab" :class="{ active: tab === 'ssf' }" @click="tab = 'ssf'">社保</button>
        <button type="button" class="tab" :class="{ active: tab === 'qfii' }" @click="tab = 'qfii'">QFII·外资</button>
        <button type="button" class="tab" :class="{ active: tab === 'stock' }" @click="tab = 'stock'">单股查询</button>
      </div>
      <span v-if="o.period" class="asof">
        {{ o.report_name }} · 行情 {{ o.quote_date }} · 评分 {{ o.score_date }}
      </span>
    </div>

    <p class="banner">
      前十大流通股东口径:进不了前十的持仓系统性缺席,不补全;
      季报口径滞后最多 3 个月;战略持股为近似标签(占流通比≥5% 且近 4 期连续不变)。
      研究工具非投资建议。
    </p>

    <!-- ① 总览 -->
    <template v-if="tab === 'ov'">
      <div v-if="!o.period" class="empty">库内无机构持仓数据(先跑 python -m collector.run inst)</div>
      <template v-if="o.period">
        <div class="ov-cards">
          <section v-for="c in typeCards" :key="c.key" class="card ov-card"
                   :title="c.key + ' 持仓概览(前十大口径)'">
            <h3>{{ c.key }}<span class="sub">{{ c.holders }} 个组合/机构 · {{ c.stocks }} 只</span></h3>
            <div class="big">{{ yi(c.mktcap_yi, 1) }}<i>亿</i></div>
            <div class="ov-chips">
              <span class="chip"><em class="up">新进 {{ c.new }}</em></span>
              <span class="chip"><em class="up">增持 {{ c.add }}</em></span>
              <span class="chip"><em class="down">减持 {{ c.cut }}</em></span>
              <span class="chip">不变 {{ c.flat }}</span>
            </div>
            <p v-if="c.adhoc" class="adhoc-note">另有 {{ c.adhoc }} 条期后临时披露(单股视图可见)</p>
          </section>
        </div>

        <section class="card" v-if="(o.cluster || []).length">
          <h3>社保抱团榜<span class="sub">≥3 个组合同持一标的 · 组合数/合计持仓市值排</span></h3>
          <div class="tbl-scroll">
            <table class="tbl">
              <thead><tr><th>代码/名称</th><th>组合数</th><th>同持组合</th><th class="num">合计持仓</th><th class="num">R</th></tr></thead>
              <tbody>
                <tr v-for="c in o.cluster" :key="c.secu_code">
                  <td>
                    <a href="javascript:void(0)" class="code-link" @click="goStock(c.secu_code)">
                      {{ c.secu_code }} {{ c.name || '' }}
                    </a>
                  </td>
                  <td class="num strong">{{ c.combo_count }}</td>
                  <td>
                    <span v-for="cm in c.combos" :key="cm.code" class="chip combo-chip"
                          :title="cm.name + ' 持仓 ' + yi(cm.mktcap_yi) + ' 亿'">{{ cm.name.replace('全国社保基金', '') }}</span>
                  </td>
                  <td class="num">{{ yi(c.total_yi) }}</td>
                  <td class="num" :class="{ up: c.recommend >= 65, down: c.recommend != null && c.recommend < 45 }">
                    {{ c.recommend == null ? '-' : c.recommend.toFixed(1) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="card">
          <h3>报告期覆盖<span class="sub">历史期由回填冻结,当期周任务刷新</span></h3>
          <div class="tbl-scroll">
            <table class="tbl">
              <thead><tr><th>报告期</th><th class="num">行数</th></tr></thead>
              <tbody>
                <tr v-for="p in o.periods" :key="p.period" :class="{ cur: p.period === o.period }">
                  <td>{{ p.period }}</td>
                  <td class="num">{{ p.rows }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </template>
    </template>

    <!-- ②③ 社保 / QFII 明细 -->
    <template v-else-if="tab === 'ssf' || tab === 'qfii'">
      <div class="toolbar filters">
        <select v-model="period">
          <option v-for="p in o.periods" :key="p.period" :value="p.period">{{ p.period }}</option>
        </select>
        <select v-model="changeF">
          <option value="all">全部变动</option>
          <option value="new">新进</option>
          <option value="add">增持/新进</option>
          <option value="cut">减持</option>
          <option value="flat">不变</option>
        </select>
        <input v-model="q" type="search" placeholder="代码 / 名称 / 组合" />
        <label class="chk"><input v-model="exclStrat" type="checkbox" />
          剔除战略持股<span class="sub">(≥5% 且近 4 期不变,近似口径)</span></label>
        <span class="grow"></span>
        <span class="asof">{{ total }} 行 · 第 {{ page }}/{{ pages }} 页</span>
        <button type="button" class="tab" :disabled="page <= 1" @click="page -= 1">‹ 上一页</button>
        <button type="button" class="tab" :disabled="page >= pages" @click="page += 1">下一页 ›</button>
      </div>

      <div v-if="listLoading" class="empty">加载中…</div>
      <div v-else-if="!rows.length" class="empty">无符合条件的行。</div>
      <div v-else class="tbl-scroll">
        <table class="tbl">
          <thead>
            <tr>
              <th class="sortable" @click="sortBy('code')">代码/名称<span v-if="sortKey === 'code'" class="arrow">{{ order === 'desc' ? '▼' : '▲' }}</span></th>
              <th>组合 / 机构</th>
              <th class="num sortable" @click="sortBy('holdnum')">持股数<span v-if="sortKey === 'holdnum'" class="arrow">{{ order === 'desc' ? '▼' : '▲' }}</span></th>
              <th class="num sortable" @click="sortBy('free_ratio')" title="占该股流通股比例(前十大名单披露口径)">占流通比<span v-if="sortKey === 'free_ratio'" class="arrow">{{ order === 'desc' ? '▼' : '▲' }}</span></th>
              <th class="num sortable" @click="sortBy('qoq')" title="占流通比环比变动(pp)">环比<span v-if="sortKey === 'qoq'" class="arrow">{{ order === 'desc' ? '▼' : '▲' }}</span></th>
              <th>变动</th>
              <th class="num sortable" @click="sortBy('mktcap')" title="披露时点价 × 持股数(报告期价,非现价)">持仓市值<span v-if="sortKey === 'mktcap'" class="arrow">{{ order === 'desc' ? '▼' : '▲' }}</span></th>
              <th class="num">现价</th>
              <th class="num">涨跌</th>
              <th class="num" title="最新总市值(亿)">总市值</th>
              <th class="num" title="掘金台推荐分 R(0~100);门槛状态见悬停">R</th>
              <th>行业</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.holder_code + r.holder_name + r.secu_code">
              <td>
                <a href="javascript:void(0)" class="code-link" @click="goStock(r.secu_code)">
                  {{ r.secu_code }} {{ r.name }}
                </a>
                <span v-if="r.strategic" class="tag-strat" title="战略持股近似标签:占流通比≥5% 且近 4 期连续不变">战略</span>
              </td>
              <td>
                <a href="javascript:void(0)" class="org-link" :title="'查看 ' + r.holder_name + ' 全持仓'"
                   @click="loadOrg(r.holder_code)">{{ r.holder_name }}</a>
              </td>
              <td class="num">{{ wan(r.hold_num) }}</td>
              <td class="num">{{ pct1(r.free_ratio) }}</td>
              <td class="num" :class="{ up: r.free_ratio_qoq > 0, down: r.free_ratio_qoq < 0 }">{{ pp(r.free_ratio_qoq) }}</td>
              <td><span class="chg" :class="CHG_CLS[r.change_type] || 'chg-flat'">{{ r.change_type || '-' }}</span></td>
              <td class="num strong">{{ yi(r.hold_mktcap_yi) }}</td>
              <td class="num">{{ r.price == null ? '-' : r.price }}</td>
              <td class="num" :class="{ up: r.change_pct > 0, down: r.change_pct < 0 }">
                {{ r.change_pct == null ? '-' : (r.change_pct >= 0 ? '+' : '') + r.change_pct.toFixed(2) + '%' }}
              </td>
              <td class="num">{{ yi(r.market_cap_yi, 0) }}</td>
              <td class="num" :class="{ up: r.recommend >= 65, down: r.recommend != null && r.recommend < 45 }"
                  :title="(r.recommend_gate || 'pass') + (gateNote(r.recommend_gate) ? ' · ' + gateNote(r.recommend_gate) : '')">
                {{ recText(r) }}<i v-if="gateNote(r.recommend_gate)" class="gate">{{ gateNote(r.recommend_gate) }}</i>
              </td>
              <td class="dim">{{ r.industry || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 组合视图(点击机构名展开) -->
      <section class="card org-card" v-if="org || orgLoading">
        <h3 v-if="org">
          组合视图:{{ org.holder_name }}<span class="sub">{{ org.holder_type }} · {{ org.period }} 当期 {{ (org.rows || []).length }} 只 · 历史覆盖 {{ (org.history || []).length }} 期</span>
          <button type="button" class="tab close" @click="org = null">收起</button>
        </h3>
        <div v-if="orgLoading" class="empty">加载中…</div>
        <template v-if="org">
          <div class="tbl-scroll">
            <table class="tbl">
              <thead><tr><th>代码/名称</th><th class="num">持股数</th><th class="num">占流通比</th><th>变动</th><th class="num">持仓市值</th><th class="num">现价</th><th class="num">R</th></tr></thead>
              <tbody>
                <tr v-for="r in org.rows" :key="r.secu_code">
                  <td><a href="javascript:void(0)" class="code-link" @click="goStock(r.secu_code)">{{ r.secu_code }} {{ r.name }}</a></td>
                  <td class="num">{{ wan(r.hold_num) }}</td>
                  <td class="num">{{ pct1(r.free_ratio) }}</td>
                  <td><span class="chg" :class="CHG_CLS[r.change_type] || 'chg-flat'">{{ r.change_type || '-' }}</span></td>
                  <td class="num strong">{{ yi(r.hold_mktcap_yi) }}</td>
                  <td class="num">{{ r.price == null ? '-' : r.price }}</td>
                  <td class="num" :class="{ up: r.recommend >= 65, down: r.recommend != null && r.recommend < 45 }">{{ recText(r) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="hist-line" v-if="(org.history || []).length">
            逐期合计:<span v-for="h in org.history" :key="h.period" class="chip">{{ h.period.slice(2) }} {{ yi(h.mktcap_yi, 0) }}亿/{{ h.stocks }}只</span>
          </p>
        </template>
      </section>
    </template>

    <!-- ④ 单股查询 -->
    <template v-else-if="tab === 'stock'">
      <div class="toolbar">
        <input class="code-in" v-model="stockCode" type="search" placeholder="输入 6 位代码,如 601899"
               @keydown.enter="loadStock(stockCode.trim())" />
        <button type="button" class="tab" @click="loadStock(stockCode.trim())">查询</button>
      </div>
      <div v-if="stockLoading" class="empty">加载中…</div>
      <template v-if="stockData">
        <section class="card head">
          <div class="title">
            {{ stockData.name }}<span class="code">{{ stockData.secu_code }}</span>
            <span v-if="stockData.industry" class="idx">{{ stockData.industry }}</span>
          </div>
          <div class="chips">
            <span class="chip">现价 {{ stockData.price == null ? '-' : stockData.price }}
              <em :class="stockData.change_pct >= 0 ? 'up' : 'down'">{{ stockData.change_pct >= 0 ? '+' : '' }}{{ pct1(stockData.change_pct) }}</em></span>
            <span class="chip">总市值 {{ yi(stockData.market_cap_yi, 0) }} 亿</span>
            <span class="chip" :title="(stockData.recommend_gate || 'pass')">R {{ recText(stockData) }}<i v-if="gateNote(stockData.recommend_gate)" class="gate">{{ gateNote(stockData.recommend_gate) }}</i></span>
            <span class="chip">覆盖 {{ (stockData.rows || []).length }} 行 · {{ (stockData.by_period || []).length }} 期</span>
          </div>
        </section>

        <section class="card" v-if="(stockData.by_period || []).length">
          <h3>逐期机构持仓合计<span class="sub">标准报告期,按类型合计持仓市值(披露时点价)</span></h3>
          <div class="tbl-scroll">
            <table class="tbl">
              <thead><tr><th>报告期</th><th v-for="t in ['社保', 'QFII', '保险', '养老']" :key="t" class="num">{{ t }}</th></tr></thead>
              <tbody>
                <tr v-for="p in stockData.by_period" :key="p.period">
                  <td>{{ p.period }}</td>
                  <td v-for="t in ['社保', 'QFII', '保险', '养老']" :key="t" class="num">
                    {{ p.types[t] ? yi(p.types[t].mktcap_yi, 1) + ' 亿/' + p.types[t].holders + '组合' : '-' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="card">
          <h3>机构持仓明细<span class="sub">含期后临时披露(定增/回购股东名单,标「临时」)</span></h3>
          <div class="tbl-scroll">
            <table class="tbl">
              <thead><tr><th>报告期</th><th>类型</th><th>组合 / 机构</th><th class="num">持股数</th><th class="num">占流通比</th><th>变动</th><th class="num">持仓市值</th><th class="num">十大内排名</th></tr></thead>
              <tbody>
                <tr v-for="r in stockData.rows" :key="r.end_date + r.holder_code + r.holder_name">
                  <td>{{ r.end_date }}<span v-if="!r.is_std_period" class="tag-adhoc">临时</span></td>
                  <td>{{ r.holder_type }}</td>
                  <td>{{ r.holder_name }}</td>
                  <td class="num">{{ wan(r.hold_num) }}</td>
                  <td class="num">{{ pct1(r.free_ratio) }}</td>
                  <td><span class="chg" :class="CHG_CLS[r.change_type] || 'chg-flat'">{{ r.change_type || '-' }}</span></td>
                  <td class="num strong">{{ yi(r.hold_mktcap_yi) }}</td>
                  <td class="num">{{ r.holder_rank || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </template>
      <div v-else-if="!stockLoading && stockCode" class="empty">输入代码后查询。</div>
    </template>

    <p class="note">
      口径说明:
      <br>① 数据源为上市公司定期报告「十大流通股东」名单(东财数据中心聚合),社保组合、
      QFII、保险、基本养老保险基金四类;进不了前十的持仓系统性缺席——小票上的机构仓
      位大多看不见,这是口径不是缺陷。
      <br>② 持仓市值 = 披露时点价 × 持股数(报告期价),与现价列分开;环比 pp = 占流通比
      相对上一标准期的变动。
      <br>③ 社保组合由基金管理人受托管理,组合编号(如一零四组合)≠ 决策人,「国家队护盘」
      叙事不成立;QFII 名单含战略入股(标签过滤),社保≠养老金(基本养老保险基金单列)。
      <br>④ 北向(陆股通)个股日度持仓已随港交所 2024-08 改革停更,现仅季末快照,本页暂未接入
      (记 P1);单股视图中如见「香港中央结算有限公司」为前十大名单自带,即北向季度代理。
      <br>⑤ 数据更新:每周日 10:25 inst job;历史期(近 12 个报告期)由回填冻结。
    </p>
  </div>
</template>

<style scoped>
.inst { display: flex; flex-direction: column; gap: 12px; }
.err { color: var(--up); }
.empty { color: var(--text-dim, #64748b); padding: 20px 0; text-align: center; }
.asof { color: var(--text-dim, #64748b); font-size: 12px; }
.banner {
  margin: 0; padding: 8px 12px; border: 1px solid var(--line, #e2e8f0);
  border-left: 3px solid var(--warn, #d97706); border-radius: var(--r-m, 8px);
  font-size: 12.5px; color: var(--text-dim, #475569); background: var(--bg-soft, #f8fafc);
}
.grow { flex: 1; }
.filters select, .filters input[type="search"] {
  min-height: 32px; border: 1px solid var(--line, #cbd5e1); border-radius: var(--r-m, 8px);
  padding: 4px 8px; background: var(--bg, #fff); font-size: 13px;
}
.chk { display: flex; align-items: center; gap: 5px; font-size: 12.5px; color: var(--text-dim, #475569); }
.chk .sub { font-size: 11px; }

.ov-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.ov-card h3 { display: flex; flex-direction: column; }
.big { font-size: 26px; font-weight: 700; margin: 6px 0; }
.big i { font-size: 13px; font-style: normal; color: var(--text-dim, #64748b); margin-left: 3px; }
.ov-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.ov-chips .chip { font-size: 12px; }
.ov-chips em { font-style: normal; font-weight: 600; }
.adhoc-note { margin: 6px 0 0; font-size: 11.5px; color: var(--text-dim, #64748b); }

.tbl-scroll { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tbl th, .tbl td { padding: 6px 8px; border-bottom: 1px solid var(--line, #eef2f7); white-space: nowrap; text-align: left; }
.tbl th { color: var(--text-dim, #64748b); font-weight: 500; position: sticky; top: 0; background: var(--bg, #fff); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .strong { font-weight: 600; }
.tbl .dim { color: var(--text-dim, #94a3b8); }
.tbl th.sortable { cursor: pointer; user-select: none; }
.tbl th.sortable:hover { color: var(--accent, #2563eb); }
.arrow { font-size: 9px; margin-left: 2px; }
.cur td { background: var(--bg-soft, #f8fafc); font-weight: 600; }

.code-link { color: inherit; text-decoration: none; border-bottom: 1px dashed var(--line, #cbd5e1); }
.code-link:hover { color: var(--accent, #2563eb); }
.org-link { color: var(--accent, #2563eb); text-decoration: none; cursor: pointer; }
.org-link:hover { border-bottom: 1px dashed var(--accent, #2563eb); }

.chg { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11.5px; }
.chg-new, .chg-add { background: rgba(208, 48, 80, 0.1); color: var(--up); }
.chg-cut { background: rgba(23, 160, 90, 0.1); color: var(--down); }
.chg-flat { background: rgba(100, 116, 139, 0.1); color: #64748b; }
.tag-strat, .tag-adhoc {
  display: inline-block; margin-left: 5px; padding: 0 5px; border-radius: 4px;
  font-size: 10.5px; background: rgba(217, 119, 6, 0.12); color: #b45309;
}
.gate { font-style: normal; font-size: 10px; margin-left: 3px; color: #b45309; }
.combo-chip { font-size: 11px; margin: 1px 3px 1px 0; padding: 1px 6px; background: rgba(37, 99, 235, 0.08); }

.org-card h3 { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.org-card .close { margin-left: auto; }
.hist-line { margin: 8px 0 0; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; font-size: 12px; color: var(--text-dim, #475569); }

.code-in { min-width: 220px; }
.head .title { font-size: 17px; font-weight: 700; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.head .code { font-size: 12px; color: var(--text-dim, #64748b); font-weight: 400; }
.head .idx { font-size: 12px; color: var(--text-dim, #64748b); font-weight: 400; }
.head .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { font-size: 12.5px; padding: 2px 8px; border-radius: var(--r-m, 8px); background: var(--bg-soft, #f1f5f9); }
.chip em { font-style: normal; font-weight: 600; margin-left: 3px; }

.note { font-size: 12px; color: var(--text-dim, #64748b); line-height: 1.7; }

@media (max-width: 720px) {
  .filters { gap: 6px; }
  .filters select, .filters input[type="search"] { flex: 1 1 130px; }
  .ov-cards { grid-template-columns: 1fr 1fr; }
  .tbl { font-size: 12px; }
  .tbl th, .tbl td { padding: 5px 6px; }
}
</style>
