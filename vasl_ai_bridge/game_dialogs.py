"""Small player dialogs for game folders and their shared saved progress.

Creation and opening return a selection to the main window. Only the storage
manager invokes mutations, after an explicit confirmation for each deletion.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)


EXPERIENCE_HINT = (
    "Opening an earlier save point restores that point's game record. "
    "The shared experience library is not rolled back."
)
CLEAR_SCREENSHOTS_WARNING = (
    "Delete all board screenshots for this game? All save points and full-game "
    "review will lose access to these original images. Chat, tactical journals, "
    "lessons, and reference files will remain."
)
DELETE_GAME_WARNING = (
    "Delete this game's managed data? Current progress, all save points, "
    "screenshots, reference copies, tactical journals, and reviews for this "
    "game will be permanently deleted. The shared experience library and "
    "unrelated files in the game folder will remain. This cannot be undone."
)
_STORAGE_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 bytes"


def _record_folder(record: dict[str, Any]) -> Path:
    value = record.get("folder") or record.get("path")
    if not value:
        raise ValueError("The game folder is missing from its registration.")
    return Path(value)


def _game_label(record: dict[str, Any]) -> str:
    try:
        fallback = _record_folder(record).name
    except ValueError:
        fallback = "Unavailable game"
    return str(record.get("name") or record.get("title") or fallback)


def _point_id(point: dict[str, Any]) -> str:
    return str(point.get("id") or point.get("point_id") or "")


def _point_name(point: dict[str, Any]) -> str:
    return str(point.get("name") or point.get("title") or _point_id(point))


class NewGameDialog(QDialog):
    """Ask where a new game folder belongs; do not create it in this dialog."""

    def __init__(self, parent: QWidget | None = None, default_parent: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New game")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        introduction = QLabel("Choose where to create this game's folder. Save points and shared screenshots will stay together inside it.")
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        form = QFormLayout()
        self.location_edit = QLineEdit(str(default_parent) if default_parent else "")
        self.location_edit.setPlaceholderText("Choose an existing parent folder")
        location_row = QHBoxLayout()
        location_row.addWidget(self.location_edit, 1)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse)
        location_row.addWidget(self.browse_button)
        form.addRow("Location", location_row)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example, Retaking Vierville")
        self.name_edit.setMaxLength(120)
        form.addRow("Game / folder name", self.name_edit)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create game")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a location for the new game folder", self.location_edit.text())
        if path:
            self.location_edit.setText(path)

    def selection(self) -> tuple[Path, str]:
        return Path(self.location_edit.text().strip()).expanduser(), self.name_edit.text().strip()

    def accept(self) -> None:
        location, name = self.selection()
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if not self.location_edit.text().strip() or not location.is_dir():
            self.error_label.setText("Choose an existing location for the new game folder.")
        elif not name or name in (".", ".."):
            self.error_label.setText("Enter a name for the game folder.")
        elif any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name) or name.endswith((".", " ")) or name.split(".", 1)[0].upper() in reserved:
            self.error_label.setText("Choose a folder name without reserved names or these characters: < > : \" / \\ | ? *")
        elif (location / name).exists():
            self.error_label.setText("A file or folder with this name already exists. Choose another name.")
        else:
            super().accept()


class SavePointDialog(QDialog):
    """Name a new point or select an existing name; caller confirms overwrite."""

    def __init__(self, points: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save point")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        explanation = QLabel("Enter a new name, or select an existing save point to replace it.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.name_combo = QComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.addItems([_point_name(point) for point in points])
        self.name_combo.setCurrentIndex(-1)
        self.name_combo.lineEdit().setMaxLength(120)
        self.name_combo.lineEdit().setPlaceholderText("Save point name")
        layout.addWidget(self.name_combo)
        self.error_label = QLabel()
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def point_name(self) -> str:
        return self.name_combo.currentText().strip()

    def accept(self) -> None:
        name = self.point_name()
        if not name or any(ord(character) < 32 for character in name):
            self.error_label.setText("Enter a save point name.")
            return
        super().accept()


class OpenGameDialog(QDialog):
    """Select current progress or a named point in a registered/browsed game."""

    def __init__(self, folder_store: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.folder_store = folder_store
        self._folder: Path | None = None
        self.setWindowTitle("Open game")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        self.game_combo = QComboBox()
        row = QHBoxLayout()
        row.addWidget(self.game_combo, 1)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        layout.addLayout(row)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        form = QFormLayout()
        self.point_combo = QComboBox()
        self.point_combo.setToolTip(EXPERIENCE_HINT)
        form.addRow("Continue from", self.point_combo)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.buttons.button(QDialogButtonBox.Open).setToolTip(EXPERIENCE_HINT)
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        try:
            records = self.folder_store.list_known_folders()
            for record in records:
                self.game_combo.addItem(_game_label(record), str(_record_folder(record)))
            if not records:
                self.error_label.setText("Browse to an existing game folder to open it.")
        except _STORAGE_ERRORS as exc:
            self.error_label.setText(str(exc))
        self._update_open_button()

    def _game_changed(self, index: int) -> None:
        path = self.game_combo.itemData(index)
        self._load_folder(Path(path) if path else None)

    def _load_folder(self, folder: Path | None) -> None:
        self._folder = None
        self.point_combo.clear()
        self.error_label.clear()
        self.path_label.setText(str(folder) if folder else "")
        if folder is not None:
            try:
                self.folder_store.describe(folder)
                points = self.folder_store.list_points(folder)
                self.point_combo.addItem("Current progress", None)
                for point in points:
                    self.point_combo.addItem(_point_name(point), _point_id(point))
                self._folder = folder
            except _STORAGE_ERRORS as exc:
                self.error_label.setText(f"This game cannot be opened: {exc}")
        self._update_open_button()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Open a game folder", str(self._folder or ""))
        if not selected:
            return
        index = self.game_combo.findData(str(Path(selected)))
        if index < 0:
            self.game_combo.addItem(Path(selected).name, str(Path(selected)))
            index = self.game_combo.count() - 1
        self.game_combo.setCurrentIndex(index)
        self._load_folder(Path(selected))

    def _update_open_button(self) -> None:
        self.buttons.button(QDialogButtonBox.Open).setEnabled(self._folder is not None)

    def selection(self) -> tuple[Path, str | None]:
        if self._folder is None:
            raise ValueError("Choose an existing game folder first.")
        return self._folder, self.point_combo.currentData()

    def accept(self) -> None:
        if self._folder is not None:
            super().accept()


class StorageManagerDialog(QDialog):
    """Show managed storage and perform confirmed game/point/image deletions."""

    screenshotsCleared = Signal(str)
    gameDeleted = Signal(str)

    def __init__(self, folder_store: Any, active_folder: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.folder_store = folder_store
        self._folder: Path | None = None
        self._active_folder = Path(active_folder).expanduser().resolve() if active_folder is not None else None
        self.setWindowTitle("Games and storage")
        self.setMinimumSize(570, 420)
        layout = QVBoxLayout(self)
        self.game_combo = QComboBox()
        layout.addWidget(self.game_combo)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        self.usage_label = QLabel()
        layout.addWidget(self.usage_label)
        layout.addWidget(QLabel("Named save points"))
        self.point_list = QListWidget()
        layout.addWidget(self.point_list, 1)
        self.delete_point_button = QPushButton("Delete selected save point...")
        self.delete_point_button.clicked.connect(self._delete_point)
        layout.addWidget(self.delete_point_button)
        note = QLabel("Deleting a save point keeps shared originals needed by current progress or review, so it may free little space.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.clear_screenshots_button = QPushButton("Clear all board screenshots...")
        self.clear_screenshots_button.clicked.connect(self._clear_screenshots)
        layout.addWidget(self.clear_screenshots_button)
        self.delete_game_button = QPushButton("Delete game data...")
        self.delete_game_button.clicked.connect(self._delete_game)
        layout.addWidget(self.delete_game_button)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        self.point_list.currentRowChanged.connect(self._update_buttons)
        try:
            for record in self.folder_store.list_known_folders():
                self.game_combo.addItem(_game_label(record), str(_record_folder(record)))
            if active_folder is not None:
                index = self.game_combo.findData(str(active_folder))
                if index >= 0:
                    self.game_combo.setCurrentIndex(index)
            if not self.game_combo.count():
                self.usage_label.setText("No game folders have been registered yet.")
        except _STORAGE_ERRORS as exc:
            self.error_label.setText(str(exc))
        self._update_buttons()

    def _game_changed(self, index: int) -> None:
        value = self.game_combo.itemData(index)
        self._folder = Path(value) if value else None
        self._refresh()

    def _refresh(self) -> None:
        self.point_list.clear()
        self.error_label.clear()
        self.path_label.setText(str(self._folder) if self._folder else "")
        if self._folder is not None:
            try:
                description = self.folder_store.describe(self._folder)
                points = self.folder_store.list_points(self._folder)
                total = int(description.get("total_bytes", description.get("size_bytes", description.get("bytes", 0))))
                self.usage_label.setText(f"Disk space used: {format_size(total)}")
                for point in points:
                    self.point_list.addItem(_point_name(point))
                    item = self.point_list.item(self.point_list.count() - 1)
                    item.setData(Qt.UserRole, _point_id(point))
            except _STORAGE_ERRORS as exc:
                self.usage_label.setText("Storage information unavailable")
                self.error_label.setText(str(exc))
                self._folder = None
        else:
            self.usage_label.setText("No game folder selected.")
        self._update_buttons()

    def _is_active_game(self) -> bool:
        return self._folder is not None and self._active_folder is not None and self._folder.expanduser().resolve() == self._active_folder

    def _update_buttons(self, *_args: Any) -> None:
        self.delete_point_button.setEnabled(self._folder is not None and self.point_list.currentItem() is not None)
        self.clear_screenshots_button.setEnabled(self._folder is not None)
        active = self._is_active_game()
        self.delete_game_button.setEnabled(self._folder is not None and not active)
        self.delete_game_button.setToolTip("Open another game before deleting this game's data." if active else "")

    def _delete_point(self) -> None:
        item = self.point_list.currentItem()
        if self._folder is None or item is None:
            return
        answer = QMessageBox.question(
            self, "Delete save point", f'Delete the save point "{item.text()}"? Shared screenshots, references, current progress, and review history will remain.',
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.folder_store.delete_point(self._folder, item.data(Qt.UserRole))
            self._refresh()
        except _STORAGE_ERRORS as exc:
            QMessageBox.warning(self, "Save point was not deleted", str(exc))

    def _clear_screenshots(self) -> None:
        if self._folder is None:
            return
        answer = QMessageBox.question(
            self, "Clear all board screenshots", CLEAR_SCREENSHOTS_WARNING,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        folder = self._folder
        try:
            self.folder_store.clear_screenshots(folder)
            self._refresh()
            self.screenshotsCleared.emit(str(folder))
        except _STORAGE_ERRORS as exc:
            QMessageBox.warning(self, "Screenshots were not cleared", str(exc))

    def _delete_game(self) -> None:
        if self._folder is None or self._is_active_game():
            return
        answer = QMessageBox.question(
            self, "Delete game data", DELETE_GAME_WARNING,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        folder = self._folder
        try:
            self.folder_store.delete_folder(folder)
        except _STORAGE_ERRORS as exc:
            QMessageBox.warning(self, "Game data was not deleted", str(exc))
            return
        self.game_combo.removeItem(self.game_combo.currentIndex())
        self._game_changed(self.game_combo.currentIndex())
        self.gameDeleted.emit(str(folder))
