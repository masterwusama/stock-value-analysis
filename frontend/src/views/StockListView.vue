<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '../api/client'

const router = useRouter()

const COLS = [
  // stick：横向滚动时固定在左侧，滚到右边仍知道当前是哪只（同原站 .stick）
  { key: 'code', label: '代码/名称', l: true, stick: true },
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
  // 排序键用列头=买入价(原站 thSort 语义),格内两档卖价由小字各自点击排序
  { key: 'buy_graham_agg', label: '格进取 买/保/公', ref: true, school: 'grahamAgg' },
  { key: 'buy_graham_def', label: '格防御 买/保/公', ref: true, school: 'grahamDef' },
  { key: 'buy_schloss', label: '施洛斯 买/保/公', ref: true, school: 'schloss' },
  { key: 'buy_buffett', label: '巴菲特 买/保/公', ref: true, school: 'buffett' },
]

const market = ref('')
// 全市场 5500 只规模下“翻页”不实用:板块/行业/ST 作为基本维度先缩小范围
const BOARDS = [['', '全部板块'], ['shMain', '沪主'], ['szMain', '深主'], ['gem', '创业'], ['star', '科创'], ['bj', '北交']]
const board = ref('')
const industry = ref('')
const noSt = ref(false)
const industries = ref([])
const keyword = ref('')
const kwDebounced = ref('')
const sort = ref('market_cap')
const order = ref('desc')
const page = ref(1)
const pageSize = ref(50)
const jump = ref(null)   // 5500 只×50 行 = 110 页，逐页点不现实

const data = ref(null)
const loading = ref(false)
const error = ref('')

// 筛选(语义同原版):造假≤/管理≥/买点多选×折扣%/卖点多选(须同时达保守与公允);空值=不限
// 规模相关:剔除ST(低 PB 假便宜的重灾区)、行业单选(全市场几十个字)、市值区间(本币亿)
const SCHOOLS = [['grahamAgg', '格进取'], ['grahamDef', '格防御'], ['schloss', '施洛斯'], ['buffett', '巴菲特']]
const flt = reactive({ fraudMax: '', mgmtMin: '', capMin: '', capMax: '', buys: [], discount: '', sells: [] })
// 市值框让用户填“亿”(与列头 市值(亿) 同口径),发请求时转回接口单位元：
// 接口接受本币元、响应 market_cap 也是本币元,两处同一单位才能拿着返回值直接核对边界。
// Math.round 而非直乘：1.1 * 1e8 = 110000000.00000001 这种尾差会把恰好在边上的票顶出筛选。
const capYiToYuan = (v) => Math.round(Number(v) * 1e8)
// 不折成人民币：汇率源未落地,拿估算值折算只会让人误以为是可比口径
const CAP_TIP = '总市值区间（单位：亿，按各市场本币计价——A股人民币、港股港元、美股美元，不折算）：'
  + '“全部”tab 下三个市场混在一起比数值没有意义，要跨市场比体量请切到对应市场 tab 分开筛；'
  + '无最新行情的公司（市值列显示 -）不进区间，设任一门槛即被排除'

// Wind 事件增强分档：造假/管理两列在“基础财报分”与“基础分 + 一次性 Wind 事件增量”之间切换，
// 显示值在本页算（dispScore），筛选与排序把 wind=1 透给后端用同一公式的 SQL 表达式，
// 故不会出现“表头按增强分排、格子里是另一套分”。localStorage 记忆同旧内嵌页的 va_wind。
const windMode = ref((() => {
  try { return localStorage.getItem('va_wind') === '1' } catch (e) { return false }
})())
// 旧内嵌页靠 ./data/events/index.json 能否加载来决定这个按钮出不出现；本机列表全走 /api，
// 覆盖层已在 score_daily.wind_* 列里，没有“加载失败”这个信号可判，改按市场显示：
// Wind 事件是一次性抓取、只覆盖部分 A 股，切到港股/美股 tab 时整列都是“-”，摆出来只会误导
const windVisible = () => market.value === '' || market.value === 'A'
function toggleWind() {
  windMode.value = !windMode.value
  try { localStorage.setItem('va_wind', windMode.value ? '1' : '0') } catch (e) { /* 无痕模式下写不进，切换照样生效 */ }
  page.value = 1
}
// Wind 档下的列显示值：无事件条目不给分（“-”），有则基础分叠 delta 钉 0~100；
// 基础分本身缺失时同样“-”（无基可加），与后端 _wind_score 的三条分支一一对应
function dispScore(s, kind) {
  if (!windMode.value) return s[kind]
  if (!s.wind_hit || s[kind] == null) return null
  const d = (kind === 'fraud' ? s.wind_fraud_delta : s.wind_mgmt_delta) || 0
  return Math.max(0, Math.min(100, s[kind] + d))
}
const FRAUD_TIP = '财报造假可能性（0-100，越高越可疑）：净现背离/高应计/应收存货增速背离/毛利率逆势上升/其他应收占用等量化红旗加权'
const MGMT_TIP = '管理层水平（0-100，越高越好）：分红连续性与规模、回购、股权激励、机构持股等治理口径加权'
function windTip(s, kind, baseTip) {
  if (!windMode.value) return baseTip
  if (!s.wind_hit) {
    return '无 Wind 事件数据（一次性抓取仅覆盖部分 A 股），“事件增强分”档下不给分；切回“基础”档可看财报基础分'
  }
  const base = s[kind]
  const disp = dispScore(s, kind)
  const d = (kind === 'fraud' ? s.wind_fraud_delta : s.wind_mgmt_delta) || 0
  const flags = s.wind_flags?.length ? '；事件：' + s.wind_flags.join('、') : ''
  return `Wind 事件增强：基础财报 ${base == null ? '-' : base.toFixed(1)} 分 ${d >= 0 ? '+' : ''}${d.toFixed(1)} → ${disp == null ? '-' : disp.toFixed(1)}${flags}`
}

