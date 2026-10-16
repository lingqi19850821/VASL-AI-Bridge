"""Explicit, on-demand Windows window capture and immutable PNG preparation.

Pillow's Windows ``window=HWND`` path captures the selected client area. It
does not guarantee that every Java, hardware-accelerated, or obscured window
will render correctly. The caller must show the prepared image to the human.
No whole-screen capture, background capture, or network access is used here.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from io import BytesIO
import os
import sys
from uuid import uuid4

from PIL import Image, ImageGrab

from .session import Observation


class CaptureError(RuntimeError):
    """A capture could not produce usable evidence for the selected window."""


@dataclass(frozen=True, slots=True)
class WindowSource:
    handle: int
    title: str


@lru_cache(maxsize=1)
def _user32():
    if sys.platform != "win32":
        raise CaptureError("Window capture is available on Windows only.")
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
        function = getattr(api, name)
        function.argtypes = [wintypes.HWND]
        function.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    api.GetWindowTextLengthW.restype = ctypes.c_int
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.restype = ctypes.c_int
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.GetClientRect.restype = wintypes.BOOL
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    api.ClientToScreen.restype = wintypes.BOOL
    api.GetForegroundWindow.argtypes = []
    api.GetForegroundWindow.restype = wintypes.HWND
    api.GetSystemMetrics.argtypes = [ctypes.c_int]
    api.GetSystemMetrics.restype = ctypes.c_int
    api.GetDC.argtypes = [wintypes.HWND]
    api.GetDC.restype = wintypes.HDC
    api.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    api.ReleaseDC.restype = ctypes.c_int
    return api


@lru_cache(maxsize=1)
def _gdi32():
    api = ctypes.WinDLL("gdi32", use_last_error=True)
    signatures = {
        "CreateCompatibleDC": ([wintypes.HDC], wintypes.HDC),
        "CreateCompatibleBitmap": ([wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        "SelectObject": ([wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        "DeleteObject": ([wintypes.HGDIOBJ], wintypes.BOOL),
        "DeleteDC": ([wintypes.HDC], wintypes.BOOL),
        "BitBlt": ([wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL),
        "GetDIBits": ([wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
        ("planes", wintypes.WORD), ("bit_count", wintypes.WORD), ("compression", wintypes.DWORD),
        ("image_size", wintypes.DWORD), ("x_pixels_per_meter", wintypes.LONG),
        ("y_pixels_per_meter", wintypes.LONG), ("colors_used", wintypes.DWORD),
        ("colors_important", wintypes.DWORD),
    ]


def _clear_native_error() -> None:
    # GDI does not clear the thread's last error on successful calls.
    if hasattr(ctypes, "set_last_error"):
        ctypes.set_last_error(0)


def _native_failure(operation: str, detail: str) -> OSError:
    # Read this before any cleanup call can replace the original native error.
    code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
    message = f"{operation}: {detail}"
    if code:
        message += f" (Windows error {code})"
    error = OSError(message)
    error.winerror = code
    return error


def _invalid_gdi_object(handle) -> bool:
    return handle in (None, 0, ctypes.c_void_p(-1).value)


def _grab_visible_region(api, box: tuple[int, int, int, int]) -> Image.Image:
    """Copy only the selected rectangle; Pillow's bbox path first grabs the desktop."""
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    gdi = _gdi32()
    _clear_native_error()
    screen_dc = api.GetDC(None)
    memory_dc = bitmap = previous = None
    bitmap_selected = False
    original_error = None
    try:
        if not screen_dc:
            raise _native_failure("GetDC", "Screen pixels are unavailable.")
        _clear_native_error()
        memory_dc = gdi.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise _native_failure("CreateCompatibleDC", "A local screenshot buffer could not be allocated.")
        _clear_native_error()
        bitmap = gdi.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise _native_failure("CreateCompatibleBitmap", "A local screenshot bitmap could not be allocated.")
        _clear_native_error()
        previous = gdi.SelectObject(memory_dc, bitmap)
        if _invalid_gdi_object(previous):
            raise _native_failure("SelectObject", "The screenshot bitmap is unavailable.")
        bitmap_selected = True
        # SRCCOPY | CAPTUREBLT retains visible layered overlays in this rectangle.
        _clear_native_error()
        if not gdi.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x40CC0020):
            raise _native_failure("BitBlt", "The selected screen pixels could not be copied.")
        _clear_native_error()
        restored = gdi.SelectObject(memory_dc, previous)
        if _invalid_gdi_object(restored):
            raise _native_failure("SelectObject", "The original bitmap could not be restored before reading pixels.")
        bitmap_selected = False
        header = _BitmapInfoHeader()
        header.size, header.width, header.height = ctypes.sizeof(header), width, -height
        header.planes, header.bit_count = 1, 32
        header.image_size = width * height * 4
        pixels = ctypes.create_string_buffer(header.image_size)
        _clear_native_error()
        rows = gdi.GetDIBits(screen_dc, bitmap, 0, height, pixels, ctypes.byref(header), 0)
        if rows != height:
            raise _native_failure("GetDIBits", f"The selected screen pixels could not be read ({rows} of {height} rows).")
        return Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
    except BaseException as error:
        original_error = error
        raise
    finally:
        cleanup_errors = []
        if bitmap_selected:
            _clear_native_error()
            if _invalid_gdi_object(gdi.SelectObject(memory_dc, previous)):
                cleanup_errors.append(_native_failure("SelectObject", "The original bitmap could not be restored during cleanup."))
        # Release the DC first, including a bitmap whose restoration failed.
        if memory_dc:
            _clear_native_error()
            if not gdi.DeleteDC(memory_dc):
                cleanup_errors.append(_native_failure("DeleteDC", "The local screenshot buffer could not be released."))
        if bitmap:
            _clear_native_error()
            if not gdi.DeleteObject(bitmap):
                cleanup_errors.append(_native_failure("DeleteObject", "The local screenshot bitmap could not be released."))
        if screen_dc:
            _clear_native_error()
            if not api.ReleaseDC(None, screen_dc):
                cleanup_errors.append(_native_failure("ReleaseDC", "The screen device context could not be released."))
        if cleanup_errors:
            if original_error is not None:
                for error in cleanup_errors:
                    original_error.add_note(str(error))
            else:
                raise cleanup_errors[0]


