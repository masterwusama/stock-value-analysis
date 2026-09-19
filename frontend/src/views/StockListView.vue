<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get } from '../api/client'
import { MOBILE_QUERY, useMediaQuery } from '../lib/useMediaQuery'

const router = useRouter()
const route = useRoute()
const isMobile = useMediaQuery(MOBILE_QUERY)

const SEO_TIP = '最近一次已发行定增：发行价（元/股）、发行年月、发行数量；完整日期与募集资金见悬浮和详情。'
  + '定增只指非公开发行，公开增发与配股不计。发行日缺失取上市日。历史价格未复权。'
  + '仅 A 股，来源东方财富；未收录或未覆盖显示 -，不参与评分、买卖点或排序筛选。'
const BUY_TIP = '新=最新公告记录，完=最近已完成回购；同一笔合并展示。价、量、金额各自优先取累计成交值，缺项标注方案上限或计划区间。'
  + '新行日期为公告日（缺失取决议日），完行为完成日（缺失取公告日）。价格单位元/股，历史价格未复权。'
  + '注销/非注销/待核是公告中的股份用途分类；注销不代表已办理注销，回购完成也不等于注销完成。'
  + '仅 A 股，未收录或未覆盖显示 -；不参与评分、买卖点或排序筛选。'

const COLS = [
  // stick：横向滚动时固定在左侧，滚到右边仍知道当前是哪只（同原站 .stick）
  // group/edge：两级表头的分组归属与「组首列」标记（组首列左侧画分隔线），行业并入名称格后不再单列
  { key: 'code', label: '代码/名称', l: true, stick: true, group: 'id', edge: true },
  { key: 'price', label: '现价', noSort: true, group: 'quote', edge: true },
  { key: null, label: '涨跌', noSort: true, group: 'quote' },
  { key: 'pe_ttm', label: 'PE(TTM)', group: 'quote' },
  { key: 'pb', label: 'PB', group: 'quote' },
  { key: 'market_cap', label: '市值(亿)', group: 'quote' },
  { key: 'score_graham_agg', label: '格进取', group: 'school', edge: true },
  { key: 'score_graham_def', label: '格防御', group: 'school' },
  { key: 'score_schloss', label: '施洛斯', group: 'school' },
  { key: 'score_buffett', label: '巴菲特', group: 'school' },
  { key: 'fraud', label: '造假', group: 'score', edge: true },
  { key: 'mgmt', label: '管理', group: 'score' },
  { key: 'cycle', label: '周期', group: 'score' },
  { key: 'trap', label: '陷阱', trap: true, group: 'score' },
  // 价值列：格内是 V（0-100，越高越便宜且有质量），右上角是「七项里查得动几项」
  { key: 'value', label: '价值', value: true, group: 'score' },
  // 成长列：格内是 G（0-100，越高越好，与陷阱分方向相反），右上角是「七项里查得动几项」
  { key: 'growth', label: '成长', growth: true, group: 'score' },
  // 推荐列：门槛外的 0.6×V + 0.4×G（资格线：造假≤40·陷阱≤20·中报≤−70%），门槛外整格 `-`
  { key: 'recommend', label: '推荐', group: 'score' },
  // 清算列：格内是每股清算价值绝对值，排序走性价比（后端 fair_liq 排折价率 1-现价/清算价值）
  { key: 'fair_liq', label: '清算', ratio: true, group: 'asset', edge: true },
  // 净现金三件套（等式顺序：加权现金−有息负债=净现金），紧贴宽口径比值列。
  // 两个「净现金」口径不同：本组是亿（减有息负债），比值列是 %（减负债合计）——列名与
  // 小注直接标明，悬停各有代入式
  { key: 'w_cash', label: '加权现金(亿)', group: 'asset' },
  { key: 'int_debt', label: '有息负债(亿)', group: 'asset' },
  { key: 'net_cash_w', label: '净现金(扣有息)', group: 'asset' },
  // 金额口径（本币亿）；占市值的百分比保留在筛选「净现金/市值」与详情页 ①
  { key: 'net_cash_b', label: '净现金', sub: '减全部负债', group: 'asset' },
  // PB 十年分位（外源 Wind 口径）：只放这一列，PE/PS 分位在详情页——
  // PE 分位对亏损股无意义（一片 “-”）、PS 分位又宽又少人看，摆进这张表只会稀释信号。
  { key: 'pb_pctile', label: 'PB十年分位', group: 'asset' },
  // 价格参考合并列:每流派一列,竖排 买→保守/公允(同原站 listCells)
  // 列头排序键 buy_* 走的是"买入性价比"（现价相对买价的折价深度，后端算），不是买价绝对值；
  // 格内保守/公允两档小字仍按各自卖价排。键名与 score_daily 列/SecurityItem 字段保持一致。
  // 排在股本事件之前：价格参考是估值域的延伸，紧贴资产组；事件是消息域，放表尾。
  { key: 'buy_graham_agg', label: '格进取 买/保/公', ref: true, school: 'grahamAgg', group: 'ref', edge: true },
  { key: 'buy_graham_def', label: '格防御 买/保/公', ref: true, school: 'grahamDef', group: 'ref' },
  { key: 'buy_schloss', label: '施洛斯 买/保/公', ref: true, school: 'schloss', group: 'ref' },
  { key: 'buy_buffett', label: '巴菲特 买/保/公', ref: true, school: 'buffett', group: 'ref' },
  { key: null, label: '定增', noSort: true, tip: SEO_TIP, group: 'event', edge: true },
  { key: null, label: '回购', noSort: true, tip: BUY_TIP, group: 'event' },
]
// 两级表头：组标签行由 COLS 归并派生（span = 组内列数），列名行沿用原 v-for
const GROUP_LABEL = { id: '标的', quote: '行情', school: '四派评分', score: '量化评分', asset: '资产 · 分位', event: '股本事件', ref: '买卖参考价' }
const COL_GROUPS = COLS.reduce((acc, c) => {
  const last = acc[acc.length - 1]
  if (last && last.key === c.group) last.span++
  else acc.push({ key: c.group, label: GROUP_LABEL[c.group], span: 1 })
  return acc
}, [])

const market = ref('')
// 全市场 5500 只规模下“翻页”不实用:板块/行业/ST 作为基本维度先缩小范围
const BOARDS = [['', '全部板块'], ['shMain', '沪主'], ['szMain', '深主'], ['gem', '创业'], ['star', '科创'], ['bj', '北交']]
const board = ref('')
const industry = ref('')
// 排除行业（多选，与上面的单选包含是 AND）：行业是上百项的长尾维度，逐个"包含"看不现实，
// 一次摘掉几个不关心的才是常用路径。展开态与搜索词只服务这个控件本身。
const exIndustries = ref([])
const exOpen = ref(false)
const exKw = ref('')
const noSt = ref(false)
// 硬门槛（接口 gate / gate_flags）：命中「审计非标 / 简称 ST / 立案处罚 / 资不抵债」这类定性事实。
// 默认不拦：列表页是排序工具，默默替用户改筛选结果比多一个红标更糟；而且 gate 为 NULL
//（一个信号都判不了）的标的在勾选时会连同门排除，默认不勾才不会让人误以为「剩下的都干净」。
const noGate = ref(false)
const GATE_TIP = '排除触发硬门槛的标的：最近一份年报的审计意见非标准无保留 / 简称含 ST / *ST（交易所风险警示）/ Wind 有违规立案处罚记录 / 最新年报归母权益为负。'
  + '这四类是定性否决，不进任何分数（四派分照旧），所以触发者出现在高分前排并不罕见（实测 344 家触发，其中 24 家四派最高分 ≥ 70）。'
  + '注意：本开关只保留「明确未触发」的标的，一个信号都判不了的（未抓财务、无审计也无权益数据）会一并被排除'
const industries = ref([])
// 细分（Phase 3）：主营构成词典归属，仅 A 股有值；选项在 loadIndustries 时一并拉取
const niches = ref([])
const niche = ref('')
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

// 筛选(语义同原版):造假≤/管理≥/买点多选×折扣%/卖点多选(现价≥公允卖价即命中,公允恒高于保守);空值=不限
// 规模相关:剔除ST(低 PB 假便宜的重灾区)、行业单选(全市场几十个字)、市值区间(本币亿)、净现金/市值区间(%)、PB 十年分位区间(0~100)
const SCHOOLS = [['grahamAgg', '格进取'], ['grahamDef', '格防御'], ['schloss', '施洛斯'], ['buffett', '巴菲特']]
const flt = reactive({ fraudMax: '', mgmtMin: '', recMin: '', capMin: '', capMax: '', ncrMin: '', ncrMax: '', pbpMin: '', pbpMax: '', ageMax: '', peMin: '', peMax: '', pbMin: '', pbMax: '', divMin: '', buys: [], discount: '', sells: [], bm: [] })
const gateFlag = ref('')
// 评分基准报告期距今多少月（接口 report_age_max）：四派分永远建在最新年报上，那一期越旧分越旧。
// 阈值取 13 月：上一年 12-31 的年报最迟 12 个月龄，超出去就是再上一年度。
const AGE_TIP = '评分用的财报期（最新年报）距今 ≤ 多少月：填 13 就是只要「基准年报还是上一年度」的公司；'
  + '超出的那些是新一期年报还没披露（或还没回灌），四派分与买卖参考价都建在再上一年的财报上。留空不限；'
  + '美股有几个财年不在 12-31 收尾的公司（Copart/Cisco/Intuit 那类，实测 7 家），采集层把期次归一到 12-31，'
  + '它们的基准期会显示成未来期（期龄负数），不是算错，也不会被这个门槛误拦（门槛只卡上限）'
// 市值框让用户填“亿”(与列头 市值(亿) 同口径),发请求时转回接口单位元：
// 接口接受本币元、响应 market_cap 也是本币元,两处同一单位才能拿着返回值直接核对边界。
// Math.round 而非直乘：1.1 * 1e8 = 110000000.00000001 这种尾差会把恰好在边上的票顶出筛选。
const capYiToYuan = (v) => Math.round(Number(v) * 1e8)
// 不折成人民币：汇率源未落地,拿估算值折算只会让人误以为是可比口径
const CAP_TIP = '总市值区间（单位：亿，按各市场本币计价——A股人民币、港股港元、美股美元，不折算）：'
  + '“全部”tab 下三个市场混在一起比数值没有意义，要跨市场比体量请切到对应市场 tab 分开筛；'
  + '无最新行情的公司（市值列显示 -）不进区间，设任一门槛即被排除'
// 净现金/市值框填百分数（与列面 8.5% 一致），接口与库里都是小数比率，所以只在发请求前折一次。
// 别照抄 capYiToYuan 的 Math.round：比率是 0.x，取整会把每个 <50% 的门槛压成 0、≥50% 压成 1，
// 整列筛选静默变成“净现金≥0”而毫无报错。也不能一律 /100：实测两位小数的百分数里约 27%
// 会差 1 ulp（-199.98/100 = -1.9997999999999998），把十进制串成字面量才精确落到目标 double。
// 串字面量这招对自带指数的数会失效（框绑的是 number，1e-7 串出来是 "1e-7"，拼成 "1e-7e-2" → NaN，
// 实测真会把 ncr_min=NaN 发出去），故那种量级退回除法——尾差在 1e-9 上，筛出来的还是同一批。
const pctToRatio = (v) => {
  const n = Number(`${v}e-2`)
  return Number.isNaN(n) ? Number(v) / 100 : n
}
const NCR_TIP = '净现金/市值区间（单位：%）：≥0 表示加权类现金已够覆盖全部负债（类现金里的定期存款按 1.0 计、受限货币资金不计，两者读财报附注），≥100% 才是格雷厄姆意义上的 net-net'
  + '（全市场仅个位数且全在美股，A股 tab 下填 100 必为空）；负数是净负债的真实值，最深实测到 -8768%，'
  + '集中在预收/合同负债庞大的地产建筑与 AMC，所以“≤0”捞到的不是便宜的反面；'
  + '算不出这列的公司（列面显示 -，实测 126 家）不进区间，设任一门槛即被排除'
