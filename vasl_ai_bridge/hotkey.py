"""One Windows capture binding, scoped to the board and companion main window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot
from PySide6.QtGui import QKeySequence

from . import APP_NAME

DEFAULT_HOTKEY = ""
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x20B, 0x20C
_LIVE_HOOK_APIS = set()


@dataclass(frozen=True)
class HotkeyStatus:
    success: bool
    message: str
    shortcut: str = ""


def native_shortcut(sequence: QKeySequence | str) -> tuple[str, int, int]:
    """Translate one binding; negative VK values identify mouse side buttons."""
    if isinstance(sequence, str) and not sequence.strip():
        raise ValueError("Choose one key combination for the screenshot hotkey.")
    aliases = {"mouse4": ("Mouse4", -4), "xbutton1": ("Mouse4", -4),
               "mouse xbutton1": ("Mouse4", -4), "mouse5": ("Mouse5", -5),
               "xbutton2": ("Mouse5", -5), "mouse xbutton2": ("Mouse5", -5)}
    if isinstance(sequence, str) and sequence.strip().lower() in aliases:
        label, virtual_key = aliases[sequence.strip().lower()]
        return label, MOD_NOREPEAT, virtual_key
    key_sequence = QKeySequence(sequence)
    if key_sequence.count() != 1:
        raise ValueError("Choose one key combination for the screenshot hotkey.")
    combination = key_sequence[0]
    key, modifiers = int(combination.key()), combination.keyboardModifiers()
    supported = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier |
                 Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.MetaModifier)
    if modifiers.value & ~supported.value:
        raise ValueError("This modifier is not supported by the Windows screenshot hotkey.")
    native_modifiers = MOD_NOREPEAT
    for qt_modifier, win_modifier in (
        (Qt.KeyboardModifier.ControlModifier, MOD_CONTROL),
        (Qt.KeyboardModifier.AltModifier, MOD_ALT),
        (Qt.KeyboardModifier.ShiftModifier, MOD_SHIFT),
        (Qt.KeyboardModifier.MetaModifier, MOD_WIN),
    ):
        if modifiers & qt_modifier:
            native_modifiers |= win_modifier
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        raise ValueError("Function keys F1-F24 are reserved for the board application. Choose another capture binding.")
    elif int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9) or int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        virtual_key = key
    else:
        virtual_key = {
            int(Qt.Key.Key_Backspace): 0x08, int(Qt.Key.Key_Tab): 0x09,
            int(Qt.Key.Key_Return): 0x0D, int(Qt.Key.Key_Enter): 0x0D,
            int(Qt.Key.Key_Pause): 0x13, int(Qt.Key.Key_Escape): 0x1B,
            int(Qt.Key.Key_Space): 0x20, int(Qt.Key.Key_PageUp): 0x21,
            int(Qt.Key.Key_PageDown): 0x22, int(Qt.Key.Key_End): 0x23,
            int(Qt.Key.Key_Home): 0x24, int(Qt.Key.Key_Left): 0x25,
            int(Qt.Key.Key_Up): 0x26, int(Qt.Key.Key_Right): 0x27,
            int(Qt.Key.Key_Down): 0x28, int(Qt.Key.Key_Print): 0x2C,
            int(Qt.Key.Key_Insert): 0x2D, int(Qt.Key.Key_Delete): 0x2E,
            int(Qt.Key.Key_CapsLock): 0x14, int(Qt.Key.Key_NumLock): 0x90,
            int(Qt.Key.Key_ScrollLock): 0x91, int(Qt.Key.Key_Shift): 0x10,
            int(Qt.Key.Key_Control): 0x11, int(Qt.Key.Key_Alt): 0x12,
            int(Qt.Key.Key_Meta): 0x5B, int(Qt.Key.Key_Menu): 0x5D,
        }.get(key)
        if virtual_key is None and 0x21 <= key <= 0x7E and sys.platform == "win32":
            api = ctypes.WinDLL("user32", use_last_error=True)
            api.VkKeyScanW.argtypes = [wintypes.WCHAR]
            api.VkKeyScanW.restype = wintypes.SHORT
            mapped = api.VkKeyScanW(chr(key))
            if mapped != -1:
                virtual_key = mapped & 0xFF
                layout_modifiers = (mapped >> 8) & 7
                for layout_bit, modifier in ((1, MOD_SHIFT), (2, MOD_CONTROL), (4, MOD_ALT)):
                    if layout_modifiers & layout_bit:
                        native_modifiers |= modifier
        if virtual_key is None:
            raise ValueError("Use one keyboard key or Mouse4 / Mouse5 for the capture binding.")
    return key_sequence.toString(QKeySequence.SequenceFormat.PortableText), native_modifiers, virtual_key


class WindowsHotkeyApi:
    """Historical RegisterHotKey diagnostic API; desktop capture uses scoped hooks."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Global screenshot hotkeys are available on Windows only.")
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.UnregisterHotKey.restype = wintypes.BOOL
        self.last_error = 0

    def register_hotkey(self, identifier: int, modifiers: int, virtual_key: int) -> bool:
        success = bool(self.api.RegisterHotKey(None, identifier, modifiers, virtual_key))
        self.last_error = 0 if success else ctypes.get_last_error()
        return success

    def unregister_hotkey(self, identifier: int) -> bool:
        success = bool(self.api.UnregisterHotKey(None, identifier))
        self.last_error = 0 if success else ctypes.get_last_error()
        return success


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def _normalized_key(key: int) -> int:
    return {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11,
            0xA4: 0x12, 0xA5: 0x12, 0x5C: 0x5B}.get(key, key)


