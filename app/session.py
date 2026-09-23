"""编辑会话：持有原图 / 当前图 / 撤销历史。

历史步骤以 PNG 写在 .session/<id>/ 下，不占内存；
原图和 EXIF 元信息留在内存里，用于随时「还原」和无损保存。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

from . import config
from .imaging import encode_png, load_image, save_image


def _safe_unlink(path: Path) -> None:
    """删文件，永远不抛。清理动作失败不该打断正常流程。"""
    try:
        path.unlink(missing_ok=True)
    except BaseException:  # noqa: BLE001
        pass


@dataclass
class Step:
    """一步历史记录。"""

    index: int
    file: Path
    label: str = ""
    elapsed: float = 0.0


@dataclass
class Session:
    id: str
    name: str
    width: int
    height: int
    original: np.ndarray          # HWC RGB uint8，原始像素
    source_path: Optional[Path]   # 从本地文件打开时记录
    info: dict = field(default_factory=dict)
    alpha: Optional[np.ndarray] = None

    current: np.ndarray = field(default=None)          # type: ignore[assignment]
    steps: list[Step] = field(default_factory=list)
    cursor: int = 0
    created: float = field(default_factory=time.time)

    # -------------------------------------------------- 生命周期

    @property
    def dir(self) -> Path:
        return config.SESSION_DIR / self.id

    def initialize(self) -> "Session":
        """把原图写成第 0 步，之后所有编辑都基于它。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        first = self.dir / "0000.png"
        first.write_bytes(encode_png(self.original))
        self.steps = [Step(index=0, file=first, label="原图")]
        self.cursor = 0
        self.current = self.original.copy()
        return self

    # -------------------------------------------------- 编辑

    def push(self, image_rgb: np.ndarray, label: str = "去杂物", elapsed: float = 0.0) -> Step:
        """把一次修复的结果记为一步。会丢弃当前步之后的重做分支。"""
        for s in self.steps[self.cursor + 1:]:
            _safe_unlink(s.file)
        self.steps = self.steps[: self.cursor + 1]

        idx = len(self.steps)
        path = self.dir / f"{idx:04d}.png"
        path.write_bytes(encode_png(image_rgb))
        step = Step(index=idx, file=path, label=label, elapsed=elapsed)
        self.steps.append(step)
        self.cursor = idx
        self.current = image_rgb

        # 超出上限时把最老的一步挪走（保留原图）
        while len(self.steps) > config.UNDO_LIMIT + 1:
            victim = self.steps.pop(1)
            _safe_unlink(victim.file)
            for i, s in enumerate(self.steps):
                s.index = i
            self.cursor = len(self.steps) - 1
        return step

    def undo(self) -> bool:
        if self.cursor <= 0:
            return False
        self.cursor -= 1
        self.current = load_image(self.steps[self.cursor].file)[0]
        return True

    def redo(self) -> bool:
        if self.cursor >= len(self.steps) - 1:
            return False
        self.cursor += 1
        self.current = load_image(self.steps[self.cursor].file)[0]
        return True

    def reset(self) -> None:
        for s in self.steps[1:]:
            _safe_unlink(s.file)
        self.steps = self.steps[:1]
        self.cursor = 0
        self.current = self.original.copy()

    # -------------------------------------------------- 输出

    def current_png(self) -> bytes:
        return encode_png(self.current, quality_hint=2)

    def output_name(self, fmt: str) -> str:
        stem = Path(self.name).stem
        ext = "png" if fmt == "png" else "jpg"
        return f"{stem}_inpaint.{ext}"

    def save(self, target: str = "output", fmt: str = config.DEFAULT_SAVE_FORMAT) -> Path:
        """保存结果。

        target = output     -> 存到项目的 output/ 目录
        target = alongside  -> 存到原图同目录（加 _inpaint 后缀，绝不覆盖原图）
        若目标文件已存在，会自动加序号，任何情况下都不会覆盖已有文件。
        """
        fmt = (fmt or "png").lower()
        name = self.output_name(fmt)

        if target == "alongside" and self.source_path:
            base = self.source_path.parent
        else:
            base = config.OUTPUT_DIR

        dest = base / name
        n = 1
        while dest.exists():
            dest = base / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1

        save_image(
            self.current, dest, fmt=fmt, info=self.info,
            original_alpha=self.alpha, jpeg_quality=config.JPEG_QUALITY,
        )
        logger.info(f"已保存：{dest}")
        return dest

    def state(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "cursor": self.cursor,
            "steps": [
                {"index": s.index, "label": s.label, "elapsed": round(s.elapsed, 2)}
                for s in self.steps
            ],
            "can_undo": self.cursor > 0,
            "can_redo": self.cursor < len(self.steps) - 1,
            "source_path": str(self.source_path) if self.source_path else None,
        }

    def dispose(self) -> None:
        """清理这个会话的磁盘缓存。

        删文件这种事**绝不能把异常抛出去** —— 它只是一个清理动作，
        失败了顶多留点垃圾，不该让「打开图片」这个用户操作整体失败。
        所以这里连 BaseException 一起吞掉（比如某些环境会在 unlink 时抛 SystemExit）。
        """
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except BaseException:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 会话管理


class SessionManager:
    """极简会话池。本工具是单人本机使用，一个会话对应浏览器的一个标签页。"""

    def __init__(self, limit: int = 4):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self.limit = limit
        self._cleanup()

    def _cleanup(self) -> None:
        """启动时清掉上次残留的会话数据。"""
        if config.SESSION_DIR.exists():
            for p in config.SESSION_DIR.iterdir():
                try:
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else _safe_unlink(p)
                except OSError:
                    pass

    # --------------------------------------------------

    def create_from_file(self, path: Path) -> Session:
        rgb, alpha, info, _fmt = load_image(path)
        return self._register(
            Session(
                id=uuid.uuid4().hex[:12],
                name=path.name,
                width=rgb.shape[1],
                height=rgb.shape[0],
                original=rgb,
                source_path=path,
                info=info,
                alpha=alpha,
            )
        )

    def create_from_bytes(self, data: bytes, name: str) -> Session:
        rgb, alpha, info, _fmt = load_image(data)
        return self._register(
            Session(
                id=uuid.uuid4().hex[:12],
                name=name or "untitled.png",
                width=rgb.shape[1],
                height=rgb.shape[0],
                original=rgb,
                source_path=None,
                info=info,
                alpha=alpha,
            )
        )

    def _register(self, s: Session) -> Session:
        with self._lock:
            if len(self._sessions) >= self.limit:
                # 超出上限时淘汰最早的那个。淘汰失败也不能影响当前这次打开。
                try:
                    oldest = min(self._sessions.values(), key=lambda x: x.created)
                    self._sessions.pop(oldest.id, None)
                    oldest.dispose()
                except BaseException as e:  # noqa: BLE001
                    logger.warning(f"淘汰旧会话时出错（已忽略）：{e}")
            self._sessions[s.id] = s
        s.initialize()
        logger.info(f"会话 {s.id}：{s.name} {s.width}x{s.height}")
        return s

    def get(self, sid: str) -> Optional[Session]:
        return self._sessions.get(sid)

    def close(self, sid: str) -> None:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            s.dispose()

    def all(self) -> list[Session]:
        return list(self._sessions.values())
