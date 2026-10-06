/* ============================================================
   CleanPlate —— 前端逻辑
   涂抹 -> 修复 -> 对比 -> 保存
   ============================================================ */
'use strict';

const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------- 全局状态 */

const S = {
  session: null,
  img: null,          // 当前图（ImageBitmap）
  prev: null,         // 上一版，用于对比
  orig: null,         // 原图，用于对比
  view: { scale: 1, x: 0, y: 0 },
  brush: { size: 60, hardness: 85, mode: 'brush' },
  expand: 6,
  maxSide: 2560,
  grain: 0,          // 默认关。实测补全区主要问题不在颗粒层（见 config.py 注释）
  useCv2: false,
  tile: true,
  autoClear: true,
  mask: null,         // 离屏 mask 画布
  maskScale: 1,
  painting: false,
  panning: false,
  spaceDown: false,
  compare: false,
  compareX: 0.5,
  cursor: { x: 0, y: 0, inside: false },
  busy: false,
  lastPos: null,
  pendingFile: null,

  /* --- 智能选区（传统算法） --- */
  tool: 'brush',        // brush | eraser | lasso | shrink | wand
  edgeCost: null,       // 边缘代价图（懒构建，换图时清掉）
  lassoPts: [],         // 磁性套索已确认的点（图像坐标）
  lassoTip: null,       // 当前吸附到的点
  boxStart: null,       // 粗框起点（图像坐标）
  boxNow: null,         // 粗框当前点
  selSnapRadius: 14,    // 吸附半径（屏幕像素）：越大越爱跳边
  selTolerance: 28,     // 魔棒容差 0~120
};

const MASK_MAX_SIDE = 3072;   // mask 画布长边上限，兼顾精度与内存
const ACCENT = 'rgba(255,176,32,0.62)';

/* ---------------------------------------------------------- 画布 */

const stage = $('stage');
const view = $('view');
const vctx = view.getContext('2d');
let dpr = window.devicePixelRatio || 1;
let cssW = 0, cssH = 0;

function resizeCanvas() {
  const r = stage.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  cssW = r.width; cssH = r.height;
  view.width = Math.round(cssW * dpr);
  view.height = Math.round(cssH * dpr);
  render();
}
window.addEventListener('resize', resizeCanvas);

/* ---------------------------------------------------------- 提示 */

let toastTimer = null;
function toast(msg, kind = '') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'err' ? 7000 : 3200);
}

function loading(on, text = '处理中…') {
  $('loading').hidden = !on;
  $('loadingText').textContent = text;
}

/* ---------------------------------------------------------- API */

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    let j;
    try { j = await r.json(); } catch { j = {}; }
    if (!r.ok || j.ok === false) {
      throw new Error(j.error || j.detail || `请求失败 HTTP ${r.status}`);
    }
    return j;
  }
  if (!r.ok) throw new Error(`请求失败 HTTP ${r.status}`);
  return r;
}

async function loadBitmap(url) {
  const sep = url.includes('?') ? '&' : '?';
  const r = await fetch(`${url}${sep}_t=${Date.now()}`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`加载图片失败 HTTP ${r.status}`);
  return createImageBitmap(await r.blob());
}

/* ---------------------------------------------------------- 渲染 */

function render() {
  vctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  vctx.clearRect(0, 0, cssW, cssH);

  if (!S.img) return;

  const { scale, x, y } = S.view;
  const w = S.img.width * scale;
  const h = S.img.height * scale;

  vctx.imageSmoothingEnabled = true;
  vctx.imageSmoothingQuality = 'high';

  // 阴影让浅色照片也能看清边界
  vctx.save();
  vctx.shadowColor = 'rgba(0,0,0,.55)';
  vctx.shadowBlur = 22;
  vctx.fillStyle = '#000';
  vctx.fillRect(x, y, w, h);
  vctx.restore();

  if (S.compare && (S.prev || S.orig)) {
    const before = S.prev || S.orig;
    const split = Math.round(S.compareX * cssW);
    vctx.save();
    vctx.beginPath(); vctx.rect(x, 0, Math.max(0, split - x), cssH); vctx.clip();
    vctx.drawImage(before, x, y, w, h);
    vctx.restore();
    vctx.save();
    vctx.beginPath(); vctx.rect(Math.max(x, split), 0, cssW, cssH); vctx.clip();
    vctx.drawImage(S.img, x, y, w, h);
    vctx.restore();
    vctx.strokeStyle = 'rgba(255,176,32,.9)';
    vctx.lineWidth = 1;
    vctx.beginPath(); vctx.moveTo(split + .5, 0); vctx.lineTo(split + .5, cssH); vctx.stroke();
  } else {
    vctx.drawImage(S.img, x, y, w, h);
  }

  // mask 叠加（琥珀色半透明）
  if (S.mask && !S.compare) {
    vctx.save();
    vctx.imageSmoothingEnabled = true;
    vctx.drawImage(S.mask, x, y, w, h);
    vctx.restore();
  }

  // ---- 智能选区的实时预览 ----
  if (!S.compare) drawSelectOverlay(x, y, scale);

  // 笔刷光标圆环（只在画笔 / 橡皮下显示 —— 选区工具有自己的提示）
  if (S.cursor.inside && !S.compare && (S.tool === 'brush' || S.tool === 'eraser')) {
    const r = (S.brush.size * scale) / 2;
    vctx.save();
    vctx.beginPath();
    vctx.arc(S.cursor.x, S.cursor.y, Math.max(2, r), 0, Math.PI * 2);
    vctx.strokeStyle = 'rgba(0,0,0,.55)';
    vctx.lineWidth = 3;
    vctx.stroke();
    vctx.strokeStyle = S.brush.mode === 'eraser' ? 'rgba(255,255,255,.9)' : ACCENT;
    vctx.lineWidth = 1.25;
    vctx.stroke();
    vctx.restore();
  }

  if (S.compare && $('compareHandle').hidden === false) {
    // 句柄跟随分割线
    $('compareHandle').style.left = Math.round(S.compareX * cssW) + 'px';
  }
}