class WindowsInputApi:
    """Keep native callback lifetimes intact until their hooks are released."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Capture bindings are available on Windows only.")
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._procedure_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                                 wintypes.WPARAM, wintypes.LPARAM)
        self.api.SetWindowsHookExW.argtypes = [ctypes.c_int, self._procedure_type,
                                               wintypes.HINSTANCE, wintypes.DWORD]
        self.api.SetWindowsHookExW.restype = wintypes.HANDLE
        self.api.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        self.api.UnhookWindowsHookEx.restype = wintypes.BOOL
        self.api.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        self.api.CallNextHookEx.restype = ctypes.c_ssize_t
        self.api.GetForegroundWindow.argtypes = []
        self.api.GetForegroundWindow.restype = wintypes.HWND
        self.api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.api.GetAsyncKeyState.restype = wintypes.SHORT
        self.kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel.GetModuleHandleW.restype = wintypes.HMODULE
        self._procedures = {}
        self.last_error = 0
        self.last_callback_error = ""

    def foreground_window(self) -> int:
        return int(self.api.GetForegroundWindow() or 0)

    def modifiers(self) -> int:
        value = 0
        for modifier, keys in ((MOD_SHIFT, (0x10,)), (MOD_CONTROL, (0x11,)),
                               (MOD_ALT, (0x12,)), (MOD_WIN, (0x5B, 0x5C))):
            if any(self.api.GetAsyncKeyState(key) & 0x8000 for key in keys):
                value |= modifier
        return value

    def install_hook(self, kind: str, receiver) -> int:
        def procedure(code, message, pointer):
            if code >= 0:
                try:
                    if kind == "keyboard" and message in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
                        key = _KeyboardInput.from_address(pointer).vkCode
                        if receiver(message, key):
                            return 1
                    elif kind == "mouse" and message in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                        button = (_MouseInput.from_address(pointer).mouseData >> 16) & 0xFFFF
                        if button in (1, 2) and receiver(message, -3 - button):
                            return 1
                except Exception as error:
                    # Never let Python errors cross a Windows callback boundary.
                    self.last_callback_error = str(error)
            return self.api.CallNextHookEx(None, code, message, pointer)

        callback = self._procedure_type(procedure)
        handle = self.api.SetWindowsHookExW(13 if kind == "keyboard" else 14,
                                           callback, self.kernel.GetModuleHandleW(None), 0)
        self.last_error = 0 if handle else ctypes.get_last_error()
        if handle:
            self._procedures[int(handle)] = callback
            _LIVE_HOOK_APIS.add(self)
        return int(handle or 0)

    def uninstall_hook(self, handle: int) -> bool:
        success = bool(self.api.UnhookWindowsHookEx(handle))
        self.last_error = 0 if success else ctypes.get_last_error()
        if success:
            self._procedures.pop(handle, None)
            if not self._procedures:
                _LIVE_HOOK_APIS.discard(self)
        return success


class GlobalCaptureHotkey(QObject):
    """Deliver board or main-window capture outside the scoped native hook."""

    activated = Signal()
    application_activated = Signal()
    _capture_requested = Signal(object, int)

    def __init__(self, parent: QObject | None = None, *, native_api=None,
                 application=None, target_window_getter=None,
                 application_window_getter=None) -> None:
        super().__init__(parent)
        self._api = native_api
        self._application = application or QCoreApplication.instance()
        self._target_window_getter = target_window_getter or (lambda: 0)
        self._application_window_getter = application_window_getter or (lambda: 0)
        self._scope_label = f"selected board or {APP_NAME}" if application_window_getter else "selected board only"
        self._binding = None
        self._handles = {}
        self._generation = 0
        self._pending_generation = None
        self._consumed = set()
        self._native_matches = self._delivered = self._scope_dropped = 0
        self.shortcut = ""
        self._capture_requested.connect(self._dispatch_capture, Qt.ConnectionType.QueuedConnection)
        if self._application is not None:
            self._application.aboutToQuit.connect(self.unregister)

    @property
    def is_registered(self) -> bool:
        return self._binding is not None

    @property
    def diagnostics(self) -> dict:
        return {"native_matches": self._native_matches, "delivered": self._delivered,
                "scope_dropped": self._scope_dropped,
                "callback_error": getattr(self._api, "last_callback_error", "")}

    def _target_window(self) -> int:
        return self._window_handle(self._target_window_getter)

    def _application_window(self) -> int:
        return self._window_handle(self._application_window_getter)

    @staticmethod
    def _window_handle(getter) -> int:
        try:
            handle = getter()
            return int(handle) if handle and not isinstance(handle, bool) else 0
        except (TypeError, ValueError, RuntimeError, AttributeError):
            return 0

    def start(self, sequence: QKeySequence | str = DEFAULT_HOTKEY) -> HotkeyStatus:
        return self.replace(sequence)

    def replace(self, sequence: QKeySequence | str) -> HotkeyStatus:
        if sequence == "" or isinstance(sequence, QKeySequence) and sequence.isEmpty():
            return self.unregister()
        try:
            shortcut, modifiers, key = native_shortcut(sequence)
            if self.is_registered and shortcut == self.shortcut:
                return HotkeyStatus(True, f"Capture binding: {shortcut} ({self._scope_label})", shortcut)
            if self._application is None:
                raise OSError("The capture binding requires the desktop application to be running.")
            if self._api is None:
                self._api = WindowsInputApi()
            kind = "mouse" if key < 0 else "keyboard"
            if kind not in self._handles:
                receiver = self._native_mouse if kind == "mouse" else self._native_keyboard
                handle = self._api.install_hook(kind, receiver)
                if not handle:
                    error = getattr(self._api, "last_error", 0)
                    raise OSError(f"Windows could not enable the {kind} capture binding. Windows error {error}.")
                self._handles[kind] = handle
            old_kind = self._binding[0] if self._binding else None
            if old_kind and old_kind != kind:
                if not self._api.uninstall_hook(self._handles[old_kind]):
                    if self._api.uninstall_hook(self._handles[kind]):
                        self._handles.pop(kind)
                    return HotkeyStatus(False, "Windows could not replace the capture binding. The previous binding remains active.", self.shortcut)
                self._handles.pop(old_kind)
            self._generation += 1
            self._pending_generation = None
            self._binding = kind, modifiers & ~MOD_NOREPEAT, key
            self.shortcut = shortcut
            return HotkeyStatus(True, f"Capture binding: {shortcut} ({self._scope_label})", shortcut)
        except (OSError, ValueError, RuntimeError) as error:
            return HotkeyStatus(False, str(error), self.shortcut)

    def _native_keyboard(self, message: int, key: int) -> bool:
        return self._native_input("keyboard", message, _normalized_key(key),
                                  message in (WM_KEYUP, WM_SYSKEYUP), physical_key=key)

    def _native_mouse(self, message: int, key: int) -> bool:
        return self._native_input("mouse", message, key, message == WM_XBUTTONUP)

    def _native_input(self, kind: str, message: int, key: int, released: bool,
                      *, physical_key: int | None = None) -> bool:
        pair = kind, key if physical_key is None else physical_key
        if released:
            consumed = pair in self._consumed
            self._consumed.discard(pair)
            return consumed
        if pair in self._consumed:
            return True  # Suppress repeat without queueing another capture.
        if not self._binding or self._binding[0] != kind or self._binding[2] != key:
            return False
        target = self._target_window()
        foreground = self._api.foreground_window()
        if not target or not foreground or foreground not in (target, self._application_window()):
            return False
        if kind == "keyboard":
            modifiers = self._api.modifiers()
            # A modifier used as the key must not count as a chord modifier itself.
            modifiers &= ~{0x10: MOD_SHIFT, 0x11: MOD_CONTROL, 0x12: MOD_ALT, 0x5B: MOD_WIN}.get(key, 0)
            if modifiers != self._binding[1]:
                return False
        # Side buttons remain usable while a VASL LOS gesture holds a modifier.
        self._consumed.add(pair)
        self._native_matches += 1
        if self._pending_generation is None:
            self._pending_generation = self._generation
            # Keep the selected source and the route fixed until queued delivery.
            self._capture_requested.emit((target, foreground), self._generation)
        return True

    @Slot(object, int)
    def _dispatch_capture(self, context: tuple[int, int], generation: int) -> None:
        if generation != self._generation or not self.is_registered:
            return
        self._pending_generation = None
        target, foreground = context
        if (self._target_window() != target or self._api.foreground_window() != foreground
                or foreground not in (target, self._application_window())):
            self._scope_dropped += 1
            return
        self._delivered += 1
        if foreground == target:
            # Freeze visible board pixels before activating the companion.
            self.activated.emit()
        else:
            # Use exactly the button's selected-window capture path.
            self.application_activated.emit()

    def unregister(self) -> HotkeyStatus:
        self._generation += 1
        self._pending_generation = None
        self._binding = None
        self.shortcut = ""
        self._consumed.clear()
        failed = False
        if self._api is not None:
            for kind, handle in tuple(self._handles.items()):
                try:
                    success = self._api.uninstall_hook(handle)
                except (OSError, RuntimeError):
                    success = False
                if success:
                    self._handles.pop(kind)
                else:
                    failed = True
        if failed:
            return HotkeyStatus(False, "Capture is disabled, but Windows could not release its input hook.", self.shortcut)
        return HotkeyStatus(True, "Capture binding disabled.")
