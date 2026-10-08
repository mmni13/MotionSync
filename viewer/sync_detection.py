from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Any

import cv2

from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QApplication,
)
from PySide6.QtGui import QPixmap, QKeyEvent, QTransform, QImage
from PySide6.QtCore import Qt

if TYPE_CHECKING:
    from viewer.video_backend import VideoBackend


logger = logging.getLogger(__name__)

DATA_START_DELAY_MS: float = 136.66  + 166.66# empirical delay from LED ON to data time zero, in milliseconds + extra calculated buffer
ROUNDING_POLICY: str = "round"
FRAME_SPACE_CACHE: str = "cache"
META_VERSION: int = 4


# ---------------- FPS helpers ----------------

def _get_video_fps(video_path: Path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        return fps if fps > 0 else 30.0
    finally:
        cap.release()


def _get_backend_skip(video_backend: "VideoBackend") -> int:
    try:
        return max(
            1,
            int(getattr(video_backend, "skip", getattr(video_backend, "_skip", 1)) or 1),
        )
    except Exception:
        return 1


def _get_cache_fps(*, video_backend: "VideoBackend", original_fps: float, skip: int) -> float:
    backend_fps = float(getattr(video_backend, "fps", 0.0) or 0.0)
    if backend_fps > 0:
        return backend_fps
    return float(original_fps) / max(1, skip)


def _frames_for_delay_original(*, original_fps: float, delay_ms: float) -> int:
    x = original_fps * (delay_ms / 1000.0)
    if ROUNDING_POLICY == "floor":
        return int(math.floor(x))
    if ROUNDING_POLICY == "ceil":
        return int(math.ceil(x))
    return int(round(x))


def _cache_to_original(cache_frame: int, *, skip: int) -> int:
    return int(cache_frame * skip)


def _original_to_cache(original_frame: int, *, skip: int) -> int:
    return int(math.ceil(original_frame / skip))


# ---------------- core math ----------------

def apply_data_start_delay_cache_space(
    *,
    led_frame_cache: int,
    original_fps: float,
    skip: int,
    frame_count_cache: Optional[int] = None,
    delay_ms: float = DATA_START_DELAY_MS,
) -> tuple[int, int, int, int]:

    led_orig = _cache_to_original(led_frame_cache, skip=skip)
    offset_orig = _frames_for_delay_original(original_fps=original_fps, delay_ms=delay_ms)
    sync_orig = max(0, led_orig - offset_orig)
    sync_cache = _original_to_cache(sync_orig, skip=skip)

    if frame_count_cache:
        sync_cache = min(sync_cache, frame_count_cache - 1)

    return sync_cache, led_orig, sync_orig, offset_orig


def _is_cache_frame_space_safe(data: dict, video_backend: "VideoBackend") -> bool:
    frame_space = data.get("frame_space")
    skip = _get_backend_skip(video_backend)
    if frame_space == FRAME_SPACE_CACHE:
        return True
    if frame_space is None and skip <= 1:
        return True
    return False


# ---------------- UI ----------------

class _SyncConfirmDialog(QDialog):
    """
    Modal dialog to manually confirm the FIRST LED-ON frame.

    Controls:
    - A / D : step -1 / +1 frame
    - S / W : step -10 / +10 frames
    - R     : rotate preview 90 degrees clockwise
    - Enter : accept
    - Esc   : cancel, returns current/proposed frame
    """

    def __init__(self, video_backend: "VideoBackend", initial_frame: int = 0):
        super().__init__()

        self.setWindowTitle("Confirm LED synchronization frame")
        self.setWindowModality(Qt.ApplicationModal)

        self._vb = video_backend
        frame_count = int(getattr(self._vb, "frame_count", 0) or 0)
        self._max_frame = max(0, frame_count - 1) if frame_count > 0 else 0

        self.selected_frame = max(0, min(int(initial_frame or 0), self._max_frame))
        self._rotation = int(getattr(self._vb, "sync_rotation", 0) or 0) % 360

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)

        self.info_label = QLabel()
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_label.setAlignment(Qt.AlignCenter)

        self.help_label = QLabel(
            "Select the FIRST cached/backend frame where the LED is ON.\n"
            "A/D = ±1 frame, S/W = ±10 frames, R = rotate preview, Enter = accept."
        )
        self.help_label.setAlignment(Qt.AlignCenter)

        rotate_btn = QPushButton("Rotate 90°")
        rotate_btn.clicked.connect(self._on_rotate_clicked)

        accept_btn = QPushButton("Accept")
        accept_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(rotate_btn)
        btn_layout.addWidget(accept_btn)
        btn_layout.addWidget(cancel_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.image_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.help_label)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        self._update_display()
        self.setFocus()

    @staticmethod
    def _to_pixmap(frame_obj: Any) -> Optional[QPixmap]:
        if frame_obj is None:
            return None
        if isinstance(frame_obj, QPixmap):
            return frame_obj
        if isinstance(frame_obj, QImage):
            return QPixmap.fromImage(frame_obj)
        return None

    def _update_display(self) -> None:
        frame_obj = self._vb.get_frame(self.selected_frame)
        pix = self._to_pixmap(frame_obj)

        if pix is None or pix.isNull():
            self.image_label.setText("(no frame available)")
            self.image_label.setPixmap(QPixmap())
        else:
            disp_pix = pix

            if self._rotation % 360 != 0:
                transform = QTransform()
                transform.rotate(self._rotation)
                try:
                    disp_pix = pix.transformed(transform, Qt.SmoothTransformation)
                except Exception:
                    disp_pix = pix

            scaled = disp_pix.scaled(
                960,
                540,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )

            self.image_label.setPixmap(scaled)

        self.info_label.setText(
            f"LED frame, cache space: {self.selected_frame} / {self._max_frame} "
            f"(preview rotation {self._rotation}°)"
        )

        # ✅ FIX: force repaint (important when using cached JPG backend)
        self.image_label.repaint()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        old = self.selected_frame

        if key in (Qt.Key_A, Qt.Key_Left):
            self.selected_frame = max(0, self.selected_frame - 1)
        elif key in (Qt.Key_D, Qt.Key_Right):
            self.selected_frame = min(self._max_frame, self.selected_frame + 1)
        elif key in (Qt.Key_W, Qt.Key_Up):
            self.selected_frame = min(self._max_frame, self.selected_frame + 10)
        elif key in (Qt.Key_S, Qt.Key_Down):
            self.selected_frame = max(0, self.selected_frame - 10)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        elif key == Qt.Key_Escape:
            self.reject()
            return
        elif key == Qt.Key_R:
            self._rotation = (self._rotation + 90) % 360
            self._update_display()
            return
        else:
            super().keyPressEvent(event)
            return

        if self.selected_frame != old:
            self._update_display()

    def _on_rotate_clicked(self) -> None:
        self._rotation = (self._rotation + 90) % 360
        self._update_display()

    def accept(self) -> None:
        try:
            setattr(self._vb, "sync_rotation", int(self._rotation))
        except Exception:
            pass
        super().accept()