/* ---------------------------------------------------------- 视图变换 */

/**
 * 画智能选区的预览：套索的吸附线、粗框的矩形。
 * 这些只画在**视图层**上（vctx），不写进 mask —— 松手才落笔。
 */
function drawSelectOverlay(ox, oy, scale) {
  const toScreen = (p) => ({ x: ox + p.x * scale, y: oy + p.y * scale });

  // 磁性套索：已确认的路径 + 到光标的橡皮筋
  if (S.lassoing && S.lassoPts.length) {
    vctx.save();
    vctx.strokeStyle = 'rgba(255,176,32,.95)';
    vctx.lineWidth = 1.6;
    vctx.setLineDash([]);
    vctx.beginPath();
    const p0 = toScreen(S.lassoPts[0]);
    vctx.moveTo(p0.x, p0.y);
    for (let i = 1; i < S.lassoPts.length; i++) {
      const p = toScreen(S.lassoPts[i]);
      vctx.lineTo(p.x, p.y);
    }
    vctx.stroke();

    // 起点标记（提示「回到这里闭合」）
    vctx.fillStyle = 'rgba(255,176,32,.95)';
    vctx.beginPath();
    vctx.arc(p0.x, p0.y, 4, 0, Math.PI * 2);
    vctx.fill();
    vctx.strokeStyle = 'rgba(0,0,0,.6)';
    vctx.lineWidth = 1.5;
    vctx.stroke();
    vctx.restore();
  }

  // 粗框：显示待收缩的矩形 + 中心提示
  if (S.boxing && S.boxStart && S.boxNow) {
    const a = toScreen(S.boxStart), b = toScreen(S.boxNow);
    const rx = Math.min(a.x, b.x), ry = Math.min(a.y, b.y);
    const rw = Math.abs(b.x - a.x), rh = Math.abs(b.y - a.y);
    vctx.save();
    vctx.strokeStyle = 'rgba(255,176,32,.95)';
    vctx.lineWidth = 1.5;
    vctx.setLineDash([6, 4]);
    vctx.strokeRect(rx, ry, rw, rh);
    vctx.setLineDash([]);
    vctx.fillStyle = 'rgba(255,176,32,.08)';
    vctx.fillRect(rx, ry, rw, rh);
    vctx.restore();
  }
}

function fitToWindow() {
  if (!S.img) return;
  const pad = 56;
  const scale = Math.min((cssW - pad) / S.img.width, (cssH - pad) / S.img.height, 1);
  S.view.scale = scale;
  S.view.x = (cssW - S.img.width * scale) / 2;
  S.view.y = (cssH - S.img.height * scale) / 2;
  updateStatus();
  render();
}

function zoomAt(cx, cy, factor) {
  const s0 = S.view.scale;
  const s1 = Math.min(16, Math.max(0.03, s0 * factor));
  if (s1 === s0) return;
  // 以光标为锚点缩放
  S.view.x = cx - (cx - S.view.x) * (s1 / s0);
  S.view.y = cy - (cy - S.view.y) * (s1 / s0);
  S.view.scale = s1;
  updateStatus();
  render();
}

function screenToImage(cx, cy) {
  return { x: (cx - S.view.x) / S.view.scale, y: (cy - S.view.y) / S.view.scale };
}

/* ---------------------------------------------------------- mask 画布 */

function resetMask() {
  if (!S.img) return;
  const long = Math.max(S.img.width, S.img.height);
  S.maskScale = Math.min(1, MASK_MAX_SIDE / long);
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(S.img.width * S.maskScale));
  c.height = Math.max(1, Math.round(S.img.height * S.maskScale));
  S.mask = c;
  // 换图后原来的边缘代价图就不能用了（尺寸/内容都变了）
  S.edgeCost = null;
  // 新画布是空的 —— 这里必须把 dirty 清掉。
  // 否则上一张图涂完直接开下一张，按 Enter 会拿这张空 mask 去修复，
  // 结果「什么都没涂也能点修复」，服务端还会白跑一次推理。
  S.maskDirty = false;
  resetSelectState();
  render();
}

function maskCtx() { return S.mask.getContext('2d'); }

function hasMaskPaint() {
  return S.maskDirty === true;
}

function clearMask() {
  if (!S.mask) return;
  maskCtx().clearRect(0, 0, S.mask.width, S.mask.height);
  S.maskDirty = false;
  render();
}

/* ---------------------------------------------------------- 事件：绘制 */

