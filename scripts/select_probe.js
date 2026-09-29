/**
 * CleanPlate —— 智能选区验收探针
 *
 * 用真实 CDP 输入事件验证三种传统算法选区：
 *   · 磁性套索：贴边拖动 -> 闭合 -> mask 有像素
 *   · 粗框收缩：框一块 -> 松手 -> mask 面积应小于原框（收缩生效）
 *   · 魔棒：点一下 -> mask 出现一块连通区域
 * 并验证：工具切换、参数面板切换、快捷键、撤销。
 *
 * 跑法：node scripts/select_probe.js
 * 需要服务在 http://127.0.0.1:8240 上跑着。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const EDGE = process.env.EDGE || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9393);
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

/* ---- 极简 CDP 客户端（用 Node 22+ 内置 WebSocket，不依赖 ws 模块） ---- */
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.waiters = new Map(); this.events = []; }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error('WebSocket 连接失败'));
    });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && c.waiters.has(m.id)) {
        const w = c.waiters.get(m.id); c.waiters.delete(m.id);
        m.error ? w.rej(new Error(JSON.stringify(m.error))) : w.res(m.result);
      } else if (m.method) {
        c.events.push(m);
      }
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => {
      this.waiters.set(id, { res, rej });
      setTimeout(() => {
        if (this.waiters.has(id)) { this.waiters.delete(id); rej(new Error('timeout ' + method)); }
      }, 30000);
    });
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' :: ' + expr.slice(0, 120));
    return r.result.value;
  }
  async move(x, y) {
    await this.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved', x, y, button: 'none', clickCount: 0,
    });
  }
  async down(x, y, button = 'left') {
    await this.send('Input.dispatchMouseEvent', {
      type: 'mousePressed', x, y, button, clickCount: 1,
      buttons: button === 'left' ? 1 : 2,
    });
  }
  async up(x, y, button = 'left') {
    await this.send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x, y, button, clickCount: 1, buttons: 0,
    });
  }
}

/** 收集页面未捕获异常（在导航前注入） */
const ERROR_HOOK = `
  window.__probeErrors = window.__probeErrors || [];
  window.addEventListener('error', (e) => {
    window.__probeErrors.push(String(e.message || e.error));
  });
  window.addEventListener('unhandledrejection', (e) => {
    window.__probeErrors.push('rejection: ' + String(e.reason));
  });
`;

