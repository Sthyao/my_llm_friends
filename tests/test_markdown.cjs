const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const page = vm.createContext({ URL });
vm.runInContext("window = globalThis;", page);
for (const script of ["vendor/markdown-it.min.js", "markdown.js"]) {
  const filename = path.join(__dirname, "..", "static", script);
  vm.runInContext(fs.readFileSync(filename, "utf8"), page, { filename });
}
const { render, plainText } = page.thoughtMarkdown;

test("renders basic Markdown, keeps line breaks, and wraps tables for horizontal scrolling", () => {
  const html = render([
    "## 观察",
    "",
    "- **事实** 和 *解读*",
    "- `证据`",
    "",
    "1. 首先",
    "2. 然后",
    "",
    "> 段落一",
    "> 第二行",
    "",
    "| 解释 | 依据 |",
    "| --- | --- |",
    "| 甲 | 乙 |",
    "",
    "```js",
    'const html = "<section>";',
    "```",
  ].join("\n"));

  assert.match(html, /<h2>观察<\/h2>/);
  assert.match(html, /<ul>\s*<li><strong>事实<\/strong> 和 <em>解读<\/em><\/li>/);
  assert.match(html, /<li><code>证据<\/code><\/li>/);
  assert.match(html, /<ol>\s*<li>首先<\/li>\s*<li>然后<\/li>/);
  assert.match(html, /<blockquote>\s*<p>段落一<br>\s*第二行<\/p>/);
  assert.match(html, /<div class="markdown-table"><table>[\s\S]*<th>解释<\/th>[\s\S]*<td>乙<\/td>[\s\S]*<\/table>\s*<\/div>/);
  assert.match(html, /<pre><code class="language-js">const html = &quot;&lt;section&gt;&quot;;\s*<\/code><\/pre>/);
  assert.equal(render(null), "");
});

test("keeps raw HTML escaped and renders image descriptions as text", () => {
  const html = render([
    '<span class="example">HTML</span>',
    "",
    "![图示 <tag> *标题*](https://example.test/image.png)",
  ].join("\n"));

  assert.match(html, /&lt;span class=&quot;example&quot;&gt;HTML&lt;\/span&gt;/);
  assert.match(html, /<p>图示 &lt;tag&gt; 标题<\/p>/);
  assert.doesNotMatch(html, /<span|<img|https:\/\/example.test\/image.png/);
});

test("keeps the readable content of a Markdown preview", () => {
  const text = [
    "## 我的计划",
    "1. 查看 **主要** [资料](https://example.test)",
    "2. 试用 `命令`",
    "> ![配图 <tag>](https://example.test/image.png)",
    "",
    "```js",
    "run()",
    "```",
    "",
    "| 项目 | 值 |",
    "| --- | --- |",
    "| 样本 A | 14 |",
  ].join("\n");

  assert.equal(
    plainText(text).trim().replace(/\s+/g, " "),
    "我的计划 查看 主要 资料 试用 命令 配图 <tag> run() 项目 值 样本 A 14",
  );
});

test("opens external web URLs safely and keeps valid relative links in the app", () => {
  const html = render([
    "[外链](https://example.test/docs?q=1&order=2)",
    "http://example.test/",
    "[房间](/rooms/A)",
    "[上一层](../guide)",
    "[章节](#start)",
    "[查询](?page=2)",
  ].join(" "));

  assert.match(html, /<a href="https:\/\/example.test\/docs\?q=1&amp;order=2" target="_blank" rel="noopener noreferrer">外链<\/a>/);
  assert.match(html, /<a href="http:\/\/example.test\/" target="_blank" rel="noopener noreferrer">http:\/\/example.test\/<\/a>/);
  assert.match(html, /<a href="\/rooms\/A">房间<\/a>/);
  assert.match(html, /<a href="\.\.\/guide">上一层<\/a>/);
  assert.match(html, /<a href="#start">章节<\/a>/);
  assert.match(html, /<a href="\?page=2">查询<\/a>/);
});

test("rejects unsafe destinations, including encoded schemes and network paths", () => {
  const destinations = [
    "javascript:example",
    "JaVaScRiPt:example",
    "java&#x73;cript:example",
    "%6Aavascript%3Aexample",
    "java%09script:example",
    "data:text/plain,example",
    "file:///example",
    "//example.test",
    "/%2fexample.test",
    "%2F%2Fexample.test",
    "/%5cexample.test",
  ];
  for (const destination of destinations) {
    assert.doesNotMatch(render(`[显示文本](${destination})`), /<a\b/i, destination);
  }
});
