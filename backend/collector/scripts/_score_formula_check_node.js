/* _score_formula_check.py 的 Node 对端：把合成 fixture 喂给 stockLegacy.js 的原函数，
 * 打出与 Python value_scores 同构的四派总分 + 成长分（growth / growthEval），
 * 供 Python 侧比对边界形状是否两边一致。
 *
 * 与 _score_check_node.js 的差别只在输入来源：那个跑全量真实公司，这个跑指定目录下的
 * 合成 fixture（几十份，秒级），所以浏览器垫片按同样的路子垫一份。
 *
 * 用法：node scripts/_score_formula_check_node.js <fixture 目录>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const SCRIPTS = __dirname;
const DIR = process.argv[2];
if (!DIR) {
  process.stderr.write('缺少 fixture 目录参数\n');
  process.exit(2);
}
const LIB = path.join(SCRIPTS, '..', '..', '..', 'frontend', 'src', 'lib', 'stockLegacy.js');

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
  if (v === null || v === undefined) return null;
  if (typeof v === 'number' && !isFinite(v)) return null;
  return v;
}

async function main() {
  const m = await import(pathToFileURL(LIB).href);
  const out = {};
  for (const f of fs.readdirSync(DIR).filter(function (x) { return x.endsWith('.json'); }).sort()) {
    const d = JSON.parse(fs.readFileSync(path.join(DIR, f), 'utf8'));
    const vs = m.valueScores(d, m.valueAnalysis(d));
    const gs = m.growthScore(d) || {};
    out[path.basename(f, '.json')] = {
      grahamAgg: num(vs.grahamAgg.total),
      grahamDef: num(vs.grahamDef.total),
      schloss: num(vs.schloss.total),
      buffett: num(vs.buffett.total),
      growth: num(gs.total),
      growthEval: num(gs.eff ? gs.eff.evaluated : null),
    };
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch(function (e) {
  process.stderr.write('NODE FAIL: ' + (e && e.stack ? e.stack : String(e)) + '\n');
  process.exit(1);
});
