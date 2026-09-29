/* ============================================================
   CleanPlate —— 智能选区（传统算法）
   ============================================================

   三种交互，共用一个内核：
     · 磁性套索  —— 沿边缘移动，自动吸附到最近的强边缘
     · 粗框收缩  —— 随手框一块，自动收缩到框内的物体轮廓
     · 点击选中  —— 点一下，自动扩展成连通区域（魔棒）

   内核 = 一张「边缘代价图」+ 一个「路径搜索」：
     1. 用 Sobel 算梯度，得到每个像素「有多像边缘」
     2. 代价 = 反过来的梯度（边缘处代价低）
     3. 找路径时走代价最低的路线 —— 自然就贴着边缘跑了

   为什么不用现成库：
     本项目坚持零前端依赖。边缘检测和路径搜索都在这里自己实现，
     总共几百行，可控、可调、不引 CDN。

   设计约束（沿用项目约定）：
     · 不改 app.js 的 mask 绘制逻辑，只往 S.mask 上「填」结果
     · 选区外像素不动（选区内部我们也只是往 mask 上画，原始照片不碰）
   ============================================================ */
'use strict';

// 长边上限：太大卡、太小不准。900 是实测手感与速度的平衡点。
// 必须定义在 IIFE 之前 —— 闭包内部 buildCost 会引用它。
const SEL_MAX_SIDE = 900;

