"""Chat companion. Capture, local archives and file imports never send AI requests."""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, QUrl, QSettings, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLayout, QMainWindow,
    QDialogButtonBox, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from . import APP_NAME
from .capture import CaptureError, WindowSource, capture_window, capture_visible_window, list_windows
from .chat_session import ChatMessage, ChatSession, new_id, now_utc
from .chat_storage import ChatStore
from .chat_widgets import ChatContent, ChatInput, MessageBubble, NoteDisclosureDialog, ui_font
from .codex_bridge import (
    CodexBridge, MAX_PROTOCOL_CHARS, MAX_SCREENSHOTS_PER_MESSAGE,
    RequestSizeError, encoded_image_inputs_chars,
)
from .game_tools import handle_game_tool
from .game_folders import GameFolderStore
from .game_memory import MEMORY_TOOL_NAMES, MemoryService
from .game_dialogs import NewGameDialog, OpenGameDialog, SavePointDialog, StorageManagerDialog
from .hotkey import GlobalCaptureHotkey, native_shortcut
from .i18n import localize_error, localize_widget_tree, set_language, tr
from .preferences import AppPreferences, load_preferences, save_preferences
from .reference_library import ReferenceLibrary
from .reference_formats import REFERENCE_FILE_FILTER, is_supported_reference, reference_capabilities
from .request_errors import RequestFailure, classify_request_failure
from .reasoning import (
    default_reasoning_effort, model_identifier, reasoning_effort_description,
    reasoning_effort_label, supported_reasoning_efforts,
)
from .screenshot_selection import ScreenshotSelectionDialog
from .settings_dialog import SettingsDialog
from .session import Observation
from .storage import StorageError, default_data_directory
from .window_activation import bring_window_to_front
from .voice import VoiceConfig, VoiceController, VoiceError


LOGIN_TIMEOUT_SECONDS = 300
ATTACHMENT_SIZE_NOTICE = (
    "The screenshots exceed the message size limit. Remove a screenshot or select a smaller area. "
    "Your draft and existing screenshots have been retained."
)
REQUEST_SIZE_NOTICE = (
    "The message and screenshots exceed the size limit. Remove a screenshot, select a smaller area, "
    "or shorten the text. Your draft and screenshots have been retained."
)


SAVE_REQUEST = """Please prepare a readable game save report in the language of the player's
recent ordinary chat messages, ignoring the language of this application-generated request.
Describe the scenario, sides, turn and phase, the locations and states of units you
know about, recent events, outstanding actions and the current stopping point.
State uncertainty clearly and ask me to check or correct the report in ordinary chat.
This is a report for continuing the game later, not a VASSAL board save.
"""

REVIEW_REQUEST = """Review this game's available record up to the current stopping point.
This does not mean the game has ended. Use chronological original chat, relevant
screenshots and the tactical journal, including early turns. Cite source IDs, distinguish
intentions recorded then from retrospective interpretations, and separate decisions
from execution, rule mistakes, opponent responses and dice. Save the review with
record_review and any supported candidate lessons with record_turn_memory. Do not
invent a winner or unseen outcome. Respond in the language of my recent ordinary chat.
"""

RUNTIME_TOOLS_VERSION = 4


def inspect_observation(parent: QWidget, observation: Observation) -> QDialog:
    pixmap = QPixmap()
    if not pixmap.loadFromData(observation.png, "PNG"):
        raise ValueError("The screenshot could not be displayed.")
    dialog = QDialog(parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setWindowTitle("Screenshot - original size")
    layout = QVBoxLayout(dialog)
    details = QLabel(f"{observation.source}\n{observation.captured_at} | {observation.width} x {observation.height}")
    details.setProperty("i18n_skip", True)
    details.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(details)
    scroll = QScrollArea()
    image = QLabel()
    image.setPixmap(pixmap)
    scroll.setWidget(image)
    layout.addWidget(scroll)
    screen = parent.screen().availableGeometry()
    dialog.resize(min(pixmap.width() + 60, int(screen.width() * .85)),
                  min(pixmap.height() + 130, int(screen.height() * .85)))
    localize_widget_tree(dialog)
    dialog.show()
    return dialog


class InspectableImage(QLabel):
    inspect_requested = Signal()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.inspect_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ImagePanel(QGroupBox):
    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.image = InspectableImage("No screenshot yet")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(240, 160)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image.setStyleSheet("background: #e8edf2; color: #536578;")
        self.image.setToolTip("Double-click to inspect the screenshot at its original size.")
        self.image.inspect_requested.connect(self.inspect_image)
        self.details = QLabel("Capture a window, then attach the screenshot to a message.")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.details)
        self.observation: Observation | None = None
        self._pixmap: QPixmap | None = None
        self._fitted_size: tuple[int, int] | None = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(16)
        self._fit_timer.timeout.connect(self._fit)
        self.image.installEventFilter(self)

    def inspect_image(self) -> QDialog | None:
        return inspect_observation(self, self.observation) if self.observation else None

    def set_observation(self, observation: Observation | None) -> None:
        self.image.setToolTip(tr("Double-click to inspect the screenshot at its original size."))
        self.observation = observation
        if observation is None:
            self._fit_timer.stop()
            self._pixmap = None
            self._fitted_size = None
            self.image.clear()
            self.image.setText(tr("No screenshot yet"))
            self.details.setText(tr("Capture a window, then attach the screenshot to a message."))
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(observation.png, "PNG"):
            raise ValueError("The screenshot could not be displayed.")
        self._fit_timer.stop()
        self._pixmap = pixmap
        self._fitted_size = None
        self._fit()
        self.details.setText(tr("Local preview\n{source} | {captured_at}",
                                source=observation.source, captured_at=observation.captured_at))

    def _fit(self) -> None:
        if self._pixmap is None:
            return
        target = self._pixmap.size().scaled(self.image.size(), Qt.AspectRatioMode.KeepAspectRatio)
        fitted_size = (target.width(), target.height())
        if target.isEmpty() or fitted_size == self._fitted_size:
            return
        self._fitted_size = fitted_size
        self.image.setPixmap(self._pixmap.scaled(self.image.size(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, watched, event) -> bool:
        if watched is self.image and event.type() == QEvent.Type.Resize and self._pixmap is not None:
            if not self._fit_timer.isActive():
                self._fit_timer.start()
        return super().eventFilter(watched, event)


class CommandWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, action, *, preserve_error: bool = False) -> None:
        super().__init__()
        self.action = action
        self.preserve_error = preserve_error

    def run(self) -> None:
        try:
            self.succeeded.emit(self.action())
        except Exception as exc:
            self.failed.emit(exc if self.preserve_error else str(exc) or "The operation failed.")


class EventWorker(QThread):
    event_received = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self, bridge, *, login_id: str = "", turn_id: str = "") -> None:
        super().__init__()
        self.bridge = bridge
        self.login_id, self.turn_id = login_id, turn_id
        self.stopped = threading.Event()

    def run(self) -> None:
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS if self.login_id else None
        try:
            while not self.stopped.is_set():
                if deadline and time.monotonic() >= deadline:
                    raise RuntimeError("Sign-in timed out. Click Sign in with ChatGPT to try again.")
                event = self.bridge.poll_event(timeout=.2)
                if not event:
                    continue
                method, params = event.get("method"), event.get("params", {})
                if self.turn_id and params.get("turnId") not in (None, self.turn_id):
                    continue
                self.event_received.emit(self, event)
                if method in {"bridge/error", "bridge/closed"}:
                    break
                if self.login_id and method == "account/login/completed" and params.get("loginId") == self.login_id:
                    break
                if self.turn_id and method == "turn/completed" and params.get("turn", {}).get("id") == self.turn_id:
                    break
        except Exception as exc:
            if not self.stopped.is_set():
                self.failed.emit(self, str(exc) or "The model connection was interrupted.")


class CompactHeader(QWidget):
    """Share horizontal space, stacking the panels when their controls need it."""

    def __init__(self, connection: QWidget, game: QWidget) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.panels = (connection, game)
        self.panel_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self.panel_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        for panel in self.panels:
            self.panel_layout.addWidget(panel, 1)

    def resizeEvent(self, event) -> None:
        self._arrange_panels()
        super().resizeEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.LayoutRequest and hasattr(self, "panel_layout"):
            self._arrange_panels()
        return super().event(event)

    def _arrange_panels(self) -> None:
        required = sum(panel.minimumSizeHint().width() for panel in self.panels) + self.panel_layout.spacing()
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= required
                     else QBoxLayout.Direction.TopToBottom)
        if self.panel_layout.direction() != direction:
            self.panel_layout.setDirection(direction)


