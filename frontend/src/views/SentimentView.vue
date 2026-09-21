<script setup>
/* 情绪日报页:盘后状态卡 + 明日预案 + 板块效应/候选池/黑名单 + 近期状态带。
 * 数据源 /api/sentiment/*,采集与口径见 docs/使用说明书.md「情绪日报」。
 * 候选池股票名链到详情页(#/stock/:code),与其余名单共用的紧凑表格样式就地 scoped,不复用 stock.css。
 */
import { onMounted, ref } from 'vue'
import { get } from '../api/client'

const daily = ref(null)
const history = ref([])
const error = ref('')

onMounted(async () => {
  try {
    daily.value = await get('/sentiment/daily')
    history.value = await get('/sentiment/history', { limit: 60 })
  } catch (e) {
    error.value = String(e.message || e)
  }
})

const IND = [
  ['red_rate', '红盘率', v => v == null ? '-' : (v * 100).toFixed(0) + '%'],
  ['zt_count', '涨停', v => v ?? '-'],
  ['zha_rate', '炸板率', v => v == null ? '-' : (v * 100).toFixed(0) + '%'],
  ['dt_count', '跌停', v => v ?? '-'],
  ['yz_dt_count', '一字跌停', v => v ?? '-'],
  ['r5', 'R5(国证2000)', v => v == null ? '-' : (v * 100).toFixed(2) + '%'],
]

const TONE_CN = { strong: '极强', warm: '回暖', flat: '普通', weak: '极弱' }
function fmtAmount(wan) {
  if (wan == null) return '-'
  return wan >= 10000 ? (wan / 10000).toFixed(1) + '亿' : Math.round(wan).toLocaleString() + '万'
}
</script>

