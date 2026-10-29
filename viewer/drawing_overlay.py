from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


@dataclass
class DrawingShape:
    kind: str
    points: list[QPointF]


class StillFrameWidget(QWidget):
    """
    Paints the paused/still PyAV frame.

    Used by StillCoverWidget. QVideoWidget remains mounted underneath to avoid
    native video surface cold-start lag when pressing Play after Pause.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("stillFrameWidget")
        self._image: QImage | None = None
        self._aspect_width: int | None = None
        self._aspect_height: int | None = None
        self.setMinimumSize(320, 240)

    def set_aspect_size(self, width: int, height: int) -> None:
        self._aspect_width = int(width) if width and width > 0 else None
        self._aspect_height = int(height) if height and height > 0 else None
        self.update()

    def set_image(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            self._image = None
            self._aspect_width = None
            self._aspect_height = None
        else:
            self._image = image.copy()
            self._aspect_width = image.width()
            self._aspect_height = image.height()
        self.update()

    def clear_image(self) -> None:
        self._image = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0f14"))

        if self._image is None or self._image.isNull():
            return

        image_w = self._image.width()
        image_h = self._image.height()
        aspect_w = self._aspect_width or image_w
        aspect_h = self._aspect_height or image_h
        outer_w = self.width()
        outer_h = self.height()

        if aspect_w <= 0 or aspect_h <= 0 or outer_w <= 0 or outer_h <= 0:
            return

        scale = min(outer_w / aspect_w, outer_h / aspect_h)
        target_w = aspect_w * scale
        target_h = aspect_h * scale
        target_x = (outer_w - target_w) / 2.0
        target_y = (outer_h - target_h) / 2.0

        painter.drawImage(QRectF(target_x, target_y, target_w, target_h), self._image)


class DrawingOverlayWidget(QWidget):
    MODE_NONE = "none"
    MODE_LINE = "line"
    MODE_ANGLE = "angle"

    drawing_changed = Signal()
    space_pressed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("drawingOverlay")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.enabled = False
        self.mode = self.MODE_NONE
        self.on_mode_finished: Callable[[], None] | None = None

        self.pending_points: list[QPointF] = []
        self.preview_pos: QPointF | None = None
        self.shapes: list[DrawingShape] = []

        self.active_shape: DrawingShape | None = None
        self.active_handle: int | None = None
        self.selected_shape: DrawingShape | None = None
        self.dragging_whole = False
        self.last_mouse: QPointF | None = None

        self.handle_radius = 7.0
        self.hit_radius = 12.0

        self.pen_final = QPen(QColor(255, 215, 0), 2, Qt.DashLine)
        self.pen_preview = QPen(QColor(255, 215, 0, 215), 2, Qt.DashLine)

        # Slightly stronger visual style for selected line/angle.
        self.pen_selected = QPen(QColor(255, 165, 0), 3, Qt.DashLine)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled:
            self.setCursor(Qt.CrossCursor)
            self.setFocus()
        else:
            self.unsetCursor()
            self.pending_points.clear()
            self.preview_pos = None
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode not in {self.MODE_NONE, self.MODE_LINE, self.MODE_ANGLE}:
            return
        self.mode = mode
        self.pending_points.clear()
        self.preview_pos = None
        self.set_enabled(mode != self.MODE_NONE)

    def cancel_current_drawing(self) -> bool:
        """Cancel the active tool and discard only its unfinished points."""
        drawing_was_active = (
            self.mode != self.MODE_NONE
            or bool(self.pending_points)
        )
        if not drawing_was_active:
            return False
        self.pending_points.clear()
        self.preview_pos = None
        self.set_mode(self.MODE_NONE)
        if callable(self.on_mode_finished):
            self.on_mode_finished()
        self.update()
        return True

    def export_shapes(self) -> list[dict]:
        """Return camera-local drawings as plain serializable coordinates."""
        return [
            {
                "kind": shape.kind,
                "points": [[float(point.x()), float(point.y())] for point in shape.points],
            }
            for shape in self.shapes
        ]

    def import_shapes(self, values) -> None:
        """Replace visible drawings with a previously exported camera state."""
        shapes: list[DrawingShape] = []
        for value in values or []:
            if not isinstance(value, dict):
                continue
            kind = str(value.get("kind") or "")
            if kind not in {"line", "angle"}:
                continue
            points = []
            for pair in value.get("points") or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    points.append(QPointF(float(pair[0]), float(pair[1])))
            required = 2 if kind == "line" else 3
            if len(points) >= required:
                shapes.append(DrawingShape(kind, points[:required]))
        self.shapes = shapes
        self.pending_points.clear()
        self.preview_pos = None
        self.selected_shape = None
        self.active_shape = None
        self.active_handle = None
        self.dragging_whole = False
        self.set_mode(self.MODE_NONE)
        self.update()

    def clear_drawings(self) -> None:
        self.shapes.clear()
        self.pending_points.clear()
        self.preview_pos = None
        self.active_shape = None
        self.active_handle = None
        self.selected_shape = None
        self.dragging_whole = False
        self.update()
        self.drawing_changed.emit()


    def delete_selected_shape(self) -> bool:
        """
        Delete the currently selected line/angle.

        Returns True if something was deleted, otherwise False.
        """
        if self.selected_shape is None:
            return False

        try:
            self.shapes.remove(self.selected_shape)
        except ValueError:
            self.selected_shape = None
            self.update()
            return False

        self.selected_shape = None
        self.active_shape = None
        self.active_handle = None
        self.dragging_whole = False
        self.pending_points.clear()
        self.preview_pos = None

        self.update()
        self.drawing_changed.emit()
        return True


    def deselect_shape(self) -> None:
        self.selected_shape = None
        self.update()

    def mousePressEvent(self, event):
        pos = QPointF(event.position())
        
        try:
            self.setFocus(Qt.MouseFocusReason)
            self.window().activateWindow()
        except Exception:
            pass

        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        if self.enabled and self.mode in {self.MODE_LINE, self.MODE_ANGLE}:
            self._tool_click(pos)
            event.accept()
            return

        shape, handle = self._hit_test_handle(pos)
        if shape is not None and handle is not None:
            self.selected_shape = shape
            self.active_shape = shape
            self.active_handle = handle
            self.dragging_whole = False
            self.last_mouse = pos
            self.setCursor(Qt.SizeAllCursor)
            self.setFocus()
            self.update()
            event.accept()
            return

        shape = self._hit_test_shape(pos)
        if shape is not None:
            self.selected_shape = shape
            self.active_shape = shape
            self.active_handle = None
            self.dragging_whole = True
            self.last_mouse = pos
            self.setCursor(Qt.SizeAllCursor)
            self.setFocus()
            self.update()
            event.accept()
            return
        

        self.selected_shape = None
        self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())

        if self.enabled and self.mode != self.MODE_NONE:
            self.preview_pos = pos
            self.update()
            event.accept()
            return

        if self.active_shape is not None and self.last_mouse is not None:
            delta = pos - self.last_mouse
            self.last_mouse = pos

            if self.dragging_whole:
                self.active_shape.points = [p + delta for p in self.active_shape.points]
            elif self.active_handle is not None:
                self.active_shape.points[self.active_handle] = pos

            self.update()
            self.drawing_changed.emit()
            event.accept()
            return

        if self._hit_test_handle(pos)[0] is not None or self._hit_test_shape(pos) is not None:
            self.setCursor(Qt.SizeAllCursor)
        elif not self.enabled:
            self.unsetCursor()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.active_shape = None
        self.active_handle = None
        self.dragging_whole = False
        self.last_mouse = None
        if not self.enabled:
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.pending_points.clear()
            self.preview_pos = None
            self.set_mode(self.MODE_NONE)
            self.update()
            event.accept()
            return

        if event.key() == Qt.Key_Space:
            self.space_pressed.emit()
            event.accept()
            return

        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            try:
                if self.delete_selected_shape():
                    event.accept()
                    return
            except Exception:
                pass

        super().keyPressEvent(event)


    def _tool_click(self, pos: QPointF) -> None:
        self.pending_points.append(pos)
        if self.mode == self.MODE_LINE and len(self.pending_points) == 2:
            self.shapes.append(DrawingShape("line", list(self.pending_points)))
            self._finish_tool()
        elif self.mode == self.MODE_ANGLE and len(self.pending_points) == 3:
            self.shapes.append(DrawingShape("angle", list(self.pending_points)))
            self._finish_tool()
        self.update()

    def _finish_tool(self) -> None:
        self.pending_points.clear()
        self.preview_pos = None
        self.set_mode(self.MODE_NONE)
        self.drawing_changed.emit()

        # Keep keyboard focus on the paused video overlay after drawing.
        # This makes Space/Delete work immediately after completing a line/angle.
        try:
            self.setFocus(Qt.OtherFocusReason)
            self.window().activateWindow()
        except Exception:
            pass

        if callable(self.on_mode_finished):
            self.on_mode_finished()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for shape in self.shapes:
            self._paint_shape(painter, shape)
        self._paint_pending(painter)


    def _paint_shape(self, painter: QPainter, shape: DrawingShape) -> None:
        painter.setPen(self.pen_selected if shape is self.selected_shape else self.pen_final)

        if shape.kind == "line" and len(shape.points) >= 2:
            p1, p2 = shape.points[:2]
            painter.drawLine(p1, p2)
            self._paint_handle(painter, p1)
            self._paint_handle(painter, p2)
        elif shape.kind == "angle" and len(shape.points) >= 3:
            p1, pc, p2 = shape.points[:3]
            painter.drawLine(pc, p1)
            painter.drawLine(pc, p2)
            for p in (p1, pc, p2):
                self._paint_handle(painter, p)
            painter.setPen(QColor(255, 215, 0))
            font = QFont()
            font.setPointSize(12)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pc + QPointF(12, -14), f"{self._angle_degrees(p1, pc, p2):.0f}°")

    def _paint_pending(self, painter: QPainter) -> None:
        if not self.pending_points or self.preview_pos is None:
            return
        painter.setPen(self.pen_preview)
        if self.mode == self.MODE_LINE and len(self.pending_points) == 1:
            painter.drawLine(self.pending_points[0], self.preview_pos)
        elif self.mode == self.MODE_ANGLE:
            if len(self.pending_points) == 1:
                painter.drawLine(self.pending_points[0], self.preview_pos)
            elif len(self.pending_points) == 2:
                painter.drawLine(self.pending_points[1], self.pending_points[0])
                painter.drawLine(self.pending_points[1], self.preview_pos)

    def _paint_handle(self, painter: QPainter, p: QPointF) -> None:
        r = self.handle_radius
        painter.setPen(QPen(QColor(20, 20, 20), 1.25))
        painter.setBrush(QColor(255, 215, 0))
        painter.drawEllipse(QRectF(p.x() - r / 2, p.y() - r / 2, r, r))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(self.pen_final)

    def _hit_test_handle(self, pos: QPointF):
        for shape in reversed(self.shapes):
            for i, p in enumerate(shape.points):
                if math.hypot(pos.x() - p.x(), pos.y() - p.y()) <= self.hit_radius:
                    return shape, i
        return None, None

    def _hit_test_shape(self, pos: QPointF):
        for shape in reversed(self.shapes):
            pts = shape.points
            if shape.kind == "line" and len(pts) >= 2:
                if self._distance_point_to_segment(pos, pts[0], pts[1]) <= self.hit_radius:
                    return shape
            elif shape.kind == "angle" and len(pts) >= 3:
                if self._distance_point_to_segment(pos, pts[1], pts[0]) <= self.hit_radius:
                    return shape
                if self._distance_point_to_segment(pos, pts[1], pts[2]) <= self.hit_radius:
                    return shape
        return None

    @staticmethod
    def _distance_point_to_segment(p: QPointF, a: QPointF, b: QPointF) -> float:
        ax, ay = a.x(), a.y()
        bx, by = b.x(), b.y()
        px, py = p.x(), p.y()
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        cx, cy = ax + t * dx, ay + t * dy
        return math.hypot(px - cx, py - cy)

    @staticmethod
    def _angle_degrees(outer1: QPointF, center: QPointF, outer2: QPointF) -> float:
        v1 = np.array([outer1.x() - center.x(), outer1.y() - center.y()], dtype=float)
        v2 = np.array([outer2.x() - center.x(), outer2.y() - center.y()], dtype=float)
        n1, n2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
        if n1 <= 1e-12 or n2 <= 1e-12:
            return 0.0
        cos_theta = float(np.dot(v1, v2) / (n1 * n2))
        cos_theta = max(-1.0, min(1.0, cos_theta))
        return float(np.degrees(np.arccos(cos_theta)))


class StillCoverWidget(QWidget):
    space_pressed = Signal()
    """
    Native top-level paused cover.

    QVideoWidget stays alive underneath. This cover is positioned exactly over
    the video panel while paused/stepping.
    """

    def __init__(self, owner: QWidget):
        super().__init__(parent=owner.window())
        self.owner = owner
        self.setObjectName("stillCoverWidget")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.NoDropShadowWindowHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.still = StillFrameWidget(self)
        self.overlay = DrawingOverlayWidget(self)

        # Catch Space regardless of whether focus is on the cover, still frame,
        # or drawing overlay after drawing/editing.
        self.installEventFilter(self)
        self.still.installEventFilter(self)
        self.overlay.installEventFilter(self)

        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space_pressed.emit()
            event.accept()
            return

        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            # Clicking this top-level cover must not steal keyboard shortcuts
            # from VideoDataViewer. Restore the owner after Qt finishes the
            # current mouse event.
            QTimer.singleShot(0, self._restore_owner_keyboard_focus)

        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                self.space_pressed.emit()
                event.accept()
                return True

        return super().eventFilter(obj, event)

    def _restore_owner_keyboard_focus(self) -> None:
        """Keep main-window shortcuts active after the still cover is clicked."""
        try:
            main_window = self.owner.window()
            main_window.activateWindow()
            main_window.setFocus(Qt.MouseFocusReason)
            # Keep the visual cover above the native video after activating owner.
            self.raise_()
            self.overlay.raise_()
        except Exception:
            pass


    def _owner_geometry(self) -> QRect | None:
        if self.owner is None or not self.owner.isVisible():
            return None
        size = self.owner.size()
        if size.width() <= 0 or size.height() <= 0:
            return None
        top_left = self.owner.mapToGlobal(QPoint(0, 0))
        return QRect(top_left, size)

    def _apply_owner_geometry(self) -> bool:
        geometry = self._owner_geometry()
        if geometry is None:
            self.hide()
            return False
        changed = self.geometry() != geometry
        if changed:
            self.setUpdatesEnabled(False)
            self.setGeometry(geometry)
            rect = self.rect()
            self.still.setGeometry(rect)
            self.overlay.setGeometry(rect)
            self.overlay.raise_()
            self.setUpdatesEnabled(True)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.rect()
        self.still.setGeometry(rect)
        self.overlay.setGeometry(rect)
        self.overlay.raise_()

    def sync_to_owner(self) -> None:
        if not self._apply_owner_geometry():
            return
        if self.isVisible():
            self.raise_()
            self.overlay.raise_()
            self.update()

    def set_image(self, image: QImage | None) -> None:
        # Position cover before changing image/showing. Prevents short displaced frame.
        if not self._apply_owner_geometry():
            return
        self.still.set_image(image)
        rect = self.rect()
        self.still.setGeometry(rect)
        self.overlay.setGeometry(rect)
        self.overlay.raise_()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.overlay.raise_()
        self.update()
        QTimer.singleShot(0, self.sync_to_owner)
        QTimer.singleShot(30, self.sync_to_owner)

    def clear_image(self) -> None:
        self.still.clear_image()
        self.hide()


class VideoStackWidget(QWidget):

    space_pressed = Signal()
    """
    Hybrid video panel.

    QVideoWidget stays mounted and visible all the time to avoid native video
    surface cold-start lag when pressing Play.
    """

    def __init__(self, video_widget: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("videoStackWidget")
        self.video_widget = video_widget
        try:
            self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        except Exception:
            pass
        self.video_widget.setParent(self)
        self.video_widget.show()
        self.video_widget.lower()

        self.still_cover = StillCoverWidget(self)

        # Used to cancel delayed cover hiding if user pauses/seeks again quickly.
        self._display_generation = 0

        # Keep paused still cover visible briefly after Play, so QVideoWidget can
        # present the seeked frame before the old native frame becomes visible.
        self.cover_release_delay_ms = 60

        # Main window uses this as self.drawing.
        self.overlay = self.still_cover.overlay
        
        self.still_cover.space_pressed.connect(self.space_pressed.emit)
        self.overlay.space_pressed.connect(self.space_pressed.emit)


        self.overlay_sync_timer = QTimer(self)
        self.overlay_sync_timer.setInterval(33)
        self.overlay_sync_timer.timeout.connect(self._sync_cover_if_visible)
        self.overlay_sync_timer.start()

        self.setMinimumSize(320, 240)

    def _sync_cover_if_visible(self) -> None:
        if self.still_cover.isVisible():
            self.still_cover.sync_to_owner()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.video_widget.setGeometry(self.rect())
        self.video_widget.lower()
        if self.still_cover.isVisible():
            self.still_cover.sync_to_owner()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.still_cover.isVisible():
            self.still_cover.sync_to_owner()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.still_cover.hide()

    def closeEvent(self, event):
        try:
            self.still_cover.close()
        except Exception:
            pass
        super().closeEvent(event)

    def set_video_aspect(self, width: int, height: int, rotation: int = 0) -> None:
        width = int(width or 0)
        height = int(height or 0)
        rotation = int(rotation or 0) % 360
        if width <= 0 or height <= 0:
            return
        if rotation in (90, 270):
            aspect_w, aspect_h = height, width
        else:
            aspect_w, aspect_h = width, height
        self.still_cover.still.set_aspect_size(aspect_w, aspect_h)
        try:
            self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        except Exception:
            pass

    def show_video(self) -> None:
        self._display_generation += 1
        generation = self._display_generation
        self.video_widget.show()
        self.video_widget.setGeometry(self.rect())
        self.video_widget.lower()

        def _fix_video_widget():
            if generation != self._display_generation:
                return
            self.video_widget.setGeometry(self.rect())
            self.video_widget.updateGeometry()
            self.video_widget.repaint()
            if self.still_cover.isVisible():
                self.still_cover.sync_to_owner()

        def _release_cover():
            if generation != self._display_generation:
                return
            self.video_widget.setGeometry(self.rect())
            self.video_widget.updateGeometry()
            self.video_widget.repaint()
            self.still_cover.hide()

        QTimer.singleShot(0, _fix_video_widget)
        QTimer.singleShot(50, _fix_video_widget)
        QTimer.singleShot(int(self.cover_release_delay_ms), _release_cover)

    def show_still_image(self, image: QImage | None) -> None:
        self._display_generation += 1
        self.video_widget.show()
        self.video_widget.setGeometry(self.rect())
        self.video_widget.lower()
        self.still_cover.set_image(image)

    def clear_still_image(self) -> None:
        self.still_cover.clear_image()


VideoOverlayWidget = VideoStackWidget
