"""Selected-window capture checks without reading any real screen content."""

from dataclasses import FrozenInstanceError
import ctypes
from datetime import datetime
from io import BytesIO
import os
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from vasl_ai_bridge.capture import (
    CaptureError, WindowSource, capture_visible_window, capture_window, list_windows, prepare_image
)


def detailed_image(size=(800, 400)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill="black")
    draw.text((30, 30), "Hex K9 / LOS", fill="red")
    return image


def available_api():
    api = Mock()
    api.IsWindow.return_value = True
    api.IsIconic.return_value = False
    api.IsWindowVisible.return_value = True

    def client_rect(_handle, rectangle):
        rectangle._obj.left = 0
        rectangle._obj.top = 0
        rectangle._obj.right = 800
        rectangle._obj.bottom = 400
        return True

    def process_id(_handle, pointer):
        pointer._obj.value = os.getpid() + 1
        return 1

    api.GetClientRect.side_effect = client_rect
    api.GetWindowThreadProcessId.side_effect = process_id
    return api


class PrepareImageTests(unittest.TestCase):
    def test_bounds_aspect_ratio_rgb_and_png_dimensions(self):
        observation = prepare_image(detailed_image((4000, 2000)), "Synthetic board")
        self.assertEqual((observation.width, observation.height), (2000, 1000))
        with Image.open(BytesIO(observation.png)) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertEqual(decoded.mode, "RGB")
            self.assertEqual(decoded.size, (2000, 1000))
        self.assertIsNotNone(datetime.fromisoformat(observation.captured_at).utcoffset())

    def test_small_image_is_lossless_and_not_enlarged(self):
        original = detailed_image((100, 50))
        observation = prepare_image(original, "Synthetic board")
        with Image.open(BytesIO(observation.png)) as decoded:
            self.assertEqual(decoded.size, original.size)
            self.assertEqual(decoded.tobytes(), original.tobytes())

    def test_portrait_aspect_ratio_and_thin_image(self):
        portrait = prepare_image(detailed_image((1000, 3000)), "Portrait", 600)
        self.assertEqual((portrait.width, portrait.height), (200, 600))
        thin = prepare_image(Image.new("RGB", (4000, 1)), "Thin", 2000)
        self.assertEqual((thin.width, thin.height), (2000, 1))

    def test_frozen_png_is_independent_of_original_image(self):
        original = detailed_image((100, 50))
        observation = prepare_image(original, "Synthetic board")
        before = observation.png
        original.paste("green", (0, 0, 100, 50))
        self.assertEqual(observation.png, before)
        with self.assertRaises(FrozenInstanceError):
            observation.width = 9

    def test_alpha_is_composited_and_metadata_removed(self):
        original = Image.new("RGBA", (40, 20), (255, 0, 0, 0))
        original.info["private_note"] = "must not travel with image"
        observation = prepare_image(original, "Transparent source")
        with Image.open(BytesIO(observation.png)) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255))
            self.assertNotIn("private_note", decoded.info)

    def test_invalid_limits_empty_image_and_missing_source(self):
        for limit in (0, -1, 8193, True, 12.5, "2000"):
            with self.subTest(limit=limit), self.assertRaises(CaptureError):
                prepare_image(detailed_image(), "Synthetic", limit)
        for image in (None, Image.new("RGB", (0, 1))):
            with self.subTest(image=image), self.assertRaises(CaptureError):
                prepare_image(image, "Synthetic")
        with self.assertRaises(CaptureError):
            prepare_image(detailed_image(), " ")


