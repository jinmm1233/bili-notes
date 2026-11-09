/* 前端渲染器校验（node 跑）：把 app.js 里的 esc / 公式 / renderMarkdown 抠出来单测。
 *
 * 为什么要单独测：阅读器是自研的 markdown 渲染器，数学公式（矩阵/分式）
 * 以前完全没有支持，笔记里出现 LaTeX 就是一坨源码。这里保证：
 *   1. $...$ / $$...$$ 交给 KaTeX 渲染成真公式；
 *   2. "$50" 这种价格不会被误当公式；
 *   3. KaTeX 没加载 / 语法写错时退回等宽原文，不糊版面。
 *
 * 用法：node tests/web_render_check.js
 */
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const appSrc = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');

function slice(from, to) {
  const i = appSrc.indexOf(from);
  const j = appSrc.indexOf(to);
  if (i < 0 || j < 0 || j <= i) throw new Error(`切片失败: ${from} -> ${to}`);
  return appSrc.slice(i, j);
}

// 只取不依赖 DOM 的纯函数：esc + 公式三个函数 + renderMarkdown
const escSrc = slice('const esc = (s)', 'const fmtNum');
// 含 renderMindmapTree：真实笔记里有 mermaid 知识地图块，渲染时要用到
const mathSrc = slice('const MATH_RE', '/* ==================== 设置');

function load(windowStub) {
  const factory = new Function('window', `${escSrc}\n${mathSrc}\n`
    + 'return { renderMarkdown, holdMath, isMath, renderMath, renderFlowTree, renderMindmapTree };');
  return factory(windowStub);
}

const katex = require(path.join(root, 'web', 'vendor', 'katex', 'katex.min.js'));
const withKatex = load({ katex });
const noKatex = load({});   // 模拟 KaTeX 资源没加载（离线/打包缺文件）

let failed = 0;
function ok(cond, msg) {
  if (cond) { console.log('[OK] ' + msg); } else { failed++; console.log('[FAIL] ' + msg); }
}

const render = (md) => withKatex.renderMarkdown(md, 'note1');

// 1. 行内公式
const inline = render('矩阵 $A^{-1}$ 是逆矩阵。');
ok(inline.includes('class="katex"') && !inline.includes('$A'), '行内公式 $...$ → KaTeX');
ok(!inline.includes('\\begin'), '行内公式不残留 LaTeX 源码');

// 2. 独立矩阵
const matrix = render('$$\n\\begin{bmatrix} 1 & 2 \\\\ 3 & 4 \\end{bmatrix}\n$$');
ok(matrix.includes('katex-display'), '独立公式 $$...$$ → 块级 KaTeX');
ok((matrix.match(/<span class="mord">/g) || []).length >= 4, '矩阵四个元素都渲染出来了');
ok(!/&lt;|&amp;/.test(matrix.split('katex-display')[1].slice(0, 400)), '公式里的 & < > 没被二次转义');

// 3. 价格不当公式（成对 $..$ 中间夹中文，被 isMath 的中文规则拦下）
const price = render('门票 $50，折合 $35 左右。');
ok(!price.includes('katex') && price.includes('$50'), '"$50" 不被当成公式');

// 3.5 负数/纯数字是公式（用户截图反馈：$-1$、$2$ 显示成原文）
const neg = render('将第一行的 $-1$ 倍加到第二行，将第二行的 $2$ 倍加到第三行。');
ok((neg.match(/class="katex"/g) || []).length === 2, '$-1$ 和 $2$ 按公式渲染，不再露 $ 符号');

// 4. 语法写错 / 未知命令 → 兜底原文，不炸
const broken = render('$$\\begin{notacmd} x \\end{notacmd}$$');
ok(broken.includes('katex') || broken.includes('code class="tex"'), '错误语法不抛异常，有兜底');

// 5. KaTeX 未加载 → 等宽原文
const fallback = noKatex.renderMarkdown('逆矩阵 $A^{-1}$', 'n');
ok(fallback.includes('code class="tex"') && fallback.includes('A^{-1}'), 'KaTeX 缺失时退回等宽 LaTeX 原文');

// 6. 公式与列表 / 图片 / 引用共存
const mixed = [
  '> **画面要点**：**初等行变换**',
  '> - 构造增广矩阵 $[A|E]$，把 $A$ 化为 $E$',
  '',
  '![画面 01:08](assets/a.jpg)',
].join('\n');
const out = render(mixed);
ok(out.includes('<blockquote>') && out.includes('<img'), '公式不破坏引用块与图片');
ok((out.match(/class="katex"/g) || []).length >= 3, '引用块内的多处行内公式都渲染');

// 7. 空 / 无公式输入
ok(render('').trim() === '', '空输入安全');
ok(withKatex.isMath('A^{-1}') && withKatex.isMath('-1') && !withKatex.isMath('门票 50 元'), 'isMath 判定：公式/负数是，夹中文的不是');

// 7.4 CRLF 老笔记：frontmatter 要剥掉、正文正常渲染
const crlfMd = '---\r\ntitle: "测试"\r\n---\r\n\r\n正文 $x^2$ 一行\r\n';
const crlfHtml = render(crlfMd);
ok(!crlfHtml.includes('title:'), 'CRLF 笔记的 frontmatter 也要剥掉');
ok(crlfHtml.includes('正文') && (crlfHtml.match(/class="katex"/g) || []).length === 1, 'CRLF 正文与公式正常');