// 这一列取值本身的悬浮说明：桌面表格格与移动端卡片徽标共用，不写成两份
const NCR_CELL_TIP = '净现金/市值（最近一期财报），≥100% 表示扣除全部负债后的类现金仍高于市值；'
  + '其他流动资产里的定期存款与货币资金里的受限部分按财报附注拆分折算，逐家代入式见详情页 ① 快照的该格'
// PB 十年分位：口径整份在 Wind 那边（窗口长度、样本是否含极早期点、亏损期计不计），我们无法复算也不去复算，
// 所以两处文案都直说「Wind 口径」，并且这一列不参与四派评分与买卖点，只用来筛与看。
const PB_TIP = 'PB 近十年分位（Wind 口径，0~100，越低越接近十年低位）：分位由 Wind 按自己的窗口与样本算，'
  + '我们只负责取回与置空（PB 为负或算不出、外源日期过旧的都置空，实测亏损/停牌股会置空）；'
  + '未覆盖或已置空的公司（列面显示 -）不进区间，设任一门槛即被排除；'
  + '按口径不刷的标的一直是 -：美股整市场不刷，A 股/港股里管理分 < 30 或造假分 > 50 的不刷，'
  + '其余才是按游标分轮铺，具体截止日看格内悬浮说明'
// 格内一位小数 + %：库里存的就是 0~100 的分位数，不能再走 score2（那套是 0~1 比率口径，会多乘 100）
const pbCell = (s) => (s.pb_pctile == null ? '-' : fmt(s.pb_pctile, 1) + '%')
const pbCellTip = (s) => 'PB 近十年分位（Wind 口径）：十年内 PB 低于当前值的交易日占比 '
  + (s.pb_pctile == null ? '（无值）' : fmt(s.pb_pctile, 1) + '%')
  + '，样本 ' + (s.pb_days == null ? '-' : s.pb_days) + ' 个交易日'
  + (data.value?.valuation_date ? '，外源观测日 ' + data.value.valuation_date : '')
  + '；点击列头可按分位排序，此列仅展示与筛选，不进四派评分'

const ym = (v) => (v ? String(v).slice(0, 7) : '-')
const qty = (n) => n == null ? '-' : n >= 1e8 ? fmt(n / 1e8) + '亿' : n >= 1e4 ? fmt(n / 1e4) + '万' : fmt(n, 0)
const rng = (f, lo, hi) => hi == null ? (lo == null ? '-' : '≥' + f(lo))
  : lo == null ? '≤' + f(hi) : lo === hi ? f(hi) : f(lo) + '~' + f(hi)
const seoTip = (s) => {
  const v = s.actions?.seo
  if (!v) return '未收录定增记录或不在覆盖范围\n' + SEO_TIP
  return `最近一次定增：${v.date || '-'}，发行价 ${fmt(v.price, 4)} 元/股，发行量 ${fmt(v.num, 0)} 股，募集资金 ${fmt(v.amount)} 元\n${SEO_TIP}`
}
function buyRow(tag, b) {
  return {
    tag, date: ym(b.date), full: b.date || '-',
    price: (b.price != null && !b.price_actual ? '≤' : '') + fmt(b.price),
    kind: b.price == null ? '缺失' : b.price_actual ? '成交均价' : '方案上限',
    num: b.num_actual ? qty(b.num) : '拟' + rng(qty, b.num_lo, b.num),
    numFull: b.num_actual ? fmt(b.num, 0) : '计划 ' + rng((v) => fmt(v, 0), b.num_lo, b.num),
    amount: b.amount_actual ? fmt(b.amount) : '计划 ' + rng(fmt, b.amount_lo, b.amount),
    cx: b.cancel_type || '待核', progress: b.progress || '-',
  }
}
function buyRows(s) {
  const a = s.actions
  if (!a) return []
  const latest = a.buy_latest, done = a.buy_done
  // 同一方案保留完成日，避免把公告日误当完成日。
  if (latest && done && done.src_id === latest.src_id) return [buyRow('完', done)]
  return [...(latest ? [buyRow('新', latest)] : []), ...(done ? [buyRow('完', done)] : [])]
}
const buyTip = (s) => {
  const rows = buyRows(s)
  if (!rows.length) return '未收录回购记录或不在覆盖范围\n' + BUY_TIP
  return rows.map((r) => `${r.tag === '完' ? '最近已完成' : '最新公告'}：${r.progress}｜${r.full}`
    + `｜价 ${r.price} 元/股（${r.kind}）｜量 ${r.numFull} 股｜额 ${r.amount} 元｜用途 ${r.cx}`
  ).join('\n') + '\n' + BUY_TIP
}

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
const TRAP_TIP = '价值陷阱分 T（0-100，越高越可疑）：把「扣非撑不起报告净利、商誉占比高、ROE/毛利率减速、利润兑不出钱、造假红旗高、近 5 年定增摊薄」七类各自亮灯的坏消息，'
  + '按 A 股回测出的危险比加权成合成分。分母固定为 7 项，所以缺项只压低分数——低分不等于没风险，看格子右上角「可判项数/7」。'
  + '口径只覆盖 A 股，港美股显示 -（不适用，不是查过没毛病）。各档实测发生率见详情页「价值陷阱分」一节。'
// 档位边界按 C（未归一的证据合计）查，不按显示分——分四舍五入到一位小数后压在边界上会串档。
// 镜像 stockLegacy.js 的 TRAP_BANDS（列表页不加载那个库，沿本页 gradeOf 的既有做法在此复述切点）；
// 改那边要同步这边。发生率表刻意不复述，只在详情页给，避免两处各写一份数字。
const TRAP_BAND_CUTS = [[0.001, '档1', 'good'], [0.5, '档2', 'mid'], [1.0, '档3', 'mid'], [1.6, '档4', 'low']]
const V_TIP = '价值综合分 V（0-100，越高越便宜且不是烂账）：每股账面价的边际（PB 倒数减近 5 年 ROE 中位）、收益率 EP、现金收益率、ROE 近 5 年中位、有息负债率、经营现金流/净利、连续分红年数，七项加权。'
  + '分母固定为 100 权重，缺项只压低分数——低分不等于贵，看格子右上角「可判项数/7」。'
  + '前三项要吃市值，行情没返市值的标的那三项一起判不动，剩下的质量分照样能给出一个看着不错的数，所以覆盖项数必须跟分数一起看。各档实测结局见详情页「价值综合分」一节。'
// 档位切点镜像 stockLegacy.js 的 V_BANDS（列表页不加载那个库，沿本页既有做法在此复述切点），改那边要同步这边。
// 与 G 同形：查的就是那个舍入到一位小数的显示分本身，两页用同一个值所以不会各说一套。
// 各档实测结局刻意不复述，只在详情页给，避免两处各写一份数字。
const V_BAND_CUTS = [[19.3, '档1 价值最低', 'bad'], [26.7, '档2 偏低', 'low'], [34.0, '档3 中位', 'mid'], [45.9, '档4 偏高', 'mid']]
const G_TIP = '成长综合分 G（0-100，越高越好）：净利/营收/每股净资产各自的近 5 年年化增速、ROE 近 5 年中位与其趋势、近 5 年净利负增长年数、增长加速度，七项加权。'
  + '分母固定为 100 权重，缺项只压低分数——低分不等于没增长，看格子右上角「可判项数/7」。'
  + '七项只用公开年报，各市场都有分（不像陷阱分只覆盖 A 股）；档位与该档实测的未来增速来自 A 股回测，数字见详情页「成长综合分 G」一节。'
// 档位切点镜像 stockLegacy.js 的 G_BANDS（列表页不加载那个库，沿本页既有做法在此复述切点），改那边要同步这边。
// 与陷阱分不同：这里查的就是那个舍入到一位小数的显示分本身，详情页 gBandOf 用的是同一个值，所以两页不会各说一套。
// 各档实测未来增速刻意不复述，只在详情页给，避免两处各写一份数字。
const G_BAND_CUTS = [[16.0, '档1 增长最弱', 'bad'], [28.3, '档2 偏低', 'low'], [47.7, '档3 中位', 'mid'], [69.0, '档4 偏高', 'mid']]
const R_TIP = '综合推荐分 R（0-100，越高越好）：门槛外的 0.6×价值综合分 V + 0.4×成长综合分 G。'
  + '三道发分门槛——造假 ≤ 40 · 陷阱 ≤ 20 · 中报恶化 dip ≤ −70%（判不动的门槛输入按无证据放行，港美股陷阱/扣非缺失侧 dip 不适用）。'
  + '门槛外的公司显示 -，那是「不过资格线」不是 0 分；悬停可见具体原因（fraud/trap/interim/nodata）。'
  + '回测（A 股 2021~2023 事件时面板）：门槛内按无价格版五分位，其后转亏率 16.0%→2.4% 单调、减值≥5% 26.8%→3.3% 单调、逐年 3/3。'
  + '收益侧实测为负向（高分组 2 年超额跑输 10.1pp，2022~2024 质量/成长风格杀跌）——R 是排雷与选质的入口，不是买点。'
// 档位切点镜像 stockLegacy.js 的 R_BANDS（回测面板 R_full 五分位 24/36/46/56 取整），改那边要同步这边。
const R_BAND_CUTS = [[24, '档1 最差', 'bad'], [36, '档2 偏低', 'low'], [46, '档3 中位', 'mid'], [56, '档4 偏高', 'mid']]
const R_GATE_LABEL = {
  fraud: '造假红旗分 > 40，被资格线拦下',
  trap: '价值陷阱分 > 20，被资格线拦下',
  interim: '中报恶化：最新一期季报/半年报扣非同比 ≤ −70%，被资格线拦下（回测转亏 lift 7.8×）',
  nodata: '门槛已过，但 V/G 至少一条判不动（公开年报不足 3 期）',
}
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

// 硬门槛与财报期龄都只做「标注」，不参与排序与着色；名字取自接口的 gate_flags
const GATE_TEXT = {
  audit_qualify: '最近一份年报的审计意见非标准无保留',
  risk_warning: '简称含 ST / *ST（交易所风险警示）',
  case_filed: 'Wind 事件里有违规/立案/处罚记录',
  neg_equity: '最新年报归母股东权益为负（资不抵债）',
}
// 中报恶化徽标：评分轴只吃年报，这里标注「最新中报/季报扣非同比 ≤ −30%」的标的
// （600866 星湖科技式盲区：年报好、中报塌，分数要等下一次年报才会反映）
const DIP_TIP = '中报恶化：最新一期季报/半年报的扣非（缺则净利）同比 ≤ −30%。' +
  '评分轴只读年报（12-31），年报披露后的经营恶化不进任何分数——徽标只是把库里已有的' +
  'interim 数据亮出来，不改分数；下一次年报披露后的深抓才会让分数反映它。'
