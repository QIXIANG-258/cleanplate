"""全局配置：目录、服务参数、模型信息。

所有可调项都可以用环境变量覆盖，方便在 VSCode 的 launch.json 里直接改。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 目录

ROOT = Path(__file__).resolve().parent.parent

MODELS_DIR = ROOT / "models"      # 模型权重缓存
OUTPUT_DIR = ROOT / "output"      # 处理结果默认输出目录
WEB_DIR = ROOT / "web"            # 前端静态文件
SAMPLES_DIR = ROOT / "samples"    # 测试图
SESSION_DIR = ROOT / ".session"   # 编辑会话的临时数据（退出即清）
CACHE_DIR = ROOT / ".cache"       # 浏览图片时的缩略图缓存，可随时删

for _d in (MODELS_DIR, OUTPUT_DIR, SAMPLES_DIR, SESSION_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

THUMB_DIR = CACHE_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# 缩略图缓存上限（超过就按最旧的删），避免长期用下来无限膨胀
THUMB_CACHE_MAX_FILES = 3000


# ---------------------------------------------------------------- 服务

HOST = os.environ.get("IOPAINT_HOST", "127.0.0.1")
PORT = int(os.environ.get("IOPAINT_PORT", "8240"))

# 服务只监听回环地址，任何情况下都不要改成 0.0.0.0，
# 否则局域网内任何人都能读你本地的照片目录。


# ---------------------------------------------------------------- 模型

# LaMa 权重。默认走 GitHub Release；国内网络不通时自动尝试 HF 镜像。
# 无论从哪个源下载，都会用 MD5 校验，不通过就删掉换下一个源。
MODEL_NAME = "lama"
MODEL_FILENAME = "big-lama.pt"
MODEL_MD5 = "e3aa4aaa15225a33ec84f9f4bc47e500"
MODEL_SIZE_HINT = "约 200 MB"

MODEL_URLS = [
    "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt",
    "https://hf-mirror.com/smartywu/big-lama/resolve/main/big-lama.pt",
    "https://huggingface.co/smartywu/big-lama/resolve/main/big-lama.pt",
]

# LaMa 要求输入尺寸是 8 的倍数（与 IOPaint 的 pad_mod 保持一致）
PAD_MOD = 8


# ---------------------------------------------------------------- 推理参数

# 送入模型前，图片长边超过此值会先等比缩放。
# 2400 万像素的照片直接进模型会吃掉十几 GB 显存，8G 显存必须先缩。
# 缩放只影响送进模型的副本，输出会还原回原始分辨率。
MAX_SIDE = int(os.environ.get("IOPAINT_MAX_SIDE", "2560"))

# mask 向外膨胀的默认像素数。去杂物时多扩几像素，模型补出来的过渡更自然。
DEFAULT_EXPAND = int(os.environ.get("IOPAINT_EXPAND", "6"))

# 设备：auto / cuda / cpu
DEVICE = os.environ.get("IOPAINT_DEVICE", "auto")

# 编辑历史最多保留多少步（撤销用）。存磁盘，不占内存。
UNDO_LIMIT = int(os.environ.get("IOPAINT_UNDO_LIMIT", "12"))

# 保存结果时的默认格式: png（无损）/ jpg（保留 EXIF，quality 95）
DEFAULT_SAVE_FORMAT = os.environ.get("IOPAINT_SAVE_FORMAT", "png")
JPEG_QUALITY = int(os.environ.get("IOPAINT_JPEG_QUALITY", "95"))

# 允许浏览/打开的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".jfif"}
