"""Foreground-return checks without activating any real window."""

import ctypes
from ctypes import wintypes
import os
import unittest
from unittest.mock import Mock, call, patch

from vasl_ai_bridge.window_activation import (
    SW_MINIMIZE, SW_RESTORE, SW_SHOW, SW_SHOWMAXIMIZED,
    _user32, bring_window_to_front,
)


def own_window_api(handle):
    api = Mock()
    api.IsWindow.return_value = True
    api.IsIconic.return_value = False
    api.IsZoomed.return_value = False
    api.SetForegroundWindow.return_value = True
    api.GetForegroundWindow.return_value = handle

    def window_process_id(_handle, pointer):
        pointer._obj.value = os.getpid()
        return 17

    api.GetWindowThreadProcessId.side_effect = window_process_id
    return api


class WindowActivationTests(unittest.TestCase):
    def setUp(self):
        # A high-bit handle also catches accidental 32-bit handle truncation.
        self.handle = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)) + 123
        self.api = own_window_api(self.handle)
        self.platform = patch("vasl_ai_bridge.window_activation.sys.platform", "win32")
        self.native = patch("vasl_ai_bridge.window_activation._user32", return_value=self.api)
        self.platform.start()
        self.load_api = self.native.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.native.stop)

    def test_activates_only_supplied_own_handle_and_verifies_foreground(self):
        self.assertTrue(bring_window_to_front(self.handle))
        self.api.IsWindow.assert_called_once_with(self.handle)
        self.api.GetWindowThreadProcessId.assert_called_once()
        supplied_handle, pointer = self.api.GetWindowThreadProcessId.call_args.args
        self.assertEqual(supplied_handle, self.handle)
        self.assertIsInstance(pointer._obj, wintypes.DWORD)
        self.api.SetForegroundWindow.assert_called_once_with(self.handle)
        self.api.GetForegroundWindow.assert_called_once_with()
        self.api.ShowWindow.assert_called_once_with(self.handle, SW_SHOW)

    def test_non_minimized_window_uses_show_without_restoring_size(self):
        self.api.ShowWindow.return_value = False
        self.assertTrue(bring_window_to_front(self.handle))
        self.api.ShowWindow.assert_called_once_with(self.handle, SW_SHOW)
        names = [call[0] for call in self.api.mock_calls]
        self.assertLess(names.index("ShowWindow"), names.index("SetForegroundWindow"))

    def test_restores_minimized_window_and_checks_actual_foreground(self):
        self.api.IsIconic.return_value = True
        self.api.ShowWindow.return_value = False
        self.assertTrue(bring_window_to_front(self.handle))
        self.api.ShowWindow.assert_called_once_with(self.handle, SW_RESTORE)
        names = [call[0] for call in self.api.mock_calls]
        self.assertLess(names.index("ShowWindow"), names.index("SetForegroundWindow"))

    def test_denied_foreground_returns_false_even_after_native_success(self):
        self.api.GetForegroundWindow.return_value = self.handle + 1
        self.assertFalse(bring_window_to_front(self.handle))
        self.assertEqual(self.api.SetForegroundWindow.call_args_list,
                         [call(self.handle), call(self.handle)])
        self.assertEqual(self.api.ShowWindow.call_args_list, [
            call(self.handle, SW_SHOW), call(self.handle, SW_MINIMIZE),
            call(self.handle, SW_RESTORE),
        ])
        self.assertEqual(self.api.GetForegroundWindow.call_count, 2)

    def test_restore_cycle_only_runs_after_denied_normal_activation(self):
        self.api.GetForegroundWindow.side_effect = [self.handle + 1, self.handle]
        self.assertTrue(bring_window_to_front(self.handle))
        self.assertEqual(self.api.ShowWindow.call_args_list, [
            call(self.handle, SW_SHOW), call(self.handle, SW_MINIMIZE),
            call(self.handle, SW_RESTORE),
        ])
        self.assertEqual(self.api.SetForegroundWindow.call_count, 2)

    def test_restore_cycle_preserves_maximized_state(self):
        self.api.IsZoomed.return_value = True
        self.api.GetForegroundWindow.side_effect = [self.handle + 1, self.handle]
        self.assertTrue(bring_window_to_front(self.handle))
        self.assertEqual(self.api.ShowWindow.call_args_list, [
            call(self.handle, SW_SHOW), call(self.handle, SW_MINIMIZE),
            call(self.handle, SW_SHOWMAXIMIZED),
        ])
        self.api.IsZoomed.assert_called_once_with(self.handle)

    def test_initially_minimized_denial_does_not_repeat_restore_cycle(self):
        self.api.IsIconic.return_value = True
        self.api.GetForegroundWindow.return_value = self.handle + 1
        self.assertFalse(bring_window_to_front(self.handle))
        self.api.ShowWindow.assert_called_once_with(self.handle, SW_RESTORE)
        self.api.SetForegroundWindow.assert_called_once_with(self.handle)
        self.api.GetForegroundWindow.assert_called_once_with()

    def test_native_return_value_does_not_replace_actual_foreground_check(self):
        self.api.SetForegroundWindow.return_value = False
        self.assertTrue(bring_window_to_front(self.handle))
        self.api.GetForegroundWindow.return_value = None
        self.assertFalse(bring_window_to_front(self.handle))

    def test_invalid_handles_do_not_load_native_api(self):
        for handle in (None, 0, -1, True, "123", 123.0,
                       1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
            with self.subTest(handle=handle):
                self.assertFalse(bring_window_to_front(handle))
        self.load_api.assert_not_called()

    def test_closed_handle_is_not_activated(self):
        self.api.IsWindow.return_value = False
        self.assertFalse(bring_window_to_front(self.handle))
        self.api.GetWindowThreadProcessId.assert_not_called()
        self.api.ShowWindow.assert_not_called()
        self.api.SetForegroundWindow.assert_not_called()
        self.api.IsZoomed.assert_not_called()

    def test_foreign_process_handle_is_not_restored_or_activated(self):
        def foreign_process_id(_handle, pointer):
            pointer._obj.value = os.getpid() + 1
            return 18

        self.api.GetWindowThreadProcessId.side_effect = foreign_process_id
        self.assertFalse(bring_window_to_front(self.handle))
        self.api.IsIconic.assert_not_called()
        self.api.ShowWindow.assert_not_called()
        self.api.SetForegroundWindow.assert_not_called()

    def test_failed_ownership_lookup_is_not_activated(self):
        self.api.GetWindowThreadProcessId.side_effect = None
        self.api.GetWindowThreadProcessId.return_value = 0
        self.assertFalse(bring_window_to_front(self.handle))
        self.api.SetForegroundWindow.assert_not_called()

    def test_non_windows_platform_does_not_load_native_api(self):
        with patch("vasl_ai_bridge.window_activation.sys.platform", "linux"):
            self.assertFalse(bring_window_to_front(self.handle))
        self.load_api.assert_not_called()

    def test_native_failures_return_false(self):
        with patch("vasl_ai_bridge.window_activation._user32", side_effect=OSError("unavailable")):
            self.assertFalse(bring_window_to_front(self.handle))
        for method in ("IsWindow", "GetWindowThreadProcessId", "IsIconic", "IsZoomed",
                       "ShowWindow", "SetForegroundWindow", "GetForegroundWindow"):
            with self.subTest(method=method):
                api = own_window_api(self.handle)
                api.IsIconic.return_value = True
                getattr(api, method).side_effect = OSError("native call failed")
                with patch("vasl_ai_bridge.window_activation._user32", return_value=api):
                    self.assertFalse(bring_window_to_front(self.handle))


class WindowsApiSignatureTests(unittest.TestCase):
    def test_native_signatures_preserve_hwnd_and_process_id_pointer(self):
        api = Mock()
        _user32.cache_clear()
        self.addCleanup(_user32.cache_clear)
        with patch("vasl_ai_bridge.window_activation.ctypes.WinDLL", return_value=api,
                   create=True) as loader:
            self.assertIs(_user32(), api)
            loader.assert_called_once_with("user32", use_last_error=True)
        for name in ("IsWindow", "IsIconic", "IsZoomed", "SetForegroundWindow"):
            self.assertEqual(getattr(api, name).argtypes, [wintypes.HWND])
            self.assertIs(getattr(api, name).restype, wintypes.BOOL)
        self.assertEqual(api.ShowWindow.argtypes, [wintypes.HWND, ctypes.c_int])
        self.assertIs(api.ShowWindow.restype, wintypes.BOOL)
        self.assertEqual(api.GetWindowThreadProcessId.argtypes,
                         [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)])
        self.assertIs(api.GetWindowThreadProcessId.restype, wintypes.DWORD)
        self.assertEqual(api.GetForegroundWindow.argtypes, [])
        self.assertIs(api.GetForegroundWindow.restype, wintypes.HWND)


if __name__ == "__main__":
    unittest.main()