const dipOf = (s) => s.interim_dip == null ? null : (s.interim_dip <= -0.3 ? Math.round(s.interim_dip * 100) : null)
// 净现金三件套的悬停口径（列在资产组，单位本币亿；与「净现金/市值」比值列的口径差是重点）
const WCASH_TIP = '加权类现金（最新一期财报，本币亿）：货币资金(扣受限)×1.0 ＋ 交易性金融资产×0.7 ＋ 应收票据×0.4 ＋ 其他流动资产非存款部分×0.3 ＋ 定期存款×1.0（定期存款/受限来自财报附注，闭合才采信）。'
const IDEBT_TIP = '有息负债（最新一期财报，本币亿）：短期借款 ＋ 一年内到期的非流动负债 ＋ 长期借款 ＋ 应付债券 ＋ 租赁负债。'
const NCW_TIP = '净现金(扣有息) ＝ 加权类现金 − 有息负债（本币亿），回答「活钱够不够还有息债」。注意与右侧「净现金(减全部负债)」不是同一个数：那一列减的是负债合计（含应付款/预收），回答「净资产缓冲」——两列都对，别拿一处的数核对另一处。'
const NCB_TIP = '净现金(减全部负债) ＝ 加权类现金 − 负债合计（本币亿，最新一期财报），回答「净资产缓冲」。负债合计含应付款/预收等无息经营负债——制造业/链主企业此列深负是营运模式（华域汽车 −882.8 亿里 1,084 亿是经营负债），不是偿债问题。占市值百分比与 net-net（≥100%，全市场仅个位数）看筛选「净现金/市值」或详情页 ①。'
function gateTip(s) {
  const f = (s.gate_flags || []).map((k) => GATE_TEXT[k] || k)
  return '触发硬门槛：' + (f.join('、') || '（后端未给出行因）')
    + '。这类定性否决不进任何分数，四派分照旧；要排掉请勾筛选栏的「排除门槛」'
}
// 期龄超 13 月才标：没超标的就是正常路径（年报当年 4 月披露、下半年读到 8~12 月龄），标出来只会成屏噪声
const STALE_MONTHS = 13
const staleOf = (s) => (s.report_age_months == null || s.report_age_months <= STALE_MONTHS ? null : s.report_age_months)
function ageTip(s) {
  const m = s.report_age_months
  if (m == null) return '评分基准报告期未知（这一行的评分行缺失或旧于本次口径）'
  // 月数是相对这一行自己的快照日算的（美股会比顶部全局快照日旧一天），不报出来就像算错了
  return `评分用的是 ${s.report_date} 年报（距本行快照日 ${s.score_date} ${m} 个月）`
    + (m > STALE_MONTHS ? '：新一期年报还没出或还没回灌，四派分与买卖参考价都建在这份旧财报上' : '')
}

function toggleFlt(arr, key, on) {
  const i = arr.indexOf(key)
  if (on && i < 0) arr.push(key)
  if (!on && i >= 0) arr.splice(i, 1)
}
function resetFlt() {
  Object.assign(flt, { fraudMax: '', mgmtMin: '', recMin: '', capMin: '', capMax: '', ncrMin: '', ncrMax: '', pbpMin: '', pbpMax: '', ageMax: '', peMin: '', peMax: '', pbMin: '', pbMax: '', divMin: '', buys: [], discount: '', sells: [], bm: [] })
  gateFlag.value = ''
  industry.value = ''
  niche.value = ''
  exIndustries.value = []
  exKw.value = ''
  noSt.value = false
  noGate.value = false
  applyFlt()
}
function applyFlt() {
  if (page.value !== 1) page.value = 1  // watch 会触发 load;首页时需手动
  else load()
}
const fltCount = () =>
  (flt.fraudMax !== '' ? 1 : 0) + (flt.mgmtMin !== '' ? 1 : 0) +
  (flt.capMin !== '' ? 1 : 0) + (flt.capMax !== '' ? 1 : 0) +
  (flt.ncrMin !== '' ? 1 : 0) + (flt.ncrMax !== '' ? 1 : 0) +
  (flt.pbpMin !== '' ? 1 : 0) + (flt.pbpMax !== '' ? 1 : 0) +
  (flt.ageMax !== '' ? 1 : 0) +
  (flt.buys.length ? 1 : 0) + (flt.sells.length ? 1 : 0) + (flt.bm.length ? 1 : 0) +
  ((flt.recMin !== '' ? 1 : 0) + (flt.peMin !== '' ? 1 : 0) + (flt.peMax !== '' ? 1 : 0) +
   (flt.pbMin !== '' ? 1 : 0) + (flt.pbMax !== '' ? 1 : 0) + (flt.divMin !== '' ? 1 : 0) +
   (gateFlag.value ? 1 : 0)) +
  (industry.value ? 1 : 0) + (exIndustries.value.length ? 1 : 0) + (noSt.value ? 1 : 0) + (noGate.value ? 1 : 0)

// 勾选清单过搜索词。只数保持 /securities/industries 的原生顺序（count 降序），
// 大行业排在前面正是排除操作想要的顺序，不另排。
const exChoices = computed(() => {
  const kw = exKw.value.trim()
  return kw ? industries.value.filter((i) => i.industry.includes(kw)) : industries.value
})
function clearEx() {
  exIndustries.value = []
  applyFlt()
}

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
      niche: niche.value || null,
      bm: flt.bm.length ? flt.bm.join(',') : null,
      recommend_min: flt.recMin === '' ? null : Number(flt.recMin),
      gate_flag: gateFlag.value || null,
      pe_min: flt.peMin === '' ? null : Number(flt.peMin),
      pe_max: flt.peMax === '' ? null : Number(flt.peMax),
      pb_min: flt.pbMin === '' ? null : Number(flt.pbMin),
      pb_max: flt.pbMax === '' ? null : Number(flt.pbMax),
      div_min: flt.divMin === '' ? null : Number(flt.divMin),
      ex_industry: exIndustries.value.length ? exIndustries.value.join(',') : null,
      st: noSt.value ? false : null,
      gate: noGate.value ? false : null,
      // 不换算：接口收的就是「月」，与列面标注同一口径
      report_age_max: flt.ageMax === '' ? null : flt.ageMax,
      keyword: kwDebounced.value,
      fraud_max: flt.fraudMax === '' ? null : flt.fraudMax,
      mgmt_min: flt.mgmtMin === '' ? null : flt.mgmtMin,
      cap_min: flt.capMin === '' ? null : capYiToYuan(flt.capMin),
      cap_max: flt.capMax === '' ? null : capYiToYuan(flt.capMax),
      ncr_min: flt.ncrMin === '' ? null : pctToRatio(flt.ncrMin),
      ncr_max: flt.ncrMax === '' ? null : pctToRatio(flt.ncrMax),
      // 不换算：框里就是 0~100 的分位数，接口 pbp_* 同一数域，多乘一道只会引入尾差
      pbp_min: flt.pbpMin === '' ? null : flt.pbpMin,
      pbp_max: flt.pbpMax === '' ? null : flt.pbpMax,
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
  if (market.value === '' || market.value === 'A') {
    try { niches.value = await get('/securities/niches', { min_count: 3 }) }
    catch (e) { niches.value = [] }
  } else {
    niches.value = []
  }
}

// 必须注册在下面 load 的 watch 之前：切市场先把已选行业清掉，同一轮里触发的 load 才带着空行业去请求
// 排除项同理且更必须——三个市场是三套字典（A 国标 / 港股恒生 / 美股东财中文），带过去的名字新市场里不存在
watch(market, () => {
  industry.value = ''
  niche.value = ''
  exIndustries.value = []
  exKw.value = ''
  loadIndustries()
})

// kwDebounced 必须在依赖里：搜索框原本只靠下面防抖回调里的 page=1 间接触发刷新，
// 而搜索时通常已在第一页，页码不变 → watch 不触发 → 输入了也没发请求（applyFlt 同坑）。
// windMode 同理：它是整列口径的开关，不在依赖里就会看到“点了没反应”的老毛病。
watch([market, board, sort, order, page, kwDebounced, windMode, niche, gateFlag], load)
watch(pageSize, load)
// 状态写入 hash query（防抖 replace 不产生历史；只写非默认值，URL 保持可读）。
// 刷新/分享/收藏即恢复——这是筛选状态唯一的持久化出口。
let routeTimer = null
watch([market, board, industry, niche, kwDebounced, noSt, noGate, windMode, sort, order, page,
       () => JSON.stringify(flt)], () => {
  clearTimeout(routeTimer)
  routeTimer = setTimeout(() => {
    const q = {}
    if (market.value) q.market = market.value
    if (board.value) q.board = board.value
    if (industry.value) q.ind = industry.value
    if (niche.value) q.niche = niche.value
    if (kwDebounced.value) q.kw = kwDebounced.value
    if (noSt.value) q.nost = '1'
    if (noGate.value) q.nog = '1'
    if (flt.fraudMax !== '') q.fm = flt.fraudMax
    if (flt.mgmtMin !== '') q.mm = flt.mgmtMin
    if (flt.capMin !== '') q.cmin = flt.capMin
    if (flt.capMax !== '') q.cmax = flt.capMax
    if (flt.ncrMin !== '') q.nmin = flt.ncrMin
    if (flt.ncrMax !== '') q.nmax = flt.ncrMax
    if (flt.pbpMin !== '') q.pmin = flt.pbpMin
    if (flt.pbpMax !== '') q.pmax = flt.pbpMax
    if (flt.ageMax !== '') q.age = flt.ageMax
    if (flt.recMin !== '') q.rmin = flt.recMin
    if (gateFlag.value) q.gf = gateFlag.value
    if (flt.peMin !== '') q.pemin = flt.peMin
    if (flt.peMax !== '') q.pemax = flt.peMax
    if (flt.pbMin !== '') q.pbmin = flt.pbMin
    if (flt.pbMax !== '') q.pbmax = flt.pbMax
    if (flt.divMin !== '') q.dmin = flt.divMin
    if (flt.buys.length) q.buys = flt.buys.join(',')
    if (flt.discount !== '' && flt.buys.length) q.disc = flt.discount
    if (flt.sells.length) q.sells = flt.sells.join(',')
    if (flt.bm.length) q.bm = flt.bm.join(',')
    if (sort.value !== 'code') q.sort = sort.value
    if (order.value !== 'desc') q.order = order.value
    if (page.value > 1) q.page = String(page.value)
    if (windMode.value) q.wind = '1'
    router.replace({ query: q }).catch(() => {})
  }, 300)
})

watch(keyword, (v) => {
  clearTimeout(setSort._t)
  setSort._t = setTimeout(() => { kwDebounced.value = v.trim(); page.value = 1 }, 300)
})

