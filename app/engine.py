"""推理引擎：设备选择、显存保护、补边、合成。

对外的唯一入口是 InpaintEngine.inpaint()。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from loguru import logger

from . import config
from .imaging import pad_img_to_modulo, resize_max_size
from .lama import LamaModel, ModelDownloader, cv2_inpaint


def pick_device(pref: str = "auto") -> str:
    """决定用 GPU 还是 CPU。pref 为 auto 时有 cuda 就用 cuda。"""
    if pref == "cpu":
        return "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            if pref in ("auto", "cuda"):
                return "cuda"
        elif pref == "cuda":
            logger.warning("指定了 cuda 但当前环境不可用，回退到 cpu")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"检测 CUDA 失败（{e}），使用 cpu")
    return "cpu"


def gpu_info() -> dict:
    """拿一张显卡名片，给前端状态栏显示。"""
    info = {"available": False, "name": "", "vram_mb": 0, "torch": "", "cuda": ""}
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda"] = torch.version.cuda or ""
        if torch.cuda.is_available():
            info["available"] = True
            info["name"] = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            info["vram_mb"] = int(props.total_memory / 1024 / 1024)
    except Exception:  # noqa: BLE001
        pass
    return info


class InpaintEngine:
    """LaMa 推理引擎（线程安全，模型只加载一次）。"""

    def __init__(self, max_side: Optional[int] = None, device: Optional[str] = None):
        self.max_side = max_side or config.MAX_SIDE
        self.device_pref = device or config.DEVICE
        self.device = pick_device(self.device_pref)

        self.model_path = config.MODELS_DIR / config.MODEL_FILENAME
        self.downloader = ModelDownloader(config.MODEL_URLS, self.model_path, config.MODEL_MD5)
        self._model: Optional[LamaModel] = None
        self._lock = threading.RLock()
        self._load_error: str = ""

    # -------------------------------------------------- 模型状态

    @property
    def model_ready(self) -> bool:
        return self._model is not None and self._model.loaded

    @property
    def model_downloaded(self) -> bool:
        return self.model_path.exists()

    def status(self) -> dict:
        return {
            "device": self.device,
            "device_pref": self.device_pref,
            "gpu": gpu_info(),
            "model_ready": self.model_ready,
            "model_downloaded": self.model_downloaded,
            "model_path": str(self.model_path),
            "max_side": self.max_side,
            "download": self.downloader.state(),
            "error": self._load_error,
        }

    # -------------------------------------------------- 加载

    def ensure_model(self, blocking: bool = True, timeout: float = 3600) -> bool:
        """确保模型已下载并加载。返回是否就绪。"""
        with self._lock:
            if self.model_ready:
                return True

            if not self.downloader.verify():
                if not blocking:
                    self.downloader.start_async()
                    return False
                logger.info("模型不存在，开始下载…")
                self.downloader.download_blocking()

            if self._model is None:
                self._model = LamaModel(self.model_path, self.device)
            try:
                self._model.load()
                self._load_error = ""
            except Exception as e:  # noqa: BLE001
                self._load_error = f"{type(e).__name__}: {e}"
                logger.exception("模型加载失败")
                return False
            return True

    def load_async(self) -> None:
        """后台预热，不阻塞服务启动。"""
        def _work():
            try:
                self.ensure_model(blocking=True)
            except Exception as e:  # noqa: BLE001
                logger.error(f"后台加载模型失败：{e}")

        threading.Thread(target=_work, daemon=True).start()

    # -------------------------------------------------- 推理

    def inpaint(
        self,
        image_rgb: np.ndarray,
        mask_alpha: np.ndarray,
        expand: int = config.DEFAULT_EXPAND,
        max_side: Optional[int] = None,
        feather: float = 0.8,
        method: str = "lama",
        tile: bool = True,
    ) -> tuple[np.ndarray, float]:
        """去杂物主流程。

        image_rgb : HWC uint8 RGB 原图
        mask_alpha: HW uint8 选区（0~255，来自前端 PNG 的 alpha 通道）
        expand    : mask 向外膨胀像素数
        max_side  : 单块送模型的长边上限（None 用默认值）
        feather   : 合成时边缘羽化强度（像素），0 表示硬边
        method    : lama（默认）/ cv2（零模型快速模式）
        tile      : True 走分区域推理（大图清晰得多），False 退回整图缩放

        返回 (结果图, 耗时秒)。
        """
        t0 = time.time()
        h, w = image_rgb.shape[:2]

        # 1) mask 二值化 / 膨胀
        mask = (mask_alpha > 8).astype(np.uint8) * 255
        if mask.max() == 0:
            raise ValueError("没有检测到涂抹区域，请先用画笔圈出要去掉的杂物")

        # 至少外扩 2px：合成时边缘要做羽化，如果不多扩一点，
        # 羽化会吃掉选区最外圈，导致被去掉的东西留下一道淡淡的残影。
        expand_eff = max(int(expand), 2)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (expand_eff * 2 + 1, expand_eff * 2 + 1))
        mask = cv2.dilate(mask, k, iterations=1)

        # 2) OpenCV 快速模式，不碰模型
        if method == "cv2":
            out = cv2_inpaint(image_rgb, mask, radius=max(3, expand_eff))
            return self._composite(image_rgb, out, mask, feather), time.time() - t0

        # 3) 准备模型
        if not self.model_ready and not self.ensure_model(blocking=True):
            raise RuntimeError(self._load_error or "模型未就绪")

        limit = max_side or self.max_side

        # 4) 推理
        if tile:
            result_full = self._inpaint_regions(image_rgb, mask, limit)
        else:
            result_full = self._inpaint_full(image_rgb, mask, limit)

        # 5) 只在选区合成，选区外保持原始像素完全不变
        out = self._composite(image_rgb, result_full, mask, feather)
        elapsed = time.time() - t0
        logger.info(
            f"修复完成 {w}x{h}（{method}{'/' + ('分区' if tile else '整图')}，"
            f"耗时 {elapsed:.2f}s，设备 {self.device}）"
        )
        return out, elapsed

    # -------------------------------------------------- 整图模式

    def _inpaint_full(self, image: np.ndarray, mask: np.ndarray, limit: int) -> np.ndarray:
        """整图缩放到 limit 以内再推理。

        大图上这个模式会把补出来的内容再放大回去，细节会糊，
        所以默认走分区模式；这里主要作为「区域太大切不动」时的兜底。
        """
        h, w = image.shape[:2]
        scale = 1.0
        if limit and max(h, w) > limit:
            scale = limit / max(h, w)
            nw, nh = max(1, int(w * scale + 0.5)), max(1, int(h * scale + 0.5))
            work = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
            mwork = cv2.resize(mask, (nw, nh), interpolation=cv2.INTER_AREA)
            mwork = (mwork > 8).astype(np.uint8) * 255
        else:
            work, mwork = image, mask

        img_pad = pad_img_to_modulo(work, config.PAD_MOD)
        mask_pad = pad_img_to_modulo(mwork, config.PAD_MOD)[:, :, 0]
        wh, ww = work.shape[:2]
        pred = self._run_model(img_pad, mask_pad)[:wh, :ww]

        if scale != 1.0:
            pred = cv2.resize(pred, (w, h), interpolation=cv2.INTER_LANCZOS4)
        return pred

    # -------------------------------------------------- 分区模式

    TILE_OVERLAP = 160      # 瓦片重叠像素，用来消除接缝
    MIN_TILE = 640          # 瓦片最小边长

    def _inpaint_regions(self, image: np.ndarray, mask: np.ndarray, limit: int) -> np.ndarray:
        """只把有选区的区域裁出来，按**原始分辨率**送模型。

        为什么必须这么做：
          整图缩放（比如 7502px 压到 2560）之后，模型补出来的内容只有 1/3 分辨率，
          再放大回原图就是一团糊。改成「只裁选区附近的区域」之后，绝大多数情况下
          模型看到的就是原始像素，补出来的细节立刻清晰。顺带还省显存。
        区域特别大时（比如用户一笔刷满全图），再切成带重叠的瓦片，
        用距离边缘的渐变权重做混合，避免出现瓦片接缝。
        """
        H, W = image.shape[:2]
        n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        boxes = [
            (int(x), int(y), int(x + bw), int(y + bh))
            for x, y, bw, bh, area in stats[1:]
            if area >= 16
        ]
        if not boxes:
            return self._inpaint_full(image, mask, limit)

        # 挨得近的区域合并，免得同一个物体被拆成几块分别补，接不上
        boxes = self._merge_boxes(boxes, gap=max(48, limit // 12))
        logger.debug(f"分区推理：{n - 1} 个连通块 -> 合并为 {len(boxes)} 个区域")

        acc = np.zeros((H, W, 3), np.float32)
        wsum = np.zeros((H, W), np.float32)
        tile_limit = max(self.MIN_TILE, limit)

        for bx0, by0, bx1, by1 in boxes:
            bw, bh = bx1 - bx0, by1 - by0
            # 上下文边距：给模型一些周围环境，补出来的内容才合理
            margin = int(min(384, max(96, 0.35 * max(bw, bh))))
            cx0, cy0 = max(0, bx0 - margin), max(0, by0 - margin)
            cx1, cy1 = min(W, bx1 + margin), min(H, by1 + margin)

            tiles = self._plan_tiles(cx0, cy0, cx1, cy1, tile_limit, self.TILE_OVERLAP)
            single = len(tiles) == 1

            for tx0, ty0, tx1, ty1 in tiles:
                crop = image[ty0:ty1, tx0:tx1]
                cmask = mask[ty0:ty1, tx0:tx1]
                pred = self._run_tile(crop, cmask, limit)
                th, tw = pred.shape[:2]

                if single:
                    weights = np.ones((th, tw), np.float32)
                else:
                    weights = self._ramp_weights(th, tw, self.TILE_OVERLAP // 2)
                # 只在选区里累加，选区外一点都不碰
                weights = weights * (cmask > 0)
                acc[ty0:ty1, tx0:tx1] += pred.astype(np.float32) * weights[:, :, None]
                wsum[ty0:ty1, tx0:tx1] += weights

        covered = wsum > 1e-6
        out = image.copy()
        blended = (acc / np.maximum(wsum, 1e-6)[:, :, None]).clip(0, 255).astype(np.uint8)
        out[covered] = blended[covered]
        return out

    def _run_tile(self, crop: np.ndarray, cmask: np.ndarray, limit: int) -> np.ndarray:
        """对单个区域/瓦片跑一次推理，返回与原裁剪同尺寸的预测。"""
        h, w = crop.shape[:2]
        scale = 1.0
        # 单块瓦片原则上不超过 tile_limit，所以这里通常不会触发缩放
        if limit and max(h, w) > limit:
            scale = limit / max(h, w)
            nw, nh = max(1, int(w * scale + 0.5)), max(1, int(h * scale + 0.5))
            work = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
            mwork = cv2.resize(cmask, (nw, nh), interpolation=cv2.INTER_AREA)
            mwork = (mwork > 8).astype(np.uint8) * 255
        else:
            work, mwork = crop, cmask

        img_pad = pad_img_to_modulo(work, config.PAD_MOD)
        mask_pad = pad_img_to_modulo(mwork, config.PAD_MOD)[:, :, 0]
        wh, ww = work.shape[:2]
        pred = self._run_model(img_pad, mask_pad)[:wh, :ww]

        if scale != 1.0:
            pred = cv2.resize(pred, (w, h), interpolation=cv2.INTER_LANCZOS4)
        return pred

    @staticmethod
    def _merge_boxes(boxes: list[tuple[int, int, int, int]], gap: int) -> list[tuple[int, int, int, int]]:
        """把互相靠近（间距小于 gap）的矩形合并成一个。"""
        boxes = [list(b) for b in boxes]
        changed = True
        while changed and len(boxes) > 1:
            changed = False
            merged: list[list[int]] = []
            while boxes:
                a = boxes.pop()
                i = 0
                while i < len(boxes):
                    b = boxes[i]
                    if (a[0] - gap <= b[2] and b[0] - gap <= a[2]
                            and a[1] - gap <= b[3] and b[1] - gap <= a[3]):
                        a = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                        boxes.pop(i)
                        changed = True
                    else:
                        i += 1
                merged.append(a)
            boxes = merged
        return [tuple(b) for b in boxes]  # type: ignore[return-value]

    @staticmethod
    def _plan_tiles(x0: int, y0: int, x1: int, y1: int, tile: int, overlap: int):
        """把一个矩形切成不超过 tile 的瓦片；装得下就返回它自己。"""
        def starts(a0: int, a1: int) -> list[tuple[int, int]]:
            if a1 - a0 <= tile:
                return [(a0, a1)]
            step = max(1, tile - overlap)
            segs, s = [], a0
            while True:
                e = min(s + tile, a1)
                segs.append((s, e))
                if e >= a1:
                    break
                s += step
            return segs

        xs = starts(x0, x1)
        ys = starts(y0, y1)
        return [(a, c, b, d) for (a, b) in xs for (c, d) in ys]

    @staticmethod
    def _ramp_weights(h: int, w: int, ramp: int) -> np.ndarray:
        """中间为 1、边缘按距离线性降到 0 的权重图，用来混合相邻瓦片。"""
        ramp = max(1, min(ramp, min(h, w) // 2 or 1))
        wx = np.minimum(np.arange(w), w - 1 - np.arange(w)).astype(np.float32)
        wy = np.minimum(np.arange(h), h - 1 - np.arange(h)).astype(np.float32)
        wx = np.clip(wx / ramp, 0.0, 1.0)
        wy = np.clip(wy / ramp, 0.0, 1.0)
        # 保证不会整块权重都为 0
        return np.maximum(wy[:, None] * wx[None, :], 1e-3)

    # -------------------------------------------------- 内部

    def _run_model(self, img_pad: np.ndarray, mask_pad: np.ndarray, attempt: int = 0) -> np.ndarray:
        """跑一次模型。显存不足时逐级降尺寸，最后退到 CPU。"""
        import torch

        assert self._model is not None
        try:
            return self._model.run(img_pad, mask_pad)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if attempt >= 2:
                logger.warning("显存持续不足，改用 CPU 跑这一步")
                self._model.to("cpu")
                self.device = "cpu"
                return self._model.run(img_pad, mask_pad)

            h, w = img_pad.shape[:2]
            new_h, new_w = max(8, int(h * 0.7)), max(8, int(w * 0.7))
            logger.warning(f"显存不足，把这一块缩到 {new_w}x{new_h} 重试（第 {attempt + 1} 次）")
            img_small = cv2.resize(img_pad, (new_w, new_h), interpolation=cv2.INTER_AREA)
            mask_small = cv2.resize(mask_pad, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
            img_small = pad_img_to_modulo(img_small, config.PAD_MOD)
            mask_small = pad_img_to_modulo(mask_small, config.PAD_MOD)[:, :, 0]
            return self._run_model(img_small, mask_small, attempt + 1)

    @staticmethod
    def _composite(
        original: np.ndarray, painted: np.ndarray, mask: np.ndarray, feather: float
    ) -> np.ndarray:
        """把修复结果贴回原图。

        选区外一律用原始像素（bit 级不变），只有选区内采用模型输出。
        边缘做一点羽化，避免出现生硬的接缝。
        """
        alpha = mask.astype(np.float32) / 255.0
        if feather and feather > 0:
            alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=float(feather))
            alpha = np.clip(alpha * 1.6, 0.0, 1.0)
        alpha = alpha[:, :, None]

        out = original.astype(np.float32) * (1.0 - alpha) + painted.astype(np.float32) * alpha
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)