function localXY(e) {
  const r = view.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

/**
 * 安全地捕获指针。
 * setPointerCapture 在「指针已经不活跃」时会抛 NotFoundError，
 * 而它挡在落笔之前 —— 一旦抛出去，这一笔就画不上了。
 * 捕获失败最多是鼠标移出窗口时丢事件，不影响作画，所以直接吞掉。
 */
function capturePointer(el, pointerId) {
  try { el.setPointerCapture(pointerId); } catch { /* 忽略 */ }
}

stage.addEventListener('pointerdown', (e) => {
  if (!S.img || S.busy) return;

  const l = localXY(e);

  // 中键 / 空格 / 右键 -> 平移
  if (e.button === 1 || S.spaceDown || e.button === 2) {
    S.panning = true;
    stage.classList.add('panning');
    capturePointer(stage, e.pointerId);
    S.lastPos = l;
    return;
  }
  if (e.button !== 0) return;

  // 拖动对比分割线
  if (S.compare) {
    S.compareX = Math.min(1, Math.max(0, l.x / cssW));
    $('compareHandle').style.left = Math.round(S.compareX * cssW) + 'px';
    render();
    return;
  }

  /* ---- 智能选区工具 ---- */
  if (S.tool === 'lasso') {
    const p = screenToImage(l.x, l.y);
    const cm = edgeMap();
    const c = { x: p.x * cm.scale / S.maskScale, y: p.y * cm.scale / S.maskScale };
    const snapped = SEL.snapPoint(cm, c.x, c.y, S.selSnapRadius);
    const pt = fromCM(snapped.x, snapped.y);
    S.lassoPts = [pt];
    S.lassoTip = pt;
    capturePointer(stage, e.pointerId);
    S.lassoing = true;
    render();
    return;
  }
  if (S.tool === 'shrink') {
    const p = screenToImage(l.x, l.y);
    S.boxStart = p;
    S.boxNow = p;
    capturePointer(stage, e.pointerId);
    S.boxing = true;
    render();
    return;
  }
  if (S.tool === 'wand') {
    // 魔棒：点一下就完事，不需要拖动
    doMagicWand(screenToImage(l.x, l.y));
    return;
  }

  S.painting = true;
  S.lastPos = l;
  capturePointer(stage, e.pointerId);
  paintDot(screenToImage(l.x, l.y));
  render();
});

stage.addEventListener('pointermove', (e) => {
  const l = localXY(e);
  S.cursor = { x: l.x, y: l.y, inside: true };

  if (S.panning && S.lastPos) {
    S.view.x += l.x - S.lastPos.x;
    S.view.y += l.y - S.lastPos.y;
    S.lastPos = l;
    render();
    return;
  }

  if (S.painting) {
    const prev = S.lastPos ? screenToImage(S.lastPos.x, S.lastPos.y) : null;
    const cur = screenToImage(l.x, l.y);
    paintLine(prev, cur);
    S.lastPos = l;
    render();
    return;
  }

  /* ---- 磁性套索：移动时持续吸附并追点 ---- */
  if (S.lassoing && S.lassoPts.length) {
    const p = screenToImage(l.x, l.y);
    const cm = edgeMap();
    const c = { x: p.x * cm.scale / S.maskScale, y: p.y * cm.scale / S.maskScale };
    const snapped = SEL.snapPoint(cm, c.x, c.y, S.selSnapRadius);
    const pt = fromCM(snapped.x, snapped.y);
    const last = S.lassoPts[S.lassoPts.length - 1];
    // 离上一个点够远才记一个，避免点上万个把内存吃满
    if (Math.hypot(pt.x - last.x, pt.y - last.y) > 3 / S.maskScale) {
      S.lassoPts.push(pt);
    }
    S.lassoTip = pt;
    render();
    return;
  }

  /* ---- 粗框：记录当前角点 ---- */
  if (S.boxing && S.boxStart) {
    S.boxNow = screenToImage(l.x, l.y);
    render();
    return;
  }

  if (S.img) render();
});

function endInteract(e) {
  if (S.painting) S.painting = false;
  if (S.panning) {
    S.panning = false;
    stage.classList.remove('panning');
  }

  /* ---- 套索收尾：闭合路径并填充 ---- */
  if (S.lassoing) {
    S.lassoing = false;
    if (S.lassoPts.length >= 3) {
      fillPolygon(S.lassoPts);
    }
    resetSelectState();
    render();
  }

  /* ---- 粗框收尾：收缩到物体 ---- */
  if (S.boxing) {
    S.boxing = false;
    if (S.boxStart && S.boxNow) {
      doShrink(S.boxStart, S.boxNow);
    }
    resetSelectState();
    render();
  }

  S.lastPos = null;
  try { stage.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
}
stage.addEventListener('pointerup', endInteract);
stage.addEventListener('pointercancel', endInteract);
stage.addEventListener('pointerleave', () => { S.cursor.inside = false; render(); });
stage.addEventListener('contextmenu', (e) => e.preventDefault());

/* ---------------------------------------------------------- 选区操作实现 */

/** 粗框收缩：把矩形收缩到里面的物体轮廓 */
function doShrink(a, b) {
  const cm = edgeMap();
  const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
  const y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y);
  if (x1 - x0 < 4 || y1 - y0 < 4) return;

  // 图像坐标 -> 代价图坐标
  const k = cm.scale / S.maskScale;
  const rect = { x0: x0 * k, y0: y0 * k, x1: x1 * k, y1: y1 * k };

  const res = SEL.shrinkToObject(cm, rect);
  if (!res) return;

  if (res.inside) {
    // 收缩成功：填收缩后的区域
    fillInsideMap(res, { x: res.x0, y: res.y0 }, cm.scale / S.maskScale);
  } else {
    // 长不开时不能退回「填满原框」—— 那就等于框选没起作用，用户会觉得工具坏了。
    // 退而求其次：填一个内缩的矩形（砍掉 18%），至少比手画的框更收敛。
    const inset = 0.18;
    const dx = (x1 - x0) * inset, dy = (y1 - y0) * inset;
    const ctx = maskCtx();
    ctx.save();
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = ACCENT;
    ctx.fillRect(
      Math.round((x0 + dx) * S.maskScale), Math.round((y0 + dy) * S.maskScale),
      Math.round((x1 - x0 - dx * 2) * S.maskScale), Math.round((y1 - y0 - dy * 2) * S.maskScale)
    );
    ctx.restore();
    S.maskDirty = true;
  }
}

/** 魔棒：点一下选中颜色相近的连通区域 */
function doMagicWand(p) {
  const cm = edgeMap();
  const k = cm.scale / S.maskScale;
  const x = Math.round(p.x * k), y = Math.round(p.y * k);
  const tol = S.selTolerance;
  const res = SEL.magicWand(cm, x, y, tol);
  if (!res) return;
  fillInsideMap(res, null, cm.scale / S.maskScale);
}

function applyBrush(ctx) {
  const w = Math.max(1, S.brush.size * S.maskScale);
  ctx.lineWidth = w;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = ACCENT;
  ctx.fillStyle = ACCENT;
  ctx.globalCompositeOperation = S.brush.mode === 'eraser' ? 'destination-out' : 'source-over';
  // 用阴影模拟软边
  const soft = (100 - S.brush.hardness) / 100;
  ctx.shadowBlur = soft * w * 0.45;
  ctx.shadowColor = ACCENT;
}

function paintDot(p) {
  if (!S.mask) return;
  const ctx = maskCtx();
  const mx = p.x * S.maskScale, my = p.y * S.maskScale;
  applyBrush(ctx);
  const r = Math.max(0.6, (S.brush.size * S.maskScale) / 2);
  ctx.beginPath();
  ctx.arc(mx, my, r * 0.92, 0, Math.PI * 2);
  ctx.fill();
  S.maskDirty = true;
}

function paintLine(from, to) {
  if (!S.mask) return;
  const ctx = maskCtx();
  applyBrush(ctx);
  const a = from || to;
  ctx.beginPath();
  ctx.moveTo(a.x * S.maskScale, a.y * S.maskScale);
  ctx.lineTo(to.x * S.maskScale, to.y * S.maskScale);
  ctx.stroke();
  S.maskDirty = true;
}

/* ---------------------------------------------------------- 智能选区 */

/**
 * 取边缘代价图（懒构建）。
 * 换图 / 换尺寸都会清掉，所以这里判空即可。
 * 构建要跑 Sobel，大图约 100~300ms，所以放在第一次用到时才做。
 */
function edgeMap() {
  if (!S.edgeCost && S.img) {
    S.edgeCost = SEL.buildCost(S.img, S.maskScale);
  }
  return S.edgeCost;
}

/** 屏幕坐标 -> 边缘代价图坐标 */
function toCM(p) {
  const cm = edgeMap();
  return { x: p.x * cm.scale / S.maskScale, y: p.y * cm.scale / S.maskScale };
}
/** 边缘代价图坐标 -> 图像坐标（mask 坐标系） */
function fromCM(x, y) {
  const cm = edgeMap();
  return { x: x / cm.scale * S.maskScale, y: y / cm.scale * S.maskScale };
}

/** 把一条闭合路径画进 mask（图像坐标数组） */
function fillPolygon(pts) {
  if (!S.mask || pts.length < 3) return;
  const ctx = maskCtx();
  ctx.save();
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = ACCENT;
  ctx.beginPath();
  ctx.moveTo(pts[0].x * S.maskScale, pts[0].y * S.maskScale);
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(pts[i].x * S.maskScale, pts[i].y * S.maskScale);
  }
  ctx.closePath();
  ctx.fill();
  ctx.restore();
  S.maskDirty = true;
}

