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

# 颗粒再注入强度。0 = 关；1.0 = 匹配选区周围的真实质感；>1 加强。
#
# ⚠ 默认 0（关闭）—— 这是做过实测后的决定，不是保守。
#
# 起因是「补出来的地方糊」。实测拆频带之后发现问题不在颗粒层：
#
#     频带              保留率
#     低频（结构/明暗）    54%   ← 掉得最多的在这里
#     中低频（大纹理）     99%
#     中频（纹理细节）    104%
#     中高频（细纹）      85%
#     高频（颗粒）        81%   ← 只是略掉
#
# 也就是说，LaMa 补出来的区域主要不是「缺颗粒」，而是**结构层次被压平了**
# （局部对比度的 90 分位只剩 69%，原本有起伏的地方变成均匀一片）。
# 往一个只掉了 19% 的频带里继续加能量，投入产出比很低，
# 而且从邻域搬 patch 平铺会留下块状拼接痕迹。
#
# 所以这个功能保留为**手动可选**：遇到天空、墙面这类本来就平的区域，
# 补完看着发干时可以开一点。默认不开。
#
# 想真正治「大面积糊」，得换思路（结构层补偿，或换模型），
# 详见 docs/去杂物技术选型与PS插件可行性研究.md。
DEFAULT_GRAIN = float(os.environ.get("IOPAINT_GRAIN", "0"))

# 颗粒强度滑块的上限（前端用）
GRAIN_MAX = float(os.environ.get("IOPAINT_GRAIN_MAX", "2.0"))

# 设备：auto / cuda / cpu
DEVICE = os.environ.get("IOPAINT_DEVICE", "auto")

# 编辑历史最多保留多少步（撤销用）。存磁盘，不占内存。
UNDO_LIMIT = int(os.environ.get("IOPAINT_UNDO_LIMIT", "12"))

# 保存结果时的默认格式: png（无损）/ jpg（保留 EXIF，quality 95）
DEFAULT_SAVE_FORMAT = os.environ.get("IOPAINT_SAVE_FORMAT", "png")
JPEG_QUALITY = int(os.environ.get("IOPAINT_JPEG_QUALITY", "95"))

# 允许浏览/打开的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".jfif"}