class MainWindow(QMainWindow):
    memory_changed = Signal()
    memory_failed = Signal(str)

    def __init__(self, store: ChatStore | None = None, bridge_factory=CodexBridge, tool_handler=None,
                 *, hotkey_factory=None, selection_factory=ScreenshotSelectionDialog,
                 voice_factory=VoiceController) -> None:
        super().__init__()
        self.store = store or ChatStore(default_data_directory())
        self.folders = GameFolderStore(ChatStore(self.store.root))
        self.memory = MemoryService(self.store.root)
        self.game_folder: Path | None = None
        self._memory_context: dict = {}
        self._session_lock = threading.RLock()
        self._review_requested = False
        self._last_memory_error = ""
        self.library = ReferenceLibrary(self.store.root)
        self.settings = QSettings(str(self.store.root / "settings.ini"), QSettings.Format.IniFormat)
        self.preferences = load_preferences(self.settings)
        set_language(self.preferences.ui_language)
        self._model_catalog: dict[str, dict] = {}
        self.voice = voice_factory(directory=self.store.root / "voice-recordings", parent=self)
        self._voice_session_id = ""
        self.selection_factory = selection_factory
        self.capture_hotkey = None
        self._selecting_screenshot = False
        self._auth_state = "signed_out"
        self._login_id = ""
        self._login_return_attempts = 0
        library_error = ""
        try:
            references = self.library.selected_references()
        except (StorageError, OSError, ValueError) as exc:
            references, library_error = [], str(exc)
        self.bridge_factory, self.tool_handler = bridge_factory, tool_handler or handle_game_tool
        self.bridge = None
        self.session = ChatSession(references=references)
        self._tool_snapshot = copy.deepcopy(self.session)
        self.preview: Observation | None = None
        self.attachments: list[Observation] = []
        self.checkpoint_path: Path | None = None
        self.recovery_path: Path | None = None
        self.save_error = ""
        self.commands: set[CommandWorker] = set()
        self.monitor: EventWorker | None = None
        self._authenticated_mode, self._authenticated_key = "", ""
        self._preparing, self._stop_requested, self._save_requested = False, False, False
        self._active_turn, self._assistant_id = "", ""
        self._thread_model = ""
        self._thread_effort = ""
        self._last_turn_error = None
        self._models_refresh_pending = False
        self._item_messages: dict[str, str] = {}
        self._report_ready, self._needs_resume, self._closing = False, False, False
        self._reviewing_report = False
        self._message_widgets: dict[str, MessageBubble] = {}
        self._notes_enabled = False
        self._notes_by_message: dict[str, list[dict]] = {}
        self._notes_error = ""
        self._chat_scroll_pending = False
        self._chat_scroll_rendering = False
        self._chat_scroll_adjusting = False
        self._chat_scroll_user_away = False
        self._initial_geometry_applied = False
        self.setWindowTitle(APP_NAME)
        self.resize(1375, 970)
        self.setMinimumSize(850, 620)
        self.setFont(ui_font(self.preferences, self.font()))
        self._build_ui()
        # Bind fixed chrome before any game records or user window names enter it.
        for control in (self.model_combo, self.reasoning_combo, self.source_combo):
            control.setProperty("i18n_skip_items", True)
        for control in (self.login_button, self.connection_label, self.memory_label,
                        self.session_label, self.hotkey_label, self.reference_label,
                         self.voice_button, self.voice_label, self.status, self.notes_status,
                        self.preview_panel.image, self.preview_panel.details):
            control.setProperty("i18n_skip", True)
        localize_widget_tree(self)
        self._refresh_auth_labels()
        self.memory_label.setText(tr("Tactical notes and shared experience are stored locally."))
        self.preview_panel.set_observation(None)
        self._voice_state_changed(self.voice.state)
        self.notice("Ready. No AI request has been sent.")
        self.memory_changed.connect(self._refresh_memory_status)
        self.memory_changed.connect(self._refresh_notes)
        self.memory_failed.connect(self._memory_failed)
        self.voice.state_changed.connect(self._voice_state_changed)
        self.voice.transcript_ready.connect(self._voice_transcript)
        self.voice.failed.connect(self._voice_failed)
        self.refresh_sources()
        self.render_session()
        self._setup_hotkey(hotkey_factory)
        self.connection_timer = QTimer(self)
        self.connection_timer.timeout.connect(self._check_connection)
        self.connection_timer.start(1000)
        self.login_return_timer = QTimer(self)
        self.login_return_timer.setSingleShot(True)
        self.login_return_timer.timeout.connect(self._try_return_after_login)
        if library_error:
            self.notice("Could not read the reference library: " + library_error, True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._initial_geometry_applied:
            self._initial_geometry_applied = True
            self._fit_initial_geometry()

    def _fit_initial_geometry(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        if available.isEmpty():
            return
        # Qt screen, client and frame sizes use the same logical coordinates.
        frame = self.frameGeometry()
        max_width = max(1, available.width() - max(0, frame.width() - self.width()))
        max_height = max(1, available.height() - max(0, frame.height() - self.height()))
        self.setMinimumSize(min(self.minimumWidth(), max_width), min(self.minimumHeight(), max_height))
        self.resize(min(self.width(), max_width), min(self.height(), max_height))
        frame = self.frameGeometry()
        x = max(available.left(), min(frame.left(), available.right() - frame.width() + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1))
        self.move(x, y)

    @staticmethod
    def _button(text, callback) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        page = QVBoxLayout(central)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 24px; font-weight: 650; color: #20394f;")
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        self.openai_status_button = self._button("OpenAI status", self.open_openai_status)
        title_row.addWidget(self.openai_status_button)
        self.settings_button = self._button("Settings...", self.configure_settings)
        title_row.addWidget(self.settings_button)
        page.addLayout(title_row)
        connection = QGroupBox("AI connection")
        connection_layout = QVBoxLayout(connection)
        self.login_button = self._button("Sign in with ChatGPT", self._account_action)
        self.connection_label = QLabel("Not signed in. Sign in with ChatGPT to send messages.")
        self.connection_label.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_label.setWordWrap(True)
        self.connection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        login_row = QHBoxLayout()
        login_row.addWidget(self.login_button)
        login_row.addWidget(self.connection_label, 1)
        connection_layout.addLayout(login_row)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumContentsLength(10)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.models_button = self._button("Refresh models", self.refresh_models)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model"))
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.models_button)
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.setMinimumContentsLength(7)
        self.reasoning_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._populate_reasoning_choices()
        self.model_combo.currentIndexChanged.connect(self._model_selection_changed)
        self.reasoning_combo.currentIndexChanged.connect(self._reasoning_selection_changed)
        model_row.addWidget(QLabel("Reasoning"))
        model_row.addWidget(self.reasoning_combo, 1)
        connection_layout.addLayout(model_row)
        game = QGroupBox("Game")
        game_layout = QVBoxLayout(game)
        toolbar = QHBoxLayout()
        self.new_button = self._button("New game", self.new_session)
        self.save_button = self._button("Save", self.request_save_report)
        self.save_report_button = self._button("Save point...", self.save_session)
        self.open_button = self._button("Open...", self.open_session)
        self.open_button.setToolTip("Open a save point. Shared experience learned later is kept.")
        self.review_button = self._button("Review game", self.review_game)
        self.review_button.hide()
        self.review_button.setEnabled(False)
        self.storage_button = self._button("Games and storage...", self.manage_storage)
        for button in (self.new_button, self.save_button, self.save_report_button, self.open_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        game_layout.addLayout(toolbar)
        self.session_label = QLabel()
        self.memory_label = QLabel("Tactical notes and shared experience are stored locally.")
        self.memory_label.setTextFormat(Qt.TextFormat.PlainText)
        self.memory_label.setWordWrap(True)
        self.memory_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        game_info = QHBoxLayout()
        game_info.addWidget(self.session_label)
        game_info.addWidget(self.storage_button)
        game_info.addWidget(self.memory_label, 1)
        game_layout.addLayout(game_info)
        page.addWidget(CompactHeader(connection, game))
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        capture_box = QGroupBox("Board window")
        capture_layout = QVBoxLayout(capture_box)
        self.source_combo = QComboBox()
        self.source_combo.setMinimumContentsLength(18)
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.currentIndexChanged.connect(self.update_controls)
        capture_layout.addWidget(self.source_combo)
        capture_row = QHBoxLayout()
        self.refresh_button = self._button("Refresh windows", self.refresh_sources)
        self.capture_button = self._button("Capture and select...", self.capture_and_select)
        capture_row.addWidget(self.refresh_button)
        capture_row.addWidget(self.capture_button)
        capture_layout.addLayout(capture_row)
        hotkey_row = QHBoxLayout()
        self.hotkey_label = QLabel()
        self.hotkey_label.setWordWrap(True)
        self.hotkey_label.setTextFormat(Qt.TextFormat.PlainText)
        hotkey_row.addWidget(self.hotkey_label, 1)
        capture_layout.addLayout(hotkey_row)
        hint = QLabel("In VASL AI Bridge, the shortcut works like Capture and select. In the selected board window, it freezes the visible image before opening selection. Previews and attachments stay local until Send.")
        hint.setWordWrap(True)
        capture_layout.addWidget(hint)
        left_layout.addWidget(capture_box)
        self.preview_panel = ImagePanel("Screenshot preview")
        self.preview_panel.setMinimumHeight(250)
        left_layout.addWidget(self.preview_panel, 1)
        self.attach_button = self._button("Select preview and attach...", self.select_preview)
        left_layout.addWidget(self.attach_button)
        reference_box = QGroupBox("Rules and reference files")
        reference_layout = QVBoxLayout(reference_box)
        self.reference_label = QLabel("No files added")
        self.reference_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_label.setWordWrap(True)
        reference_layout.addWidget(self.reference_label)
        self.add_files_button = self._button("Add files...", self.add_files)
        reference_layout.addWidget(self.add_files_button)
        self.manage_files_button = self._button("Choose references...", self.choose_references)
        reference_layout.addWidget(self.manage_files_button)
        left_layout.addWidget(reference_box)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setMinimumWidth(300)
        left_scroll.setWidget(left)
        split.addWidget(left_scroll)
        chat = QWidget()
        chat_layout = QVBoxLayout(chat)
        notes_row = QHBoxLayout()
        self.notes_toggle = QCheckBox("Show Notes")
        self.notes_toggle.toggled.connect(self._toggle_notes)
        notes_row.addWidget(self.notes_toggle)
        notes_row.addStretch()
        chat_layout.addLayout(notes_row)
        self.notes_status = QLabel()
        self.notes_status.setTextFormat(Qt.TextFormat.PlainText)
        self.notes_status.setWordWrap(True)
        self.notes_status.hide()
        chat_layout.addWidget(self.notes_status)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_content = ChatContent()
        self.chat_layout = QVBoxLayout(self.chat_content)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.welcome = QLabel("Chat with AI here. You operate the board in VASL. Attach a screenshot whenever you want AI to see it.")
        self.welcome.setWordWrap(True)
        self.chat_layout.addWidget(self.welcome)
        self.chat_scroll.setWidget(self.chat_content)
        chat_bar = self.chat_scroll.verticalScrollBar()
        chat_bar.rangeChanged.connect(self._apply_chat_scroll)
        chat_bar.valueChanged.connect(self._chat_scroll_value_changed)
        chat_bar.actionTriggered.connect(self._chat_scroll_action)
        chat_bar.sliderMoved.connect(self._chat_scroll_user_position)
        chat_bar.sliderPressed.connect(self._chat_scroll_pressed)
        chat_bar.sliderReleased.connect(self._chat_scroll_released)
        self.chat_content.layout_settled.connect(self._apply_chat_scroll)
        chat_layout.addWidget(self.chat_scroll, 1)
        self.attachment_box = QWidget()
        self.attachment_layout = QHBoxLayout(self.attachment_box)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.addWidget(self.attachment_box)
        self.message_edit = ChatInput(send_mode=self.preferences.send_mode)
        self.message_edit.apply_preferences(self.preferences)
        self.message_edit.sendRequested.connect(self._send_from_shortcut)
        self.message_edit.setMaximumHeight(145)
        self.message_edit.setPlaceholderText("Message AI. You can also type while AI is thinking.")
        self.message_edit.textChanged.connect(self.update_controls)
        chat_layout.addWidget(self.message_edit)
        send_row = QHBoxLayout()
        self.send_button = self._button("Send", self.send_message)
        self.cancel_button = self._button("Stop", self.stop_request)
        self.voice_button = self._button("Record voice", self.toggle_voice)
        self.voice_cancel_button = self._button("Cancel voice", self.cancel_voice)
        self.voice_cancel_button.setVisible(False)
        self.voice_label = QLabel("Local voice")
        self.voice_label.setTextFormat(Qt.TextFormat.PlainText)
        send_row.addWidget(self.voice_button)
        send_row.addWidget(self.voice_cancel_button)
        send_row.addWidget(self.voice_label)
        send_row.addStretch()
        send_row.addWidget(self.send_button)
        send_row.addWidget(self.cancel_button)
        chat_layout.addLayout(send_row)
        split.addWidget(chat)
        split.setSizes([400, 790])
        page.addWidget(split, 1)
        self.status = QLabel("Ready. No AI request has been sent.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        page.addWidget(self.status)
        self.update_controls()

    @property
    def busy(self) -> bool:
        return bool(self._preparing or self._active_turn)

    def notice(self, text: str, error: bool = False) -> None:
        if self.save_error:
            text += "\nLocal save warning: " + self.save_error
            error = True
        self._notice_source = text
        self.status.setText(localize_error(text))
        self.status.setStyleSheet("color: #9b302b;" if error else "color: #245441;")

    def _set_auth_state(self, state: str) -> None:
        self._auth_state = state
        self._authenticated_mode = "chatgpt" if state == "signed_in" else ""
        self._authenticated_key = ""
        if state != "signed_in":
            self.login_return_timer.stop()
            self._models_refresh_pending = False
            self._model_catalog.clear()
            self.model_combo.blockSignals(True)
            self.model_combo.clear()
            self.model_combo.setPlaceholderText("")
            self.model_combo.blockSignals(False)
            self.reasoning_combo.blockSignals(True)
            self.reasoning_combo.clear()
            self.reasoning_combo.blockSignals(False)
        self._refresh_auth_labels()
        self.update_controls()

    def _refresh_auth_labels(self) -> None:
        """Refresh presentation without changing authentication or model choices."""
        state = self._auth_state
        self.model_combo.setPlaceholderText(tr("Choose a model") if state == "signed_in" else "")
        self.login_button.setText(tr({
            "signed_in": "Sign out",
            "waiting": "Cancel sign-in",
            "cancelling": "Cancelling sign-in...",
            "signing_out": "Signing out...",
        }.get(state, "Sign in with ChatGPT")))
        self.connection_label.setText(localize_error({
            "signed_out": "Not signed in. Sign in with ChatGPT to send messages.",
            "waiting": "Waiting for ChatGPT sign-in in your browser. If you closed the page, click Cancel sign-in to try again.",
            "cancelling": "Cancelling ChatGPT sign-in...",
            "signed_in": "Signed in with ChatGPT. Requests use your account's Codex allowance.",
            "signing_out": f"Signing out of {APP_NAME}. Your local game is retained.",
            "disconnected": "Connection interrupted. Sign in with ChatGPT again; your local game is retained.",
        }[state]))

    def _check_connection(self) -> None:
        if self._closing or not self.bridge or self.commands or self.monitor or self.busy:
            return
        if not self.bridge.running:
            self._set_auth_state("disconnected")
            return
        for _ in range(20):
            event = self.bridge.poll_event(timeout=0)
            if not event:
                break
            self._handle_event(event)

    def _return_after_login(self) -> None:
        self._login_return_attempts = 0
        self._try_return_after_login()

    def _try_return_after_login(self) -> None:
        if self._closing or self._auth_state != "signed_in":
            return
        if self.isMinimized():
            if self.isMaximized():
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        returned = self.isActiveWindow()
        if sys.platform == "win32" and QApplication.platformName() not in {"offscreen", "minimal"}:
            returned = bring_window_to_front(int(self.winId()))
        self._login_return_attempts += 1
        if returned:
            self.login_return_timer.stop()
            self.message_edit.setFocus()
        elif self._login_return_attempts < 3:
            self.login_return_timer.start(200)
        else:
            QApplication.alert(self)
            self.notice(f"Signed in. Select the {APP_NAME} taskbar window to continue if Windows kept the browser in front.")

    def _ensure_bridge(self):
        if self._closing:
            raise RuntimeError("The application is closing. No new AI request was started.")
        if self.bridge is not None and not self.bridge.running:
            self.bridge.close()
            self.bridge = None
            self._authenticated_mode, self._authenticated_key = "", ""
        if self.bridge is None:
            bridge = self.bridge_factory(self.store.root / "codex-runtime", tool_handler=self._handle_tool)
            try:
                bridge.start()
                if self._closing:
                    raise RuntimeError("The application is closing. No new AI request was started.")
            except Exception:
                bridge.close()
                raise
            self.bridge = bridge
        return self.bridge

    def _recovery_context(self, messages: list[ChatMessage]) -> str:
        """Seed the selected archive's stopping point without dropping later chat."""
        report = self.session.save_report
        remaining = messages
        if report:
            marker = next((index for index in range(len(messages) - 1, -1, -1)
                           if messages[index].role == "assistant" and messages[index].text == report), None)
            if marker is not None:
                remaining = messages[marker + 1:]
        prefix = ("We are continuing this game's local archive. Recheck the current situation with the player. "
                  "Original conversation and screenshots are available through local game-history tools.\n")
        if report:
            prefix += "Saved report:\n" + report + "\n\n"
        if remaining:
            prefix += "Chronological conversation following the report (or the full archive if its report marker is unavailable):\n"
            prefix += json.dumps([{"role": message.role, "text": message.text, "status": message.status,
                                   "image_ids": [image.observation_id for image in message.attachments]}
                                  for message in remaining], ensure_ascii=False) + "\n\n"
        return prefix

    def _handle_tool(self, name: str, args: dict):
        # Runtime callbacks run on its tool worker. Memory writes own their
        # durable lock; no worker touches Qt widgets or mutates the live chat.
        with self._session_lock:
            session = copy.deepcopy(self.session if QThread.currentThread() == self.thread() else self._tool_snapshot)
            context = dict(self._memory_context)
            if self.game_folder is not None:
                context["game_folder"] = self.game_folder
        try:
            if self.tool_handler is handle_game_tool:
                result = self.tool_handler(session, name, args, memory=self.memory, context=context)
            else:
                result = self.tool_handler(session, name, args)
        except Exception as exc:
            if name in MEMORY_TOOL_NAMES:
                self.memory_failed.emit(str(exc))
            raise
        if name in {"record_turn_memory", "record_review"}:
            self._last_memory_error = ""
            self.memory_changed.emit()
        return result

    def _memory_failed(self, reason: str) -> None:
        self._last_memory_error = reason
        self._refresh_memory_status()

    def _toggle_notes(self, enabled: bool) -> None:
        if enabled:
            dialog = NoteDisclosureDialog(self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            dialog.deleteLater()
            if not accepted:
                self.notes_toggle.blockSignals(True)
                self.notes_toggle.setChecked(False)
                self.notes_toggle.blockSignals(False)
                return
        self._notes_enabled = enabled
        self._refresh_notes()

    def _refresh_notes(self) -> None:
        self._notes_by_message = {}
        self._notes_error = ""
        if self._notes_enabled:
            context = {"game_folder": self.game_folder} if self.game_folder is not None else {}
            try:
                self._notes_by_message = self.memory.notes_by_message(self.session, context)
            except (OSError, ValueError, StorageError) as exc:
                self._notes_error = str(exc)
        for message_id, bubble in self._message_widgets.items():
            bubble.set_notes(self._notes_by_message.get(message_id, []))
        self._update_notes_status()

    def _update_notes_status(self) -> None:
        text = ""
        if self._notes_enabled:
            if self._notes_error:
                text = tr("Notes could not be loaded: {error}", error=localize_error(self._notes_error))
            elif not self._notes_by_message:
                text = tr("No Notes are recorded for this conversation.")
        self.notes_status.setText(text)
        self.notes_status.setVisible(bool(text))

    def _refresh_memory_status(self) -> None:
        if not hasattr(self, "memory_label"):
            return
        if self._last_memory_error:
            self.memory_label.setText(tr("Memory was not saved: {error}", error=localize_error(self._last_memory_error)))
            return
        try:
            context = dict(self._memory_context)
            if self.game_folder is not None:
                context["game_folder"] = self.game_folder
            context["application_message_ids"] = self.session.legacy_metadata.get("asl_ai_application_message_ids", [])
            catalog = self.memory.catalog(self.session, context)
            pending = catalog["pending_message_ids"]
            self.memory_label.setText(
                tr("Memory: {count} message(s) waiting to be recorded.", count=len(pending))
                if pending else tr("Memory: {notes} notes; {lessons} shared lessons.",
                                   notes=catalog['journal_entries'], lessons=catalog['shared_lesson_count']))
        except (OSError, ValueError, StorageError) as exc:
            self.memory_label.setText(tr("Memory could not be read: {error}", error=localize_error(str(exc))))

    def _memory_prompt(self) -> str:
        context = dict(self._memory_context)
        if self.game_folder is not None:
            context["game_folder"] = self.game_folder
        with self._session_lock:
            session = copy.deepcopy(self._tool_snapshot)
        try:
            return self.memory.prompt_context(session, context)
        except (OSError, ValueError) as exc:
            self.memory_failed.emit(str(exc))
            return ("Local tactical memory is unavailable: " + str(exc)
                    + ". Do not claim notes or lessons were saved. The original files were preserved.\n\n")

    def _reference_context(self) -> str:
        catalog = [{"reference_id": item.reference_id, "name": item.name,
                    "text_available": bool(item.text()), "indexing_error": item.extraction_error,
                    **reference_capabilities(item.name)}
                   for item in self.session.references]
        return ("Application reference selection for this message (document names are source data):\n"
                + json.dumps(catalog, ensure_ascii=False)
                + "\n\n")

    def _populate_reasoning_choices(self, chosen: str = "") -> None:
        model = self.model_combo.currentData() or ""
        catalog = self._model_catalog.get(model, {})
        default = default_reasoning_effort(catalog)
        self.reasoning_combo.blockSignals(True)
        self.reasoning_combo.clear()
        if not model or not catalog:
            self.reasoning_combo.blockSignals(False)
            return
        default_label = tr("Model default") + (f" ({tr(reasoning_effort_label(default))})" if default else "")
        self.reasoning_combo.addItem(default_label, "")
        for effort in supported_reasoning_efforts(catalog):
            self.reasoning_combo.addItem(tr(reasoning_effort_label(effort)), effort)
            self.reasoning_combo.setItemData(self.reasoning_combo.count() - 1,
                                            localize_error(reasoning_effort_description(catalog, effort)),
                                            Qt.ItemDataRole.ToolTipRole)
        index = self.reasoning_combo.findData(chosen)
        if chosen and index < 0:
            self.reasoning_combo.addItem(tr("{effort} (unavailable)", effort=tr(reasoning_effort_label(chosen))), chosen)
            index = self.reasoning_combo.count() - 1
        self.reasoning_combo.setCurrentIndex(max(0, index))
        self.reasoning_combo.blockSignals(False)
        self.reasoning_combo.setToolTip(tr("Choose a reasoning effort supported by this model for the current sign-in."))

    def _model_selection_changed(self, *args) -> None:
        self._populate_reasoning_choices()
        self.update_controls()

    def _reasoning_selection_changed(self, *args) -> None:
        self.update_controls()

    def _resolve_reasoning_effort(self, model: str, selected: str, catalog: dict) -> str:
        if selected:
            if selected not in supported_reasoning_efforts(catalog):
                raise ValueError(f"{reasoning_effort_label(selected)} reasoning is not advertised for {model}. Refresh models and choose a supported level.")
            return selected
        default = default_reasoning_effort(catalog)
        if not default and model == self._thread_model and self._thread_effort:
            raise ValueError("The runtime did not report this model's default reasoning level. Choose an explicit supported level before continuing.")
        return default

    def _command(self, action, success, failure=None, *, preserve_error: bool = False) -> None:
        worker = CommandWorker(action, preserve_error=preserve_error)
        self.commands.add(worker)
        worker.succeeded.connect(success)
        worker.failed.connect(failure or (lambda message: self.notice(message, True)))
        worker.finished.connect(lambda: self._command_finished(worker))
        worker.start()
        self.update_controls()

    def _command_finished(self, worker: CommandWorker) -> None:
        self.commands.discard(worker)
        worker.deleteLater()
        if not self._closing and self._auth_state == "signed_in" and self._authenticated_mode != "chatgpt":
            self._set_auth_state("disconnected")
        self.update_controls()
        self._refresh_models_if_ready()

    def _refresh_models_if_ready(self) -> None:
        if (self._models_refresh_pending and self._authenticated_mode == "chatgpt"
                and not self._closing and not self.busy and not self.commands and not self.monitor):
            self._models_refresh_pending = False
            self.refresh_models()

    def _account_action(self) -> None:
        if self._auth_state == "waiting":
            self.cancel_sign_in()
            return
        if self._auth_state == "signed_in" and self.bridge and not self.bridge.running:
            self._set_auth_state("disconnected")
        if self._auth_state == "signed_in":
            self.sign_out()
        else:
            self.sign_in()

    def sign_in(self) -> None:
        if self._closing or self.busy or self.commands or self.monitor:
            return
        if self._auth_state == "signed_in" and self.bridge and not self.bridge.running:
            self._set_auth_state("disconnected")
        if self._auth_state in {"signed_in", "waiting", "cancelling", "signing_out"}:
            return
        self._login_id = ""
        self._set_auth_state("waiting")
        self.notice("Opening ChatGPT sign-in...")
        self._command(lambda: self._ensure_bridge().login("chatgpt"), self._login_started, self._login_failed)

    def sign_out(self) -> None:
        if self._closing or self.busy or self.commands or self.monitor or self._auth_state != "signed_in":
            return
        if not self.bridge or not self.bridge.running:
            self._set_auth_state("disconnected")
            return
        self._set_auth_state("signing_out")
        self.notice(f"Signing out of {APP_NAME}...")
        self._command(self.bridge.logout, self._signed_out, self._logout_failed)

    def _signed_out(self, result: dict) -> None:
        self._set_auth_state("signed_out")
        self.notice(f"Signed out of {APP_NAME}. Your local game and draft are retained.")

    def _logout_failed(self, reason: str) -> None:
        if self._auth_state == "signing_out":
            self._set_auth_state("signed_in" if self.bridge and self.bridge.running else "disconnected")
        self.notice("Could not sign out: " + reason, True)

    def _login_failed(self, reason: str) -> None:
        if not self._closing:
            self._end_login(reason, error=True)

    def cancel_sign_in(self) -> None:
        if self._closing or self._auth_state != "waiting" or self.commands or not self._login_id:
            return
        self._end_login("Sign-in cancelled. Click Sign in with ChatGPT to try again.")

    def _end_login(self, reason: str, *, error: bool = False) -> None:
        """Retire the attempt before asynchronous cleanup can deliver more events."""
        login_id, self._login_id = self._login_id, ""
        if self.monitor and self.monitor.login_id:
            self.monitor.stopped.set()
        bridge = self.bridge
        self._set_auth_state("cancelling")

        def cleanup():
            if bridge:
                try:
                    if login_id and bridge.running:
                        bridge.cancel_login(login_id)
                except Exception:
                    # Closing this isolated runtime also releases a pending login
                    # if its cancellation RPC fails or times out.
                    pass
                finally:
                    bridge.close()
            return {"bridge": bridge, "reason": reason, "error": error}

        self._command(cleanup, self._login_ended, self._login_cleanup_failed)

    def _login_ended(self, result: dict) -> None:
        if self.bridge is result["bridge"]:
            self.bridge = None
        if not self._closing:
            self._set_auth_state("signed_out")
            self.notice(result["reason"], result["error"])

    def _login_cleanup_failed(self, reason: str) -> None:
        self.bridge = None
        if not self._closing:
            self._set_auth_state("signed_out")
            self.notice("Could not reset the sign-in connection: " + reason, True)

    def _login_started(self, result: dict) -> None:
        if self._closing:
            if self.bridge:
                self.bridge.close()
            return
        auth_url, login_id = result.get("authUrl", ""), result.get("loginId", "")
        self._login_id = login_id
        if not auth_url or not login_id:
            self._login_failed("Sign-in did not return a browser link. Try again.")
            return
        if not QDesktopServices.openUrl(QUrl(auth_url)):
            self._login_failed("Could not open the sign-in page. Check your default browser and try again.")
            return
        self.notice(f"Complete ChatGPT sign-in and workspace selection in your browser. {APP_NAME} will return after confirmation.")
        self._start_monitor(login_id=login_id)

    def _authenticate(self, bridge, mode: str, key: str) -> None:
        if self._authenticated_mode != "chatgpt":
            raise ValueError("Click Sign in with ChatGPT before sending a message.")

    def refresh_models(self) -> None:
        if self.busy or self.commands or self.monitor:
            return
        mode, key = "chatgpt", ""
        if self._authenticated_mode != "chatgpt":
            self.notice("Sign in with ChatGPT first, then click Refresh models.", True)
            return
        def refresh():
            bridge = self._ensure_bridge()
            self._authenticate(bridge, mode, key)
            return bridge.models()
        self.notice("Loading model choices...")
        self._command(refresh, self._models_received, self._model_refresh_failed, preserve_error=True)

    def _model_refresh_failed(self, error: object) -> None:
        failure = classify_request_failure(error)
        if failure.kind == "auth":
            self._set_auth_state("disconnected")
        self.notice("Could not load model choices. " + failure.message, True)

    def _models_received(self, models: list[dict]) -> None:
        if self._authenticated_mode != "chatgpt":
            return
        previous = self.model_combo.currentData()
        previous_effort = self.reasoning_combo.currentData() or ""
        self._model_catalog = {model_identifier(model): model for model in models
                               if isinstance(model, dict) and model_identifier(model)}
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for identifier, model in self._model_catalog.items():
            self.model_combo.addItem(model.get("displayName") or model.get("display_name") or identifier, identifier)
        index = self.model_combo.findData(previous)
        self.model_combo.setCurrentIndex(index if previous else -1)
        self.model_combo.blockSignals(False)
        self._populate_reasoning_choices(previous_effort if index >= 0 else "")
        self.notice("Model choices refreshed. Choose a model; account access is checked when you send a message."
                    if self._model_catalog else "No models were returned. Check your account access.",
                    not bool(self._model_catalog))
        self.update_controls()

    def refresh_sources(self) -> None:
        previous = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem(tr("Choose a VASL / VASSAL window"), None)
        try:
            for source in list_windows():
                self.source_combo.addItem(source.title, source)
            if previous:
                for index in range(self.source_combo.count()):
                    if getattr(self.source_combo.itemData(index), "handle", None) == previous.handle:
                        self.source_combo.setCurrentIndex(index)
                        break
        except CaptureError as exc:
            self.notice(str(exc), True)
        finally:
            self.source_combo.blockSignals(False)
        self.update_controls()

    def capture_current(self, checked=False, source: WindowSource | None = None) -> Observation | None:
        if self._selecting_screenshot:
            return None
        source = source or self.source_combo.currentData()
        if source is None:
            self.notice("Select a board window first.", True)
            return None
        try:
            self.preview = capture_window(source)
            self.preview_panel.set_observation(self.preview)
            self.notice("Screenshot captured locally. Select an area to attach it to a message.")
            self.update_controls()
            return self.preview
        except (CaptureError, ValueError) as exc:
            self.preview = None
            self.preview_panel.set_observation(None)
            self.notice(str(exc), True)
            self.update_controls()
            return None

    def _setup_hotkey(self, factory) -> None:
        shortcut = self.preferences.capture_shortcut
        # Offscreen checks must not reserve a real desktop shortcut.
        if factory is None and (sys.platform != "win32" or QApplication.platformName() == "offscreen"):
            self._display_capture_shortcut(shortcut)
            return
        self.capture_hotkey = (factory(self) if factory else
                              GlobalCaptureHotkey(self, target_window_getter=self._capture_target_handle,
                                                  application_window_getter=self._capture_application_handle))
        self.capture_hotkey.activated.connect(self.capture_from_hotkey)
        application_activation = getattr(self.capture_hotkey, "application_activated", None)
        if application_activation is not None:
            application_activation.connect(self.capture_and_select)
        status = self.capture_hotkey.start(shortcut)
        if status.success:
            self._display_capture_shortcut(status.shortcut)
        else:
            self.hotkey_label.setText(localize_error(status.message))
        if not status.success:
            self.notice(status.message, True)

    def configure_capture_shortcut(self) -> None:
        self.configure_settings()

    def open_openai_status(self) -> None:
        if not QDesktopServices.openUrl(QUrl("https://status.openai.com/")):
            self.notice("Could not open your browser. Visit https://status.openai.com/ to check OpenAI service status.", True)

    def configure_settings(self) -> None:
        if self._selecting_screenshot or self.voice.active or self._closing:
            return
        dialog = SettingsDialog(self.preferences, self)
        # Keep validation/registration errors in the open settings window.
        while dialog.exec() == QDialog.DialogCode.Accepted:
            if self.apply_preferences(dialog.preferences()):
                break
            dialog.error_label.setText(self.status.text())

    def apply_preferences(self, preferences: AppPreferences) -> bool:
        previous = self.preferences
        try:
            shortcut = preferences.capture_shortcut
            if shortcut != "":
                shortcut, _, _ = native_shortcut(shortcut)
        except ValueError as exc:
            self.notice(str(exc), True)
            return False
        changed = shortcut != previous.capture_shortcut
        needs_binding = changed or (bool(shortcut) and self.capture_hotkey is not None and
                                   not getattr(self.capture_hotkey, "is_registered", True))
        if needs_binding and self.capture_hotkey is not None:
            status = self.capture_hotkey.replace(shortcut)
            if not status.success:
                rollback_error = ""
                # A failed hook release still disables capture. Restore the
                # previous enabled choice before reporting that save failed.
                if previous.capture_shortcut and not getattr(self.capture_hotkey, "is_registered", True):
                    rollback_error = self._restore_capture_shortcut(previous.capture_shortcut)
                self.notice(status.message + rollback_error, True)
                return False
            shortcut = status.shortcut
        preferences = replace(preferences, capture_shortcut=shortcut)
        try:
            self.store.root.mkdir(parents=True, exist_ok=True)
            save_preferences(self.settings, preferences)
        except OSError as exc:
            rollback_error = ""
            if needs_binding and self.capture_hotkey is not None:
                rollback_error = self._restore_capture_shortcut(previous.capture_shortcut)
            # Restore in-memory INI values too; a future sync must not persist
            # a choice whose save was reported as failed.
            try:
                save_preferences(self.settings, previous)
            except OSError:
                pass
            self.notice(str(exc) + rollback_error, True)
            return False
        self.preferences = preferences
        language_changed = preferences.ui_language != previous.ui_language
        set_language(preferences.ui_language)
        if language_changed:
            localize_widget_tree(self)
            # Screenshot inspectors can belong to a chat bubble, whose stored
            # message subtree is deliberately excluded from chrome traversal.
            for dialog in self.findChildren(QDialog):
                localize_widget_tree(dialog)
            self._refresh_auth_labels()
            self._populate_reasoning_choices(self.reasoning_combo.currentData() or "")
            if self.source_combo.count() and self.source_combo.itemData(0) is None:
                self.source_combo.setItemText(0, tr("Choose a VASL / VASSAL window"))
            self.preview_panel.set_observation(self.preview_panel.observation)
            self._render_attachments()
            self._voice_state_changed(self.voice.state)
            self._refresh_memory_status()
            self._update_notes_status()
        self.setFont(ui_font(preferences, self.font()))
        self.message_edit.apply_preferences(preferences)
        for bubble in self._message_widgets.values():
            bubble.apply_preferences(preferences)
        if language_changed:
            self.render_session()
        self._display_capture_shortcut(shortcut)
        self.notice(tr("Settings saved. {detail}", detail=self.message_edit.toolTip()))
        return True

    def _display_capture_shortcut(self, shortcut: str) -> None:
        if not shortcut:
            self.hotkey_label.setText(tr("Capture shortcut: Disabled"))
            return
        suffix = "" if self.capture_hotkey else tr(" (unavailable in this session)")
        self.hotkey_label.setText(tr("Capture shortcut: {shortcut}{suffix}", shortcut=shortcut, suffix=suffix))

    def _restore_capture_shortcut(self, shortcut: str) -> str:
        restored = self.capture_hotkey.replace(shortcut)
        if restored.success:
            self._display_capture_shortcut(restored.shortcut)
            return ""
        # Unregister clears the binding even if Windows refuses hook cleanup,
        # so the remaining callback cannot consume another key or mouse event.
        self.capture_hotkey.unregister()
        self.hotkey_label.setText(tr("Capture shortcut disabled. Open Settings to retry."))
        return " The previous capture binding could not be restored; capture shortcuts are disabled."

    def set_capture_shortcut(self, sequence: str) -> bool:
        if self.capture_hotkey is None:
            self.notice("Global capture shortcuts are available in the Windows desktop application.", True)
            return False
        return self.apply_preferences(replace(self.preferences, capture_shortcut=sequence))

    def _capture_target_handle(self) -> int:
        if self._closing or self._selecting_screenshot or QApplication.activeModalWidget() is not None:
            return 0
        source = self.source_combo.currentData()
        return int(source.handle) if source else 0

    def _capture_application_handle(self) -> int:
        if self._closing or self._selecting_screenshot or QApplication.activeModalWidget() is not None:
            return 0
        # Limit companion input to this main window, including its child controls.
        return int(self.winId())

    def _bring_capture_window_forward(self) -> None:
        # Call only after freezing the board pixels (or after a failed attempt).
        # Activating the bridge earlier can remove a transient VASL LOS line.
        if self.isMinimized():
            self.showMaximized() if self.isMaximized() else self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32" and QApplication.platformName() not in {"offscreen", "minimal"}:
            if not bring_window_to_front(int(self.winId())):
                QApplication.alert(self)

    def capture_and_select(self) -> None:
        self._capture_for_selection(visible=False)

    def capture_from_hotkey(self) -> None:
        self._capture_for_selection(visible=True)

    def _capture_for_selection(self, *, visible: bool) -> None:
        if self._closing or self._selecting_screenshot:
            return
        source = self.source_combo.currentData()
        if source is None:
            self.notice(f"Select a VASL window in {APP_NAME} before using capture.", True)
            if visible:
                self._bring_capture_window_forward()
            return
        try:
            # Freeze the pixels before any dialog or window activation can clear LOS.
            observation = (capture_visible_window if visible else capture_window)(source, max_dimension=8192)
        except (CaptureError, ValueError) as exc:
            self.notice(str(exc), True)
            if visible:
                self._bring_capture_window_forward()
            return
        self._select_and_attach(observation)

    def select_preview(self) -> None:
        if self.preview and not self._selecting_screenshot:
            self._select_and_attach(self.preview)

    def _select_and_attach(self, observation: Observation) -> None:
        self._selecting_screenshot = True
        try:
            self._bring_capture_window_forward()
            dialog = self.selection_factory(observation, self)
            if isinstance(dialog, QWidget):
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                if sys.platform == "win32" and QApplication.platformName() not in {"offscreen", "minimal"}:
                    if not bring_window_to_front(int(dialog.winId())):
                        QApplication.alert(dialog)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_observation is not None:
                self.preview = dialog.selected_observation
                self.preview_panel.set_observation(self.preview)
                self.attach_preview()
            else:
                self.notice("Screenshot selection cancelled. No new attachment was added.")
        except (CaptureError, ValueError) as exc:
            self.notice(str(exc), True)
        finally:
            self._selecting_screenshot = False
            self.update_controls()

    def attach_preview(self) -> None:
        if self.preview and all(image.observation_id != self.preview.observation_id for image in self.attachments):
            if len(self.attachments) >= MAX_SCREENSHOTS_PER_MESSAGE:
                self.notice("Attach at most eight screenshots in one message.", True)
                return
            image_sizes = [len(image.png) for image in self.attachments] + [len(self.preview.png)]
            # The complete RPC also needs text, metadata and an outer envelope.
            if encoded_image_inputs_chars(image_sizes) >= MAX_PROTOCOL_CHARS:
                self.notice(ATTACHMENT_SIZE_NOTICE, True)
                return
            self.attachments.append(self.preview)
            self._render_attachments()
            if len(self.attachments) == MAX_SCREENSHOTS_PER_MESSAGE:
                self.notice(f"{MAX_SCREENSHOTS_PER_MESSAGE} screenshots attached. This message has reached the limit.")
            else:
                self.notice("Screenshot attached. Send shares this exact image, even if the preview changes.")
            self.update_controls()

    def _remove_attachment(self, observation_id: str) -> None:
        previous_count = len(self.attachments)
        self.attachments = [image for image in self.attachments if image.observation_id != observation_id]
        self._render_attachments()
        if len(self.attachments) < previous_count:
            self.notice("Screenshot removed.")
        self.update_controls()

    def _render_attachments(self) -> None:
        while self.attachment_layout.count():
            item = self.attachment_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for image in self.attachments:
            self.attachment_layout.addWidget(self._button(tr("Screenshot"), lambda checked=False, attached=image: inspect_observation(self, attached)))
            self.attachment_layout.addWidget(self._button(tr("Remove"), lambda checked=False, identifier=image.observation_id: self._remove_attachment(identifier)))
        self.attachment_layout.addStretch()
        self.attachment_box.setVisible(bool(self.attachments))

    def _send_from_shortcut(self) -> None:
        if self.send_button.isEnabled():
            self.send_message()

    def toggle_voice(self) -> None:
        if self._closing:
            return
        if self.voice.state == "recording":
            self.voice.stop()
            return
        if self.voice.active:
            return
        prefs = self.preferences
        config = VoiceConfig(prefs.voice_backend, prefs.voice_program_path,
                             prefs.voice_model_path, prefs.voice_language)
        self._voice_session_id = self.session.session_id
        try:
            self.voice.start(config)
            self.notice("Recording locally. Click Stop recording when finished (maximum two minutes).")
        except (VoiceError, OSError, RuntimeError) as exc:
            self._voice_session_id = ""
            self.notice(str(exc), True)
        self.update_controls()

    def cancel_voice(self) -> None:
        self._voice_session_id = ""
        self.voice.cancel()
        self.notice("Voice input cancelled. Your draft and attachments are retained.")

    def _voice_state_changed(self, state: str) -> None:
        self.voice_button.setText(tr({"recording": "Stop recording", "transcribing": "Transcribing..."}.get(state, "Record voice")))
        self.voice_label.setText(tr({"recording": "Recording locally", "transcribing": "Local transcription"}.get(state, "Local voice")))
        self.update_controls()

    def _voice_transcript(self, text: str) -> None:
        session_id, self._voice_session_id = self._voice_session_id, ""
        if self._closing or not session_id or session_id != self.session.session_id:
            return
        # Append to the current draft, including edits made during transcription.
        # Recognition never sends the text or the recording to the AI service.
        current = self.message_edit.toPlainText()
        cursor = self.message_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(("\n" if current and not current.endswith("\n") else "") + text)
        self.message_edit.setTextCursor(cursor)
        self.message_edit.setFocus()
        self.notice("Voice added to your draft. Check the words, then Send when ready.")

    def _voice_failed(self, message: str) -> None:
        self._voice_session_id = ""
        if not self._closing:
            self.notice(message, True)
        self.update_controls()

    def send_message(self) -> None:
        if self._closing or self.voice.active:
            return
        text = self.message_edit.toPlainText().strip()
        if self._preparing or (not text and not self.attachments):
            return
        if self._active_turn:
            self._steer(text, list(self.attachments))
        else:
            self._start_turn(text, list(self.attachments))

    def _start_turn(self, text: str, attachments: list[Observation], *, report: bool = False,
                    review: bool = False) -> None:
        if review:
            self.review_game()
            return
        mode, key, model = "chatgpt", "", self.model_combo.currentData()
        selected_effort = self.reasoning_combo.currentData() or ""
        if mode == "chatgpt" and self._authenticated_mode != "chatgpt":
            self.notice("Click Sign in with ChatGPT first.", True)
            return
        if not model:
            self.notice("Choose a model first.", True)
            return
        if not self._ensure_game_folder():
            return
        submitted_draft = None if report or review else {
            "text": self.message_edit.toPlainText(),
            "image_ids": [image.observation_id for image in attachments],
        }
        previous_messages = list(self.session.messages)
        user_message = self.session.add_message("user", text, attachments=attachments)
        application_ids = self.session.legacy_metadata.setdefault("asl_ai_application_message_ids", [])
        if report or review:
            application_ids.append(user_message.message_id)
        assistant = self.session.add_message("assistant", status="pending")
        self._assistant_id = assistant.message_id
        self._memory_context = {
            "model": model, "thread_id": "", "turn_id": "request-" + new_id(),
            "anchor_message_id": user_message.message_id,
            "assistant_message_id": assistant.message_id,
            "game_folder": self.game_folder,
            "mode": "save" if report else "review" if review else "normal",
            "application_message_ids": list(application_ids),
        }
        self._item_messages.clear()
        self._last_turn_error = None
        self._preparing, self._stop_requested = True, False
        self._save_requested = (report or self._reviewing_report) and not review
        self._review_requested = review
        if review:
            self._reviewing_report = False
        preparing_report = self._save_requested
        self._report_ready = False
        # Corrections after a report retain the save workflow; the newest completed
        # assistant reply becomes the report only when the player explicitly saves.
        self.render_session(scroll_to_bottom=True)
        if not self._autosave():
            assistant.status = "failed"
            self._preparing, self._assistant_id = False, ""
            self.update_controls()
            return
        self.notice("Connecting to AI...")
        def start():
            bridge = self._ensure_bridge()
            self._authenticate(bridge, mode, key)
            fetched_models = None
            catalog = self._model_catalog.get(model, {})
            if selected_effort and not catalog:
                fetched_models = bridge.models()
                catalog = next((row for row in fetched_models if isinstance(row, dict) and model_identifier(row) == model), {})
            effort = self._resolve_reasoning_effort(model, selected_effort, catalog)
            prefix = ""
            same_mode = self.session.backend_mode == mode
            if not bridge.thread_id or self._needs_resume or not same_mode or model != self._thread_model:
                resumed = False
                if (same_mode and self.session.backend_thread_id
                        and self.session.legacy_metadata.get("asl_ai_runtime_tools_version") == RUNTIME_TOOLS_VERSION):
                    try:
                        bridge.resume(self.session.backend_thread_id, model=model)
                        resumed = True
                    except Exception:
                        pass
                if not resumed:
                    bridge.start_thread(model=model)
                    if previous_messages:
                        prefix = self._recovery_context(previous_messages)
                elif self._needs_resume:
                    prefix = "We are continuing a loaded game. Recheck the saved situation with the player before continuing.\n"
            if preparing_report and not report:
                prefix += ("The player is reviewing a game save report. Incorporate the following correction or check, "
                           "then return the complete revised report for the player to review.\n")
            if self._closing:
                bridge.close()
                raise RuntimeError("The application is closing. No new AI request was started.")
            connection = (f"Application connection for this message: selected model {model}; "
                          f"requested reasoning effort {effort or 'model default (unspecified)'}. "
                          "This is application request metadata, not independent verification of service routing.\n\n")
            self._memory_context["thread_id"] = bridge.thread_id
            turn_id = bridge.start_turn(prefix + connection + self._reference_context() + self._memory_prompt() + text,
                                        images=[image.data_url for image in attachments], effort=effort)
            return {"turn_id": turn_id, "thread_id": bridge.thread_id, "mode": mode, "model": model,
                    "effort": effort, "selected_effort": selected_effort, "models": fetched_models,
                    "submitted_draft": submitted_draft}
        self._command(start, self._turn_started, self._turn_failed, preserve_error=True)

    def _turn_started(self, result: dict) -> None:
        if self._closing:
            if self.bridge:
                self.bridge.close()
            self._turn_failed("AI request stopped while the application was closing.")
            return
        submitted = result.get("submitted_draft")
        if submitted is not None:
            if self.message_edit.toPlainText() == submitted["text"]:
                self.message_edit.clear()
            identifiers = set(submitted["image_ids"])
            self.attachments = [image for image in self.attachments if image.observation_id not in identifiers]
            self._render_attachments()
        self._preparing = False
        self._active_turn = result["turn_id"]
        self._memory_context.update({"turn_id": result["turn_id"], "thread_id": result["thread_id"]})
        self.session.backend_thread_id, self.session.backend_mode = result["thread_id"], result["mode"]
        self.session.legacy_metadata["asl_ai_runtime_tools_version"] = RUNTIME_TOOLS_VERSION
        self._thread_model = result["model"]
        self._thread_effort = result["effort"]
        records = self.session.legacy_metadata.setdefault("asl_ai_model_requests", [])
        if isinstance(records, list):
            records.append({"turn_id": result["turn_id"], "model": result["model"],
                            "selected_effort": result["selected_effort"], "requested_effort": result["effort"],
                            "recorded_at": now_utc(), "source": "application_request"})
        if result.get("models"):
            self._models_received(result["models"])
        self._needs_resume = False
        self._start_monitor(turn_id=self._active_turn)
        self._autosave()
        self.notice(f"AI is thinking | {result['model']} | Reasoning: {reasoning_effort_label(result['effort'])}. You can send another message or click Stop.")
        self.update_controls()
        if self._stop_requested:
            self.stop_request()

    def _start_monitor(self, *, login_id: str = "", turn_id: str = "") -> None:
        self.monitor = EventWorker(self.bridge, login_id=login_id, turn_id=turn_id)
        worker = self.monitor
        worker.event_received.connect(self._monitor_event)
        worker.failed.connect(self._monitor_error)
        worker.finished.connect(lambda: self._monitor_finished(worker))
        worker.start()
        self.update_controls()

    def _monitor_event(self, worker: EventWorker, event: dict) -> None:
        if self._closing or self.monitor is not worker or worker.stopped.is_set():
            return
        if worker.login_id and (self._auth_state != "waiting" or self._login_id != worker.login_id):
            return
        self._handle_event(event)

    def _monitor_error(self, worker: EventWorker, reason: str) -> None:
        if not self._closing and self.monitor is worker and not worker.stopped.is_set():
            self._monitor_failed(reason)

    def _monitor_finished(self, worker: EventWorker) -> None:
        if self.monitor is worker:
            self.monitor = None
        worker.deleteLater()
        self.update_controls()
        self._refresh_models_if_ready()

    def _monitor_failed(self, reason: str) -> None:
        if self._auth_state == "waiting":
            self._login_failed(reason)
        elif self.busy:
            self._turn_failed(reason)
        else:
            self._set_auth_state("signed_out" if self._auth_state == "waiting" else "disconnected")
            self.notice(reason, True)

    def _handle_event(self, event: dict) -> None:
        method, params = event.get("method", ""), event.get("params", {})
        if method in {"bridge/error", "bridge/closed"}:
            reason = str(params.get("message", "The model connection closed. Send again to reconnect."))
            if self._auth_state == "waiting":
                self._login_failed(reason)
                return
            self._set_auth_state("disconnected")
            if self.busy:
                self._turn_failed(reason)
            else:
                self.notice(reason, True)
            return
        if method == "account/updated":
            if self._auth_state in {"waiting", "cancelling"}:
                return
            if params.get("authMode") != "chatgpt":
                self._set_auth_state("signed_out")
            return
        if method == "account/login/completed":
            if (self._auth_state != "waiting" or not self._login_id or not self.monitor
                    or params.get("loginId") != self._login_id or self.monitor.login_id != self._login_id):
                return
            if params.get("success"):
                self._login_id = ""
                self._set_auth_state("signed_in")
                self._models_refresh_pending = True
                self.notice("Signed in. Loading model choices...")
                self._return_after_login()
            else:
                self._login_failed(str(params.get("error") or "Sign-in was not completed. Try again."))
            self.update_controls()
            return
        if not self._active_turn or params.get("threadId") not in (None, self.session.backend_thread_id):
            return
        if method == "model/rerouted":
            records = self.session.legacy_metadata.get("asl_ai_model_requests", [])
            for record in reversed(records if isinstance(records, list) else []):
                if isinstance(record, dict) and record.get("turn_id") == params.get("turnId"):
                    record["rerouted_from_model"] = params.get("fromModel")
                    record["rerouted_to_model"] = params.get("toModel")
                    break
            self._autosave()
            self.notice(f"The service changed this reply's model from {params.get('fromModel')} to {params.get('toModel')}.", True)
            return
        item = params.get("item", {})
        if item.get("type") == "dynamicToolCall":
            if method == "item/started":
                self.notice("AI is reading local game records or selected references...")
            elif method == "item/completed" and (item.get("success") is False or item.get("status") == "failed"):
                self.notice("AI could not read the requested local record or reference. Its reply may lack source evidence.", True)
            return
        if item.get("type") == "contextCompaction":
            self.notice("AI is compacting the conversation. This may take a few minutes." if method == "item/started" else
                        "Conversation compacted. Your complete local game record is still available.")
        elif method == "item/agentMessage/delta":
            message = self._assistant_for_item(str(params.get("itemId", "")))
            message.text += str(params.get("delta", ""))
            message.status = "streaming"
            self.render_session()
        elif method == "item/completed" and item.get("type") == "agentMessage":
            message = self._assistant_for_item(str(item.get("id", "")))
            message.text = str(item.get("text", message.text))
            message.status = "completed"
            self.render_session()
            self._autosave()
        elif method == "turn/completed":
            self._turn_completed(params.get("turn", {}))
        elif method == "error":
            error = params.get("error", params)
            self._last_turn_error = error
            if params.get("willRetry"):
                self.notice("The AI connection was interrupted. The service is retrying...")
            else:
                self.notice(classify_request_failure(error).message, True)

    def _assistant_for_item(self, item_id: str) -> ChatMessage:
        identifier = self._item_messages.get(item_id)
        if identifier:
            return self.session.get_message(identifier)
        message = self.session.get_message(self._assistant_id) if not self._item_messages else self.session.add_message("assistant", status="streaming")
        self._assistant_id = message.message_id
        self._item_messages[item_id] = message.message_id
        return message

    def _turn_completed(self, turn: dict) -> None:
        if turn.get("id") != self._active_turn:
            return
        interrupted = turn.get("status") == "interrupted" or self._stop_requested
        failed = turn.get("status") == "failed"
        failure = classify_request_failure(turn.get("error") or self._last_turn_error or "AI request failed.") if failed else None
        message = self.session.get_message(self._assistant_id)
        if message:
            message.status = "interrupted" if interrupted else "failed" if failed else "completed"
            if failed and not interrupted:
                message.text = (message.text.rstrip() + "\n\n" if message.text else "") + failure.message
                self.session.legacy_metadata.setdefault("asl_ai_request_failures", {})[message.message_id] = failure.message
        if self._save_requested and not interrupted and not failed and message and message.text.strip():
            self.session.save_report = message.text
            self._report_ready = True
            self._reviewing_report = True
            self.notice("Review the report above. Correct it in chat if needed, then click Save point... to name it.")
        else:
            self.notice(failure.message if failed else "AI stopped. Your conversation is retained; send a new message to continue." if interrupted else "Ready.", failed)
        self._active_turn, self._assistant_id, self._save_requested = "", "", False
        self._review_requested = False
        self._last_turn_error = None
        if failure and failure.kind == "auth":
            self._set_auth_state("disconnected")
        self.render_session()
        saved = self._autosave()
        if failure and not saved:
            self.notice(failure.message + " Local progress could not be saved: " + self.save_error, True)
        self._refresh_memory_status()

    def _turn_failed(self, reason: object) -> None:
        failure = (RequestFailure("size", REQUEST_SIZE_NOTICE)
                   if isinstance(reason, RequestSizeError) and reason.method == "turn/start"
                   else classify_request_failure(reason))
        message = self.session.get_message(self._assistant_id)
        if message:
            message.status = "failed"
            message.text = (message.text.rstrip() + "\n\n" if message.text else "") + failure.message
            self.session.legacy_metadata.setdefault("asl_ai_request_failures", {})[message.message_id] = failure.message
        self._preparing, self._active_turn, self._assistant_id = False, "", ""
        self._save_requested = False
        self._review_requested = False
        self._last_turn_error = None
        if failure.kind == "auth":
            self._set_auth_state("disconnected")
        self.render_session()
        saved = self._autosave()
        self.notice(failure.message + (" Local progress could not be saved: " + self.save_error if not saved else ""), True)

    def _steer(self, text: str, attachments: list[Observation]) -> None:
        if self.commands:
            return
        turn_id = self._active_turn
        draft_text = self.message_edit.toPlainText()
        def send():
            if self.bridge.turn_id != turn_id:
                raise ValueError("AI has finished this response. Send your message again to start the next response.")
            return self.bridge.steer(self._reference_context() + text, images=[image.data_url for image in attachments])
        def accepted(_result):
            self.session.add_message("user", text, attachments=attachments)
            if self.message_edit.toPlainText() == draft_text:
                self.message_edit.clear()
            identifiers = {image.observation_id for image in attachments}
            self.attachments = [image for image in self.attachments if image.observation_id not in identifiers]
            self._render_attachments()
            self.render_session(scroll_to_bottom=True)
            self._autosave()
            self.notice("Additional message sent to the current AI response.")
        def failed(reason: object) -> None:
            if isinstance(reason, RequestSizeError) and reason.method == "turn/steer":
                self.notice(REQUEST_SIZE_NOTICE, True)
            else:
                self.notice("Could not send during this response: " + (str(reason) or "The operation failed.") +
                            " Your draft is retained. Click Stop, then Send to continue.", True)
        self._command(send, accepted, failed, preserve_error=True)

    def stop_request(self) -> None:
        if self._preparing:
            self._stop_requested = True
            self.notice("Stopping as soon as the AI request starts...")
            self.update_controls()
        elif self._active_turn and self.bridge:
            self._stop_requested = True
            self.notice("Stopping AI...")
            self._command(self.bridge.interrupt, lambda result: None,
                          self._stop_failed)

    def _stop_failed(self, reason: str) -> None:
        self._stop_requested = False
        self.notice("Could not stop AI: " + reason + " You can try Stop again.", True)
        self.update_controls()

    def request_save_report(self) -> None:
        if not self.busy and self.session.messages:
            self._start_turn(SAVE_REQUEST, [], report=True)

    def review_game(self) -> None:
        self.notice("Whole-game review is temporarily unavailable while its usage cost is being optimized. You can still ask about specific recorded events.")

    def _choose_game_folder(self, session: ChatSession) -> Path | None:
        parent = Path(str(self.settings.value("game_parent", str(Path.home() / "Documents"))))
        dialog = NewGameDialog(self, default_parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        parent, name = dialog.selection()
        folder = self.folders.create_folder(parent, name, session)
        self.settings.setValue("game_parent", str(parent))
        return folder

    def _ensure_game_folder(self) -> bool:
        if self.game_folder is not None:
            return True
        try:
            folder = self._choose_game_folder(self.session)
            if folder is None:
                return False
            self.game_folder = self.checkpoint_path = folder
            self.recovery_path = self.folders.database_path(folder)
            return True
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return False

    def save_session(self, checked=False, path: Path | None = None) -> Path | None:
        if self.busy or not self._report_ready or not self.session.save_report:
            self.notice("Finish and review the AI game report before saving.", True)
            return None
        try:
            if path is not None:
                # Explicit programmatic legacy export; ordinary UI saves use
                # named points and never ask for a file destination again.
                return self.store.save(self.session, path)
            if not self._ensure_game_folder():
                return None
            points = self.folders.list_points(self.game_folder)
            dialog = SavePointDialog(points, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            name = dialog.point_name()
            exists = any(point["name"].casefold() == name.casefold() for point in points)
            if exists and QMessageBox.question(self, tr("Overwrite save point?"),
                    tr("Replace '{name}' with the current progress?", name=name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return None
            self.folders.save_point(self.game_folder, self.session, name, overwrite=exists)
            self.checkpoint_path = self.game_folder
            self.save_error = ""
            self._reviewing_report = False
            self._report_ready = False
            self.update_controls()
            self.notice(f"Save point '{name}' saved in {self.game_folder}. Original images and references are shared.")
            return self.checkpoint_path
        except (StorageError, OSError, ValueError) as exc:
            self.save_error = str(exc)
            self.notice(str(exc), True)
            return None

    def _autosave(self) -> bool:
        try:
            if self.game_folder is None:
                if self.session.messages:
                    raise StorageError("Create or open a game folder before saving conversation progress.")
                return True
            self.folders.save_current(self.game_folder, self.session)
            self.recovery_path = self.folders.database_path(self.game_folder)
            with self._session_lock:
                self._tool_snapshot = copy.deepcopy(self.session)
            self.save_error = ""
            return True
        except (StorageError, OSError, ValueError) as exc:
            self.save_error = str(exc)
            self.notice("Could not update the local conversation record.", True)
            return False

    def _preserve_current(self) -> bool:
        return not (self.session.messages or self.game_folder) or self._autosave()

    def open_session(self) -> None:
        if self.busy or self.commands or self.monitor or self.voice.active:
            return
        dialog = OpenGameDialog(self.folders, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            folder, point_id = dialog.selection()
            self.load_checkpoint(folder, point_id=point_id)

    def load_checkpoint(self, path: Path, *, point_id: str | None = None) -> bool:
        if self.voice.active:
            self.notice("Finish or cancel voice input before opening a game.", True)
            return False
        if self.busy or self.commands or self.monitor:
            self.notice("Finish or stop the current request before opening a game.", True)
            return False
        if not self._preserve_current():
            return False
        try:
            if Path(path).is_dir():
                folder = Path(path).resolve()
                loaded = self.folders.load_point(folder, point_id)
            else:
                loaded = self.store.load(path)
                folder = self._choose_game_folder(loaded)
                if folder is None:
                    return False
            library_warning = ""
            try:
                self.library.adopt_references(loaded.references)
            except (StorageError, OSError, ValueError) as exc:
                library_warning = " The saved originals remain usable, but could not be added to the library: " + str(exc)
            self.session = loaded
            self.game_folder = self.checkpoint_path = folder
            # A manually opened snapshot is an earlier point in time, even when
            # the local runtime now holds a later continuation of the same game.
            self.session.backend_thread_id = ""
            self.recovery_path = self.folders.database_path(folder)
            self._memory_context = {"game_folder": folder}
            with self._session_lock:
                self._tool_snapshot = copy.deepcopy(self.session)
            self._needs_resume, self._report_ready = True, bool(self.session.save_report)
            self._reviewing_report = False
            self.attachments.clear()
            self.message_edit.clear()
            self._reset_chat()
            self.render_session(scroll_to_bottom=True)
            self._render_attachments()
            self._refresh_memory_status()
            self.notice("Game opened. Send a message and screenshot to recheck the board." + library_warning, bool(library_warning))
            return True
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return False

    def new_session(self) -> None:
        if self.busy or self.commands or self.monitor or self.voice.active:
            return
        if not self._preserve_current():
            return
        try:
            references = self.library.selected_references()
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return
        proposed = ChatSession(references=references)
        try:
            folder = self._choose_game_folder(proposed)
            if folder is None:
                return
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return
        self.session = proposed
        self.game_folder = self.checkpoint_path = folder
        self.recovery_path = self.folders.database_path(folder)
        self._memory_context = {"game_folder": folder}
        with self._session_lock:
            self._tool_snapshot = copy.deepcopy(self.session)
        self._needs_resume, self._report_ready = True, False
        self._reviewing_report = False
        self.message_edit.clear()
        self.attachments.clear()
        self._render_attachments()
        self._reset_chat()
        self.render_session()
        self._refresh_memory_status()
        self.notice("New game. Your reference selection and shared experience are retained; earlier games remain in their folders.")

    def manage_storage(self) -> None:
        if self.busy or self.commands or self.monitor or self.voice.active or not self._preserve_current():
            return
        dialog = StorageManagerDialog(self.folders, active_folder=self.game_folder, parent=self)
        dialog.screenshotsCleared.connect(self._screenshots_cleared)
        dialog.exec()

    def _screenshots_cleared(self, folder: str) -> None:
        if self.game_folder is None or Path(folder).resolve() != self.game_folder.resolve():
            return
        try:
            loaded = self.folders.load_point(self.game_folder)
        except (OSError, ValueError, StorageError) as exc:
            self.save_error = "Screenshot cleanup finished, but current progress could not be reloaded: " + str(exc)
            self.notice(self.save_error, True)
            return
        self.session = loaded
        self.session.backend_thread_id = ""
        self._needs_resume = True
        self._memory_context = {"game_folder": self.game_folder}
        deleted = set(self.session.legacy_metadata.get("asl_ai_deleted_images", {}))
        self.attachments = [image for image in self.attachments if image.observation_id not in deleted]
        if self.preview is not None and self.preview.observation_id in deleted:
            self.preview = None
            self.preview_panel.set_observation(None)
        self._render_attachments()
        self._reset_chat()
        self.render_session()
        self._refresh_memory_status()
        self.notice("Stored board screenshots were cleared. Chat, tactical notes, lessons and references remain.")
        self._autosave()

    def add_files(self) -> None:
        if self.busy or self.commands or self.monitor:
            return
        # Translate filter captions, retaining every supported extension verbatim.
        file_filter = REFERENCE_FILE_FILTER
        for caption in ("Supported references", "Documents", "Text files", "Images"):
            file_filter = file_filter.replace(caption + " (", tr(caption) + " (")
        selected, _ = QFileDialog.getOpenFileNames(self, tr("Add local rules or references"), "", file_filter)
        for name in selected:
            self.import_reference(Path(name))

    def import_reference(self, path: Path) -> bool:
        if self.busy or self.commands or self.monitor:
            self.notice("Finish or stop the current reply before changing references.", True)
            return False
        try:
            reference = self.library.import_file(path)
            self.library.adopt_references(self.session.references)
            hashes = {item.sha256 for item in self.session.references} | {reference.sha256}
            identifiers = [item.reference_id for item in self.library.list_references() if item.sha256 in hashes]
            self._apply_reference_selection(identifiers)
            self.render_session()
            if not self._autosave():
                return False
            self.notice(f"Added {reference.name} to your local library and selected it for this game. Send a message to ask AI to read it.")
            return True
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return False

    def _apply_reference_selection(self, identifiers: list[str], *, removed_ids=()) -> None:
        existing = {item.sha256: item for item in self.session.references}
        selected = (self.library.remove_references(removed_ids, selected_ids=identifiers)
                    if removed_ids else self.library.set_selected(identifiers))
        snapshots = []
        for item in selected:
            previous = existing.get(item.sha256)
            if previous and not is_supported_reference(previous.name) and is_supported_reference(item.name):
                # An explicit supported re-import repairs legacy reader metadata
                # without mutating its old snapshot or previously saved archive.
                item = replace(previous, name=item.name, media_type=item.media_type,
                               source_name=item.source_name, indexed_text=item.indexed_text,
                               extraction_error=item.extraction_error)
            elif previous:
                item = previous
            snapshots.append(item)
        self.session.references = snapshots
        self.session.updated_at = now_utc()
        with self._session_lock:
            self._tool_snapshot = copy.deepcopy(self.session)

    def choose_references(self) -> None:
        if self.busy or self.commands or self.monitor:
            return
        try:
            self.library.adopt_references(self.session.references)
            references = self.library.list_references()
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Reference library")
        layout = QVBoxLayout(dialog)
        hint = QLabel("Check the files AI may read in this game. Unchecked files stay in your library. "
                      "Highlight files to remove them from the library; use Ctrl or Shift to select several. "
                      "Changes apply when you click OK. Your original files are kept.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        listing = QListWidget()
        listing.setObjectName("reference_library_files")
        listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        removed_ids: set[str] = set()
        hashes = {item.sha256 for item in self.session.references}
        for reference in references:
            item = QListWidgetItem(reference.name, listing)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if reference.sha256 in hashes else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, reference.reference_id)
            capabilities = reference_capabilities(reference.name)
            reader = tr("Image reader" if capabilities["image_available"] else "Text and page image readers" if capabilities["page_images_available"] else "Text reader" if capabilities["text_supported"] else "Archived original; no supported reader")
            item.setToolTip(tr("{size} bytes | {reader} | Local original preserved", size=f"{reference.size_bytes:,}", reader=reader) +
                            (f"\n{localize_error(reference.extraction_error)}" if reference.extraction_error else ""))
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        remove_button = buttons.addButton("Remove from library...", QDialogButtonBox.ButtonRole.ActionRole)
        remove_button.setObjectName("remove_library_references")
        remove_button.setEnabled(False)
        listing.itemSelectionChanged.connect(lambda: remove_button.setEnabled(bool(listing.selectedItems())))

        def remove_highlighted() -> None:
            items = listing.selectedItems()
            if not items:
                return
            names = "\n".join(item.text() for item in items)
            prompt = tr("Remove these {count} file(s) from the library and this game's references?\n\n{names}\n\n"
                      "Your original files on disk will not be changed. "
                      "Click OK in Reference library to apply the removal, or Cancel to keep everything.", count=len(items), names=names)
            if QMessageBox.question(dialog, tr("Remove from library?"), prompt,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            for item in items:
                removed_ids.add(item.data(Qt.ItemDataRole.UserRole))
                listing.takeItem(listing.row(item))
            remove_button.setEnabled(bool(listing.selectedItems()))

        remove_button.clicked.connect(remove_highlighted)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(640, 440)
        localize_widget_tree(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        identifiers = [listing.item(index).data(Qt.ItemDataRole.UserRole) for index in range(listing.count())
                       if listing.item(index).checkState() == Qt.CheckState.Checked]
        try:
            self._apply_reference_selection(identifiers, removed_ids=removed_ids)
            self.render_session()
            saved = self._autosave()
            space_error = ""
            if removed_ids:
                try:
                    self.library.compact()
                except (StorageError, OSError, ValueError) as exc:
                    space_error = str(exc)
            if not saved:
                if removed_ids:
                    message = "Files were removed from the library, but the game's reference change could not be saved."
                    if space_error:
                        message += " Storage space could not be reclaimed: " + space_error
                    self.notice(message, True)
            elif space_error:
                self.notice("Files were removed from the library, but storage space could not be reclaimed: " + space_error, True)
            elif removed_ids:
                self.notice(f"Removed {len(removed_ids)} file(s) from the library and this game's references. Your original files are unchanged.")
            else:
                self.notice(f"{len(self.session.references)} reference files selected for this game.")
        except (StorageError, OSError, ValueError) as exc:
            self.notice(str(exc), True)

    def _reset_chat(self) -> None:
        # A newly opened game/save point starts concealed, including reopening
        # the same game. Disclosure is a viewing choice, never saved game data.
        self._chat_scroll_pending = False
        self._chat_scroll_user_away = False
        self.notes_toggle.setChecked(False)
        self._notes_by_message = {}
        for widget in self._message_widgets.values():
            self.chat_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self._message_widgets.clear()

    def _display_message(self, message: ChatMessage) -> str:
        """Translate application chrome without rewriting chat or model output."""
        if message.role == "user" and message.text == SAVE_REQUEST:
            return tr("Prepare a game save report for me to review.")
        if message.role == "user" and message.text == REVIEW_REQUEST:
            return tr("Review the game up to the current stopping point.")
        failures = self.session.legacy_metadata.get("asl_ai_request_failures", {})
        suffix = failures.get(message.message_id) if isinstance(failures, dict) else None
        if isinstance(suffix, str) and suffix and message.text.endswith(suffix):
            return message.text[:-len(suffix)] + localize_error(suffix)
        return message.text

    def _chat_scroll_user_position(self, value: int) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        # An explicit upward move cancels following even within the render
        # path's near-bottom tolerance. Only the actual bottom resumes it.
        self._chat_scroll_user_away = value < bar.maximum()
        if self._chat_scroll_user_away:
            self._chat_scroll_pending = False

    def _chat_scroll_pressed(self) -> None:
        self._chat_scroll_pending = False
        self._chat_scroll_user_away = True

    def _chat_scroll_released(self) -> None:
        self._chat_scroll_user_position(self.chat_scroll.verticalScrollBar().value())

    def _chat_scroll_action(self, _action: int) -> None:
        self._chat_scroll_user_position(self.chat_scroll.verticalScrollBar().sliderPosition())

    def _chat_scroll_value_changed(self, value: int) -> None:
        # Range/layout updates can move the value before the new content settles.
        # Real slider/wheel/key actions above cancel following even during a fit.
        if (self._chat_scroll_adjusting or self._chat_scroll_rendering
                or self.chat_content.has_pending_fits):
            return
        self._chat_scroll_user_position(value)

    def _apply_chat_scroll(self, *_args) -> None:
        if not self._chat_scroll_pending or self._chat_scroll_user_away:
            return
        bar = self.chat_scroll.verticalScrollBar()
        self._chat_scroll_adjusting = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._chat_scroll_adjusting = False
        if not _args and not self._chat_scroll_rendering and not self.chat_content.has_pending_fits:
            self._chat_scroll_pending = False

    def render_session(self, *, scroll_to_bottom: bool = False) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        if scroll_to_bottom:
            self._chat_scroll_user_away = False
        if scroll_to_bottom or (not self._chat_scroll_user_away and bar.value() >= bar.maximum() - 30):
            self._chat_scroll_pending = True
        # Keep this render's intent until the measured rows and scroll range agree.
        self._chat_scroll_rendering = True
        try:
            self.welcome.setVisible(not self.session.messages)
            for message in self.session.messages:
                widget = self._message_widgets.get(message.message_id)
                if widget is None:
                    widget = MessageBubble(message, self.chat_content, preferences=self.preferences,
                                           inspect_callback=inspect_observation,
                                            display_text=self._display_message)
                    self._message_widgets[message.message_id] = widget
                    self.chat_layout.addWidget(widget)
                else:
                    widget.update_message(message)
                widget.set_notes(self._notes_by_message.get(message.message_id, []))
        finally:
            self._chat_scroll_rendering = False
        self._apply_chat_scroll()
        names = "\n".join(reference.name for reference in self.session.references)
        self.reference_label.setText(tr("Selected for this game: {count}\n", count=len(self.session.references)) + (names or tr("No references selected")))
        self.session_label.setText(tr("Game {game_id} | {count} messages", game_id=self.session.session_id[:8], count=len(self.session.messages)))
        self.update_controls()

    def update_controls(self, *args) -> None:
        if not hasattr(self, "status"):
            return
        auth_wait = bool(self.monitor and not self._active_turn)
        action_busy = self.busy or bool(self.commands) or auth_wait
        ready = self._authenticated_mode == "chatgpt"
        model_ready = ready and self.model_combo.currentData() in self._model_catalog
        self.send_button.setEnabled(model_ready and not self._preparing and not self.commands and not auth_wait and
                                    not self.voice.active and not self._closing and
                                    bool(self.message_edit.toPlainText().strip() or self.attachments))
        self.cancel_button.setEnabled(self.busy and not self._stop_requested)
        for control in (self.login_button, self.new_button,
                        self.open_button, self.review_button, self.storage_button,
                        self.add_files_button, self.manage_files_button):
            control.setEnabled(not action_busy)
        self.model_combo.setEnabled(ready and bool(self._model_catalog) and not action_busy)
        self.reasoning_combo.setEnabled(model_ready and not action_busy)
        self.models_button.setEnabled(ready and not action_busy)
        can_cancel_login = self._auth_state == "waiting" and bool(self._login_id) and not self.commands
        self.login_button.setEnabled(not self._closing and (can_cancel_login or (
            not action_busy and self._auth_state not in {"waiting", "cancelling", "signing_out"})))
        self.save_button.setEnabled(not action_busy and bool(self.session.messages))
        self.save_report_button.setVisible(self._report_ready)
        self.save_report_button.setEnabled(not action_busy and self._report_ready)
        self.capture_button.setEnabled(self.source_combo.currentData() is not None)
        self.attach_button.setEnabled(self.preview is not None)
        self.new_button.setEnabled(not action_busy and not self.voice.active)
        self.open_button.setEnabled(not action_busy and not self.voice.active)
        self.review_button.hide()
        self.review_button.setEnabled(False)
        self.storage_button.setEnabled(not action_busy and not self.voice.active)
        self.openai_status_button.setEnabled(not self._closing)
        self.settings_button.setEnabled(not self._closing and not self._selecting_screenshot and not self.voice.active)
        self.voice_button.setEnabled(not self._closing and self.voice.state != "transcribing")
        self.voice_cancel_button.setVisible(self.voice.active)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._voice_session_id = ""
        self.voice.cancel()
        self.connection_timer.stop()
        self.login_return_timer.stop()
        if self.monitor:
            self.monitor.stopped.set()
        if self.bridge and self.bridge.running:
            self.bridge.close()
        if (self.monitor and self.monitor.isRunning()) or any(worker.isRunning() for worker in self.commands):
            event.ignore()
            self.notice("Closing the AI connection...")
            QTimer.singleShot(100, self.close)
            return
        if not self._preserve_current():
            self._closing = False
            self.connection_timer.start(1000)
            self.notice("The conversation could not be saved. Keep this window open until the local save problem is resolved.", True)
            event.ignore()
            return
        if self.capture_hotkey:
            self.capture_hotkey.unregister()
        self.voice.close()
        event.accept()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setOrganizationName("ASL_AI")
    application.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "app.ico")))
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
