/* _score_check.py 的 Node 对端：抽取 stockLegacy.js（原 stock.js）的评分函数，
 * 对 backend/collector/data/companies 下每家公司算出与 scoring.py compute_scores
 * 同构的结果，以 JSON 打到 stdout 供 Python 侧逐项对比。
 *
 * scoring.py 文件头要求“以 JS 为基准”做一致性校验，此前该文件缺失导致校验根本跑不了。
 * stockLegacy.js 是浏览器端 ESM，顶层会碰 window.matchMedia，故先垫全局再动态 import。
 *
 * 用法：node scripts/_score_check_node.js   （由 _score_check.py 调用，勿手工看输出）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const SCRIPTS = __dirname;
const COMPANIES = path.join(SCRIPTS, '..', 'data', 'companies');
const LIB = path.join(SCRIPTS, '..', '..', '..', 'frontend', 'src', 'lib', 'stockLegacy.js');

// 浏览器全局垫片：只覆盖模块顶层与评分路径真正会碰到的部分
global.window = {
  matchMedia: function () { return { matches: false, addEventListener: function () {}, removeEventListener: function () {} }; },
  addEventListener: function () {},
};
global.document = {
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  createElement: function () { return { style: {}, classList: { add: function () {}, remove: function () {} }, appendChild: function () {} }; },
  body: { appendChild: function () {}, classList: { add: function () {}, remove: function () {} } },
};
global.localStorage = { getItem: function () { return null; }, setItem: function () {} };

function num(v) {
  // Python 侧 None ↔ JS null；NaN/Infinity 转成 null 以免 JSON.stringify 产出非法字面量
  if (v === null || v === undefined) return null;
  if (typeof v === 'number' && !isFinite(v)) return null;
  return v;
}

async function main() {
  const m = await import(pathToFileURL(LIB).href);
  const out = {};
  const files = fs.readdirSync(COMPANIES).filter(function (f) { return f.endsWith('.json'); }).sort();
  for (const f of files) {
    const d = JSON.parse(fs.readFileSync(path.join(COMPANIES, f), 'utf8'));
    const code = d.code || path.basename(f, '.json');
    const va = m.valueAnalysis(d);
    const vs = m.valueScores(d, va);
    const pr = m.priceReferences(d, va);
    const ca = m.cycleAnalysis(d);
    const hist = m.cycleHistory(d);

    function refs(key) {
      const r = pr[key] || {};
      return { buy: num(r.buy), sellCons: num(r.sellCons), sellFair: num(r.sellFair) };
    }

    out[code] = {
      grahamAgg: num(vs.grahamAgg.total),
      grahamDef: num(vs.grahamDef.total),
      schloss: num(vs.schloss.total),
      buffett: num(vs.buffett.total),
      priceRefs: {
        fairLiq: num(pr.fairLiq),
        netCashRatio: num(pr.netCashRatio),
        netCashCalc: pr.netCashCalc == null ? null : {
          cash: num(pr.netCashCalc.cash), fin: num(pr.netCashCalc.fin),
          notes: num(pr.netCashCalc.notes), otherCA: num(pr.netCashCalc.otherCA),
          tl: num(pr.netCashCalc.tl), mcap: num(pr.netCashCalc.mcap),
          report: pr.netCashCalc.report == null ? null : pr.netCashCalc.report,
        },
        grahamAgg: refs('grahamAgg'),
        grahamDef: refs('grahamDef'),
        schloss: refs('schloss'),
        buffett: refs('buffett'),
      },
      fraud: num((m.fraudAnalysis(d) || {}).total),
      mgmt: num((m.managementAnalysis(d) || {}).total),
      cyclical: !!ca.cyclical,
      cyclicalScore: num(ca.cyclicalScore),
      cycle: num(ca.total),
      cycleHistory: hist.map(function (x) { return { year: x.year, score: num(x.score) }; }),
      // compute_scores 的口径：非周期性公司（cycle total 为空）不出趋势
      cycleTrend: ca.total == null ? null : m.cycleTrendOf(hist),
    };
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch(function (e) {
  process.stderr.write('NODE FAIL: ' + (e && e.stack ? e.stack : String(e)) + '\n');
  process.exit(1);
});
