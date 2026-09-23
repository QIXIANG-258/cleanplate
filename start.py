#!/usr/bin/env python
"""CleanPlate 统一入口。

用法（在项目根目录下执行）::

    python start.py                    # 启动 Web 界面（默认，VSCode 里按 F5 就是这条）
    python start.py serve --open       # 启动并自动打开浏览器
    python start.py download           # 只下载模型
    python start.py info               # 查看设备与模型状态
    python start.py batch -i 图片目录 -m mask.png -o 输出目录

"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 `python start.py` 在任意工作目录下都能 import 到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

from app import __app_name__, __version__, config  # noqa: E402


def _setup_logging(level: str = "INFO") -> None:
    logger.remove()

    # 注意：用 pythonw.exe 启动时 sys.stderr / sys.stdout 都是 None，
    # 直接 logger.add(None) 会抛异常。这里做个保护，
    # 实在没有可写的流就只写文件日志。
    stream = sys.stderr or sys.stdout
    if stream is not None:
        logger.add(
            stream,
            level=level,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
            colorize=True,
        )

    log_file = config.ROOT / "iopaint.log"
    logger.add(log_file, level="DEBUG", rotation="5 MB", retention=3, encoding="utf-8")


# ---------------------------------------------------------------- 子命令


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from app.engine import gpu_info
    # 注意：必须复用 server 模块里那一个 engine 实例去预热。
    # 之前这里另外 new 了一个 InpaintEngine，预热的是个没人用的对象，
    # 真正响应请求的 engine 还是冷的 —— 等于白预热一遍（还多占一份显存）。
    from app.server import app as fastapi_app, engine

    gpu = gpu_info()
    logger.info(f"{__app_name__} v{__version__}")
    if gpu["available"]:
        logger.info(f"设备：{gpu['name']}（显存 {gpu['vram_mb']} MB）  torch {gpu['torch']}+cu{gpu['cuda']}")
    else:
        logger.info(f"设备：CPU（未检测到可用 CUDA）  torch {gpu['torch']}")

    # 后台预热：模型没下载就先下载，下载完自动加载，不阻塞服务启动
    engine.load_async()

    url = f"http://{config.HOST}:{config.PORT}"
    logger.info(f"界面地址：{url}")

    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        fastapi_app,
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
        access_log=False,
    )
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    from app.engine import InpaintEngine

    eng = InpaintEngine(device=args.device or "auto")
    if args.force and eng.model_path.exists():
        eng.model_path.unlink()
        logger.info("已删除旧的模型文件")
    ok = eng.ensure_model(blocking=True)
    if ok:
        logger.info(f"模型就绪：{eng.model_path}")
        return 0
    logger.error(eng.status().get("error") or "下载失败")
    return 1


def cmd_info(args: argparse.Namespace) -> int:
    from app.engine import InpaintEngine, pick_device

    eng = InpaintEngine(device=args.device or "auto")
    st = eng.status()
    print(f"{__app_name__} v{__version__}")
    print(f"项目目录   : {config.ROOT}")
    print(f"Python     : {sys.version.split()[0]}")
    print(f"推理设备   : {st['device']}")
    try:
        import torch

        print(f"PyTorch    : {torch.__version__}（CUDA {torch.version.cuda}）")
        print(f"CUDA 可用  : {torch.cuda.is_available()}")
    except ImportError:
        print("PyTorch    : 未安装")
    g = st["gpu"]
    if g["available"]:
        print(f"显卡       : {g['name']}（{g['vram_mb']} MB）")
    print(f"模型文件   : {st['model_path']}")
    print(f"模型已下载 : {st['model_downloaded']}")
    print(f"模型已加载 : {st['model_ready']}")
    print(f"默认长边   : {st['max_side']}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """对一个文件夹里的所有图片套用同一张 mask，批量去杂物。"""
    import cv2
    import numpy as np

    from app.engine import InpaintEngine
    from app.imaging import load_image, mask_from_alpha, save_image

    src = Path(args.image)
    if not src.is_dir():
        logger.error(f"图片目录不存在：{src}")
        return 1

    files = sorted(
        p for p in src.iterdir() if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS
    )
    if not files:
        logger.error(f"目录里没有图片：{src}")
        return 1

    mask_path = Path(args.mask)
    out_dir = Path(args.output) if args.output else config.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    eng = InpaintEngine(max_side=args.max_side, device=args.device)
    if args.method == "lama" and not eng.ensure_model(blocking=True):
        logger.error("模型未就绪，批量处理中止")
        return 1

    logger.info(f"待处理 {len(files)} 张 -> {out_dir}")
    ok_count, fail = 0, 0

    for i, f in enumerate(files, 1):
        try:
            img, alpha, info, _ = load_image(f)
            h, w = img.shape[:2]

            # mask 可以是一张图（对所有图片生效），也可以是一个目录（按文件名一一对应）
            if mask_path.is_dir():
                cand = [mask_path / f"{f.stem}{e}" for e in (".png", ".PNG", ".jpg")]
                cand = [c for c in cand if c.exists()]
                if not cand:
                    logger.warning(f"[{i}/{len(files)}] 跳过 {f.name}：找不到同名 mask")
                    fail += 1
                    continue
                m_alpha = mask_from_alpha(cand[0].read_bytes(), (h, w))
            else:
                m_alpha = mask_from_alpha(mask_path.read_bytes(), (h, w))

            result, elapsed = eng.inpaint(
                img, m_alpha, expand=args.expand, max_side=args.max_side, method=args.method
            )
            dest = out_dir / f"{f.stem}_inpaint.{args.format}"
            n = 1
            while dest.exists():
                dest = out_dir / f"{f.stem}_inpaint_{n}.{args.format}"
                n += 1
            save_image(result, dest, fmt=args.format, info=info, alpha=alpha)
            logger.info(f"[{i}/{len(files)}] {f.name} -> {dest.name}  ({elapsed:.2f}s)")
            ok_count += 1
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{i}/{len(files)}] {f.name} 处理失败：{e}")
            fail += 1

    logger.info(f"批量完成：成功 {ok_count}，失败 {fail}，输出目录 {out_dir}")
    return 0 if fail == 0 else 2


# ---------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="start.py",
        description=f"{__app_name__} —— 本地 AI 去杂物工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"{__app_name__} v{__version__}")
    p.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")

    sub = p.add_subparsers(dest="command")

    ps = sub.add_parser("serve", help="启动 Web 界面（默认）")
    ps.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ps.set_defaults(func=cmd_serve)

    pd = sub.add_parser("download", help="下载 LaMa 模型")
    pd.add_argument("--force", action="store_true", help="删除已有模型重新下载")
    pd.add_argument("--device", default=None, help="cuda / cpu / auto")
    pd.set_defaults(func=cmd_download)

    pi = sub.add_parser("info", help="查看设备与模型状态")
    pi.add_argument("--device", default=None)
    pi.set_defaults(func=cmd_info)

    pb = sub.add_parser("batch", help="批量处理整个文件夹")
    pb.add_argument("-i", "--image", required=True, help="输入图片目录")
    pb.add_argument("-m", "--mask", required=True, help="mask 图片，或按文件名对应的 mask 目录")
    pb.add_argument("-o", "--output", default=None, help="输出目录，默认 output/")
    pb.add_argument("--expand", type=int, default=config.DEFAULT_EXPAND, help="mask 膨胀像素")
    pb.add_argument("--max-side", type=int, default=config.MAX_SIDE, help="送模型的长边上限")
    pb.add_argument("--method", default="lama", choices=["lama", "cv2"], help="推理方式")
    pb.add_argument("--format", default="png", choices=["png", "jpg"], help="输出格式")
    pb.add_argument("--device", default=None)
    pb.set_defaults(func=cmd_batch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve", *(argv or [])])
    _setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
