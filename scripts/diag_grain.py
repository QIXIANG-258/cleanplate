"""颗粒强度滑块的标定诊断。

回答一个问题：**这个滑块的可调范围，是否真的全程都有可见效果？**

做法：固定同一张图、同一块选区，扫描一串强度值，
测量每次输出的「补全区局部标准差 σ」相对基准的增益，
再跟理论线性值对照。

用法：
    python scripts/diag_grain.py                     # 默认扫 demo.jpg
    python scripts/diag_grain.py 图片路径 [图片路径2...]

读表方法：
    0.00 那一档必须真的是 0.0%（基准）
    各档增益应随强度近似线性增长，且与「理论」列接近
    若某一整段增益恒为 0 —— 说明存在提前返回，滑块在该区间失效
    若增益全程远小于理论 —— 说明标定基准选错了

背景：这个滑块的语义在 v0.03 改过。原先定义成「达到周围质感的百分之多少」，
但实测周围质感只比补全区高 7%~26%，导致 0~1.2 整段几乎没有变化。
现在改成「在补全区现有质感上的增益倍率」（1.0 = 翻倍），全程线性。
详见 app/grain.py 里 want 处的注释。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

# 允许直接以脚本方式运行（把仓库根目录加进 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine import InpaintEngine                      # noqa: E402
from app.grain import MASK_THRESHOLD, _local_sigma        # noqa: E402

# 扫描档位。区间取满 0~2（滑块量程），中间加密以便看出线性度。
STEPS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]


def imread_unicode(path: str | Path) -> np.ndarray | None:
    """读图，支持中文路径。

    cv2.imread 遇到非 ASCII 路径会静默返回 None，必须走 fromfile + imdecode。
    """
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except OSError:
        return None


def build_mask(h: int, w: int, fy: float, fx: float, side: float = 0.3) -> np.ndarray:
    """在 (fy, fx) 处造一块边长为 side 的矩形选区。"""
    mask = np.zeros((h, w), np.uint8)
    y0, x0 = int(h * fy), int(w * fx)
    mask[y0:min(h, y0 + int(h * side)), x0:min(w, x0 + int(w * side))] = 255
    return mask


def median_sigma(rgb: np.ndarray, sel: np.ndarray, win: int = 5) -> float:
    """选区内局部标准差的中位数 —— 即「这块地方有多粗糙」。"""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return float(np.median(_local_sigma(gray, win)[sel]))


def sweep(engine: InpaintEngine, rgb: np.ndarray, mask: np.ndarray) -> None:
    """对单张图 + 单块选区跑一遍强度扫描，打印增益表。"""
    sel = mask > MASK_THRESHOLD

    base, _ = engine.inpaint(rgb, mask, grain=0.0)
    base_sigma = median_sigma(base, sel)
    if base_sigma <= 1e-9:
        print("  补全区几乎没有质感（σ≈0），无法判断增益，跳过")
        return

    print(f"  基准 σ = {base_sigma:.5f}")
    print(f"  {'强度':>6} {'σ':>9} {'实际增益':>9} {'理论':>7} {'偏差':>8}")
    print("  " + "-" * 46)

    for s in STEPS:
        out, _ = engine.inpaint(rgb, mask, grain=s)
        sigma = median_sigma(out, sel)
        gain = (sigma - base_sigma) / base_sigma * 100
        theory = s * 100
        if s == 0:
            print(f"  {s:>6.2f} {sigma:>9.5f} {gain:>8.1f}% {theory:>6.1f}% {'—':>8}")
        else:
            print(
                f"  {s:>6.2f} {sigma:>9.5f} {gain:>8.1f}% {theory:>6.1f}% "
                f"{gain - theory:>+7.1f}%"
            )


def main() -> int:
    args = sys.argv[1:]
    if args:
        files = [Path(a) for a in args]
    else:
        default = ROOT / "samples" / "demo.jpg"
        if not default.exists():
            print(f"找不到默认测试图：{default}")
            print("用法：python scripts/diag_grain.py [图片路径 ...]")
            return 1
        files = [default]

    engine = InpaintEngine()

    for path in files:
        bgr = imread_unicode(path)
        if bgr is None:
            print(f"[跳过] 读不了：{path}")
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        print(f"\n■ {path.name}  {w}x{h}")

        # 两个位置各测一次，避免单点结论（曾经踩过：单点测出「全死」，
        # 换成 16 处后发现只有 2 处真的落在死区）
        for fy, fx in [(0.35, 0.35), (0.15, 0.55)]:
            mask = build_mask(h, w, fy, fx)
            if int(mask.max()) == 0:
                continue
            print(f"  ── 位置 ({fy:.2f}, {fx:.2f})")
            sweep(engine, rgb, mask)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