// —— URL 状态持久化（Phase：筛选/排序写进 hash query，刷新/分享/收藏即恢复）——
// 恢复必须在首次 load 之前：onMounted 里的 load() 拿到的就是恢复后的完整状态
{
  const q = route.query
  const s = (k) => (typeof q[k] === 'string' ? q[k] : '')
  if (['', 'A', 'HK', 'US'].includes(q.market)) market.value = q.market
  if (BOARDS.some(([k]) => k === q.board)) board.value = q.board
  if (s('ind')) industry.value = s('ind')
  if (s('niche')) niche.value = s('niche')
  if (s('kw')) { keyword.value = s('kw'); kwDebounced.value = s('kw') }
  noSt.value = s('nost') === '1'
  noGate.value = s('nog') === '1'
  flt.fraudMax = s('fm'); flt.mgmtMin = s('mm')
  flt.capMin = s('cmin'); flt.capMax = s('cmax')
  flt.ncrMin = s('nmin'); flt.ncrMax = s('nmax')
  flt.pbpMin = s('pmin'); flt.pbpMax = s('pmax')
  flt.ageMax = s('age')
  flt.recMin = s('rmin'); flt.gateFlag = s('gf')
  flt.peMin = s('pemin'); flt.peMax = s('pemax')
  flt.pbMin = s('pbmin'); flt.pbMax = s('pbmax')
  flt.divMin = s('dmin')
  flt.buys = s('buys').split(',').filter((x) => SCHOOLS.some(([k]) => k === x))
  flt.discount = s('disc')
  flt.sells = s('sells').split(',').filter((x) => SCHOOLS.some(([k]) => k === x))
  flt.bm = s('bm').split(',').filter((x) => ['light', 'pricing'].includes(x))
  if (SORT_NAME[q.sort]) sort.value = q.sort
  if (q.order === 'asc' || q.order === 'desc') order.value = q.order
  const pg = parseInt(q.page, 10)
  if (Number.isFinite(pg) && pg > 1) page.value = pg
  windMode.value = s('wind') === '1'
}

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
// 买入性价比：现价相对买入参考价的偏离，负数=已经比建议买价便宜。
// 后端 buy_* 与 fair_liq 排序键排的就是这个比率的相反数（折价率），响应里没有对应字段，
// 故这里用同一公式在前端算出来给 tooltip 和行内展示用——两边口径必须一致，
// 连 MIN_PRICE_REF 这道门槛也要一致，否则被后端判成无值的行会在前端显示出一个假折价。
const MIN_PRICE_REF = 0.01
const refSpace = (s, k) => {
  const b = refBuy(s, k)
  return b == null || b < MIN_PRICE_REF || s.price == null ? null : s.price / b - 1
}
// 清算列同理：格内是每股清算价值绝对值，排序用的是现价相对它的偏离
const liqSpace = (s) => (s.fair_liq == null || s.fair_liq < MIN_PRICE_REF || s.price == null
  ? null : s.price / s.fair_liq - 1)
// 折最深到 100% 有界，溢价无上界：999% 以上统一简写成 溢999%（精确值看悬停），
// 卡片那一栏只有 68px，多一个 > 就会把末尾的 % 挤出去。
const refSpaceText = (v) => v == null ? ''
  : v <= 0 ? '折' + (Math.abs(v) * 100).toFixed(0) + '%'
    : v >= 9.995 ? '溢999%' : '溢' + (v * 100).toFixed(0) + '%'
// 当前按哪一列的「买」在排序（排序键是 snake，流派键是驼峰）
const camel = (v) => v.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
const buySortSchool = computed(() => (sort.value.startsWith('buy_') ? camel(sort.value.slice(4)) : ''))
// 表头高亮:本流派列的买/保/公任一档被排序都算激活(同原站 thSort 把 sellC-/sellF- 归一到 buy-)
function sortActive(c) {
  if (!c.ref) return sort.value === c.key
  const s = snake(c.school)
  return ['buy', 'sell_cons', 'sell_fair'].some((k) => sort.value === `${k}_${s}`)
}
// 表头提示：买价与清算价值两档排的都是折价率，要说清「排序看比率、格内是绝对值」
function thTip(c) {
  // 静态口径说明（股本事件两列既不排序也不筛选，列头是唯一的解释入口）
  if (c.tip) return c.tip
  if (!c.key) return ''
  if (c.ref) return '按买入性价比排序：现价相对买入参考价的折价越深越靠前（保守/公允价点格内小字），再点切换升/降序'
  if (c.ratio) return '按清算性价比排序：现价相对每股清算价值折得越深越靠前（格内是清算价值本身），再点切换升/降序'
  if (c.trap) return TRAP_TIP + '｜点击按陷阱分排序（降序＝最可疑的在前），再点切换升/降序'
  if (c.value) return V_TIP + '｜点击按价值分排序（降序＝最便宜且有质量的在前），再点切换升/降序'
  if (c.growth) return G_TIP + '｜点击按成长分排序（降序＝过去五年更能长的在前），再点切换升/降序'
  return '点击排序，再点切换升/降序'
}
const refTitle = (s, k) => {
  const f = (v) => v == null ? '-' : fmt(v)
  const sp = refSpace(s, k)
  const tail = sp == null ? '' : `｜现价较买价${sp <= 0 ? '折价' : '溢价'} ${sp >= 9.995 ? '>999' : (Math.abs(sp) * 100).toFixed(1)}%`
  return `${REF_LABELS[k]}：买 ${f(refBuy(s, k))} / 保卖 ${f(refCons(s, k))} / 公卖 ${f(refFair(s, k))}${tail}`
}
const liqTitle = (s) => {
  const sp = liqSpace(s)
  const tail = sp == null ? '' : `｜现价较清算价值${sp <= 0 ? '折价' : '溢价'} ${sp >= 9.995 ? '>999' : (Math.abs(sp) * 100).toFixed(1)}%`
  return `公允清算价值估算：(流动资产合计-负债合计)/股本${tail}`
}
// 陷阱分格子：格内是分，右上角是「可判几项」，档位与着色按 C 查（见 TRAP_BAND_CUTS 的注释）
function bandOfC(c) {
  for (const [hi, label, grade] of TRAP_BAND_CUTS) if (c <= hi) return [label, grade]
  return ['档5', 'bad']
}
const trapGrade = (s) => s.trap_c == null ? 'na' : bandOfC(s.trap_c)[1]
function trapTitle(s) {
  if (s.trap_c == null) return TRAP_TIP + '｜本标的：不适用（非 A 股），不是「查过了没毛病」'
  const [label] = bandOfC(s.trap_c)
  return `${TRAP_TIP}｜本标的：${label}（证据合计 C ${s.trap_c.toFixed(2)}）· 七项里查得动 ${s.trap_eval} 项`
}
// 价值分格子：与成长分同一形状（格内分 + 右上角可判项数），档位同样是越高越好，兜底档给 good
function bandOfV(v) {
  for (const [hi, label, grade] of V_BAND_CUTS) if (v <= hi) return [label, grade]
  return ['档5 价值最高', 'good']
}
const vGrade = (s) => s.value == null ? 'na' : bandOfV(s.value)[1]
function vTitle(s) {
  if (s.value == null) return V_TIP + '｜本标的：七项全部判不动（公开年报不足 3 期，或年报有但市值与质量项一起取不到），不是「查过了不便宜」'
  const [label] = bandOfV(s.value)
  return `${V_TIP}｜本标的：${label}${s.value_eval == null ? '' : ' · 七项里查得动 ' + s.value_eval + ' 项'}`
}
// 成长分格子：与陷阱分同一形状（格内分 + 右上角可判项数），但档位是越高越好，所以兜底档给 good
function bandOfG(v) {
  for (const [hi, label, grade] of G_BAND_CUTS) if (v <= hi) return [label, grade]
  return ['档5 最强', 'good']
}
const gGrade = (s) => s.growth == null ? 'na' : bandOfG(s.growth)[1]
function gTitle(s) {
  if (s.growth == null) return G_TIP + '｜本标的：七项全部判不动（公开年报不足 3 期，或期次够但基期都取不到正的值），不是「查过了没长」'
  const [label] = bandOfG(s.growth)
  return `${G_TIP}｜本标的：${label}${s.growth_eval == null ? '' : ' · 七项里查得动 ' + s.growth_eval + ' 项'}`
}
// 推荐分格子：无分时按门槛原因给提示（`-` 要能区分「不过线」与「算不出」）
function bandOfR(v) {
  for (const [hi, label, grade] of R_BAND_CUTS) if (v <= hi) return [label, grade]
  return ['档5 最高', 'good']
}
const rGrade = (s) => s.recommend == null ? 'na' : bandOfR(s.recommend)[1]
// 组合门槛（fraud+interim 这类）按 '+ ' 拆开拼装，单门槛直接查表
function rGateText(g) {
  if (!g) return '原因未知'
  if (R_GATE_LABEL[g]) return R_GATE_LABEL[g]
  const parts = String(g).split('+').filter((k) => R_GATE_LABEL[k])
  return parts.length ? parts.map((k) => R_GATE_LABEL[k]).join('；') : '原因未知'
}
function rTitle(s) {
  if (s.recommend == null) {
    return R_TIP + '｜本标的：无 R（' + rGateText(s.recommend_gate) + '）'
  }
  const [label] = bandOfR(s.recommend)
  const ing = []
  if (s.value != null) ing.push('V ' + s.value.toFixed(1))
  if (s.growth != null) ing.push('G ' + s.growth.toFixed(1))
  return `${R_TIP}｜本标的：${label}${ing.length ? ' · ' + ing.join(' · ') : ''}`
}
// 移动卡片第二层：推荐格无分时的门槛原因速记（与详情 R 卡一致，缩写版）
const HERO_GATE = { fraud: '造假', trap: '陷阱', interim: '中报', nodata: '无数据' }
const heroGate = (s) => {
  const g = s.recommend_gate
  if (!g) return ''
  return String(g).split('+').map((k) => HERO_GATE[k] || k).join('+')
}
// 排序联动：当前排序字段在本行的值（挂在卡片第一层，折叠的指标排序时也能看到排序依据）
const SORT_VAL_FMT = {
  price: (s) => fmt(s.price) + (s.market !== 'A' ? s.currency : ''),
  pe_ttm: (s) => 'PE ' + fmt(s.pe_ttm),
  pb: (s) => 'PB ' + fmt(s.pb),
  market_cap: (s) => '市值 ' + yi(s.market_cap) + (s.market !== 'A' ? s.currency : ''),
  fraud: (s) => '造假 ' + score(dispScore(s, 'fraud')),
  mgmt: (s) => '管理 ' + score(dispScore(s, 'mgmt')),
  cycle: (s) => '周期 ' + score(s.cycle),
  trap: (s) => '陷阱 ' + score(s.trap),
  growth: (s) => '成长 ' + score(s.growth),
  value: (s) => '价值 ' + score(s.value),
  recommend: (s) => '推荐 ' + score(s.recommend),
  fair_liq: (s) => '清算 ' + fmt(s.fair_liq),
  net_cash_ratio: (s) => '净现金/市值 ' + score2(s.net_cash_ratio),
  pb_pctile: (s) => 'PB分位 ' + pbCell(s),
  w_cash: (s) => '加权现金 ' + yi(s.w_cash) + (s.market !== 'A' ? s.currency : ''),
  int_debt: (s) => '有息负债 ' + yi(s.int_debt) + (s.market !== 'A' ? s.currency : ''),
  net_cash_b: (s) => '净现金 ' + yi(s.net_cash_b) + (s.market !== 'A' ? s.currency : ''),
  score_graham_agg: (s) => '格进取 ' + score(s.score_graham_agg),
  score_graham_def: (s) => '格防御 ' + score(s.score_graham_def),
  score_schloss: (s) => '施洛斯 ' + score(s.score_schloss),
  score_buffett: (s) => '巴菲特 ' + score(s.score_buffett),
}
const sortValChip = (s) => (SORT_VAL_FMT[sort.value] ? SORT_VAL_FMT[sort.value](s) : null)
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

