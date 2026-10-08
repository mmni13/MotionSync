from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage


class BcapReader:
    """Random-access reader for BELLCAP1 files containing complete JPEG frames."""

    SIGNATURE = b"BELLCAP1"
    JPEG_START = b"\xff\xd8\xff"
    JPEG_END = b"\xff\xd9"
    CHUNK_SIZE = 8 * 1024 * 1024

    def __init__(self, path, *, fps=100.0, index_path=None):
        self.path = Path(path).resolve()
        self.fps = float(fps or 100.0)
        self.index_path = Path(index_path) if index_path else None
        self.offsets = np.array([], dtype=np.int64)
        self.lengths = np.array([], dtype=np.int64)
        self._stream = None

    @property
    def frame_count(self):
        return int(self.offsets.size)

    @property
    def duration_seconds(self):
        return self.frame_count / self.fps if self.fps > 0 else 0.0

    def open(self):
        with self.path.open("rb") as stream:
            if stream.read(8) != self.SIGNATURE:
                raise ValueError(f"Not a BELLCAP1 recording: {self.path.name}")
        if not self._load_index():
            self._build_index()
            self._save_index()
        self._stream = self.path.open("rb")

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def frame_to_time(self, frame_index):
        return max(0, int(frame_index)) / self.fps

    def time_to_frame(self, seconds):
        if self.frame_count <= 0:
            return 0
        return max(0, min(int(round(float(seconds) * self.fps)), self.frame_count - 1))

    def read_image(self, frame_index):
        if self._stream is None:
            self.open()
        index = max(0, min(int(frame_index), self.frame_count - 1))
        self._stream.seek(int(self.offsets[index]))
        payload = self._stream.read(int(self.lengths[index]))
        image = QImage.fromData(payload, "JPG")
        if image.isNull():
            raise ValueError(f"Could not decode BCAP frame {index}")
        return image

    def _identity(self):
        stat = self.path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _load_index(self):
        if self.index_path is None or not self.index_path.exists():
            return False
        try:
            with np.load(self.index_path, allow_pickle=False) as data:
                if json.loads(str(data["identity"].item())) != self._identity():
                    return False
                self.offsets = np.asarray(data["offsets"], dtype=np.int64)
                self.lengths = np.asarray(data["lengths"], dtype=np.int64)
            return self.frame_count > 0 and self.offsets.size == self.lengths.size
        except Exception:
            return False

    def _save_index(self):
        if self.index_path is None:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            self.index_path,
            identity=json.dumps(self._identity(), sort_keys=True),
            offsets=self.offsets,
            lengths=self.lengths,
        )

    def _find(self, pattern):
        offsets = []
        tail = b""
        absolute = 0
        overlap = len(pattern) - 1
        with self.path.open("rb") as stream:
            while True:
                chunk = stream.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                data = tail + chunk
                base = absolute - len(tail)
                cursor = 0
                while True:
                    position = data.find(pattern, cursor)
                    if position < 0:
                        break
                    offsets.append(base + position)
                    cursor = position + 1
                absolute += len(chunk)
                tail = data[-overlap:]
        return offsets

    def _build_index(self):
        starts = self._find(self.JPEG_START)
        ends = self._find(self.JPEG_END)
        offsets, lengths = [], []
        end_index = 0
        for index, start in enumerate(starts):
            while end_index < len(ends) and ends[end_index] < start:
                end_index += 1
            if end_index >= len(ends):
                break
            end = ends[end_index] + 2
            next_start = starts[index + 1] if index + 1 < len(starts) else self.path.stat().st_size
            if end <= next_start:
                offsets.append(start)
                lengths.append(end - start)
                end_index += 1
        if not offsets:
            raise ValueError(f"No complete JPEG frames found in {self.path.name}")
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.lengths = np.asarray(lengths, dtype=np.int64)
