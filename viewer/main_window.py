
from __future__ import annotations

from pathlib import Path
from typing import Any
import os

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QThread, QObject, QEvent, QSize
from PySide6.QtGui import QIcon, QKeyEvent, QShortcut, QKeySequence, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget, QComboBox, QToolButton, QScrollArea

from viewer.video_backend import VideoBackend
from viewer.drawing_overlay import VideoStackWidget
from viewer.sync import SyncEngine
from viewer.sync_detection import get_or_create_sync_frame, _read_json, _write_json
from viewer.motion_api import Motion1080Api
from viewer.run_library import RunIdentity, RunLibrary
from viewer.video_matcher import scan_video_folder, match_runs_to_multiple_videos
from viewer.contemplas_catalog import attach_manifest_package, cached_snapshot
from viewer.multi_camera import CameraThumbnailWidget, CameraView, ThumbnailDecoder, cache_dir_for_video
from viewer.api_workers import ApiFetchClientsWorker, ApiFetchSessionsWorker, ApiFetchSessionDetailsWorker, ApiFetchTrainingDataForSetWorker, ApiFetchAndCacheRunDataWorker


class VideoDataViewer(QWidget):
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".MP4", ".MOV", ".AVI", ".MKV", ".M4V"}
    EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".XLSX", ".XLSM"}

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setAttribute(Qt.WA_StyledBackground, True)
        # The main viewer owns all non-text keyboard shortcuts. This must be an
        # explicit focus target when the paused still-cover is hidden for native
        # QVideoWidget playback.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle("MotionSync")

        # MotionSync branding only. Missing icon files never prevent startup.
        icon_dir = Path(__file__).resolve().parent / "icons"
        for icon_name in ("motionsync.ico", "motionsync.png"):
            icon_path = icon_dir / icon_name
            if icon_path.is_file():
                app_icon = QIcon(str(icon_path))
                if not app_icon.isNull():
                    self.setWindowIcon(app_icon)
                    app = QApplication.instance()
                    if app is not None:
                        app.setWindowIcon(app_icon)
                break

        #self.resize(1450, 900)
        self.resize(3440, 1440)
        self.video = VideoBackend()
        self.sync = SyncEngine()
        self.run_library = RunLibrary()

        self.current_folder: Path | None = None
        self.current_video_path: Path | None = None
        self.current_data_path: Path | None = None

        self.playing = False
        self.pending_play_start = False
        self.playback_warmup = False
        self.playback_warmup_ms = 300
        self._playback_warmup_start_frame = 0

        self.frame_index = 0
        self.playback_speeds = [1.0, 0.5, 0.25, 0.1]
        self.playback_speed_index = 0
        self.playback_speed = 1.0

        self.marker_mode = False
        self._saved_selection_region = None
        self._vb_state = None
        self._held_step_key = None
        self._held_step_delta = 0

        self._api_thread: QThread | None = None
        self._api_worker: QObject | None = None
        self._api_finished_handler = None
        self._api_request_finishing = False


        self._auto_match_fetch_queue = []
        self._auto_match_fetch_total = 0
        self._auto_match_fetch_done = 0
        self.api_clients: list[dict[str, Any]] = []
        self.api_sessions: list[dict[str, Any]] = []
        self.api_sets: list[dict[str, Any]] = []
        self.api_runs: list[dict[str, Any]] = []
        self.selected_client: dict[str, Any] | None = None
        self.selected_session: dict[str, Any] | None = None
        self.selected_set: dict[str, Any] | None = None
        self.selected_run: dict[str, Any] | None = None
        self.selected_session_detail: dict[str, Any] | None = None

        # Multi-camera state. The existing VideoBackend remains the single active
        # full-resolution player; secondary views are lightweight thumbnails.
        self.current_run_identity: RunIdentity | None = None
        self.camera_views: list[CameraView] = []
        self.active_camera_id: str | None = None
        self.camera_thumbnail_widgets: dict[str, CameraThumbnailWidget] = {}
        self.camera_preview_images: dict[str, QImage] = {}
        self.camera_drawings: dict[str, list[dict]] = {}
        # Manual aftersync is a physical time correction shared by every camera
        # attached to the current run. Frame counts are derived per camera FPS.
        self.shared_sync_offset_seconds: float | None = None
        self.graph_sync_mode = False
        self.graph_sync_locked_frame: int | None = None
        self.graph_sync_previous_cursor = None
        # Set only when a camera switch must automatically resume playback.
        # QVideoWidget can become active after play() returns, so focus is
        # restored again at playback-start and after warm-up completion.
        self._restore_focus_after_camera_switch = False
        self._thumbnail_generation = 0
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_decoder = ThumbnailDecoder(self)
        self._thumbnail_decoder.frame_ready.connect(self._on_thumbnail_frame_ready)

        self._apply_app_style()

        self.open_folder_btn = self._make_top_tool_button("Open folder")
        self.open_folder_btn.clicked.connect(self.open_video_data_folder)
        self.clear_cache_btn = self._make_top_tool_button("Clear cache")
        self.clear_cache_btn.clicked.connect(self._clear_cache_clicked)
        self.help_btn = QPushButton("Help")
        self.help_btn.clicked.connect(self._show_keyboard_shortcuts)
        self.draw_line_btn = self._make_top_tool_button("Line")
        self.draw_line_btn.clicked.connect(self._select_draw_line)
        self.draw_angle_btn = self._make_top_tool_button("Angle")
        self.draw_angle_btn.clicked.connect(self._select_draw_angle)
        self.clear_draw_btn = self._make_top_tool_button("Clear drawings")
        self.clear_draw_btn.clicked.connect(self._clear_drawings)
        self.play_btn = self._make_top_tool_button("Play")
        self.play_btn.clicked.connect(self._toggle_play_pause_shortcut)
        self.play_btn.setEnabled(False)
        self.speed_btn = self._make_top_tool_button("Speed: x1")
        self.speed_btn.clicked.connect(self.cycle_playback_speed)
        self.speed_btn.setEnabled(False)
        self.region_btn = self._make_top_tool_button("Region")
        self.region_btn.clicked.connect(self._toggle_marker_mode)
        self.rotate_btn = self._make_top_tool_button("Rotate")
        self.rotate_btn.clicked.connect(self.rotate_video_90)
        self.graph_sync_btn = self._make_top_tool_button("Sync mode")
        self.sync_back_btn = self._make_top_tool_button("Sync -")
        self.sync_forward_btn = self._make_top_tool_button("Sync +")
        self.save_sync_btn = self._make_top_tool_button("Save sync")
        self.sync_reset_btn = self._make_top_tool_button("Reset sync")
        self.sync_back_btn.clicked.connect(lambda: self._sync_button_nudge(-1))
        self.sync_forward_btn.clicked.connect(lambda: self._sync_button_nudge(+1))
        self.sync_reset_btn.clicked.connect(self._reset_sync)
        self.graph_sync_btn.clicked.connect(self._toggle_graph_sync_mode)
        self.save_sync_btn.clicked.connect(self._save_sync_offset)
        self.sync_back_btn.setEnabled(False)
        self.sync_forward_btn.setEnabled(False)
        self.sync_reset_btn.setEnabled(False)
        self.save_sync_btn.setEnabled(False)
        self._set_toolbar_icons()


        self.status_label = QLabel("Select a folder or choose a 1080 run from the sidebar.")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.status_label.setWordWrap(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self._configure_buttons()

        top = QHBoxLayout()
        top.setContentsMargins(10, 7, 10, 6)
        top.setSpacing(6)

        brand_label = QLabel()
        brand_label.setObjectName("brandLogo")
        brand_logo_path = icon_dir / "motionsync_logo.png"
        if brand_logo_path.is_file():
            brand_pixmap = QPixmap(str(brand_logo_path))
            if not brand_pixmap.isNull():
                brand_label.setPixmap(brand_pixmap.scaledToHeight(60, Qt.SmoothTransformation))
        if brand_label.pixmap() is None or brand_label.pixmap().isNull():
            brand_label.setText("Motion<span>Sync</span>")
            brand_label.setObjectName("brandLabel")
            brand_label.setTextFormat(Qt.RichText)
        brand_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        brand_label.setFixedWidth(244)
        top.addWidget(brand_label)

        for group in ([self.open_folder_btn, self.clear_cache_btn], [self.draw_line_btn, self.draw_angle_btn, self.clear_draw_btn], [self.play_btn, self.speed_btn], [self.region_btn, self.rotate_btn]):
            top.addWidget(self._make_toolbar_group(group))
        top.addWidget(self.status_label, stretch=1)
        top.addWidget(self._make_toolbar_group([
            self.graph_sync_btn,
            self.sync_back_btn,
            self.sync_forward_btn,
            self.sync_reset_btn,
            self.save_sync_btn,
        ]))

        self.sidebar = self._build_1080_sidebar()

        self.video_panel = VideoStackWidget(self.video.create_video_widget())
        self.video_panel.setObjectName("videoView")
        self.video_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_panel.setFocusPolicy(Qt.NoFocus)
        try:
            self.video.video_widget.setFocusProxy(self)
        except Exception:
            pass
        self.drawing = self.video_panel.overlay
        self.drawing.on_mode_finished = self._on_draw_finished
        self.video_panel.space_pressed.connect(self._toggle_play_pause_shortcut)
        
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)


        # Make Space always toggle play/pause while the main window has focus.
        # This avoids relying on which child widget/button currently has keyboard focus.
        self.space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.space_shortcut.setContext(Qt.WindowShortcut)
        self.space_shortcut.activated.connect(self._toggle_play_pause_shortcut)

        # If the paused still-cover is a separate top-level window, give it the same
        # Space shortcut as well, so Space also works while drawing/paused overlay has focus.
        try:
            self.space_shortcut_cover = QShortcut(
                QKeySequence(Qt.Key_Space),
                self.video_panel.still_cover,
            )
            self.space_shortcut_cover.setContext(Qt.WindowShortcut)
            self.space_shortcut_cover.activated.connect(self._toggle_play_pause_shortcut)
        except Exception:
            self.space_shortcut_cover = None



        self.video.frame_changed.connect(self._on_video_frame_changed)
        self.video.playback_started.connect(self._on_playback_started)
        self.video.playback_finished.connect(self._on_video_playback_finished)
        self.video.error.connect(lambda m: self.status_label.setText(str(m)))

        self.graph_help_btn = QPushButton("?")
        self.graph_help_btn.setFixedSize(28, 28)
        self.graph_help_btn.setFocusPolicy(Qt.NoFocus)
        self.graph_help_btn.setToolTip("Keyboard shortcuts (F1)")
        self.graph_help_btn.clicked.connect(self._show_keyboard_shortcuts)


        # --------------------------------------------------
        # Graph axis dropdowns
        # --------------------------------------------------

        self.y_axis_combo = QComboBox()
        self.y_axis_combo.setObjectName("axisCombo")
        self.y_axis_combo.setEditable(False)
        self.y_axis_combo.setFocusPolicy(Qt.NoFocus)
        self.y_axis_combo.setMinimumWidth(145)

        self.y_axis_combo.addItem("Speed", 1)
        self.y_axis_combo.addItem("Acceleration", 2)
        self.y_axis_combo.addItem("Force", 3)
        self.y_axis_combo.addItem("Power", 4)


        self.x_axis_combo = QComboBox()
        self.x_axis_combo.setObjectName("axisCombo")
        self.x_axis_combo.setEditable(False)
        self.x_axis_combo.setFocusPolicy(Qt.NoFocus)
        self.x_axis_combo.setMinimumWidth(145)

        self.x_axis_combo.addItem("Time", "time")
        self.x_axis_combo.addItem("Position", "position")


        self.y_axis_combo.currentIndexChanged.connect(
            self._on_y_axis_combo_changed
        )

        self.x_axis_combo.currentIndexChanged.connect(
            self._on_x_axis_combo_changed
        )



        self.plot = pg.PlotWidget()
        self.plot.setBackground("#07111f")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        axis_pen = pg.mkPen(color="#54718f")
        text_pen = pg.mkPen(color="#b9d2ec")
        self.plot.getAxis("bottom").setPen(axis_pen)
        self.plot.getAxis("left").setPen(axis_pen)
        self.plot.getAxis("bottom").setTextPen(text_pen)
        self.plot.getAxis("left").setTextPen(text_pen)
        self.plot_item = self.plot.plot([], [], pen=pg.mkPen(color="#22c7f2", width=2))
        self.plot.setLabel("bottom", "Time [s]")
        self.plot.setLabel("left", "")
        self.marker = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(color="#f59e0b", width=2))
        self.plot.addItem(self.marker)
        self.plot.scene().sigMouseClicked.connect(self.on_plot_click)

        self.selection_region = pg.LinearRegionItem(orientation="vertical", movable=False, brush=(29, 155, 240, 55))
        self.selection_region.setVisible(False)
        self.plot.addItem(self.selection_region)
        self.selection_region.sigRegionChanged.connect(self._update_selection_stats)
        self.selection_label = QLabel(self.plot)
        self.selection_label.setVisible(False)
        self.selection_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.selection_label.setStyleSheet("background:rgba(13,27,45,242);border:1px solid #38bdf8;border-radius:8px;padding:8px;color:#e6f4ff;font-size:12px;")
        self.plot.getViewBox().sigRangeChanged.connect(lambda *_: self._position_selection_text())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addLayout(top)
        pw = QVBoxLayout()
        pw.setContentsMargins(12, 0, 12, 8)
        pw.addWidget(self.progress_bar)
        root.addLayout(pw)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        self.video_area = QWidget()
        self.video_area.setObjectName("multiCameraArea")
        video_area_layout = QHBoxLayout(self.video_area)
        video_area_layout.setContentsMargins(0, 0, 0, 0)
        video_area_layout.setSpacing(8)
        video_area_layout.addWidget(self.video_panel, stretch=1)

        self.camera_strip_scroll = QScrollArea()
        self.camera_strip_scroll.setObjectName("cameraStrip")
        self.camera_strip_scroll.setWidgetResizable(True)
        self.camera_strip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.camera_strip_scroll.setMinimumWidth(190)
        self.camera_strip_scroll.setMaximumWidth(250)
        self.camera_strip_container = QWidget()
        self.camera_strip_container.setObjectName("cameraStripContainer")
        self.camera_strip_layout = QVBoxLayout(self.camera_strip_container)
        self.camera_strip_layout.setContentsMargins(4, 4, 4, 4)
        self.camera_strip_layout.setSpacing(7)
        self.camera_strip_layout.addStretch(1)
        self.camera_strip_scroll.setWidget(self.camera_strip_container)
        self.camera_strip_scroll.hide()
        video_area_layout.addWidget(self.camera_strip_scroll)

        rl.addWidget(self.video_area, stretch=3)

        # --------------------------------------------------
        # Horizontal graph header
        # Data analysis [Y axis] over [X axis]          [?]
        # --------------------------------------------------

        graph_header_widget = QWidget()
        graph_header_widget.setObjectName("graphHeader")

        graph_header = QHBoxLayout(graph_header_widget)
        graph_header.setContentsMargins(8, 4, 4, 4)
        graph_header.setSpacing(8)

        graph_title = QLabel("Data analysis")
        graph_title.setObjectName("graphTitle")

        axis_over_label = QLabel("over")
        axis_over_label.setObjectName("axisOverLabel")
        axis_over_label.setAlignment(Qt.AlignCenter)

        # Keep the controls compact instead of expanding across the header.
        self.y_axis_combo.setMinimumWidth(130)
        self.y_axis_combo.setMaximumWidth(180)

        self.x_axis_combo.setMinimumWidth(115)
        self.x_axis_combo.setMaximumWidth(160)

        graph_header.addWidget(graph_title)
        graph_header.addWidget(self.y_axis_combo)
        graph_header.addWidget(axis_over_label)
        graph_header.addWidget(self.x_axis_combo)

        # Push the Help button to the far right.
        graph_header.addStretch(1)
        graph_header.addWidget(
            self.graph_help_btn,
            alignment=Qt.AlignVCenter,
        )

        rl.addWidget(graph_header_widget)
        rl.addWidget(self.plot, stretch=1)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(right)
        splitter.setSizes([330, 1080])
        splitter.setStretchFactor(1, 1)
        cl = QVBoxLayout()
        cl.setContentsMargins(12, 0, 12, 12)
        cl.addWidget(splitter)
        root.addLayout(cl, stretch=1)


        self.player_park_timer = QTimer(self)
        self.player_park_timer.setSingleShot(True)
        self.player_park_timer.setInterval(80)
        self.player_park_timer.timeout.connect(self._park_player_on_current_frame)


        self.marker_timer = QTimer(self)
        self.marker_timer.timeout.connect(self._update_marker)
        self.marker_timer.start(33)
        # Thumbnail previews update only after paused navigation settles.
        # No secondary video decoding is allowed during active playback.
        self.thumbnail_update_timer = QTimer(self)
        self.thumbnail_update_timer.setSingleShot(True)
        self.thumbnail_update_timer.setInterval(220)
        self.thumbnail_update_timer.timeout.connect(
            self._update_camera_thumbnails
        )
        self.key_hold_delay_timer = QTimer(self)
        self.key_hold_delay_timer.setSingleShot(True)
        self.key_hold_delay_timer.setInterval(220)
        self.key_hold_delay_timer.timeout.connect(self._begin_held_frame_step)
        self.key_step_timer = QTimer(self)
        self.key_step_timer.setInterval(55)
        self.key_step_timer.timeout.connect(self._held_frame_step_tick)

        self._update_run_action_buttons()
        QTimer.singleShot(0, self._startup_api_init)

    def _apply_app_style(self):
        arrow_icon = (
            Path(__file__).resolve().parent
            / "icons"
            / "chevron_down.svg"
        ).as_posix()

        self.setStyleSheet(f"""
            QWidget#root {{
                background: #07111f;
                color: #dbeafe;
                font-size: 13px;
                font-family: "Segoe UI";
            }}

            QLabel#brandLabel {{
                color: #f8fafc;
                font-size: 23px;
                font-weight: 700;
                padding: 0 8px 0 2px;
                background: transparent;
            }}

            QLabel#brandLabel span {{
                color: #1d9bf0;
            }}

            QFrame#toolbarGroup,
            QFrame#sidebarPanel {{
                background: #0d1b2d;
                border: 1px solid #203754;
                border-radius: 10px;
            }}

            QFrame#sidebarPanel {{
                background: #0a1727;
            }}

            QPushButton {{
                background: #12243a;
                color: #dbeafe;
                border: 1px solid #294767;
                border-radius: 7px;
                padding: 6px 11px;
                font-weight: 600;
            }}

            QPushButton:hover {{
                background: #17314d;
                border-color: #38bdf8;
                color: #ffffff;
            }}

            QPushButton:pressed {{
                background: #0b75c9;
                border-color: #7dd3fc;
            }}

            QPushButton:disabled {{
                background: #0c1928;
                color: #52677f;
                border-color: #1b2d42;
            }}

            QPushButton[active="true"] {{
                background: #087fdb;
                color: white;
                border-color: #38bdf8;
            }}

            QLabel#statusLabel {{
                background: #0d1b2d;
                color: #b9d2ec;
                border: 1px solid #203754;
                border-radius: 10px;
                padding: 7px 10px;
            }}

            QLabel#sidebarHint {{
                color: #89a5c1;
                font-size: 12px;
            }}

            QWidget#videoView {{
                background: #030914;
                border: 1px solid #244663;
                border-radius: 10px;
            }}

            QListWidget {{
                background: #081422;
                color: #cfe4f8;
                border: 1px solid #203754;
                border-radius: 8px;
                padding: 4px;
                outline: none;
            }}

            QListWidget::item {{
                padding: 7px;
                border-radius: 6px;
                margin: 1px;
            }}

            QListWidget::item:hover {{
                background: #102b45;
            }}

            QListWidget::item:selected {{
                background: #0b75c9;
                color: #ffffff;
            }}

            QProgressBar {{
                background: #0a1727;
                color: #dbeafe;
                border: 1px solid #203754;
                border-radius: 6px;
                text-align: center;
                min-height: 12px;
            }}

            QProgressBar::chunk {{
                background: #1699e8;
                border-radius: 5px;
            }}

            QWidget#graphHeader,
            QWidget#graphControls {{
                background: transparent;
            }}

            QLabel#graphTitle {{
                color: #cfe8ff;
                font-size: 12px;
                font-weight: 700;
            }}

            QLabel#graphControlsLabel {{
                color: #6e8aa6;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
            }}

            QLabel#axisOverLabel {{
                color: #7f9db9;
                font-size: 14px;
                padding-left: 1px;
                padding-right: 1px;
            }}

            QComboBox#axisCombo {{
                background: #0d1b2d;
                color: #e6f4ff;
                border: 1px solid #2b4a68;
                border-radius: 6px;
                padding: 6px 34px 6px 10px;
                min-width: 120px;
                min-height: 22px;
                font-size: 13px;
            }}

            QComboBox#axisCombo:hover {{
                background: #102740;
                border-color: #38bdf8;
            }}

            QComboBox#axisCombo:on {{
                border-color: #38bdf8;
                background: #102740;
            }}

            QComboBox#axisCombo::drop-down {{
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 30px;
                border: none;
                background: transparent;
            }}

            QComboBox#axisCombo::down-arrow {{
                image: url("{arrow_icon}");
                width: 12px;
                height: 8px;
            }}

            QComboBox#axisCombo QAbstractItemView {{
                background: #0d1b2d;
                color: #e6f4ff;
                border: 1px solid #2b4a68;
                border-radius: 4px;
                padding: 3px;
                outline: none;
                selection-background-color: #087fdb;
                selection-color: #ffffff;
            }}

            QSplitter::handle {{
                background: #13283f;
            }}

            QSplitter::handle:hover {{
                background: #1d9bf0;
            }}

            QScrollBar:vertical {{
                background: #081422;
                width: 10px;
                margin: 0;
            }}

            QScrollBar::handle:vertical {{
                background: #294767;
                border-radius: 5px;
                min-height: 24px;
            }}

            QScrollBar::handle:vertical:hover {{
                background: #1d9bf0;
            }}

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
                height: 0;
            }}


            QScrollArea#cameraStrip {{
                background: #081422;
                border: 1px solid #203754;
                border-radius: 9px;
            }}
            QWidget#cameraStripContainer {{
                background: #081422;
            }}
            QFrame#cameraThumbnail {{
                background: #0d1b2d;
                border: 1px solid #294767;
                border-radius: 8px;
            }}
            QFrame#cameraThumbnail:hover {{
                border-color: #38bdf8;
                background: #102740;
            }}
            QFrame#cameraThumbnail[active="true"] {{
                border: 2px solid #22c7f2;
                background: #102740;
            }}
            QLabel#cameraThumbnailImage {{
                background: #030914;
                color: #6e8aa6;
                border-radius: 5px;
            }}
            QLabel#cameraThumbnailTitle {{
                color: #e6f4ff;
                font-weight: 700;
            }}
            QLabel#cameraThumbnailStatus {{
                color: #89a5c1;
                font-size: 10px;
            }}
            QToolTip {{
                background: #102740;
                color: #f8fafc;
                border: 1px solid #38bdf8;
                padding: 5px;
            }}
        """)

    def _make_top_tool_button(self, text):
        button = QToolButton(self)
        button.setText(str(text))
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(22, 22))
        button.setMinimumSize(62, 52)
        button.setAutoRaise(False)
        button.setObjectName("topToolButton")
        button.setStyleSheet("""
            QToolButton#topToolButton {
                background-color: #0b1b2e;
                color: #dbeafe;
                border: 1px solid #1e3a56;
                border-radius: 9px;
                padding: 5px 8px 4px 8px;
                font-family: "Segoe UI";
                font-size: 10px;
                font-weight: 600;
            }
            QToolButton#topToolButton:hover {
                background-color: #102a43;
                border-color: #22c7f2;
                color: #ffffff;
            }
            QToolButton#topToolButton:pressed,
            QToolButton#topToolButton[active="true"] {
                background-color: #0b75c9;
                border-color: #67d9f5;
                color: #ffffff;
            }
            QToolButton#topToolButton:disabled {
                background-color: #091523;
                color: #52677f;
                border-color: #172a3d;
            }
        """)
        return button

    def _set_button_icon(self, button, filename):
        path = Path(__file__).resolve().parent / "icons" / filename
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                button.setIcon(icon)

    def _set_toolbar_icons(self):
        icon_map = {
            self.open_folder_btn: "folder.svg",
            self.clear_cache_btn: "trash.svg",
            self.draw_line_btn: "line.svg",
            self.draw_angle_btn: "angle.svg",
            self.clear_draw_btn: "eraser.svg",
            self.play_btn: "play.svg",
            self.speed_btn: "speed.svg",
            self.region_btn: "region.svg",
            self.rotate_btn: "rotate.svg",
            self.sync_back_btn: "sync_back.svg",
            self.sync_forward_btn: "sync_forward.svg",
            self.sync_reset_btn: "reset.svg",
            self.save_sync_btn: "save.svg",
            self.graph_sync_btn: "Sync_mode.svg",
        }
        for button, filename in icon_map.items():
            self._set_button_icon(button, filename)

    def _make_toolbar_group(self, widgets):
        f = QFrame()
        f.setObjectName("toolbarGroup")
        f.setAttribute(Qt.WA_StyledBackground, True)
        f.setStyleSheet("QFrame#toolbarGroup { background-color: transparent; border: none; border-left: 1px solid #172b40; }")
        l = QHBoxLayout(f)
        l.setContentsMargins(9, 2, 3, 2)
        l.setSpacing(6)
        for w in widgets:
            l.addWidget(w)
        return f

    def _configure_buttons(self):
        for b in [self.open_folder_btn, self.clear_cache_btn, self.draw_line_btn, self.draw_angle_btn, self.clear_draw_btn, self.play_btn, self.speed_btn, self.region_btn, self.rotate_btn, self.sync_back_btn, self.sync_forward_btn, self.sync_reset_btn, self.graph_sync_btn, self.save_sync_btn]:
            b.setFocusPolicy(Qt.NoFocus)
            b.setProperty("active", False)

    def _set_button_active(self, b, a):
        b.setProperty("active", bool(a))
        b.style().unpolish(b)
        b.style().polish(b)
        b.update()

    def _set_tool_active(self, active):
        for b in [self.draw_line_btn, self.draw_angle_btn]:
            self._set_button_active(b, b is active)

    def _section(self, t):
        lab = QLabel(t)
        lab.setStyleSheet("font-size:11px;font-weight:700;color:#52606d;")
        return lab

    def _build_1080_sidebar(self):
        p = QFrame()
        p.setObjectName("sidebarPanel")
        p.setMinimumWidth(300)
        p.setMaximumWidth(420)
        l = QVBoxLayout(p)
        l.setContentsMargins(10, 10, 10, 10)
        l.setSpacing(8)
        title = QLabel("1080 Motion")
        title.setStyleSheet("font-size:17px;font-weight:700; color:#fcfcfc;")
        self.refresh_api_btn = QPushButton("Refresh API")
        self.refresh_api_btn.clicked.connect(self._refresh_api_clicked)
        self.change_api_key_btn = QPushButton("API key")
        self.change_api_key_btn.clicked.connect(self._change_api_key_clicked)
        row = QHBoxLayout()
        row.addWidget(title, stretch=1)
        row.addWidget(self.refresh_api_btn)
        row.addWidget(self.change_api_key_btn)
        l.addLayout(row)
        self.api_status_label = QLabel("Select a client to begin.")
        self.api_status_label.setObjectName("sidebarHint")
        self.api_status_label.setWordWrap(True)
        l.addWidget(self.api_status_label)
        self.clients_list = QListWidget()
        self.sessions_list = QListWidget()
        self.sets_list = QListWidget()
        self.runs_list = QListWidget()
        self.clients_list.currentItemChanged.connect(self._on_client_item_changed)
        self.sessions_list.currentItemChanged.connect(self._on_session_item_changed)
        self.sets_list.currentItemChanged.connect(self._on_set_item_changed)
        self.runs_list.currentItemChanged.connect(self._on_run_item_changed)
        self.runs_list.itemDoubleClicked.connect(lambda *_: self._load_selected_run())
        for name, w in [("Clients", self.clients_list), ("Sessions", self.sessions_list), ("Sets", self.sets_list), ("Runs", self.runs_list)]:
            l.addWidget(self._section(name))
            l.addWidget(w, stretch=1)
        self.attach_video_btn = QPushButton("Attach video")
        self.attach_video_btn.clicked.connect(self._attach_video_and_fetch_data)
        self.add_angle_btn = QPushButton("Add camera angle")
        self.add_angle_btn.clicked.connect(self._add_camera_angle)
        self.auto_match_video_btn = QPushButton("Auto-match videos")
        self.auto_match_video_btn.clicked.connect(self._auto_match_video_folder)
        self.load_run_btn = QPushButton("Load run")
        self.load_run_btn.clicked.connect(self._load_selected_run)
        r1 = QHBoxLayout()
        r1.addWidget(self.attach_video_btn)
        r1.addWidget(self.add_angle_btn)
        r1.addWidget(self.auto_match_video_btn)
        r2 = QHBoxLayout()
        r2.addWidget(self.load_run_btn)
        l.addLayout(r1)
        l.addLayout(r2)
        self.run_details_label = QLabel("No run selected.")
        self.run_details_label.setObjectName("sidebarHint")
        self.run_details_label.setWordWrap(True)
        l.addWidget(self.run_details_label)
        return p

    def _sync_video_panel_aspect(self):
        try:
            width, height = self.video.source_size
            rotation = int(getattr(self.video, "sync_rotation", 0) or 0)
            self.video_panel.set_video_aspect(width, height, rotation)
        except Exception:
            pass

    def _startup_api_init(self):
        if self._ensure_1080_api_key(show_cancel_warning=False):
            self._refresh_api_clicked()

    def _ensure_1080_api_key(self, show_cancel_warning=True):

        cover_was_visible = self._hide_still_cover()
        try:
            if Motion1080Api.has_saved_api_key():
                return True
        except Exception as e:
            QMessageBox.warning(self, "1080 API config", str(e))

    
        
        try:
            key, ok = QInputDialog.getText(
                self,
                "1080 API Key",
                "Enter your 1080 Motion API key:<br>" \
                "You can find it in your 1080 Motion account settings on the 1080 motion webapp<br>"
                "Go to <a href='https://webapp.1080motion.com/Admin/ApiKeys'>https://webapp.1080motion.com/Admin/ApiKeys</a> and copy the MotionSync API key.",
                QLineEdit.Password,
                flags=Qt.Dialog | Qt.WindowStaysOnTopHint
            )
        finally:
            self._restore_still_cover(cover_was_visible)

        if not ok or not key.strip():
            return False
        Motion1080Api.save_api_key_to_config(key.strip())
        return True

    def _change_api_key_clicked(self):
        key, ok = QInputDialog.getText(
            self, 
            "Change API key", 
            "Enter new 1080 Motion API key:<br>"
            "You can find it in your 1080 Motion account settings on the 1080 motion webapp<br>"
            "Go to <a href='https://webapp.1080motion.com/Admin/ApiKeys'>https://webapp.1080motion.com/Admin/ApiKeys</a> and copy the MotionSync API key."
                , QLineEdit.Password, flags=Qt.Dialog | Qt.WindowStaysOnTopHint)
        if ok and key.strip():
            Motion1080Api.save_api_key_to_config(key.strip())
            self._refresh_api_clicked()

    def _set_api_navigation_enabled(self, enabled):
        """Enable or disable API navigation while one request is active."""
        enabled = bool(enabled)

        for name in (
            "clients_list",
            "sessions_list",
            "sets_list",
            "runs_list",
            "refresh_api_btn",
            "change_api_key_btn",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(enabled)

    def _start_api_worker(self, worker, finished_handler):
        """Run exactly one API worker at a time.

        Navigation controls are disabled while the request is active, which
        prevents client/session/set state from changing underneath a response.
        Result handling always completes before the worker thread is stopped.
        """
        if self._api_thread is not None:
            # This should only be reachable from non-navigation background work.
            # Do not start a second request or replace the active handler.
            try:
                worker.deleteLater()
            except Exception:
                pass
            return False

        thread = QThread(self)
        self._api_thread = thread
        self._api_worker = worker
        self._api_finished_handler = finished_handler
        self._api_request_finishing = False
        self._set_api_navigation_enabled(False)

        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        if hasattr(worker, "progress"):
            worker.progress.connect(
                self._on_api_progress,
                Qt.QueuedConnection,
            )

        if hasattr(worker, "error"):
            worker.error.connect(
                self._on_active_api_worker_error,
                Qt.QueuedConnection,
            )

        if hasattr(worker, "finished"):
            worker.finished.connect(
                self._on_active_api_worker_finished,
                Qt.QueuedConnection,
            )

        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_active_api_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return True

    def _on_api_progress(self, message):
        message = str(message)

        try:
            self.api_status_label.setText(message)
        except Exception:
            pass

        try:
            self.status_label.setText(message)
        except Exception:
            pass

    def _on_active_api_worker_finished(self, payload):
        """Apply the payload before allowing the active thread to shut down."""
        if self._api_request_finishing:
            return

        self._api_request_finishing = True
        handler = self._api_finished_handler
        thread = self._api_thread

        try:
            if callable(handler):
                handler(payload)
        except Exception as exc:
            self._show_error(
                "1080 API finished handler failed",
                str(exc),
            )
            self._refresh_current_run_visuals()
        finally:
            if thread is not None:
                thread.quit()

    def _on_active_api_worker_error(self, message):
        """Show the active request error, then cleanly stop its thread."""
        if self._api_request_finishing:
            return

        self._api_request_finishing = True
        thread = self._api_thread

        try:
            self._on_api_error(message)
        finally:
            if thread is not None:
                thread.quit()

    def _on_api_error(self, message):
        message = str(message)

        try:
            self.api_status_label.setText("API operation failed.")
        except Exception:
            pass

        try:
            self.status_label.setText("1080 API operation failed.")
        except Exception:
            pass

        self._show_error(
            "1080 API error",
            message,
        )

    def _on_api_finished(self, payload):
        """Compatibility wrapper for code that invokes this method directly."""
        handler = self._api_finished_handler
        if callable(handler):
            handler(payload)

    def _on_active_api_thread_finished(self):
        """Clean up only after payload/error processing has completed."""
        self._api_thread = None
        self._api_worker = None
        self._api_finished_handler = None
        self._api_request_finishing = False
        self._set_api_navigation_enabled(True)

    def _clear_api_thread_refs(self):
        """Compatibility cleanup method."""
        self._api_thread = None
        self._api_worker = None
        self._api_finished_handler = None
        self._api_request_finishing = False
        self._set_api_navigation_enabled(True)

    def _refresh_api_clicked(self):
        if self._api_thread is not None:
            return

        if not self._ensure_1080_api_key():
            return

        for widget in (
            self.clients_list,
            self.sessions_list,
            self.sets_list,
            self.runs_list,
        ):
            widget.blockSignals(True)

        try:
            self.clients_list.clear()
            self.sessions_list.clear()
            self.sets_list.clear()
            self.runs_list.clear()
        finally:
            for widget in (
                self.clients_list,
                self.sessions_list,
                self.sets_list,
                self.runs_list,
            ):
                widget.blockSignals(False)

        self.api_clients = []
        self.api_sessions = []
        self.api_sets = []
        self.api_runs = []
        self.selected_client = None
        self.selected_session = None
        self.selected_session_detail = None
        self.selected_set = None
        self.selected_run = None
        self._update_run_action_buttons()
        self._update_run_details_label()
        self.api_status_label.setText("Loading clients...")

        self._start_api_worker(
            ApiFetchClientsWorker(),
            self._on_clients_loaded,
        )

    def _on_clients_loaded(self, clients):
        self.api_clients = list(clients or [])
        self.clients_list.blockSignals(True)

        try:
            self.clients_list.clear()
            for client in self.api_clients:
                item = QListWidgetItem(
                    client.get("display_name")
                    or client.get("id")
                    or "Client"
                )
                item.setData(Qt.UserRole, client)
                self.clients_list.addItem(item)
        finally:
            self.clients_list.blockSignals(False)

        self.api_status_label.setText(
            f"Loaded {len(self.api_clients)} clients. Select a client."
        )

    def _on_client_item_changed(self, cur, prev):
        if self._api_thread is not None:
            return

        # Avoid generating lower-level selection events while clearing.
        self.sessions_list.blockSignals(True)
        self.sets_list.blockSignals(True)
        self.runs_list.blockSignals(True)

        try:
            self.sessions_list.clear()
            self.sets_list.clear()
            self.runs_list.clear()
        finally:
            self.sessions_list.blockSignals(False)
            self.sets_list.blockSignals(False)
            self.runs_list.blockSignals(False)

        self.api_sessions = []
        self.api_sets = []
        self.api_runs = []

        self.selected_client = (
            cur.data(Qt.UserRole)
            if cur is not None
            else None
        )

        self.selected_session = None
        self.selected_session_detail = None
        self.selected_set = None
        self.selected_run = None

        self._update_run_action_buttons()
        self._update_run_details_label()

        if self.selected_client is None:
            self.api_status_label.setText(
                "Select a client to begin."
            )
            return

        client_id = str(
            self.selected_client.get("id") or ""
        )

        if not client_id:
            self.api_status_label.setText(
                "The selected client has no ID."
            )
            return

        self.api_status_label.setText(
            "Loading sessions..."
        )

        self._start_api_worker(
            ApiFetchSessionsWorker(
                client_id=client_id,
                max_age_days=-1,
            ),
            self._on_sessions_loaded,
        )

    def _on_sessions_loaded(self, sessions):
        self.api_sessions = list(sessions or [])

        self.sessions_list.blockSignals(True)

        try:
            self.sessions_list.clear()

            for session in self.api_sessions:
                session_date = str(
                    session.get("date") or ""
                )[:10]

                session_name = str(
                    session.get("display_name")
                    or session.get("id")
                    or "Session"
                )

                text = " | ".join(
                    value
                    for value in (
                        session_date,
                        session_name,
                    )
                    if value
                )

                item = QListWidgetItem(text)
                item.setData(
                    Qt.UserRole,
                    session,
                )
                self.sessions_list.addItem(item)

        finally:
            self.sessions_list.blockSignals(False)

        self.api_status_label.setText(
            f"Loaded {len(self.api_sessions)} sessions."
        )

    def _on_session_item_changed(self, cur, prev):
        if self._api_thread is not None:
            return

        self.sets_list.blockSignals(True)
        self.runs_list.blockSignals(True)

        try:
            self.sets_list.clear()
            self.runs_list.clear()
        finally:
            self.sets_list.blockSignals(False)
            self.runs_list.blockSignals(False)

        self.api_sets = []
        self.api_runs = []

        self.selected_session = (
            cur.data(Qt.UserRole)
            if cur is not None
            else None
        )

        self.selected_session_detail = None
        self.selected_set = None
        self.selected_run = None

        self._update_run_action_buttons()
        self._update_run_details_label()

        if self.selected_session is None:
            return

        session_id = str(
            self.selected_session.get("id") or ""
        )

        if not session_id:
            self.api_status_label.setText(
                "The selected session has no ID."
            )
            return

        self.api_status_label.setText(
            "Loading sets..."
        )

        self._start_api_worker(
            ApiFetchSessionDetailsWorker(
                session_id=session_id,
                fallback_session=(
                    self.selected_session.get("_raw")
                    or {}
                ),
            ),
            self._on_session_details_loaded,
        )

    def _on_session_details_loaded(self, payload):
        self.selected_session_detail = (
            (payload or {}).get("session")
        )

        self.api_sets = list(
            (payload or {}).get("sets") or []
        )

        self.sets_list.blockSignals(True)

        try:
            self.sets_list.clear()

            for set_data in self.api_sets:
                set_index = set_data.get("index")

                set_name = (
                    set_data.get("display_name")
                    or set_data.get("id")
                    or "Set"
                )

                item = QListWidgetItem(
                    f"Set {set_index}: {set_name}"
                )

                item.setData(
                    Qt.UserRole,
                    set_data,
                )

                self.sets_list.addItem(item)

        finally:
            self.sets_list.blockSignals(False)

        self.api_status_label.setText(
            f"Loaded {len(self.api_sets)} sets."
        )

    def _on_set_item_changed(self, cur, prev):
        if self._api_thread is not None:
            return

        self.runs_list.blockSignals(True)

        try:
            self.runs_list.clear()
        finally:
            self.runs_list.blockSignals(False)

        self.api_runs = []

        self.selected_set = (
            cur.data(Qt.UserRole)
            if cur is not None
            else None
        )

        self.selected_run = None

        self._update_run_action_buttons()
        self._update_run_details_label()

        if self.selected_set is None:
            return

        set_id = str(
            self.selected_set.get("id") or ""
        )

        if not set_id:
            self.api_status_label.setText(
                "The selected set has no ID."
            )
            return

        self.api_status_label.setText(
            "Loading runs..."
        )

        self._start_api_worker(
            ApiFetchTrainingDataForSetWorker(
                set_id=set_id,
                include_samples=True,
                filter_mode="None",
            ),
            self._on_training_data_loaded,
        )


    def _on_training_data_loaded(self, payload):
        self.api_runs = list(
            (payload or {}).get("runs") or []
        )

        self._populate_runs_list()

        self.api_status_label.setText(
            f"Loaded {len(self.api_runs)} runs."
        )

    def _populate_runs_list(self, select_run_id=None):
        self.runs_list.blockSignals(True)

        try:
            self.runs_list.clear()

            item_to_select = None

            for run in self.api_runs:
                identity = self._identity_for_run(run)

                item = QListWidgetItem(
                    f"{self.run_library.get_status_icon(identity)} "
                    f"{run.get('display_name') or run.get('id') or 'Run'}"
                )

                item.setData(
                    Qt.UserRole,
                    run,
                )

                self.runs_list.addItem(item)

                if (
                    select_run_id is not None
                    and str(run.get("id"))
                    == str(select_run_id)
                ):
                    item_to_select = item

            if item_to_select is not None:
                self.runs_list.setCurrentItem(
                    item_to_select
                )

        finally:
            self.runs_list.blockSignals(False)

    def _on_run_item_changed(self, cur, prev):
        self.selected_run = cur.data(Qt.UserRole) if cur else None
        self._update_run_action_buttons()
        self._update_run_details_label()

    def _identity_for_run(self, run=None):
        run = run or self.selected_run or {}
        c = self.selected_client or {}
        s = self.selected_session or {}
        st = self.selected_set or {}
        return RunIdentity(client_id=str(c.get("id") or "client"), client_name=str(c.get("display_name") or ""), session_id=str(s.get("id") or "session"), session_name=str(s.get("display_name") or ""), set_id=str(st.get("id") or st.get("index") or "set"), set_name=str(st.get("display_name") or ""), set_index=st.get("index"), run_id=str(run.get("id") or run.get("index") or "run"), run_name=str(run.get("display_name") or "Run"), run_index=run.get("index"))

    def _update_run_action_buttons(self):
        has = self.selected_run is not None
        self.attach_video_btn.setEnabled(has)
        self.add_angle_btn.setEnabled(has)
        self.auto_match_video_btn.setEnabled(bool(self.api_runs))
        self.load_run_btn.setEnabled(False if not has else not self.run_library.get_missing_requirements(self._identity_for_run()))

    def _auto_match_video_folder(self):
        """Match selected-set runs to only the relevant videos in a folder."""
        if not self.api_runs:
            self._show_warning("Auto-match videos", "Load a set with runs first.")
            return

        parent = self.window()
        parent.showNormal()
        parent.raise_()
        parent.activateWindow()

        source, accepted = QInputDialog.getItem(
            self,
            "Auto-match videos",
            "Choose video source:",
            ["CONTEMPLAS recordings", "Selected folder"],
            0,
            False,
        )
        if not accepted:
            return

        include_contemplas = source == "CONTEMPLAS recordings"
        if include_contemplas:
            appdata = os.environ.get("APPDATA", "").strip()
            if not appdata:
                self._show_warning(
                    "Auto-match CONTEMPLAS",
                    "Windows APPDATA is unavailable, so the CONTEMPLAS sessions "
                    "folder could not be located automatically.",
                )
                return

            folder = Path(appdata) / "NTC SwimLab Analyse" / "accounts"
            if not folder.exists():
                self._show_warning(
                    "Auto-match CONTEMPLAS",
                    "The CONTEMPLAS accounts folder was not found:\n\n"
                    f"{folder}\n\n"
                    "Create a recording in NTC SwimLab Analyse first, or use "
                    "Add camera angle to select a manifest manually.",
                )
                return
        else:
            cover_was_visible = self._hide_still_cover()
            try:
                selected_folder = QFileDialog.getExistingDirectory(
                    self,
                    "Select video folder",
                    str(Path.cwd()),
                )
            finally:
                self._restore_still_cover(cover_was_visible)
            if not selected_folder:
                return
            folder = Path(selected_folder)

        self.status_label.setText("Scanning videos and reading timestamps...")
        QApplication.processEvents()
        try:
            # Filesystem mtime is intentionally disabled. OneDrive/copying can
            # rewrite it, so only embedded video timestamps are used.
            videos, metadata_errors = scan_video_folder(
                folder,
                recursive=include_contemplas,
                allow_filesystem_fallback=False,
                include_contemplas=include_contemplas,
            )
            if not videos:
                details = "\n".join(
                    f"{path.name}: {error}"
                    for path, error in metadata_errors[:8]
                )
                message = "No videos with readable embedded timestamps were found."
                if details:
                    message += "\n\n" + details
                self._show_warning("Auto-match videos", message)
                return

            result = match_runs_to_multiple_videos(
                self.api_runs,
                videos,
                # Verified recordings put GoPro creation_time roughly 58 s
                # after 1080 motion.created. This is a clock-model prior, not
                # a pre-roll assumption. The matcher refines it per folder.
                expected_clock_offset_seconds=0.0,
                offset_search_seconds=600.0,
                residual_tolerance_seconds=8.0,
                ambiguity_margin_seconds=2.0,
            )
            matches = result["matches"]
            if not matches:
                self._show_warning(
                    "Auto-match videos",
                    self._format_auto_match_summary(
                        result,
                        metadata_errors,
                        attached=0,
                    ),
                )
                return

            answer = self._ask_question(
                "Attach automatically matched videos?",
                self._format_auto_match_summary(
                    result,
                    metadata_errors,
                    attached=None,
                )
                + "\n\nOnly confident one-to-one matches within each camera group will be attached. "
                  "Several camera groups may attach to the same run. "
                  "Unused folder videos are ignored. Attach video remains "
                  "available as a manual override.",
                default_button=QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return

            group_by_video_path = {}
            for group in result.get("match_groups", []):
                group_index = int(group.get("index") or 1)
                for group_match in group.get("matches", []):
                    group_by_video_path[
                        group_match.video.path.resolve()
                    ] = group_index

            for match in matches:
                match_identity = self._identity_for_run(match.run)
                if match.video.path.name.lower() in {"session.manifest.json", "swimlab-recording.json"}:
                    attach_manifest_package(self.run_library, match_identity, match.video.path)
                    self._choose_automatched_led_camera(match_identity, match.video.path)
                    continue
                existing_videos = self.run_library.get_videos(match_identity)
                attach_method = (
                    self.run_library.attach_video_angle
                    if existing_videos
                    else self.run_library.attach_video
                )
                attach_kwargs = {
                    "attachment_mode": "automatic",
                    "match_details": {
                        "run_timestamp_utc": match.run_timestamp.isoformat(),
                        "video_timestamp_utc": match.video.timestamp.isoformat(),
                        "video_duration_seconds": float(match.video.duration_seconds),
                        "clock_delta_seconds": float(match.clock_delta_seconds),
                        "session_clock_offset_seconds": result.get(
                            "estimated_clock_offset_seconds"
                        ),
                        "clock_residual_seconds": float(match.residual_seconds),
                        "timestamp_source": match.video.timestamp_source,
                        "confidence": match.confidence,
                        "pre_roll_assumed": False,
                    },
                }
                if existing_videos:
                    group_index = group_by_video_path.get(
                        match.video.path.resolve(),
                        len(existing_videos) + 1,
                    )
                    folder_label = match.video.path.parent.name.strip()
                    attach_kwargs["label"] = (
                        f"{folder_label} camera {group_index}"
                        if folder_label
                        else f"Camera {group_index}"
                    )
                attach_method(
                    match_identity,
                    match.video.path,
                    **attach_kwargs,
                )

            # Fetch/cache data exactly as the manual workflow does, but one run
            # at a time so the existing single-worker design stays intact.
            fetch_by_run_id = {}
            for match in matches:
                identity = self._identity_for_run(match.run)
                fetch_by_run_id[str(identity.run_id)] = (
                    identity,
                    dict(match.run.get("_raw") or {}),
                )
            self._auto_match_fetch_queue = list(fetch_by_run_id.values())
            self._auto_match_fetch_total = len(self._auto_match_fetch_queue)
            self._auto_match_fetch_done = 0
            self._refresh_current_run_visuals()
            self.status_label.setText(
                f"Attached {len(matches)} video link(s). Caching run data..."
            )
            QTimer.singleShot(0, self._start_next_auto_match_fetch)
#            self._show_info(
#                "Auto-match complete",
#                self._format_auto_match_summary(
#                    result,
#                    metadata_errors,
#                    attached=len(matches),
#                ),
#            )
        except Exception as exc:
            self._show_error("Could not auto-match videos", str(exc))

    def _start_next_auto_match_fetch(self):
        if self._api_thread is not None:
            QTimer.singleShot(100, self._start_next_auto_match_fetch)
            return
        if not self._auto_match_fetch_queue:
            total = int(self._auto_match_fetch_total)
            self.status_label.setText(
                f"Auto-match complete: {total} run(s) linked."
            )
            self._refresh_current_run_visuals()
            return

        identity, raw_run_data = self._auto_match_fetch_queue.pop(0)
        current = self._auto_match_fetch_done + 1
        total = self._auto_match_fetch_total
        self.status_label.setText(
            f"Caching matched run data {current}/{total}..."
        )
        worker = ApiFetchAndCacheRunDataWorker(
            identity=identity,
            raw_run_data=raw_run_data,
        )
        worker.error.connect(self._handle_auto_match_fetch_error)
        self._start_api_worker(
            worker,
            lambda payload, ident=identity: self._finish_auto_match_fetch(
                ident,
                payload,
            ),
        )

    def _handle_auto_match_fetch_error(self, _message):
        self._auto_match_fetch_done += 1
        QTimer.singleShot(150, self._start_next_auto_match_fetch)

    def _finish_auto_match_fetch(self, identity, payload):
        self._auto_match_fetch_done += 1
        try:
            self.run_library.set_complete(identity, True)
        except Exception:
            pass
        self._refresh_current_run_visuals()
        QTimer.singleShot(100, self._start_next_auto_match_fetch)

    def _format_auto_match_summary(self, result, metadata_errors, attached=None):
        matches = result.get("matches", [])
        ambiguous = result.get("ambiguous", [])
        unmatched = result.get("unmatched", [])
        unused_videos = result.get("unused_videos", [])
        offset = result.get("estimated_clock_offset_seconds")
        groups = result.get("match_groups", [])

        lines = [
            f"Camera groups found: {len(groups)}",
            f"{'Confident matches found' if attached is None else 'Videos attached'}: "
            f"{len(matches) if attached is None else attached}",
           # f"Ambiguous runs left unchanged: {len(ambiguous)}",
            f"Unmatched runs left unchanged: {len(unmatched)}",
           # f"Unrelated/unused folder videos: {len(unused_videos)}",
           # f"Unreadable videos: {len(metadata_errors)}",
        ]
        if groups:
            offsets = [
                group.get("estimated_clock_offset_seconds")
                for group in groups
            ]
            #for group_index, group_offset in enumerate(offsets, start=1):
                #if group_offset is not None:
                    #lines.append(
                        #f"Camera group {group_index} clock offset: "
                        #f"{float(group_offset):+.3f} s"
                    #)
        #elif offset is not None:
            #lines.append(f"Estimated session clock offset: {offset:+.3f} s")

        if matches:
            lines.append("\nMatches:")
            for match in matches[:12]:
                run_name = (
                    match.run.get("display_name")
                    or match.run.get("id")
                    or "Run"
                )
                lines.append(
                    f"  {run_name} -> {match.video.path.name} "
                    #f"(clock residual {match.residual_seconds:.3f} s)"
                )

        if ambiguous:
            lines.append("\nAmbiguous runs requiring manual attachment:")
            for item in ambiguous[:6]:
                run = item.get("run") or {}
                run_name = run.get("display_name") or run.get("id") or "Run"
                names = ", ".join(
                    candidate[1].path.name
                    for candidate in item.get("candidates", [])
                )
                lines.append(f"  {run_name}: {names}")

        if unmatched:
            lines.append("\nUnmatched runs:")
            for item in unmatched[:6]:
                run = item.get("run") or {}
                run_name = run.get("display_name") or run.get("id") or "Run"
                lines.append(f"  {run_name}: {item.get('reason', 'no match')}")



        return "\n".join(lines)

    def _attach_video_and_fetch_data(self):
        if not self.selected_run:
            return

        selected_run_snapshot = dict(self.selected_run)
        raw_run_data = selected_run_snapshot.get("_raw") or {}

        try:
            if self.playing or self.pending_play_start:
                self.pause()
        except Exception:
            pass

        cover_was_visible = self._hide_still_cover()

        try:
            filename, _ = QFileDialog.getOpenFileName(
                self,
                "Select video",
                str(Path.cwd()),
                "Video files (*.mp4 *.mov *.avi *.mkv *.m4v);;All files (*.*)",
            )
        finally:
            self._restore_still_cover(cover_was_visible)

        if not filename:
            return

        try:
            identity = self._identity_for_run(selected_run_snapshot)

            self.run_library.attach_video(
                identity,
                Path(filename),
                attachment_mode="manual",
                match_details={"source": "manual file selection"},
            )
            if self.current_run_identity == identity:
                self._load_camera_views(identity)

            self.status_label.setText(
                f"Attached video to {identity.run_name}. Fetching run data..."
            )

            QTimer.singleShot(
                0,
                lambda ident=identity, raw=raw_run_data: self._start_fetch_for_attached_video(
                    ident,
                    raw,
                ),
            )

            self._refresh_current_run_visuals()

        except Exception as exc:
            self._show_error(
                "Could not attach video",
                str(exc),
            )
            self._refresh_current_run_visuals()

    def _start_fetch_for_attached_video(self, identity, raw_run_data):
        try:
            worker = ApiFetchAndCacheRunDataWorker(
                identity=identity,
                raw_run_data=raw_run_data or {},
            )

            self._start_api_worker(
                worker,
                lambda payload: self._finish_attach_video_fetch(
                    identity,
                    payload,
                ),
            )

        except Exception as exc:
            self._show_error(
                "Could not start data fetch",
                str(exc),
            )
            self._refresh_current_run_visuals()


    def _finish_attach_video_fetch(self, identity, payload):
        try:
            if payload is None:
                payload = {}

            rows = payload.get("rows", 0)

            self.run_library.set_complete(
                identity,
                True,
            )

            self.status_label.setText(
                f"Attached video and fetched {rows} samples."
            )

            self._refresh_current_run_visuals()

        except Exception as exc:
            self._show_error(
                "Could not finish attaching run",
                str(exc),
            )
            self._refresh_current_run_visuals()

    def _update_run_details_label(self):
        if not self.selected_run:
            self.run_details_label.setText("No run selected.")
            return
        ident = self._identity_for_run()
        miss = self.run_library.get_missing_requirements(ident)
        self.run_details_label.setText(f"{ident.run_name}\n" + ("Ready to load." if not miss else "Missing: " + ", ".join(miss)))

    def _refresh_current_run_visuals(self):
        self._populate_runs_list()
        self._update_run_action_buttons()
        self._update_run_details_label()

    def _load_selected_run(self):
        identity = self._identity_for_run()
        video_path, data_path = self.run_library.get_load_paths(identity)
        if self.current_run_identity != identity:
            self.camera_drawings = {}
            self.shared_sync_offset_seconds = None
        self.current_run_identity = identity
        self._load_camera_views(identity)
        active = self._active_camera_view()
        if active is not None:
            video_path = active.path
        self.load_video_data_files(video_path, data_path)

    def _load_camera_views(self, identity: RunIdentity) -> None:
        entries = self.run_library.get_videos(identity)
        self.camera_views = [
            CameraView.from_mapping(entry, index)
            for index, entry in enumerate(entries)
            if entry.get("path") and Path(entry["path"]).exists()
        ]
        requested = self.run_library.get_active_video_id(identity)
        ids = {camera.id for camera in self.camera_views}
        if requested not in ids:
            primary = next((camera for camera in self.camera_views if camera.is_primary), None)
            requested = primary.id if primary is not None else (self.camera_views[0].id if self.camera_views else None)
        self.active_camera_id = requested
        self._rebuild_camera_strip()

    def _active_camera_view(self) -> CameraView | None:
        return next(
            (camera for camera in self.camera_views if camera.id == self.active_camera_id),
            None,
        )

    def _rebuild_camera_strip(self) -> None:
        self._thumbnail_generation += 1
        self._thumbnail_pending.clear()
        self._thumbnail_decoder.cancel()
        while self.camera_strip_layout.count() > 0:
            item = self.camera_strip_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.camera_thumbnail_widgets.clear()
        for camera in self.camera_views:
            widget = CameraThumbnailWidget(camera.id, camera.label)
            widget.clicked.connect(self._switch_active_camera)
            widget.set_active(camera.id == self.active_camera_id)
            cached_preview = self.camera_preview_images.get(camera.id)
            if cached_preview is not None and not cached_preview.isNull():
                widget.set_image(cached_preview)
                widget.set_status("Active" if camera.id == self.active_camera_id else None)
            #else:
                #widget.set_status("Open camera to preview")
            self.camera_thumbnail_widgets[camera.id] = widget
            self.camera_strip_layout.addWidget(widget)
        self.camera_strip_layout.addStretch(1)
        self.camera_strip_scroll.setVisible(len(self.camera_views) > 1)
        self._schedule_thumbnail_update(80)

    def _choose_automatched_led_camera(self, identity, manifest_path: Path) -> None:
        """Choose which camera in an automatically matched package sees the LED."""
        snapshot = cached_snapshot(manifest_path)
        package_paths = {
            str(Path(camera["path"]).resolve())
            for camera in snapshot.get("cameras") or []
        }
        attached = [
            camera
            for camera in self.run_library.get_videos(identity)
            if camera.get("path")
            and str(Path(camera["path"]).resolve()) in package_paths
        ]
        if not attached:
            return

        labels = [
            str(camera.get("label") or Path(camera["path"]).stem)
            for camera in attached
        ]
        selected_label, accepted = QInputDialog.getItem(
            self,
            "Select LED synchronization camera",
            "Select the camera angle that can see the synchronization LED:",
            labels,
            0,
            False,
            flags=Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        if not accepted:
            return

        selected = attached[labels.index(selected_label)]
        selected_id = str(selected["id"])
        self.run_library.set_active_video(identity, selected_id)

        details = dict(selected.get("match_details") or {})
        group_id = details.get("sync_group_id")
        if group_id:
            run_meta = self.run_library.ensure_run_meta(identity)
            groups = dict(run_meta.get("synchronized_camera_groups") or {})
            groups.pop(str(group_id), None)
            run_meta["synchronized_camera_groups"] = groups
            self.run_library.save_run_meta(identity, run_meta)

        selected_path = Path(selected["path"]).resolve()
        selected_cache_dir = cache_dir_for_video(selected_path, self.video.cache_root)
        try:
            (selected_cache_dir / "sync_meta.json").unlink(missing_ok=True)
        except Exception:
            pass

        cache_meta_file = selected_cache_dir / "cache_meta.json"
        if cache_meta_file.exists():
            cache_meta = _read_json(cache_meta_file)
            for key in (
                "led_frame",
                "sync_frame",
                "led_time_seconds",
                "sync_time_seconds",
                "data_delay_ms",
                "user_offset_frames",
            ):
                cache_meta.pop(key, None)
            _write_json(cache_meta_file, cache_meta)

        if self.current_run_identity == identity:
            self.active_camera_id = selected_id
            self._load_camera_views(identity)
            self._rebuild_camera_strip()

    def _add_camera_angle(self):
        if self.selected_run is None:
            return

        cover_was_visible = self._hide_still_cover()
        try:
            filename, _ = QFileDialog.getOpenFileName(
                self,
                "Add camera angle or CONTEMPLAS recording",
                str(Path.cwd()),
                (
                    "Videos and CONTEMPLAS manifests "
                    "(*.mp4 *.mov *.avi *.mkv *.m4v *.json);;"
                    "All files (*.*)"
                ),
            )
        finally:
            self._restore_still_cover(cover_was_visible)

        if not filename:
            return

        selected_path = Path(filename)
        if selected_path.name.lower() in {
            "session.manifest.json",
            "swimlab-recording.json",
        }:
            try:
                self._attach_contemplas_manifest(selected_path)
            except Exception as exc:
                self._show_error(
                    "Could not add CONTEMPLAS cameras",
                    str(exc),
                )
            return

        default_label = (
            f"Camera {len(self.run_library.get_videos(self._identity_for_run())) + 1}"
        )
        label, ok = QInputDialog.getText(
            self,
            "Camera label",
            "Name this camera angle:",
            QLineEdit.Normal,
            default_label,
            flags=Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        if not ok:
            return

        identity = self._identity_for_run()
        self.run_library.attach_video_angle(
            identity,
            selected_path,
            label=label.strip() or default_label,
            attachment_mode="manual",
        )
        if self.current_run_identity == identity:
            self._load_camera_views(identity)
            self._update_all_camera_previews()
        self._refresh_current_run_visuals()
        self.status_label.setText(f"Added camera angle: {selected_path.name}")

    def _attach_contemplas_manifest(self, manifest_path: Path) -> None:
        from viewer.contemplas_manifest import load_contemplas_manifest

        if self.selected_run is None:
            raise RuntimeError(
                "Select a 1080 run before importing CONTEMPLAS cameras."
            )

        selected_run_snapshot = dict(self.selected_run)
        identity = self._identity_for_run(selected_run_snapshot)
        raw_run_data = dict(selected_run_snapshot.get("_raw") or {})
        package = load_contemplas_manifest(manifest_path)
        cameras = list(package.get("cameras") or [])

        if not cameras:
            raise ValueError("The manifest contains no BCAP cameras.")

        missing = [
            camera["path"]
            for camera in cameras
            if not Path(camera["path"]).exists()
        ]
        if missing:
            raise FileNotFoundError(
                "Missing BCAP files: "
                + "; ".join(str(item) for item in missing)
            )

        existing = self.run_library.get_videos(identity)
        existing_paths = {
            str(Path(item.get("path", "")).resolve())
            for item in existing
            if item.get("path")
        }
        added = 0

        for camera in cameras:
            camera_path = Path(camera["path"]).resolve()
            if str(camera_path) in existing_paths:
                continue

            details = {
                "source_type": "bcap",
                "manifest_path": str(manifest_path.resolve()),
                "manifest_camera_id": str(camera["camera_id"]),
                "sync_group_id": str(camera["sync_group_id"]),
                "synchronized": True,
                "sync_offset_frames": int(
                    camera.get("sync_offset_frames") or 0
                ),
                "fps": float(camera.get("fps") or 100.0),
                "frame_count": int(camera.get("frame_count") or 0),
                "rotation": int(camera.get("rotation") or 0),
            }

            self.run_library.attach_video_angle(
                identity,
                camera_path,
                label=str(camera.get("label") or camera_path.stem),
                attachment_mode="contemplas_manifest",
                match_details=details,
                make_primary=(not existing and added == 0),
            )
            existing_paths.add(str(camera_path))
            added += 1

        was_current_run_loaded = (
            self.current_run_identity == identity and self.video.is_ready
        )
        self.current_run_identity = identity
        self._load_camera_views(identity)
        if was_current_run_loaded:
            self._update_all_camera_previews()

        # The synchronized camera group shares one LED frame, but not every
        # camera angle can see the LED. Choose the reference camera explicitly.
        imported_paths = {
            str(Path(camera["path"]).resolve())
            for camera in cameras
        }
        selectable_cameras = [
            camera
            for camera in self.camera_views
            if str(camera.path.resolve()) in imported_paths
        ]
        if not selectable_cameras:
            raise RuntimeError(
                "The CONTEMPLAS cameras were attached, but no imported "
                "camera could be selected for LED synchronization."
            )

        labels = [camera.label for camera in selectable_cameras]
        selected_label, accepted = QInputDialog.getItem(
            self,
            "Select LED synchronization camera",
            "Select the camera angle that can see the synchronization LED:",
            labels,
            0,
            False,
            flags=Qt.Dialog | Qt.WindowStaysOnTopHint,
        )
        if not accepted:
            self._refresh_current_run_visuals()
            self.status_label.setText(
                "CONTEMPLAS cameras were added, but LED synchronization "
                "was not started. Import the manifest again to choose a camera."
            )
            return

        selected_camera = selectable_cameras[labels.index(selected_label)]
        self.active_camera_id = selected_camera.id
        self.run_library.set_active_video(identity, selected_camera.id)

        # Selecting a reference camera is an explicit request to verify the LED
        # from that camera. Remove any old group reference and only the selected
        # camera's previous automatic sync values. Rotation and frame index cache
        # are preserved.
        selected_details = dict(selected_camera.match_details or {})
        selected_group_id = selected_details.get("sync_group_id")
        if selected_group_id:
            run_meta = self.run_library.ensure_run_meta(identity)
            groups = dict(run_meta.get("synchronized_camera_groups") or {})
            groups.pop(str(selected_group_id), None)
            run_meta["synchronized_camera_groups"] = groups
            self.run_library.save_run_meta(identity, run_meta)

        selected_cache_dir = cache_dir_for_video(selected_camera.path)
        selected_sync_file = selected_cache_dir / "sync_meta.json"
        try:
            selected_sync_file.unlink(missing_ok=True)
        except Exception:
            pass

        selected_cache_meta_file = selected_cache_dir / "cache_meta.json"
        if selected_cache_meta_file.exists():
            selected_cache_meta = _read_json(selected_cache_meta_file)
            for key in (
                "led_frame",
                "sync_frame",
                "led_time_seconds",
                "sync_time_seconds",
                "data_delay_ms",
                "user_offset_frames",
            ):
                selected_cache_meta.pop(key, None)
            _write_json(selected_cache_meta_file, selected_cache_meta)

        self._rebuild_camera_strip()
        self._refresh_current_run_visuals()

        data_path = self.run_library.get_data_path(identity)
        if data_path is not None and Path(data_path).exists():
            self.run_library.set_complete(identity, True)
            self._refresh_current_run_visuals()
            self.status_label.setText(
                f"Imported {added} CONTEMPLAS cameras. Loading run..."
            )
            QTimer.singleShot(0, self._load_selected_run)
            return

        self.status_label.setText(
            f"Imported {added} CONTEMPLAS cameras. "
            "Fetching 1080 run data..."
        )
        QTimer.singleShot(
            0,
            lambda ident=identity, raw=raw_run_data: (
                self._start_fetch_for_contemplas(ident, raw)
            ),
        )

    def _start_fetch_for_contemplas(self, identity, raw_run_data) -> None:
        try:
            worker = ApiFetchAndCacheRunDataWorker(
                identity=identity,
                raw_run_data=raw_run_data or {},
            )
            self._start_api_worker(
                worker,
                lambda payload: self._finish_contemplas_fetch(
                    identity,
                    payload,
                ),
            )
        except Exception as exc:
            self._show_error("Could not fetch run data", str(exc))
            self._refresh_current_run_visuals()

    def _finish_contemplas_fetch(self, identity, payload) -> None:
        try:
            payload = payload or {}
            rows = int(payload.get("rows") or 0)
            data_path = self.run_library.get_data_path(identity)
            if data_path is None or not Path(data_path).exists():
                raise RuntimeError(
                    "The 1080 data download finished without creating "
                    "the local data file."
                )

            self.run_library.set_complete(identity, True)
            self.current_run_identity = identity
            self._load_camera_views(identity)
            self._refresh_current_run_visuals()
            self.status_label.setText(
                f"Imported CONTEMPLAS cameras and fetched {rows} samples. "
                "Loading run..."
            )
            QTimer.singleShot(0, self._load_selected_run)
        except Exception as exc:
            self._show_error(
                "Could not finish importing CONTEMPLAS recording",
                str(exc),
            )
            self._refresh_current_run_visuals()

    def _save_active_camera_drawings(self) -> None:
        if self.active_camera_id is None:
            return
        try:
            self.camera_drawings[self.active_camera_id] = self.drawing.export_shapes()
        except Exception:
            pass

    def _restore_active_camera_drawings(self) -> None:
        try:
            self.drawing.import_shapes(
                self.camera_drawings.get(self.active_camera_id or "", [])
            )
        except Exception:
            self.drawing.clear_drawings()

    def _initialize_shared_sync_offset(self, saved_offset_frames: int) -> None:
        """Initialize run-wide aftersync from run metadata or legacy active video."""
        if self.current_run_identity is None:
            return
        if self.shared_sync_offset_seconds is not None:
            return

        stored = self.run_library.get_shared_sync_offset_seconds(
            self.current_run_identity
        )
        if stored is not None:
            self.shared_sync_offset_seconds = float(stored)
            return

        fps = float(self.video.fps or 0.0)
        self.shared_sync_offset_seconds = (
            float(saved_offset_frames) / fps
            if fps > 0
            else 0.0
        )

    def _shared_offset_frames_for_fps(self, fps: float) -> int:
        fps = float(fps or 0.0)
        if fps <= 0 or self.shared_sync_offset_seconds is None:
            return 0
        return int(round(float(self.shared_sync_offset_seconds) * fps))

    def _apply_shared_sync_to_active_camera(self) -> None:
        if self.current_run_identity is None:
            return
        self.sync._user_offset_frames = self._shared_offset_frames_for_fps(
            self.video.fps
        )

    def _camera_fps_from_cache(self, camera: CameraView) -> float:
        try:
            cache_dir = cache_dir_for_video(camera.path, self.video.cache_root)
            meta_path = cache_dir / "cache_meta.json"
            if meta_path.exists():
                meta = _read_json(meta_path)
                fps = float(
                    meta.get("cache_fps")
                    or meta.get("orig_fps")
                    or 0.0
                )
                if fps > 0:
                    return fps
        except Exception:
            pass
        if camera.id == self.active_camera_id:
            return float(self.video.fps or 0.0)
        return 0.0

    def _persist_shared_sync_to_all_cameras(self) -> None:
        """Save the same time correction in each camera's native frame space."""
        if self.current_run_identity is None:
            # Preserve the original standalone-folder save behavior.
            data = _read_json(self.video.cache_dir / "sync_meta.json")
            data["user_offset_frames"] = int(self.sync.user_offset)
            _write_json(self.video.cache_dir / "sync_meta.json", data)
            return

        seconds = float(self.shared_sync_offset_seconds or 0.0)
        self.run_library.set_shared_sync_offset_seconds(
            self.current_run_identity,
            seconds,
        )

        for camera in self.camera_views:
            try:
                cache_dir = cache_dir_for_video(
                    camera.path,
                    self.video.cache_root,
                )
                sync_path = cache_dir / "sync_meta.json"
                if not sync_path.exists():
                    # Unsynchronized cameras receive the shared value when they
                    # are first opened and their sync metadata is created.
                    continue
                fps = self._camera_fps_from_cache(camera)
                if fps <= 0:
                    continue
                data = _read_json(sync_path)
                data["user_offset_frames"] = int(
                    round(seconds * fps)
                )
                data["shared_sync_offset_seconds"] = seconds
                _write_json(sync_path, data)
            except Exception:
                continue

    def _shared_sync_status_text(self) -> str:
        frames = int(self.sync.user_offset)
        milliseconds = float(self.shared_sync_offset_seconds or 0.0) * 1000.0
        camera_count = max(1, len(self.camera_views))
        return (
            f"Run sync: {frames:+d} frames on active camera "
            f"({milliseconds:+.2f} ms), applied to {camera_count} camera(s)"
        )

    def _active_sync_media_time(self) -> float:
        """Actual media timestamp corresponding to 1080 data time zero."""
        base_frame = int(getattr(self.sync, "_base_offset_frames", 0) or 0)
        base_time = float(self.video.frame_to_time(base_frame))
        if self.current_run_identity is not None and self.shared_sync_offset_seconds is not None:
            return base_time + float(self.shared_sync_offset_seconds)
        return float(self.video.frame_to_time(self.sync.video_offset_frames))

    def _video_frame_to_data_time(self, frame_index: int) -> float:
        return float(self.video.frame_to_time(int(frame_index)) - self._active_sync_media_time())

    def _data_time_to_video_frame(self, data_time: float) -> int:
        return int(self.video.time_to_frame(self._active_sync_media_time() + float(data_time)))

    def _get_or_create_active_sync(self):
        """Use one LED confirmation for an explicitly synchronized camera group."""
        camera = self._active_camera_view()
        details = dict(camera.match_details or {}) if camera is not None else {}
        group_id = details.get("sync_group_id") if details.get("synchronized") else None
        camera_offset = int(details.get("sync_offset_frames") or 0)

        if group_id and self.current_run_identity is not None:
            run_meta = self.run_library.ensure_run_meta(self.current_run_identity)
            stored = dict(run_meta.get("synchronized_camera_groups") or {}).get(str(group_id))
            if stored is not None:
                return int(stored["reference_sync_frame"]) + camera_offset, 0

        sync_frame, saved_offset = get_or_create_sync_frame(
            Path(self.video.video_path), self.video.cache_dir, self.video
        )

        if group_id and self.current_run_identity is not None:
            local_meta = _read_json(self.video.cache_dir / "sync_meta.json")
            run_meta = self.run_library.ensure_run_meta(self.current_run_identity)
            groups = dict(run_meta.get("synchronized_camera_groups") or {})
            groups[str(group_id)] = {
                "reference_camera_id": camera.id if camera is not None else None,
                "reference_led_frame": int(local_meta.get("led_frame", sync_frame)) - camera_offset,
                "reference_sync_frame": int(sync_frame) - camera_offset,
                "fps": float(details.get("fps") or self.video.fps),
            }
            run_meta["synchronized_camera_groups"] = groups
            self.run_library.save_run_meta(self.current_run_identity, run_meta)

        return sync_frame, saved_offset

    def _switch_active_camera(self, camera_id: str) -> None:
        camera_id = str(camera_id)
        if camera_id == self.active_camera_id:
            return
        camera = next((item for item in self.camera_views if item.id == camera_id), None)
        if camera is None or not camera.path.exists() or not self.sync.has_data:
            return

        was_playing = bool(self.playing or self.pending_play_start or self.playback_warmup)
        self._restore_focus_after_camera_switch = was_playing
        data_time = self._video_frame_to_data_time(self.frame_index)
        self._suspend_thumbnail_updates()
        self.pause(update_camera_previews=False)
        self._save_active_camera_drawings()
        self.active_camera_id = camera.id
        if self.current_run_identity is not None:
            self.run_library.set_active_video(self.current_run_identity, camera.id)

        try:
            self._set_ready(False)
            self.video_panel.clear_still_image()
            self.current_video_path = camera.path
            self.current_folder = camera.path.parent
            self.status_label.setText(f"Switching to {camera.label}...")
            QApplication.processEvents()
            self.video.open(camera.path)
            self.video.load_frames_from_cache()
            sync_frame, saved_offset = self._get_or_create_active_sync()
            self._reload_video_rotation_from_sync_meta()
            self.sync.set_video_offset(sync_frame)
            self._initialize_shared_sync_offset(saved_offset)
            self._apply_shared_sync_to_active_camera()
            self.frame_index = max(
                0,
                min(
                    self._data_time_to_video_frame(data_time),
                    self.video.frame_count - 1,
                ),
            )
            self._sync_video_panel_aspect()
            self._show_still_frame(self.frame_index)
            self.video.set_position_frame(self.frame_index)
            self.video.update_cache_meta_rotation()
            self._restore_active_camera_drawings()
            self._set_ready(True)
            self._rebuild_camera_strip()
            self.status_label.setText(
                f"Active camera: {camera.label} | {camera.path.name}"
            )
            if was_playing:
                QTimer.singleShot(80, self.play)
            else:
                self._update_all_camera_previews()
                self._restore_focus_after_camera_switch = False
                QTimer.singleShot(0, self._focus_loaded_still_image)
                QTimer.singleShot(120, self._focus_loaded_still_image)
        except Exception as exc:
            self._restore_focus_after_camera_switch = False
            self._show_error("Could not switch camera", str(exc))
            self._set_ready(False)

    def _suspend_thumbnail_updates(self) -> None:
        """Stop all secondary decoding before playback or camera switching."""
        try:
            self.thumbnail_update_timer.stop()
        except Exception:
            pass
        self._thumbnail_generation += 1
        self._thumbnail_pending.clear()
        self._thumbnail_decoder.cancel()

        for camera in self.camera_views:
            if camera.id == self.active_camera_id:
                continue
            widget = self.camera_thumbnail_widgets.get(camera.id)
            if widget is not None:
                #widget.set_status("Static during playback")
                pass

    def _schedule_thumbnail_update(self, delay_ms: int = 220) -> None:
        """Secondary previews are generated only when their camera is opened."""
        return

    def _camera_preview_time(self, camera: CameraView, data_time: float) -> tuple[float, str, int]:
        try:
            cache_dir = cache_dir_for_video(camera.path, self.video.cache_root)
            cache_meta = _read_json(cache_dir / "cache_meta.json") if (cache_dir / "cache_meta.json").exists() else {}
            sync_meta = _read_json(cache_dir / "sync_meta.json") if (cache_dir / "sync_meta.json").exists() else {}
            fps = float(cache_meta.get("cache_fps") or cache_meta.get("orig_fps") or 0.0)
            rotation = int(sync_meta.get("rotation", cache_meta.get("rotation", 0)) or 0) % 360
            sync_frame = sync_meta.get("sync_frame")
            if sync_frame is None:
                sync_frame = cache_meta.get("sync_frame")
            if self.current_run_identity is not None and self.shared_sync_offset_seconds is not None:
                user_offset = int(round(self.shared_sync_offset_seconds * fps))
            else:
                user_offset = int(sync_meta.get("user_offset_frames", 0) or 0)
            if fps > 0 and sync_frame is not None:
                local_seconds = (float(sync_frame) + user_offset + float(data_time) * fps) / fps
                return max(0.0, local_seconds), "Synchronized", rotation
        except Exception:
            pass
        # Before an angle has its own LED confirmation, use the active local media
        # time as a harmless visual preview and label it clearly.
        local_seconds = self.frame_index / self.video.fps if self.video.fps > 0 else 0.0
        return max(0.0, local_seconds), "Open once to sync", 0

    def _update_camera_thumbnails(self, force: bool = False) -> None:
        """Refresh the active preview only while paused."""
        if (
            self.playing
            or self.pending_play_start
            or self.playback_warmup
            or not self.video.is_ready
            or self.active_camera_id is None
        ):
            return
        try:
            image = self.video.get_frame_image(self.frame_index)
        except Exception:
            image = None
        if image is None or image.isNull():
            return
        detached = image.copy()
        self.camera_preview_images[self.active_camera_id] = detached
        widget = self.camera_thumbnail_widgets.get(self.active_camera_id)
        if widget is not None:
            widget.set_image(detached)
            widget.set_status("Active")

    def _update_all_camera_previews(self) -> None:
        """Decode each attached camera at the current paused data time."""
        if (
            self.playing
            or self.pending_play_start
            or self.playback_warmup
            or not self.video.is_ready
        ):
            return

        self._thumbnail_generation += 1
        self._thumbnail_pending.clear()
        self._thumbnail_decoder.cancel()
        self._update_camera_thumbnails()

        data_time = self._video_frame_to_data_time(self.frame_index)
        for camera in self.camera_views:
            if camera.id == self.active_camera_id:
                continue
            widget = self.camera_thumbnail_widgets.get(camera.id)
            if widget is None:
                continue

            seconds, status, rotation = self._camera_preview_time(camera, data_time)
            self._thumbnail_pending.add(camera.id)
            #widget.set_status(f"Finding paused frame | {status}")
            self._thumbnail_decoder.request(
                camera.id,
                self._thumbnail_generation,
                camera.path,
                seconds,
                rotation,
            )

    def _on_thumbnail_frame_ready(self, camera_id, generation, image, error):
        if generation != self._thumbnail_generation:
            return
        self._thumbnail_pending.discard(camera_id)
        if self.playing or self.pending_play_start or self.playback_warmup:
            return

        widget = self.camera_thumbnail_widgets.get(camera_id)
        if widget is None:
            return
        if error or image is None or image.isNull():
            widget.set_status("Preview unavailable")
            return

        detached = image.copy()
        self.camera_preview_images[camera_id] = detached
        widget.set_image(detached)
        #widget.set_status("Paused preview")


    def _exec_dialog_with_hidden_cover(self, dialog):
        """
        Execute a modal dialog while the top-level paused still-cover is hidden.

        This is needed because still_cover is a separate top-level/native window and
        can otherwise appear above QMessageBox/QInputDialog/QFileDialog on Windows.
        """
        cover_was_visible = self._hide_still_cover()

        try:
            dialog.setWindowModality(Qt.ApplicationModal)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return dialog.exec()
        finally:
            self._restore_still_cover(cover_was_visible)


    def _show_info(self, title, message):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(str(title))
        box.setText(str(message))
        box.setStandardButtons(QMessageBox.Ok)
        return self._exec_dialog_with_hidden_cover(box)


    def _show_warning(self, title, message):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(str(title))
        box.setText(str(message))
        box.setStandardButtons(QMessageBox.Ok)
        return self._exec_dialog_with_hidden_cover(box)


    def _show_critical(self, title, message):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(str(title))
        box.setText(str(message))
        box.setStandardButtons(QMessageBox.Ok)
        return self._exec_dialog_with_hidden_cover(box)


    def _ask_question(self, title, message, default_button=QMessageBox.No):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(str(title))
        box.setText(str(message))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(default_button)
        return self._exec_dialog_with_hidden_cover(box)




    def _hide_still_cover(self):
        try:
            if hasattr(self, "video_panel") and hasattr(self.video_panel, "still_cover"):
                was_visible = self.video_panel.still_cover.isVisible()

                if was_visible:
                    self.video_panel.still_cover.hide()

                return was_visible
        except Exception:
            pass

        return False


    def _restore_still_cover(self, was_visible):
        try:
            if was_visible and hasattr(self, "video_panel") and hasattr(self.video_panel, "still_cover"):
                self.video_panel.still_cover.sync_to_owner()
                self.video_panel.still_cover.show()
                self.video_panel.still_cover.raise_()
                self.video_panel.still_cover.overlay.raise_()
        except Exception:
            pass



    def open_video_data_folder(self):
        cover_was_visible = self._hide_still_cover()

        try:
            folder = QFileDialog.getExistingDirectory(
                self,
                "Select folder",
                str(Path.cwd()),
            )

            if folder:
                self.current_run_identity = None
                self.camera_views = []
                self.active_camera_id = None
                self.camera_drawings = {}
                self.shared_sync_offset_seconds = None
                self._rebuild_camera_strip()
                self.load_video_data_folder(Path(folder))

        finally:
            self._restore_still_cover(cover_was_visible)

    def load_video_data_folder(self, folder):
        files = [p for p in Path(folder).iterdir() if p.is_file()]
        vids = [p for p in files if p.suffix in self.VIDEO_EXTENSIONS]
        exc = [p for p in files if p.suffix in self.EXCEL_EXTENSIONS]
        if len(vids) != 1 or len(exc) != 1:
            raise ValueError("Folder must contain exactly one video and one Excel file")
        self.load_video_data_files(vids[0], exc[0])

    def load_video_data_files(self, video_path, data_path):
        try:
            if self.current_run_identity is None:
                self.camera_views = []
                self.active_camera_id = None
                self._rebuild_camera_strip()
            self.pause()
            self._set_ready(False)
            self._reset_current_view()
            self.current_video_path = Path(video_path)
            self.current_data_path = Path(data_path)
            self.current_folder = self.current_video_path.parent
            self.status_label.setText("Loading data...")
            QApplication.processEvents()
            self.sync.load_data(self.current_data_path)
            self._refresh_plot_data(reset_view=True)
            self.status_label.setText("Opening video...")
            QApplication.processEvents()
            self.video.open(self.current_video_path)
            self._load_frames_then_sync()
        except Exception as e:
            self._show_error("Could not load", str(e))
            self._set_ready(False)

    def eventFilter(self, obj, event):
        """Application-level keyboard routing for both still and native video."""
        event_type = event.type()
        if event_type not in (
            QEvent.KeyPress,
            QEvent.KeyRelease,
            QEvent.ShortcutOverride,
        ):
            return super().eventFilter(obj, event)

        if QApplication.activeModalWidget() is not None:
            return super().eventFilter(obj, event)

        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QLineEdit):
            return super().eventFilter(obj, event)

        key = event.key()
        recognized = {
            Qt.Key_Space,
            Qt.Key_F1,
            Qt.Key_O,
            Qt.Key_M,
            Qt.Key_R,
            Qt.Key_T,
            Qt.Key_X,
            Qt.Key_1,
            Qt.Key_2,
            Qt.Key_3,
            Qt.Key_4,
            Qt.Key_Delete,
            Qt.Key_Backspace,
            Qt.Key_A,
            Qt.Key_D,
            Qt.Key_S,
            Qt.Key_W,
            Qt.Key_Left,
            Qt.Key_Right,
            Qt.Key_Up,
            Qt.Key_Down,
            Qt.Key_Escape,
        }
        if key not in recognized:
            return super().eventFilter(obj, event)

        event.accept()
        if event_type == QEvent.ShortcutOverride:
            return True

        if event_type == QEvent.KeyRelease:
            if key in (
                Qt.Key_A, Qt.Key_D, Qt.Key_S, Qt.Key_W,
                Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
            ) and not event.isAutoRepeat():
                self._stop_step_key(key)
            return True

        # KeyPress reaches this path regardless of whether Windows currently
        # routes native playback focus through QVideoWidget or the main viewer.
        self._dispatch_keyboard_shortcut(event)
        return True

    def _dispatch_keyboard_shortcut(self, event) -> None:
        """Execute one shortcut with command-specific playback restrictions."""
        key = event.key()

        if key == Qt.Key_F1:
            self._show_keyboard_shortcuts()
            return
        if key == Qt.Key_O:
            self.open_video_data_folder()
            return
        if key == Qt.Key_Space:
            if self.video.is_ready:
                self._toggle_play_pause_shortcut()
            return
        if key == Qt.Key_M:
            self._toggle_marker_mode()
            return

        if not self.video.is_ready or not self.sync.has_data:
            return

        # Graph signal and axis changes are safe during native playback.
        if key in (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4):
            signal_index = {
                Qt.Key_1: 1,
                Qt.Key_2: 2,
                Qt.Key_3: 3,
                Qt.Key_4: 4,
            }[key]
            self._change_signal_preserve_zoom(signal_index)
            return
        if key == Qt.Key_T:
            self._change_x_axis_preserve_zoom("time")
            return
        if key == Qt.Key_X:
            self._change_x_axis_preserve_zoom("position")
            return

        # Rotation is rendered by the exact PyAV still-frame path, not by the
        # native QVideoWidget. Pause first so the requested rotation is visible
        # immediately and the synchronized frame is preserved.
        if key == Qt.Key_R:
            if self.playing or self.pending_play_start or self.playback_warmup:
                self.pause()
            self.rotate_video_90()
            return

        if key == Qt.Key_Escape:
            if self.graph_sync_mode:
                self._exit_graph_sync_mode("Graph alignment mode exited.")
                return
            try:
                if self.drawing.cancel_current_drawing():
                    self._set_tool_active(None)
            except Exception:
                pass
            return

        if self.graph_sync_mode:
            if event.isAutoRepeat():
                return
            if key in (Qt.Key_A, Qt.Key_Left):
                self._graph_sync_nudge(-1)
            elif key in (Qt.Key_D, Qt.Key_Right):
                self._graph_sync_nudge(+1)
            elif key in (Qt.Key_S, Qt.Key_Down):
                self._graph_sync_nudge(-10)
            elif key in (Qt.Key_W, Qt.Key_Up):
                self._graph_sync_nudge(+10)
            return

        # Frame stepping and drawing deletion remain paused-only operations.
        if self.playing or self.pending_play_start or self.playback_warmup:
            return

        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            try:
                self.drawing.delete_selected_shape()
            except Exception:
                pass
            return

        if event.isAutoRepeat():
            return
        if key in (Qt.Key_A, Qt.Key_Left):
            self._start_step_key(key, -1)
        elif key in (Qt.Key_D, Qt.Key_Right):
            self._start_step_key(key, +1)
        elif key in (Qt.Key_S, Qt.Key_Down):
            self._start_step_key(key, -10)
        elif key in (Qt.Key_W, Qt.Key_Up):
            self._start_step_key(key, +10)

    def _reset_current_view(self):
        if self.graph_sync_mode:
            self._exit_graph_sync_mode()
        self.frame_index = 0
        self.video_panel.clear_still_image()
        self.drawing.clear_drawings()
        self.progress_bar.setVisible(False)
        self.marker_mode = False
        self.pending_play_start = False
        self.playback_warmup = False
        self.playing = False
        self.selection_region.setVisible(False)
        self.selection_region.setMovable(False)
        self.selection_label.setVisible(False)
        self._set_button_active(self.region_btn, False)

    def _refresh_plot_data(self, reset_view=False):
        self.plot_item.setData(self.sync.x_axis_data, self.sync.current_signal)
        self.plot.setLabel("bottom", self.sync.x_axis_label)
        self.plot.setLabel("left", self.sync.current_signal_name)
        if reset_view and self.sync.has_data:
            self.plot.autoRange()

    def _reload_video_rotation_from_sync_meta(self):
        try:
            sync_file = self.video.cache_dir / "sync_meta.json"
            if not sync_file.exists():
                return
            data = _read_json(sync_file)
            self.video.sync_rotation = int(data.get("rotation", getattr(self.video, "sync_rotation", 0)) or 0) % 360
            self.video._still_image_cache.clear()
            self.video._still_pixmap_cache.clear()
        except Exception:
            pass

    def _load_frames_then_sync(self):
        self.progress_bar.setVisible(True)
        def prog(v, m):
            self.progress_bar.setValue(max(0, min(100, int(v))))
            self.status_label.setText(m)
            QApplication.processEvents()
        self.video.load_frames_from_cache(progress_callback=prog)
        self.status_label.setText("Determining synchronization...")
        QApplication.processEvents()
        sync_frame, saved_offset = self._get_or_create_active_sync()
        self._reload_video_rotation_from_sync_meta()
        self.sync.set_video_offset(sync_frame)
        if self.current_run_identity is not None:
            self._initialize_shared_sync_offset(saved_offset)
            self._apply_shared_sync_to_active_camera()
        else:
            self.sync._user_offset_frames = saved_offset
        self.frame_index = max(0, min(sync_frame, self.video.frame_count - 1))
        self._sync_video_panel_aspect()
        self._show_still_frame(self.frame_index)
        self.video.set_position_frame(self.frame_index)
        self.video.update_cache_meta_rotation()
        self.progress_bar.setVisible(False)
        self._sync_axis_dropdowns()
        self._set_ready(True)
        self.status_label.setText(f"Ready: {Path(self.current_video_path).name} | {self.video.frame_count} frames @ {self.video.fps:.3f} FPS")
        self._restore_active_camera_drawings()
        self._rebuild_camera_strip()
        self._update_all_camera_previews()

        # Both run-loading paths finish here. Restore main-window keyboard focus
        # after Qt has shown and positioned the separate paused still cover.
        QTimer.singleShot(0, self._focus_loaded_still_image)
        QTimer.singleShot(120, self._focus_loaded_still_image)

    def _focus_loaded_still_image(self):
        """Enable shortcuts immediately while keeping the paused still on top."""
        if not self.video.is_ready:
            return

        try:
            QApplication.setActiveWindow(self)
            self.activateWindow()
            self.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

        # The still cover is a separate top-level window. Restack it after the
        # main window receives focus, without assigning keyboard focus to it.
        try:
            cover = self.video_panel.still_cover
            if cover.isVisible():
                cover.sync_to_owner()
                cover.raise_()
                cover.overlay.raise_()
        except Exception:
            pass

    def _schedule_player_park(self, delay_ms: int = 80):
        """
        While paused, keep QMediaPlayer/QVideoWidget parked on the same frame as
        the PyAV still cover.

        This prevents QVideoWidget from showing the old pause frame when Play is
        pressed after ASDW/plot seeking.
        """
        if not self.video.is_ready:
            return

        if self.playing or self.pending_play_start:
            return

        try:
            self.player_park_timer.start(int(delay_ms))
        except Exception:
            pass


    def _park_player_on_current_frame(self):
        """
        Seek QMediaPlayer underneath the still cover to the current paused frame.

        This is allowed while paused because frame_changed from QMediaPlayer should
        not drive marker/frame_index when self.playing is False.
        """
        if not self.video.is_ready:
            return

        if self.playing or self.pending_play_start:
            return

        try:
            self.video.set_position_frame(self.frame_index)
        except Exception:
            pass



    def _show_still_frame(self, frame_index):
        if not self.video.is_ready:
            return

        self._sync_video_panel_aspect()

        self.frame_index = max(
            0,
            min(
                int(frame_index),
                self.video.frame_count - 1,
            ),
        )

        img = self.video.get_frame_image(self.frame_index)

        if img is not None:
            self.video_panel.show_still_image(img)
            if self.active_camera_id is not None:
                detached = img.copy()
                self.camera_preview_images[self.active_camera_id] = detached
                widget = self.camera_thumbnail_widgets.get(self.active_camera_id)
                if widget is not None:
                    widget.set_image(detached)
                    widget.set_status("Active")

        self._update_marker()

        # Keep native QVideoWidget parked behind the still cover.
        # This prevents first visible playback frame from being the old pause frame.
        self._schedule_player_park(80)
        self._schedule_thumbnail_update(220)

    def _toggle_play_pause_shortcut(self):
        if not self.video.is_ready:
            return

        if (
            self.playing
            or self.pending_play_start
            or getattr(self, "playback_warmup", False)
            or getattr(self, "playback_startup_guard", False)
        ):
            self.pause()
        else:
            self.play()

    def play(self):
        if not self.video.is_ready or not self.sync.has_data:
            return
        self._suspend_thumbnail_updates()
        if self.pending_play_start or self.playback_warmup:
            return
        self._sync_video_panel_aspect()
        self.pending_play_start = True
        self.playback_warmup = False
        self.playing = False
        self.play_btn.setText("Pause")
        self._set_button_icon(self.play_btn, "pause.svg")

        try:
            self.player_park_timer.stop()
        except Exception:
            pass

        try:
            self.video.set_position_frame(self.frame_index)
        except Exception:
            pass


        self._start_playback_when_thumbnails_idle()

    def _start_playback_when_thumbnails_idle(self):
        if not self.pending_play_start or not self.video.is_ready:
            return
        if self._thumbnail_decoder.is_busy:
            QTimer.singleShot(16, self._start_playback_when_thumbnails_idle)
            return
        self.video.start_playback(self.frame_index, self.playback_speed)

    def _on_playback_started(self):
        if not self.pending_play_start:
            return
        self._suspend_thumbnail_updates()
        self.pending_play_start = False
        self.playing = True
        self.playback_warmup = not self.video.is_bcap
        self._playback_warmup_start_frame = int(self.frame_index)
        self.play_btn.setText("Pause")
        self._set_button_icon(self.play_btn, "pause.svg")
        if self.video.is_bcap:
            image = self.video.get_frame_image(self.frame_index)
            if image is not None:
                self.video_panel.show_still_image(image)
        else:
            self.video_panel.show_video()
        if self._restore_focus_after_camera_switch:
            QTimer.singleShot(0, self._restore_shortcut_focus_after_camera_switch)
            QTimer.singleShot(100, self._restore_shortcut_focus_after_camera_switch)
        if not self.video.is_bcap:
            QTimer.singleShot(int(self.playback_warmup_ms), self._finish_playback_warmup)

    def _restore_shortcut_focus_after_camera_switch(self):
        """Return all keyboard shortcuts to the main viewer during playback."""
        if not self.video.is_ready:
            return
        try:
            self.video.video_widget.clearFocus()
            self.video.video_widget.setFocusPolicy(Qt.NoFocus)
        except Exception:
            pass
        try:
            QApplication.setActiveWindow(self)
            self.activateWindow()
            self.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def _finish_playback_warmup(self):
        if not self.playing:
            self.playback_warmup = False
            return
        if not self.playback_warmup:
            return
        self.playback_warmup = False
        try:
            self.frame_index = max(0, min(int(self.video.current_frame()), self.video.frame_count - 1))
        except Exception:
            pass
        self._update_marker()
        if self._restore_focus_after_camera_switch:
            self._restore_shortcut_focus_after_camera_switch()
            QTimer.singleShot(80, self._restore_shortcut_focus_after_camera_switch)
            self._restore_focus_after_camera_switch = False

    def pause(self, *, update_camera_previews: bool = True):
        # Preserve the camera-switch resume flag while pausing the old active
        # camera. Clear it only for an ordinary pause from an already-idle state.
        if (
            not self.playing
            and not self.pending_play_start
            and not self.playback_warmup
        ):
            self._restore_focus_after_camera_switch = False
        if not self.video.is_ready:
            self.playing = False
            self.pending_play_start = False
            self.playback_warmup = False
            self.play_btn.setText("Play")
            self._set_button_icon(self.play_btn, "play.svg")
            return

        if self.pending_play_start:
            try:
                self.video.pause_playback()
            except Exception:
                pass
            self.pending_play_start = False
            self.playback_warmup = False
            self.playing = False
            self.play_btn.setText("Play")
            self._set_button_icon(self.play_btn, "play.svg")
            self._show_still_frame(self.frame_index)
            if update_camera_previews:
                self._update_all_camera_previews()
            return

        if self.playing or self.playback_warmup:
            try:
                paused_frame = self.video.current_frame()
            except Exception:
                paused_frame = self.frame_index

            self.playing = False
            self.pending_play_start = False
            self.playback_warmup = False
            self.play_btn.setText("Play")
            self._set_button_icon(self.play_btn, "play.svg")
            try:
                self.video.pause_playback()
            except Exception:
                pass

            self.frame_index = max(
                0,
                min(int(paused_frame), self.video.frame_count - 1),
            )
            self._show_still_frame(self.frame_index)
            if update_camera_previews:
                self._update_all_camera_previews()
            return

        self.playing = False
        self.pending_play_start = False
        self.playback_warmup = False
        self.play_btn.setText("Play")
        self._set_button_icon(self.play_btn, "play.svg")

    def cycle_playback_speed(self):
        was = self.playing
        self.playback_speed_index = (self.playback_speed_index + 1) % len(self.playback_speeds)
        self.playback_speed = self.playback_speeds[self.playback_speed_index]
        self.speed_btn.setText(f"Speed: x{self.playback_speed:g}")
        self.video.set_playback_rate(self.playback_speed)
        if was:
            self.play()

    def _set_ready(self, ready):
        self.open_folder_btn.setEnabled(True)
        self.play_btn.setEnabled(bool(ready))
        self.speed_btn.setEnabled(bool(ready))
        if not ready:
            self.playing = False
            self.pending_play_start = False
            self.playback_warmup = False
            self.play_btn.setText("Play")
            self._set_button_icon(self.play_btn, "play.svg")

    def _update_video_frame(self):
        self._show_still_frame(self.frame_index)

    def _on_video_frame_changed(self, frame):
        if self.video.is_bcap and self.playing:
            self.frame_index = int(frame)
            image = self.video.get_frame_image(self.frame_index)
            if image is not None:
                self.video_panel.show_still_image(image)
            self._update_marker()
            return
        if self.pending_play_start or self.playback_warmup:
            return
        if self.playing:
            self.frame_index = int(frame)
            self._update_marker()

    def _on_video_playback_finished(self):
        self.frame_index = max(0, self.video.frame_count - 1)
        self.pause()

    def _step_frame_once(self, delta):
        if self.pending_play_start or self.playback_warmup:
            return
        if not self.playing and self.video.is_ready and self.sync.has_data:
            self._show_still_frame(self.frame_index + int(delta))

    def _start_step_key(self, key, delta):
        if self.pending_play_start or self.playback_warmup:
            return
        self._held_step_key = key
        self._held_step_delta = int(delta)
        self._step_frame_once(delta)
        self.key_hold_delay_timer.start()

    def _begin_held_frame_step(self):
        if self._held_step_key is not None and not self.key_step_timer.isActive():
            self.key_step_timer.start()

    def _held_frame_step_tick(self):
        if self._held_step_key is not None:
            self._step_frame_once(self._held_step_delta)

    def _stop_step_key(self, key):
        if self._held_step_key == key:
            self._held_step_key = None
            self._held_step_delta = 0
            self.key_hold_delay_timer.stop()
            self.key_step_timer.stop()

    def _update_marker(self):
        if self.playing and QApplication.focusWidget() is None:
            QTimer.singleShot(0, self._restore_shortcut_focus_after_camera_switch)
        if not self.video.is_ready or not self.sync.has_data or self.sync.time_data is None or self.sync.time_data.size == 0:
            return
        t = self._video_frame_to_data_time(self.frame_index)
        idx = int(np.abs(self.sync.time_data - t).argmin())
        self.marker.setPos(float(self.sync.time_data[idx] if self.sync.x_mode == "time" else self.sync.position_data[idx]))
        if self.marker_mode:
            self._update_selection_stats()
        else:
            current_value = float(self.sync.current_signal[idx])
            self.selection_label.setText(
                f"{self.sync.current_signal_name}: {current_value:.3f}"
            )
            self.selection_label.adjustSize()
            self.selection_label.setVisible(True)
            self._position_selection_text()

    def _toggle_graph_sync_mode(self):
        if self.graph_sync_mode:
            self._exit_graph_sync_mode("Graph alignment cancelled.")
            return
        self._enter_graph_sync_mode()

    def _set_sync_controls_enabled(
        self,
        enabled: bool,
    ):
        """
        Sync adjustment controls are available only while Sync mode is active.
        """
        enabled = bool(enabled)

        self.sync_back_btn.setEnabled(enabled)
        self.sync_forward_btn.setEnabled(enabled)
        self.sync_reset_btn.setEnabled(enabled)
        self.save_sync_btn.setEnabled(enabled)


    def _enter_graph_sync_mode(self):
        if not self.video.is_ready or not self.sync.has_data:
            return

        if self.playing or self.pending_play_start or self.playback_warmup:
            self.pause()

        self.graph_sync_mode = True
        self.graph_sync_locked_frame = int(self.frame_index)
        self._set_button_active(self.graph_sync_btn, True)

        self._set_sync_controls_enabled(True)


        try:
            self.graph_sync_previous_cursor = self.plot.cursor()
            self.plot.setCursor(Qt.CrossCursor)
        except Exception:
            self.graph_sync_previous_cursor = None

        self.status_label.setText(
            "Graph alignment: click the graph point matching the paused video. "
            "Then use A/Left or D/Right for visual left/right by 1 frame, and S/Down or W/Up by 10 frames. "
            "Escape or Align graph exits."
        )

    def _exit_graph_sync_mode(self, status_text: str | None = None):
        self.graph_sync_mode = False
        self.graph_sync_locked_frame = None
        self._set_button_active(self.graph_sync_btn, False)
        self._set_sync_controls_enabled(False)
        try:
            if self.graph_sync_previous_cursor is not None:
                self.plot.setCursor(self.graph_sync_previous_cursor)
            else:
                self.plot.unsetCursor()
        except Exception:
            pass
        self.graph_sync_previous_cursor = None
        if status_text:
            self.status_label.setText(status_text)

    def _align_locked_video_frame_to_data_time(self, selected_data_time: float):
        if not self.graph_sync_mode or self.graph_sync_locked_frame is None:
            return

        selected_data_time = float(selected_data_time)
        paused_frame = int(self.graph_sync_locked_frame)
        paused_media_time = float(self.video.frame_to_time(paused_frame))
        base_frame = int(getattr(self.sync, "_base_offset_frames", 0) or 0)
        base_media_time = float(self.video.frame_to_time(base_frame))
        required_zero_media_time = paused_media_time - selected_data_time

        if self.current_run_identity is not None:
            self.shared_sync_offset_seconds = (
                required_zero_media_time - base_media_time
            )
            self._apply_shared_sync_to_active_camera()
        else:
            required_zero_frame = int(
                self.video.time_to_frame(required_zero_media_time)
            )
            self.sync._user_offset_frames = required_zero_frame - base_frame

        self.frame_index = paused_frame
        self._update_marker()
        self._schedule_thumbnail_update(80)
        self.status_label.setText(
            "Graph aligned at data time "
            f"{selected_data_time:.3f} s. "
            "Fine tune visually with A/Left and D/Right (1 frame), or S/Down and W/Up (10 frames), "
            "then press Save sync. Escape exits alignment mode."
        )


    def _sync_button_nudge(self, delta_frames: int):
        """
        Apply a synchronization adjustment from the Sync - / Sync + buttons.

        In graph alignment mode, keep the paused video frame locked.
        Outside graph alignment mode, retain the normal sync-button behavior.
        """
        if self.graph_sync_mode:
            self._graph_sync_nudge(delta_frames)
            return

        self._adjust_sync(delta_frames)


    def _graph_sync_nudge(self, visual_delta_frames: int):
        """Move the graph cursor in the same visual direction as the key press.

        Increasing the stored sync offset moves the mapped data cursor left,
        because data_time = video_time - sync_zero_time. Therefore a visual
        move to the right requires the opposite sync-offset adjustment.
        """
        if not self.graph_sync_mode or self.graph_sync_locked_frame is None:
            return
        self._adjust_sync(-int(visual_delta_frames))
        self.frame_index = int(self.graph_sync_locked_frame)
        self._update_marker()

    def on_plot_click(self, event):
        if not self.sync.has_data:
            return
        x = float(self.plot.plotItem.vb.mapSceneToView(event.scenePos()).x())
        if self.graph_sync_mode:
            clicked_data_time = float(self.sync.x_value_to_time(x))
            # Snap to the nearest real 200 Hz data sample for deterministic alignment.
            if self.sync.time_data.size:
                sample_index = int(
                    np.abs(self.sync.time_data - clicked_data_time).argmin()
                )
                clicked_data_time = float(self.sync.time_data[sample_index])
            self._align_locked_video_frame_to_data_time(clicked_data_time)
            return
        if self.marker_mode:
            self.selection_region.blockSignals(True)
            self.selection_region.setRegion((x, x))
            self.selection_region.setVisible(True)
            self.selection_region.setMovable(True)
            self.selection_region.blockSignals(False)
            self._update_selection_stats()
            return
        if self.playing or self.pending_play_start or self.playback_warmup or not self.video.is_ready:
            return
        target = self._data_time_to_video_frame(self.sync.x_value_to_time(x))
        self.frame_index = max(0, min(target, self.video.frame_count - 1))
        self._update_video_frame()

    def _update_selection_stats(self):
        if not self.sync.has_data:
            return
        x0, x1 = self.selection_region.getRegion()
        x_min, x_max = sorted((float(x0), float(x1)))
        x_data = self.sync.x_axis_data
        y_data = self.sync.current_signal
        if x_data.size == 0 or y_data.size == 0:
            return
        t = self._video_frame_to_data_time(self.frame_index)
        current_index = int(np.abs(self.sync.time_data - t).argmin())
        current_value = float(y_data[current_index])
        mask = (x_data >= x_min) & (x_data <= x_max)
        if not mask.any():
            mid = (x_min + x_max) / 2.0
            idx = int(np.abs(x_data - mid).argmin())
            avg = float(np.mean(np.atleast_1d(y_data[idx])))
            delta = 0.0
        else:
            avg = float(np.mean(y_data[mask]))
            delta = x_max - x_min
        delta_label = f"Δt = {delta:.3f} s" if self.sync.x_mode == "time" else f"Δd = {delta:.3f} m"
        self.selection_label.setText(
            f"{self.sync.current_signal_name}: {current_value:.3f}\n"
            f"Mean ({self.sync.current_signal_name}) = {avg:.3f}\n"
            f"{delta_label}"
        )
        self.selection_label.adjustSize()
        self.selection_label.setVisible(True)
        try:
            self.selection_label.raise_()
        except Exception:
            pass
        self._position_selection_text()

    def _position_selection_text(self):
        if not hasattr(self, "selection_label") or not self.selection_label.isVisible():
            return
        margin = 10
        self.selection_label.adjustSize()
        x = self.plot.width() - self.selection_label.width() - margin
        y = margin
        self.selection_label.move(int(x), int(y))

    def _save_sync_offset(self):
        self._persist_shared_sync_to_all_cameras()
        saved_status = (
            self._shared_sync_status_text()
            if self.current_run_identity is not None
            else f"Sync offset saved: {self.sync.user_offset:+d} frames"
        )
        if self.graph_sync_mode:
            self._exit_graph_sync_mode(saved_status)
        else:
            self.status_label.setText(saved_status)
        self._schedule_thumbnail_update(80)

    def _adjust_sync(self, d):
        if not self.video.is_ready:
            return

        if self.current_run_identity is None:
            self.sync.adjust_offset(d)
        else:
            if self.shared_sync_offset_seconds is None:
                self._initialize_shared_sync_offset(self.sync.user_offset)
            fps = float(self.video.fps or 0.0)
            if fps <= 0:
                return
            reference = int(
                self.graph_sync_locked_frame
                if self.graph_sync_mode and self.graph_sync_locked_frame is not None
                else self.frame_index
            )
            target = max(
                0,
                min(reference + int(d), self.video.frame_count - 1),
            )
            delta_seconds = float(
                self.video.frame_to_time(target)
                - self.video.frame_to_time(reference)
            )
            if delta_seconds == 0.0:
                delta_seconds = int(d) / fps
            self.shared_sync_offset_seconds = float(
                self.shared_sync_offset_seconds or 0.0
            ) + delta_seconds
            self._apply_shared_sync_to_active_camera()

        self._update_marker()
        self.status_label.setText(
            self._shared_sync_status_text()
            if self.current_run_identity is not None
            else f"Sync offset: {self.sync.user_offset:+d} frames"
        )
        self._schedule_thumbnail_update(80)

    def _reset_sync(self):
        if not self.video.is_ready:
            return

        if self.current_run_identity is None:
            self.sync.reset_offset()
        else:
            self.shared_sync_offset_seconds = 0.0
            self._apply_shared_sync_to_active_camera()

        self._update_marker()
        self.status_label.setText(
            self._shared_sync_status_text()
            if self.current_run_identity is not None
            else "Sync offset reset."
        )
        self._schedule_thumbnail_update(80)

    def _clear_cache_clicked(self):
        """
        Clear the active video's cache and detach that video from the current run.

        Other camera angles attached to the run remain untouched.
        """
        video_name = (
            Path(self.current_video_path).name
            if self.current_video_path
            else "video"
        )

        run_identity = self.current_run_identity
        camera_id = self.active_camera_id

        # Stop playback and secondary preview activity before deleting anything.
        try:
            self.pause()
        except Exception:
            pass

        try:
            self._suspend_thumbnail_updates()
        except Exception:
            pass

        # Save no temporary drawing state for the video being removed.
        if camera_id:
            self.camera_drawings.pop(
                str(camera_id),
                None,
            )

        # Delete cache_meta.json, sync_meta.json, timestamp index, and other
        # per-video cache files.
        try:
            self.video.clear_cache_frames()
        except Exception as exc:
            self._show_error(
                "Could not clear video cache",
                str(exc),
            )
            return

        # Remove the run-to-video association from run_meta.json.
        if run_identity is not None and camera_id:
            try:
                self.run_library.remove_video_angle(
                    run_identity,
                    str(camera_id),
                )
            except Exception as exc:
                self._show_error(
                    "Video cache was cleared, but the run attachment "
                    "could not be removed",
                    str(exc),
                )
                return

        # Reset the active video backend and UI state.
        try:
            self.video.reset()
        except Exception:
            pass

        self.current_video_path = None
        self.active_camera_id = None
        self.camera_views = []

        try:
            self._reset_current_view()
        except Exception:
            pass

        # Reload the run metadata after removing the active camera.
        remaining_videos = []

        if run_identity is not None:
            try:
                remaining_videos = (
                    self.run_library.get_videos(
                        run_identity
                    )
                )
            except Exception:
                remaining_videos = []

        if remaining_videos:
            # remove_video_angle() automatically assigns a new primary/active camera
            # when the previous active camera is removed. Load that remaining camera
            # through the normal run-loading path so the backend, still image, sync,
            # drawings, previews, and keyboard focus are all restored correctly.
            try:
                self.current_run_identity = run_identity
                self._load_selected_run()

                remaining_count = len(
                    self.camera_views
                )

                self.status_label.setText(
                    f"Cleared cache and detached {video_name}. "
                    f"Switched to the remaining camera. "
                    f"{remaining_count} camera angle(s) remain attached."
                )

            except Exception as exc:
                self._set_ready(False)
                self._refresh_current_run_visuals()

                self._show_error(
                    "The video was detached, but the remaining "
                    "camera could not be loaded",
                    str(exc),
                )

                return

        else:
            # No attached cameras remain.
            self.camera_views = []
            self.active_camera_id = None
            self.current_video_path = None

            self._rebuild_camera_strip()
            self._set_ready(False)
            self._refresh_current_run_visuals()

            self.status_label.setText(
                f"Cleared cache and detached {video_name}. "
                "The run now has no attached video."
            )

    def _save_viewbox_state(self):
        vb = self.plot.getViewBox()
        state = {}

        try:
            vr = vb.viewRange()
            state["viewRange"] = (
                [float(vr[0][0]), float(vr[0][1])],
                [float(vr[1][0]), float(vr[1][1])],
            )
        except Exception:
            state["viewRange"] = None

        try:
            state["mouseEnabled"] = tuple(
                bool(v) for v in vb.state["mouseEnabled"]
            )
        except Exception:
            state["mouseEnabled"] = (True, True)

        try:
            state["autoRange"] = tuple(
                bool(v) for v in vb.state["autoRange"]
            )
        except Exception:
            state["autoRange"] = (False, False)

        self._vb_state = state

    def _restore_viewbox_state(self):
        vb = self.plot.getViewBox()
        state = getattr(self, "_vb_state", None)

        if not state:
            # Safety fallback: never leave graph locked.
            try:
                vb.setMouseEnabled(x=True, y=True)
            except Exception:
                pass
            return

        try:
            vb.blockSignals(True)
        except Exception:
            pass

        me = state.get("mouseEnabled")
        if me is not None:
            try:
                vb.setMouseEnabled(
                    x=bool(me[0]),
                    y=bool(me[1]),
                )
            except Exception:
                pass

        ar = state.get("autoRange")
        if ar is not None:
            try:
                vb.enableAutoRange(
                    x=bool(ar[0]),
                    y=bool(ar[1]),
                )
            except Exception:
                pass

        vr = state.get("viewRange")
        if vr:
            try:
                vb.setRange(
                    xRange=tuple(vr[0]),
                    yRange=tuple(vr[1]),
                    padding=0,
                )
            except Exception:
                pass

        try:
            vb.blockSignals(False)
        except Exception:
            pass

    def _toggle_marker_mode(self):
        if not self.sync.has_data:
            return
        if not self.marker_mode:
            self._save_viewbox_state()
            vb = self.plot.getViewBox()
            try:
                vb.disableAutoRange()
                vb.setMouseEnabled(x=False, y=False)
            except Exception:
                pass
            try:
                if self._saved_selection_region is not None:
                    a, b = self._saved_selection_region
                else:
                    vr = vb.viewRange()
                    x_min, x_max = float(vr[0][0]), float(vr[0][1])
                    width = max((x_max - x_min) * 0.02, 1e-9)
                    mid = (x_min + x_max) / 2.0
                    a, b = mid - width / 2.0, mid + width / 2.0
                self.selection_region.setRegion((a, b))
            except Exception:
                self.selection_region.setRegion((0, 0))
            self.selection_region.setVisible(True)
            self.selection_region.setMovable(True)
            self.marker_mode = True
            self._set_button_active(self.region_btn, True)
            self._update_selection_stats()
        else:
            try:
                r = self.selection_region.getRegion()
                self._saved_selection_region = (float(r[0]), float(r[1]))
            except Exception:
                pass
            self.selection_region.setVisible(False)
            self.selection_region.setMovable(False)
            self._restore_viewbox_state()


            try:
                self.plot.getViewBox().setMouseEnabled(x=True, y=True)
            except Exception:
                pass


            self.marker_mode = False
            self._vb_state = None
            self._set_button_active(self.region_btn, False)
            self._update_marker()

    def _sync_axis_dropdowns(self):
        """
        Synchronize both dropdowns with the current SyncEngine state.
        """
        try:
            signal_index = int(getattr(self.sync, "current_signal_index", 1))
        except (TypeError, ValueError):
            signal_index = 1

        y_combo_index = self.y_axis_combo.findData(signal_index)

        if y_combo_index >= 0:
            self.y_axis_combo.blockSignals(True)

            try:
                self.y_axis_combo.setCurrentIndex(y_combo_index)
            finally:
                self.y_axis_combo.blockSignals(False)

        x_mode = str(getattr(self.sync, "x_mode", "time"))
        x_combo_index = self.x_axis_combo.findData(x_mode)

        if x_combo_index >= 0:
            self.x_axis_combo.blockSignals(True)

            try:
                self.x_axis_combo.setCurrentIndex(x_combo_index)
            finally:
                self.x_axis_combo.blockSignals(False)



    def _on_y_axis_combo_changed(self, combo_index):
        """
        Change the graph's Y-axis signal when the Y dropdown changes.
        """
        if combo_index < 0:
            return

        signal_index = self.y_axis_combo.itemData(combo_index)

        if signal_index is None:
            return

        try:
            signal_index = int(signal_index)
        except (TypeError, ValueError):
            return

        if not self.sync.has_data:
            return

        self._change_signal_preserve_zoom(signal_index)


    def _on_x_axis_combo_changed(self, combo_index):
        """
        Change the graph's X-axis when the X dropdown changes.
        """
        if combo_index < 0:
            return

        mode = self.x_axis_combo.itemData(combo_index)

        if mode not in ("time", "position"):
            return

        if not self.sync.has_data:
            return

        self._change_x_axis_preserve_zoom(str(mode))



    def _change_signal_preserve_zoom(self, i):
        signal_index = int(i)

        self.sync.set_signal(signal_index)
        self._refresh_plot_data(reset_view=False)
        self._rescale_y_to_visible_x()

        # Keep the dropdown synchronized when the signal is changed using
        # keyboard shortcuts 1, 2, 3, or 4.
        combo_index = self.y_axis_combo.findData(signal_index)

        if combo_index >= 0 and combo_index != self.y_axis_combo.currentIndex():
            self.y_axis_combo.blockSignals(True)

            try:
                self.y_axis_combo.setCurrentIndex(combo_index)
            finally:
                self.y_axis_combo.blockSignals(False)

        if self.selection_region.isVisible():
            self._update_selection_stats()

        self._update_marker()

    def _rescale_y_to_visible_x(self):
        x_data = np.asarray(self.sync.x_axis_data, dtype=float)
        y_data = np.asarray(self.sync.current_signal, dtype=float)
        if x_data.size == 0 or y_data.size == 0:
            return

        x_range = self.plot.getViewBox().viewRange()[0]
        x_min, x_max = sorted((float(x_range[0]), float(x_range[1])))
        visible = (
            np.isfinite(x_data)
            & np.isfinite(y_data)
            & (x_data >= x_min)
            & (x_data <= x_max)
        )
        if not visible.any():
            valid = np.isfinite(x_data) & np.isfinite(y_data)
            if not valid.any():
                return
            valid_indices = np.flatnonzero(valid)
            midpoint = (x_min + x_max) / 2.0
            nearest = valid_indices[
                int(np.abs(x_data[valid] - midpoint).argmin())
            ]
            visible[nearest] = True

        visible_values = y_data[visible]
        y_min = float(visible_values.min())
        y_max = float(visible_values.max())
        padding = (y_max - y_min) * 0.05
        if padding == 0:
            padding = max(abs(y_min) * 0.05, 0.01)
        self.plot.setYRange(y_min - padding, y_max + padding, padding=0)

    def _x_to_time_for_mode(self, x_value, mode):
        if mode == "time":
            return float(x_value)
        position = np.asarray(self.sync.position_data, dtype=float)
        time = np.asarray(self.sync.time_data, dtype=float)
        order = np.argsort(position)
        return float(np.interp(float(x_value), position[order], time[order]))

    def _time_to_x_for_mode(self, time_value, mode):
        if mode == "time":
            return float(time_value)
        position = np.asarray(self.sync.position_data, dtype=float)
        time = np.asarray(self.sync.time_data, dtype=float)
        order = np.argsort(time)
        return float(np.interp(float(time_value), time[order], position[order]))

    def _change_x_axis_preserve_zoom(self, mode):
        old_mode = self.sync.x_mode
        if old_mode == mode:
            return
        region = self.selection_region.getRegion() if self.selection_region.isVisible() else None
        self.sync.set_x_axis(mode)
        self._refresh_plot_data(reset_view=False)
        if region is not None:
            r0, r1 = region
            t0 = self._x_to_time_for_mode(r0, old_mode)
            t1 = self._x_to_time_for_mode(r1, old_mode)
            nr0 = self._time_to_x_for_mode(t0, mode)
            nr1 = self._time_to_x_for_mode(t1, mode)
            self.selection_region.blockSignals(True)
            self.selection_region.setRegion((nr0, nr1))
            self.selection_region.blockSignals(False)
        self._update_marker()
        if self.selection_region.isVisible():
            self._update_selection_stats()

        # Keep the dropdown synchronized when T or X changes the axis.
        combo_index = self.x_axis_combo.findData(mode)

        if combo_index >= 0 and combo_index != self.x_axis_combo.currentIndex():
            self.x_axis_combo.blockSignals(True)

            try:
                self.x_axis_combo.setCurrentIndex(combo_index)
            finally:
                self.x_axis_combo.blockSignals(False)

    def rotate_video_90(self):
        if not self.video.video_path:
            return
        self.video.sync_rotation = (int(self.video.sync_rotation or 0) + 90) % 360
        self.video.update_cache_meta_rotation()
        self._sync_video_panel_aspect()
        if self.video.is_ready and not self.playing and not self.pending_play_start and not self.playback_warmup:
            self._show_still_frame(self.frame_index)
        self._schedule_thumbnail_update(80)

    def _select_draw_line(self):
        self.drawing.set_mode(self.drawing.MODE_LINE)
        self._set_tool_active(self.draw_line_btn)

    def _select_draw_angle(self):
        self.drawing.set_mode(self.drawing.MODE_ANGLE)
        self._set_tool_active(self.draw_angle_btn)

    def _clear_drawings(self):
        self.drawing.clear_drawings()
        self._set_tool_active(None)

    def _on_draw_finished(self):
        self._set_tool_active(None)

    def keyPressEvent(self, event):
        self._dispatch_keyboard_shortcut(event)
        event.accept()

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key_A, Qt.Key_D, Qt.Key_S, Qt.Key_W, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and not event.isAutoRepeat():
            self._stop_step_key(event.key())
            event.accept()
            return
        super().keyReleaseEvent(event)


    def _show_keyboard_shortcuts(self):
        shortcuts = (
            "<h2>Keyboard shortcuts</h2>"

            "<h3>Playback and frame navigation</h3>"
            "<p>"
            "<b>Space</b> &nbsp; Play or pause the video<br>"
            "<b>A / Left arrow</b> &nbsp; Previous frame<br>"
            "<b>D / Right arrow</b> &nbsp; Next frame<br>"
            "<b>S / Down arrow</b> &nbsp; Move back 10 frames<br>"
            "<b>W / Up arrow</b> &nbsp; Move forward 10 frames"
            "</p>"

            "<h3>Drawing</h3>"
            "<p>"
            "<b>Delete / Backspace</b> &nbsp; Delete selected drawing<br>"
            "<b>Escape</b> &nbsp; Cancel the active drawing tool"
            "</p>"

            "<h3>Graph and analysis</h3>"
            "<p>"
            "<b>M</b> &nbsp; Toggle region selection<br>"
            "<b>1, 2, 3, 4</b> &nbsp; Select data signal<br>"
            "<b>T</b> &nbsp; Use time for the horizontal axis<br>"
            "<b>X</b> &nbsp; Use position for the horizontal axis"
            "</p>"

            "<h3>Video and files</h3>"
            "<p>"
            "<b>R</b> &nbsp; Rotate the video 90 degrees<br>"
            "<b>O</b> &nbsp; Open a video and data folder<br>"
            "<b>F1</b> &nbsp; Open this shortcut guide"
            "</p>"
        )

        # Parent the Help dialog to the still cover while it is visible. This keeps
        # the dialog above the cover without hiding the paused frame underneath.
        parent = self

        try:
            cover = self.video_panel.still_cover

            if cover.isVisible():
                parent = cover
        except Exception:
            cover = None

        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Keyboard shortcuts")
        box.setText(shortcuts)
        box.setTextFormat(Qt.RichText)
        box.setStandardButtons(QMessageBox.Ok)
        box.setWindowModality(Qt.ApplicationModal)

        # Keep the Help dialog above the separate top-level still-cover window.
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        box.show()
        box.raise_()
        box.activateWindow()
        box.exec()

        # Restore normal focus and cover stacking after closing Help.
        try:
            self.activateWindow()
            self.setFocus(Qt.OtherFocusReason)

            if cover is not None and cover.isVisible():
                cover.sync_to_owner()
                cover.raise_()
                cover.overlay.raise_()
        except Exception:
            pass


    def _show_error(self, title, msg):
        self._show_critical(title, msg)
