from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from viewer.video_backend import VideoBackend


class CacheBuildWorker(QObject):
    """
    Worker for building the video frame cache.

    Responsibilities:
    - Calls VideoBackend.build_cache_sync()
    - Emits progress updates
    - Handles cancellation cleanly
    - Emits finished OR error (never both)
    """

    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, video_backend: VideoBackend):
        super().__init__()
        self.video_backend = video_backend

        self._cancel_requested: bool = False
        self._is_running: bool = False

    # ---------------- RUN ----------------

    @Slot()
    def run(self) -> None:
        if self._is_running:
            return  # prevent double-start

        self._is_running = True
        self._cancel_requested = False

        try:
            def emit_progress(value: int, message: str) -> None:
                # Fast cancel check (hot path)
                if self._cancel_requested:
                    raise RuntimeError("CACHE_BUILD_CANCELLED")

                # Clamp value for safety
                value = max(0, min(100, int(value)))

                self.progress.emit(value, str(message))

            # ---- BUILD CACHE ----

            self.video_backend.build_cache_sync(
                progress_callback=emit_progress
            )




            # If cancelled during final stage, avoid finished
            if self._cancel_requested:
                return

            self.finished.emit()

        except RuntimeError as exc:
            # Handle cancellation separately (clean exit, no error spam)
            if str(exc) == "CACHE_BUILD_CANCELLED":
                return

            msg = f"{exc}\n\n{traceback.format_exc()}"
            self.error.emit(msg)

        except Exception as exc:
            # Real error
            msg = f"{exc}\n\n{traceback.format_exc()}"
            self.error.emit(msg)

        finally:
            self._is_running = False

    # ---------------- CANCEL ----------------

    def cancel(self) -> None:
        """
        Request cancellation. Safe to call from UI thread.
        """
        self._cancel_requested = True
