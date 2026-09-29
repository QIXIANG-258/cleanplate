# -*- coding: utf-8 -*-
"""验证「mask 面积 → 补全质量」的关系。

目的：方向 1（引导用户分小块修）的前提是「面积小则质量好」。
这个前提必须量化验证，不能凭感觉。

指标：结构保留率 = 补全区的低频结构强度 / 周围环境的结构强度。
      < 1 说明补全区比周围平坦（糊），越接近 1 越好。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.engine import InpaintEngine  # noqa: E402


def lo_std(a: np.ndarray, sel: np.ndarray) -> float:
    g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32)
    return float(np.std(cv2.GaussianBlur(g, (49, 49), 0)[sel]))


def main() -> int:
    src = Path.home() / "Desktop" / "图层 4.png"
    if not src.exists():
        print("找不到测试图")
        return 1

    full = np.asarray(Image.open(src).convert("RGB"))
    H, W = full.shape[:2]
    cs = 1000
    cx, cy = int(W * 0.30), int(H * 0.55)
    crop = full[cy:cy + cs, cx:cx + cs].copy()
    print(f"测试区域 {cs}x{cs} 于 ({cx},{cy})")

    eng = InpaintEngine()
    if not eng.ensure_model(blocking=True):
        print("LaMa 不可用")
        return 1

    print()
    print(f"{'mask占比':>9s} {'尺寸':>13s} {'耗时':>8s} {'结构保留率':>11s}")
    print("-" * 48)
    rows = []
    for frac in (0.02, 0.05, 0.10, 0.20, 0.35, 0.50):
        mw = mh = max(8, int(cs * (frac ** 0.5)))
        mask = np.zeros((cs, cs), np.uint8)
        x0 = y0 = (cs - mw) // 2
        mask[y0:y0 + mh, x0:x0 + mw] = 255

        out, dt = eng.inpaint(crop, mask, tile=True)

        m = mask > 0
        k_in, k_ring = max(5, mw // 20), max(15, mw // 3)
        inner = cv2.erode(m.astype(np.uint8), np.ones((k_in, k_in), np.uint8)).astype(bool)
        ring = (cv2.dilate(m.astype(np.uint8), np.ones((k_ring * 2 + 1,) * 2, np.uint8)).astype(bool)) & ~m
        if inner.sum() < 50 or ring.sum() < 50:
            print(f"{frac * 100:8.1f}% {mw}x{mh:<8d} {dt:7.2f}s   (太小，跳过)")
            continue
        a, b = lo_std(out, inner), lo_std(crop, ring)
        r = a / max(b, 1e-6)
        rows.append((frac, r))
        print(f"{frac * 100:8.1f}% {mw}x{mh:<8d} {dt:7.2f}s {r * 100:10.0f}%")

    print()
    if len(rows) >= 2:
        small = np.mean([r for f, r in rows if f <= 0.05])
        big = np.mean([r for f, r in rows if f >= 0.20])
        print(f"小面积（≤5%）平均结构保留 {small * 100:.0f}%")
        print(f"大面积（≥20%）平均结构保留 {big * 100:.0f}%")
        print(f"差距 {(small - big) * 100:+.0f} 个百分点")
        if small > big * 1.15:
            print("→ 结论：小面积确实明显更好，方向 1 有依据")
        else:
            print("→ 结论：面积对质量影响不明显，方向 1 依据不足")
    return 0


if __name__ == "__main__":
    sys.exit(main())