def _window_title(api, handle: int) -> str:
    # Bound allocation even when another process returns an unusual title.
    length = min(max(api.GetWindowTextLengthW(handle), 0), 4095)
    buffer = ctypes.create_unicode_buffer(length + 1)
    api.GetWindowTextW(handle, buffer, len(buffer))
    return " ".join(buffer.value.split())


def _window_pid(api, handle: int) -> int:
    process_id = wintypes.DWORD()
    api.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
    return process_id.value


def list_windows() -> list[WindowSource]:
    """List visible named top-level windows outside this application's process.

    Minimized windows remain selectable, but capture asks the user to restore
    them. Enumeration never activates a window or takes a screenshot.
    """
    api = _user32()
    windows: list[WindowSource] = []
    own_pid = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    @callback_type
    def visit(handle, _parameter):
        if api.IsWindowVisible(handle) and _window_pid(api, handle) != own_pid:
            title = _window_title(api, handle)
            if title:
                windows.append(WindowSource(int(handle), title))
        return True

    if not api.EnumWindows(visit, 0):
        raise CaptureError("Windows could not list available capture sources.")
    return sorted(windows, key=lambda item: (item.title.casefold(), item.handle))


def _validate_max_dimension(max_dimension: int) -> None:
    if (
        isinstance(max_dimension, bool)
        or not isinstance(max_dimension, int)
        or not 1 <= max_dimension <= 8192
    ):
        raise CaptureError("Maximum image dimension must be an integer from 1 to 8192.")


def _prepare_image(
    image: Image.Image,
    source: str,
    max_dimension: int,
    captured_at: str,
) -> Observation:
    _validate_max_dimension(max_dimension)
    if not isinstance(image, Image.Image) or image.width < 1 or image.height < 1:
        raise CaptureError("The selected source returned an empty image.")
    if not isinstance(source, str) or not source.strip():
        raise CaptureError("The image must identify its capture source.")
    if image.width * image.height > 40_000_000:
        raise CaptureError("The source image exceeds the local capture size limit.")

    try:
        # Remove metadata and flatten transparency onto a readable background.
        # Work on a copy; future mutations of the caller's image cannot alter
        # the immutable bytes shown by the UI and supplied to the API.
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = image.convert("RGB")
        rgb.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        rgb.info.clear()
        output = BytesIO()
        rgb.save(output, format="PNG")
        prepared = output.getvalue()
    except (OSError, ValueError) as error:
        raise CaptureError("The image could not be prepared as a PNG.") from error

    if len(prepared) > 16 * 1024 * 1024:
        raise CaptureError("The prepared PNG exceeds the local image size limit.")
    return Observation(
        observation_id=str(uuid4()),
        captured_at=captured_at,
        source=" ".join(source.split()),
        png=prepared,
        width=rgb.width,
        height=rgb.height,
    )


def prepare_image(
    image: Image.Image, source: str, max_dimension: int = 2000
) -> Observation:
    """Prepare a local image without capturing a window or contacting a service.

    ``captured_at`` is preparation time for this helper. Native window capture
    records completion of the actual grab separately, before preparation.
    """
    return _prepare_image(
        image, source, max_dimension, datetime.now(UTC).isoformat(timespec="milliseconds")
    )


def _validate_window(api, handle: int) -> None:
    if not api.IsWindow(handle):
        raise CaptureError("The selected window has closed. Refresh the source list.")
    if api.IsIconic(handle):
        raise CaptureError("The selected window is minimized. Restore it before capturing.")
    if not api.IsWindowVisible(handle):
        raise CaptureError("The selected window is not visible. Show it before capturing.")
    rectangle = wintypes.RECT()
    if not api.GetClientRect(handle, ctypes.byref(rectangle)):
        raise CaptureError("The selected window's image area is unavailable.")
    if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
        raise CaptureError("The selected window has an empty image area.")
    if (rectangle.right - rectangle.left) * (rectangle.bottom - rectangle.top) > 40_000_000:
        raise CaptureError("The selected window exceeds the local capture size limit.")


