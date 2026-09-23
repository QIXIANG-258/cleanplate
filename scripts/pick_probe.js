/**
 * 选片界面验收探针
 *
 * 用真实鼠标事件跑一遍：打开弹窗 → 列表模式 → 切缩略图模式 → 方向键选择
 * → 大图预览联动 → 隐藏预览 → 双击打开。
 *
 * 跑法：node scripts/pick_probe.js   （需要服务在 8240 上跑着）
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const EDGE = process.env.EDGE || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9395);
const APP = process.env.APP_URL || 'http://127.0.0.1:8240';
// 默认用项目自带的测试图（8 张不同构图）；换成你自己的目录也行：set TEST_DIR=...
const DIR = process.env.TEST_DIR || path.join(__dirname, '..', 'samples', 'pick-test');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let fails = 0;
const results = [];
function ok(cond, name, extra) {
  results.push({ ok: !!cond, name });
  if (!cond) fails++;
  console.log(`  ${cond ? '[PASS]' : '[FAIL]'} ${name}${extra ? '  ' + extra : ''}`);
}

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

(async () => {
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pick-probe-'));
  const child = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-proxy-server', '--no-first-run',
    '--hide-scrollbars', '--window-size=1600,1000',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir, 'about:blank',
  ], { stdio: 'ignore' });

  let ws, msgId = 0;
  const pending = new Map();
  const exceptions = [];

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('页面异常: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result?.value;
  };

  const waitFor = async (expr, label, timeout = 15000, interval = 120) => {
    const t0 = Date.now();
    let last;
    while (Date.now() - t0 < timeout) {
      last = await ev(expr);
      if (last) return last;
      await sleep(interval);
    }
    throw new Error(`等待超时：${label}（最后 ${JSON.stringify(last)}）`);
  };

  const clickAt = async (x, y, clickCount = 1) => {
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
    for (let n = 1; n <= clickCount; n++) {
      await send('Input.dispatchMouseEvent', {
        type: 'mousePressed', x, y, button: 'left', clickCount: n, buttons: 1,
      });
      await send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x, y, button: 'left', clickCount: n, buttons: 0,
      });
      if (n < clickCount) await sleep(30);
    }
  };

  const clickEl = async (sel, clickCount = 1) => {
    const box = await ev(`(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return { zero: true };
      return { x: r.left + r.width/2, y: r.top + r.height/2 };
    })()`);
    if (!box) throw new Error('找不到元素 ' + sel);
    if (box.zero) throw new Error('元素不可见 ' + sel);
    await clickAt(box.x, box.y, clickCount);
  };

  /**
   * 取第 idx 个文件格子的中心点。
   * ⚠ 必须先滚进视口再量 —— 网格超出可视区时 getBoundingClientRect 会给出
   *   视口外的坐标，照着点等于点了个空（症状像"功能坏了"，其实是探针打偏）。
   */
  const cellCenter = async (idx) => {
    const sel = `#fileList .row-item.file:nth-of-type(${idx + 1})`;
    await ev(`(() => {
      const all = document.querySelectorAll('#fileList .row-item.file');
      const el = all[${idx}];
      if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' });
      return true;
    })()`);
    await sleep(220);
    const raw = await ev(`(() => {
      const el = document.querySelectorAll('#fileList .row-item.file')[${idx}];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const rn = el.querySelector('.rn');
      return JSON.stringify({
        x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height,
        name: rn ? rn.textContent : '',
        inView: r.left >= 4 && r.top >= 4 && r.right <= innerWidth - 4 && r.bottom <= innerHeight - 4
      });
    })()`);
    if (!raw) throw new Error(`找不到第 ${idx} 个文件格子`);
    const b = JSON.parse(raw);
    if (!b.inView) throw new Error(`第 ${idx} 个格子滚进视口后仍在视口外：${raw}`);
    return b;
  };

  const pressKey = async (key, code, vk, modifiers = 0) => {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode: vk, modifiers });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: vk, modifiers });
  };

  const log = (s) => console.log(s);

  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      try { target = (await getJSON(`http://127.0.0.1:${PORT}/json/list`)).find((t) => t.type === 'page'); } catch {}
      if (!target) await sleep(250);
    }
    if (!target) throw new Error('连不上无头浏览器');

    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws 失败')); });
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.id && pending.has(d.id)) {
        const { resolve, reject } = pending.get(d.id);
        pending.delete(d.id);
        d.error ? reject(new Error(JSON.stringify(d.error))) : resolve(d.result);
      } else if (d.method === 'Runtime.exceptionThrown') {
        exceptions.push(d.params.exceptionDetails?.exception?.description || d.params.exceptionDetails?.text);
      } else if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') {
        exceptions.push('console.error: ' + d.params.args.map((a) => a.value ?? a.description).join(' '));
      }
    };
    await send('Runtime.enable');
    await send('Page.enable');

    log('\n=== 选片界面验收探针 ===');

    await send('Page.navigate', { url: APP });
    await waitFor(`document.readyState === 'complete' && !!document.getElementById('chipDevice')`, '页面就绪', 25000);

    // ---------- 打开弹窗 ----------
    log('\n[1] 用 Ctrl+O 打开选片弹窗');
    await ev(`localStorage.removeItem('iopaint.view'); curDir = ${JSON.stringify(DIR)}; true`);
    await pressKey('o', 'KeyO', 79, 2);   // modifiers 2 = Ctrl
    await waitFor(`document.getElementById('modalFiles').hidden === false`, '弹窗出现', 8000);
    ok(true, 'Ctrl+O 能打开选片弹窗');

    await waitFor(`document.querySelectorAll('#fileList .row-item.file').length > 0`, '文件列表渲染', 10000);
    const n0 = await ev(`document.querySelectorAll('#fileList .row-item.file').length`);
    ok(n0 >= 6, `列出了 ${n0} 张图片`);

    const dims = await ev(`JSON.stringify(Array.from(document.querySelectorAll('#fileList .row-item.file .rs')).map(e=>e.textContent))`);
    log('    每行信息：' + JSON.parse(dims).slice(0, 4).join('  |  ') + ' …');
    ok(/^\d+×\d+ · /.test(JSON.parse(dims)[0]), '列表里带上了真实尺寸', '');

    // ---------- 缩略图网格 ----------
    log('\n[2] 缩略图网格');
    await waitFor(`Array.from(document.querySelectorAll('#fileList img.row-thumb'))
                    .filter(i => i.naturalWidth > 0).length >= 6`, '缩略图加载', 25000);
    const gridInfo = JSON.parse(await ev(`(() => {
      const rows = document.querySelectorAll('#fileList .row-item');
      const top0 = rows[0] ? rows[0].offsetTop : 0;
      let cols = 0;
      for (const r of rows) { if (r.offsetTop === top0) cols++; else break; }
      const thumbs = Array.from(document.querySelectorAll('#fileList img.row-thumb'));
      return JSON.stringify({
        cols,
        total: thumbs.length,
        loaded: thumbs.filter(i => i.naturalWidth > 0).length,
        sampleW: thumbs[0] ? thumbs[0].naturalWidth : 0,
        thumbCssH: thumbs[0] ? Math.round(thumbs[0].getBoundingClientRect().height) : 0,
        hasCaption: !!rows[0].querySelector('.rn')
      });
    })()`));
    log(`    网格 ${gridInfo.cols} 列 · 缩略图 ${gridInfo.loaded}/${gridInfo.total} 已加载 · 源宽 ${gridInfo.sampleW}px · 显示高 ${gridInfo.thumbCssH}px`);
    ok(gridInfo.cols >= 2, '网格排布成多列');
    ok(gridInfo.loaded >= 6, '缩略图都加载出来了');
    ok(gridInfo.sampleW > 100, '缩略图有真实内容（不是空壳）');
    ok(gridInfo.thumbCssH > 80, '缩略图有合理高度');
    ok(gridInfo.hasCaption, '每格下面带文件名和尺寸');
    ok(await ev(`!!document.getElementById('btnViewList') === false
                 && !!document.getElementById('btnViewGrid') === false`),
       '列表视图的切换按钮已移除');

    // ---------- 大图预览 ----------
    log('\n[3] 大图预览联动');
    await waitFor(`document.getElementById('pvMeta').hidden === false`, '预览元信息出现', 8000);
    await waitFor(`!document.getElementById('pvImg').hidden
                   && document.getElementById('pvImg').naturalWidth > 0`, '预览图加载完成', 15000);
    const pv1 = JSON.parse(await ev(`JSON.stringify({
      name: document.getElementById('pvName').textContent,
      dim: document.getElementById('pvDim').textContent,
      w: document.getElementById('pvImg').naturalWidth,
      h: document.getElementById('pvImg').naturalHeight
    })`));
    log(`    预览：${pv1.name}  ${pv1.dim}  实际 ${pv1.w}x${pv1.h}`);
    ok(pv1.w > 0 && pv1.h > 0, '大图预览真的加载出来了');
    const mm = pv1.dim.match(/(\d+)\s*×\s*(\d+)/);
    const srcRatio = mm ? (+mm[1]) / (+mm[2]) : 0;
    const gotRatio = pv1.w / pv1.h;
    log(`    原图比例 ${srcRatio.toFixed(3)} · 预览比例 ${gotRatio.toFixed(3)} · 长边 ${Math.max(pv1.w, pv1.h)}`);
    ok(Math.abs(srcRatio - gotRatio) < 0.02, '预览保持原始宽高比（没被拉伸压扁）');
    ok(Math.max(pv1.w, pv1.h) <= 1800, '预览按上限缩过（不是原图直出，省流量）');

    // ---------- 方向键选择 ----------
    log('\n[4] 方向键选择');
    await pressKey('ArrowDown', 'ArrowDown', 40);
    await sleep(500);
    const pv2 = await ev(`document.getElementById('pvName').textContent`);
    log(`    ↓ 之后预览：${pv2}`);
    ok(pv2 !== pv1.name, '↓ 切换了预览的图（按整行跳）');
    const movedSel = await ev(`document.querySelectorAll('#fileList .row-item.sel').length`);
    ok(movedSel === 1, '始终只有一个格子是选中态', `→ ${movedSel}`);

    await pressKey('ArrowRight', 'ArrowRight', 39);
    await sleep(450);
    const pv3 = await ev(`document.getElementById('pvName').textContent`);
    log(`    → 之后预览：${pv3}`);
    ok(pv3 !== pv2, '→ 也能逐个切换');
    await pressKey('ArrowLeft', 'ArrowLeft', 37);
    await sleep(450);
    ok(await ev(`document.getElementById('pvName').textContent`) === pv2, '← 能退回来');

    // ---------- 点一个格子 ----------
    log('\n[5] 点缩略图选中');
    const before = await ev(`document.getElementById('pvName').textContent`);
    const tg = await cellCenter(5);
    await clickAt(tg.x, tg.y);
    await waitFor(`document.getElementById('pvName').textContent === ${JSON.stringify(tg.name)}`, '预览切到点击的那张', 8000);
    log(`    ${before}  ->  ${tg.name}`);
    ok(true, '点缩略图能选中并联动预览');

    // ---------- 隐藏大图 ----------
    log('\n[6] 隐藏 / 显示大图');
    await clickEl('#btnTogglePreview');
    await sleep(350);
    const hidden = await ev(`getComputedStyle(document.querySelector('.modal-preview')).display === 'none'`);
    const listW = await ev(`Math.round(document.getElementById('fileList').getBoundingClientRect().width)`);
    const colsWide = await ev(`(() => {
      const rows = document.querySelectorAll('#fileList .row-item');
      const top0 = rows[0] ? rows[0].offsetTop : 0;
      let n = 0; for (const r of rows) { if (r.offsetTop === top0) n++; else break; }
      return n;
    })()`);
    ok(hidden, '大图区已隐藏');
    ok(listW > 900, '隐藏后网格铺满宽度', `→ ${listW}px`);
    ok(colsWide > gridInfo.cols, '铺满后一屏能放更多格', `→ ${colsWide} 列`);
    await clickEl('#btnTogglePreview');
    await sleep(350);
    ok(await ev(`getComputedStyle(document.querySelector('.modal-preview')).display !== 'none'`), '能再显示回来');

    // ---------- 双击打开 ----------
    log('\n[7] 双击打开一张');
    const t2 = await cellCenter(2);
    await clickAt(t2.x, t2.y, 2);
    await waitFor(`S && S.session !== null`, '会话建立', 20000);
    const opened = await ev(`JSON.stringify({ name: S.session.name, w: S.session.width, h: S.session.height,
                                              modal: document.getElementById('modalFiles').hidden })`);
    const op = JSON.parse(opened);
    log(`    打开了 ${op.name}  ${op.w}x${op.h}  弹窗已关=${op.modal}`);
    ok(op.name === t2.name, '打开的就是双击的那张');
    ok(op.modal === true, '打开后弹窗自动关闭');

    // ---------- 异常 ----------
    log('\n[8] 未捕获异常');
    ok(exceptions.length === 0, '零未捕获异常',
      exceptions.length ? '\n      ' + exceptions.slice(0, 5).join('\n      ') : '');

    log('\n[9] 截图');
    await ev(`curDir = ${JSON.stringify(DIR)}; openFileModal()`);
    await sleep(1200);
    const shot = await send('Page.captureScreenshot', { format: 'png' });
    const outDir = path.join(__dirname, '..', '.tmpdl');
    fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(path.join(outDir, 'pick_grid.png'), Buffer.from(shot.data, 'base64'));
    log('    已存 .tmpdl/pick_grid.png');

  } catch (err) {
    fails++;
    console.log('\n[探针异常] ' + err.message);
    try {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(__dirname, '..', '.tmpdl', 'pick_error.png'), Buffer.from(shot.data, 'base64'));
      console.log('    出错截图 pick_error.png');
    } catch {}
  } finally {
    try { ws?.close(); } catch {}
    try { child.kill(); } catch {}
    try { fs.rmSync(userDir, { recursive: true, force: true }); } catch {}
  }

  console.log('\n' + '='.repeat(52));
  console.log(`  通过 ${results.filter(r => r.ok).length} / ${results.length}，失败 ${fails}`);
  console.log('='.repeat(52) + '\n');
  process.exit(fails ? 1 : 0);
})();