class CaptureWindowTests(unittest.TestCase):
    def setUp(self):
        self.api = available_api()
        self.source = WindowSource(123, "Old title")
        self.native = patch("vasl_ai_bridge.capture._user32", return_value=self.api)
        self.grabber = patch("vasl_ai_bridge.capture.ImageGrab.grab", return_value=detailed_image())
        self.title = patch("vasl_ai_bridge.capture._window_title", return_value="Current board title")
        self.native.start()
        self.grab = self.grabber.start()
        self.title.start()
        self.addCleanup(self.native.stop)
        self.addCleanup(self.grabber.stop)
        self.addCleanup(self.title.stop)

    def test_captures_only_selected_window_and_records_latest_title(self):
        observation = capture_window(self.source)
        self.grab.assert_called_once_with(window=123)
        self.assertEqual(observation.source, "Current board title")
        self.assertEqual((observation.width, observation.height), (800, 400))
        self.assertIsNotNone(datetime.fromisoformat(observation.captured_at).utcoffset())

    def test_rejects_closed_minimized_and_hidden_sources_before_grab(self):
        for attribute, value, message in (
            ("IsWindow", False, "closed"),
            ("IsIconic", True, "minimized"),
            ("IsWindowVisible", False, "not visible"),
        ):
            with self.subTest(attribute=attribute):
                api = available_api()
                getattr(api, attribute).return_value = value
                with patch("vasl_ai_bridge.capture._user32", return_value=api):
                    with self.assertRaisesRegex(CaptureError, message):
                        capture_window(self.source)
        self.grab.assert_not_called()

    def test_closed_during_capture_rejects_new_evidence(self):
        self.api.IsWindow.side_effect = (True, False)
        with self.assertRaisesRegex(CaptureError, "closed"):
            capture_window(self.source)
        self.grab.assert_called_once_with(window=123)

    def test_zero_handle_invalid_limit_and_own_process_do_not_grab(self):
        for handle in (0, -1, True, 2**80):
            with self.subTest(handle=handle), self.assertRaises(CaptureError):
                capture_window(WindowSource(handle, "Invalid"))
        with self.assertRaises(CaptureError):
            capture_window(self.source, 0)

        def own_process(_handle, pointer):
            pointer._obj.value = os.getpid()
            return 1

        self.api.GetWindowThreadProcessId.side_effect = own_process
        with self.assertRaisesRegex(CaptureError, "another application"):
            capture_window(self.source)
        self.grab.assert_not_called()

    def test_failure_does_not_capture_the_desktop_as_fallback(self):
        self.grab.side_effect = OSError("native failure")
        with self.assertRaisesRegex(CaptureError, "could not be captured"):
            capture_window(self.source)
        self.grab.assert_called_once_with(window=123)

    def test_unsupported_pillow_empty_dark_and_uniform_captures_fail(self):
        self.grab.side_effect = TypeError("unexpected keyword argument window")
        with self.assertRaisesRegex(CaptureError, "does not support"):
            capture_window(self.source)
        self.grab.side_effect = None
        for image, message in (
            (None, "empty"),
            (Image.new("RGB", (20, 20), (4, 4, 4)), "too dark"),
            (Image.new("RGB", (20, 20), "white"), "no visible detail"),
        ):
            with self.subTest(message=message):
                self.grab.return_value = image
                with self.assertRaisesRegex(CaptureError, message):
                    capture_window(self.source)

    def test_missing_or_empty_client_area_fails_before_grab(self):
        self.api.GetClientRect.side_effect = None
        self.api.GetClientRect.return_value = False
        with self.assertRaisesRegex(CaptureError, "unavailable"):
            capture_window(self.source)
        self.api.GetClientRect.return_value = True
        with self.assertRaisesRegex(CaptureError, "empty image area"):
            capture_window(self.source)
        self.grab.assert_not_called()


class VisibleWindowCaptureTests(unittest.TestCase):
    def setUp(self):
        self.api = available_api()
        self.source = WindowSource(123, "Synthetic VASL")
        self.api.GetForegroundWindow.return_value = 123
        self.api.GetSystemMetrics.side_effect = {76: -1000, 77: 0, 78: 2920, 79: 1080}.get

        def position(_handle, pointer):
            pointer._obj.x -= 900
            pointer._obj.y += 100
            return True

        self.api.ClientToScreen.side_effect = position
        self.native = patch("vasl_ai_bridge.capture._user32", return_value=self.api)
        self.grabber = patch("vasl_ai_bridge.capture._grab_visible_region", return_value=detailed_image())
        self.title = patch("vasl_ai_bridge.capture._window_title", return_value="Synthetic VASL")
        self.native.start()
        self.grab = self.grabber.start()
        self.title.start()
        self.addCleanup(self.native.stop)
        self.addCleanup(self.grabber.stop)
        self.addCleanup(self.title.stop)

    def test_grabs_only_selected_visible_client_pixels_without_activating_window(self):
        observation = capture_visible_window(self.source)
        self.grab.assert_called_once_with(self.api, (-900, 100, -100, 500))
        self.api.SetForegroundWindow.assert_not_called()
        self.api.SetFocus.assert_not_called()
        self.assertEqual((observation.width, observation.height), (800, 400))

    def test_background_or_offscreen_source_never_grabs(self):
        self.api.GetForegroundWindow.return_value = 456
        with self.assertRaisesRegex(CaptureError, "foreground"):
            capture_visible_window(self.source)
        self.api.GetForegroundWindow.return_value = 123
        self.api.GetSystemMetrics.side_effect = {76: 0, 77: 0, 78: 1920, 79: 1080}.get
        with self.assertRaisesRegex(CaptureError, "entirely on screen"):
            capture_visible_window(self.source)
        self.grab.assert_not_called()

    def test_focus_change_and_grab_failure_do_not_fall_back(self):
        self.api.GetForegroundWindow.side_effect = (123, 456)
        with self.assertRaisesRegex(CaptureError, "changed during capture"):
            capture_visible_window(self.source)
        self.api.GetForegroundWindow.side_effect = None
        self.grab.reset_mock()
        self.grab.side_effect = OSError("screen capture failure")
        with self.assertRaisesRegex(CaptureError, "could not be captured"):
            capture_visible_window(self.source)
        self.grab.assert_called_once_with(self.api, (-900, 100, -100, 500))

    def test_native_failure_stage_and_code_remain_visible_to_the_app(self):
        self.grab.side_effect = OSError("BitBlt: copy failed (Windows error 87)")
        with self.assertRaisesRegex(CaptureError, r"BitBlt.*Windows error 87") as failure:
            capture_visible_window(self.source)
        self.assertIs(failure.exception.__cause__, self.grab.side_effect)


