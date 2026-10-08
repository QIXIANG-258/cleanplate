"""颗粒再注入：把补全区缺掉的高频细节补回来。

用途：LaMa 补出来的区域结构没问题，但表面太平 —— 放大看就是「一块磨过皮的」。
人眼判断一块区域是否真实，很大程度靠表面那层细颗粒；补全区少了这层，
即使颜色完全对，也会显得假。

这个模块从选区**外面**的真实像素统计质感，再把同质的颗粒铺回选区里。

不变式（必须守住）：只在 mask 内改像素，mask 外一律不动。
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from loguru import logger

# 判断「选中」的 alpha 阈值，跟 engine.py 的 mask 二值化保持一致
MASK_THRESHOLD = 8


def _local_sigma(gray: np.ndarray, win: int = 5) -> np.ndarray:
    """局部标准差图（衡量「这块地方有多粗糙」）。

    用均值滤波而不是高斯：颗粒的尺度就是几个像素，
    高斯一糊就把我们要测的高频本身给抹掉了。
    """
    f = gray.astype(np.float32) / 255.0
    k = (max(3, int(win)), max(3, int(win)))
    mean = cv2.blur(f, k)
    sq = cv2.blur(f * f, k)
    var = np.maximum(sq - mean * mean, 0.0)
    return np.sqrt(var)


def estimate_grain(
    image: np.ndarray,
    mask: np.ndarray,
    ring: int = 12,
    win: int = 5,
    patch: int = 24,
    max_patches: int = 96,
    rng: Optional[np.random.Generator] = None,
) -> dict:
    """从选区外围一圈「环带」里估计颗粒强度。

    返回 ``{"value": float, "n": int}``：
        value —— 颗粒强度（0~1，相对满量程）
        n     —— 有效采样点数量，太少说明没法估（比如选区占满整图）

    为什么用**环带**而不是整张图：一张照片往往有天空（干净）、地面（有质感）
    两种区域。拿整张图平均的话，噪点重的地面会把强度拉高，
    最后往干净的天空里塞一堆颗粒 —— 比不做还假。只统计紧挨着选区的那一圈，
    补出来的区域才能跟它的邻居接上。
    """
    h, w = image.shape[:2]
    rng = rng or np.random.default_rng(20260925)

    inside = (mask > MASK_THRESHOLD).astype(np.uint8)
    if int(inside.sum()) == 0:
        return {"value": 0.0, "n": 0}

    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, ring * 2 + 1), max(3, ring * 2 + 1))
    )
    outer = cv2.dilate(inside * 255, k, iterations=1) > 0
    band = outer & (inside == 0)          # 环带 = 外扩后减去选区本身

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    sig = _local_sigma(gray, win)

    ys, xs = np.nonzero(band)
    if ys.size < 64:
        # 环带太小（选区把画面占满了），退回「整个选区之外」统计
        ys, xs = np.nonzero(inside == 0)
    if ys.size < 64:
        return {"value": 0.0, "n": 0}

    # 随机取若干个 patch，每个 patch 里取中位数 ——
    # 用中位数而不是均值：环带里可能夹到边缘、高对比结构，均值会被它们带偏。
    half = max(2, patch // 2)
    take = min(int(max_patches), int(ys.size))
    idx = rng.choice(ys.size, size=take, replace=False)
    vals: list[float] = []
    for i in idx:
        cy, cx = int(ys[i]), int(xs[i])
        y0, y1 = max(0, cy - half), min(h, cy + half)
        x0, x1 = max(0, cx - half), min(w, cx + half)
        if y1 - y0 < 4 or x1 - x0 < 4:
            continue
        vals.append(float(np.median(sig[y0:y1, x0:x1])))
    if not vals:
        return {"value": 0.0, "n": 0}

    return {"value": float(np.median(vals)), "n": len(vals)}


def _collect_patches(
    image: np.ndarray,
    mask: np.ndarray,
    count: int,
    size: int,
    rng: np.random.Generator,
) -> list[np.ndarray]:
    """从选区外随机抠若干小方块，作为颗粒的「种子」。

    直接用照片自己的颗粒，而不是生成高斯白噪声 —— 真实照片的噪点有它的性格
    （各通道强度不同、跟感光度/机内降噪绑定），随手生成的白噪声撒上去像电视雪花，
    反而更假。
    """
    h, w = image.shape[:2]
    outside = (mask <= MASK_THRESHOLD).astype(np.uint8)
    if int(outside.sum()) < size * size * 4:
        return []

    # 腐蚀一下，别抠到选区边界附近的羽化过渡带（那里的像素已经被混过了）
    safe = cv2.erode(outside * 255, np.ones((3, 3), np.uint8), iterations=2)
    ys, xs = np.nonzero(safe)
    if ys.size == 0:
        return []

    half = size // 2
    out: list[np.ndarray] = []
    tries = 0
    while len(out) < count and tries < count * 12:
        tries += 1
        i = int(rng.integers(0, ys.size))
        cy, cx = int(ys[i]), int(xs[i])
        y0, y1 = cy - half, cy + half
        x0, x1 = cx - half, cx + half
        if y0 < 0 or x0 < 0 or y1 > h or x1 > w:
            continue
        out.append(image[y0:y1, x0:x1])
    return out


def _grain_layer(
    image: np.ndarray,
    mask: np.ndarray,
    region: tuple[int, int, int, int],
    patch: int,
    win: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """在 region 范围内铺一层颗粒（只铺，不管强度）。

    铺法是「真实 patch 优先，缺的地方用合成噪声补」：
    真实 patch 质感对，但边界可能铺不满；合成噪声填缝，反正都要被归一化。
    """
    h, w = image.shape[:2]
    rx0, ry0, rx1, ry1 = region
    layer = np.zeros((h, w, 3), np.float32)

    src = _collect_patches(image, mask, count=64, size=patch, rng=rng)
    filled = np.zeros((h, w), bool)

    if src:
        step = max(4, patch // 2)
        blur_k = max(3, win * 3) | 1
        for y in range(ry0, ry1, step):
            for x in range(rx0, rx1, step):
                p = src[int(rng.integers(0, len(src)))]
                ph, pw = p.shape[:2]
                y1, x1 = min(ry1, y + ph), min(rx1, x + pw)
                piece = cv2.cvtColor(p[: y1 - y, : x1 - x], cv2.COLOR_RGB2GRAY).astype(np.float32)
                # 只取高频残差，把 patch 自身的低频去掉 ——
                # 不然搬进来的是一整块色斑，不是颗粒。
                piece = piece - cv2.blur(piece, (blur_k, blur_k))
                layer[y:y1, x:x1] += piece[:, :, None]
                filled[y:y1, x:x1] = True

    # 没被真实 patch 覆盖的部分用合成噪声兜底（正态白噪声，强度靠后面的归一化对齐）
    miss = ~filled
    n_miss = int(miss.sum())
    if n_miss:
        layer[miss] = rng.normal(0.0, 1.0, (n_miss, 3)).astype(np.float32)

    return layer


def inject_grain(
    image: np.ndarray,
    painted: np.ndarray,
    mask: np.ndarray,
    strength: float = 1.0,
    win: int = 5,
    patch: int = 24,
    seed: int = 20260925,
) -> np.ndarray:
    """把颗粒混进补全区。

    image    : 原图（取颗粒种子、取 mask）
    painted  : 模型补完的整图
    mask     : 选区（uint8，>8 视为选中）
    strength : 0 = 关。语义是**在补全区现有质感上的增益倍率**（2026-10-06 重标定）：
               0.5 = 现有 σ 提高一半；1.0 = σ 翻倍；2.0 = σ 变三倍。
               旧语义是「达到周围质感的百分之多少」，已废弃 —— 原因见下方 want 处注释。

    ------------------------------------------------------------------
    为什么是「闭环」而不是「按公式一次算到位」
    ------------------------------------------------------------------
    容易踩的坑：``_local_sigma`` 量的是**局部标准差**，而颗粒层的振幅是
    **逐像素**的量，两者差一个和窗口大小相关的系数。想用
    ``振幅 = sigma / 系数`` 这类公式一次算准，系数会随噪声的空间相关性漂移，
    很难标定对 —— 实测中这个系数偏了十几倍，导致不同强度算出来的目标值
    全都落在同一个上限上，**滑块整个失效**（0.6 和 2.0 输出一模一样）。

    所以这里改用闭环：先按比例试一版，**实际测一次**改完之后的 sigma，
    不够就补。这样不管系数是多少都自洽，强度滑块也真的有区分度。
    最多迭代 3 轮，每轮只多做两次中位数统计，开销可以忽略。
    """
    if strength is None or float(strength) <= 0:
        return painted

    h, w = image.shape[:2]
    inside = (mask > MASK_THRESHOLD).astype(np.uint8)
    n_inside = int(inside.sum())
    if n_inside == 0:
        return painted

    strength = float(strength)
    # 注意：estimate_grain 的结果现在**只用来做守门判断**（周围是不是一片纯色、
    # 根本没有可参考的质感），不再参与目标值计算 —— 目标值改成以补全区
    # 现有质感为基准了，见下方 want 的注释。
    est = estimate_grain(image, mask, ring=max(8, patch // 2), win=win, patch=patch)
    surround = est["value"]
    if surround <= 1e-4:
        logger.debug("颗粒：周围没有可参考的质感，跳过")
        return painted

    # 补全区当前的高频能量（灰度，跟 estimate_grain 取同一个空间）
    gray_p = cv2.cvtColor(painted, cv2.COLOR_RGB2GRAY)
    sig_p = _local_sigma(gray_p, win)
    sel = inside > 0
    cur_med = float(np.median(sig_p[sel]))

    # strength 的语义：**在补全区现有质感上再增加多少倍**（2026-10-06 重标定）。
    #   strength=0.5 → 现有 σ 提高一半（温和，日常够用）
    #   strength=1.0 → 现有 σ 翻倍（明显但不是塑料感）
    #   strength=2.0 → 现有 σ 变三倍（想要明显的胶片颗粒感时用）
    #
    # ------------------------------------------------------------------
    # 为什么从「达到周围质感的百分之多少」改成「在现有基础上增益」
    # ------------------------------------------------------------------
    # 旧语义听起来合理，实测却是坏的：LaMa 补出来的区域本来就不比周围平滑，
    # 16 处实测显示「周围 σ / 补全区 σ」只有 1.07~1.26 —— 也就是说
    # 「匹配周围质感」这个目标值只比现状高 7%~26%。
    # 于是 strength=1.0（定义为「完全匹配周围」）实际只带来约 +15% 的变化，
    # 视觉上几乎看不出，滑块前 60% 的行程（0~1.2）全被浪费掉，
    # 有效区全挤在 1.2~2.0。
    #
    # 新语义直接以「现有质感」为基准，0~2 的行程均匀映射到
    # σ 增益 0%~200%，全程都有可感知的反馈。
    #
    # 注意：这里不再做「目标值低于现状就跳过」的提前返回 —— 旧代码里
    # `want <= cur_med * 1.02` 那一条会让「补全区已比周围粗糙」时静默失效，
    # 用户拧滑块完全没反应（实测踩到过）。现在只有 strength=0 才跳过。
    want = cur_med * (1.0 + strength)

    # 颗粒层只铺在选区外接矩形内 —— 选区通常只占画面一小块，
    # 全图铺一遍纯属浪费（大图上是秒级 vs 毫秒级的差距）。
    ys, xs = np.nonzero(inside)
    pad = int(patch)
    region = (
        max(0, int(xs.min()) - pad),
        max(0, int(ys.min()) - pad),
        min(w, int(xs.max()) + pad + 1),
        min(h, int(ys.max()) + pad + 1),
    )

    rng = np.random.default_rng(seed)
    layer = _grain_layer(image, mask, region, patch, win, rng)

    # 把颗粒层归一化到「单位强度」：让 _local_sigma(layer) 恰好等于 1.0
    # （_local_sigma 返回值是「占满量程的比例」，即已除以 255）。
    # 有了这个统一基准，后面的倍率才有意义（否则层里的绝对量纲不可控）。
    unit = float(np.median(_local_sigma(cv2.cvtColor(
        np.clip(layer, -255, 255).astype(np.float32), cv2.COLOR_RGB2GRAY
    ), win)[sel])) if sel.any() else 0.0
    if unit <= 1e-9:
        # 退化情况（选区只有一个像素之类），退回用整体标准差做基准
        unit = float(layer[sel].std()) / 255.0
    if unit <= 1e-9:
        logger.debug("颗粒：样本不足，跳过")
        return painted
    layer /= unit

    # ---- 闭环搜索倍率
    # 目标：改完之后选区内的 sigma ≈ want。最多迭代 3 轮，每轮多做两次
    # 中位数统计，开销可以忽略。收工判据是误差 8% 以内。
    out = painted.astype(np.float32)
    a = inside.astype(np.float32)[:, :, None]
    k = want                                          # 第一轮用目标值当倍率
    best = None
    for _ in range(3):
        cand = np.clip(out + layer * k * a + 0.5, 0, 255).astype(np.uint8)
        got = float(np.median(_local_sigma(cv2.cvtColor(cand, cv2.COLOR_RGB2GRAY), win)[sel]))
        if best is None or abs(got - want) < abs(best[1] - want):
            best = (cand, got)
        if abs(got - want) / max(want, 1e-6) < 0.08:
            break
        # 往残余缺口方向修正。用比例迭代而不是一次算到位，
        # 因为 painted 本身可能也有一定质感（sigma 不随 k 从 0 线性增长）。
        k = float(np.clip(k * (want / max(got, 1e-6)), 0.0, 96.0))

    result = best[0]
    logger.debug(
        f"颗粒：现有 {cur_med:.4f} × (1+{strength:g}) → 目标 {want:.4f}"
        f"（周围参考 {surround:.4f}）实际 {best[1]:.4f} 倍率 {k:.2f}"
    )
    return result