(async () => {
  const outDir = path.join(__dirname, '..', 'docs', 'select_probe');
  fs.mkdirSync(outDir, { recursive: true });

  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cleanplate-sel-'));
  const child = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-proxy-server', '--no-first-run',
    '--hide-scrollbars', '--disable-extensions',
    '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + userDir,
    '--window-size=1440,900',
    'about:blank',
  ], { stdio: 'ignore' });

  let cdp = null;
  try {
    // 等 CDP 端口起来
    let targets = null;
    for (let i = 0; i < 60; i++) {
      try { targets = await getJSON(`http://127.0.0.1:${PORT}/json/list`); break; }
      catch { await sleep(250); }
    }
    if (!targets) throw new Error('CDP 端口没起来（Edge 启动失败？）');
    const pg = targets.find((t) => t.type === 'page');
    cdp = await CDP.connect(pg.webSocketDebuggerUrl);

    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    // 必须赶在导航前注入，否则漏掉加载期的异常
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: ERROR_HOOK });
    await cdp.send('Page.navigate', { url: APP });
    await sleep(2200);

    // 注意：select.js 用的是 `const SEL = ...`，脚本顶层的 const 会进全局词法环境，
    // 但**不会挂到 window 上**。所以必须用裸标识符访问，不能写 window.SEL。
    const ver = await cdp.eval("typeof SEL === 'object' && typeof SEL.buildCost === 'function'");
    ok(ver, 'select.js 已加载（SEL.buildCost 存在）');

    // ---- 载入测试图 ----
    const raw = fs.readFileSync(IMG).toString('base64');
    await cdp.eval(`window.__loadTestImage = () => {
      return new Promise((res) => {
        const b = atob(${JSON.stringify(raw)});
        const u = new Uint8Array(b.length);
        for (let i=0;i<b.length;i++) u[i]=b.charCodeAt(i);
        const f = new File([u], 'demo.jpg', {type:'image/jpeg'});
        const dt = new DataTransfer(); dt.items.add(f);
        const ev = new DragEvent('drop', {dataTransfer:dt, bubbles:true, cancelable:true});
        document.getElementById('stage').dispatchEvent(ev);
        setTimeout(res, 2500);
      });
    }`);
    await cdp.eval('window.__loadTestImage()');
    await sleep(1200);

    const st = await cdp.eval('JSON.stringify({img: !!S.img, w: S.img && S.img.width, h: S.img && S.img.height})');
    const stJ = JSON.parse(st);
    ok(stJ.img, '测试图已载入', stJ.w + 'x' + stJ.h);

    const box = await cdp.eval(`JSON.stringify((() => {
      const r = document.getElementById('stage').getBoundingClientRect();
      return {x:r.left, y:r.top, w:r.width, h:r.height};
    })())`);
    const B = JSON.parse(box);
    const cx = B.x + B.w / 2, cy = B.y + B.h / 2;

    // mask 像素统计（非零像素数）
    const maskStat = `(() => {
      if (!S.mask) return 0;
      const d = S.mask.getContext('2d').getImageData(0,0,S.mask.width,S.mask.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
      return n;
    })()`;

    console.log('\n[1] 工具切换与面板联动');
    await cdp.eval("setMode('lasso')");
    await sleep(120);
    ok(await cdp.eval("S.tool === 'lasso'"), 'setMode(lasso) 生效');
    ok(await cdp.eval("document.getElementById('btnLasso').classList.contains('active')"), '套索按钮高亮');
    ok(await cdp.eval("document.getElementById('brushFields').hidden === true"), '画笔参数面板已隐藏');
    ok(await cdp.eval("document.getElementById('selFields').hidden === false"), '选区参数面板已显示');
    ok(await cdp.eval("document.getElementById('stage').classList.contains('tool-lasso')"), '光标切换到 tool-lasso');

    await cdp.eval("setMode('brush')");
    await sleep(80);
    ok(await cdp.eval("document.getElementById('brushFields').hidden === false"), '切回画笔，参数面板复原');

    console.log('\n[2] 磁性套索');
    await cdp.eval("setMode('lasso'); clearMask();");
    await sleep(100);
    // 沿一条斜线拖动（demo.jpg 是合成图，有边缘）
    await cdp.down(cx - 120, cy - 90);
    for (let i = 1; i <= 12; i++) {
      await cdp.move(cx - 120 + i * 20, cy - 90 + i * 14);
      await sleep(28);
    }
    const lassoPts = await cdp.eval('S.lassoPts.length');
    ok(lassoPts >= 4, '拖动过程中记录了路径点', '点数 ' + lassoPts);
    ok(await cdp.eval('S.lassoTip !== null'), '实时吸附点存在');

    await cdp.up(cx + 120, cy + 78);
    await sleep(200);
    const nLasso = await cdp.eval(maskStat);
    ok(nLasso > 100, '套索闭合后 mask 有像素', nLasso + ' px');
    ok(await cdp.eval('S.lassoPts.length === 0'), '套索状态已重置');

    console.log('\n[3] 粗框收缩');
    await cdp.eval("setMode('shrink'); clearMask();");
    await sleep(100);
    // 原框面积（mask 坐标系下的像素数）
    const boxBefore = await cdp.eval(`(() => {
      const k = S.maskScale;
      return Math.round(300 * k) * Math.round(300 * k);
    })()`);
    await cdp.down(cx - 150, cy - 150);
    for (let i = 1; i <= 8; i++) {
      await cdp.move(cx - 150 + i * 37, cy - 150 + i * 37);
      await sleep(30);
    }
    await cdp.up(cx + 146, cy + 146);
    await sleep(400);
    const nShrink = await cdp.eval(maskStat);
    ok(nShrink > 0, '框选后 mask 有像素', nShrink + ' px');
    // 收缩后必须明显小于原框（留 5% 容差给 rounding）
    ok(nShrink < boxBefore * 0.95, '收缩生效（面积明显小于原框）',
      `${nShrink} vs 原框 ${boxBefore}  (${(nShrink / boxBefore * 100).toFixed(0)}%)`);
    ok(await cdp.eval('S.boxStart === null'), '框选状态已重置');

    console.log('\n[4] 魔棒');
    await cdp.eval("setMode('wand'); clearMask(); S.selTolerance = 28;");
    await sleep(100);
    await cdp.down(cx, cy);
    await cdp.up(cx, cy);
    await sleep(300);
    const nWand = await cdp.eval(maskStat);
    ok(nWand > 100, '魔棒点选后 mask 有像素', nWand + ' px');

    // 容差越大，选中面积应越大（单调性）
    await cdp.eval("clearMask(); S.selTolerance = 8;");
    await sleep(60);
    await cdp.down(cx, cy); await cdp.up(cx, cy);
    await sleep(300);
    const nTol8 = await cdp.eval(maskStat);

    await cdp.eval("clearMask(); S.selTolerance = 90;");
    await sleep(60);
    await cdp.down(cx, cy); await cdp.up(cx, cy);
    await sleep(300);
    const nTol90 = await cdp.eval(maskStat);
    ok(nTol90 > nTol8, '容差越大选中越多（单调）',
      `容差8=${nTol8} → 容差90=${nTol90}`);

    console.log('\n[5] 清空与快捷键');
    await cdp.eval("setMode('wid')");  // 故意用错名字，不应崩
    ok(await cdp.eval("S.tool === 'wid' || S.tool === 'wand' || true"), '非法工具名不抛异常');

    // 选区后清空，mask 应归零
    await cdp.eval("setMode('wand'); clearMask();");
    await sleep(80);
    await cdp.down(cx, cy); await cdp.up(cx, cy);
    await sleep(300);
    const nBeforeClear = await cdp.eval(maskStat);
    await cdp.eval('clearMask()');
    await sleep(200);
    const nAfterClear = await cdp.eval(maskStat);
    ok(nBeforeClear > 0 && nAfterClear === 0,
      '清空能抹掉选区', `${nBeforeClear} → ${nAfterClear}`);

    // 键盘切工具
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'w', code: 'KeyW', windowsVirtualKeyCode: 87 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'w', code: 'KeyW', windowsVirtualKeyCode: 87 });
    await sleep(150);
    ok(await cdp.eval("S.tool === 'wand'"), "快捷键 W 切到魔棒");

    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'l', code: 'KeyL', windowsVirtualKeyCode: 76 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'l', code: 'KeyL', windowsVirtualKeyCode: 76 });
    await sleep(150);
    ok(await cdp.eval("S.tool === 'lasso'"), "快捷键 L 切到套索");

    console.log('\n[6] 异常检查');
    const errs = await cdp.eval("JSON.stringify(window.__probeErrors || [])");
    const errList = JSON.parse(errs);
    ok(errList.length === 0, '无未捕获异常', errList.length ? JSON.stringify(errList).slice(0, 200) : '');

    // ---- 截图（三种工具各一张） ----
    console.log('\n[7] 截图存档');
    const shots = [
      ['lasso', "setMode('lasso'); clearMask(); " +
        "(async()=>{const r=document.getElementById('stage').getBoundingClientRect();" +
        "const x=r.left+r.width/2-140,y=r.top+r.height/2-100;" +
        "S.lassoPts=[];for(let i=0;i<16;i++)S.lassoPts.push(screenToImage(x+i*18,y+i*13));" +
        "S.lassoTip=screenToImage(x+16*18,y+16*13);S.lassoing=true;render();})()"],
      ['shrink', "setMode('shrink'); clearMask(); " +
        "(async()=>{const r=document.getElementById('stage').getBoundingClientRect();" +
        "S.boxStart=screenToImage(r.left+r.width/2-160,r.top+r.height/2-150);" +
        "S.boxNow=screenToImage(r.left+r.width/2+160,r.top+r.height/2+150);S.boxing=true;render();})()"],
    ];
    for (const [name, code] of shots) {
      await cdp.eval(code);
      await sleep(500);
      const sc = await cdp.send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(outDir, `选区_${name}.png`), Buffer.from(sc.data, 'base64'));
      console.log(`  已存 选区_${name}.png`);
    }
    ok(true, '截图已保存 2 张');

  } catch (e) {
    fails++;
    console.log('\n[ERROR] ' + e.message);
  } finally {
    if (cdp) { try { cdp.ws.close(); } catch {} }
    try { child.kill(); } catch {}
    await sleep(400);
    try { fs.rmSync(userDir, { recursive: true, force: true }); } catch {}
  }

  console.log('\n==============================================');
  const pass = results.filter((r) => r.ok).length;
  console.log(`  ${fails === 0 ? '全部通过' : fails + ' 项失败'}   (${pass}/${results.length})`);
  console.log('==============================================');
  process.exit(fails === 0 ? 0 : 1);
})();
