"""Readable plain-text chat bubbles and configurable, IME-aware message input."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .chat_session import ChatMessage
from .preferences import AppPreferences, SEND_MODES
from .i18n import get_language, tr


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
        self.setToolTip(tr({"enter": "Enter sends. Shift+Enter adds a new line.",
                         "ctrl_enter": "Ctrl+Enter sends. Enter adds a new line.",
                         "shift_enter": "Shift+Enter sends. Enter adds a new line."}[mode]))

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


class NoteDisclosureDialog(QDialog):
    """A plain dialog: no QMessageBox alert role or application/system chime."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(parent.font() if parent is not None else ui_font(AppPreferences()))
        self.setWindowTitle(tr("Show Notes?"))
        layout = QVBoxLayout(self)
        self.explanation = QLabel(tr(
            "Viewing Notes will reveal the AI's recorded tactical intentions and action goals. "
            "This may affect fairness and your play experience. Best viewed after the game."
            "\n\nDo you still want to show Notes?"))
        self.explanation.setTextFormat(Qt.TextFormat.PlainText)
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        buttons = QDialogButtonBox()
        self.continue_button = buttons.addButton(tr("Show Notes"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        self.continue_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(490, 200)


class NoteWidget(QWidget):
    """Persistent, plain-text intent fields, concealed while collapsed."""

    size_changed = Signal()
    FIELDS = (("assumptions", "Current situation"), ("goal", "Goal"), ("expected_result", "Expected result"))

    def __init__(self, note: dict, number: int, parent: QWidget | None = None, *,
                 preferences: AppPreferences) -> None:
        super().__init__(parent)
        self.note = dict(note)
        self.number = number
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.details = QWidget()
        self.details_layout = QVBoxLayout(self.details)
        self.details_layout.setContentsMargins(12, 2, 4, 7)
        self.details_layout.setSpacing(5)
        self.headings: dict[str, QLabel] = {}
        self.values: dict[str, QLabel] = {}
        for field, _ in self.FIELDS:
            heading, value = QLabel(), QLabel()
            for label in (heading, value):
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                self.details_layout.addWidget(label)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.headings[field], self.values[field] = heading, value
        layout.addWidget(self.details)
        self.toggle.toggled.connect(self._toggled)
        self.apply_preferences(preferences)

    def update_note(self, note: dict, number: int) -> None:
        if self.note != note or self.number != number:
            self.note, self.number = dict(note), number
            self._refresh_text()
            self.size_changed.emit()

    def _refresh_text(self) -> None:
        expanded = self.toggle.isChecked()
        self.toggle.setText(tr("Note {number}", number=self.number))
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded)
        for field, title in self.FIELDS:
            # Clear collapsed labels as well as hiding them. Neither tooltips
            # nor the disclosure button contain any saved Note content.
            self.headings[field].setText(tr(title) if expanded else "")
            self.values[field].setText((self.note[field] or tr("Not recorded.")) if expanded else "")

    def _toggled(self, checked: bool) -> None:
        self._refresh_text()
        self.size_changed.emit()

    def apply_preferences(self, preferences: AppPreferences) -> None:
        self.toggle.setFont(ui_font(preferences, self.font()))
        heading_font = ui_font(preferences, self.font())
        heading_font.setWeight(QFont.Weight.Bold)
        for field, _ in self.FIELDS:
            self.headings[field].setFont(heading_font)
            self.values[field].setFont(chat_font(preferences, self.font()))
        self._refresh_text()
        self.size_changed.emit()

    def fit_height(self, width: int) -> int:
        height = self.toggle.sizeHint().height()
        if self.toggle.isChecked():
            margins = self.details_layout.contentsMargins()
            content_width = max(1, width - margins.left() - margins.right())
            rows = []
            for field, _ in self.FIELDS:
                for label in (self.headings[field], self.values[field]):
                    label.setMinimumHeight(0)
                    row_height = max(label.fontMetrics().height(), label.heightForWidth(content_width))
                    label.setMinimumHeight(row_height)
                    rows.append(row_height)
            detail_height = sum(rows) + self.details_layout.spacing() * (len(rows) - 1)
            detail_height += margins.top() + margins.bottom()
            self.details.setMinimumHeight(detail_height)
            height += self.layout().spacing() + detail_height
        else:
            self.details.setMinimumHeight(0)
        self.setMinimumHeight(height)
        return height


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
        self.heading.setProperty("i18n_skip", True)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)
        self.text = QLabel()
        self.text.setProperty("i18n_skip", True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(0)
        layout.addWidget(self.text)
        self.attachment_buttons = []
        for image in message.attachments:
            button = QPushButton(tr("Screenshot"))
            button.setProperty("i18n_skip", True)
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
        self._notes: list[dict] = []
        self.note_widgets: dict[str, NoteWidget] = {}
        self.notes_container = QWidget()
        self.notes_layout = QVBoxLayout(self.notes_container)
        self.notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_layout.setSpacing(6)
        self.notes_container.hide()
        layout.addWidget(self.notes_container)
        self.row_layout.addStretch(1)
        self.row_layout.addWidget(self.body)
        self.row_layout.addStretch(1)
        self.apply_preferences(self._preferences)
        self.update_message(message)

    def title(self) -> str:
        return self.heading.text()

    def set_notes(self, notes: list[dict]) -> None:
        notes = notes if self._message.role == "assistant" else []
        if notes == self._notes:
            return
        self._notes = [dict(note) for note in notes]
        identifiers = {note["entry_id"] for note in notes}
        for identifier in list(self.note_widgets):
            if identifier not in identifiers:
                widget = self.note_widgets.pop(identifier)
                widget.toggle.setChecked(False)
                self.notes_layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        for index, note in enumerate(notes):
            widget = self.note_widgets.get(note["entry_id"])
            if widget is None:
                widget = NoteWidget(note, index + 1, self.notes_container, preferences=self._preferences)
                widget.size_changed.connect(self._schedule_fit)
                self.note_widgets[note["entry_id"]] = widget
            else:
                widget.update_note(note, index + 1)
            self.notes_layout.insertWidget(index, widget)
        self.notes_container.setVisible(bool(notes))
        self._schedule_fit()

    def apply_preferences(self, preferences: AppPreferences) -> None:
        self._preferences = preferences
        self.text.setFont(chat_font(preferences, self.text.font()))
        heading_font = ui_font(preferences, self.font())
        heading_font.setPointSize(max(10, preferences.chat_font_size - 3))
        heading_font.setWeight(QFont.Weight.Bold)
        self.heading.setFont(heading_font)
        for button in self.attachment_buttons:
            button.setText(tr("Screenshot"))
        for note in self.note_widgets.values():
            note.apply_preferences(preferences)
        self._schedule_fit()
        self.updateGeometry()

    def update_message(self, message: ChatMessage) -> None:
        display = (message.role, message.status, message.text, message.created_at, get_language())
        if display == self._last_display:
            return
        self._last_display = display
        self._message = message
        role = tr({"user": "You", "assistant": "AI", "system": "Notice", "tool": "Reference"}[message.role])
        state = "" if message.status == "completed" else f" - {tr(message.status)}"
        timestamp = datetime.fromisoformat(message.created_at.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        self.heading.setText(f"{role}   {timestamp}{state}")
        text = self._display_text(message) if self._display_text else message.text
        empty_reply = {"pending": "Thinking...", "streaming": "Thinking...",
                       "failed": "Request failed.", "interrupted": "Stopped."}.get(message.status, "Empty reply.")
        self.text.setText(text or tr(empty_reply if message.role == "assistant" else "Screenshot"))
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
        heights = [self.heading.sizeHint().height(), text_height]
        for button in self.attachment_buttons:
            height = button.sizeHint().height()
            button.setMinimumHeight(height)
            heights.append(height)
        if self.note_widgets:
            notes_height = sum(note.fit_height(text_width) for note in self.note_widgets.values())
            notes_height += self.notes_layout.spacing() * (len(self.note_widgets) - 1)
            self.notes_container.setMinimumHeight(notes_height)
            heights.append(notes_height)
        else:
            self.notes_container.setMinimumHeight(0)
        content_height = sum(heights) + self.body.layout().spacing() * (len(heights) - 1)
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
