"""Select pixels from a frozen local screenshot without taking another capture."""

from __future__ import annotations

from io import BytesIO
import math

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .capture import _prepare_image
from .session import Observation


def image_box_from_drag(
    image_size: tuple[int, int],
    display_rect: tuple[float, float, float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[int, int, int, int] | None:
    """Map a drag across a fitted image to a clamped, exclusive pixel box."""
    width, height = image_size
    x, y, shown_width, shown_height = display_rect
    if width < 1 or height < 1 or shown_width <= 0 or shown_height <= 0:
        return None
    x1, x2 = sorted((max(x, min(x + shown_width, start[0])),
                     max(x, min(x + shown_width, end[0]))))
    y1, y2 = sorted((max(y, min(y + shown_height, start[1])),
                     max(y, min(y + shown_height, end[1]))))
    if x1 == x2 or y1 == y2:
        return None
    left = max(0, math.floor((x1 - x) * width / shown_width))
    top = max(0, math.floor((y1 - y) * height / shown_height))
    right = min(width, math.ceil((x2 - x) * width / shown_width))
    bottom = min(height, math.ceil((y2 - y) * height / shown_height))
    return (left, top, right, bottom) if left < right and top < bottom else None


def crop_observation(observation: Observation, box: tuple[int, int, int, int]) -> Observation:
    """Create independent PNG evidence, retaining the original capture time."""
    if len(box) != 4 or any(isinstance(value, bool) or not isinstance(value, int) for value in box):
        raise ValueError("The screenshot selection must use four integer pixel coordinates.")
    left, top, right, bottom = box
    if not (0 <= left < right <= observation.width and 0 <= top < bottom <= observation.height):
        raise ValueError("Select an area inside the captured image.")
    with Image.open(BytesIO(observation.png)) as original:
        selected = original.crop(box)
        return _prepare_image(
            selected, f"{observation.source} | crop {left},{top},{right},{bottom}",
            2000, observation.captured_at,
        )


def prepare_full_observation(observation: Observation) -> Observation:
    if max(observation.width, observation.height) <= 2000:
        return observation
    with Image.open(BytesIO(observation.png)) as original:
        return _prepare_image(original, observation.source, 2000, observation.captured_at)


class ScreenshotSelectionCanvas(QWidget):
    selection_changed = Signal(object)

    def __init__(self, observation: Observation, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.observation = observation
        self.pixmap = QPixmap()
        if not self.pixmap.loadFromData(observation.png, "PNG"):
            raise ValueError("The captured screenshot could not be displayed.")
        self.selection_box: tuple[int, int, int, int] | None = None
        self._drag_start: QPointF | None = None
        self.setMinimumSize(320, 240)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def image_rect(self) -> QRectF:
        scale = min(self.width() / self.observation.width, self.height() / self.observation.height)
        width, height = self.observation.width * scale, self.observation.height * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _update_selection(self, position: QPointF) -> None:
        if self._drag_start is None:
            return
        rect = self.image_rect()
        self.selection_box = image_box_from_drag(
            (self.observation.width, self.observation.height),
            (rect.x(), rect.y(), rect.width(), rect.height()),
            (self._drag_start.x(), self._drag_start.y()), (position.x(), position.y()),
        )
        self.selection_changed.emit(self.selection_box)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.image_rect().contains(event.position()):
            self._drag_start = event.position()
            self._update_selection(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._update_selection(event.position())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._update_selection(event.position())
            self._drag_start = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202832"))
        rect = self.image_rect()
        painter.drawPixmap(rect, self.pixmap, QRectF(self.pixmap.rect()))
        if self.selection_box:
            left, top, right, bottom = self.selection_box
            selected = QRectF(
                rect.x() + left * rect.width() / self.observation.width,
                rect.y() + top * rect.height() / self.observation.height,
                (right - left) * rect.width() / self.observation.width,
                (bottom - top) * rect.height() / self.observation.height,
            )
            painter.fillRect(selected, QColor(40, 140, 255, 45))
            painter.setPen(QPen(QColor("#168bff"), 2))
            painter.drawRect(selected)


class ScreenshotSelectionDialog(QDialog):
    """Accepted dialogs expose the chosen frozen image; Cancel exposes None."""

    def __init__(self, observation: Observation, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.observation = observation
        self.selected_observation: Observation | None = None
        self.setWindowTitle("Select screenshot area")
        self.canvas = ScreenshotSelectionCanvas(observation, self)
        self.details = QLabel(
            "Drag to select any area, including the board or dice log.\n"
            f"Captured: {observation.captured_at} | {observation.width} x {observation.height}"
        )
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.selection_label = QLabel("No area selected")
        self.keep_button = QPushButton("Keep full image")
        self.use_button = QPushButton("Use selection")
        self.cancel_button = QPushButton("Cancel")
        self.use_button.setEnabled(False)
        # Enter must not accept a screenshot accidentally while selecting.
        for button in (self.keep_button, self.use_button, self.cancel_button):
            button.setAutoDefault(False)
        self.keep_button.clicked.connect(self.keep_full_image)
        self.use_button.clicked.connect(self.use_selection)
        self.cancel_button.clicked.connect(self.reject)
        self.canvas.selection_changed.connect(self._selection_changed)
        buttons = QHBoxLayout()
        buttons.addWidget(self.selection_label, 1)
        buttons.addWidget(self.keep_button)
        buttons.addWidget(self.use_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.details)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(buttons)
        screen = self.screen().availableGeometry()
        self.resize(min(1100, int(screen.width() * .9)), min(800, int(screen.height() * .85)))

    def _selection_changed(self, box) -> None:
        self.use_button.setEnabled(box is not None)
        if box:
            left, top, right, bottom = box
            self.selection_label.setText(f"Selected: {right - left} x {bottom - top}")
        else:
            self.selection_label.setText("No area selected")

    def keep_full_image(self) -> None:
        self.selected_observation = prepare_full_observation(self.observation)
        self.accept()

    def use_selection(self) -> None:
        if self.canvas.selection_box is None:
            return
        self.selected_observation = crop_observation(self.observation, self.canvas.selection_box)
        self.accept()

    def reject(self) -> None:
        self.selected_observation = None
        super().reject()