class ScreenRegionBitmapTests(unittest.TestCase):
    def native_buffers(self):
        api, gdi = Mock(), Mock()
        api.GetDC.return_value = 1
        gdi.CreateCompatibleDC.return_value = 2
        gdi.CreateCompatibleBitmap.return_value = 3
        gdi.SelectObject.return_value = 4
        gdi.BitBlt.return_value = True
        return api, gdi

    def test_gdi_copies_only_bounded_pixels_and_releases_native_resources(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = Mock(), Mock()
        api.GetDC.return_value = 1
        gdi.CreateCompatibleDC.return_value = 2
        gdi.CreateCompatibleBitmap.return_value = 3
        gdi.SelectObject.return_value = 4
        gdi.BitBlt.return_value = True

        def read_pixels(_dc, _bitmap, _start, rows, pixels, header, _usage):
            self.assertEqual(header._obj.height, -2)
            ctypes.memmove(pixels, bytes([0, 0, 255, 0]) * 6, 24)
            return rows

        gdi.GetDIBits.side_effect = read_pixels
        with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), patch("vasl_ai_bridge.capture.ImageGrab.grab") as desktop:
            image = _grab_visible_region(api, (-900, 100, -897, 102))
        desktop.assert_not_called()
        gdi.CreateCompatibleBitmap.assert_called_once_with(1, 3, 2)
        gdi.BitBlt.assert_called_once_with(2, 0, 0, 3, 2, 1, -900, 100, 0x40CC0020)
        self.assertEqual(image.size, (3, 2))
        self.assertEqual(image.getpixel((2, 1)), (255, 0, 0))
        gdi.DeleteObject.assert_called_once_with(3)
        gdi.DeleteDC.assert_called_once_with(2)
        api.ReleaseDC.assert_called_once_with(None, 1)

    def test_failed_copy_still_deselects_and_releases_resources(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = Mock(), Mock()
        api.GetDC.return_value = 1
        gdi.CreateCompatibleDC.return_value = 2
        gdi.CreateCompatibleBitmap.return_value = 3
        gdi.SelectObject.return_value = 4
        gdi.BitBlt.return_value = False
        with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), self.assertRaises(OSError):
            _grab_visible_region(api, (0, 0, 3, 2))
        self.assertEqual(gdi.SelectObject.call_count, 2)
        gdi.GetDIBits.assert_not_called()
        gdi.DeleteObject.assert_called_once_with(3)
        gdi.DeleteDC.assert_called_once_with(2)
        api.ReleaseDC.assert_called_once_with(None, 1)

    def test_failed_deselection_never_reads_a_selected_bitmap(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = self.native_buffers()
        gdi.SelectObject.side_effect = (4, 0, 0)
        with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), self.assertRaisesRegex(OSError, "SelectObject.*restore"):
            _grab_visible_region(api, (0, 0, 3, 2))
        gdi.GetDIBits.assert_not_called()
        self.assertEqual(gdi.SelectObject.call_count, 3)
        # Deleting the memory DC first releases any bitmap whose restoration failed.
        calls = [call[0] for call in gdi.mock_calls]
        self.assertLess(calls.index("DeleteDC"), calls.index("DeleteObject"))
        api.ReleaseDC.assert_called_once_with(None, 1)

    def test_native_error_code_is_saved_before_cleanup_can_replace_it(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = self.native_buffers()
        state = {"code": 0}

        def read_failure(*args):
            state["code"] = 87
            return 0

        def cleanup_failure(*args):
            state["code"] = 5
            return 0

        gdi.GetDIBits.side_effect = read_failure
        gdi.DeleteDC.side_effect = cleanup_failure
        with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), \
                patch("vasl_ai_bridge.capture.ctypes.get_last_error", side_effect=lambda: state["code"], create=True), \
                patch("vasl_ai_bridge.capture.ctypes.set_last_error", side_effect=lambda code: state.update(code=code), create=True):
            with self.assertRaisesRegex(OSError, r"GetDIBits.*0 of 2.*Windows error 87") as failure:
                _grab_visible_region(api, (0, 0, 3, 2))
        self.assertEqual(failure.exception.winerror, 87)
        self.assertTrue(any("DeleteDC" in note and "Windows error 5" in note for note in failure.exception.__notes__))
        gdi.DeleteDC.assert_called_once_with(2)
        gdi.DeleteObject.assert_called_once_with(3)
        api.ReleaseDC.assert_called_once_with(None, 1)

    def test_a_native_failure_without_a_code_does_not_report_a_stale_error(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = self.native_buffers()
        state = {"code": 87}
        gdi.BitBlt.return_value = 0
        with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), \
                patch("vasl_ai_bridge.capture.ctypes.get_last_error", side_effect=lambda: state["code"], create=True), \
                patch("vasl_ai_bridge.capture.ctypes.set_last_error", side_effect=lambda code: state.update(code=code), create=True):
            with self.assertRaisesRegex(OSError, "BitBlt") as failure:
                _grab_visible_region(api, (0, 0, 3, 2))
        self.assertEqual(failure.exception.winerror, 0)
        self.assertNotIn("Windows error 87", str(failure.exception))

    def test_cleanup_failure_is_not_hidden_by_an_unrelated_outer_exception(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        api, gdi = self.native_buffers()
        gdi.GetDIBits.side_effect = lambda *args: args[3]
        gdi.DeleteDC.return_value = 0
        try:
            raise ValueError("Unrelated caller recovery")
        except ValueError:
            with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), self.assertRaisesRegex(OSError, "DeleteDC"):
                _grab_visible_region(api, (0, 0, 3, 2))
        gdi.DeleteObject.assert_called_once_with(3)
        api.ReleaseDC.assert_called_once_with(None, 1)

    def test_every_native_allocation_failure_identifies_its_stage(self):
        from vasl_ai_bridge.capture import _grab_visible_region
        for name in ("GetDC", "CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject", "BitBlt"):
            with self.subTest(operation=name):
                api, gdi = self.native_buffers()
                getattr(api if name == "GetDC" else gdi, name).return_value = 0
                with patch("vasl_ai_bridge.capture._gdi32", return_value=gdi), self.assertRaisesRegex(OSError, name):
                    _grab_visible_region(api, (0, 0, 3, 2))


@unittest.skipUnless(sys.platform == "win32", "Window enumeration uses Win32 callbacks")
class WindowListTests(unittest.TestCase):
    def test_lists_named_visible_other_processes_in_stable_order(self):
        api = available_api()
        api.IsWindowVisible.side_effect = lambda handle: handle != 3
        titles = {1: "Zulu board", 2: "alpha board", 3: "Hidden", 4: "", 5: "Own app"}

        def process_id(handle, pointer):
            pointer._obj.value = os.getpid() if handle == 5 else os.getpid() + 1
            return 1

        def enumerate_windows(callback, _parameter):
            for handle in titles:
                callback(handle, 0)
            return True

        api.GetWindowThreadProcessId.side_effect = process_id
        api.EnumWindows.side_effect = enumerate_windows
        with patch("vasl_ai_bridge.capture._user32", return_value=api):
            with patch("vasl_ai_bridge.capture._window_title", side_effect=lambda _api, h: titles[h]):
                sources = list_windows()
        self.assertEqual(sources, [WindowSource(2, "alpha board"), WindowSource(1, "Zulu board")])
        # Minimized state is intentionally not a list filter; capture rejects it.
        api.IsIconic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
