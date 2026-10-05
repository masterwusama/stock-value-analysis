<script setup>
/* 投研库:研报列表 + 阅读器(#/research 与 #/research/:slug)。
 * 研报本体是 git 版本化的 docs/research/*.md,后端解析 frontmatter 并渲染 HTML,
 * 本页只做索引展示与排版注入;结构契约见 docs/设计-投研库模块.md。
 * 结论徽章配色与 A股语义绑定:买入/增持红、回避/卖出绿、观察灰。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { get } from '../api/client'

const route = useRoute()
const list = ref(null)
const report = ref(null)
const error = ref('')

const slug = computed(() => route.params.slug)

async function load() {
  error.value = ''
  if (!slug.value) {
    report.value = null
    try { list.value = await get('/research/list') } catch (e) { error.value = String(e.message || e) }
  } else {
    list.value = null
    report.value = null   // 先清旧文:slug 切换时不能让上一篇短暂残留
    try { report.value = await get(`/research/report/${encodeURIComponent(slug.value)}`) }
    catch (e) { error.value = String(e.message || e) }
  }
}
onMounted(load)
// 同一记录下 slug 变化会复用组件实例,不重挂载;显式跟载(跳去别的页面不触发)
watch(slug, () => { if (String(route.path).startsWith('/research')) load() })

const CONC_TONE = {
  买入: 'buy', 增持: 'buy', 观察: 'hold', 回避: 'sell', 卖出: 'sell',
}
const concTone = (c) => CONC_TONE[c] || 'hold'
// 列表徽章一句话:结论 + 现价落在估值区间的哪一档
const valNote = (m) => {
  if (m.val_low == null || m.val_high == null || !m.price_ref) return ''
  const pct = (v) => ((v - 1) * 100).toFixed(0)
  if (m.price_ref <= m.val_low) return `现价为下限的 ${(m.price_ref / m.val_low * 100).toFixed(0)}%`
  if (m.price_ref >= m.val_high) return `现价为上限的 ${(m.price_ref / m.val_high * 100).toFixed(0)}%`
  return `区间内:距下限+${pct(m.price_ref / m.val_low)}%,距上限${pct(m.price_ref / m.val_high)}%`
}
// hash 路由下 v-html 内文的锚点不会自动滚动(改 hash 不触发 vue-router 重载),手动滚
function jump(id) {
  const el = document.getElementById(id)
  if (el) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - 70, behavior: 'smooth' })
  history.replaceState(null, '', '#' + route.fullPath)
}

// 目录按 h2 小节分组:每节独立 <details> 可折叠;level≥3 归入所在小节作子条目
const tocGroups = computed(() => {
  const groups = []
  for (const t of report.value?.toc || []) {
    if (t.level <= 2) groups.push({ id: t.id, name: t.name, children: [] })
    else if (groups.length) groups[groups.length - 1].children.push(t)
    else groups.push({ id: t.id, name: t.name, children: [] })
  }
  return groups
})
</script>

<template>
  <div class="res">
    <div v-if="error" class="err">加载失败:{{ error }}</div>

    <!-- 列表页 -->
    <template v-if="!slug">
      <header class="page-head">
        <h2>投研库</h2>
        <p class="sub">深度研报:生意模式 · 护城河 · 财务拆骨 · 估值区间 · 反方证伪。
          每份研报自带数据来源附录;结构与写作纪律见 <code>docs/设计-投研库模块.md</code>。</p>
      </header>

      <p v-if="list && !list.length" class="empty">还没有研报。</p>
      <section v-for="m in list" :key="m.slug" class="card" @click="$router.push('/research/' + m.slug)">
        <div class="row1">
          <router-link :to="'/stock/' + m.code" class="code-link" @click.stop>{{ m.code }} {{ m.name }}</router-link>
          <span class="conc" :class="'c-' + concTone(m.conclusion)">{{ m.conclusion || '未定' }}</span>
          <span class="date">{{ m.date }}</span>
        </div>
        <div class="row2">
          <h3>{{ m.title }}</h3>
          <span v-if="m.val_low != null && m.val_high != null" class="val">
            估值 {{ m.val_low }}~{{ m.val_high }}<template v-if="m.price_ref"> · 现价 {{ m.price_ref }} {{ valNote(m) }}</template>
          </span>
        </div>
        <p v-if="m.summary" class="summary">{{ m.summary }}</p>
        <p class="tags"><span v-for="t in (m.tags || [])" :key="t" class="tag">{{ t }}</span></p>
      </section>
    </template>

    <!-- 阅读器 -->
    <template v-else-if="report">
      <header class="card head">
        <div class="row1">
          <router-link :to="'/stock/' + report.meta.code" class="code-link">{{ report.meta.code }} {{ report.meta.name }}</router-link>
          <span class="conc" :class="'c-' + concTone(report.meta.conclusion)">{{ report.meta.conclusion || '未定' }}</span>
          <span class="date">{{ report.meta.date }}</span>
          <button type="button" class="back" @click="$router.push('/research')">← 返回列表</button>
        </div>
        <h2>{{ report.meta.title }}</h2>
        <p v-if="report.meta.val_low != null" class="val-big">
          估值区间 <b>{{ report.meta.val_low }} ~ {{ report.meta.val_high }}</b>
          <template v-if="report.meta.price_ref"> · 基准价 {{ report.meta.price_ref }}</template>
          <span v-if="report.meta.val_method" class="val-m">方法:{{ report.meta.val_method }}</span>
        </p>
        <p v-if="report.meta.tags" class="tags"><span v-for="t in (report.meta.tags || [])" :key="t" class="tag">{{ t }}</span></p>
      </header>

      <div class="reader">
        <nav class="toc card" v-if="(report.toc || []).length">
          <details open>
            <summary>目录</summary>
            <div class="toc-scroll">
              <details v-for="g in tocGroups" :key="g.id" class="toc-sec" open>
                <summary><a :href="'#' + g.id" :title="g.name" @click.prevent.stop="jump(g.id)">{{ g.name }}</a></summary>
                <ul v-if="g.children.length">
                  <li v-for="c in g.children" :key="c.id">
                    <a :href="'#' + c.id" :title="c.name" @click.prevent="jump(c.id)">{{ c.name }}</a></li>
                </ul>
              </details>
            </div>
          </details>
        </nav>
        <!-- eslint-disable-next-line vue/no-v-html: 内容为仓库内自有 Markdown,服务端渲染 -->
        <article class="md-body card" v-html="report.html"></article>
      </div>
    </template>
    <p v-else-if="!error" class="empty">加载中…</p>
  </div>
</template>

<style scoped>
.res { max-width: 1100px; margin: 0 auto; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 18px; margin-bottom: 14px; }
.err { color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; padding: 10px 14px; border-radius: 8px; }
.empty { color: #9ca3af; }
.page-head h2 { margin: 4px 0 6px; }
.page-head .sub { color: #6b7280; font-size: 13px; margin: 0 0 14px; }
.page-head code { background: #f3f4f6; border-radius: 4px; padding: 0 4px; font-size: 12px; }

section.card { cursor: pointer; transition: border-color 0.15s; }
section.card:hover { border-color: #93c5fd; }
.row1 { display: flex; align-items: center; gap: 10px; }
.code-link { color: #2563eb; text-decoration: none; font-weight: 600; }
.code-link:hover { text-decoration: underline; }
.conc { font-size: 12px; font-weight: 600; border-radius: 999px; padding: 1px 10px; }
.c-buy { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
.c-sell { background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }
.c-hold { background: #f9fafb; color: #6b7280; border: 1px solid #e5e7eb; }
.date { color: #9ca3af; font-size: 12px; margin-left: auto; }
.row2 { display: flex; align-items: baseline; gap: 12px; margin-top: 6px; flex-wrap: wrap; }
.row2 h3 { margin: 0; font-size: 16px; }
.val { color: #1d4ed8; font-size: 13px; }
.summary { color: #374151; font-size: 13px; line-height: 1.7; margin: 8px 0 6px; }
.tags { margin: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 12px; color: #6b7280; background: #f3f4f6; border-radius: 999px; padding: 1px 8px; }

.head .row1 { margin-bottom: 8px; }
.head h2 { margin: 0 0 8px; font-size: 20px; }
.back { font-size: 12px; padding: 3px 10px; border: 1px solid #e5e7eb; background: #fff;
  color: #374151; border-radius: 6px; cursor: pointer; }
.back:hover { border-color: #2563eb; color: #2563eb; }
.val-big { font-size: 14px; color: #374151; margin: 0 0 8px; }
.val-big b { color: #1d4ed8; font-size: 16px; }
.val-m { display: block; color: #9ca3af; font-size: 12px; margin-top: 2px; }

.reader { display: flex; gap: 14px; align-items: flex-start; }
/* 目录:粘性 + 限高,超长在 .toc-scroll 内部滚动;外层 details 收起时高度自然收缩 */
.toc { position: sticky; top: 70px; width: 220px; flex-shrink: 0; font-size: 13px;
  display: flex; flex-direction: column; max-height: calc(100vh - 84px); overflow: hidden; padding: 12px 12px 8px; }
