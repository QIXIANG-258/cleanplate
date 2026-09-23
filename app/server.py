"""FastAPI 服务：静态页面 + 一组本地 API。

服务只监听 127.0.0.1，不对外开放。
"""

from __future__ import annotations

import os
import string
import traceback
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__, config
from .engine import InpaintEngine
from .imaging import list_dirs, list_images, mask_from_alpha, prune_cache, render_scaled
from .session import SessionManager

app = FastAPI(title="CleanPlate", version=__version__, docs_url=None, redoc_url=None)

engine = InpaintEngine()
manager = SessionManager()

# 缩略图缓存别无限长，启动时清一下超出的部分
prune_cache(config.THUMB_DIR, config.THUMB_CACHE_MAX_FILES)


# ---------------------------------------------------------------- 异常兜底


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # noqa: ANN001
    logger.error(f"未处理异常 {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------- 页面


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    p = config.WEB_DIR / "favicon.svg"
    if p.exists():
        return FileResponse(p, media_type="image/svg+xml")
    return Response(status_code=204)


# 前端静态资源挂在 /static 下
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


# ---------------------------------------------------------------- 状态


@app.get("/api/status")
def api_status():
    st = engine.status()
    st.update({"ok": True, "version": __version__, "root": str(config.ROOT)})
    return st


@app.post("/api/model/download")
def api_model_download():
    """手动触发模型下载（首次启动会自动触发，这里给个重试入口）。"""
    engine.downloader.start_async()
    return {"ok": True, "state": engine.downloader.state()}


# ---------------------------------------------------------------- 文件浏览


@app.get("/api/fs/roots")
def api_fs_roots():
    """列出可用盘符和常用目录，给前端做起点。"""
    home = Path.home()
    items = []
    for letter in string.ascii_uppercase:
        d = Path(f"{letter}:\\")
        if d.exists():
            items.append({"name": f"{letter}:", "path": str(d)})
    for name, p in (
        ("桌面", home / "Desktop"),
        ("图片", home / "Pictures"),
        ("下载", home / "Downloads"),
        ("主目录", home),
        ("项目根目录", config.ROOT),
    ):
        if p.exists():
            items.append({"name": name, "path": str(p)})
    return {"ok": True, "items": items}


@app.get("/api/fs/list")
def api_fs_list(path: str = Query(..., description="要列出的目录")):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, f"目录不存在：{path}")
    try:
        parent = str(p.parent) if p.parent != p else None
        return {
            "ok": True,
            "path": str(p),
            "parent": parent,
            "dirs": list_dirs(p),
            "images": list_images(p)[:2000],
        }
    except PermissionError as e:
        raise HTTPException(403, f"没有权限读取该目录：{e}") from e


def _checked_image(path: str) -> Path:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    if p.suffix.lower() not in config.IMAGE_EXTS:
        raise HTTPException(400, f"不是支持的图片：{p.suffix}")
    return p


@app.get("/api/fs/thumb")
def api_fs_thumb(path: str = Query(...), size: int = Query(240, ge=48, le=512)):
    """列表里的小缩略图。"""
    p = _checked_image(path)
    try:
        data = render_scaled(p, int(size), config.THUMB_DIR, quality=78)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"生成缩略图失败：{e}") from e
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/fs/preview")
def api_fs_preview(path: str = Query(...), max_side: int = Query(1800, ge=256, le=4096)):
    """选片用的大图预览。"""
    p = _checked_image(path)
    try:
        data = render_scaled(p, int(max_side), config.THUMB_DIR, quality=88)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"生成预览失败：{e}") from e
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- 会话


@app.post("/api/session/open")
def api_session_open(payload: dict):
    path = Path(str(payload.get("path", "")))
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    if path.suffix.lower() not in config.IMAGE_EXTS:
        raise HTTPException(400, f"不支持的图片格式：{path.suffix}")
    s = manager.create_from_file(path)
    return {"ok": True, "session": s.state()}


@app.post("/api/session/upload")
async def api_session_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    s = manager.create_from_bytes(data, file.filename or "untitled.png")
    return {"ok": True, "session": s.state()}


@app.get("/api/session/{sid}/image")
def api_session_image(sid: str):
    s = _need(sid)
    return Response(content=s.current_png(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/session/{sid}/original")
def api_session_original(sid: str):
    s = _need(sid)
    from .imaging import encode_png

    return Response(content=encode_png(s.original), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/session/{sid}/inpaint")
async def api_session_inpaint(
    sid: str,
    mask: UploadFile = File(...),
    expand: int = Form(config.DEFAULT_EXPAND),
    max_side: Optional[int] = Form(None),
    feather: float = Form(0.8),
    method: str = Form("lama"),
    tile: bool = Form(True),
):
    s = _need(sid)
    mask_bytes = await mask.read()
    if not mask_bytes:
        raise HTTPException(400, "没有收到 mask")

    alpha = mask_from_alpha(mask_bytes, (s.height, s.width))
    try:
        result, elapsed = engine.inpaint(
            s.current, alpha, expand=max(0, int(expand)),
            max_side=max_side, feather=feather, method=method, tile=tile,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("推理失败")
        raise HTTPException(500, f"推理失败：{type(e).__name__}: {e}") from e

    step = s.push(result, label="去杂物", elapsed=elapsed)
    return {"ok": True, "elapsed": round(elapsed, 2), "step": step.index, "session": s.state()}


@app.post("/api/session/{sid}/undo")
def api_session_undo(sid: str):
    s = _need(sid)
    return {"ok": s.undo(), "session": s.state()}


@app.post("/api/session/{sid}/redo")
def api_session_redo(sid: str):
    s = _need(sid)
    return {"ok": s.redo(), "session": s.state()}


@app.post("/api/session/{sid}/reset")
def api_session_reset(sid: str):
    s = _need(sid)
    s.reset()
    return {"ok": True, "session": s.state()}


@app.post("/api/session/{sid}/save")
def api_session_save(sid: str, payload: dict | None = None):
    s = _need(sid)
    payload = payload or {}
    target = payload.get("target", "output")
    fmt = payload.get("format", config.DEFAULT_SAVE_FORMAT)
    try:
        dest = s.save(target=target, fmt=fmt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"保存失败：{e}") from e
    return {"ok": True, "path": str(dest), "name": dest.name}


@app.post("/api/session/{sid}/close")
def api_session_close(sid: str):
    manager.close(sid)
    return {"ok": True}


# ---------------------------------------------------------------- 工具


def _need(sid: str):
    s = manager.get(sid)
    if s is None:
        raise HTTPException(404, "会话不存在或已过期，请重新打开图片")
    return s


def open_in_explorer(path: str) -> None:
    """在资源管理器里定位到某个文件（给「打开所在目录」按钮用）。"""
    p = Path(path)
    if not p.exists():
        return
    if os.name == "nt":
        os.startfile(p.parent)  # noqa: S606