// 7.5 行内公式不能截断句子（用户截图反馈：一句话被公式拆成好几行）
const inlineMd = '- 变量有下界 $l_j$ 和上界 $u_j$，即 $l_j \\le x_j \\le u_j$。';
const inlineHtml = render(inlineMd);
const inlinePs = (inlineHtml.match(/<p>/g) || []).length;
ok(inlinePs === 0, `行内公式混排的列表项不该产生 <p>（实际 ${inlinePs} 个）`);
const lis = inlineHtml.match(/<li>[\s\S]*?<\/li>/) || [''];
ok(lis[0].includes('变量有下界') && lis[0].includes('和上界'), '整句话留在同一个 <li> 里');
ok((inlineHtml.match(/class="katex"/g) || []).length === 3, '三个行内公式都渲染了');

const mixBlock = '前面 $a+b$ 文字\n\n$$\\\\begin{bmatrix} 1 \\\\\\\\ 2 \\\\end{bmatrix}$$\n\n后面 $c$ 文字';
const mixedHtml = render(mixBlock);
ok((mixedHtml.match(/katex-display/g) || []).length === 1, '块级公式仍是块级');
ok(mixedHtml.includes('前面') && mixedHtml.includes('后面'), '块级公式前后的文字都还在');

// 7.6 例题精解：有序步骤要渲染成 <ol>，关键句要保留荧光笔
const probMd = [
  '## 例题精解', '',
  '### 例题 1：矩阵连乘', '',
  '> **题目**', '>', '> 给定 4 个矩阵，求最少乘法次数。', '',
  '> **解题关键**：==把连乘顺序当成区间划分==，用区间 DP。', '',
  '**步骤**', '',
  '1. 先算长度为 2 的区间', '2. 再递推到长度 4', '3. 取最小值', '',
  '> **易错**：区间长度要从 2 开始枚举。',
].join('\n');
const probHtml = render(probMd);
ok((probHtml.match(/<ol>/g) || []).length === 1, '步骤渲染成有序列表');
ok((probHtml.match(/<li class="oli">/g) || []).length === 3, '三条步骤都在 <ol> 里');
ok(probHtml.includes('<mark class="hl">把连乘顺序当成区间划分</mark>'), '解题关键被高亮');
ok(probHtml.includes('易错') && probHtml.includes('题目'), '题干与易错点都渲染了');

// 8. 知识地图：flowchart TD 画成纵向流程，递进步骤排成一列
const flowSrc = [
  'flowchart TD',
  '  n1(["线代 22 求逆矩阵"])',
  '  n2["初等行变换法求逆矩阵"]',
  '  n1 --> n2',
  '  n3["① 组成增广矩阵 [A|E]"]',
  '  n2 --> n3',
  '  n4["② 初等行变换把左边化为 E"]',
  '  n3 --> n4',
  '  n5["③ 右边即 A 的逆矩阵"]',
  '  n4 --> n5',
].join('\n');
const flow = withKatex.renderFlowTree(flowSrc);
ok(flow && flow.includes('class="mmflow"'), 'flowchart TD → 图（不再是源码）');
const pos = [...flow.matchAll(/<text x="([\d.]+)" y="([\d.]+)"/g)]
  .map((m) => [Number(m[1]), Number(m[2])]);
ok(pos.length === 5, `五个节点都画出来了（实际 ${pos.length}）`);
ok(pos.every(([x]) => Math.abs(x - pos[0][0]) < 0.5), '单链的节点在同一列（不再放射）');
ok(pos.every(([, y], i) => i === 0 || y > pos[i - 1][1]), '自上而下按顺序排列');
ok((flow.match(/class="arw"/g) || []).length === 4, '每条连线都带箭头，先后关系可见');

const bad = render('```mermaid\nflowchart TD\n  A -- 中文 --> B\n```');
ok(bad.includes('class="code"'), '认不出的图语法退回源码，不乱画');

const legacy = withKatex.renderMindmapTree('mindmap\n  root((旧图))\n    甲\n      乙');
ok(legacy && legacy.includes('class="mmap"'), '旧笔记的 mindmap 块仍然能渲染（向后兼容）');

// 8. 端到端：给一条真实笔记的路径，看矩阵到底渲染出来没有
const real = process.argv[2];
if (real) {
  const md = fs.readFileSync(real, 'utf8');
  const html = withKatex.renderMarkdown(md, 'note1');
  const total = (html.match(/class="katex"/g) || []).length;
  const display = (html.match(/katex-display/g) || []).length;
  console.log(`\n[真实笔记] ${path.basename(path.dirname(real))}`);
  console.log(`  公式 ${total} 处，其中块级（矩阵等）${display} 处`);
  ok(total > 0, '真实笔记里有公式被渲染');
  ok(!/\\begin\{bmatrix\}/.test(html), '笔记里没有残留未渲染的 LaTeX 源码');
}

console.log(failed ? `\n${failed} 项失败` : '\nALL PASS');
process.exit(failed ? 1 : 0);