/* ---------------- 手机（≤600px）：卡片视图 + 折叠筛选/排序 ---------------- */

// 筛选栏一行约 17 个控件，手机上默认收起，只留一行摘要按钮；两个面板互斥，
// 同时展开会把首屏全部吃掉（同原内嵌页 fltOpen/sortOpen 的语义）
const fltsOpen = ref(false)
const sortOpen = ref(false)
function toggleFlts() {
  fltsOpen.value = !fltsOpen.value
  if (fltsOpen.value) sortOpen.value = false
}
function toggleSort() {
  sortOpen.value = !sortOpen.value
  if (sortOpen.value) fltsOpen.value = false
}

// 排序 chip 的键直接从 COLS 派生，与桌面表头可排序列一一对应，后端 sort= 白名单不会失配。
// 保守/公允卖价不进面板，靠卡片里点卖价小字触发（同桌面表格格内小字）。
// 按性价比排的两组（四派买价、清算价值）在名字上就标出来，免得看着像按绝对值排
const SORT_CHIPS = COLS.filter((c) => c.key)
  .map((c) => [c.key, c.ref ? c.label.split(' ')[0] + '折价' : c.ratio ? c.label + '性价比' : c.label])
const SORT_NAME = Object.fromEntries(SORT_CHIPS)

// 收起态按钮文案：把“已启用了哪些条件”直接写在按钮上，省得为了确认状态反复展开
const fltSummary = computed(() => {
  const parts = []
  if (flt.fraudMax !== '') parts.push('造假≤' + flt.fraudMax)
  if (flt.mgmtMin !== '') parts.push('管理≥' + flt.mgmtMin)
  if (flt.capMin !== '') parts.push('市值≥' + flt.capMin)
  if (flt.capMax !== '') parts.push('市值≤' + flt.capMax)
  // 带 % 而非裸数字：框里填的就是百分数，摘要省掉单位会被读成倍数（净现金≥12.9）
  if (flt.ncrMin !== '') parts.push('净现金≥' + flt.ncrMin + '%')
  if (flt.ncrMax !== '') parts.push('净现金≤' + flt.ncrMax + '%')
  if (flt.pbpMin !== '') parts.push('PB分位≥' + flt.pbpMin + '%')
  if (flt.pbpMax !== '') parts.push('PB分位≤' + flt.pbpMax + '%')
  if (flt.ageMax !== '') parts.push('财报期龄≤' + flt.ageMax + '月')
  if (flt.buys.length) parts.push(flt.buys.length + '个买点' + (flt.discount !== '' ? '×' + flt.discount + '%' : ''))
  if (flt.sells.length) parts.push(flt.sells.length + '个卖点')
  if (industry.value) parts.push(industry.value)
  // 排除项只报数量：行业名最长 20 字且可能同时排好几个，写在按钮上会把摘要撑成一行多
  if (exIndustries.value.length) parts.push('排除' + exIndustries.value.length + '行业')
  if (noSt.value) parts.push('剔除ST')
  if (noGate.value) parts.push('排除门槛')
  return parts.length ? '筛选：' + parts.join(' · ') : '筛选条件'
})
const sortSummary = computed(() => {
  const n = SORT_NAME[sort.value]
  return n ? '排序：' + n + (order.value === 'desc' ? ' ↓' : ' ↑') : '选择排序方式'
})

// 等级色阈值镜像 stockLegacy.js 的 gradeOf / fraudGradeOf；造假与周期是“越低越好”，走反向那套。
// 仅用于卡片着色，不参与任何计算。
const gradeOf = (v) => v == null ? 'na' : v >= 80 ? 'good' : v >= 60 ? 'mid' : v >= 40 ? 'low' : 'bad'
const fraudGradeOf = (v) => v == null ? 'na' : v < 20 ? 'good' : v < 40 ? 'mid' : v < 60 ? 'low' : 'bad'
const GRADE_TEXT = { good: '优秀', mid: '良好', low: '一般', bad: '较差', na: '数据不足' }
const FRAUD_GRADE_TEXT = { good: '低', mid: '中', low: '较高', bad: '高', na: '数据不足' }

