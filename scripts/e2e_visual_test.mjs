// -*- coding: utf-8 -*-
/**
 * AI-KM 端到端可视化测试（Node + puppeteer-core，驱动本机 Google Chrome）。
 * 真实访问本地服务 http://localhost:5200，逐用例截图 + 断言，产出 results.json。
 *
 * 运行：
 *   NODE_PATH=/Users/ikingsmart/.workbuddy/binaries/node/workspace/node_modules \
 *   /Users/ikingsmart/.workbuddy/binaries/node/versions/22.22.2/bin/node scripts/e2e_visual_test.mjs
 */
import puppeteer from '/Users/ikingsmart/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';
import fs from 'fs';
import path from 'path';

const ROOT = '/Users/ikingsmart/AI-KM';
const OUT = path.join(ROOT, 'test_report_assets');
fs.mkdirSync(OUT, { recursive: true });

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = 'http://localhost:5200';
const USER = 'admin';
const PWD = 'Admin@123456';

const results = [];
function rec(tc, name, passed, detail, shot) {
  results.push({ tc, name, passed: !!passed, detail, shot });
  console.log(`[${passed ? 'PASS' : 'FAIL'}] ${tc} ${name} —— ${detail}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  // 桌面视口，保证截图信息量
  const page = await browser.newPage();
  await page.setViewport({ width: 1366, height: 900, deviceScaleFactor: 1 });
  page.setDefaultTimeout(20000);
  // 调试：监听 UI 实际发出的检索请求，便于核对真实 URL 与返回
  page.on('response', async (resp) => {
    const u = resp.url();
    if (u.includes('/api/search')) {
      try {
        const j = await resp.json();
        const top = j.results && j.results[0] ? j.results[0].title : '(空)';
        console.log('  [API]', u, '-> TOP1=', top, 'count=', (j.results || []).length);
      } catch (e) {}
    }
  });

  try {
    // ============ TC-01 登录页渲染 ============
    await page.goto(`${BASE}/login`, { waitUntil: 'networkidle2' });
    await sleep(800);
    const shot1 = '01_login.png';
    await page.screenshot({ path: path.join(OUT, shot1) });
    const hasForm = (await page.$('#username')) && (await page.$('#password'));
    rec('TC-01', '登录页渲染', !!hasForm, '登录表单（账号/密码输入框）已渲染', shot1);

    // ============ TC-02 登录成功 → 工作台 ============
    await page.type('#username', USER);
    await page.type('#password', PWD);
    await page.click('button[type=submit]');
    await page.waitForSelector('.cat-card', { timeout: 15000 });
    await sleep(800);
    const shot2 = '02_dashboard.png';
    await page.screenshot({ path: path.join(OUT, shot2) });
    const catCount = await page.$$eval('.cat-card', (els) => els.length);
    rec('TC-02', '登录成功跳转工作台', catCount > 0, `登录后进入知识库工作台，分类卡片 ${catCount} 个`, shot2);

    // ============ TC-03 分类浏览（点「政策法规库」卡片）============
    // 点文本含「政策法规」的分类卡片 → 跳 /search?category_l1=POLICY
    const cards = await page.$$('.cat-card');
    let target = null;
    for (const c of cards) {
      const txt = await page.evaluate((el) => el.textContent, c);
      if (txt.includes('政策法规')) { target = c; break; }
    }
    if (!target) throw new Error('未找到「政策法规」分类卡片');
    await target.click();
    await page.waitForFunction(() => location.href.includes('category_l1=POLICY'), { timeout: 15000 });
    await page.waitForSelector('.result-card', { timeout: 15000 });
    await sleep(800);
    const shot3 = '03_browse_policy.png';
    await page.screenshot({ path: path.join(OUT, shot3) });
    const cardCount = await page.$$eval('.result-card', (els) => els.length);
    const browseMark = await page.$$eval('.result-meta', (els) => els.filter((e) => e.textContent.includes('📁 文档')).length);
    const urlOk = page.url().includes('category_l1=POLICY');
    rec('TC-03', '分类浏览模式', urlOk && cardCount > 0,
      `URL 带 category_l1=POLICY；返回文档 ${cardCount} 篇，浏览标记 ${browseMark} 条`, shot3);

    // ============ TC-04 语义检索「冠脉支架」============
    // 先清空上一步遗留的分类/密级等过滤器与搜索框，避免沿用浏览模式的 category_l1=POLICY
    await page.evaluate(() => {
      ['filterCat', 'filterSec', 'filterTag', 'filterDept'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      document.getElementById('searchInput').value = '';
    });
    await page.type('#searchInput', '冠脉支架');
    // 点击「检索」按钮触发 doSearch（比回车更可靠）
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find((x) => x.textContent.includes('检索'));
      if (b) b.click();
    });
    await sleep(1500);
    await sleep(800);
    const shot4 = '04_search_coronary.png';
    await page.screenshot({ path: path.join(OUT, shot4) });
    const firstTitle = await page.$eval('.result-card .result-title', (el) => el.textContent).catch(() => '');
    const hit = ['集采', '高值', '冠脉'].some((k) => firstTitle.includes(k));
    rec('TC-04', '语义检索召回', hit, `查询『冠脉支架』TOP1=${firstTitle}（应命中冠脉支架集采政策文档）`, shot4);

    // ============ TC-05 关键词检索「DRG」============
    await page.evaluate(() => {
      ['filterCat', 'filterSec', 'filterTag', 'filterDept'].forEach((id) => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      document.getElementById('searchInput').value = '';
    });
    await page.type('#searchInput', 'DRG');
    await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find((x) => x.textContent.includes('检索'));
      if (b) b.click();
    });
    await sleep(1500);
    await sleep(800);
    const shot5 = '05_search_drg.png';
    await page.screenshot({ path: path.join(OUT, shot5) });
    const drgCount = await page.$$eval('.result-card', (els) => els.length);
    rec('TC-05', '关键词检索 DRG', drgCount > 0, `查询『DRG』命中 ${drgCount} 条（DRG 付费改革文档）`, shot5);

    // ============ TC-06 问答带引用 ============
    await page.goto(`${BASE}/ask`, { waitUntil: 'networkidle2' });
    await sleep(800);
    await page.type('#askInput', '冠脉支架集中带量采购何时开始、覆盖哪些品种？');
    await page.evaluate(() => {   // 点「发送」按钮（按文本，避免 Playwright 专属选择器）
      const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('发送'));
      if (btn) btn.click();
    });
    // 等待流式答案完成（出现引用 [n]）
    try { await page.waitForSelector('.cites .cite', { timeout: 25000 }); } catch (e) {}
    await sleep(2500);
    const shot6 = '06_ask_coronary.png';
    await page.screenshot({ path: path.join(OUT, shot6) });
    const ans = await page.$$eval('.msg-bot .msg-bubble', (els) => els.map((e) => e.textContent).join('')).catch(() => '');
    const cites = await page.$$eval('.cites .cite', (els) => els.length).catch(() => 0);
    rec('TC-06', '问答带引用', cites > 0 && ans.length > 10,
      `答案 ${ans.length} 字，引用 ${cites} 条（应基于真实文档标注来源）`, shot6);

    // ============ TC-07 知识库外问题诚实拒答 ============
    await page.click('#askInput', { clickCount: 3 });
    await page.type('#askInput', '山东省药品和医用耗材招采政策知识库的建设预算是多少？');
    await page.evaluate(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('发送'));
      if (btn) btn.click();
    });
    try { await page.waitForSelector('.msg-bot .msg-bubble', { timeout: 25000 }); } catch (e) {}
    await sleep(3000);
    const shot7 = '07_ask_outofscope.png';
    await page.screenshot({ path: path.join(OUT, shot7) });
    const ans2 = await page.$$eval('.msg-bot .msg-bubble', (els) => els.map((e) => e.textContent).join('')).catch(() => '');
    const honest = ['未找到', '未包含', '缺乏', '暂无', '没有', '补充'].some((k) => ans2.includes(k));
    rec('TC-07', '知识库外诚实拒答', honest,
      `知识库外问题应答含诚实标注：${ans2.slice(0, 60)}…`, shot7);
  } catch (e) {
    rec('UNEXPECTED', '执行异常', false, `脚本异常：${e.name}: ${e.message}`);
  } finally {
    await browser.close();
  }

  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 2), 'utf-8');
  const passed = results.filter((r) => r.passed).length;
  console.log(`\n=== 完成：${passed}/${results.length} 通过 ===`);
  console.log('截图目录：', OUT);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