/** 把一张「二值 inside 图」放大填进 mask（用于粗框收缩 / 魔棒） */
function fillInsideMap(res, origin, cmScale) {
  if (!S.mask || !res) return;
  const ctx = maskCtx();
  ctx.save();
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = ACCENT;

  // 代价图坐标系 -> mask 坐标系
  // res.w/res.h 是整个代价图的尺寸；origin 是子区域的左上角（仅在 shrink 时有意义）
  const ox = origin ? origin.x : 0;
  const oy = origin ? origin.y : 0;
  const gridW = origin ? res.cw : res.w;
  const gridH = origin ? res.ch : res.h;
  const arr = res.inside;

  // 直接按像素往 mask 上画方块太慢（900² = 81 万次），
  // 改成「按行合并连续段」再一次性填充，快几十倍。
  const sx = S.maskScale / cmScale;   // 一个网格单元对应多少 mask 像素
  for (let y = 0; y < gridH; y++) {
    let runStart = -1;
    for (let x = 0; x <= gridW; x++) {
      const on = x < gridW && arr[y * gridW + x];
      if (on && runStart < 0) runStart = x;
      if (!on && runStart >= 0) {
        const mx = (ox + runStart) * sx;
        const my = (oy + y) * sx;
        ctx.fillRect(mx, my, (x - runStart) * sx + 1, sx + 1);
        runStart = -1;
      }
    }
  }
  ctx.restore();
  S.maskDirty = true;
}

/** 清掉当前选区工具的半成品状态 */
function resetSelectState() {
  S.lassoPts = [];
  S.lassoTip = null;
  S.boxStart = null;
  S.boxNow = null;
  S.lassoing = false;
  S.boxing = false;
}

/* 滚轮缩放 */
stage.addEventListener('wheel', (e) => {
  if (!S.img) return;
  e.preventDefault();
  const l = localXY(e);
  zoomAt(l.x, l.y, e.deltaY < 0 ? 1.12 : 1 / 1.12);
}, { passive: false });

/* ---------------------------------------------------------- 选片（列表 + 大图预览） */

const fileModal = $('modalFiles');
const fileListEl = $('fileList');
const modalSplit = $('modalSplit');
let curDir = '';
let files = [];            // 当前目录的图片
let lastListing = null;    // 最近一次目录列表
let rowEls = [];           // 当前所有格子（按 DOM 顺序），键盘导航用
let selRow = -1;           // 选中格子在 rowEls 里的下标
let previewToken = 0;      // 防止快速划过时旧请求把新图盖掉
let hoverTimer = null;
let thumbObserver = null;

async function openFileModal() {
  fileModal.hidden = false;
  if (!$('roots').children.length) {
    try {
      const j = await api('/api/fs/roots');
      $('roots').innerHTML = '';
      j.items.forEach((it) => {
        const b = document.createElement('button');
        b.className = 'root-chip';
        b.textContent = it.name;
        b.title = it.path;
        b.onclick = () => listDir(it.path);
        $('roots').appendChild(b);
      });
    } catch (e) { toast(String(e.message), 'err'); }
  }
  await listDir(curDir || null);
  fileListEl.focus();
}

