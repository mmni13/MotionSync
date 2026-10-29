from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import av
from viewer.bcap_reader import BcapReader
from viewer.app_paths import user_cache_dir
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap, QTransform
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


@dataclass(frozen=True)
class CameraView:
    id: str
    path: Path
    label: str
    is_primary: bool = False
    attachment_mode: str = "manual"
    match_details: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any], index: int = 0) -> "CameraView":
        path = Path(str(value.get("path") or value.get("video_path") or ""))
        return cls(
            id=str(value.get("id") or f"camera_{index + 1}"),
            path=path,
            label=str(value.get("label") or f"Camera {index + 1}"),
            is_primary=bool(value.get("is_primary", index == 0)),
            attachment_mode=str(value.get("attachment_mode") or "manual"),
            match_details=dict(value.get("match_details") or {}),
        )


def default_cache_root() -> Path:
    return user_cache_dir()


def cache_dir_for_video(video_path: Path, cache_root: Path | None = None) -> Path:
    video_path = Path(video_path).resolve()
    stat = video_path.stat()
    identity = f"{video_path}|{stat.st_size}|{stat.st_mtime_ns}"
    root = Path(cache_root) if cache_root is not None else default_cache_root()
    return root / f"{video_path.stem}__{hashlib.md5(identity.encode('utf-8')).hexdigest()[:10]}"


class CameraThumbnailWidget(QFrame):
    clicked = Signal(str)

    def __init__(self, camera_id: str, label: str, parent=None):
        super().__init__(parent)
        self.camera_id = str(camera_id)
        self.setObjectName("cameraThumbnail")
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(178)
        self.setMaximumWidth(238)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 6)
        layout.setSpacing(4)

        self.image_label = QLabel("Preview")
        self.image_label.setObjectName("cameraThumbnailImage")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(160, 90)
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.title_label = QLabel(str(label))
        self.title_label.setObjectName("cameraThumbnailTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.status_label = QLabel("")
        self.status_label.setObjectName("cameraThumbnailStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(self.image_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.camera_id)
            event.accept()
            return
        super().mousePressEvent(event)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", bool(active))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))

    def set_image(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        self.image_label.setPixmap(
            pixmap.scaled(
                self.image_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )


class _ThumbnailSignals(QObject):
    finished = Signal(str, int, object, str)


class _ThumbnailJob(QRunnable):
    def __init__(
        self,
        camera_id: str,
        generation: int,
        path: Path,
        seconds: float,
        rotation: int,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.camera_id = str(camera_id)
        self.generation = int(generation)
        self.path = Path(path)
        self.seconds = max(0.0, float(seconds))
        self.rotation = int(rotation or 0) % 360
        self.cancel_event = cancel_event
        self.signals = _ThumbnailSignals()

    def _cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def run(self):
        image = None
        error = ""
        container = None
        try:
            if self._cancelled():
                return

            if self.path.suffix.lower() == ".bcap":
                index_path = cache_dir_for_video(self.path) / "bcap_frame_index.npz"
                reader = BcapReader(self.path, fps=100.0, index_path=index_path)
                reader.open()
                try:
                    if self._cancelled():
                        return
                    frame_index = reader.time_to_frame(self.seconds)
                    image = reader.read_image(frame_index)
                    if self.rotation:
                        transform = QTransform()
                        transform.rotate(self.rotation)
                        image = image.transformed(transform)
                finally:
                    reader.close()
                if not self._cancelled():
                    self.signals.finished.emit(
                        self.camera_id,
                        self.generation,
                        image,
                        "",
                    )
                return

            container = av.open(str(self.path))
            if self._cancelled():
                return

            stream = container.streams.video[0]
            seek_seconds = max(0.0, self.seconds - 0.25)
            try:
                pts = int(seek_seconds / float(stream.time_base))
                container.seek(
                    pts,
                    any_frame=False,
                    backward=True,
                    stream=stream,
                )
            except Exception:
                container.seek(
                    int(seek_seconds * av.time_base),
                    any_frame=False,
                    backward=True,
                )

            best = None
            best_distance = float("inf")
            for frame in container.decode(stream):
                if self._cancelled():
                    return

                frame_seconds = None
                if frame.pts is not None and stream.time_base is not None:
                    frame_seconds = float(frame.pts * stream.time_base)
                if frame_seconds is None:
                    frame_seconds = self.seconds

                distance = abs(frame_seconds - self.seconds)
                if distance < best_distance:
                    best = frame
                    best_distance = distance

                if frame_seconds >= self.seconds or best_distance < 0.01:
                    break

            if best is None or self._cancelled():
                return

            # Convert directly to a small in-memory RGB preview. No JPG/proxy
            # files are created, and a full-resolution RGB frame is avoided.
            source_w = max(1, int(best.width or 1))
            source_h = max(1, int(best.height or 1))
            scale = min(384.0 / source_w, 216.0 / source_h, 1.0)
            output_w = max(1, int(round(source_w * scale)))
            output_h = max(1, int(round(source_h * scale)))
            preview = best.reformat(
                width=output_w,
                height=output_h,
                format="rgb24",
            )
            rgb = preview.to_ndarray()

            if self._cancelled():
                return

            height, width, _ = rgb.shape
            image = QImage(
                rgb.data,
                width,
                height,
                int(rgb.strides[0]),
                QImage.Format_RGB888,
            ).copy()

            if self.rotation:
                transform = QTransform()
                transform.rotate(self.rotation)
                image = image.transformed(transform)

        except Exception as exc:
            if not self._cancelled():
                error = str(exc)
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

        if not self._cancelled():
            self.signals.finished.emit(
                self.camera_id,
                self.generation,
                image,
                error,
            )


class ThumbnailDecoder(QObject):
    frame_ready = Signal(str, int, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        # Sequential decoding is intentional. Secondary cameras must never
        # compete with each other for HEVC decoding, disk, or memory bandwidth.
        self.pool.setMaxThreadCount(1)
        self._cancel_event = threading.Event()

    @property
    def is_busy(self) -> bool:
        return self.pool.activeThreadCount() > 0

    def cancel(self) -> None:
        """Cancel the running job cooperatively and remove queued jobs."""
        self._cancel_event.set()
        try:
            self.pool.clear()
        except Exception:
            pass
        self._cancel_event = threading.Event()

    def request(
        self,
        camera_id: str,
        generation: int,
        path: Path,
        seconds: float,
        rotation: int = 0,
    ) -> None:
        job = _ThumbnailJob(
            camera_id,
            generation,
            path,
            seconds,
            rotation,
            self._cancel_event,
        )
        job.signals.finished.connect(self.frame_ready)
        self.pool.start(job)