<template>
  <div class="sent">
    <div v-if="error" class="err">加载失败:{{ error }}(服务端可能还没跑过 fetch_sentiment)</div>

    <template v-if="daily">
      <!-- ① 状态卡 -->
      <section class="card head" :class="'tone-' + daily.tone">
        <div class="badge">{{ daily.state }}</div>
        <div class="head-meta">
          <div class="date">{{ daily.date }}<span v-if="daily.prev_state" class="prev">· 昨日 {{ daily.prev_state }}</span></div>
          <div class="inds">
            <span v-for="[k, label, fmt] in IND" :key="k" class="ind">
              <em>{{ label }}</em>{{ fmt(daily[k]) }}
            </span>
          </div>
        </div>
      </section>
      <p v-if="daily.trap" class="trap">
        ⚠ 陷阱日:昨日极弱、今日转普通——该形态次日涨停溢价实测 +0.55%/胜率 50%,全局禁止隔夜。
      </p>

      <!-- ② 明日预案 -->
      <section class="card">
        <h3>明日预案(转移概率,2024-01~2026-09 校准)</h3>
        <div v-for="(p, k) in daily.transitions" :key="k" class="trow">
          <span class="tname">{{ k }}</span>
          <span class="tbar"><i :style="{ width: (p * 100) + '%' }"></i></span>
          <span class="tpct">{{ Math.round(p * 100) }}%</span>
        </div>
      </section>

      <!-- ③ 板块效应 -->
      <section class="card">
        <h3>板块效应<span class="sub">同行业涨停≥5家且占该行业成分≥2% · 次日溢价档 {{ daily.report.premium_hint || '-' }}</span></h3>
        <table v-if="(daily.sectors || []).length" class="tbl">
          <thead><tr><th>行业</th><th class="num">涨停家数</th><th class="num">占行业%</th><th class="num">成交额</th></tr></thead>
          <tbody>
            <tr v-for="s in daily.sectors" :key="s.industry">
              <td>{{ s.industry }}</td>
              <td class="num">{{ s.limit_count }}</td>
              <td class="num">{{ s.share_of_industry }}%</td>
              <td class="num">{{ fmtAmount(s.amount_yi * 10000) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">今日无板块效应行业(散点行情,谨慎追高)。</p>
      </section>

      <!-- ④ 候选池 -->
      <section class="card">
        <h3>候选池<span class="sub">板块效应行业内自然涨停 · 预期竞价溢价 {{ daily.report.premium_hint || '-' }}</span></h3>
        <table v-if="(daily.report.candidates || []).length" class="tbl">
          <thead><tr><th>代码/名称</th><th>行业</th><th class="num">收盘</th><th class="num">涨跌幅</th><th class="num">成交额</th><th class="num">流通市值(亿)</th></tr></thead>
          <tbody>
            <tr v-for="c in daily.report.candidates" :key="c.code">
              <td><router-link :to="'/stock/' + c.code" class="code-link">{{ c.code }} {{ c.name }}</router-link></td>
              <td>{{ c.industry }}</td>
              <td class="num">{{ c.price }}</td>
              <td class="num">{{ c.chg_pct }}%</td>
              <td class="num">{{ fmtAmount(c.amount_wan) }}</td>
              <td class="num">{{ c.float_mcap_yi }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">无(当日无板块效应或全是一字板)。</p>
      </section>

      <!-- ⑤ 黑名单 / 跌停观察 -->
      <section class="card">
        <h3>炸板黑名单<span class="sub">次日无条件禁入,实测次日溢价 -1.6% · 共 {{ daily.zha_count }} 只</span></h3>
        <p class="chips">
          <router-link v-for="b in (daily.report.blacklist || []).slice(0, 20)" :key="b.code"
                       :to="'/stock/' + b.code" class="chip chip-bad">{{ b.name }}</router-link>
        </p>
        <h3>跌停观察<span class="sub">一字跌停 {{ daily.yz_dt_count }} 只,关注打开(掏肛需质地与板块预期配合)</span></h3>
        <p class="chips">
          <router-link v-for="w in (daily.report.watch_dt || [])" :key="w.code"
                       :to="'/stock/' + w.code" class="chip chip-watch">{{ w.name }}</router-link>
          <span v-if="!(daily.report.watch_dt || []).length" class="empty">今日无跌停。</span>
        </p>
      </section>

      <!-- ⑥ 近期状态带 -->
      <section v-if="history.length" class="card">
        <h3>近期状态带<span class="sub">最近 {{ history.length }} 个交易日</span></h3>
        <div class="strip">
          <span v-for="h in history" :key="h.date" class="seg" :class="'bg-' + h.tone"
                :title="`${h.date} ${h.state} 涨停${h.zt_count ?? '-'} 红盘率${h.red_rate != null ? (h.red_rate*100).toFixed(0)+'%' : '-'}`"></span>
        </div>
        <p class="legend">
          <i class="bg-strong"></i>极强 <i class="bg-warm"></i>回暖 <i class="bg-flat"></i>普通 <i class="bg-weak"></i>极弱
        </p>
      </section>

      <p class="note">口径:全市场 A 股收盘快照(剔除停牌),涨停按交易所涨停价判定;状态阈值与转移矩阵为
        2024-01~2026-09 校准,积累满 60 交易日后换滚动分位。采集:交易日 15:35 fetch_sentiment。</p>
    </template>
    <p v-else-if="!error" class="empty">加载中…</p>
  </div>
</template>

<style scoped>
.sent { max-width: 1100px; margin: 0 auto; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.card .sub { font-weight: normal; color: #6b7280; font-size: 12px; margin-left: 8px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }

.head { display: flex; align-items: center; gap: 16px; }
.badge { font-size: 30px; font-weight: 700; color: #fff; background: #6b7280; border-radius: 10px; padding: 10px 18px; }
.tone-strong .badge { background: #dc2626; }  /* A股红涨 */
.tone-warm .badge { background: #ea580c; }
.tone-flat .badge { background: #6b7280; }
.tone-weak .badge { background: #16a34a; }
.head-meta .date { font-size: 16px; font-weight: 600; }
.head-meta .prev { color: #6b7280; font-weight: normal; font-size: 13px; margin-left: 6px; }
.inds { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 6px; font-size: 13px; }
.inds em { font-style: normal; color: #6b7280; margin-right: 4px; }

.trap { color: #92400e; background: #fffbeb; border: 1px solid #fde68a; padding: 8px 14px; border-radius: 8px; }

.trow { display: flex; align-items: center; gap: 10px; margin: 6px 0; font-size: 13px; }
.tname { width: 3em; text-align: right; }
.tbar { flex: 1; background: #f3f4f6; border-radius: 4px; height: 14px; overflow: hidden; }
.tbar i { display: block; height: 100%; background: #2563eb; }
.tpct { width: 3em; }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 5px 8px; border-bottom: 1px solid #f1f5f9; text-align: left; }
.tbl th { color: #6b7280; font-weight: 500; }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.code-link { color: #2563eb; text-decoration: none; }
.code-link:hover { text-decoration: underline; }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; }
.chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; text-decoration: none; }
.chip-bad { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
.chip-watch { background: #f0f9ff; color: #075985; border: 1px solid #bae6fd; }

.strip { display: flex; gap: 2px; }
.seg { flex: 1; height: 18px; border-radius: 2px; min-width: 4px; }
.bg-strong { background: #dc2626; } .bg-warm { background: #ea580c; }
.bg-flat { background: #d1d5db; } .bg-weak { background: #16a34a; }
.legend { font-size: 12px; color: #6b7280; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 10px; }

.note { color: #9ca3af; font-size: 12px; line-height: 1.6; }
.empty { color: #9ca3af; font-size: 13px; }
@media (max-width: 600px) {
  .head { flex-direction: column; align-items: flex-start; }
  .tbl { font-size: 12px; }
}
</style>
