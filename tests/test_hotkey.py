"""Scoped native binding tests without installing hooks or sending input."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes
import sys
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from vasl_ai_bridge.hotkey import (
    DEFAULT_HOTKEY, GlobalCaptureHotkey, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT,
    MOD_SHIFT, WM_KEYDOWN, WM_KEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
    WindowsInputApi, native_shortcut,
)

APP = QApplication.instance() or QApplication(["Scoped capture binding tests"])


class FakeNativeApi:
    def __init__(self):
        self.handles = {}
        self.calls = []
        self.blocked_kind = None
        self.failed_release_kind = None
        self.last_error = 5
        self.foreground = 123
        self.modifier_state = 0
        self.next_handle = 1

    def install_hook(self, kind, receiver):
        self.calls.append(("install", kind))
        if kind == self.blocked_kind:
            return 0
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = (kind, receiver)
        return handle

    def uninstall_hook(self, handle):
        kind = self.handles[handle][0]
        self.calls.append(("uninstall", kind))
        if kind == self.failed_release_kind:
            return False
        self.handles.pop(handle)
        return True

    def foreground_window(self):
        return self.foreground

    def modifiers(self):
        return self.modifier_state

    def fire(self, kind, message, key):
        receiver = next(receiver for current, receiver in self.handles.values() if current == kind)
        return receiver(message, key)


class GlobalCaptureHotkeyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeNativeApi()
        self.target = 123
        self.application_target = 789
        self.hotkey = GlobalCaptureHotkey(native_api=self.api, application=APP,
                                          target_window_getter=lambda: self.target,
                                          application_window_getter=lambda: self.application_target)
        self.events = []
        self.hotkey.activated.connect(lambda: self.events.append("capture"))
        self.hotkey.application_activated.connect(lambda: self.events.append("application capture"))
        self.addCleanup(self.hotkey.unregister)

    def press(self, key, modifiers=0):
        self.api.modifier_state = modifiers
        return self.api.fire("keyboard", WM_KEYDOWN, key)

    def release(self, key):
        return self.api.fire("keyboard", WM_KEYUP, key)

    def test_target_capture_is_queued_after_hook_and_before_caller_ui_work(self):
        self.assertTrue(self.hotkey.start("Tab").success)
        self.assertTrue(self.press(9))
        self.assertEqual(self.events, [])
        self.events.append("hook returned")
        APP.processEvents()
        self.assertEqual(self.events, ["hook returned", "capture"])
        self.assertEqual(self.hotkey.diagnostics["delivered"], 1)

    def test_single_keys_and_chords_pass_through_outside_target(self):
        for label, key, modifiers in (("Tab", 9, 0), ("CapsLock", 20, 0),
                ("Shift", 0xA0, MOD_SHIFT), ("Shift+A", 65, MOD_SHIFT),
                ("Alt+S", 83, MOD_ALT), ("Home", 0x24, 0), ("A", 65, 0)):
            with self.subTest(label=label):
                self.assertTrue(self.hotkey.replace(label).success)
                self.api.foreground = 456
                self.assertFalse(self.press(key, modifiers))
                self.assertFalse(self.release(key))
                APP.processEvents()
                self.assertEqual(self.events, [])

    def test_application_capture_is_queued_and_uses_only_the_application_route(self):
        self.api.foreground = self.application_target
        self.assertTrue(self.hotkey.start("Tab").success)
        self.assertTrue(self.press(9))
        self.assertEqual(self.events, [])
        self.events.append("hook returned")
        APP.processEvents()
        self.assertEqual(self.events, ["hook returned", "application capture"])
        self.assertEqual(self.hotkey.diagnostics["delivered"], 1)

    def test_single_keys_chords_and_repeats_keep_application_capture_scoped(self):
        self.api.foreground = self.application_target
        for label, key, modifiers in (("Tab", 9, 0), ("CapsLock", 20, 0),
                ("Shift", 0xA0, MOD_SHIFT), ("Shift+A", 65, MOD_SHIFT),
                ("Alt+S", 83, MOD_ALT), ("Home", 0x24, 0), ("A", 65, 0)):
            with self.subTest(label=label):
                self.assertTrue(self.hotkey.replace(label).success)
                before = len(self.events)
                self.assertTrue(self.press(key, modifiers))
                self.assertTrue(self.press(key, modifiers))
                APP.processEvents()
                self.assertTrue(self.press(key, modifiers))
                self.assertTrue(self.release(key))
                self.assertFalse(self.release(key))
                APP.processEvents()
                self.assertEqual(self.events[before:], ["application capture"])

    def test_application_mouse_side_buttons_accept_held_modifiers_once(self):
        self.api.foreground = self.application_target
        self.api.modifier_state = MOD_CONTROL | MOD_SHIFT
        for label, key in (("Mouse4", -4), ("Mouse5", -5)):
            with self.subTest(label=label):
                self.assertTrue(self.hotkey.replace(label).success)
                self.assertFalse(self.api.fire("mouse", WM_XBUTTONDOWN, -5 if key == -4 else -4))
                self.assertTrue(self.api.fire("mouse", WM_XBUTTONDOWN, key))
                self.assertTrue(self.api.fire("mouse", WM_XBUTTONDOWN, key))
                self.assertTrue(self.api.fire("mouse", WM_XBUTTONUP, key))
                APP.processEvents()
        self.assertEqual(self.events, ["application capture", "application capture"])

    def test_application_source_focus_or_scope_change_cancels_queued_capture(self):
        for change in ("source", "board focus", "outside focus", "application disabled"):
            with self.subTest(change=change):
                self.target, self.application_target = 123, 789
                self.api.foreground = self.application_target
                self.hotkey.start("Tab")
                self.assertTrue(self.press(9))
                self.release(9)
                if change == "source":
                    self.target = 456
                elif change == "board focus":
                    self.api.foreground = self.target
                elif change == "outside focus":
                    self.api.foreground = 456
                else:
                    self.application_target = 0
                APP.processEvents()
                self.assertEqual(self.events, [])
        self.assertEqual(self.hotkey.diagnostics["scope_dropped"], 4)

    def test_board_to_application_focus_change_does_not_switch_queued_route(self):
        self.hotkey.start("Tab")
        self.assertTrue(self.press(9))
        self.release(9)
        self.api.foreground = self.application_target
        APP.processEvents()
        self.assertEqual(self.events, [])
        self.assertEqual(self.hotkey.diagnostics["scope_dropped"], 1)

    def test_application_without_selected_source_never_consumes_input(self):
        self.api.foreground = self.application_target
        self.hotkey.start("Tab")
        for target in (0, None, False):
            self.target = target
            self.assertFalse(self.press(9))
            self.assertFalse(self.release(9))
        APP.processEvents()
        self.assertEqual(self.events, [])

    def test_exact_modifiers_are_required_in_target(self):
        self.hotkey.start("Shift+A")
        self.assertFalse(self.press(65, 0))
        self.assertFalse(self.press(65, MOD_SHIFT | MOD_CONTROL))
        self.assertTrue(self.press(65, MOD_SHIFT))
        APP.processEvents()
        self.assertEqual(self.events, ["capture"])

    def test_single_modifier_accepts_left_or_right_physical_key(self):
        self.hotkey.start("Shift")
        self.assertTrue(self.press(0xA0, MOD_SHIFT))
        self.assertTrue(self.release(0xA0))
        APP.processEvents()
        self.assertTrue(self.press(0xA1, MOD_SHIFT))
        self.assertTrue(self.release(0xA1))
        APP.processEvents()
        self.assertEqual(self.events, ["capture", "capture"])

    def test_left_and_right_modifier_releases_are_tracked_independently(self):
        self.hotkey.start("Shift")
        self.assertTrue(self.press(0xA0, MOD_SHIFT))
        APP.processEvents()
        self.assertTrue(self.press(0xA1, MOD_SHIFT))
        APP.processEvents()
        self.assertTrue(self.release(0xA0))
        self.assertTrue(self.release(0xA1))
        self.assertEqual(self.events, ["capture", "capture"])

    def test_repeat_and_matching_release_do_not_repeat_capture(self):
        self.hotkey.start("Home")
        self.assertTrue(self.press(0x24))
        self.assertTrue(self.press(0x24))
        APP.processEvents()
        self.assertTrue(self.press(0x24))
        self.assertTrue(self.release(0x24))
        self.assertFalse(self.release(0x24))
        APP.processEvents()
        self.assertEqual(self.events, ["capture"])
        self.assertTrue(self.press(0x24))
        APP.processEvents()
        self.assertEqual(self.events, ["capture", "capture"])

    def test_mouse_side_buttons_are_scoped_and_other_button_passes(self):
        for label, key in (("Mouse4", -4), ("Mouse5", -5)):
            with self.subTest(label=label):
                self.assertTrue(self.hotkey.replace(label).success)
                self.api.foreground = 456
                self.assertFalse(self.api.fire("mouse", WM_XBUTTONDOWN, key))
                self.api.foreground = self.target
                self.api.modifier_state = MOD_CONTROL | MOD_SHIFT
                self.assertFalse(self.api.fire("mouse", WM_XBUTTONDOWN, -5 if key == -4 else -4))
                self.assertTrue(self.api.fire("mouse", WM_XBUTTONDOWN, key))
                self.assertTrue(self.api.fire("mouse", WM_XBUTTONUP, key))
                APP.processEvents()
        self.assertEqual(self.events, ["capture", "capture"])

    def test_focus_or_selected_source_change_cancels_queued_capture(self):
        for change in ("focus", "selection"):
            with self.subTest(change=change):
                self.target = self.api.foreground = 123
                self.hotkey.start("Tab")
                self.assertTrue(self.press(9))
                self.release(9)
                if change == "focus":
                    self.api.foreground = 456
                else:
                    self.target = 456
                APP.processEvents()
                self.assertEqual(self.events, [])
        self.assertEqual(self.hotkey.diagnostics["scope_dropped"], 2)

    def test_replacement_cancels_old_capture_and_reuses_keyboard_hook(self):
        self.hotkey.start("Tab")
        self.press(9)
        self.release(9)
        self.api.calls.clear()
        self.assertTrue(self.hotkey.replace("Home").success)
        APP.processEvents()
        self.assertEqual(self.events, [])
        self.assertEqual(self.api.calls, [])

    def test_failed_mouse_replacement_preserves_keyboard_binding(self):
        self.hotkey.start("Tab")
        original = dict(self.api.handles)
        self.api.blocked_kind = "mouse"
        status = self.hotkey.replace("Mouse4")
        self.assertFalse(status.success)
        self.assertIn("Windows error 5", status.message)
        self.assertEqual(self.hotkey.shortcut, "Tab")
        self.assertEqual(self.api.handles, original)
        self.assertTrue(self.press(9))
        APP.processEvents()
        self.assertEqual(self.events, ["capture"])

    def test_kind_replacement_installs_before_releasing_previous_hook(self):
        self.hotkey.start("Tab")
        self.api.calls.clear()
        self.assertTrue(self.hotkey.replace("Mouse4").success)
        self.assertEqual(self.api.calls, [("install", "mouse"), ("uninstall", "keyboard")])
        self.assertEqual(len(self.api.handles), 1)

    def test_failed_previous_release_rolls_back_replacement(self):
        self.hotkey.start("Tab")
        self.api.failed_release_kind = "keyboard"
        self.assertFalse(self.hotkey.replace("Mouse4").success)
        self.assertEqual(self.hotkey.shortcut, "Tab")
        self.assertEqual([kind for kind, _ in self.api.handles.values()], ["keyboard"])
        self.api.failed_release_kind = None

    def test_unregister_cancels_pending_capture_and_is_idempotent(self):
        self.hotkey.start("Tab")
        self.press(9)
        self.assertTrue(self.hotkey.unregister().success)
        APP.processEvents()
        self.assertEqual(self.events, [])
        self.assertFalse(self.hotkey.is_registered)
        self.assertEqual(self.api.handles, {})
        self.assertTrue(self.hotkey.unregister().success)

    def test_missing_or_disabled_target_never_consumes_a_key(self):
        self.hotkey.start("Tab")
        for target in (0, None, False):
            self.target = target
            self.assertFalse(self.press(9))
        APP.processEvents()
        self.assertEqual(self.events, [])

    def test_failed_cleanup_disables_capture_and_can_be_retried(self):
        self.hotkey.start("Tab")
        self.assertTrue(self.press(9))
        self.api.failed_release_kind = "keyboard"
        status = self.hotkey.replace("")
        self.assertFalse(status.success)
        self.assertEqual(status.shortcut, "")
        self.assertEqual(self.hotkey.shortcut, "")
        self.assertFalse(self.hotkey.is_registered)
        self.assertFalse(self.release(9))
        self.assertFalse(self.press(9))
        APP.processEvents()
        self.assertEqual(self.events, [])
        self.api.failed_release_kind = None
        self.assertTrue(self.hotkey.unregister().success)
        self.assertEqual(self.api.handles, {})

    def test_pointer_above_signed_32_bit_survives_queued_delivery(self):
        self.target = self.api.foreground = 0x123456789
        self.hotkey.start("Tab")
        self.assertTrue(self.press(9))
        APP.processEvents()
        self.assertEqual(self.events, ["capture"])

    def test_invalid_whitespace_multi_key_and_reserved_bindings_do_not_install(self):
        for label in (" ", "Ctrl+K, Ctrl+S", "F12", "Ctrl+F12", "Mouse1"):
            with self.subTest(label=label):
                self.assertFalse(self.hotkey.start(label).success)
        self.assertEqual(self.api.calls, [])

    def test_default_disabled_does_not_construct_api_or_install_hook(self):
        self.assertEqual(DEFAULT_HOTKEY, "")
        hotkey = GlobalCaptureHotkey(application=APP)
        with patch("vasl_ai_bridge.hotkey.WindowsInputApi") as constructor:
            self.assertTrue(hotkey.start().success)
            self.assertFalse(hotkey.is_registered)
            self.assertEqual(hotkey.shortcut, "")
            constructor.assert_not_called()
        self.assertTrue(self.hotkey.start().success)
        self.assertEqual(self.api.calls, [])

    def test_disabling_removes_hook_and_cancels_queued_capture_then_can_enable(self):
        self.hotkey.start("Tab")
        self.assertTrue(self.press(9))
        status = self.hotkey.replace("")
        self.assertTrue(status.success)
        self.assertEqual(status.shortcut, "")
        self.assertFalse(self.hotkey.is_registered)
        self.assertEqual(self.api.handles, {})
        APP.processEvents()
        self.assertEqual(self.events, [])
        self.assertTrue(self.hotkey.replace("Tab").success)
        self.assertTrue(self.press(9))
        self.assertTrue(self.release(9))
        APP.processEvents()
        self.assertEqual(self.events, ["capture"])

    def test_all_function_keys_and_modified_chords_are_rejected_without_hooks(self):
        for key in range(1, 25):
            for prefix in ("", "Ctrl+", "Ctrl+Alt+Shift+", "Meta+"):
                label = f"{prefix}F{key}"
                with self.subTest(label=label):
                    with self.assertRaisesRegex(ValueError, "F1-F24"):
                        native_shortcut(label)
                    self.assertFalse(self.hotkey.start(label).success)
        self.assertEqual(self.api.calls, [])

    def test_parser_requires_a_keyboard_key_and_supports_canonical_mouse_aliases(self):
        with self.assertRaises(ValueError):
            native_shortcut("")
        self.assertEqual(native_shortcut("Tab"), ("Tab", MOD_NOREPEAT, 9))
        self.assertEqual(native_shortcut("XButton1"), ("Mouse4", MOD_NOREPEAT, -4))
        self.assertEqual(native_shortcut("Mouse XButton2"), ("Mouse5", MOD_NOREPEAT, -5))


@unittest.skipUnless(sys.platform == "win32", "Actual Windows native signatures")
class WindowsApiSignatureTests(unittest.TestCase):
    def test_pointer_sized_native_signatures(self):
        api = WindowsInputApi()
        self.assertIs(api.api.CallNextHookEx.restype, ctypes.c_ssize_t)
        self.assertEqual(ctypes.sizeof(api.api.SetWindowsHookExW.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(api.api.GetAsyncKeyState.argtypes, [ctypes.c_int])
        self.assertIs(api.api.GetAsyncKeyState.restype, ctypes.c_short)


if __name__ == "__main__":
    unittest.main()
