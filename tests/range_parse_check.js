/* 选集序号解析校验（node 跑）：parseRange 是纯函数，直接从 app.js 里抠出来测。
 *
 * 用户会输入各种写法：3,5-7,11-20 / 3，5~7 / 5 至 7 / 7-5 倒着写。
 * 这里保证：能认的都对、认不了的给明确报错、越界不静默吞掉。
 *
 * 用法：node tests/range_parse_check.js
 */
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');

const start = src.indexOf('function parseRange(');
const end = src.indexOf('function renderPicker(');
if (start < 0 || end <= start) throw new Error('没能从 app.js 里找到 parseRange');
const parseRange = new Function(`${src.slice(start, end)}; return parseRange;`)();

let failed = 0;
function eq(actual, expected, msg) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log((ok ? '[OK] ' : '[FAIL] ') + msg + (ok ? '' : ` → 实际 ${JSON.stringify(actual)}`));
  if (!ok) failed += 1;
}
function bad(input, max, msg) {
  const r = parseRange(input, max);
  const ok = !!r.error;
  console.log((ok ? '[OK] ' : '[FAIL] ') + msg + (ok ? `（${r.error}）` : ' → 没有报错'));
  if (!ok) failed += 1;
}

// 用户原话：「第三集，第五到第七集，第十一到第二十集」
eq(parseRange('3,5-7,11-20', 40).nums, [3, 5, 6, 7, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
   '3,5-7,11-20 → 14 集');
eq(parseRange('3，5-7，11-20', 40).nums.length, 14, '中文逗号同样能认');
eq(parseRange('3 5~7 11至13', 40).nums, [3, 5, 6, 7, 11, 12, 13], '空格 / ~ / 至 都能认');
eq(parseRange('7-5', 40).nums, [5, 6, 7], '倒着写 7-5 自动纠正');
eq(parseRange('5,5,6', 40).nums, [5, 6], '重复序号去重');
eq(parseRange('', 40).nums, [], '空输入不报错');
eq(parseRange('1-40', 40).nums.length, 40, '1 到全部集数');

bad('0', 40, '第 0 集不存在要报错');
bad('41', 40, '超出总集数要报错');
bad('1-99', 40, '区间超出要报错');
bad('abc', 40, '看不懂的输入要报错');
bad('3-', 40, '只有半截的区间要报错');

console.log();
if (failed) {
  console.log(`${failed} 项未通过`);
  process.exit(1);
}
console.log('序号解析全部通过');
