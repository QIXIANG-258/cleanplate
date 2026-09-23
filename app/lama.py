"""LaMa 模型：权重下载 + 加载 + 前向推理。

对应 IOPaint 里的 iopaint/model/lama.py，逻辑一致：
    image [H,W,C] RGB 0~255  +  mask [H,W]  ->  补全后的 RGB 图
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
import requests
from loguru import logger

from . import config
from .imaging import md5sum


# ---------------------------------------------------------------- 下载


class ModelDownloader:
    """带进度的模型下载器。多源依次尝试，MD5 对不上就换下一个。"""

    def __init__(self, urls: list[str], dest: Path, md5: str):
        self.urls = urls
        self.dest = dest
        self.md5 = md5
        self._lock = threading.Lock()
        self._state: dict = {
            "status": "idle",      # idle / downloading / verifying / done / error
            "url": "",
            "downloaded": 0,
            "total": 0,
            "percent": 0.0,
            "speed": 0.0,
            "message": "",
        }
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------- 状态

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # -------------------------------------------------- 校验

    def verify(self) -> bool:
        """检查本地文件是否已存在且 MD5 正确。"""
        if not self.dest.exists():
            return False
        size = self.dest.stat().st_size
        # 先按大小粗筛，省得每次启动都算 200MB 的 MD5
        if size < 50 * 1024 * 1024:
            logger.warning(f"模型文件偏小（{size} 字节），判定为损坏：{self.dest}")
            self.dest.unlink(missing_ok=True)
            return False
        logger.info(f"校验模型 MD5（{size / 1024 / 1024:.1f} MB）…")
        got = md5sum(self.dest)
        if got != self.md5:
            logger.error(f"模型 MD5 不匹配：得到 {got}，期望 {self.md5}，删除后重新下载")
            self.dest.unlink(missing_ok=True)
            return False
        logger.info("模型校验通过")
        return True

    # -------------------------------------------------- 下载

    def start_async(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.verify():
            self._set(status="done", percent=100.0, message="模型已就绪")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def download_blocking(self, progress_cb: Optional[Callable[[dict], None]] = None) -> Path:
        """阻塞式下载，命令行 / 首启动用。"""
        self._run(progress_cb)
        if self._state["status"] != "done":
            raise RuntimeError(self._state.get("message") or "模型下载失败")
        return self.dest

    def _run(self, progress_cb: Optional[Callable[[dict], None]] = None) -> None:
        if self.verify():
            self._set(status="done", percent=100.0, message="模型已就绪")
            if progress_cb:
                progress_cb(self.state())
            return

        last_err = ""
        for url in self.urls:
            try:
                self._download_one(url, progress_cb)
                if self.verify():
                    self._set(status="done", percent=100.0, message="模型已就绪")
                    if progress_cb:
                        progress_cb(self.state())
                    logger.info(f"模型已就绪：{self.dest}")
                    return
                last_err = "MD5 校验未通过"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(f"从 {url} 下载失败：{last_err}")
                self.dest.unlink(missing_ok=True)

        msg = (
            f"模型自动下载失败（{last_err}）。\n"
            f"请手动下载 big-lama.pt 放到：{self.dest}\n"
            f"下载地址：{self.urls[0]}\n"
            f"文件名必须是 {config.MODEL_FILENAME}，大小 {config.MODEL_SIZE_HINT}。"
        )
        self._set(status="error", message=msg)
        if progress_cb:
            progress_cb(self.state())

    def _download_one(self, url: str, progress_cb: Optional[Callable[[dict], None]]) -> None:
        self._set(status="downloading", url=url, percent=0.0, downloaded=0, total=0,
                  speed=0.0, message="")
        logger.info(f"开始下载模型：{url}")

        tmp = self.dest.with_suffix(self.dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=(15, 120), allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            self._set(total=total)

            done = 0
            t0 = time.time()
            last_tick = 0.0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_tick > 0.25:      # 限流，别把 UI 刷爆
                        last_tick = now
                        elapsed = max(now - t0, 1e-6)
                        st = {
                            "downloaded": done,
                            "percent": round(done / total * 100, 1) if total else 0.0,
                            "speed": done / elapsed,
                        }
                        self._set(**st)
                        if progress_cb:
                            progress_cb(self.state())

        self._set(status="verifying", message="正在校验…")
        tmp.replace(self.dest)

    def stop(self) -> None:
        self._set(status="idle", message="已取消")


# ---------------------------------------------------------------- 模型


class LamaModel:
    """big-lama 的 TorchScript 封装。"""

    def __init__(self, model_path: Path, device: str = "cpu"):
        self.model_path = model_path
        self.device = device
        self.model = None

    def load(self) -> "LamaModel":
        import torch

        logger.info(f"加载 LaMa 模型：{self.model_path} -> {self.device}")
        # 先在 CPU 上反序列化再搬到目标设备：直接在 cuda 上 load 会多占一份显存
        self.model = torch.jit.load(str(self.model_path), map_location="cpu").to(self.device)
        self.model.eval()
        logger.info("模型加载完成")
        return self

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def to(self, device: str) -> None:
        import torch

        if self.model is None:
            return
        self.model.to(device)
        self.device = device
        if device == "cuda":
            torch.cuda.empty_cache()

    def run(self, image_pad: np.ndarray, mask_pad: np.ndarray) -> np.ndarray:
        """执行一次推理。

        image_pad: HWC uint8 RGB，尺寸已是 8 的倍数
        mask_pad : HW  uint8（0/255），尺寸与 image_pad 一致
        返回      : HWC uint8 RGB，尺寸与输入一致
        """
        import torch

        from .imaging import norm_img

        t_img = torch.from_numpy(norm_img(image_pad)).unsqueeze(0).to(self.device)
        t_mask = torch.from_numpy(norm_img(mask_pad)).unsqueeze(0).to(self.device)
        # LaMa 的 mask 输入是 float 0/1，不是 0~1 的连续值，这里二值化
        t_mask = (t_mask > 0).to(t_mask.dtype)

        with torch.inference_mode():
            out = self.model(t_img, t_mask)

        out = out[0].permute(1, 2, 0).detach().float().clamp_(0, 1).mul_(255)
        return out.to(torch.uint8).cpu().numpy()


# ---------------------------------------------------------------- OpenCV 兜底


def cv2_inpaint(image_rgb: np.ndarray, mask: np.ndarray, radius: int = 5,
                method: str = "telea") -> np.ndarray:
    """零模型的快速修复（OpenCV），只适合极简背景的小面积瑕疵。

    不需要下载任何模型，秒出图；但纹理复杂的地方会糊成一片。
    """
    flag = cv2.INPAINT_TELEA if method == "telea" else cv2.INPAINT_NS
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    out = cv2.inpaint(bgr, mask, radius, flag)
    return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


def clear_dir(path: Path) -> None:
    """清空目录内容（用于清理会话缓存）。"""
    if not path.exists():
        return
    for p in path.iterdir():
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass
