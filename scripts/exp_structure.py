# -*- coding: utf-8 -*-
"""B 方案实验：结构层补偿。

问题（2026-09-25 实测）：LaMa 补全区的**低频结构保留率只有 54%**，
大片区域被补成近纯色/线性渐变。人眼读作「糊」。

思路：LaMa 的补全在像素层（高频/中频）是够的（85%~104%），
丢的是**低频的明暗层次**。而低频恰恰是**可以从周围环境外推**的 ——
墙面的明暗渐变、地面的光影走势都有很强的空间延续性。

算法：
  1. low_P    = 大半径模糊(补全结果)        —— 补全区自己的低频（偏平）
  2. low_ref  = 从选区外拟合/外推的低频场   —— 周围环境该有的低频
  3. 补偿     = low_ref - low_P            —— 缺的那部分结构
  4. P'       = P + gain * 补偿
  5. 只在 mask 内混合

关键问题：low_ref 怎么算？候选三种，本脚本逐一对比：
  (a) 环带引导填充 —— 把 mask 外的低频用 inpaint 算法补进来（快，可控）
  (b) 径向基外推   —— 用边界低频做多项式/RBF 拟合
  (c) 多尺度引导   —— 逐级降采样，从粗到细填低频
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def lowpass(img: np.ndarray, radius: int) -> np.ndarray:
    """大半径低通。radius 是「结构尺度」——想补什么样的起伏就设多大。"""
    k = radius * 2 + 1
    return cv2.GaussianBlur(img.astype(np.float32), (k, k), 0)


def low_ref_guided(low: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """(a) 用 cv2.inpaint 把 mask 外已有的低频「填」进 mask 内。

    优点：尊重周围实际的明暗分布，不是凭空拟合。
    缺点：inpaint 本身也是插值，大空洞同样会偏平 —— 但比直接看 LaMa 输出好，
          因为这里的输入是**低频场**，没有高频干扰，插值出来的层次更干净。
    """
    m8 = (mask > 0).astype(np.uint8) * 255
    # 半径给大：低频场很平滑，用大半径让远处的信息能传进来
    ref = cv2.inpaint(low.astype(np.uint8), m8, 64, cv2.INPAINT_TELEA)
    return ref.astype(np.float32)


def low_ref_multiscale(low: np.ndarray, mask: np.ndarray, levels: int = 4) -> np.ndarray:
    """(c) 多尺度引导：逐级降采样，从最粗的一级开始填，再逐级上采样细化。

    这是「拉普拉斯金字塔引导填充」的低频版。
    作用：让大范围的结构（比如远处的地面斜线）能被远处的像素影响，
          而不是只看 mask 边界那一圈。
    """
    h, w = low.shape[:2]
    pyr = [low]
    mpyr = [(mask > 0).astype(np.float32)]
    for _ in range(levels):
        if min(pyr[-1].shape[:2]) < 8:
            break
        pyr.append(cv2.resize(pyr[-1], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA))
        mpyr.append(cv2.resize(mpyr[-1], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_NEAREST))

    # 从最粗一级开始，逐级向上填
    cur = pyr[-1].copy()
    m = mpyr[-1]
    for i in range(len(pyr) - 1, -1, -1):
        if i < len(pyr) - 1:
            # 上采样到当前层
            cur = cv2.resize(cur, (pyr[i].shape[1], pyr[i].shape[0]),
                             interpolation=cv2.INTER_LINEAR)
        m = mpyr[i]
        keep = m < 0.5
        if keep.sum() == 0 or (~keep).sum() == 0:
            continue
        # 当前层：外面用真实的，里面用上采样带上来的估计值
        cur = np.where(keep[:, :, None], pyr[i], cur)
        # 做一次平滑，避免层级之间接缝
        cur = lowpass(cur, 2)
        # 重新贴上真实的（别把外面改了）
        cur = np.where(keep[:, :, None], pyr[i], cur)
    return cur.astype(np.float32)


def low_ref_polynomial(low: np.ndarray, mask: np.ndarray, deg: int = 3) -> np.ndarray:
    """(b) 多项式曲面拟合：用 mask 外的低频点拟合一个平滑曲面，外推进 mask 内。

    适合「背景是大面积渐变」的场景（天空、墙面、地面）。
    """
    h, w = low.shape[:2]
    outside = (mask == 0)
    # 降采样加速：拟合不需要全部像素
    step = max(1, int(np.sqrt(outside.sum() / 20000)))
    ys, xs = np.mgrid[0:h:step, 0:w:step]
    sel = outside[::step, ::step]
    xs_f, ys_f = xs[sel].astype(np.float32), ys[sel].astype(np.float32)
    zs_f = low[::step, ::step][sel]
    if xs_f.size < 50:
        return low.copy()

    # 归一化坐标，避免数值病态
    nx = (xs_f - w / 2) / (w / 2)
    ny = (ys_f - h / 2) / (h / 2)
    A = np.stack([nx**i * ny**j
                  for i in range(deg + 1) for j in range(deg + 1 - i)], axis=1)
    try:
        coef, *_ = np.linalg.lstsq(A, zs_f, rcond=None)
    except np.linalg.LinAlgError:
        return low.copy()

    ny_full = (np.mgrid[0:h, 0:w][0] - h / 2) / (h / 2)
    nx_full = (np.mgrid[0:h, 0:w][1] - w / 2) / (w / 2)
    A_full = np.stack([nx_full**i * ny_full**j
                       for i in range(deg + 1) for j in range(deg + 1 - i)], axis=2)
    return (A_full @ coef).astype(np.float32)


def compensate(img: np.ndarray, mask: np.ndarray, radius: int, gain: float,
               method: str) -> np.ndarray:
    """核心：把补全区的低频替换成外推的低频。"""
    low = lowpass(img, radius)
    if method == "guided":
        ref = low_ref_guided(low, mask)
    elif method == "multiscale":
        ref = low_ref_multiscale(low, mask)
    elif method == "poly":
        ref = low_ref_polynomial(low, mask)
    else:
        raise ValueError(method)

    detail = img.astype(np.float32) - low           # 保留原高频
    want = ref - low                                # 结构缺口
    out = detail + low + gain * want
    return np.clip(out, 0, 255).astype(np.uint8)


def metrics(name: str, out: np.ndarray, orig: np.ndarray, mask: np.ndarray) -> dict:
    """对比补全区的低频能量：补全区 vs 周围环境。

    返回 {"inner": 补全区结构强度, "ring": 周围结构强度, "ratio": 比值}。
    ratio < 1 说明补全区的结构层次比周围弱 —— 这就是「糊」的量化表现。
    """
    m = mask > 0
    inner = cv2.erode(m.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
    ring = cv2.dilate(m.astype(np.uint8), np.ones((61, 61), np.uint8)).astype(bool) & ~m

    def lo_std(a, sel):
        g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32) if a.ndim == 3 else a.astype(np.float32)
        lp = lowpass(g, 24)   # 24px 尺度 ≈ 结构
        v = lp[sel]
        return float(np.std(v))

    a = lo_std(out, inner)
    b = lo_std(orig, ring)
    r = a / max(b, 1e-6)
    print(f"  {name:28s} 补全区结构 {a:6.2f}  周围 {b:6.2f}  比值 {r:.2f}")
    return {"inner": a, "ring": b, "ratio": r}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--crop", type=int, default=1200, help="测试区域边长")
    ap.add_argument("--cx", type=float, default=0.30, help="裁剪中心 x 比例")
    ap.add_argument("--cy", type=float, default=0.55, help="裁剪中心 y 比例")
    ap.add_argument("--radius", type=int, default=32, help="结构尺度（低通半径）")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--inpaint", default="lama", choices=["lama", "cv2"],
                    help="用哪个模型产出「补全结果」来测补偿")
    args = ap.parse_args()

    src_path = Path.home() / "Desktop" / "图层 4.png"
    if not src_path.exists():
        print("找不到测试图", src_path)
        return 1

    print("加载测试图...")
    full = np.asarray(Image.open(src_path).convert("RGB"))
    H, W = full.shape[:2]
    print(f"  {W}x{H}")

    cs = args.crop
    cx, cy = int(W * args.cx), int(H * args.cy)
    crop = full[cy:cy + cs, cx:cx + cs].copy()
    print(f"  局部 ({cx},{cy}) {cs}x{cs}")

    mask = np.zeros((cs, cs), np.uint8)
    mw, mh = int(cs * 0.34), int(cs * 0.30)
    x0, y0 = (cs - mw) // 2, (cs - mh) // 2
    mask[y0:y0 + mh, x0:x0 + mw] = 255
    print(f"  mask {mw}x{mh}  ({mw * mh / (cs * cs) * 100:.1f}% 面积)")

    # ---- 产出待补偿的「补全结果」 ----
    if args.inpaint == "lama":
        print("\n用真实 LaMa 产出补全结果...")
        from app.engine import InpaintEngine

        eng = InpaintEngine()
        if not eng.ensure_model(blocking=True):
            print("  LaMa 模型不可用:", eng._load_error)
            return 1
        base_arr, dt = eng.inpaint(crop, mask, tile=True)
        print(f"  LaMa 完成，{dt:.1f}s")
        # engine 只改 mask 内，外面没动；这里取它作为 base
        base = base_arr
    else:
        print("\n用 cv2.inpaint 模拟（快速对照）")
        base = cv2.inpaint(crop, mask, 5, cv2.INPAINT_TELEA)

    m0 = metrics("补全结果（补偿前）", base, crop, mask)
    print(f"  → 低频结构保留了 {m0['ratio'] * 100:.0f}%")

    print(f"\n三种 low_ref 方法对比（gain={args.gain}, radius={args.radius}）:")
    results = {}
    for method in ("guided", "multiscale", "poly"):
        out = compensate(base, mask, radius=args.radius, gain=args.gain, method=method)
        results[method] = out
        metrics(method, out, crop, mask)

    # 存对照图
    outdir = Path(__file__).resolve().parent.parent / "docs" / "structure_probe"
    outdir.mkdir(parents=True, exist_ok=True)
    tiles = [crop, base] + [results[m] for m in ("guided", "multiscale", "poly")]
    labels = ["原图", "补全(前)", "guided", "multiscale", "poly"]
    pad = 6
    canvas = np.full((cs + 30, (cs + pad) * len(tiles) - pad, 3), 25, np.uint8)
    for i, (t, lb) in enumerate(zip(tiles, labels)):
        xx = i * (cs + pad)
        canvas[24:24 + cs, xx:xx + cs] = t
        cv2.putText(canvas, lb, (xx + 6, 17), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (230, 230, 230), 1, cv2.LINE_AA)
    p = outdir / f"B方案_低频补偿_{args.inpaint}_r{args.radius}.png"
    Image.fromarray(canvas).save(p)
    print(f"\n对照图已存 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
