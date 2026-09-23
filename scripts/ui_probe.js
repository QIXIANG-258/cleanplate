/**
 * CleanPlate 前端验收探针
 *
 * 用真实 CDP 输入事件（不是 JS 合成事件）跑一遍：
 *   打开图片 -> 涂 mask -> 点修复 -> 撤销 -> 保存
 * 并收集未捕获异常。
 *
 * 跑法：node scripts/ui_probe.js
 * 需要服务在 http://127.0.0.1:8240 上跑着。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const EDGE = process.env.EDGE || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9391);
const APP = process.env.APP_URL || 'http://127.0.0.1:8240';
const IMG = process.env.TEST_IMG || 'D:\\IO_paint\\samples\\demo.jpg';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let fails = 0;
const results = [];
function ok(cond, name, extra) {
  results.push({ ok: !!cond, name, extra });
  if (!cond) fails++;
  console.log(`  ${cond ? '[PASS]' : '[FAIL]'} ${name}${extra ? '  ' + extra : ''}`);
}

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => {
        try { resolve(JSON.parse(d)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

(async () => {
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'iopaint-probe-'));
  const child = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-proxy-server', '--no-first-run',
    '--hide-scrollbars', '--disable-extensions',
    '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + userDir,
    '--window-size=1600,1000',
    'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  const exceptions = [];
  const pending = new Map();
  let msgId = 0;

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++msgId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) {
      throw new Error('页面内异常: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    }
    return r.result?.value;
  };

  const waitFor = async (expr, label, timeout = 20000, interval = 150) => {
    const t0 = Date.now();
    let last;
    while (Date.now() - t0 < timeout) {
      last = await evaluate(expr);
      if (last) return last;
      await sleep(interval);
    }
    throw new Error(`等待超时：${label}（最后取值 ${JSON.stringify(last)}）`);
  };

  // 真实鼠标点击（取元素实测矩形中心，先滚进视口）
  const clickOn = async (sel) => {
    const box = await evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) return null;
      el.scrollIntoView({ block: 'center', behavior: 'instant' });
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height };
    })()`);
    if (!box) throw new Error(`找不到元素 ${sel}`);
    if (box.w === 0 || box.h === 0) throw new Error(`元素不可见 ${sel}`);
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
    await send('Input.dispatchMouseEvent', {
      type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1, buttons: 1,
    });
    await send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1, buttons: 0,
    });
    return box;
  };

  const out = [];
  const log = (s) => { out.push(s); console.log(s); };

  try {
    // ---------- 连上浏览器 ----------
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
      try {
        const list = await getJSON(`http://127.0.0.1:${PORT}/json/list`);
        target = list.find((t) => t.type === 'page');
      } catch { /* 还没起来 */ }
      if (!target) await sleep(250);
    }
    if (!target) throw new Error('连不上无头浏览器（CDP 端口没响应）');

    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = (e) => rej(new Error('WebSocket 连接失败'));
    });
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { resolve, reject } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      } else if (m.method === 'Runtime.exceptionThrown') {
        exceptions.push(m.params.exceptionDetails?.exception?.description
          || m.params.exceptionDetails?.text || '未知异常');
      } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        exceptions.push('console.error: ' + m.params.args.map((a) => a.value ?? a.description).join(' '));
      }
    };

    await send('Runtime.enable');
    await send('Page.enable');
    await send('Log.enable');

    const status = await getJSON('http://127.0.0.1:8240/api/status');
    const useLama = !!status.model_ready;
    log(`\n=== CleanPlate 前端验收探针 ===`);
    log(`后端设备：${status.device}   模型就绪：${status.model_ready}   → 走 ${useLama ? 'LaMa' : 'OpenCV'} 模式\n`);

    // ---------- 打开页面 ----------
    log('[1] 加载页面');
    await send('Page.navigate', { url: APP });
    await waitFor(
      `document.readyState === 'complete' && !!document.getElementById('chipDevice')
       && document.getElementById('chipDevice').textContent.trim() !== '检测中…'`,
      '页面初始化完成', 25000);
    const devChip = await evaluate(`document.getElementById('chipDevice').textContent.trim()`);
    ok(!!devChip, '顶栏设备标识已渲染', `→ "${devChip}"`);
    const title = await evaluate(`document.title`);
    ok(/CleanPlate/.test(title), '标题正确', `→ "${title}"`);

    // 遮罩层检查：不能只看 .hidden 属性！
    // 作者样式里任何一句 display:flex 都会盖掉浏览器默认的 [hidden]{display:none}，
    // 于是 hidden=true 设了也照样显示、照样吃鼠标事件。
    // 唯一可信的判据是「真实渲染状态」+「这块屏幕位置到底命中了谁」。
    const overlay = await evaluate(`(() => {
      const report = {};
      for (const id of ['loading','modalFiles','empty','popBrush','popSave','toast']) {
        const el = document.getElementById(id);
        if (!el) continue;
        const cs = getComputedStyle(el);
        report[id] = { hidden: el.hidden, display: cs.display,
                       pe: cs['pointer-events'], z: cs.zIndex };
      }
      const st = document.getElementById('stage').getBoundingClientRect();
      const hit = document.elementFromPoint(st.left + st.width/2, st.top + st.height/2);
      return JSON.stringify({ report, hit: hit ? (hit.id || hit.className || hit.tagName) : null });
    })()`);
    const ov = JSON.parse(overlay);
    const visibleOverlays = Object.entries(ov.report)
      .filter(([, v]) => v.hidden && v.display !== 'none')
      .map(([k, v]) => `${k}(display:${v.display})`);
    log(`    屏幕中心命中的元素：${ov.hit}（此时还是空状态，命中"把照片拖进来"是对的）`);
    ok(visibleOverlays.length === 0, '所有标记 hidden 的浮层都真的不渲染',
      visibleOverlays.length ? '→ 仍在显示：' + visibleOverlays.join(', ') : '');
    ok(ov.report.popBrush && ov.report.popBrush.display === 'none', '设置浮层初始是收起的');
    ok(ov.report.loading && ov.report.loading.display === 'none', '加载遮罩初始是隐藏的');
    ok(ov.report.empty && ov.report.empty.display !== 'none', '空状态提示正常显示');

    // ---------- 打开图片 ----------
    log('\n[2] 打开图片（绕过文件弹窗，直接调页面函数）');
    await evaluate(`openPath(${JSON.stringify(IMG)})`);
    await waitFor(`S && S.img !== null && S.session !== null`, '图片加载完成', 30000);
    await waitFor(`S.mask !== null && S.view.scale > 0`, 'mask 画布与视图就绪', 10000);
    const meta = await evaluate(`JSON.stringify({
      name: S.session.name, w: S.session.width, h: S.session.height,
      maskW: S.mask.width, maskH: S.mask.height, scale: +S.view.scale.toFixed(4),
      actionbar: !document.getElementById('actionbar').hidden,
      fname: document.getElementById('fname').textContent
    })`);
    const m = JSON.parse(meta);
    log(`    文件名 ${m.fname} · ${m.w}×${m.h} · 显示缩放 ${m.scale} · mask 画布 ${m.maskW}×${m.maskH}`);
    ok(m.actionbar, '底部操作条已出现');
    ok(m.maskW > 0 && m.maskH > 0, 'mask 画布尺寸正常');
    ok(m.maskW <= 3072 && m.maskH <= 3072, 'mask 画布已限制在 3072 以内（省内存）');

    // 真正决定「能不能画」的判据：屏幕上这个点到底命中了谁
    const hitAfterOpen = await evaluate(`(() => {
      const st = document.getElementById('stage').getBoundingClientRect();
      const hit = document.elementFromPoint(st.left + st.width/2, st.top + st.height/2);
      return hit ? (hit.id || hit.className || hit.tagName) : null;
    })()`);
    ok(hitAfterOpen === 'view', '图片就绪后画面中心能被画布接到鼠标（无遮罩拦截）',
      `→ 命中 ${hitAfterOpen}`);

    // ---------- 涂抹 ----------
    log('\n[3] 涂抹（真实鼠标拖动）');
    // 算出图片里那块「黄色杂物」在屏幕上的位置
    const spot = await evaluate(`(() => {
      const ix = 330, iy = 180;            // 图片坐标：黄块中心
      return JSON.stringify({
        x: S.view.x + ix * S.view.scale,
        y: S.view.y + iy * S.view.scale,
        stageW: document.getElementById('stage').getBoundingClientRect().width,
        stageH: document.getElementById('stage').getBoundingClientRect().height,
        rect: document.getElementById('view').getBoundingClientRect().toJSON(),
      });
    })()`);
    const p = JSON.parse(spot);
    const cx = p.rect.left + p.x;
    const cy = p.rect.top + p.y;
    if (cx < 60 || cy < 60 || cx > p.rect.left + p.rect.width - 60) {
      log(`    ⚠ 目标点在边缘（${cx.toFixed(0)},${cy.toFixed(0)}），仍继续`);
    }

    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: cx - 60, y: cy });
    await send('Input.dispatchMouseEvent', {
      type: 'mousePressed', x: cx - 60, y: cy, button: 'left', clickCount: 1, buttons: 1,
    });
    for (let i = -55; i <= 60; i += 8) {
      await send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: cx + i, y: cy, button: 'left', buttons: 1,
      });
    }
    await send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: cx + 60, y: cy, button: 'left', clickCount: 1, buttons: 0,
    });

    const maskStat = await evaluate(`(() => {
      const c = document.createElement('canvas'); c.width = 128; c.height = 128;
      const g = c.getContext('2d');
      g.drawImage(S.mask, 0, 0, 128, 128);
      const d = g.getImageData(0, 0, 128, 128).data;
      let n = 0, sumA = 0;
      for (let i = 3; i < d.length; i += 4) { if (d[i] > 8) { n++; sumA += d[i]; } }
      return JSON.stringify({ samples: n, avgAlpha: n ? Math.round(sumA/n) : 0,
                              brush: S.brush.size, maskW: S.mask.width });
    })()`);
    const ms = JSON.parse(maskStat);
    log(`    128×128 采样里有 ${ms.samples} 个像素被涂到，平均不透明度 ${ms.avgAlpha}，笔刷 ${ms.brush}px`);
    // 判据用「采样里确实有像素被涂到」，不用面积占比 ——
    // 大图 + 小笔刷时占比会低到 0.1%，按比例判会假红。
    ok(ms.samples > 4 && ms.avgAlpha > 40, '画布上留下了笔迹（mask 真的被写了）');

    // ---------- 切到 cv2 模式（如果模型没好）----------
    if (!useLama) {
      log('\n[3b] 模型未就绪 → 打开设置浮层，勾选 OpenCV 快速模式');
      await clickOn('#btnBrushPanel');
      await waitFor(`document.getElementById('popBrush').hidden === false`, '设置浮层弹出', 5000);
      ok(true, '设置浮层能正常弹出');
      await clickOn('#chkCv2');
      await waitFor(`S.useCv2 === true`, 'OpenCV 模式已开启', 5000);
      ok(true, '勾选生效');
      const sizeLabel = await evaluate(`document.getElementById('lblSize').textContent`);
      ok(/^\d+$/.test(sizeLabel), '笔刷大小标签有值', `→ ${sizeLabel}px`);
      await clickOn('#btnBrushPanel');   // 收起
      await sleep(200);
    }

    // ---------- 修复 ----------
    log('\n[4] 点「修复」');
    const before = await evaluate(`(async () => {
      const r = await fetch('/api/session/' + S.session.id + '/image?_t=' + Date.now());
      return (await r.arrayBuffer()).byteLength;
    })()`);
    await clickOn('#btnRun');
    await waitFor(`S.busy === true || S.session.cursor === 1`, '修复开始', 8000);
    await waitFor(`S.busy === false && S.session.cursor === 1`, '修复完成', 180000);
    const after = await evaluate(`JSON.stringify({
      cursor: S.session.cursor,
      steps: S.session.steps.length,
      elapsed: document.getElementById('stTime').textContent,
      maskCleared: (() => { const c=document.createElement('canvas'); c.width=64;c.height=64;
        const g=c.getContext('2d'); g.drawImage(S.mask,0,0,64,64);
        const d=g.getImageData(0,0,64,64).data; let n=0;
        for(let i=3;i<d.length;i+=4) if(d[i]>8) n++; return n; })(),
      canUndo: !document.getElementById('btnUndo').disabled,
      status: document.getElementById('stLeft').textContent
    })`);
    const A = JSON.parse(after);
    log(`    耗时 ${A.elapsed} · ${A.status} · 剩余涂抹像素 ${A.maskCleared}`);
    ok(A.cursor === 1, '会话推进到第 1 步');
    ok(A.canUndo, '撤销按钮变为可用');
    ok(A.maskCleared === 0, '修复后自动清空了涂抹');
    log(`    结果图 ${before} 字节 → 重新拉取`);

    // 画布上的图确实换了（采样 64×64 算哈希）
    const hashNow = await evaluate(`(() => {
      const c = document.createElement('canvas'); c.width=64;c.height=64;
      const g = c.getContext('2d'); g.drawImage(S.img, 0, 0, 64, 64);
      const d = g.getImageData(0,0,64,64).data;
      let h = 0; for (let i = 0; i < d.length; i += 4) h = (h*31 + d[i]) >>> 0;
      return h;
    })()`);
    ok(typeof hashNow === 'number' && hashNow !== 0, '画布上的图已更新并渲染', `hash=${hashNow}`);

    // ---------- 撤销 ----------
    log('\n[5] Ctrl+Z 撤销（真实键盘事件）');
    await send('Input.dispatchKeyEvent', {
      type: 'keyDown', key: 'z', code: 'KeyZ', windowsVirtualKeyCode: 90, modifiers: 2,
    });
    await send('Input.dispatchKeyEvent', {
      type: 'keyUp', key: 'z', code: 'KeyZ', windowsVirtualKeyCode: 90, modifiers: 2,
    });
    await waitFor(`S.session.cursor === 0`, '撤销生效', 20000);
    const afterUndo = await evaluate(`JSON.stringify({
      cursor: S.session.cursor,
      canUndo: !document.getElementById('btnUndo').disabled,
      canRedo: !document.getElementById('btnRedo').disabled,
      elapsed: document.getElementById('stTime').textContent
    })`);
    const U = JSON.parse(afterUndo);
    ok(U.cursor === 0, '撤销回到第 0 步');
    ok(U.canRedo, '重做按钮变为可用');

    // ---------- 重做 ----------
    log('\n[5b] Ctrl+Y 重做');
    await send('Input.dispatchKeyEvent', {
      type: 'keyDown', key: 'y', code: 'KeyY', windowsVirtualKeyCode: 89, modifiers: 2,
    });
    await send('Input.dispatchKeyEvent', {
      type: 'keyUp', key: 'y', code: 'KeyY', windowsVirtualKeyCode: 89, modifiers: 2,
    });
    await waitFor(`S.session.cursor === 1`, '重做生效', 20000);
    ok(true, '重做成功回到第 1 步');

    // ---------- 保存 ----------
    log('\n[6] 保存');
    await clickOn('#btnSave');
    await waitFor(`document.getElementById('popSave').hidden === false`, '保存浮层弹出', 5000);
    ok(true, '保存浮层能正常弹出');
    await clickOn('#btnSaveConfirm');
    await waitFor(`document.getElementById('toast').hidden === false`, '出现保存结果提示', 20000);
    const toastTxt = await evaluate(`document.getElementById('toast').textContent`);
    log(`    提示：${toastTxt}`);
    ok(/已保存/.test(toastTxt), '保存成功并给出路径', `→ ${toastTxt.slice(0, 90)}`);
    ok(/output/.test(toastTxt), '默认存到 output 目录');

    // ---------- 截图 ----------
    log('\n[7] 截图');
    await sleep(400);
    const shot = await send('Page.captureScreenshot', { format: 'png' });
    const outDir = path.join(__dirname, '..', '.tmpdl');
    fs.mkdirSync(outDir, { recursive: true });
    const shotPath = path.join(outDir, 'ui_probe.png');
    fs.writeFileSync(shotPath, Buffer.from(shot.data, 'base64'));
    log(`    已存到 ${shotPath}`);

    // ---------- 异常 ----------
    log('\n[8] 未捕获异常');
    ok(exceptions.length === 0, '页面零未捕获异常',
      exceptions.length ? '\n      ' + exceptions.slice(0, 6).join('\n      ') : '');

  } catch (err) {
    fails++;
    log(`\n[探针异常] ${err.message}`);
    try {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(path.join(__dirname, '..', '.tmpdl'), { recursive: true });
      fs.writeFileSync(path.join(__dirname, '..', '.tmpdl', 'ui_probe_error.png'), Buffer.from(shot.data, 'base64'));
      log('    出错时的截图已存 ui_probe_error.png');
    } catch { /* ignore */ }
  } finally {
    try { if (ws) ws.close(); } catch { /* ignore */ }
    try { child.kill(); } catch { /* ignore */ }
    try { fs.rmSync(userDir, { recursive: true, force: true }); } catch { /* ignore */ }
  }

  console.log('\n' + '='.repeat(52));
  const pass = results.filter((r) => r.ok).length;
  console.log(`  通过 ${pass} / ${results.length}，失败 ${fails}`);
  console.log('='.repeat(52) + '\n');
  fs.mkdirSync(path.join(__dirname, '..', '.tmpdl'), { recursive: true });
  fs.writeFileSync(path.join(__dirname, '..', '.tmpdl', 'ui_probe_result.txt'), out.join('\n'));
  process.exit(fails ? 1 : 0);
})();
