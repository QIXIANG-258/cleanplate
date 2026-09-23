"""图像处理工具。

这里的 norm_img / pad_img_to_modulo / ceil_modulo 与 IOPaint 的实现保持一致，
保证喂给模型的张量格式正确（这是最容易出错的地方）。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps


# ---------------------------------------------------------------- 张量预处理


def ceil_modulo(x: int, mod: int) -> int:
    """把 x 向上取整到 mod 的倍数。"""
    if x % mod == 0:
        return x
    return (x // mod + 1) * mod


def norm_img(np_img: np.ndarray) -> np.ndarray:
    """HWC/ HW 的 uint8 图 -> CHW 的 float32 0~1 张量数组。

    - 单通道图会自动补一个通道维（2D -> 3D），因为 LaMa 的 mask 输入需要 (1,H,W)。
    - 一定要除以 255，这是 LaMa 训练时的归一化方式，少了这步输出会是全白或全黑。
    """
    if np_img.ndim == 2:
        np_img = np_img[:, :, np.newaxis]
    np_img = np.transpose(np_img, (2, 0, 1))
    return np_img.astype("float32") / 255


def pad_img_to_modulo(
    img: np.ndarray,
    mod: int,
    square: bool = False,
    min_size: Optional[int] = None,
) -> np.ndarray:
    """把图补齐到 mod 的倍数。

    用 symmetric 模式补边（镜像反射），比填 0 效果好得多 ——
    填黑边会在图片右侧/底部留下一条明显的暗带。
    """
    if len(img.shape) == 2:
        img = img[:, :, np.newaxis]
    height, width = img.shape[:2]
    out_height = ceil_modulo(height, mod)
    out_width = ceil_modulo(width, mod)

    if min_size is not None:
        out_width = max(min_size, out_width)
        out_height = max(min_size, out_height)

    if square:
        max_size = max(out_height, out_width)
        out_height = max_size
        out_width = max_size

    return np.pad(
        img,
        ((0, out_height - height), (0, out_width - width), (0, 0)),
        mode="symmetric",
    )


def resize_max_size(
    np_img: np.ndarray, size_limit: int, interpolation: int = cv2.INTER_AREA
) -> np.ndarray:
    """长边缩到 size_limit 以内（已经够小就原样返回）。"""
    if np_img is None:
        return np_img
    h, w = np_img.shape[:2]
    if max(h, w) <= size_limit:
        return np_img
    ratio = size_limit / max(h, w)
    new_w = max(1, int(w * ratio + 0.5))
    new_h = max(1, int(h * ratio + 0.5))
    return cv2.resize(np_img, (new_w, new_h), interpolation=interpolation)


# ---------------------------------------------------------------- 读写


def md5sum(path: Path) -> str:
    """分块算 MD5，避免大文件一次性读进内存。"""
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def load_image(source: Path | bytes | bytearray) -> Tuple[np.ndarray, Optional[np.ndarray], dict, str]:
    """读图 -> (RGB uint8 HWC, alpha 或 None, 元信息, 格式扩展名)。

    元信息里保留 EXIF / ICC，保存时可原样写回，避免照片信息丢失。
    """
    if isinstance(source, (bytes, bytearray)):
        buf = io.BytesIO(source)
        ext = ""  # 交给 Pillow 自己判断
    else:
        buf = source
        ext = Path(source).suffix.lower().lstrip(".")

    with Image.open(buf) as im:
        # 按 EXIF 方向摆正，否则竖拍照片会躺倒
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass

        info = dict(im.info)
        fmt = (im.format or ext or "png").lower()

        alpha: Optional[np.ndarray] = None
        if im.mode == "RGBA":
            rgba = np.array(im)
            alpha = rgba[:, :, 3]
            rgb = cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)
        elif im.mode in ("LA", "P"):
            im = im.convert("RGB")
            rgb = np.array(im)
        else:
            rgb = np.array(im.convert("RGB"))

    return np.ascontiguousarray(rgb), alpha, info, fmt


def mask_from_alpha(png_bytes: bytes, expected_hw: Tuple[int, int]) -> np.ndarray:
    """从带透明通道的 PNG 里取出 mask。

    前端用半透明琥珀色画笔涂抹，导出 PNG 时用 alpha 通道表达选区：
    有笔画的地方 alpha 高、没画的完全透明。所以这里直接读 alpha，
    再缩放到原图尺寸即可 —— 天然带抗锯齿边缘，比二值 mask 效果好。

    expected_hw: (H, W) 目标尺寸。
    """
    with Image.open(io.BytesIO(png_bytes)) as im:
        if im.mode in ("RGBA", "LA"):
            alpha = np.array(im.convert("RGBA"))[:, :, 3]
        else:
            # 万一前端传的是灰度白底黑字 mask，也兼容一下
            alpha = np.array(im.convert("L"))

    h, w = expected_hw
    if alpha.shape[:2] != (h, w):
        interp = cv2.INTER_AREA if alpha.shape[0] > h else cv2.INTER_NEAREST
        alpha = cv2.resize(alpha, (w, h), interpolation=interp)

    return np.ascontiguousarray(alpha)


def encode_png(img_rgb: np.ndarray, quality_hint: int = 3) -> bytes:
    """RGB 数组 -> PNG 字节。quality_hint 越小压缩越弱、编码越快。"""
    ok, buf = cv2.imencode(
        ".png", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
        [int(cv2.IMWRITE_PNG_COMPRESSION), int(quality_hint)],
    )
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return buf.tobytes()


def save_image(
    img_rgb: np.ndarray,
    dest: Path,
    fmt: str = "png",
    info: Optional[dict] = None,
    original_alpha: Optional[np.ndarray] = None,
    jpeg_quality: int = 95,
) -> Path:
    """写盘。png 走 cv2（快、无损），jpg 走 Pillow（能带上 EXIF/ICC）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or "png").lower().lstrip(".")
    if fmt in ("jpeg", "jfif"):
        fmt = "jpg"

    info = dict(info or {})

    if fmt == "jpg":
        im = Image.fromarray(img_rgb)
        if original_alpha is not None:
            im = im.convert("RGB")
        exif = info.get("exif")
        save_kwargs: dict[str, Any] = {
            "quality": jpeg_quality,
            "subsampling": 0,       # 4:4:4 不丢色度，修图后保存必须
            "optimize": True,
            "progressive": True,
        }
        if exif:
            save_kwargs["exif"] = exif
        icc = info.get("icc_profile")
        if icc:
            save_kwargs["icc_profile"] = icc
        im.save(dest, format="JPEG", **save_kwargs)
    else:
        ok, buf = cv2.imencode(
            ".png",
            cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
            [int(cv2.IMWRITE_PNG_COMPRESSION), 3],
        )
        if not ok:
            raise RuntimeError("PNG 编码失败")
        dest.write_bytes(buf.tobytes())

    return dest