// 四流派评分字段名沿用 refKey 的 snake 拼法（score_graham_agg 等），与接口字段同源不另写一份
const SCORE_CARDS = SCHOOLS.map(([k, lab]) => [refKey(k, 'score'), lab])
const REF_COLS = COLS.filter((c) => c.ref)
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

    <div v-if="isMobile" class="m-tgls">
      <button type="button" class="m-tgl" :class="{ on: fltsOpen }" :aria-expanded="fltsOpen" @click="toggleFlts">
        <span class="m-tgl-tx">{{ fltSummary }}</span><i class="tgl-arw" aria-hidden="true">▾</i>
      </button>
      <button type="button" class="m-tgl" :class="{ on: sortOpen }" :aria-expanded="sortOpen" @click="toggleSort">
        <span class="m-tgl-tx">{{ sortSummary }}</span><i class="tgl-arw" aria-hidden="true">▾</i>
      </button>
    </div>

    <div v-show="!isMobile || fltsOpen" class="flts">
      <button v-if="windVisible()" type="button" class="wind" :class="{ on: windMode }"
              title="切换造假/管理两列口径：基础财报分 ↔ Wind 事件增强分（基础分 + 一次性 Wind 事件增量，排序与造假≤/管理≥筛选同步跟随；Wind 档下无事件数据的公司不给分显示 -，切回基础档可看全部）"
              @click="toggleWind">事件增强分 <b>{{ windMode ? 'Wind' : '基础' }}</b></button>
      <label class="t">行业
        <select v-model="industry" @change="applyFlt">
          <option value="">全部</option>
          <option v-for="i in industries" :key="i.industry" :value="i.industry">{{ i.industry }}（{{ i.count }}）{{
            exIndustries.includes(i.industry) ? '（已排除）' : '' }}</option>
        </select></label>
      <label class="t" v-show="market === '' || market === 'A'">细分
        <select v-model="niche" @change="applyFlt">
          <option value="">全部</option>
          <option v-for="x in niches" :key="x.niche" :value="x.niche">{{ x.niche }}（{{ x.count }}）</option>
        </select></label>
      <button type="button" class="ex-t" :class="{ on: exIndustries.length }" :aria-expanded="exOpen"
              title="排除不关心的行业（多选）。与左侧「行业」下拉同时给出时按 AND 处理——选了它又排掉同一个行业，结果自然是空集。没有行业标注的公司不参与排除，始终保留"
              @click="exOpen = !exOpen">排除行业<span v-if="exIndustries.length">（{{ exIndustries.length }}）</span></button>
      <div v-show="exOpen" class="ex-box">
        <input v-model="exKw" class="ex-kw" type="search" placeholder="搜索行业名称" aria-label="搜索行业">
        <div class="ex-list">
          <label v-for="i in exChoices" :key="i.industry" class="cb"
                 :title="`排除「${i.industry}」——该行业在本市场共 ${i.count} 只（只数是本市场全量口径，不随其它筛选变化）`">
            <input type="checkbox" :checked="exIndustries.includes(i.industry)"
                   @change="toggleFlt(exIndustries, i.industry, $event.target.checked); applyFlt()">{{ i.industry
            }}<i class="ex-n">{{ i.count }}</i></label>
          <span v-if="!exChoices.length" class="ex-none">无匹配行业</span>
        </div>
        <div v-if="exIndustries.length" class="ex-tags">
          <span class="ex-lab">已排除</span>
          <button v-for="n in exIndustries" :key="n" type="button" class="ex-tag" :title="`不再排除「${n}」`"
                  @click="toggleFlt(exIndustries, n, false); applyFlt()">{{ n }} ×</button>
          <button type="button" class="ex-clear" @click="clearEx">清空排除</button>
        </div>
      </div>
      <label class="cb" title="名称含 ST/*ST 的公司（退市风险与财务造假高发区）"><input type="checkbox" v-model="noSt" @change="applyFlt">剔除ST</label>
      <label class="cb" :title="GATE_TIP"><input type="checkbox" v-model="noGate" @change="applyFlt">排除门槛</label>
      <select v-model="gateFlag" @change="applyFlt" class="t"
              title="硬门槛单项反查：命中该项目的公司。与左侧「排除门槛」（四类整体排除）互补——这里只看某一类">
        <option value="">门槛单项</option>
        <option value="audit_qualify">审计非标</option>
        <option value="risk_warning">含 ST</option>
        <option value="case_filed">违规/立案</option>
        <option value="neg_equity">资不抵债</option>
      </select>
      <label class="num" title="财报造假可能性(0-100,越高越可疑),只保留 ≤ 该分的公司">造假≤
        <input v-model="flt.fraudMax" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="管理层水平(0-100,越高越好),只保留 ≥ 该分的公司">管理≥
        <input v-model="flt.mgmtMin" type="number" min="0" max="100" step="1" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="综合推荐分 R ≥（0-100，越高越好）。横杆（无分）公司自动排除——门槛拦截与算不出都不进结果">推荐≥
        <input v-model="flt.recMin" type="number" min="0" max="100" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="PE(TTM) 区间：负数=亏损股的真实值，会命中 ≤ 区间（想要盈利低 PE 就叠加特征/质量筛选）">PE≥
        <input v-model="flt.peMin" type="number" placeholder="不限" @change="applyFlt"> ≤
        <input v-model="flt.peMax" type="number" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="PB 区间：负数=负净资产的真实值">PB≥
        <input v-model="flt.pbMin" type="number" placeholder="不限" @change="applyFlt"> ≤
        <input v-model="flt.pbMax" type="number" placeholder="不限" @change="applyFlt"></label>
      <label class="num" title="股息率 ≥（填 4 即 4%；近 1 年每股分红÷快照价；无分红数据的公司不进区间）">股息率≥
        <input v-model="flt.divMin" type="number" min="0" step="0.5" placeholder="不限" @change="applyFlt">%</label>
      <label class="num" :title="CAP_TIP">市值≥
        <input v-model="flt.capMin" type="number" min="0" step="0.5" placeholder="不限" @change="applyFlt"></label>
      <label class="num" :title="CAP_TIP">市值≤
        <input v-model="flt.capMax" type="number" min="0" step="0.5" placeholder="不限" @change="applyFlt"></label>
      <!-- 不写 min：这列负数是“净负债”的真实值，不是非法输入 -->
      <label class="num" :title="NCR_TIP">净现金/市值≥
        <input v-model="flt.ncrMin" type="number" step="5" placeholder="不限" @change="applyFlt">%</label>
      <label class="num" :title="NCR_TIP">净现金/市值≤
        <input v-model="flt.ncrMax" type="number" step="5" placeholder="不限" @change="applyFlt">%</label>
      <label class="num" :title="PB_TIP">PB十年分位≥
        <input v-model="flt.pbpMin" type="number" min="0" max="100" step="5" placeholder="不限" @change="applyFlt">%</label>
      <label class="num" :title="PB_TIP">PB十年分位≤
        <input v-model="flt.pbpMax" type="number" min="0" max="100" step="5" placeholder="不限" @change="applyFlt">%</label>
      <label class="num" :title="AGE_TIP">财报期龄≤
        <input v-model="flt.ageMax" type="number" min="0" max="60" step="1" placeholder="不限" @change="applyFlt">月</label>
      <span class="t" title="多选需同时满足:现价 ≤ 买价 × 折扣%">买点</span>
      <label v-for="[k, lab] in SCHOOLS" :key="'b' + k" class="cb">
        <input type="checkbox" :checked="flt.buys.includes(k)"
               @change="toggleFlt(flt.buys, k, $event.target.checked); applyFlt()">{{ lab }}</label>
      <label class="num disc" title="买点门槛 × 折扣%,如填 80 要求现价 ≤ 买价×80%,填 120 放宽到买价×120%;仅勾选买点后可用,留空等同 100%">打折
        <input v-model="flt.discount" type="number" min="0" max="500" step="1" placeholder="100"
               :disabled="!flt.buys.length" @change="applyFlt">%</label>
      <span class="t" title="多选需同时满足:现价 ≥ 公允卖价（公允恒高于保守卖价，达到公允即两档都过）">卖点</span>
      <label v-for="[k, lab] in SCHOOLS" :key="'s' + k" class="cb">
        <input type="checkbox" :checked="flt.sells.includes(k)"
               @change="toggleFlt(flt.sells, k, $event.target.checked); applyFlt()">{{ lab }}</label>
      <span class="t" title="商业模式特征多选（AND：所勾标签全部亮灯才命中）。轻资产＝capex÷经营现金流≤60%；定价权＝毛利率5年降幅≤3pp。回测（bm_validity，A 股 21,926 观测）两标签亮灯组其后转亏率显著更低；判不动的公司不命中，'-' 不是未亮灯">特征</span>
      <label class="cb" title="capex÷经营现金流 ≤60%（回测转亏 lift 0.57）"><input type="checkbox"
        :checked="flt.bm.includes('light')"
        @change="toggleFlt(flt.bm, 'light', $event.target.checked); applyFlt()">轻资产</label>
      <label class="cb" title="毛利率较 5 年前降幅 ≤3pp（回测转亏 lift 0.83）"><input type="checkbox"
        :checked="flt.bm.includes('pricing')"
        @change="toggleFlt(flt.bm, 'pricing', $event.target.checked); applyFlt()">定价权</label>
      <button type="button" class="rst" @click="resetFlt">重置筛选{{ fltCount() ? `(${fltCount()})` : '' }}</button>
      <!-- 手机没有 hover：桌面靠 title 才看得到的口径说明，触屏上必须常驻可见。
           文案复用上面的 FRAUD_TIP/MGMT_TIP/CAP_TIP/NCR_TIP，同一套解释不维护两份。 -->
      <p class="flts-hint">
        {{ FRAUD_TIP }}<br>
        {{ MGMT_TIP }}<br>
        {{ CAP_TIP }}<br>
        {{ NCR_TIP }}<br>
        {{ PB_TIP }}<br>
        买：现价 ≤ 买价×折扣；卖：现价 ≥ 公允卖价（公允恒高于保守卖价，达到公允即两档都过）；缺数据的公司自动排除<br>
        {{ GATE_TIP }}<br>
        {{ AGE_TIP }}<br>
        名称后的 ⚑ = 触发上述硬门槛（悬停看具体那条）；「期龄 N 月」= 评分用的年报期距今 N 个月，超 13 月才标<br>
        排除行业：勾中的行业整体从结果里摘掉（多选是「都排除」），与「行业」下拉同时用则是 AND；没有行业标注的公司不参与排除、始终保留
      </p>
    </div>

    <div v-if="isMobile && sortOpen" class="sorts">
      <button v-for="[k, lab] in SORT_CHIPS" :key="k" type="button" class="chip" :class="{ active: sort === k }"
              @click="setSort(k); sortOpen = false">
        {{ lab }}<template v-if="sort === k">{{ order === 'desc' ? ' ↓' : ' ↑' }}</template>
      </button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="loading && !data" class="loading">加载中…</div>

    <div v-if="data && !data.items.length && !loading" class="loading">
      无符合筛选条件的公司
      <button type="button" class="rst" style="margin-left:8px" @click="resetFlt">重置筛选</button>
    </div>
    <template v-else-if="data">
      <!-- 手机：卡片流，单指纵向滑、零横向拖动；桌面：原宽表，标记与样式一字未改 -->
      <div v-if="isMobile" class="stock-cards">
        <div v-for="s in data.items" :key="s.sid" class="stock-card" tabindex="0" role="link"
             @click="router.push(`/stock/${s.code}`)"
             @keyup.enter="router.push(`/stock/${s.code}`)">
          <!-- 第一层：身份与价格（首屏固定） -->
          <div class="sc-head">
            <span class="sc-name">{{ s.name }}</span>
            <span v-if="s.gate" class="gate-flag" :title="gateTip(s)">⚑</span>
            <span v-if="dipOf(s) != null" class="stale-flag dip-flag" :title="DIP_TIP + '｜本标的：' + dipOf(s) + '%'">中报{{ dipOf(s) }}%</span>
            <span v-if="staleOf(s)" class="stale-flag" :title="ageTip(s)">期龄{{ staleOf(s) }}月</span>
            <span class="sc-code">{{ s.code }}</span>
            <span v-if="s.market !== 'A'" class="badge">{{ MARKET_NAME[s.market] }}</span>
            <span class="sc-price" :class="cls(s.change_pct)">
              {{ fmt(s.price) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i>
              <b>{{ pct(s.change_pct) }}</b>
            </span>
          </div>
          <div class="sc-meta">
            <span class="sc-industry" :title="s.industry">{{ s.industry || '-' }}</span>
            <span v-if="s.niche" class="sc-niche" title="细分行业（主营构成词典归属）">{{ s.niche }}</span>
            <span v-if="sortValChip(s)" class="sc-sortval" :title="'当前排序字段：' + (SORT_NAME[sort] || sort)">{{ sortValChip(s) }}</span>
          </div>

          <!-- 第二层：决策格（入口分 R + 一坏一好的证据轴） -->
          <div class="sc-hero">
            <div class="hero-cell" :class="'sc-' + rGrade(s)" :title="rTitle(s)">
              <em>推荐</em>
              <b>{{ score(s.recommend) }}<i v-if="s.recommend == null && s.recommend_gate" class="hero-na">{{ heroGate(s) }}</i></b>
            </div>
            <div class="hero-cell" :class="'sc-' + trapGrade(s)" :title="trapTitle(s)">
              <em>陷阱</em><b>{{ score(s.trap) }}<i v-if="s.trap_eval != null" class="tp-ev">{{ s.trap_eval }}/7</i></b>
            </div>
            <div class="hero-cell" :class="'sc-' + vGrade(s)" :title="vTitle(s)">
              <em>价值</em><b>{{ score(s.value) }}<i v-if="s.value_eval != null" class="tp-ev">{{ s.value_eval }}/7</i></b>
            </div>
          </div>

          <!-- 第三层：其余全部指标，默认收起（点开不触发卡片跳转） -->
          <details class="sc-more" @click.stop>
            <summary>更多指标与买卖点</summary>
            <div class="sc-badges">
              <span class="sc-bd"><em>PE</em><b>{{ fmt(s.pe_ttm) }}</b></span>
              <span class="sc-bd"><em>PB</em><b>{{ fmt(s.pb) }}</b></span>
              <span class="sc-bd"><em>市值亿</em><b>{{ yi(s.market_cap) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></b></span>
              <span class="sc-bd" :class="'sc-' + fraudGradeOf(dispScore(s, 'fraud'))"
                    :title="windTip(s, 'fraud', FRAUD_TIP)">
                <em>造假</em><b>{{ score(dispScore(s, 'fraud')) }}</b></span>
              <span class="sc-bd" :class="'sc-' + gradeOf(dispScore(s, 'mgmt'))"
                    :title="windTip(s, 'mgmt', MGMT_TIP)">
                <em>管理</em><b>{{ score(dispScore(s, 'mgmt')) }}</b></span>
              <span class="sc-bd" :class="'sc-' + fraudGradeOf(s.cycle)"
                    :title="'周期位置（0-100，越低越接近周期底部）：' + FRAUD_GRADE_TEXT[fraudGradeOf(s.cycle)]">
                <em>周期</em><b>{{ score(s.cycle) }}</b></span>
              <span class="sc-bd" :class="'sc-' + gGrade(s)" :title="gTitle(s)">
                <em>成长</em><b>{{ score(s.growth) }}<i v-if="s.growth_eval != null" class="tp-ev">{{ s.growth_eval }}/7</i></b></span>
              <span class="sc-bd" :class="{ 'sc-good': s.net_cash_ratio != null && s.net_cash_ratio >= 1 }"
                    :title="NCR_CELL_TIP">
                <em>净现金/市值</em><b>{{ score2(s.net_cash_ratio) }}</b></span>
              <span class="sc-bd" :class="{ 'sc-good': s.pb_pctile != null && s.pb_pctile <= 20 }"
                    :title="pbCellTip(s)">
                <em>PB十年分位</em><b>{{ pbCell(s) }}</b></span>
              <div class="sc-act" :title="seoTip(s)"><em>定增</em>
                <span v-if="s.actions?.seo">{{ fmt(s.actions.seo.price) }} 元/股 · {{ s.actions.seo.date || '-' }} · {{ qty(s.actions.seo.num) }} 股</span>
                <span v-else>-</span>
              </div>
              <div class="sc-act" :title="buyTip(s)"><em>回购</em>
                <span v-for="r in buyRows(s)" :key="r.tag">{{ r.tag }} {{ r.price }} 元/股（{{ r.kind }}） · {{ r.full }} · {{ r.num }} 股</span>
                <span v-if="!buyRows(s).length">-</span>
              </div>
            </div>

            <div class="sc-scores">
              <div v-for="[k, lab] in SCORE_CARDS" :key="k" class="sc-score" :class="'sc-' + gradeOf(s[k])"
                   :title="GRADE_TEXT[gradeOf(s[k])]">
                <span class="sc-k">{{ lab }}</span>
                <span class="sc-v">{{ score(s[k]) }}</span>
              </div>
            </div>

            <div class="sc-refs">
              <div v-for="c in REF_COLS" :key="c.school" class="sc-ref" :title="refTitle(s, c.school)">
                <em>{{ REF_LABELS[c.school] }}<i v-if="buySortSchool === c.school && refSpace(s, c.school) != null" class="rf-sp">{{ refSpaceText(refSpace(s, c.school)) }}</i></em>
                <span class="r-buy" :class="{ 'r-hit': refBuy(s, c.school) != null && s.price != null && s.price <= refBuy(s, c.school) }">买 {{ fmt(refBuy(s, c.school)) }}</span>
                <span class="r-sell sl-sort"
                      :class="{ 'r-hit-s': refCons(s, c.school) != null && s.price != null && s.price >= refCons(s, c.school) }"
                      @click.stop="setSort(refKey(c.school, 'sellCons'))">保 {{ fmt(refCons(s, c.school)) }}</span>
                <span class="r-sell sl-sort"
                      :class="{ 'r-hit-s': refFair(s, c.school) != null && s.price != null && s.price >= refFair(s, c.school) }"
                      @click.stop="setSort(refKey(c.school, 'sellFair'))">公 {{ fmt(refFair(s, c.school)) }}</span>
              </div>
            </div>
          </details>
        </div>
      </div>

      <div v-else class="tbl-wrap">
      <table class="grid grid-list">
        <thead>
          <tr class="grp-row">
            <th v-for="g in COL_GROUPS" :key="g.key" :colspan="g.span"
                :class="['grp-' + g.key, { stick: g.key === 'id' }]">{{ g.label }}</th>
          </tr>
          <tr>
            <th v-for="c in COLS" :key="c.label" :class="{ l: c.l, unsort: !c.key, stick: c.stick, gedge: c.edge }"
                :title="thTip(c)"
                @click="c.key && setSort(c.key)">
              {{ c.label }}<i v-if="c.sub" class="th-sub">{{ c.sub }}</i><template v-if="sortActive(c)">{{ order === 'desc' ? ' ▼' : ' ▲' }}</template>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in data?.items" :key="s.sid" @click="router.push(`/stock/${s.code}`)">
            <td class="l stick name-cell">
              <div class="nm-row"><b>{{ s.name }}</b><span class="badge">{{ MARKET_NAME[s.market] }}</span><span class="cd">{{ s.code }}</span></div>
              <div class="nm-sub">
                <span class="ind" :title="s.industry">{{ s.industry || '-' }}</span>
                <span v-if="s.gate" class="gate-flag" :title="gateTip(s)">⚑</span>
                <span v-if="dipOf(s) != null" class="stale-flag dip-flag" :title="DIP_TIP + '｜本标的：' + dipOf(s) + '%'">中报{{ dipOf(s) }}%</span>
                <span v-if="staleOf(s)" class="stale-flag" :title="ageTip(s)">期龄{{ staleOf(s) }}月</span>
              </div>
            </td>
            <!-- 币种角标：港股/美股的现价与市值是本币（HKD/USD），跟 A 股人民币数值直接比大小会误读 -->
            <td class="gedge">{{ fmt(s.price) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :class="cls(s.change_pct)">{{ pct(s.change_pct) }}</td>
            <td>{{ fmt(s.pe_ttm) }}</td>
            <td>{{ fmt(s.pb) }}</td>
            <td>{{ yi(s.market_cap) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td class="gedge">{{ score(s.score_graham_agg) }}</td>
            <td>{{ score(s.score_graham_def) }}</td>
            <td>{{ score(s.score_schloss) }}</td>
            <td>{{ score(s.score_buffett) }}</td>
            <td class="gedge" :title="windTip(s, 'fraud', FRAUD_TIP)">{{ score(dispScore(s, 'fraud')) }}</td>
            <td :title="windTip(s, 'mgmt', MGMT_TIP)">{{ score(dispScore(s, 'mgmt')) }}</td>
            <td>{{ score(s.cycle) }}</td>
            <td :class="'sc-' + trapGrade(s)" :title="trapTitle(s)">{{ score(s.trap) }}<i v-if="s.trap_eval != null" class="tp-ev">{{ s.trap_eval }}/7</i></td>
            <td :class="'sc-' + vGrade(s)" :title="vTitle(s)">{{ score(s.value) }}<i v-if="s.value_eval != null" class="tp-ev">{{ s.value_eval }}/7</i></td>
            <td :class="'sc-' + gGrade(s)" :title="gTitle(s)">{{ score(s.growth) }}<i v-if="s.growth_eval != null" class="tp-ev">{{ s.growth_eval }}/7</i></td>
            <td :class="'sc-' + rGrade(s)" :title="rTitle(s)">{{ score(s.recommend) }}</td>
            <td class="c-liq gedge" :class="{ 'r-hit': s.fair_liq != null && s.price != null && s.price <= s.fair_liq }"
                :title="liqTitle(s)">{{ fmt(s.fair_liq) }}<i v-if="sort === 'fair_liq' && liqSpace(s) != null" class="rf-sp">{{ refSpaceText(liqSpace(s)) }}</i></td>
            <td :title="WCASH_TIP">{{ yi(s.weighted_cash) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :title="IDEBT_TIP">{{ yi(s.int_debt) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :title="NCW_TIP">{{ yi(s.net_cash_w) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :title="NCB_TIP" :class="{ 'r-hit': s.net_cash_b != null && s.net_cash_b > 0 }">{{ yi(s.net_cash_b) }}<i v-if="s.market !== 'A'" class="ccy">{{ s.currency }}</i></td>
            <td :class="{ 'r-hit': s.pb_pctile != null && s.pb_pctile <= 20 }" :title="pbCellTip(s)">{{ pbCell(s) }}</td>
            <td v-for="c in COLS.filter(x => x.ref)" :key="c.school" class="c-ref" :class="{ gedge: c.edge }" :title="refTitle(s, c.school)">
              <span class="rf-buy" :class="{ 'r-hit': refBuy(s, c.school) != null && s.price != null && s.price <= refBuy(s, c.school) }">{{ fmt(refBuy(s, c.school)) }}<i v-if="buySortSchool === c.school && refSpace(s, c.school) != null" class="rf-sp">{{ refSpaceText(refSpace(s, c.school)) }}</i></span>
              <span class="rf-sell">
                <span class="sl-sort" :class="{ 'r-hit-s': refCons(s, c.school) != null && s.price != null && s.price >= refCons(s, c.school) }"
                      title="按保守卖出价排序" @click.stop="setSort(refKey(c.school, 'sellCons'))">{{ fmt(refCons(s, c.school)) }}</span>
                <span class="sl-sort" :class="{ 'r-hit-s': refFair(s, c.school) != null && s.price != null && s.price >= refFair(s, c.school) }"
                      title="按公允卖出价排序" @click.stop="setSort(refKey(c.school, 'sellFair'))">{{ refFair(s, c.school) == null ? '' : fmt(refFair(s, c.school)) }}</span>
              </span>
            </td>
            <td class="c-act gedge" :title="seoTip(s)">
              <div class="ac-l" v-if="s.actions?.seo">
                <span class="ac-p">{{ fmt(s.actions.seo.price) }}</span><span class="ac-s">{{ ym(s.actions.seo.date) }} {{ qty(s.actions.seo.num) }}</span>
              </div>
              <div class="ac-l" v-else>-</div>
            </td>
            <td class="c-act" :title="buyTip(s)">
              <div v-for="r in buyRows(s)" :key="r.tag" class="ac-l">
                <span class="ac-p">{{ r.price }}</span><i class="ac-cx">{{ r.cx }}</i><span class="ac-s">{{ r.tag }} {{ r.date }} {{ r.num }}股</span>
              </div>
              <div v-if="!buyRows(s).length" class="ac-l">-</div>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
    </template>

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
/* 触屏专用的口径说明：桌面已有 title 悬浮，不再重复占位；手机端由 @media 打开 */
.flts-hint { display: none; }
/* Wind 事件增强分切换档 / 排除行业切换钮：选中时边框与文字走主题色（不加底色，与筛选栏其它控件一致） */
.flts .wind, .flts .ex-t {
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
.flts .wind.on, .flts .ex-t.on { border-color: var(--accent); color: var(--accent); }
/* 排除行业展开块：行业名 2~20 字、上百项，独占一行铺开勾选清单才点得动（原生 select multiple 触屏无法多选） */
.flts .ex-box {
  flex: 1 1 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card);
}
.flts .ex-kw {
  width: 220px;
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--bg);
  color: var(--txt);
}
.flts .ex-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 2px 10px;
  max-height: 168px;
  overflow-y: auto;
}
/* 清单项：字号比筛选栏小一档（一屏要塞几十个行业名），只数用 margin-left:auto 顶到行尾 */
.flts .ex-list .cb { min-width: 0; font-size: 12px; color: var(--txt); }
.flts .ex-n { margin-left: auto; padding-left: 6px; font-style: normal; color: var(--sub); opacity: .8; }
.flts .ex-none { color: var(--sub); font-size: 12px; }
.flts .ex-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
.flts .ex-lab { color: var(--sub); font-size: 12px; }
.flts .ex-tag {
  padding: 2px 7px;
  border: 1px solid var(--accent);
  border-radius: 10px;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
}
.flts .ex-clear {
  padding: 2px 7px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--sub);
  cursor: pointer;
  font-size: 12px;
}
.flts .ex-clear:hover, .flts .ex-tag:hover { border-color: var(--accent); color: var(--accent); }
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
/* 表格有宽度余量，标签不必压到卡片的 9px，与保/公允小字同级即可读 */
.c-ref .rf-sp { font-size: 11px; }
.c-ref .rf-sell { display: flex; flex-direction: column; font-size: 11px; line-height: 1.3; opacity: 0.78; }
/* 格内保/公允卖价：可点排序（不靠色块区分，靠下划线提示） */
.c-ref .sl-sort { cursor: pointer; }
.c-ref .sl-sort:hover { text-decoration: underline; }
.c-act { white-space: nowrap; }
.c-act .ac-l { line-height: 1.35; }
.c-act .ac-s { font-size: 11px; color: var(--sub); margin-left: 6px; }
.c-act .ac-cx { font-style: normal; font-size: 11px; margin-left: 4px; color: var(--sub); }
.sc-act { flex-basis: 100%; display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 4px 6px; font-size: 12px; }
.sc-act em { grid-column: 1; grid-row: 1 / span 2; font-style: normal; color: var(--sub); }
.sc-act span { grid-column: 2; overflow-wrap: anywhere; }
/* 币种角标：只在非 A 股出现，右上角小字，不参与排序也不撑宽列 */
.ccy { font-style: normal; font-size: 9px; color: #999; vertical-align: super; margin-left: 1px; }
/* 陷阱分右上角的「可判项数/7」：口径是覆盖度而不是分数的一部分，故只给到能认出的对比度 */
.tp-ev { font-style: normal; font-size: 9px; color: #9aa5b5; vertical-align: super; margin-left: 1px; }
/* 硬门槛角标：定性否决，只标注不着色不排序，故用最高对比的红而不是等级色（--bad 那套是给分数用的） */
.gate-flag { color: #d43b3b; font-size: 12px; margin-left: 3px; cursor: help; }
/* 财报期龄超阈标注：比门槛弱一级（多为「新一期年报还没披露」的常态），故灰字不加粗 */
.stale-flag { color: var(--sub); font-size: 10px; margin-left: 3px; cursor: help; }
/* 中报恶化徽标：比期龄更刺眼的一档（红棕），但仍是「标注」不改分 */
.dip-flag { color: #c2571a; font-weight: 600; }
table.grid th.unsort { cursor: default; }
/* ---- 宽屏铺开 + 密集排版：23 列争取在 1440 视口下不横向滚动（装不下仍由 .tbl-wrap 滚动兜底） ---- */
.tbl-wrap { overflow-x: auto; }
/* 表头允许折行：列宽改由数值决定（“格进取 买/保/公”不再硬撑一行的宽度），CJK 可任意断字 */
table.grid-list th { white-space: normal; line-height: 1.25; }
table.grid-list th, table.grid-list td { padding: 6px 6px; }
/* ---- 分组表头（2026-09 视觉整理）：组标签行 + 组间分隔线 + 斑马纹 ----
   26 列等权平铺是「乱」的根源：两级表头把列归成 7 组，组首列画竖分隔，
   行间加斑马纹让横向读行不串位；信息一列不删。 */
.grid-list thead tr.grp-row th {
  padding: 4px 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 2px;
  color: var(--sub);
  text-align: center;
  cursor: default;
}
.grid-list thead tr.grp-row th + th { border-left: 1px solid var(--line); }
/* 列名行：不折行（「格进取」折成两行是原先最刺眼的乱源），字号微降，长标签如 PB十年分位也就 ~80px */
.grid-list thead tr:not(.grp-row) th { font-size: 12px; white-space: nowrap; }
/* 列名小注：口径差异直接印在表头（如「净现金/市值 · 减全部负债」），不靠悬停 */
.grid-list thead tr:not(.grp-row) th .th-sub {
  display: block;
  font-size: 10px;
  font-weight: 400;
  color: #a5aebd;
  letter-spacing: 0;
  margin-top: 1px;
}
/* 组底色：极浅的功能色给每组一个「地盘」，只上组标签行不上数据行 */
.grid-list thead tr.grp-row th.grp-id { letter-spacing: 0; }
.grid-list thead tr.grp-row th.grp-school { background: #eef3fb; }
.grid-list thead tr.grp-row th.grp-score { background: #f9f4e8; }
.grid-list thead tr.grp-row th.grp-asset { background: #edf5ef; }
.grid-list thead tr.grp-row th.grp-event { background: #f3f1f8; }
.grid-list thead tr.grp-row th.grp-ref { background: #eaf4f6; }
/* 组首列分隔线：表头与数据行都画，滚动到右侧时组边界仍在 */
.grid-list .gedge { border-left: 1px solid var(--line); }
/* 斑马纹：td 级上色（tr 级会被 th/td 自身背景盖住），stick 列与 hover 同步 */
.grid-list tbody tr:nth-child(even) td { background: #f7f9fc; }
.grid-list tbody tr:nth-child(even) td.stick { background: #f7f9fc; box-shadow: 6px 0 6px -6px rgba(0, 0, 0, .18); }
.grid-list tbody tr:hover td { background: #eef4ff; }
.grid-list tbody tr:hover td.stick { background: #eef4ff; }
/* 名称格两行化：第一行 名称+市场+代码，第二行 行业+标注旗；行业单列就此省下 */
.grid-list .name-cell .nm-row { display: flex; align-items: baseline; gap: 5px; min-width: 148px; }
.grid-list .name-cell .nm-row b { font-size: 13px; }
.grid-list .name-cell .cd { font-size: 11px; color: var(--sub); font-variant-numeric: tabular-nums; }
.grid-list .name-cell .nm-sub { display: flex; align-items: center; gap: 6px; margin-top: 1px; font-size: 11px; color: var(--sub); }
/* 清算列按性价比排序时数字后面还跟着折价小标签，不约束会折成两行把整行撑高 */
table.grid-list .c-liq { white-space: nowrap; }
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

/* ---- 手机卡片视图 ----
   下面这些元素只在 isMobile 为真时渲染，所以不需要 @media 包裹；
   真正随视口切换的（筛选栏折行、折叠按钮、说明段落）放在文件末尾的 @media 里。 */
.stock-cards { display: flex; flex-direction: column; gap: 10px; }
.stock-card {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--card);
  padding: 10px 12px;
  cursor: pointer;
}
.stock-card:active { background: #f2f6fc; }
.stock-card:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.sc-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.sc-name { font-size: 15px; font-weight: 600; color: var(--txt); }
.sc-code { font-size: 11px; color: var(--sub); }
/* 行业最长 20 字，独占第二行，不与名称/现价抢宽度 */
.sc-industry { font-size: 11px; color: var(--sub); flex: 1 1 100%; order: 3; }
.sc-price { margin-left: auto; font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.sc-price b { font-size: 11px; font-weight: 500; margin-left: 4px; }

/* 指标条：PE/PB/市值/造假/管理/周期/净现金·市值，按 33.33% 基准自动折行（7 枚＝三行，末行一枚） */
.sc-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.sc-bd {
  flex: 1 1 calc(33.33% - 6px);
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  padding: 4px 8px;
  border-radius: 6px;
  background: #f7f9fc;
  font-size: 12px;
}
/* em 显式复位字重：stock.css 的全局 .sc-good 带 font-weight:600，
   用户访问过详情页后那张表会留在文档里，不复位就会把灰色小标签也加粗 */
.sc-bd em, .sc-ref em { font-style: normal; color: var(--sub); font-size: 10px; font-weight: 400; }
.sc-bd b { font-weight: 600; font-variant-numeric: tabular-nums; }
/* 末枚独自占一行时不 grow：否则它被拉到整行宽，配上 space-between 会让标签贴左、数值贴右，
   跟前两行的三列节奏脱节（实测 7 枚时末枚 448px，其余 145px） */
.sc-badges > .sc-bd:last-child { flex-grow: 0; }

.sc-scores { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin-top: 6px; }
.sc-score {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 4px;
  padding: 5px 8px;
  border-radius: 6px;
  background: #f7f9fc;
  font-size: 12px;
}
.sc-k { color: var(--sub); font-weight: 400; }
.sc-v { font-weight: 600; font-variant-numeric: tabular-nums; }

.sc-refs { display: flex; gap: 6px; margin-top: 6px; }
.sc-ref {
  flex: 1 1 0;
  min-width: 0;
  text-align: center;
  font-size: 11px;
  background: #f0f3f7;
  border-radius: 6px;
  padding: 5px 2px;
  font-variant-numeric: tabular-nums;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.sc-ref .r-buy, .sc-ref .r-sell { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* 题头行带折价标签时同样绝不折行——折成两行会把该列撑高、与邻列的买/保/公错位 */
.sc-ref em { white-space: nowrap; overflow: hidden; }
/* 折价标签 .rf-sp 只在按性价比排的那几列渲染（四派「买」见 buySortSchool、清算见 sort 判断），
   全列同时摊开会破坏表宽。卡片放题头行（价格行只有 ≈68px 内容区，接在买价后面会被省略号吃掉），表格放数字后面 */
.rf-sp { font-style: normal; font-size: 9px; opacity: 0.78; margin-left: 2px; }
/* 卖价小字是可点的排序入口：桌面靠下划线提示，触屏没有 hover，改按压反馈 + 加高点击区 */
.sc-ref .sl-sort { cursor: pointer; padding: 2px 0; }
.sc-ref .sl-sort:active { text-decoration: underline; }

/* 等级色：stock.css 里 .sc-* 定义了两次（189-193 与 1045-1062），后者覆盖前者，
   这里取“实际生效”的那套值——mid 琥珀、low 红；.sc-bad 只在第一处定义，同为红。
   scoped 选择器带 [data-v-*] 属性，特异性高于全局同名类，不会被 stock.css 反压。 */
.sc-good { color: #1e7e44; }
.sc-mid { color: #b07a10; }
.sc-low { color: #c0392b; }
.sc-bad { color: #c0392b; }
.sc-na { color: #9aa5b5; }

@media (max-width: 600px) {
  /* 折叠触发按钮：36px 触控高度，展开态走主题色，箭头翻转 */
  .m-tgls { display: flex; gap: 8px; margin-bottom: 8px; }
  .m-tgl {
    flex: 1 1 0;
    min-width: 0;
    min-height: 36px;
    display: inline-flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    padding: 6px 10px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--card);
    color: var(--txt);
    cursor: pointer;
    font-size: 13px;
  }
  .m-tgl.on { border-color: var(--accent); color: var(--accent); }
  .m-tgl-tx { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tgl-arw { font-style: normal; flex: none; transition: transform .15s; }
  .m-tgl[aria-expanded="true"] .tgl-arw { transform: rotate(180deg); }

  /* 筛选栏：桌面是一行约 17 个控件的横向流，手机上必然溢出。
     改成两列折行（小控件半宽，下拉/按钮/说明整行），限高内滚不吃掉首屏。 */
  .flts { gap: 8px; max-height: 52vh; overflow-y: auto; padding-right: 2px; }
  .flts > * { flex: 1 1 calc(50% - 5px); min-width: 0; }
  .flts .t, .flts .wind, .flts .ex-t, .flts .ex-box, .flts .disc, .flts .rst, .flts-hint { flex: 1 1 100%; }
  .flts .t { margin-left: 0; }
  .flts label.t { display: flex; align-items: center; gap: 6px; }
  .flts .num { justify-content: space-between; }
  .flts .num input { flex: 1; width: auto; min-width: 0; }
  .flts select { width: 100%; max-width: none; }
  .flts .cb, .flts .wind, .flts .ex-t, .flts .num, .flts .rst, .flts .num input, .flts select { min-height: 32px; }
  /* 排除行业展开块在手机上：搜索框跟着盒宽走；清单行高被上面 .flts .cb 顶到 32px，
     168px 只够五个行业，得放高些（.flts 本身限高 52vh 内滚，不会吃掉整屏） */
  .flts .ex-kw { width: auto; min-height: 32px; }
  .flts .ex-list { max-height: 216px; }
  /* 重置按钮桌面靠 margin-left:auto 顶到最右；折行后那个 auto 会让它缩到半宽并错位 */
  .flts .rst { margin-left: 0; padding: 9px 12px; }
  /* 桌面靠 title 悬浮看的口径说明，触屏上转成常驻段落 */
  .flts-hint {
    display: block;
    margin: 4px 0 0;
    padding-top: 8px;
    border-top: 1px dashed var(--line);
    font-size: 11px;
    line-height: 1.7;
    color: var(--txt);
  }

  /* 排序 chip 面板：表头在手机上不存在，排序入口全在这里 */
  .sorts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 10px 0 12px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 10px;
  }
  .chip {
    flex: none;
    min-height: 32px;
    padding: 5px 12px;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--card);
    color: var(--sub);
    cursor: pointer;
    font-size: 13px;
  }
  .chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }

  .pager .psize { margin-left: 0; }
  .pager .psize select, .pager .psize input { min-height: 32px; }
}
</style>

<style scoped>
/* ---- M1 移动卡片三层重排 ---- */
.sc-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--sub);
}
.sc-meta .sc-industry {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sc-niche {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  background: #eef3fb;
  color: #3f639c;
  font-size: 10px;
  line-height: 18px;
}
.sc-sortval {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  color: #3f639c;
}
.sc-hero {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin-top: 8px;
}
.hero-cell {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 6px 4px;
  text-align: center;
  background: #fff;
}
.hero-cell em {
  display: block;
  font-style: normal;
  font-size: 10px;
  color: var(--sub);
}
.hero-cell b {
  font-size: 17px;
  font-variant-numeric: tabular-nums;
}
.hero-cell b .tp-ev { font-size: 9px; }
.hero-cell .hero-na {
  font-style: normal;
  font-size: 9px;
  color: #a5aeb5;
  margin-left: 2px;
}
.sc-more {
  margin-top: 8px;
  border-top: 1px dashed var(--line);
}
.sc-more summary {
  list-style: none;
  text-align: center;
  padding: 7px 0 2px;
  font-size: 12px;
  color: var(--sub);
  cursor: pointer;
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.sc-more summary::-webkit-details-marker { display: none; }
.sc-more summary::after { content: ' ▾'; }
.sc-more[open] summary::after { content: ' ▴'; }
.sc-more[open] summary { border-bottom: 1px dashed var(--line); margin-bottom: 6px; }
/* 收起后 summary 承担触控目标；点开区不冒泡到卡片跳转由 @click.stop 处理 */
</style>