.toc > details { display: flex; flex-direction: column; min-height: 0; }
.toc > details > summary { flex-shrink: 0; cursor: pointer; font-weight: 600; margin-bottom: 4px; }
.toc-scroll { flex: 1 1 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
  /* flex 链从 max-height 父级收敛在部分内核不生效(clientHeight 实测溢出),直接给滚动区限高兜底:
     100vh-84px 是 .toc 外框,再扣掉外框上下 padding(20)与「目录」summary 行(约 26) */
  max-height: calc(100vh - 130px);
  scrollbar-width: thin; scrollbar-color: #d1d5db transparent; }
.toc-scroll::-webkit-scrollbar { width: 6px; }
.toc-scroll::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 3px; }
/* 小节:自绘箭头(list-style 各浏览器不一),标题是链接(点击跳转不折叠),点箭头/其余区域折叠 */
.toc-sec { border-top: 1px solid #f1f5f9; }
.toc-sec:first-child { border-top: none; }
.toc-sec > summary { display: block; position: relative; padding: 5px 0 5px 14px;
  cursor: pointer; user-select: none; list-style: none; font-weight: 600; }
.toc-sec > summary::-webkit-details-marker { display: none; }
.toc-sec > summary::before { content: '▸'; position: absolute; left: 0; top: 5px;
  color: #9ca3af; font-size: 11px; line-height: 20px; transition: transform 0.15s; }
.toc-sec[open] > summary::before { transform: rotate(90deg); }
.toc ul { list-style: none; padding-left: 14px; margin: 0 0 6px; }
.toc a { color: #374151; text-decoration: none; line-height: 1.9; display: block;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toc ul a { font-weight: normal; color: #4b5563; }
.toc a:hover { color: #2563eb; }
.md-body { flex: 1; min-width: 0; }

/* v-html 注入的正文无法用 scoped 命中,走 :deep */
.md-body :deep(h2) { font-size: 18px; margin: 26px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #f1f5f9; }
.md-body :deep(h2:first-child) { margin-top: 0; }
.md-body :deep(h3) { font-size: 15px; margin: 18px 0 8px; }
.md-body :deep(p) { line-height: 1.85; margin: 8px 0; color: #1f2937; }
.md-body :deep(table) { border-collapse: collapse; width: 100%; font-size: 13px; margin: 12px 0; }
.md-body :deep(th), .md-body :deep(td) { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
.md-body :deep(th) { background: #f8fafc; color: #475569; }
.md-body :deep(blockquote) { margin: 10px 0; padding: 8px 14px; border-left: 3px solid #93c5fd;
  background: #eff6ff; color: #374151; border-radius: 0 6px 6px 0; }
.md-body :deep(code) { background: #f3f4f6; border-radius: 4px; padding: 0 4px; font-size: 12px; }
.md-body :deep(a) { color: #2563eb; }
.md-body :deep(ul), .md-body :deep(ol) { padding-left: 22px; line-height: 1.85; }
.md-body :deep(hr) { border: none; border-top: 1px solid #f1f5f9; margin: 18px 0; }

@media (max-width: 600px) {
  /* 研报卡:标题/区间换行、摘要行高收紧 */
  section.card { padding: 12px 12px; }
  .row1 { flex-wrap: wrap; row-gap: 4px; }
  .row1 .date { margin-left: 0; flex-basis: 100%; order: 9; }
  .row2 h3 { font-size: 15px; }
  .summary { font-size: 12px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
  .val { font-size: 12px; }
  .tag { font-size: 11px; padding: 1px 7px; }

  /* 阅读器:目录从侧栏改为顶部折叠,正文单列 */
  .reader { flex-direction: column; }
  .toc { position: static; width: auto; max-height: none; }
  .toc > details > summary { margin-bottom: 2px; }
  .toc-scroll { overflow: visible; max-height: none; }
  /* nowrap 的省略号截断在窄屏会撑爆 details:改可换行 */
  .toc a { white-space: normal; line-height: 1.5; padding: 2px 0; }
  .toc-sec > summary { padding: 4px 0 4px 14px; }
  /* 正文排版:表格是研报的主要内容形态,给横向滚动而不是压碎;
     字号回退防 iOS 聚焦放大无输入框场景不适用,但表格 12px 在手机可读性尚可 */
  .md-body { padding: 12px 12px; }
  .md-body :deep(table) { display: block; width: max-content; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; font-size: 12px; }
  /* 长 URL/长标题不再把整页撑出横向滚动:正文内所有块级内容限宽并可断行 */
  .md-body { max-width: 100%; overflow-x: hidden; }
  .md-body :deep(h1), .md-body :deep(h2), .md-body :deep(h3), .md-body :deep(p), .md-body :deep(li) { overflow-wrap: anywhere; }
  .md-body :deep(th), .md-body :deep(td) { padding: 4px 6px; white-space: nowrap; }
  .md-body :deep(h2) { font-size: 16px; }
  .md-body :deep(h3) { font-size: 14px; }
  .md-body :deep(blockquote) { padding: 6px 10px; }
  .head h2 { font-size: 17px; }
  .val-big { font-size: 13px; }
  .val-big b { font-size: 15px; }
  .md-body :deep(table) { font-size: 12px; }
  .md-body :deep(th), .md-body :deep(td) { padding: 4px 6px; }
}
</style>
