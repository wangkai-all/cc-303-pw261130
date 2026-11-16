const fs = require('fs');
// 依赖：npm i jsdom（仅用于本地冒烟检查）。先运行 python3 -m risklab.cli serve。
const { JSDOM } = require(process.env.JSDOM_PATH || 'jsdom');
const path = require('path');
const root = path.resolve(__dirname, '..');
const base = process.env.BASE || 'http://127.0.0.1:8765';
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');

const dom = new JSDOM(html, {
  url: `${base}/?cutoff=2026-06-30&mode=batch_holdout&policy=policy-base-v1`,
  runScripts: 'outside-only',
  pretendToBeVisual: true,
});
const { window } = dom;
window.fetch = (url, opts) => fetch(new URL(url, base), opts);
window.console = console;
const errors = [];
window.addEventListener('error', (e) => errors.push(String(e.error || e.message)));
window.eval(appJs);

(async () => {
  await new Promise((r) => setTimeout(r, 1800));
  const doc = window.document;
  const q = (s) => doc.querySelector(s);
  const out = {
    kpis: [...doc.querySelectorAll('.kpi')].map((x) => x.textContent.replace(/\s+/g, ' ').trim()),
    integrity: q('#integrity').textContent.replace(/\s+/g, ' ').trim(),
    hero: q('#heroTitle').textContent.trim(),
    segmentsRows: doc.querySelectorAll('#segmentTable tbody tr').length,
    leakText: q('#leakage').textContent.replace(/\s+/g, ' ').trim().slice(0, 220),
    runs: doc.querySelectorAll('#jobStatus .job').length,
    findings: [...doc.querySelectorAll('.finding')].length,
    maturitySvg: !!q('#maturityChart svg'),
    errors,
  };
  // 点击收缩到月度分层
  const monthTab = [...doc.querySelectorAll('#segmentTabs .tab')].find((b) => b.textContent === '申请月');
  monthTab.dispatchEvent(new window.Event('click', { bubbles: true }));
  out.monthRows = doc.querySelectorAll('#segmentTable tbody tr').length;

  // 追溯一笔
  q('#appSearch').value = 'APP-20240102-00636';
  q('#traceBtn').dispatchEvent(new window.Event('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 900));
  out.trace = q('#traceOut').textContent.replace(/\s+/g, ' ').trim().slice(0, 420);
  out.timelineItems = doc.querySelectorAll('#traceOut .tl-item').length;

  // 版本对比
  q('#compareBtn').dispatchEvent(new window.Event('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 900));
  out.compareRows = doc.querySelectorAll('#compareOut tbody tr').length;
  out.compareWarn = q('#compareOut').textContent.replace(/\s+/g, ' ').trim().slice(0, 200);

  // 切换滚动模式并载入
  q('#mode').value = 'daily_rolling';
  q('#mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 1200));
  out.rollingHero = q('#heroTitle').textContent.trim();
  out.errors = errors;
  console.log(JSON.stringify(out, null, 2));
  const failures = [];
  if (out.errors.length) failures.push('页面脚本报错');
  if (!out.maturitySvg) failures.push('成熟度图未渲染');
  if (!out.segmentsRows) failures.push('分层表为空');
  if (!out.timelineItems) failures.push('追溯时间线为空');
  if (!out.compareRows) failures.push('版本对比为空');
  if (failures.length) {
    console.error('FAIL:', failures.join('；'));
    process.exit(1);
  }
  console.log('UI smoke OK');
  process.exit(0);
})().catch((e) => { console.error('FATAL', e); process.exit(2); });
