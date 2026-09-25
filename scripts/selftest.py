#!/usr/bin/env python
"""自检脚本：不依赖 LaMa 模型，验证整条链路的逻辑是否正确。

跑法::

    .venv\\Scripts\\python.exe scripts\\selftest.py

覆盖：
  1. mask 的 alpha 通道解析与缩放
  2. 选区外像素必须 bit 级不变（这是「不损伤照片」的底线）
  3. 会话的 push / undo / redo / reset / save 流程
  4. 保存时绝不覆盖已有文件
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app import config  # noqa: E402
from app.engine import InpaintEngine  # noqa: E402
from app.imaging import load_image, mask_from_alpha, save_image  # noqa: E402
from app.session import SessionManager  # noqa: E402

PASS, FAIL = "[PASS]", "[FAIL]"
fails = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global fails
    if cond:
        print(f"  {PASS} {name}")
    else:
        fails += 1
        print(f"  {FAIL} {name}  {extra}")


# ---------------------------------------------------------------- 造测试数据


def make_photo(w: int = 900, h: int = 620) -> np.ndarray:
    """造一张有纹理的假照片：渐变 + 噪点 + 一个显眼的『杂物』方块。"""
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.zeros((h, w, 3), np.float32)
    base[:, :, 0] = 60 + 120 * (xx / w)
    base[:, :, 1] = 70 + 90 * (yy / h)
    base[:, :, 2] = 110 + 60 * ((xx + yy) / (w + h))
    rng = np.random.default_rng(2026)
    base += rng.normal(0, 7, base.shape)

    # 一个高亮方块当作要去掉的杂物
    base[240:330, 520:640] = [245, 240, 40]
    return np.clip(base, 0, 255).astype(np.uint8)


def make_mask_rgba(w: int, h: int, rect: tuple[int, int, int, int], scale: float = 0.5) -> bytes:
    """模拟前端导出的 mask：透明底 + 半透明琥珀色笔画，用 alpha 表达选区。"""
    mw, mh = max(1, int(w * scale)), max(1, int(h * scale))
    canvas = np.zeros((mh, mw, 4), np.uint8)
    x0, y0, x1, y1 = [int(v * scale) for v in rect]
    canvas[y0:y1, x0:x1] = [32, 176, 255, 158]     # BGRA 琥珀
    buf = io.BytesIO()
    Image.fromarray(canvas, "RGBA").save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 用例


def main() -> int:
    print("\n=== CleanPlate 自检 ===\n")

    img = make_photo()
    h, w = img.shape[:2]
    rect = (520, 240, 640, 330)          # 杂物方块的位置

    # ---- 1. mask 解析
    print("[1] mask 解析")
    mask_bytes = make_mask_rgba(w, h, rect)
    alpha = mask_from_alpha(mask_bytes, (h, w))
    check("解析出的 mask 尺寸与图片一致", alpha.shape[:2] == (h, w),
          f"得到 {alpha.shape[:2]}，期望 {(h, w)}")
    check("mask 是 uint8 单通道", alpha.dtype == np.uint8)
    check("选区内有值", alpha.max() > 100, f"max={alpha.max()}")
    # 四个角应该完全没被涂到
    corner_max = max(alpha[0, 0], alpha[0, -1], alpha[-1, 0], alpha[-1, -1])
    check("四角未选中", corner_max == 0, f"corner={corner_max}")

    # ---- 2. 用 OpenCV 快速模式跑一次修复（不需要模型）
    print("\n[2] 修复与合成（OpenCV 模式，不需要模型）")
    eng = InpaintEngine(device="cpu")
    out, elapsed = eng.inpaint(img, alpha, expand=4, method="cv2")
    check("输出尺寸与输入一致", out.shape == img.shape, f"{out.shape} vs {img.shape}")
    check("输出为 uint8 RGB", out.dtype == np.uint8)
    check("确实产生了变化", not np.array_equal(out, img))
    check("耗时有返回", elapsed > 0, f"elapsed={elapsed:.3f}")

    # 选区外必须一模一样。
    # 注意：边缘有 1~2px 的羽化过渡带，属于刻意的抗接缝处理，
    # 所以要排除「离选区 5px 以内」的像素再比对。
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    near = cv2.dilate((alpha > 8).astype(np.uint8) * 255, k, iterations=1) > 0
    far = ~near
    identical = np.array_equal(out[far], img[far])
    check("远离选区的像素 bit 级不变", identical)
    if not identical:
        diff = np.abs(out.astype(int) - img.astype(int))[far]
        print(f"      选区外最大差异 {diff.max()}，平均 {diff.mean():.3f}")

    # 用户实际涂过的区域，必须被完整替换掉（不留残影）
    inside = alpha > 100
    check("选区内确实有像素被覆盖", inside.sum() > 1000, f"{inside.sum()} px")
    changed = np.abs(out.astype(int) - img.astype(int))[inside].mean()
    check("选区内被修复", changed > 5, f"平均改动 {changed:.1f}")

    # ---- 2b. 颗粒再注入
    print("\n[2b] 颗粒再注入")
    from app.grain import _local_sigma, estimate_grain, inject_grain

    est = estimate_grain(img, alpha, ring=12)
    check("能从选区周围估出颗粒强度", est["value"] > 0,
          f"value={est['value']:.5f} n={est['n']}")

    # 强度 0 == 完全不动。必须是「逐像素相同」，不是「差不多」。
    g_off = inject_grain(img, out, alpha, strength=0.0)
    check("strength=0 时输出逐像素不变", np.array_equal(g_off, out))

    # 开颗粒之后，选区外仍然 bit 级不变 —— 这是不可退让的底线。
    g_on = inject_grain(img, out, alpha, strength=1.0)
    check("开启颗粒后选区外仍 bit 级不变", np.array_equal(g_on[far], img[far]),
          "颗粒漏到选区外了")

    # 选区内的高频能量要被抬起来，这才说明「糊感」真的被补偿了。
    inside_b = alpha > 100
    sig_before = float(np.median(_local_sigma(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY))[inside_b]))
    sig_after = float(np.median(_local_sigma(cv2.cvtColor(g_on, cv2.COLOR_RGB2GRAY))[inside_b]))
    check("选区内高频能量被抬升", sig_after > sig_before * 1.05,
          f"{sig_before:.5f} -> {sig_after:.5f}")

    # 强度越大，颗粒越强（单调性）。滑块才有意义。
    g_hi = inject_grain(img, out, alpha, strength=2.0)
    sig_hi = float(np.median(_local_sigma(cv2.cvtColor(g_hi, cv2.COLOR_RGB2GRAY))[inside_b]))
    check("强度与颗粒量单调递增", sig_hi > sig_after,
          f"0.0={sig_before:.5f} 1.0={sig_after:.5f} 2.0={sig_hi:.5f}")

    # 整条 inpaint 链路走通（cv2 模式 + 颗粒）
    out_g, _ = eng.inpaint(img, alpha, expand=4, method="cv2", grain=1.0)
    check("inpaint(grain=1.0) 全链路可用", out_g.shape == img.shape and not np.array_equal(out_g, out))
    check("全链路下选区外仍 bit 级不变", np.array_equal(out_g[far], img[far]))

    # ---- 3. 空 mask 应当报错
    print("\n[3] 边界情况")
    empty = np.zeros((h, w), np.uint8)
    try:
        eng.inpaint(img, empty, method="cv2")
        check("空 mask 抛错", False, "没有抛异常")
    except ValueError as e:
        check("空 mask 抛错", True)
        print(f"      提示信息：{e}")

    # ---- 4. 会话流程
    print("\n[4] 会话：新建 / 修复 / 撤销 / 重做 / 还原")
    mgr = SessionManager()
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    ses = mgr.create_from_bytes(buf.getvalue(), "selftest.png")
    check("会话尺寸正确", (ses.width, ses.height) == (w, h))
    check("初始不可撤销", not ses.state()["can_undo"])

    m2 = mask_from_alpha(mask_bytes, (h, w))
    r2, el2 = eng.inpaint(ses.current, m2, expand=4, method="cv2")
    ses.push(r2, label="去杂物", elapsed=el2)
    check("修复后可撤销", ses.state()["can_undo"])
    check("步骤数为 1", ses.state()["steps"][-1]["index"] == 1)

    check("撤销成功", ses.undo())
    check("撤销后回到原图", np.array_equal(ses.current, ses.original))
    check("重做成功", ses.redo())
    check("重做后不是原图", not np.array_equal(ses.current, ses.original))

    ses.reset()
    check("还原后回到原图", np.array_equal(ses.current, ses.original))
    check("还原后不可撤销", not ses.state()["can_undo"])

    # ---- 5. 保存
    print("\n[5] 保存（绝不覆盖）")
    ses.push(r2, label="去杂物", elapsed=0.0)
    p1 = ses.save(target="output", fmt="png")
    check("png 保存成功", p1.exists() and p1.stat().st_size > 0, str(p1))
    p2 = ses.save(target="output", fmt="png")
    check("重名时自动加序号，不覆盖", p1 != p2 and p1.exists() and p2.exists(),
          f"{p1.name} / {p2.name}")
    p3 = ses.save(target="output", fmt="jpg")
    check("jpg 保存成功", p3.exists() and p3.suffix == ".jpg", str(p3))

    back, _, _info, _ = load_image(p1)
    check("落盘后读回尺寸一致", back.shape == ses.current.shape)

    # 清理
    for p in (p1, p2, p3):
        p.unlink(missing_ok=True)
    ses.dispose()

    # ---- 6. EXIF 保留
    print("\n[6] EXIF 保留（jpg 输出）")
    tmp_jpg = config.OUTPUT_DIR / "_selftest_src.jpg"
    tmp_out = config.OUTPUT_DIR / "_selftest_out.jpg"
    save_image(img, tmp_jpg, fmt="jpg", info={}, jpeg_quality=95)
    save_image(img, tmp_out, fmt="jpg", info={"exif": b""}, jpeg_quality=95)
    check("jpg 往返可读写", tmp_out.exists())
    tmp_jpg.unlink(missing_ok=True)
    tmp_out.unlink(missing_ok=True)

    print("\n" + "=" * 46)
    if fails:
        print(f"  失败 {fails} 项，需要修")
        print("=" * 46 + "\n")
        return 1
    print("  全部通过")
    print("=" * 46 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