def image_dimensions(path: Path) -> Optional[tuple[int, int]]:
    """只读文件头拿尺寸，不解码像素 —— 对 37MB 的 PNG 也是毫秒级。"""
    try:
        with Image.open(path) as im:
            w, h = im.size
        # 竖拍照片的 EXIF 方向会让宽高对调，跟实际显示尺寸对齐一下
        try:
            with Image.open(path) as im2:
                exif = im2.getexif()
            if exif and exif.get(274) in (5, 6, 7, 8):
                w, h = h, w
        except Exception:
            pass
        return w, h
    except Exception:
        return None


def render_scaled(
    src: Path,
    max_side: int,
    cache_dir: Path,
    quality: int = 84,
) -> bytes:
    """把图片等比缩到长边 max_side，返回 JPEG 字节，结果按 (路径, 修改时间, 尺寸) 缓存。

    浏览文件夹时同一张图会被反复请求（列表缩略图 + 大图预览），
    不缓存的话每次都要解码 30MB 的原图，翻页会卡。
    """
    try:
        st = src.stat()
        key_src = f"{src}|{st.st_mtime_ns}|{st.st_size}|{max_side}|{quality}"
    except OSError:
        key_src = f"{src}|{max_side}|{quality}"
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()
    cached = cache_dir / f"{key}.jpg"
    if cached.exists():
        try:
            return cached.read_bytes()
        except OSError:
            pass

    with Image.open(src) as im:
        # JPEG 可以先按 1/2、1/4 解码，缩略图场景能快好几倍
        try:
            im.draft("RGB", (max_side * 2, max_side * 2))
        except Exception:
            pass
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)

    data = buf.getvalue()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return data


def prune_cache(cache_dir: Path, max_files: int) -> None:
    """缓存文件太多时按最旧的删一批，控制在 max_files 以内。"""
    try:
        files = [p for p in cache_dir.iterdir() if p.suffix == ".jpg"]
    except OSError:
        return
    if len(files) <= max_files:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files[: len(files) - max_files]:
        try:
            p.unlink()
        except OSError:
            pass


def list_images(folder: Path) -> list[dict]:
    """列出目录下的图片文件（不递归），带上尺寸。"""
    from . import config

    if not folder.exists() or not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS:
            try:
                st = p.stat()
            except OSError:
                continue
            dim = image_dimensions(p)
            out.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "width": dim[0] if dim else None,
                    "height": dim[1] if dim else None,
                }
            )
    return out


def list_dirs(folder: Path) -> list[dict]:
    """列出子目录（供前端选图用）。"""
    if not folder.exists() or not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_dir() and not p.name.startswith((".", "$")):
            try:
                out.append({"name": p.name, "path": str(p)})
            except OSError:
                continue
    return out


def jsonable(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