function toggleFlt(arr, key, on) {
  const i = arr.indexOf(key)
  if (on && i < 0) arr.push(key)
  if (!on && i >= 0) arr.splice(i, 1)
}
function resetFlt() {
  Object.assign(flt, { fraudMax: '', mgmtMin: '', capMin: '', capMax: '', buys: [], discount: '', sells: [] })
  industry.value = ''
  noSt.value = false
  applyFlt()
}
function applyFlt() {
  if (page.value !== 1) page.value = 1  // watch 会触发 load;首页时需手动
  else load()
}
const fltCount = () =>
  (flt.fraudMax !== '' ? 1 : 0) + (flt.mgmtMin !== '' ? 1 : 0) +
  (flt.capMin !== '' ? 1 : 0) + (flt.capMax !== '' ? 1 : 0) +
  (flt.buys.length ? 1 : 0) + (flt.sells.length ? 1 : 0) +
  (industry.value ? 1 : 0) + (noSt.value ? 1 : 0)

// 请求序号守卫：同一轮连改两个相邻筛选框（典型如把市值≥ 清空同时填市值≤）会并发两条请求，
// 而后发的那条不保证先返回；没守卫时旧结果会后落地把新结果盖掉（实测：表格短暂回到未过滤的
// 全量只数）。只认最后发出的那条，晚到的旧响应直接丢弃。
let loadSeq = 0