function resetPreview() {
  previewToken++;
  $('pvImg').hidden = true;
  $('pvImg').removeAttribute('src');
  $('pvMeta').hidden = true;
  const hint = $('pvHint');
  hint.hidden = false;
  hint.innerHTML = '<div class="pv-hint-big">把鼠标移到文件名上</div>'
                 + '<div>右边这里就会显示大图<br>方便挑出想修的那张</div>';
  document.querySelector('.modal-preview').classList.remove('busy');
}

async function listDir(path) {
  fileListEl.innerHTML = '<div class="empty-note">读取中…</div>';
  files = [];
  lastListing = null;
  selRow = -1;
  resetPreview();
  try {
    let target = path;
    if (!target) {
      const roots = await api('/api/fs/roots');
      const pic = roots.items.find((i) => i.name === '图片') || roots.items[0];
      target = pic.path;
    }
    const j = await api(`/api/fs/list?path=${encodeURIComponent(target)}`);
    curDir = j.path;
    $('pathInput').value = j.path;
    lastListing = j;
    renderRows();
    if (files.length) selectRow(1, false);   // 自动预览第一张（0 通常是「上级目录」）
  } catch (e) {
    fileListEl.className = 'modal-list';
    fileListEl.innerHTML = `<div class="empty-note">读取失败：${e.message}</div>`;
  }
}

/** 重建整个缩略图网格 */
function renderRows() {
  const L = lastListing;
  if (!L) return;
  const body = fileListEl;
  body.className = 'modal-list';
  body.innerHTML = '';
  body.scrollTop = 0;
  rowEls = [];

  if (L.parent) body.appendChild(makeRow({ kind: 'up', name: '上级目录', path: L.parent }));
  L.dirs.forEach((d) => body.appendChild(makeRow({ kind: 'dir', name: d.name, path: d.path })));

  files = L.images;
  files.forEach((f, i) => body.appendChild(makeRow({ kind: 'file', idx: i, ...f })));

  if (!L.dirs.length && !files.length) {
    body.innerHTML = '<div class="empty-note">这个目录里没有子目录或图片</div>';
  }
  watchThumbs();

  // 重建后把选中状态贴回去
  if (selRow >= 0 && selRow < rowEls.length) {
    rowEls[selRow].classList.add('sel');
  } else {
    selRow = -1;
  }
}

function togglePreview() {
  const hidden = modalSplit.classList.toggle('no-preview');
  const btn = $('btnTogglePreview');
  btn.textContent = hidden ? '显示大图' : '隐藏大图';
  btn.classList.toggle('on', !hidden);
}

function fmtSize(n) {
  if (n == null) return '';
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

function makeRow(info) {
  const el = document.createElement('div');
  el.className = 'row-item' + (info.kind === 'dir' ? ' dir' : '');
  if (info.kind === 'file') el.classList.add('file');
  el.dataset.path = info.path;
  if (info.kind === 'file') el.dataset.idx = String(info.idx);
  el._fileIdx = info.kind === 'file' ? info.idx : -1;

  let lead;
  if (info.kind === 'file') {
    lead = document.createElement('img');
    lead.className = 'row-thumb ph';     // ph = 占位，加载完去掉
    lead.alt = '';
    lead.decoding = 'async';
    lead.dataset.thumb = info.path;
  } else {
    lead = document.createElement('div');
    lead.className = 'row-ico';
    lead.textContent = info.kind === 'up' ? '↑' : '📁';
  }

  const bodyEl = document.createElement('div');
  bodyEl.className = 'row-body';
  const nameEl = document.createElement('div');
  nameEl.className = 'rn';
  nameEl.textContent = info.name;
  const subEl = document.createElement('div');
  subEl.className = 'rs';
  subEl.textContent = info.kind === 'file'
    ? `${info.width || '?'}×${info.height || '?'} · ${fmtSize(info.size)}`
    : (info.kind === 'dir' ? '文件夹' : '');
  bodyEl.append(nameEl, subEl);
  el.append(lead, bodyEl);

  rowEls.push(el);

  if (info.kind === 'file') {
    el.addEventListener('mouseenter', () => schedulePreview(el));
    el.addEventListener('click', () => selectRow(rowEls.indexOf(el), false));
    el.addEventListener('dblclick', () => openPath(info.path));
  } else {
    el.addEventListener('click', () => listDir(info.path));
  }
  return el;
}

/** 鼠标划过时不立刻请求，停 110ms 再发 —— 快速扫过一列不会把请求打爆 */
function schedulePreview(el) {
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => selectRow(rowEls.indexOf(el), false), 110);
}

/** 选中第 pos 行（行含目录，只有图片行会触发大图预览） */
function selectRow(pos, scroll = true) {
  if (pos < 0 || pos >= rowEls.length) return;
  selRow = pos;
  const el = rowEls[pos];
  rowEls.forEach((r) => r.classList.remove('sel'));
  el.classList.add('sel');
  if (scroll) el.scrollIntoView({ block: 'nearest' });

  // 落到目录行上就预览它旁边的第一张图，不然右边的预览会停在上一张
  let target = el;
  if (el._fileIdx < 0) {
    const next = rowEls.slice(pos + 1).find((r) => r._fileIdx >= 0);
    if (!next) return;
    target = next;
  }
  showPreview(files[target._fileIdx]);
}

function showPreview(f) {
  const token = ++previewToken;
  const pane = document.querySelector('.modal-preview');
  const img = $('pvImg');

  $('pvMeta').hidden = false;
  $('pvName').textContent = f.name;
  $('pvDim').textContent = `${f.width || '?'} × ${f.height || '?'} px · ${fmtSize(f.size)}`;
  $('pvHint').hidden = true;
  pane.classList.add('busy');

  // 先离屏加载，加载完再换上去 —— 否则切图时会先白一下再出图
  const tmp = new Image();
  tmp.onload = () => {
    if (token !== previewToken) return;
    img.src = tmp.src;
    img.hidden = false;
    pane.classList.remove('busy');
  };
  tmp.onerror = () => {
    if (token !== previewToken) return;
    pane.classList.remove('busy');
    img.hidden = true;
    const hint = $('pvHint');
    hint.hidden = false;
    hint.innerHTML = '<div class="pv-hint-big">这张预览不出来</div>'
                   + '<div>格式不支持或文件损坏，不过多半还是能修的</div>';
  };
  tmp.src = `/api/fs/preview?path=${encodeURIComponent(f.path)}&max_side=1800`;
}

