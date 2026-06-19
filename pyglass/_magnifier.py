"""Windows screen capture that excludes one window — via the Magnification API.

``WDA_EXCLUDEFROMCAPTURE`` hides a window from *all* capture (globally), so a
glass pane that uses it to avoid grabbing itself also vanishes from Snipping
Tool / OBS / Teams. The Magnification API's *window filter list*
(``MagSetWindowFilterList`` + ``MW_FILTERMODE_EXCLUDE``) instead excludes a
window from only *this* magnifier's view — so the glass can refract the live
desktop without capturing itself, while staying visible to every other capturer.

This wraps that in a small synchronous capturer: :meth:`MagnifierCapture.grab`
returns an ``(H, W, 4)`` RGBA array of a screen region with the excluded window
removed. Windows-only, pure ctypes, no extra dependencies. Verified to capture
hardware-accelerated (DirectComposition) windows, not just GDI ones.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

import numpy as np

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    try:
        _mag = ctypes.windll.magnification
    except Exception:                       # pragma: no cover
        _mag = None
else:                                       # pragma: no cover - imported guard only
    _user32 = _kernel32 = _mag = None


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]


class _MAGIMAGEHEADER(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint), ("height", ctypes.c_uint),
                ("format", _GUID), ("stride", ctypes.c_uint),
                ("offset", ctypes.c_uint), ("cbSize", ctypes.c_size_t)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)
_SCALECB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_void_p,
                              _MAGIMAGEHEADER, ctypes.c_void_p, _MAGIMAGEHEADER,
                              _RECT, _RECT, wintypes.HRGN)

_MW_FILTERMODE_EXCLUDE = 0
_WS_POPUP = 0x80000000
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_LWA_ALPHA = 0x2
_SW_SHOWNA = 8


def available() -> bool:
    """True if the Magnification API can be used (Windows with magnification.dll)."""
    return _mag is not None


class MagnifierCapture:
    """Capture the screen with ``exclude_widget`` filtered out.

    Construct once (sets up a hidden magnifier host bound to the GUI thread),
    then call :meth:`grab` per frame. Call :meth:`close` to release it.
    """

    _class_atom = None   # window class registered once per process

    def __init__(self, exclude_widget) -> None:
        if _mag is None:
            raise RuntimeError("Magnification API unavailable")

        # Fix DefWindowProcW signature so large LPARAMs (pointers) don't overflow.
        _user32.DefWindowProcW.restype = ctypes.c_longlong
        _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        _mag.MagInitialize.restype = wintypes.BOOL
        if not _mag.MagInitialize():
            raise RuntimeError("MagInitialize failed")

        self._hinst = _kernel32.GetModuleHandleW(None)
        self._ensure_class()

        W = _user32.GetSystemMetrics(78)    # SM_CXVIRTUALSCREEN
        H = _user32.GetSystemMetrics(79)    # SM_CYVIRTUALSCREEN
        self._host = _user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOOLWINDOW,
            "PyGlassMagHost", "pyglass-mag", _WS_POPUP, 0, 0, max(W, 1), max(H, 1),
            None, None, self._hinst, None)
        if not self._host:
            _mag.MagUninitialize()
            raise RuntimeError("magnifier host window creation failed")
        # Near-invisible host so it renders but the user never sees it.
        _user32.SetLayeredWindowAttributes(self._host, 0, 1, _LWA_ALPHA)

        self._magw = _user32.CreateWindowExW(
            0, "Magnifier", "pyglass-magnifier", _WS_CHILD | _WS_VISIBLE,
            0, 0, max(W, 1), max(H, 1), self._host, None, self._hinst, None)
        if not self._magw:
            self.close()
            raise RuntimeError("magnifier control creation failed")

        _mag.MagSetWindowFilterList.restype = wintypes.BOOL
        _mag.MagSetWindowFilterList.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                ctypes.c_int, ctypes.POINTER(wintypes.HWND)]
        _mag.MagSetWindowSource.restype = wintypes.BOOL
        _mag.MagSetWindowSource.argtypes = [wintypes.HWND, _RECT]
        _mag.MagSetImageScalingCallback.restype = wintypes.BOOL
        _mag.MagSetImageScalingCallback.argtypes = [wintypes.HWND, _SCALECB]

        self.set_exclude(exclude_widget)
        self._frame = None
        self._cb = _SCALECB(self._on_scale)         # keep a ref alive
        if not _mag.MagSetImageScalingCallback(self._magw, self._cb):
            self.close()
            raise RuntimeError("MagSetImageScalingCallback failed")

        _user32.ShowWindow(self._host, _SW_SHOWNA)

    # ------------------------------------------------------------------ setup
    @classmethod
    def _ensure_class(cls) -> None:
        if cls._class_atom is not None:
            return

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("proc", _WNDPROC),
                        ("ce", ctypes.c_int), ("we", ctypes.c_int),
                        ("hInst", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                        ("cur", wintypes.HANDLE), ("bg", wintypes.HBRUSH),
                        ("menu", wintypes.LPCWSTR), ("cls", wintypes.LPCWSTR)]

        cls._proc = _WNDPROC(lambda h, m, w, l: _user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASS()
        wc.proc = cls._proc
        wc.hInst = _kernel32.GetModuleHandleW(None)
        wc.cls = "PyGlassMagHost"
        atom = _user32.RegisterClassW(ctypes.byref(wc))
        cls._class_atom = atom or True       # truthy even if already registered

    def set_exclude(self, widget) -> None:
        """Set which window the magnifier should leave out of the capture."""
        arr = (wintypes.HWND * 1)(int(widget.winId()))
        _mag.MagSetWindowFilterList(self._magw, _MW_FILTERMODE_EXCLUDE, 1, arr)

    # ------------------------------------------------------------------ capture
    def _on_scale(self, hw, srcdata, srch, dstdata, dsth, unclip, clip, dirty):
        try:
            h, stride = srch.height, srch.stride
            if srcdata and h and stride:
                self._frame = (ctypes.string_at(srcdata, stride * h),
                               srch.width, h, stride)
        except Exception:
            self._frame = None
        return True

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray | None:
        """Capture the screen rect (physical px) with the excluded window removed.

        Returns an (h, w, 4) RGBA uint8 array, or None on failure. Synchronous:
        forces the magnifier to repaint, which fires the scaling callback."""
        if w < 1 or h < 1:
            return None
        self._frame = None
        # 1:1 capture: size the control to the source so there's no scaling.
        _user32.MoveWindow(self._magw, 0, 0, int(w), int(h), False)
        _mag.MagSetWindowSource(self._magw, _RECT(int(x), int(y), int(x + w), int(y + h)))
        _user32.InvalidateRect(self._magw, None, True)
        _user32.UpdateWindow(self._magw)            # synchronous paint → callback
        if self._frame is None:
            return None
        buf, fw, fh, stride = self._frame
        a = np.frombuffer(buf, np.uint8)[:stride * fh].reshape(fh, stride // 4, 4)
        a = a[:, :fw, :]
        # Magnifier delivers BGRA; convert to RGBA to match qimage_to_array().
        return np.ascontiguousarray(a[:, :, [2, 1, 0, 3]])

    def close(self) -> None:
        try:
            if getattr(self, "_host", None):
                _user32.DestroyWindow(self._host)
        finally:
            self._host = self._magw = None
            try:
                _mag.MagUninitialize()
            except Exception:
                pass