async function load() {
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  try {
    const d = await get('/securities', {
      market: market.value, board: board.value, industry: industry.value,
      st: noSt.value ? false : null,
      keyword: kwDebounced.value,
      fraud_max: flt.fraudMax === '' ? null : flt.fraudMax,
      mgmt_min: flt.mgmtMin === '' ? null : flt.mgmtMin,
      cap_min: flt.capMin === '' ? null : capYiToYuan(flt.capMin),
      cap_max: flt.capMax === '' ? null : capYiToYuan(flt.capMax),
      buys: flt.buys.length ? flt.buys.join(',') : null,
      sells: flt.sells.length ? flt.sells.join(',') : null,
      discount: (flt.discount !== '' && flt.buys.length) ? flt.discount : null,
      wind: windMode.value || null,
      sort: sort.value, order: order.value, page: page.value, page_size: pageSize.value,
    })
    if (seq !== loadSeq) return
    data.value = d
  } catch (e) {
    if (seq === loadSeq) error.value = `加载失败：${e.message}`
  } finally {
    if (seq === loadSeq) loading.value = false
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

// 行业字典跟着市场走：A 股是国标行业（100+ 类）、港股是恒生行业、美股是东财中文行业（11 类），
// 混在一个下拉里切到美股根本找不到目标行业，计数也是全市场口径（原来整市场一次拉全、不随 tab 变）
async function loadIndustries() {
  try {
    industries.value = await get('/securities/industries', { market: market.value })
  } catch (e) { /* 下拉缺失不影响列表主体 */ }
}

// 必须注册在下面 load 的 watch 之前：切市场先把已选行业清掉，同一轮里触发的 load 才带着空行业去请求
watch(market, () => { industry.value = ''; loadIndustries() })

// kwDebounced 必须在依赖里：搜索框原本只靠下面防抖回调里的 page=1 间接触发刷新，
// 而搜索时通常已在第一页，页码不变 → watch 不触发 → 输入了也没发请求（applyFlt 同坑）。
// windMode 同理：它是整列口径的开关，不在依赖里就会看到“点了没反应”的老毛病。
watch([market, board, sort, order, page, kwDebounced, windMode], load)
watch(pageSize, load)
watch(keyword, (v) => {
  clearTimeout(setSort._t)
  setSort._t = setTimeout(() => { kwDebounced.value = v.trim(); page.value = 1 }, 300)
})

onMounted(() => {
  load()
  loadIndustries()
})

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
// 表头高亮:本流派列的买/保/公任一档被排序都算激活(同原站 thSort 把 sellC-/sellF- 归一到 buy-)
function sortActive(c) {
  if (!c.ref) return sort.value === c.key
  const s = snake(c.school)
  return ['buy', 'sell_cons', 'sell_fair'].some((k) => sort.value === `${k}_${s}`)
}
const refTitle = (s, k) => {
  const f = (v) => v == null ? '-' : fmt(v)
  return `${REF_LABELS[k]}：买 ${f(refBuy(s, k))} / 保卖 ${f(refCons(s, k))} / 公卖 ${f(refFair(s, k))}`
}
const cls = (n) => n > 0 ? 'up' : n < 0 ? 'down' : 'flat'
const MARKET_NAME = { A: 'A股', HK: '港股', US: '美股' }
const totalPages = () => data.value ? Math.max(1, Math.ceil(data.value.total / pageSize.value)) : 1
function doJump() {
  const n = parseInt(jump.value, 10)
  if (!Number.isFinite(n)) return
  const target = Math.min(Math.max(1, n), totalPages())
  jump.value = null
  if (target === page.value) load()
  else page.value = target
}
</script>

<template>
  <div class="card">
    <div class="toolbar">
      <div class="tabs">
        <button v-for="t in [['', '全部'], ['A', 'A股'], ['HK', '港股'], ['US', '美股']]" :key="t[0]"
                class="tab" :class="{ active: market === t[0] }"
                @click="market = t[0]; board = ''; page = 1">{{ t[1] }}</button>
      </div>
      <div v-if="market === '' || market === 'A'" class="tabs">
        <button v-for="b in BOARDS" :key="b[0]"
                class="tab" :class="{ active: board === b[0] }"
                @click="board = b[0]; page = 1">{{ b[1] }}</button>
      </div>
      <input v-model="keyword" placeholder="搜索代码 / 名称" />
      <span v-if="data" style="color: var(--sub)">共 {{ data.total }} 只 · 快照 {{ data.trade_date }}</span>
    </div>

    <div class="flts">
      <button v-if="windVisible()" type="button" class="wind" :class="{ on: windMode }"
              title="切换造假/管理两列口径：基础财报分 ↔ Wind 事件增强分（基础分 + 一次性 Wind 事件增量，排序与造假≤/管理≥筛选同步跟随；Wind 档下无事件数据的公司不给分显示 -，切回基础档可看全部）"
              @click="toggleWind">事件增强分 <b>{{ windMode ? 'Wind' : '基础' }}</b></button>
      <label class="t">行业
        <select v-model="industry" @change="applyFlt">
          <option value="">全部</option>
          <option v-for="i in industries" :key="i.industry" :value="i.industry">{{ i.industry }}（{{ i.count }}）</option>
        </select></label>
      <label class="cb" title="名称含 ST/*ST 的公司（退市风险与财务造假高发区）"><input type="checkbox" v-model="noSt" @change="applyFlt">剔除ST</label>
      <label class="num" title="财报造假可能性(0-100,越高越可疑),只保留 ≤ 该分的公司">造假≤
        <input v-model="flt.fraudMax" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="管理层水平(0-100,越高越好),只保留 ≥ 该分的公司">管理≥
        <input v-model="flt.mgmtMin" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <label class="num" :title="CAP_TIP">市值≥
        <input v-model="flt.capMin" type="number" min="0" step="0.5" placeholder="不限" @change="applyFlt"></label>
      <label class="num" :title="CAP_TIP">市值≤
        <input v-model="flt.capMax" type="number" min="0" step="0.5" placeholder="不限" @change="applyFlt"></label>
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
    <div v-else-if="data" class="tbl-wrap">
      <table class="grid grid-list">
        <thead>
          <tr>
            <th v-for="c in COLS" :key="c.label" :class="{ l: c.l, unsort: !c.key, stick: c.stick }"
                :title="c.ref ? '按买入参考价排序（保守/公允价点格内小字），再点切换升/降序'
                             : (c.key ? '点击排序，再点切换升/降序' : '')"
                @click="c.key && setSort(c.key)">
              {{ c.label }}<template v-if="sortActive(c)">{{ order === 'desc' ? ' ▼' : ' ▲' }}</template>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in data?.items" :key="s.sid" @click="router.push(`/stock/${s.code}`)">
            <td class="l stick"><b>{{ s.name }}</b> <span class="badge">{{ MARKET_NAME[s.market] }}</span> {{ s.code }}</td>
            <td class="l"><span class="ind" :title="s.industry">{{ s.industry || '-' }}</span></td>
            <!-- 币种角标：港股/美股的现价与市值是本币（HKD/USD），跟 A 股人民币数值直接比大小会误读 -->
            <td>{{ fmt(s.price) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :class="cls(s.change_pct)">{{ pct(s.change_pct) }}</td>
            <td>{{ fmt(s.pe_ttm) }}</td>
            <td>{{ fmt(s.pb) }}</td>
            <td>{{ yi(s.market_cap) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td>{{ score(s.score_graham_agg) }}</td>
            <td>{{ score(s.score_graham_def) }}</td>
            <td>{{ score(s.score_schloss) }}</td>
            <td>{{ score(s.score_buffett) }}</td>
            <td :title="windTip(s, 'fraud', FRAUD_TIP)">{{ score(dispScore(s, 'fraud')) }}</td>
            <td :title="windTip(s, 'mgmt', MGMT_TIP)">{{ score(dispScore(s, 'mgmt')) }}</td>
            <td>{{ score(s.cycle) }}</td>
            <td :class="{ 'r-hit': s.fair_liq != null && s.price != null && s.price <= s.fair_liq }"
                title="公允清算价值估算：(流动资产合计-负债合计)/股本">{{ fmt(s.fair_liq) }}</td>
            <td :class="{ 'r-hit': s.net_cash_ratio != null && s.net_cash_ratio >= 1 }"
                title="净现金/市值（最近一期财报），≥100% 表示扣除全部负债后的类现金仍高于市值">{{ score2(s.net_cash_ratio) }}</td>
            <td v-for="c in COLS.filter(x => x.ref)" :key="c.school" class="c-ref" :title="refTitle(s, c.school)">
              <span class="rf-buy" :class="{ 'r-hit': refBuy(s, c.school) != null && s.price != null && s.price <= refBuy(s, c.school) }">{{ fmt(refBuy(s, c.school)) }}</span>
              <span class="rf-sell">
                <span class="sl-sort" :class="{ 'r-hit-s': refCons(s, c.school) != null && s.price != null && s.price >= refCons(s, c.school) }"
                      title="按保守卖出价排序" @click.stop="setSort(refKey(c.school, 'sellCons'))">{{ fmt(refCons(s, c.school)) }}</span>
                <span class="sl-sort" :class="{ 'r-hit-s': refFair(s, c.school) != null && s.price != null && s.price >= refFair(s, c.school) }"
                      title="按公允卖出价排序" @click.stop="setSort(refKey(c.school, 'sellFair'))">{{ refFair(s, c.school) == null ? '' : fmt(refFair(s, c.school)) }}</span>
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
      <label class="psize">每页
        <select v-model.number="pageSize" @change="page = 1">
          <option :value="50">50</option><option :value="100">100</option><option :value="200">200</option>
        </select></label>
      <label class="psize">跳至
        <input v-model.number="jump" type="number" min="1" :max="totalPages()" @keyup.enter="doJump">
        <button @click="doJump">GO</button></label>
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
.flts select {
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--bg);
  color: var(--txt);
  max-width: 220px;
}
.pager .psize { display: inline-flex; align-items: center; gap: 4px; margin-left: 10px; color: var(--sub); }
.pager .psize input { width: 62px; padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--txt); }
.pager .psize select { padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--txt); }
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
/* Wind 事件增强分切换档：选中时边框与文字走主题色（不加底色，与筛选栏其它控件一致） */
.flts .wind {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--card);
  color: var(--txt);
  cursor: pointer;
  font-size: 13px;
}
.flts .wind b { font-weight: 600; }
.flts .wind.on { border-color: var(--accent); color: var(--accent); }
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
/* 格内保/公允卖价：可点排序（不靠色块区分，靠下划线提示） */
.c-ref .sl-sort { cursor: pointer; }
.c-ref .sl-sort:hover { text-decoration: underline; }
/* 币种角标：只在非 A 股出现，右上角小字，不参与排序也不撑宽列 */
.ccy { font-style: normal; font-size: 9px; color: #999; vertical-align: super; margin-left: 1px; }
table.grid th.unsort { cursor: default; }
/* ---- 宽屏铺开 + 密集排版：21 列争取在 1440 视口下不横向滚动（装不下仍由 .tbl-wrap 滚动兜底） ---- */
.tbl-wrap { overflow-x: auto; }
/* 表头允许折行：列宽改由数值决定（“格进取 买/保/公”不再硬撑一行的宽度），CJK 可任意断字 */
table.grid-list th { white-space: normal; line-height: 1.25; }
table.grid-list th, table.grid-list td { padding: 6px 6px; }
/* 行业名最长 20 字（“铁路、船舶、航空航天和其他运输设备制造业”），不约束会单列吃掉 260px；截断后完整名走 title */
table.grid-list .ind {
  display: inline-block;
  max-width: 112px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
.r-hit { color: #0a7d3c; font-weight: 600; }
.r-hit-s { color: #c0392b; }
</style>
