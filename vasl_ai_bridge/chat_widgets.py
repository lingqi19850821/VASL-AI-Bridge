"""Readable plain-text chat bubbles and configurable, IME-aware message input."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .chat_session import ChatMessage
from .preferences import AppPreferences, SEND_MODES


def preferred_font_family(base: QFont | None = None) -> str:
    """Choose one installed CJK family instead of per-character substitutions."""
    available = {name.casefold(): name for name in QFontDatabase.families()}
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "DengXian"):
        if family.casefold() in available:
            return available[family.casefold()]
    return base.family() if base is not None else QFont().family()


def _consistent_font(base: QFont | None = None) -> QFont:
    font = QFont(base) if base is not None else QFont()
    font.setFamily(preferred_font_family(font))
    font.setStyleName("")
    font.setStyleHint(QFont.StyleHint.SansSerif, QFont.StyleStrategy.PreferAntialias)
    # Keep Qt font merging enabled for rare characters and emoji.
    return font


def ui_font(preferences: AppPreferences, base: QFont | None = None) -> QFont:
    font = _consistent_font(base)
    font.setPointSize(preferences.ui_font_size)
    font.setWeight(QFont.Weight.Normal)
    return font


def chat_font(preferences: AppPreferences, base: QFont | None = None) -> QFont:
    font = _consistent_font(base)
    font.setPointSize(preferences.chat_font_size)
    font.setWeight({"normal": QFont.Weight.Normal, "bold": QFont.Weight.Bold}[preferences.chat_font_weight])
    return font


class ChatInput(QPlainTextEdit):
    sendRequested = Signal()

    def __init__(self, parent: QWidget | None = None, *, send_mode: str = "enter") -> None:
        super().__init__(parent)
        self._preedit_active = False
        self._ime_commit_guard = False
        self.set_send_mode(send_mode)

    def set_send_mode(self, mode: str) -> None:
        if mode not in SEND_MODES:
            raise ValueError("Choose a supported message send shortcut.")
        self.send_mode = mode
        self.setToolTip({"enter": "Enter sends. Shift+Enter adds a new line.",
                         "ctrl_enter": "Ctrl+Enter sends. Enter adds a new line.",
                         "shift_enter": "Shift+Enter sends. Enter adds a new line."}[mode])

    def apply_preferences(self, preferences: AppPreferences) -> None:
        self.setFont(chat_font(preferences, self.font()))
        self.set_send_mode(preferences.send_mode)

    def inputMethodEvent(self, event) -> None:
        # The Enter used to accept a Chinese/Japanese candidate must not send it.
        if self._preedit_active and event.commitString():
            self._ime_commit_guard = True
            QTimer.singleShot(0, self._clear_ime_commit_guard)
        self._preedit_active = bool(event.preeditString())
        super().inputMethodEvent(event)

    def _clear_ime_commit_guard(self) -> None:
        self._ime_commit_guard = False

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            wanted = {"enter": Qt.KeyboardModifier.NoModifier,
                      "ctrl_enter": Qt.KeyboardModifier.ControlModifier,
                      "shift_enter": Qt.KeyboardModifier.ShiftModifier}[self.send_mode]
            if modifiers == wanted and not (self._preedit_active or self._ime_commit_guard):
                if not event.isAutoRepeat():
                    self.sendRequested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class MessageBubble(QWidget):
    """A full-width row with a right-aligned user bubble or a left AI block.

    Text is deliberately plain and selectable: model output cannot load remote
    images, run HTML, or request a resource while a conversation is displayed.
    """

    def __init__(self, message: ChatMessage, parent: QWidget | None = None, *,
                 preferences: AppPreferences | None = None,
                 inspect_callback: Callable | None = None,
                 display_text: Callable[[ChatMessage], str] | None = None) -> None:
        super().__init__(parent)
        self._last_display = None
        self._message = message
        self._preferences = preferences or AppPreferences()
        self._display_text = display_text
        self._fit_pending = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.row_layout = QHBoxLayout(self)
        self.row_layout.setContentsMargins(0, 3, 0, 7)
        self.body = QFrame()
        self.body.setObjectName("messageBody")
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(7)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)
        self.text = QLabel()
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(0)
        layout.addWidget(self.text)
        self.attachment_buttons = []
        for image in message.attachments:
            button = QPushButton("Screenshot")
            pixmap = QPixmap()
            pixmap.loadFromData(image.png, "PNG")
            thumbnail = pixmap.scaled(120, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
            button.setIcon(QIcon(thumbnail))
            button.setIconSize(thumbnail.size())
            if inspect_callback is not None:
                button.clicked.connect(lambda checked=False, attached=image: inspect_callback(self, attached))
            else:
                button.setEnabled(False)
            layout.addWidget(button)
            self.attachment_buttons.append(button)
        self.row_layout.addStretch(1)
        self.row_layout.addWidget(self.body)
        self.row_layout.addStretch(1)
        self.apply_preferences(self._preferences)
        self.update_message(message)

    def title(self) -> str:
        return self.heading.text()

    def apply_preferences(self, preferences: AppPreferences) -> None:
        self._preferences = preferences
        self.text.setFont(chat_font(preferences, self.text.font()))
        heading_font = ui_font(preferences, self.font())
        heading_font.setPointSize(max(10, preferences.chat_font_size - 3))
        heading_font.setWeight(QFont.Weight.Bold)
        self.heading.setFont(heading_font)
        self._schedule_fit()
        self.updateGeometry()

    def update_message(self, message: ChatMessage) -> None:
        display = (message.role, message.status, message.text, message.created_at)
        if display == self._last_display:
            return
        self._last_display = display
        self._message = message
        role = {"user": "You", "assistant": "AI", "system": "Notice", "tool": "Reference"}[message.role]
        state = "" if message.status == "completed" else f" - {message.status}"
        timestamp = datetime.fromisoformat(message.created_at.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        self.heading.setText(f"{role}   {timestamp}{state}")
        text = self._display_text(message) if self._display_text else message.text
        empty_reply = {"pending": "Thinking...", "streaming": "Thinking...",
                       "failed": "Request failed.", "interrupted": "Stopped."}.get(message.status, "Empty reply.")
        self.text.setText(text or (empty_reply if message.role == "assistant" else "Screenshot"))
        user = message.role == "user"
        self.row_layout.setStretch(0, 1 if user else 0)
        self.row_layout.setStretch(1, 3 if user else 19)
        self.row_layout.setStretch(2, 0 if user else 1)
        background, border = ("#dceeff", "#b8d6ed") if user else ("#f5f7fa", "#d8dfe6")
        self.body.setStyleSheet(f"QFrame#messageBody {{ background: {background}; border: 1px solid {border}; border-radius: 10px; }}")
        self.text.setStyleSheet("color: #182e40; background: transparent; border: none;")
        self.heading.setStyleSheet("color: #4d6578; background: transparent; border: none;")
        self._fit_width()
        self._schedule_fit()

    def _fit_width(self) -> None:
        maximum = max(170, int(self.width() * (.78 if self._message.role == "user" else .95)))
        if self.body.maximumWidth() != maximum:
            self.body.setMaximumWidth(maximum)

    def _schedule_fit(self) -> None:
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self._fit_content)

    def _fit_content(self) -> None:
        self._fit_pending = False
        self._fit_width()
        # A scroll area's content can be taller than its viewport. Establish
        # the actual wrapped-text and thumbnail minimum heights instead of
        # allowing Qt to compress every message into the viewport's height.
        self.row_layout.activate()
        margins = self.body.layout().contentsMargins()
        text_width = max(1, self.body.contentsRect().width() - margins.left() - margins.right())
        # QLabel's heightForWidth is bounded by its explicit minimum height.
        # Clear the previous narrow layout before measuring a wider one.
        self.text.setMinimumHeight(0)
        text_height = max(self.text.fontMetrics().height(), self.text.heightForWidth(text_width))
        self.text.setMinimumHeight(text_height)
        button_height = 0
        for button in self.attachment_buttons:
            height = button.sizeHint().height()
            button.setMinimumHeight(height)
            button_height += height
        content_height = self.heading.sizeHint().height() + text_height + button_height
        content_height += self.body.layout().spacing() * (1 + len(self.attachment_buttons))
        content_height += margins.top() + margins.bottom() + self.body.frameWidth() * 2
        self.body.setMinimumHeight(content_height)
        row_margins = self.row_layout.contentsMargins()
        self.setMinimumHeight(content_height + row_margins.top() + row_margins.bottom())
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_width()
        self._schedule_fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_fit()