/** 列表缩略图懒加载：只请求滚动到可见范围的那些 */
function watchThumbs() {
  const imgs = fileListEl.querySelectorAll('img.row-thumb[data-thumb]');
  const load = (img) => {
    img.onload = () => img.classList.remove('ph');
    img.onerror = () => img.classList.remove('ph');
    img.src = `/api/fs/thumb?path=${encodeURIComponent(img.dataset.thumb)}&size=240`;
  };
  if (!('IntersectionObserver' in window)) {
    imgs.forEach(load);
    return;
  }
  if (thumbObserver) thumbObserver.disconnect();
  thumbObserver = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      thumbObserver.unobserve(en.target);
      load(en.target);
    });
  }, { root: fileListEl, rootMargin: '400px 0px' });
  imgs.forEach((i) => thumbObserver.observe(i));
}

/** 网格每行有几个格子，用来算上下移动的步长 */
function gridColumns() {
  if (!rowEls.length) return 1;
  const top0 = rowEls[0].offsetTop;
  let n = 0;
  for (const r of rowEls) {
    if (r.offsetTop === top0) n++;
    else break;
  }
  return Math.max(1, n);
}

/** 弹窗打开时接管方向键和回车 */
function handleModalKey(e) {
  if (e.target === $('pathInput') && e.key === 'Enter') return false;  // 让输入框自己处理
  if (!rowEls.length) return false;

  const cols = gridColumns();
  const key = e.key;
  if (['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(key)) {
    e.preventDefault();
    const step = key === 'ArrowDown' ? cols
               : key === 'ArrowUp' ? -cols
               : key === 'ArrowRight' ? 1 : -1;
    const start = selRow < 0 ? (step > 0 ? -1 : rowEls.length) : selRow;
    selectRow(Math.max(0, Math.min(rowEls.length - 1, start + step)));
    return true;
  }

  if (key === 'Enter' && selRow >= 0) {
    const el = rowEls[selRow];
    if (el._fileIdx >= 0) {
      e.preventDefault();
      openPath(files[el._fileIdx].path);
    } else if (el.dataset.path) {
      e.preventDefault();
      listDir(el.dataset.path);
    }
    return true;
  }
  return false;
}

async function openPath(path) {
  fileModal.hidden = true;
  loading(true, '打开中…');
  try {
    const j = await api('/api/session/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    await adoptSession(j.session);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    loading(false);
  }
}

async function uploadFile(file) {
  loading(true, '读取中…');
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    const j = await api('/api/session/upload', { method: 'POST', body: fd });
    await adoptSession(j.session);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    loading(false);
  }
}

async function adoptSession(s) {
  S.session = s;
  if (S.orig) S.orig.close();
  if (S.prev && S.prev !== S.orig) S.prev.close();
  S.img = null; S.prev = null;

  S.orig = await loadBitmap(`/api/session/${s.id}/original`);
  S.img = await loadBitmap(`/api/session/${s.id}/image`);
  resetMask();
  // 笔刷按图片尺寸自适应：大图缩着看的时候，60px 的笔刷在屏幕上只有几个像素，
  // 根本没法涂。取长边约 2.5%，夹在 24~600 之间。
  const autoSize = Math.min(600, Math.max(24, Math.round(Math.max(s.width, s.height) * 0.025)));
  setBrushSize(autoSize);

  $('empty').hidden = true;
  $('actionbar').hidden = false;
  $('fname').textContent = s.name;
  $('fsep').hidden = false;
  $('fdim').textContent = `${s.width} × ${s.height} px`;
  $('stTime').textContent = '—';
  $('radAlongside').disabled = !s.source_path;
  fitToWindow();
  syncButtons();
  $('stage').classList.add('ready');
}

/* ---------------------------------------------------------- 修复 */

async function runInpaint() {
  if (!S.session || S.busy) return;
  if (!hasMaskPaint()) {
    toast('先用画笔把要去掉的东西涂满', 'err');
    return;
  }
  S.busy = true;
  loading(true, S.useCv2 ? '正在修复（OpenCV）…' : '正在修复（LaMa）…');
  try {
    const blob = await new Promise((res) => S.mask.toBlob(res, 'image/png'));
    const fd = new FormData();
    fd.append('mask', blob, 'mask.png');
    fd.append('expand', String(S.expand));
    fd.append('max_side', String(S.maxSide));
    fd.append('feather', '0.8');
    fd.append('method', S.useCv2 ? 'cv2' : 'lama');
    fd.append('tile', S.tile ? 'true' : 'false');
    fd.append('grain', String(S.grain));

    const j = await api(`/api/session/${S.session.id}/inpaint`, { method: 'POST', body: fd });

    // 旧图留作对比
    if (S.prev && S.prev !== S.orig) S.prev.close();
    S.prev = S.img;
    S.img = await loadBitmap(`/api/session/${S.session.id}/image`);

    if (S.autoClear) clearMask(); else render();
    S.session = j.session;
    $('stTime').textContent = `${j.elapsed} s`;
    syncButtons();
    render();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    S.busy = false;
    loading(false);
  }
}

async function doAction(action) {
  if (!S.session || S.busy) return;
  S.busy = true;
  try {
    const j = await api(`/api/session/${S.session.id}/${action}`, { method: 'POST' });
    if (action === 'undo' || action === 'redo') {
      if (S.prev && S.prev !== S.orig) S.prev.close();
      S.prev = S.img;
      S.img = await loadBitmap(`/api/session/${S.session.id}/image`);
      clearMask();
    } else if (action === 'reset') {
      if (S.prev && S.prev !== S.orig) S.prev.close();
      S.img = await loadBitmap(`/api/session/${S.session.id}/image`);
      S.prev = null;
      clearMask();
      $('stTime').textContent = '—';
    }
    S.session = j.session;
    syncButtons();
    render();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    S.busy = false;
  }
}

function syncButtons() {
  const has = !!S.session;
  $('btnSave').disabled = !has;
  const st = S.session;
  $('btnUndo').disabled = !has || !st.can_undo;
  $('btnRedo').disabled = !has || !st.can_redo;
  $('btnReset').disabled = !has || st.cursor === 0;
  $('btnRun').disabled = !has;
}

/* ---------------------------------------------------------- 保存 */

async function doSave() {
  if (!S.session) return;
  const target = document.querySelector('input[name=saveTarget]:checked').value;
  const format = $('selFormat').value;
  $('popSave').hidden = true;
  loading(true, '保存中…');
  try {
    const j = await api(`/api/session/${S.session.id}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target, format }),
    });
    toast(`已保存：${j.path}`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    loading(false);
  }
}

/* ---------------------------------------------------------- 状态栏 / 状态轮询 */

function updateStatus() {
  $('stZoom').textContent = S.img ? Math.round(S.view.scale * 100) + '%' : '—';
}

let statusTimer = null;
async function pollStatus() {
  try {
    const j = await api('/api/status');
    const chip = $('chipDevice');
    if (j.device === 'cuda' && j.gpu.available) {
      chip.textContent = `GPU · ${j.gpu.name.replace('NVIDIA ', '')}`;
      chip.className = 'chip ok';
    } else {
      chip.textContent = 'CPU';
      chip.className = 'chip';
    }

    const warn = $('chipModel');
    if (j.model_ready) {
      warn.hidden = true;
      if (j.download.status === 'done' && !S._readyNotified) {
        S._readyNotified = true;
      }
    } else {
      warn.hidden = false;
      const d = j.download;
      if (d.status === 'downloading') {
        warn.className = 'chip chip-warn';
        warn.textContent = `模型下载中 ${d.percent.toFixed(0)}%`;
      } else if (d.status === 'error') {
        warn.className = 'chip chip-err';
        warn.textContent = '模型下载失败';
      } else {
        warn.className = 'chip chip-warn';
        warn.textContent = '模型准备中';
      }
    }
    if (j.error) $('stLeft').textContent = '模型加载失败：' + j.error;
    if (S.session) $('stLeft').textContent =
      `第 ${S.session.cursor} 步 / 共 ${S.session.steps.length - 1} 次修复`;
  } catch { /* 服务还没起来，忽略 */ }
}

/* ---------------------------------------------------------- 快捷键 */

document.addEventListener('keydown', (e) => {
  // 选片弹窗开着的时候，方向键 / 回车归它用
  if (!fileModal.hidden && handleModalKey(e)) return;
  if (e.target.matches('input,select,textarea')) return;
  const k = e.key.toLowerCase();

  if (e.code === 'Space' && !S.spaceDown) {
    S.spaceDown = true; stage.classList.add('pan'); e.preventDefault(); return;
  }
  if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); doAction(e.shiftKey ? 'redo' : 'undo'); return; }
  if ((e.ctrlKey || e.metaKey) && k === 'y') { e.preventDefault(); doAction('redo'); return; }
  if ((e.ctrlKey || e.metaKey) && k === 's') { e.preventDefault(); if (S.session) $('popSave').hidden = false; return; }
  if ((e.ctrlKey || e.metaKey) && k === 'o') { e.preventDefault(); openFileModal(); return; }

  switch (k) {
    case 'b': setMode('brush'); break;
    case 'e': setMode('eraser'); break;
    case 'l': setMode('lasso'); break;
    case 'k': setMode('shrink'); break;
    case 'w': setMode('wand'); break;
    case '[': setBrushSize(S.brush.size * 0.82); break;
    case ']': setBrushSize(S.brush.size * 1.22); break;
    case '0': fitToWindow(); break;
    case 'enter': e.preventDefault(); runInpaint(); break;
    case 'escape':
      // 选区工具进行中 -> 先取消当前这一笔，不清面板
      if (S.lassoing || S.boxing) {
        resetSelectState();
        render();
        break;
      }
      $('popBrush').hidden = true;
      $('popSave').hidden = true;
      fileModal.hidden = true;
      break;
    default: break;
  }
});

document.addEventListener('keyup', (e) => {
  if (e.code === 'Space') { S.spaceDown = false; stage.classList.remove('pan'); }
});

/* ---------------------------------------------------------- 拖拽 / 粘贴 */

stage.addEventListener('dragover', (e) => { e.preventDefault(); });
stage.addEventListener('drop', (e) => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  if (f && /image\//.test(f.type)) uploadFile(f);
  else toast('请拖入图片文件', 'err');
});

document.addEventListener('paste', (e) => {
  const items = e.clipboardData?.items || [];
  for (const it of items) {
    if (it.type.startsWith('image/')) {
      const f = it.getAsFile();
      if (f) { uploadFile(new File([f], 'clipboard.png', { type: f.type })); return; }
    }
  }
});

/* ---------------------------------------------------------- 控件绑定 */

function setMode(mode) {
  S.brush.mode = (mode === 'eraser') ? 'eraser' : 'brush';
  S.tool = mode;
  // 切工具时把半成品丢掉，避免套索的点残留到下次
  resetSelectState();

  const map = {
    brush: 'btnBrush', eraser: 'btnEraser',
    lasso: 'btnLasso', shrink: 'btnShrink', wand: 'btnWand',
  };
  for (const [k, id] of Object.entries(map)) {
    const el = $(id);
    if (el) el.classList.toggle('active', k === mode);
  }
  // 参数面板：画笔类显示笔刷参数，选区类显示选区参数
  const isBrush = (mode === 'brush' || mode === 'eraser');
  $('brushFields').hidden = !isBrush;
  $('selFields').hidden = isBrush;

  // 光标形状（选区工具用十字/方格，跟画笔区分开）
  stage.classList.toggle('tool-lasso', mode === 'lasso');
  stage.classList.toggle('tool-shrink', mode === 'shrink');
  stage.classList.toggle('tool-wand', mode === 'wand');

  updateStatusHint();
  render();
}

/** 状态栏左侧的提示语，跟着工具变 */
function updateStatusHint() {
  // 底部操作条那句提示也要跟着工具变 —— 切到魔棒了还说「涂满要去掉的东西」
  // 会让人以为必须涂抹（这句原来是写死在 HTML 里的静态文字）。
  const HINTS = {
    brush: '涂满要去掉的东西，然后',
    eraser: '擦掉多余的涂抹，然后',
    lasso: '沿边缘圈出要去掉的东西，然后',
    shrink: '框住要去掉的东西，然后',
    wand: '点一下要去掉的东西，然后',
  };
  const actionHint = $('actionHint');
  if (actionHint) actionHint.textContent = HINTS[S.tool] || HINTS.brush;

  const el = $('stLeft');
  if (!el) return;
  if (!S.img) { el.textContent = '就绪'; return; }
  const tip = {
    brush: '涂抹要去掉的东西',
    eraser: '擦掉多余的涂抹',
    lasso: '沿着物体边缘拖动，回到起点闭合',
    shrink: '随手框一块，松手自动收缩到物体',
    wand: '点一下背景（或物体），自动选中一片',
  }[S.tool] || '涂抹要去掉的东西';
  el.textContent = tip;
}

function setBrushSize(v) {
  S.brush.size = Math.min(600, Math.max(4, Math.round(v)));
  $('rngSize').value = S.brush.size;
  $('lblSize').textContent = S.brush.size;
  render();
}

$('btnOpen').onclick = openFileModal;
$('btnOpenBig').onclick = openFileModal;
$('btnBrush').onclick = () => setMode('brush');
$('btnEraser').onclick = () => setMode('eraser');
$('btnLasso').onclick = () => setMode('lasso');
$('btnShrink').onclick = () => setMode('shrink');
$('btnWand').onclick = () => setMode('wand');
$('btnClear').onclick = () => { clearMask(); toast('已清空涂抹'); };
$('btnUndo').onclick = () => doAction('undo');
$('btnRedo').onclick = () => doAction('redo');
$('btnReset').onclick = () => doAction('reset');
$('btnRun').onclick = runInpaint;
$('btnSave').onclick = () => { $('popSave').hidden = !$('popSave').hidden; };
$('btnSaveConfirm').onclick = doSave;

$('btnBrushPanel').onclick = () => { $('popBrush').hidden = !$('popBrush').hidden; };

document.addEventListener('click', (e) => {
  if (!e.target.closest('#popBrush') && !e.target.closest('#btnBrushPanel')) {
    $('popBrush').hidden = true;
  }
  if (!e.target.closest('#popSave') && !e.target.closest('#btnSave')) {
    $('popSave').hidden = true;
  }
});

$('rngSize').oninput = (e) => setBrushSize(+e.target.value);
$('rngHard').oninput = (e) => {
  S.brush.hardness = +e.target.value;
  $('lblHard').textContent = S.brush.hardness >= 80 ? '硬边'
    : S.brush.hardness >= 40 ? '中等' : '软边';
};
$('rngExpand').oninput = (e) => { S.expand = +e.target.value; $('lblExpand').textContent = S.expand; };
$('rngMaxSide').oninput = (e) => { S.maxSide = +e.target.value; $('lblMaxSide').textContent = S.maxSide; };
$('rngGrain').oninput = (e) => {
  S.grain = +e.target.value;
  // 显示成增益百分比：强度 1.0 的含义是「补全区质感翻倍」，比裸数字直观
  $('lblGrain').textContent = S.grain === 0 ? '关' : '+' + Math.round(S.grain * 100) + '%';
};
$('rngSnap').oninput = (e) => {
  S.selSnapRadius = +e.target.value;
  $('lblSnap').textContent = S.selSnapRadius;
};
$('rngTol').oninput = (e) => {
  S.selTolerance = +e.target.value;
  $('lblTol').textContent = S.selTolerance;
};
$('chkCv2').onchange = (e) => { S.useCv2 = e.target.checked; };
$('chkTile').onchange = (e) => { S.tile = e.target.checked; };
$('chkAutoClear').onchange = (e) => { S.autoClear = e.target.checked; };

/* 对比 */
const compareHandle = $('compareHandle');
$('btnCompare').onclick = () => {
  if (!S.img) return;
  S.compare = !S.compare;
  $('btnCompare').classList.toggle('on', S.compare);
  compareHandle.hidden = !S.compare;
  render();
};

/* 选片弹窗 */
$('btnCloseFiles').onclick = () => { fileModal.hidden = true; };
$('btnUp').onclick = async () => {
  try {
    const j = await api(`/api/fs/list?path=${encodeURIComponent(curDir)}`);
    if (j.parent) listDir(j.parent);
  } catch { /* ignore */ }
};
$('btnGo').onclick = () => listDir($('pathInput').value.trim());
$('pathInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') listDir($('pathInput').value.trim());
});
$('btnTogglePreview').onclick = togglePreview;
fileModal.addEventListener('click', (e) => { if (e.target === fileModal) fileModal.hidden = true; });

/* ---------------------------------------------------------- 启动 */

(async function boot() {
  resizeCanvas();
  await pollStatus();
  statusTimer = setInterval(pollStatus, 2000);
  $('stLeft').textContent = '把照片拖进窗口，或点左侧「打开」';
  render();
})();
