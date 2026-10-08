from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


class SyncEngine:
    """
    Handles measurement data and synchronization between video frames
    and data time.

    Responsibilities:
    - Load Excel measurement data
    - Convert frame <-> data time using video offset
    - Convert x-axis value <-> time
    - Manage selected signal and x-axis mode

    Pure logic only:
    - no UI
    - no OpenCV
    - no video decoding
    """

    DATA_HZ = 200.0

    def __init__(self):
        self.data_path: Path | None = None

        self.time_data = np.array([], dtype=float)
        self.position_data = np.array([], dtype=float)

        self.signals: dict[int, tuple[str, np.ndarray]] = {}
        self._current_signal = 1
        self._x_mode = "time"

        # Frame in backend/cache video that corresponds to data time zero.
        self._base_offset_frames = 0   # original sync
        self._user_offset_frames = 0   # manual adjustment


    # =====================
    # DATA LOADING
    # =====================
    def load_data(self, data_path: Path | str) -> None:
        data_path = Path(data_path)
        if not data_path.exists():
            raise FileNotFoundError(f"Data file not found: {data_path}")

        df = pd.read_excel(data_path, engine="openpyxl")
        self.data_path = data_path

        required_columns = [
            "Sample Time [s]",
            "Sample Distance [m]",
            "Speed [m/s]",
            "Acceleration [m/s2]",
            "Force [N]",
            "Power [W]",
        ]

        missing = [col for col in required_columns if col not in df.columns]
        if missing:
            raise ValueError(
                "Excel data file is missing required columns:\n"
                + "\n".join(f"- {col}" for col in missing)
            )

        self.time_data = df["Sample Time [s]"].to_numpy(dtype=float)
        self.position_data = df["Sample Distance [m]"].to_numpy(dtype=float)

        self.signals = {
            1: ("Velocity [m/s]", df["Speed [m/s]"].to_numpy(dtype=float)),
            2: ("Acceleration [m/s²]", df["Acceleration [m/s2]"].to_numpy(dtype=float)),
            3: ("Force [N]", df["Force [N]"].to_numpy(dtype=float)),
            4: ("Power [W]", df["Power [W]"].to_numpy(dtype=float)),
        }

        self._current_signal = 1
        self._x_mode = "time"
        self._video_offset_frames = 0

    @property
    def has_data(self) -> bool:
        return self.time_data.size > 0 and bool(self.signals)

    # =====================
    # SIGNAL
    # =====================
    def set_signal(self, mode: int) -> None:
        if mode in self.signals:
            self._current_signal = int(mode)

    @property
    def current_signal(self):
        if not self.signals:
            return np.array([], dtype=float)
        return self.signals[self._current_signal][1]

    @property
    def current_signal_name(self) -> str:
        if not self.signals:
            return ""
        return self.signals[self._current_signal][0]

    # =====================
    # X AXIS
    # =====================
    def set_x_axis(self, mode: str) -> None:
        if mode in ("time", "position"):
            self._x_mode = mode

    @property
    def x_axis_data(self):
        if self._x_mode == "time":
            return self.time_data
        return self.position_data

    @property
    def x_axis_label(self) -> str:
        return "Time [s]" if self._x_mode == "time" else "Position [m]"

    @property
    def x_mode(self) -> str:
        return self._x_mode

    # =====================
    # SYNC LOGIC
    # =====================
    def frame_to_time(self, frame_index: int, fps: float) -> float:
        """
        Convert backend/cache video frame index to data time in seconds.
        """
        fps = float(fps or 0.0)
        if fps <= 0:
            return 0.0
        return (int(frame_index) - self.video_offset_frames) / fps

    def time_to_frame(self, time_seconds: float, fps: float) -> int:
        """
        Convert data time in seconds to backend/cache video frame index.
        """
        fps = float(fps or 0.0)
        if fps <= 0:
            return 0
        return int(round(float(time_seconds) * fps + self.video_offset_frames))

    def set_video_offset(self, frame_index: int) -> None:
        try:
            self._base_offset_frames = int(frame_index)
            self._user_offset_frames = 0
        except Exception:
            self._base_offset_frames = 0
            self._user_offset_frames = 0

    @property
    def video_offset_frames(self) -> int:
        return int(self._base_offset_frames + self._user_offset_frames)

    def x_value_to_time(self, x_value: float) -> float:
        """
        Convert current x-axis value to data time.
        """
        if not self.has_data:
            return 0.0

        if self._x_mode == "time":
            return float(x_value)

        idx = int(np.abs(self.position_data - float(x_value)).argmin())
        return float(self.time_data[idx])
    
    def adjust_offset(self, delta: int) -> None:
        self._user_offset_frames += int(delta)
    def reset_offset(self) -> None:
        self._user_offset_frames = 0

    @property
    def user_offset(self) -> int:
        return int(self._user_offset_frames)