const SEL = (() => {
  /* ------------------------------------------------ 边缘代价图 */

  /**
   * 从 ImageBitmap / canvas 生成「边缘代价图」。
   *
   * 返回 { cost: Float32Array, w, h, scale }
   *   cost  —— 每个像素走这里的代价。边缘处低（0~0.15），平坦处高（接近 1）
   *   scale —— 代价图相对原图 mask 坐标的缩放比（为了速度会把大图缩小）
   *
   * 为什么要缩小：2000 万像素上算 Sobel + 寻路，浏览器会卡死。
   * 缩到长边 900 左右，肉眼效果几乎无差别，速度快几十倍。
   */
  function buildCost(source, maskScale) {
    const sw = source.width, sh = source.height;
    const long = Math.max(sw, sh);
    const scale = Math.min(1, SEL_MAX_SIDE / long);
    const w = Math.max(8, Math.round(sw * scale));
    const h = Math.max(8, Math.round(sh * scale));

    // 画到小 canvas 上取像素
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(source, 0, 0, w, h);
    const px = ctx.getImageData(0, 0, w, h).data;

    // 转灰度
    const gray = new Float32Array(w * h);
    for (let i = 0, p = 0; i < gray.length; i++, p += 4) {
      gray[i] = 0.299 * px[p] + 0.587 * px[p + 1] + 0.114 * px[p + 2];
    }

    // Sobel 梯度（含轻微高斯预模糊，压掉噪点造成的假边缘）
    const blur = boxBlur(gray, w, h, 1);
    const mag = sobelMag(blur, w, h);

    // 归一化到 0~1
    let max = 0;
    for (let i = 0; i < mag.length; i++) if (mag[i] > max) max = mag[i];
    const inv = max > 1e-6 ? 1 / max : 0;

    // 代价 = 1 - 梯度（边缘处代价低）。
    // 加一个下限 0.02，避免「零代价」导致寻路时在噪声上乱窜。
    const cost = new Float32Array(w * h);
    for (let i = 0; i < cost.length; i++) {
      const g = mag[i] * inv;
      // 用开方压一下曲线：让中等强度的边缘也能被吸住，不只是最强的那几条
      cost[i] = Math.max(0.02, 1 - Math.sqrt(g));
    }

    return { cost, w, h, scale: scale * (maskScale || 1), gray: blur };
  }

  /** 3x3 箱式模糊（比高斯快，够用）。 */
  function boxBlur(src, w, h, r) {
    const out = new Float32Array(src.length);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        for (let dy = -r; dy <= r; dy++) {
          const yy = y + dy;
          if (yy < 0 || yy >= h) continue;
          for (let dx = -r; dx <= r; dx++) {
            const xx = x + dx;
            if (xx < 0 || xx >= w) continue;
            s += src[yy * w + xx]; n++;
          }
        }
        out[y * w + x] = s / n;
      }
    }
    return out;
  }

  /** Sobel 梯度幅值。 */
  function sobelMag(g, w, h) {
    const out = new Float32Array(g.length);
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const i = y * w + x;
        const a = g[i - w - 1], b = g[i - w], c = g[i - w + 1];
        const d = g[i - 1], f = g[i + 1];
        const p = g[i + w - 1], q = g[i + w], r2 = g[i + w + 1];
        const gx = (c + 2 * f + r2) - (a + 2 * d + p);
        const gy = (p + 2 * q + r2) - (a + 2 * b + c);
        out[i] = Math.sqrt(gx * gx + gy * gy);
      }
    }
    return out;
  }

  /* ------------------------------------------------ 磁性套索 */

  /**
   * 给定当前点，在附近找梯度最强的位置，把点吸过去。
   *
   * 做法：以 p 为中心搜一个窗口，窗口内取「梯度 × 距离衰减」最大的像素。
   * 距离衰减保证：同样强的边缘，近的优先（不然会跳到远处的无关边缘）。
   */
  function snapPoint(cm, x, y, radius) {
    const { cost, w, h } = cm;
    const cx = Math.round(x), cy = Math.round(y);
    if (cx < 0 || cy < 0 || cx >= w || cy >= h) return { x, y };

    let best = { x: cx, y: cy, score: -1e9 };
    const R = Math.max(2, radius | 0);
    for (let dy = -R; dy <= R; dy++) {
      const yy = cy + dy;
      if (yy < 0 || yy >= h) continue;
      for (let dx = -R; dx <= R; dx++) {
        const xx = cx + dx;
        if (xx < 0 || xx >= w) continue;
        const i = yy * w + xx;
        // 边缘强度 = 1 - 代价
        const edge = 1 - cost[i];
        const dist = Math.sqrt(dx * dx + dy * dy);
        // 距离衰减：越远扣得越多
        const score = edge - dist / (R * 2.2);
        if (score > best.score) best = { x: xx, y: yy, score };
      }
    }
    return best;
  }

  /* ------------------------------------------------ 粗框收缩 */

  /**
   * 给一个粗糙矩形，收缩到里面物体的轮廓。
   *
   * 做法：在矩形内找最强边缘，再做「区域生长 + 边缘阻挡」：
   *   从矩形中心出发往四周长，遇到强边缘就停 —— 长出来的就是物体内部。
   *   比直接找轮廓线更稳（轮廓线常常是断的，区域生长不会断）。
   */
  function shrinkToObject(cm, rect) {
    const { cost, w, h } = cm;
    const x0 = Math.max(0, Math.floor(rect.x0));
    const y0 = Math.max(0, Math.floor(rect.y0));
    const x1 = Math.min(w - 1, Math.ceil(rect.x1));
    const y1 = Math.min(h - 1, Math.ceil(rect.y1));
    if (x1 <= x0 || y1 <= y0) return null;

    const cw = x1 - x0 + 1, ch = y1 - y0 + 1;
    const seedX = Math.round((x0 + x1) / 2) - x0;
    const seedY = Math.round((y0 + y1) / 2) - y0;
    const seed = seedY * cw + seedX;

    // 阻挡阈值：代价超过这个值 = 边缘，不许越过。
    //
    // 这里踩过一个坑：最初取「矩形内代价的 35 分位」，结果几乎长不开。
    // 原因是代价图里边缘处的代价被压到了 0.02 下限（见 buildCost），
    // 于是分位数偏低、阈值过严，种子点四邻域立刻全被判成边缘，一步都走不出去。
    //
    // 改成「中位数 + 抬高」：中位数代表这块区域的平均平坦程度，
    // 加上一个抬升量留出宽容度，只有在明显比周围「更边缘」的地方才停。
    const vals = [];
    for (let y = y0; y <= y1; y++)
      for (let x = x0; x <= x1; x++) vals.push(cost[y * w + x]);
    vals.sort((a, b) => a - b);
    const med = vals[Math.floor(vals.length * 0.5)];
    // 抬升量：至少 0.08，且不超过 0.45（防止阈值过高把整个画面吞掉）
    const thr = Math.min(0.45, med + Math.max(0.08, med * 0.6));

    const inside = new Uint8Array(cw * ch);
    const stack = [seed];
    inside[seed] = 1;
    let count = 0;

    while (stack.length) {
      const i = stack.pop();
      count++;
      const x = i % cw, y = (i / cw) | 0;
      // 四邻域
      const nb = [
        [x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1],
      ];
      for (const [nx, ny] of nb) {
        if (nx < 0 || ny < 0 || nx >= cw || ny >= ch) continue;
        const ni = ny * cw + nx;
        if (inside[ni]) continue;
        if (cost[(y0 + ny) * w + (x0 + nx)] > thr) continue;  // 撞到边缘，停
        inside[ni] = 1;
        stack.push(ni);
      }
    }

    // 长得太小说明种子点在边缘上或阈值太严，退化成原矩形
    if (count < 16) return rect;

    return { x0, y0, x1, y1, inside, cw, ch };
  }

  /* ------------------------------------------------ 点击选中（魔棒） */

  /**
   * 从一点出发，扩展成颜色相近的连通区域。
   *
   * 和「粗框收缩」的区别：这里比的是**颜色相似度**（不是边缘），
   * 就是 PS 魔棒的做法。适合「背景是纯色，点一下就能选中它」。
   */
  function magicWand(cm, x, y, tol) {
    const { gray, w, h } = cm;
    const cx = Math.round(x), cy = Math.round(y);
    if (cx < 0 || cy < 0 || cx >= w || cy >= h) return null;

    const seedVal = gray[cy * w + cx];
    const inside = new Uint8Array(w * h);
    const stack = [cy * w + cx];
    inside[cy * w + cx] = 1;

    while (stack.length) {
      const i = stack.pop();
      const px = i % w, py = (i / w) | 0;
      const nb = [[px - 1, py], [px + 1, py], [px, py - 1], [px, py + 1]];
      for (const [nx, ny] of nb) {
        if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
        const ni = ny * w + nx;
        if (inside[ni]) continue;
        if (Math.abs(gray[ni] - seedVal) > tol) continue;
        inside[ni] = 1;
        stack.push(ni);
      }
    }
    return { inside, w, h };
  }

  /* ------------------------------------------------ 内部小工具 */

  return {
    buildCost, snapPoint, shrinkToObject, magicWand,
  };
})();