def confirm_sync_frame(
    video_backend: "VideoBackend",
    initial_frame: int = 0,
) -> int:
    initial_frame = int(initial_frame or 0)

    app = QApplication.instance()
    if app is None:
        logger.warning(
            "No QApplication; cannot confirm interactively. Returning initial frame %d",
            initial_frame,
        )
        return initial_frame

    dlg = _SyncConfirmDialog(video_backend, initial_frame)
    result = dlg.exec()

    return int(dlg.selected_frame)

def confirm_sync_frame(video_backend: "VideoBackend", initial_frame: int = 0) -> int:
    if QApplication.instance() is None:
        return initial_frame
    dlg = _SyncConfirmDialog(video_backend, initial_frame)
    dlg.exec()
    return dlg.selected_frame


# ---------------- JSON ----------------

def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(exist_ok=True, parents=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _apply_rotation(video_backend: "VideoBackend", data: dict):
    try:
        if "rotation" in data:
            setattr(video_backend, "sync_rotation", int(data["rotation"]))
    except Exception:
        pass


# ---------------- main ----------------

def get_or_create_sync_frame(video_path: Path, cache_dir: Path, video_backend: "VideoBackend"):

    sync_file = cache_dir / "sync_meta.json"
    cache_meta_file = cache_dir / "cache_meta.json"

    original_fps = _get_video_fps(video_path)
    skip = _get_backend_skip(video_backend)

    # ---------- 1 sync_meta ----------
    if sync_file.exists():
        data = _read_json(sync_file)

        if _is_cache_frame_space_safe(data, video_backend) and isinstance(data.get("sync_frame"), int):
            _apply_rotation(video_backend, data)
            user_offset = int(data.get("user_offset_frames", 0) or 0)
            if (bool(getattr(video_backend, "uses_timestamp_timeline", False))
                    and isinstance(data.get("led_frame"), int)
                    and not isinstance(data.get("sync_time_seconds"), (int, float))):
                led_frame = int(data["led_frame"])
                led_time = float(video_backend.frame_to_time(led_frame))
                sync_time = max(0.0, led_time - DATA_START_DELAY_MS / 1000.0)
                sync_frame = int(video_backend.time_to_frame(sync_time))
                data.update({"sync_frame": sync_frame, "led_time_seconds": led_time,
                             "sync_time_seconds": sync_time, "timestamp_timeline": True,
                             "data_delay_ms": float(DATA_START_DELAY_MS)})
                _write_json(sync_file, data)
                return sync_frame, user_offset
            return int(data["sync_frame"]), user_offset

    # ---------- 2 cache_meta ----------
    if cache_meta_file.exists():
        meta = _read_json(cache_meta_file)

        if (
            isinstance(meta.get("sync_frame"), int)
            and _is_cache_frame_space_safe(meta, video_backend)
        ):
            if not sync_file.exists():
                _apply_rotation(video_backend, meta)

            user_offset = int(meta.get("user_offset_frames", 0) or 0)

            return int(meta["sync_frame"]), user_offset

    # ---------- 3 manual ----------
    led_frame_cache = confirm_sync_frame(video_backend, 0)

    led_time_seconds = float(video_backend.frame_to_time(led_frame_cache))
    if bool(getattr(video_backend, "uses_timestamp_timeline", False)):
        sync_time_seconds = max(0.0, led_time_seconds - DATA_START_DELAY_MS / 1000.0)
        sync_frame_cache = int(video_backend.time_to_frame(sync_time_seconds))
        led_orig = _cache_to_original(led_frame_cache, skip=skip)
        sync_orig = _cache_to_original(sync_frame_cache, skip=skip)
        offset_orig = max(0, led_orig - sync_orig)
    else:
        sync_frame_cache, led_orig, sync_orig, offset_orig = apply_data_start_delay_cache_space(
            led_frame_cache=led_frame_cache, original_fps=original_fps, skip=skip)
        sync_time_seconds = float(video_backend.frame_to_time(sync_frame_cache))

    rotation = int(getattr(video_backend, "sync_rotation", 0))

    # write sync_meta
    _write_json(sync_file, {
        "led_frame": led_frame_cache,
        "sync_frame": sync_frame_cache,
        "led_time_seconds": float(led_time_seconds),
        "sync_time_seconds": float(sync_time_seconds),
        "timestamp_timeline": bool(getattr(video_backend, "uses_timestamp_timeline", False)),
        "data_delay_ms": float(DATA_START_DELAY_MS),
        "frame_space": FRAME_SPACE_CACHE,
        "original_fps": original_fps,
        "skip": skip,
        "rotation": rotation,
        "user_offset_frames": 0,
    })

    # safe cache_meta write (preserve rotation)
    meta = _read_json(cache_meta_file) if cache_meta_file.exists() else {}
    existing = dict(meta)

    meta.update({
        "led_frame": led_frame_cache,
        "sync_frame": sync_frame_cache,
    })

    if "rotation" in existing:
        meta["rotation"] = existing["rotation"]
    else:
        meta["rotation"] = rotation

    _write_json(cache_meta_file, meta)

    return sync_frame_cache, 0
