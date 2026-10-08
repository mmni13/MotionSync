
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional
from viewer.bcap_reader import BcapReader

import av
import cv2
import numpy as np

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Qt
from PySide6.QtGui import QImage, QPixmap, QTransform

from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QGraphicsPixmapItem

ProgressCallback = Optional[Callable[[int, str], None]]


def _default_cache_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "1080_sync" / "cache"
    return Path.home() / ".1080_sync" / "cache"


class VideoBackend(QObject):
    """Hybrid backend: QVideoWidget for smooth playback, PyAV for paused frames."""

    CACHE_ROOT = _default_cache_root()
    DEFAULT_TARGET_HEIGHT = 1080
    DEFAULT_TARGET_FPS = 0.0
    STILL_CACHE_SIZE = 1080

    position_changed = Signal(int)
    frame_changed = Signal(int)
    duration_changed = Signal(int)
    playback_started = Signal()
    playback_finished = Signal()
    error = Signal(str)
    ready_changed = Signal(bool)

    def __init__(self, *, cache_root: Path | str = CACHE_ROOT, target_height: int = DEFAULT_TARGET_HEIGHT, target_fps: float = DEFAULT_TARGET_FPS, parent: QObject | None = None):
        super().__init__(parent)
        self.cache_root = Path(cache_root)
        self.target_height = int(target_height)
        self.target_fps = float(target_fps)
        self.video_path: Path | None = None
        self._cache_dir: Path | None = None
        self._fps = 0.0
        self._orig_fps = 0.0
        self._skip = 1
        self._frame_count = 0
        self._duration_seconds = 0.0
        self._frame_timestamps_seconds = np.array([], dtype=np.float64)
        self._uses_timestamp_timeline = False
        self._source_width = 0
        self._source_height = 0
        self._display_width = 0
        self._display_height = 0
        self.sync_rotation = 0
        self._ready = False
        self._source_kind = "media"
        self._bcap_reader: BcapReader | None = None
        self._bcap_frame = 0
        self._bcap_rate = 1.0
        self._bcap_started_at = 0.0
        self._bcap_start_frame = 0
        self._bcap_timer = QTimer(self)
        # The camera timeline is still 100 FPS, but decoding and painting a
        # 2048x896 JPEG 100 times per second on the GUI thread can starve Qt.
        # A 33 ms presentation tick keeps elapsed-time/frame synchronization
        # exact while rendering only the newest frame at up to about 30 FPS.
        self._bcap_timer.setInterval(33)
        self._bcap_timer.timeout.connect(self._on_bcap_tick)

        # Keep zero by default. The main UI now avoids clock/marker updates while
        # QVideoWidget is warming up, so a fixed lag compensation usually hurts.
        self.native_display_lag_frames = 0

        # Time in ms to let QVideoWidget/decoder settle before marker/frame_index
        # updates begin after play is pressed. This is the key fix for marker
        # outrunning the visible video during the short startup catch-up phase.
        self.playback_start_stabilize_ms = 50

        self.video_widget = QVideoWidget()
        self.video_widget.setObjectName("nativeVideoWidget")
        # Keyboard shortcuts belong to VideoDataViewer. The native video surface
        # must never retain focus after a source change or playback startup.
        self.video_widget.setFocusPolicy(Qt.NoFocus)
        try:
            self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        except Exception:
            pass

        self.audio_output = QAudioOutput()
        self.audio_output.setMuted(True)
        self.audio_output.setVolume(0.0)

        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video_widget)
        self.player.setAudioOutput(self.audio_output)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)

        self._pending_play = False
        self._pending_play_target_ms: int | None = None
        self._pending_play_start_scheduled = False
        self._pending_play_attempts = 0
        self._pending_play_generation = 0
        self._pending_play_timer = QTimer(self)
        self._pending_play_timer.setInterval(20)
        self._pending_play_timer.timeout.connect(self._check_pending_play_start)

        self._still_rgb_cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._still_image_cache: OrderedDict[tuple[int, int], QImage] = OrderedDict()
        self._still_pixmap_cache: OrderedDict[tuple[int, int], QPixmap] = OrderedDict()
        self._still_container = None
        self._still_stream = None
        self._still_last_index: int | None = None
        self._still_next_hint = 0

    def reset(self) -> None:
        if hasattr(self, "_bcap_timer"):
            self._bcap_timer.stop()
        if self._bcap_reader is not None:
            try:
                self._bcap_reader.close()
            except Exception:
                pass
        self._bcap_reader = None
        self._source_kind = "media"
        self._bcap_frame = 0
        self._bcap_start_frame = 0
        self._close_still_decoder()
        self._cancel_pending_play()
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:
            pass
        self.video_path = None
        self._cache_dir = None
        self._fps = 0.0
        self._orig_fps = 0.0
        self._skip = 1
        self._frame_count = 0
        self._duration_seconds = 0.0
        self._frame_timestamps_seconds = np.array([], dtype=np.float64)
        self._uses_timestamp_timeline = False
        self._source_width = 0
        self._source_height = 0
        self._display_width = 0
        self._display_height = 0
        self.sync_rotation = 0
        self._ready = False
        self._still_rgb_cache.clear()
        self._still_image_cache.clear()
        self._still_pixmap_cache.clear()
        self.ready_changed.emit(False)

    def open(
        self,
        video_path: Path | str,
        *,
        target_height: Optional[int] = None,
        target_fps: Optional[float] = None,
    ) -> None:
        self.reset()

        video_path = Path(video_path)

        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError(
                f"Video not found: {video_path}"
            )

        if target_height is not None:
            self.target_height = int(target_height)

        if target_fps is not None:
            self.target_fps = float(target_fps)

        self.video_path = video_path.resolve()

        self.cache_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._cache_dir = self._make_cache_dir(
            self.video_path
        )

        self._cache_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # BCAP must bypass PyAV because PyAV cannot read the
        # proprietary BELLCAP1 container.
        if self.video_path.suffix.lower() == ".bcap":
            self._open_bcap()
            return

        # Standard MP4, MOV, AVI, MKV, and M4V video path.
        self._source_kind = "media"

        self._probe_video_metadata()
        self._load_or_build_timestamp_timeline()
        self._load_cached_metadata_if_available()
        self._write_streaming_meta()

        self.player.setSource(
            QUrl.fromLocalFile(
                str(self.video_path)
            )
        )

        self.player.setPlaybackRate(1.0)

        self._ready = True
        self.ready_changed.emit(True)

    def _bcap_manifest_values(self) -> tuple[float, int, int, int]:
        """Return FPS, rotation, width, and height for the active BCAP camera."""
        fps = 100.0
        rotation = 0
        width = 0
        height = 0

        if self.video_path is None:
            return fps, rotation, width, height

        candidates = (
            self.video_path.with_name("session.manifest.json"),
            self.video_path.with_name("swimlab-recording.json"),
        )

        for manifest_path in candidates:
            if not manifest_path.exists():
                continue
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = payload.get("manifest", payload)
                fps = float(manifest.get("timing", {}).get("fps") or fps)
                for camera in manifest.get("cameras", []):
                    source = camera.get("source", {})
                    segments = source.get("containerSegments") or []
                    names = {
                        Path(str(segment.get("path") or "")).name.lower()
                        for segment in segments
                    }
                    if self.video_path.name.lower() not in names:
                        continue
                    media = camera.get("media", {})
                    presentation = camera.get("presentation", {})
                    fps = float(media.get("fps") or fps)
                    width = int(media.get("width") or 0)
                    height = int(media.get("height") or 0)
                    rotation = int(
                        round(float(presentation.get("quarterTurnDegrees") or 0))
                    ) % 360
                    return fps, rotation, width, height
            except Exception:
                continue

        return fps, rotation, width, height

    def _open_bcap(self) -> None:
        """Open a BELLCAP1/MJPEG source without passing it to PyAV."""
        if self.video_path is None:
            raise RuntimeError("No BCAP path set.")

        fps, rotation, width, height = self._bcap_manifest_values()
        reader = BcapReader(
            self.video_path,
            fps=fps,
            index_path=self.cache_dir / "bcap_frame_index.npz",
        )
        reader.open()

        if reader.frame_count <= 0:
            reader.close()
            raise ValueError(f"No readable JPEG frames found in {self.video_path.name}")

        self._bcap_reader = reader
        self._source_kind = "bcap"
        self._fps = float(reader.fps)
        self._orig_fps = float(reader.fps)
        self._frame_count = int(reader.frame_count)
        self._duration_seconds = float(reader.duration_seconds)
        self._frame_timestamps_seconds = np.array([], dtype=np.float64)
        self._uses_timestamp_timeline = False

        first_image = reader.read_image(0)
        self._source_width = width or first_image.width()
        self._source_height = height or first_image.height()
        self._display_width, self._display_height = self._compute_display_size(
            self._source_width,
            self._source_height,
        )
        self.sync_rotation = rotation
        self._load_cached_metadata_if_available()
        self._write_streaming_meta()
        self._ready = True
        self.duration_changed.emit(int(round(self._duration_seconds * 1000.0)))
        self.ready_changed.emit(True)

    @property
    def is_bcap(self) -> bool:
        return self._source_kind == "bcap"

    def _on_bcap_tick(self) -> None:
        if not self.is_bcap or self._bcap_reader is None or not self._ready:
            self._bcap_timer.stop()
            return

        elapsed = max(0.0, time.perf_counter() - self._bcap_started_at)
        target_frame = self._bcap_start_frame + int(
            elapsed * self._fps * self._bcap_rate
        )

        if target_frame >= self._frame_count:
            self._bcap_frame = max(0, self._frame_count - 1)
            self._bcap_timer.stop()
            self.position_changed.emit(
                int(round(self.frame_to_time(self._bcap_frame) * 1000.0))
            )
            self.frame_changed.emit(self._bcap_frame)
            self.playback_finished.emit()
            return

        if target_frame == self._bcap_frame:
            return

        self._bcap_frame = target_frame
        self.position_changed.emit(
            int(round(self.frame_to_time(target_frame) * 1000.0))
        )
        self.frame_changed.emit(target_frame)

    def create_video_widget(self) -> QVideoWidget:
        return self.video_widget
    
    @property
    def cache_dir(self) -> Path:
        if self._cache_dir is None:
            raise RuntimeError("VideoBackend has no cache_dir before open().")
        return self._cache_dir

    def _make_cache_dir(self, video_path: Path) -> Path:
        stat = video_path.stat()
        identity = f"{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        return self.cache_root / f"{video_path.stem}__{hashlib.md5(identity.encode('utf-8')).hexdigest()[:10]}"

    def _meta_file(self) -> Path:
        return self.cache_dir / "cache_meta.json"

    def _load_json(self, path: Path) -> dict:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        tmp.replace(path)

    def _probe_video_metadata(self) -> None:
        if self.video_path is None:
            raise RuntimeError("No video path set.")
        container = av.open(str(self.video_path))
        try:
            stream = container.streams.video[0]
            if stream.average_rate:
                self._orig_fps = float(stream.average_rate)
            elif stream.base_rate:
                self._orig_fps = float(stream.base_rate)
            elif stream.guessed_rate:
                self._orig_fps = float(stream.guessed_rate)
            else:
                self._orig_fps = 30.0
            self._fps = self._orig_fps
            self._source_width = int(getattr(stream.codec_context, "width", 0) or getattr(stream, "width", 0) or 0)
            self._source_height = int(getattr(stream.codec_context, "height", 0) or getattr(stream, "height", 0) or 0)
            self._display_width, self._display_height = self._compute_display_size(self._source_width, self._source_height)
            frame_count = int(stream.frames or 0)
            duration = 0.0
            if stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = float(container.duration / av.time_base)
            self._duration_seconds = max(0.0, duration)
            if frame_count <= 0 and self._duration_seconds > 0 and self._fps > 0:
                frame_count = int(round(self._duration_seconds * self._fps))
            self._frame_count = max(0, frame_count)
        finally:
            container.close()

    def _timestamp_index_file(self) -> Path:
        return self.cache_dir / "frame_timestamps.npz"

    def _needs_timestamp_timeline(self) -> bool:
        if self.video_path is None: return False
        container = av.open(str(self.video_path))
        try:
            stream = container.streams.video[0]
            average = float(stream.average_rate) if stream.average_rate else 0.0
            nominal = float(stream.base_rate or stream.guessed_rate or 0.0)
            metadata = {str(k).lower(): str(v) for k, v in dict(container.metadata or {}).items()}
            apple = metadata.get("com.apple.quicktime.make", "").lower() == "apple"
            mismatch = average > 0 and nominal > 0 and abs(average - nominal) / nominal > 0.002
            return bool(apple or mismatch)
        finally:
            container.close()

    def _load_or_build_timestamp_timeline(self) -> None:
        self._frame_timestamps_seconds = np.array([], dtype=np.float64)
        self._uses_timestamp_timeline = False
        if not self._needs_timestamp_timeline(): return
        index_file = self._timestamp_index_file()
        try:
            if index_file.exists():
                with np.load(index_file, allow_pickle=False) as payload:
                    values = np.asarray(payload["timestamps"], dtype=np.float64)
                if values.size > 1:
                    self._frame_timestamps_seconds = values
                    self._frame_count = int(values.size)
                    self._uses_timestamp_timeline = True
                    return
        except Exception: pass
        timestamps = []
        container = av.open(str(self.video_path))
        try:
            stream = container.streams.video[0]
            for packet in container.demux(stream):
                if packet.pts is not None:
                    timestamps.append(float(packet.pts * stream.time_base))
        finally:
            container.close()
        if len(timestamps) < 2: return
        values = np.asarray(sorted(set(timestamps)), dtype=np.float64)
        values -= values[0]
        self._frame_timestamps_seconds = values
        self._frame_count = int(values.size)
        self._uses_timestamp_timeline = True
        try: np.savez_compressed(index_file, timestamps=values)
        except Exception: pass

    @property
    def uses_timestamp_timeline(self) -> bool:
        return bool(self._uses_timestamp_timeline)

    def _compute_display_size(self, width: int, height: int) -> tuple[int, int]:
        width, height = int(width or 0), int(height or 0)
        if width <= 0 or height <= 0:
            return width, height
        if self.target_height <= 0 or height <= self.target_height:
            return width, height
        scale = self.target_height / height
        return max(1, int(round(width * scale))), int(self.target_height)

    def _expected_meta(self) -> dict:
        if self.video_path is None:
            raise RuntimeError("No video path set.")
        stat = self.video_path.stat()
        return {
            "backend_mode": "bcap_mjpeg" if self.is_bcap else "qt_qvideowidget_playback_pyav_still_stack_stable_marker",
            "video_path": str(self.video_path),
            "video_name": self.video_path.name,
            "video_size": int(stat.st_size),
            "video_mtime_ns": int(stat.st_mtime_ns),
            "target_height": int(self.target_height),
            "target_fps": float(self.target_fps),
            "orig_fps": float(self._orig_fps),
            "skip": int(self._skip),
            "cache_fps": float(self._fps),
            "frame_count": int(self._frame_count),
            "duration_seconds": float(self._duration_seconds),
            "source_width": int(self._source_width),
            "source_height": int(self._source_height),
            "display_width": int(self._display_width),
            "display_height": int(self._display_height),
            "cache_complete": True,
            "cache_format": "bcap-offset-index" if self.is_bcap else "native-playback-pyav-still-mode",
            "cache_version": 80,
        }

    def _write_streaming_meta(self) -> None:
        old = self._load_json(self._meta_file()) if self._meta_file().exists() else {}
        meta = self._expected_meta()
        for key in ["rotation", "led_frame", "sync_frame", "data_delay_ms", "fps_used", "rounding_policy", "frame_space", "proposed_led_frame_original", "proposed_led_frame_cache"]:
            if key in old:
                meta[key] = old[key]
        self._write_json(self._meta_file(), meta)

    def _load_cached_metadata_if_available(self) -> None:
        for file in [self._meta_file(), self.cache_dir / "sync_meta.json"]:
            if file.exists():
                data = self._load_json(file)
                try:
                    self.sync_rotation = int(data.get("rotation", self.sync_rotation) or 0)
                except Exception:
                    pass

    def needs_cache_build(self) -> bool:
        return False

    def build_cache_sync(self, progress_callback: ProgressCallback = None) -> None:
        if progress_callback:
            progress_callback(0, "Native video backend: no frame cache needed...")
        self._write_streaming_meta()
        if progress_callback:
            progress_callback(100, "Video ready.")

    def load_frames_from_cache(self, progress_callback: ProgressCallback = None) -> None:
        if self.video_path is None:
            raise RuntimeError("No video opened.")
        if progress_callback:
            progress_callback(0, "Preparing native video playback...")
        self._ready = True
        if progress_callback:
            progress_callback(100, "Video ready.")

    def clear_cache_frames(self) -> None:
        """
        Delete every cached file for the currently opened video.

        This includes:
        - cache_meta.json
        - sync_meta.json
        - frame_timestamps.npz
        - cached images, if present
        - other per-video metadata

        Run-to-video associations are handled by RunLibrary, not VideoBackend.
        """
        if self._cache_dir is None:
            return

        self._close_still_decoder()
        self._cancel_pending_play()

        try:
            self.player.stop()
        except Exception:
            pass

        self._still_rgb_cache.clear()
        self._still_image_cache.clear()
        self._still_pixmap_cache.clear()

        cache_dir = self._cache_dir

        if cache_dir.exists():
            # Delete files and any possible subdirectories from the bottom up.
            for path in sorted(
                cache_dir.rglob("*"),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                except Exception:
                    pass

            try:
                cache_dir.rmdir()
            except Exception:
                pass



    def _cancel_pending_play(self) -> None:
        self._pending_play_generation += 1
        self._pending_play = False
        self._pending_play_target_ms = None
        self._pending_play_start_scheduled = False
        self._pending_play_attempts = 0
        self._pending_play_timer.stop()

    def play(self) -> None:
        if not self.is_ready:
            return
        if self.is_bcap:
            self.start_playback(self._bcap_frame, self._bcap_rate)
            return
        self.player.play()
    def pause(self) -> None:
        if self.is_bcap:
            self._bcap_timer.stop()
            return
        self._cancel_pending_play()
        self.player.pause()
    def stop(self) -> None:
        if self.is_bcap:
            self._bcap_timer.stop()
            self._bcap_frame = 0
            self.position_changed.emit(0)
            self.frame_changed.emit(0)
            return
        self._cancel_pending_play()
        self.player.stop()
    def set_playback_rate(self, rate: float) -> None:
        rate = max(0.01, float(rate or 1.0))
        if self.is_bcap:
            self._bcap_rate = rate
            return
        self.player.setPlaybackRate(rate)
    def position_ms(self) -> int:
        if self.is_bcap:
            return int(round(self.frame_to_time(self._bcap_frame) * 1000.0))
        return int(self.player.position())
    def current_frame(self) -> int:
        if self.is_bcap:
            return int(self._bcap_frame)
        media_frame = self.time_to_frame(self.position_ms() / 1000.0)
        return self.media_frame_to_display_frame(media_frame)
    def set_position_ms(self, ms: int) -> None:
        ms = max(0, int(ms))
        if self.is_bcap:
            self._bcap_frame = self.time_to_frame(ms / 1000.0)
            self.position_changed.emit(ms)
            self.frame_changed.emit(self._bcap_frame)
            return
        self.player.setPosition(ms)
    def set_position_time(self, seconds: float) -> None:
        self.set_position_ms(int(round(float(seconds) * 1000.0)))

    def media_frame_to_display_frame(self, media_frame: int) -> int:
        return self._clamp_index(int(media_frame) - int(getattr(self, "native_display_lag_frames", 0) or 0))

    def display_frame_to_media_frame(self, display_frame: int) -> int:
        return self._clamp_index(int(display_frame) + int(getattr(self, "native_display_lag_frames", 0) or 0))

    def set_position_frame(self, frame_index: int) -> None:
        if self.is_bcap:
            self._bcap_frame = self._clamp_index(frame_index)
            self.position_changed.emit(
                int(round(self.frame_to_time(self._bcap_frame) * 1000.0))
            )
            return
        media_frame = self.display_frame_to_media_frame(frame_index)
        self.set_position_time(self.frame_to_time(media_frame))
    def request_frame(self, frame_index: int) -> None:
        self.set_position_frame(frame_index)

    def start_playback(self, start_frame: int, speed: float = 1.0) -> None:
        if not self.is_ready:
            return

        if self.is_bcap:
            self._bcap_frame = self._clamp_index(start_frame)
            self._bcap_start_frame = self._bcap_frame
            self._bcap_rate = max(0.01, float(speed or 1.0))
            self._bcap_started_at = time.perf_counter()
            self._bcap_timer.start()
            self.playback_started.emit()
            return

        self._cancel_pending_play()
        self._pending_play_generation += 1
        generation = self._pending_play_generation
        self.set_playback_rate(speed)
        target_ms = max(
            0,
            int(
                round(
                    self.frame_to_time(
                        self.display_frame_to_media_frame(start_frame)
                    ) * 1000.0
                )
            ),
        )
        self._pending_play = True
        self._pending_play_target_ms = target_ms
        self._pending_play_start_scheduled = False
        self._pending_play_attempts = 0
        self.player.pause()
        self.player.setPosition(target_ms)
        if not self._pending_play_timer.isActive():
            self._pending_play_timer.start()
        self._check_pending_play_start(generation)
    def pause_playback(self) -> None:
        self.pause()

    def _start_pending_play(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._pending_play_generation:
            return

        if not self._pending_play:
            return

        self._pending_play = False
        self._pending_play_target_ms = None
        self._pending_play_start_scheduled = False
        self._pending_play_attempts = 0
        self._pending_play_timer.stop()

        self.player.play()

        # Emit immediately. Main window handles warm-up protection.
        self.playback_started.emit()


    def _emit_playback_started(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._pending_play_generation:
            return
        self.playback_started.emit()

    def _pending_play_tolerance_ms(self) -> int:
        if self._fps <= 0:
            return 15
        frame_ms = 1000.0 / float(self._fps)
        return int(max(8.0, min(25.0, frame_ms * 0.75)))

    def _check_pending_play_start(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._pending_play_generation:
            return
        if not self._pending_play or self._pending_play_target_ms is None:
            self._pending_play_timer.stop(); return
        self._pending_play_attempts += 1
        current_ms = int(self.player.position())
        target_ms = int(self._pending_play_target_ms)
        if abs(current_ms - target_ms) <= self._pending_play_tolerance_ms():
            if not self._pending_play_start_scheduled:
                self._pending_play_start_scheduled = True
                gen = self._pending_play_generation
                QTimer.singleShot(35, lambda: self._start_pending_play(gen))
            return
        if self._pending_play_attempts % 10 == 0:
            self.player.setPosition(target_ms)
        if self._pending_play_attempts > 75:
            self.player.setPosition(target_ms)
            if not self._pending_play_start_scheduled:
                self._pending_play_start_scheduled = True
                gen = self._pending_play_generation
                QTimer.singleShot(60, lambda: self._start_pending_play(gen))

    def _on_position_changed(self, ms: int) -> None:
        ms = int(ms)
        self.position_changed.emit(ms)

        if self._pending_play:
            self._check_pending_play_start()
            return

        media_frame = self.time_to_frame(ms / 1000.0)
        display_frame = self.media_frame_to_display_frame(media_frame)

        self.frame_changed.emit(display_frame)

    def _on_duration_changed(self, ms: int) -> None:
        self.duration_changed.emit(int(ms))

    def _on_player_error(self, error, error_string: str = "") -> None:
        msg = str(error_string or error or "Unknown media player error")
        if msg:
            self.error.emit(msg)

    def _on_media_status_changed(self, status) -> None:
        try:
            if status == QMediaPlayer.EndOfMedia:
                self.playback_finished.emit()
        except Exception:
            pass

    def _clamp_index(self, index: int) -> int:
        idx = int(index)
        if self._frame_count > 0:
            return max(0, min(idx, self._frame_count - 1))
        return max(0, idx)

    def _close_still_decoder(self) -> None:
        if self._still_container is not None:
            try:
                self._still_container.close()
            except Exception:
                pass
        self._still_container = None; self._still_stream = None; self._still_last_index = None; self._still_next_hint = 0

    def _open_still_decoder(self) -> None:
        if self.video_path is None:
            raise RuntimeError("No video opened.")
        self._close_still_decoder()
        self._still_container = av.open(str(self.video_path))
        self._still_stream = self._still_container.streams.video[0]
        try:
            self._still_stream.thread_type = "AUTO"
        except Exception:
            pass
        self._still_last_index = None; self._still_next_hint = 0

    def _seek_still_decoder(self, target_index: int) -> None:
        if self.video_path is None:
            return
        if self._still_container is None or self._still_stream is None:
            self._open_still_decoder()
        fps = max(self._fps, 1.0)
        seek_time = max(0.0, self.frame_to_time(target_index) - 0.25)
        try:
            pts = int(seek_time / float(self._still_stream.time_base))
            self._still_container.seek(pts, any_frame=False, backward=True, stream=self._still_stream)
        except Exception:
            try:
                self._still_container.seek(int(seek_time * av.time_base), any_frame=False, backward=True)
            except Exception:
                self._open_still_decoder()
        self._still_last_index = None
        self._still_next_hint = self.time_to_frame(seek_time)

    def _decode_still_until(self, target_index: int) -> np.ndarray | None:
        if self._still_container is None or self._still_stream is None:
            self._open_still_decoder()
        fps = max(self._fps, 1.0)
        best_rgb = None; best_distance = 10**12; decoded_count = 0
        for frame in self._still_container.decode(self._still_stream):
            decoded_count += 1
            frame_time = None
            try:
                if frame.pts is not None and self._still_stream.time_base is not None:
                    frame_time = float(frame.pts * self._still_stream.time_base)
            except Exception:
                frame_time = None
            if frame_time is None:
                frame_idx = self._still_next_hint if self._still_last_index is None else self._still_last_index + 1
            else:
                frame_idx = self.time_to_frame(frame_time)
            if self._still_last_index is not None and frame_idx <= self._still_last_index:
                frame_idx = self._still_last_index + 1
            frame_idx = self._clamp_index(frame_idx)
            rgb = frame.to_ndarray(format="rgb24")
            if self.target_height > 0 and rgb.shape[0] > self.target_height:
                scale = self.target_height / rgb.shape[0]
                new_w = max(1, int(round(rgb.shape[1] * scale)))
                rgb = cv2.resize(rgb, (new_w, self.target_height), interpolation=cv2.INTER_AREA)
            if not rgb.flags["C_CONTIGUOUS"]:
                rgb = np.ascontiguousarray(rgb)
            self._still_rgb_cache[frame_idx] = rgb; self._still_rgb_cache.move_to_end(frame_idx)
            while len(self._still_rgb_cache) > self.STILL_CACHE_SIZE:
                self._still_rgb_cache.popitem(last=False)
            self._still_last_index = frame_idx; self._still_next_hint = frame_idx + 1
            dist = abs(frame_idx - target_index)
            if dist < best_distance:
                best_distance = dist; best_rgb = rgb
            if frame_idx >= target_index:
                return rgb
            if decoded_count > 500:
                break
        return best_rgb

    def _extract_still_rgb(self, index: int) -> np.ndarray | None:
        if self.video_path is None:
            return None
        idx = self._clamp_index(index)
        if idx in self._still_rgb_cache:
            self._still_rgb_cache.move_to_end(idx); return self._still_rgb_cache[idx]
        if self._still_last_index is not None and idx >= self._still_last_index and idx - self._still_last_index <= 120:
            rgb = self._decode_still_until(idx)
            if rgb is not None:
                return self._still_rgb_cache.get(idx, rgb)
        self._seek_still_decoder(idx)
        rgb = self._decode_still_until(idx)
        return self._still_rgb_cache.get(idx, rgb) if rgb is not None else None

    def get_frame_rgb(self, index: int) -> np.ndarray | None:
        return self._extract_still_rgb(index)

    def get_frame_array(self, index: int) -> np.ndarray | None:
        return self.get_frame_rgb(index)

    def get_frame_image(self, index: int) -> QImage | None:
        idx = self._clamp_index(index)
        rot = int(getattr(self, "sync_rotation", 0) or 0) % 360
        cache_key = (idx, rot)

        if cache_key in self._still_image_cache:
            self._still_image_cache.move_to_end(cache_key)
            return self._still_image_cache[cache_key]

        if self.is_bcap:
            if self._bcap_reader is None:
                return None
            image = self._bcap_reader.read_image(idx)
            if rot:
                transform = QTransform()
                transform.rotate(rot)
                image = image.transformed(transform)
            self._still_image_cache[cache_key] = image
            self._still_image_cache.move_to_end(cache_key)
            # Full-resolution BCAP JPEGs are large. Keep only a small rolling
            # window; retaining STILL_CACHE_SIZE images can exhaust RAM quickly.
            bcap_cache_limit = min(int(self.STILL_CACHE_SIZE), 8)
            while len(self._still_image_cache) > bcap_cache_limit:
                self._still_image_cache.popitem(last=False)
            return image

        rgb = self.get_frame_rgb(idx)
        if rgb is None:
            return None
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, int(rgb.strides[0]), QImage.Format_RGB888).copy()
        if rot:
            transform = QTransform(); transform.rotate(rot); image = image.transformed(transform)
        self._still_image_cache[cache_key] = image; self._still_image_cache.move_to_end(cache_key)
        while len(self._still_image_cache) > self.STILL_CACHE_SIZE:
            self._still_image_cache.popitem(last=False)
        return image

    def get_frame(self, index: int) -> QPixmap | None:
        idx = self._clamp_index(index)
        rot = int(getattr(self, "sync_rotation", 0) or 0) % 360
        key = (idx, rot)
        if key in self._still_pixmap_cache:
            self._still_pixmap_cache.move_to_end(key); return self._still_pixmap_cache[key]
        image = self.get_frame_image(idx)
        if image is None:
            return None
        pix = QPixmap.fromImage(image)
        self._still_pixmap_cache[key] = pix; self._still_pixmap_cache.move_to_end(key)
        while len(self._still_pixmap_cache) > self.STILL_CACHE_SIZE:
            self._still_pixmap_cache.popitem(last=False)
        return pix

    def create_graphics_item(self) -> QGraphicsPixmapItem:
        item = QGraphicsPixmapItem(); pix = self.get_frame(0)
        if pix is not None:
            item.setPixmap(pix)
        return item

    @property
    def fps(self) -> float: return float(self._fps)
    @property
    def orig_fps(self) -> float: return float(self._orig_fps)
    @property
    def skip(self) -> int: return int(self._skip)
    @property
    def frame_count(self) -> int: return int(self._frame_count)
    @property
    def duration_seconds(self) -> float: return float(self._duration_seconds)
    @property
    def source_size(self) -> tuple[int, int]: return int(self._source_width), int(self._source_height)
    @property
    def display_size(self) -> tuple[int, int]: return int(self._display_width), int(self._display_height)
    @property
    def is_ready(self) -> bool: return bool(self.video_path is not None and self._ready and self._fps > 0)



    def media_frame_to_display_frame(self, media_frame: int) -> int:
        """
        Convert QMediaPlayer timeline frame to the frame that appears visually
        in QVideoWidget.
        """
        lag = int(getattr(self, "native_display_lag_frames", 0) or 0)
        return self._clamp_index(int(media_frame) - lag)


    def display_frame_to_media_frame(self, display_frame: int) -> int:
        """
        Convert desired visible/displayed frame to the QMediaPlayer timeline frame
        we should seek to.
        """
        lag = int(getattr(self, "native_display_lag_frames", 0) or 0)
        return self._clamp_index(int(display_frame) + lag)



    def time_to_frame(self, time_seconds: float) -> int:
        target = float(time_seconds)
        values = self._frame_timestamps_seconds
        if values.size:
            index = int(np.searchsorted(values, target, side="left"))
            if index <= 0: return 0
            if index >= values.size: return int(values.size - 1)
            before, after = float(values[index - 1]), float(values[index])
            return index - 1 if abs(target - before) <= abs(after - target) else index
        if self._fps <= 0: return 0
        return self._clamp_index(int(round(target * self._fps)))

    def frame_to_time(self, frame_index: int) -> float:
        index = self._clamp_index(frame_index)
        values = self._frame_timestamps_seconds
        if values.size: return float(values[min(index, values.size - 1)])
        if self._fps <= 0: return 0.0
        return index / self._fps

    def update_cache_meta_rotation(self) -> None:
        if self._cache_dir is None:
            return
        meta = self._load_json(self._meta_file()) if self._meta_file().exists() else {}
        if not meta:
            try: meta = self._expected_meta()
            except Exception: meta = {}
        meta["rotation"] = int(getattr(self, "sync_rotation", 0) or 0)
        try: self._write_json(self._meta_file(), meta)
        except Exception: pass
        self._still_image_cache.clear(); self._still_pixmap_cache.clear()

    def clear_all_cache(self) -> None:
        if not self.cache_root.exists(): return
        for p in self.cache_root.iterdir():
            if p.is_dir():
                try:
                    for f in p.rglob("*"):
                        try: f.unlink()
                        except Exception: pass
                    p.rmdir()
                except Exception: pass

    def clear_current_video_cache(self) -> None:
        if self._cache_dir is None: return
        try:
            for f in self._cache_dir.rglob("*"):
                try: f.unlink()
                except Exception: pass
            self._cache_dir.rmdir()
        except Exception: pass