def capture_window(
    source: WindowSource, max_dimension: int = 2000
) -> Observation:
    """Capture only the chosen HWND; errors never fall back to the desktop."""
    _validate_max_dimension(max_dimension)
    if (
        not isinstance(source, WindowSource)
        or isinstance(source.handle, bool)
        or not isinstance(source.handle, int)
        or not 0 < source.handle < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8)
    ):
        raise CaptureError("Choose a valid window before capturing.")
    api = _user32()
    _validate_window(api, source.handle)
    original_pid = _window_pid(api, source.handle)
    if not original_pid or original_pid == os.getpid():
        raise CaptureError("Choose a window from another application before capturing.")

    try:
        image = ImageGrab.grab(window=source.handle)
    except TypeError as error:
        raise CaptureError("This Pillow installation does not support window capture.") from error
    except (OSError, ValueError) as error:
        raise CaptureError(
            "The selected window could not be captured. Restore it and try again."
        ) from error
    captured_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    _validate_window(api, source.handle)
    if _window_pid(api, source.handle) != original_pid:
        raise CaptureError("The selected window changed during capture. Select it again.")
    if not isinstance(image, Image.Image) or image.width < 1 or image.height < 1:
        raise CaptureError("The selected window returned an empty image.")
    extrema = image.convert("RGB").getextrema()
    if max(upper for _lower, upper in extrema) <= 8:
        raise CaptureError(
            "The captured window is black or too dark to verify. "
            "Bring the game into view and try again."
        )
    if all(lower == upper for lower, upper in extrema):
        raise CaptureError(
            "The captured window has no visible detail. "
            "Show the game board and try again."
        )
    title = _window_title(api, source.handle) or source.title
    return _prepare_image(image, title, max_dimension, captured_at)


def _visible_client_box(api, handle: int) -> tuple[int, int, int, int]:
    rectangle = wintypes.RECT()
    if not api.GetClientRect(handle, ctypes.byref(rectangle)):
        raise CaptureError("The selected window's image area is unavailable.")
    upper_left = wintypes.POINT(rectangle.left, rectangle.top)
    lower_right = wintypes.POINT(rectangle.right, rectangle.bottom)
    if not (api.ClientToScreen(handle, ctypes.byref(upper_left)) and
            api.ClientToScreen(handle, ctypes.byref(lower_right))):
        raise CaptureError("The selected window's screen position is unavailable.")
    box = upper_left.x, upper_left.y, lower_right.x, lower_right.y
    virtual_left, virtual_top = api.GetSystemMetrics(76), api.GetSystemMetrics(77)
    virtual_right = virtual_left + api.GetSystemMetrics(78)
    virtual_bottom = virtual_top + api.GetSystemMetrics(79)
    if not (virtual_left <= box[0] < box[2] <= virtual_right and
            virtual_top <= box[1] < box[3] <= virtual_bottom):
        raise CaptureError("Keep the selected window entirely on screen before using the screenshot hotkey.")
    return box


def capture_visible_window(source: WindowSource, max_dimension: int = 8192) -> Observation:
    """Freeze visible client pixels before a hotkey opens UI and changes focus.

    Unlike PrintWindow, this preserves transient Java overlays such as the LOS
    line. It requires the selected window to be foreground and fully on screen.
    Overlapping windows are included exactly as seen in the selected rectangle.
    """
    _validate_max_dimension(max_dimension)
    if (not isinstance(source, WindowSource) or isinstance(source.handle, bool) or
            not isinstance(source.handle, int) or
            not 0 < source.handle < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8)):
        raise CaptureError("Choose a valid window before capturing.")
    api = _user32()
    _validate_window(api, source.handle)
    original_pid = _window_pid(api, source.handle)
    if not original_pid or original_pid == os.getpid():
        raise CaptureError("Choose a window from another application before capturing.")
    if api.GetForegroundWindow() != source.handle:
        raise CaptureError("Bring the selected game window to the foreground before using the screenshot hotkey.")
    box = _visible_client_box(api, source.handle)
    try:
        image = _grab_visible_region(api, box)
    except (OSError, ValueError) as error:
        raise CaptureError(f"The selected window's visible pixels could not be captured: {error}") from error
    captured_at = datetime.now(UTC).isoformat(timespec="milliseconds")
    _validate_window(api, source.handle)
    if (api.GetForegroundWindow() != source.handle or _window_pid(api, source.handle) != original_pid or
            _visible_client_box(api, source.handle) != box):
        raise CaptureError("The selected window changed during capture. Try the screenshot hotkey again.")
    if not isinstance(image, Image.Image) or image.size != (box[2] - box[0], box[3] - box[1]):
        raise CaptureError("The selected window returned an incomplete visible screenshot.")
    title = _window_title(api, source.handle) or source.title
    return _prepare_image(image, title, max_dimension, captured_at)
