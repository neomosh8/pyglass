"""macOS screen capture that excludes one window — via ScreenCaptureKit.

``NSWindowSharingNone`` hides a window from *all* capture (globally), so a glass
pane that uses it to avoid grabbing itself also vanishes from QuickTime / OBS /
Teams. ScreenCaptureKit's ``SCContentFilter(display:excludingWindows:)`` instead
excludes a window from only *this* stream — so the glass can refract the live
desktop without capturing itself, while staying visible to every other recorder.
This is the macOS analog of the Windows Magnification filter in
:mod:`pyglass._magnifier`.

It wraps the stream in a small synchronous capturer mirroring ``MagnifierCapture``:
:meth:`ScreenCaptureKitCapture.grab` returns an ``(H, W, 4)`` RGBA array of a
screen region with the excluded window removed. macOS 12.3+, needs Screen
Recording permission, and PyObjC (``pyglass-qt[macos]``).
"""

from __future__ import annotations

import platform
import sys
import threading
import time

import numpy as np

# kCVPixelBufferLock_ReadOnly / kCVPixelFormatType_32BGRA
_LOCK_READONLY = 0x00000001
_PF_BGRA = 1111970369
_OUTPUT_TYPE_SCREEN = 0


def available() -> bool:
    """True on macOS ≥ 12.3 with the PyObjC ScreenCaptureKit framework present."""
    if sys.platform != "darwin":
        return False
    try:
        ver = tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
        if ver and ver < (12, 3):
            return False
    except Exception:
        pass
    try:
        import objc  # noqa: F401
        import ScreenCaptureKit  # noqa: F401
        import Quartz  # noqa: F401
        from CoreMedia import CMSampleBufferGetImageBuffer  # noqa: F401
    except Exception:
        return False
    return True


def _window_id(widget) -> int | None:
    """The CGWindowID of a Qt widget's native window (``NSWindow.windowNumber``)."""
    try:
        import objc

        view = objc.objc_object(c_void_p=int(widget.winId()))   # NSView*
        win = view.window()
        if win is None:
            return None
        num = int(win.windowNumber())
        return num if num > 0 else None
    except Exception:
        return None


class ScreenCaptureKitCapture:
    """Capture the screen with ``exclude_widget`` filtered out of *this* stream.

    Construct once (starts an SCStream), then call :meth:`grab` per frame.
    :meth:`close` stops the stream. Raises ``RuntimeError`` if SCK is unavailable
    or the window to exclude can't be found (so the caller can fall back).
    """

    def __init__(self, exclude_widget) -> None:
        if not available():
            raise RuntimeError("ScreenCaptureKit unavailable")

        import objc
        from Foundation import NSObject
        from ScreenCaptureKit import (
            SCContentFilter,
            SCStream,
            SCStreamConfiguration,
        )
        from CoreMedia import CMSampleBufferGetImageBuffer, CMTimeMake

        self._objc = objc
        self._SCContentFilter = SCContentFilter
        self._exclude = exclude_widget
        self._lock = threading.Lock()
        self._latest = None                     # most recent CMSampleBuffer (retained)
        self._stream = None

        self._content = self._shareable_content()
        displays = list(self._content.displays())
        if not displays:
            raise RuntimeError("no SCDisplay available")
        self._display = displays[0]             # main display (origin assumed 0,0)

        filt = self._make_filter()

        cfg = SCStreamConfiguration.alloc().init()
        try:
            scale = filt.pointPixelScale()
            rect = filt.contentRect()
            self._width = int(round(rect.size.width * scale))
            self._height = int(round(rect.size.height * scale))
        except Exception:
            self._width = int(self._display.width())
            self._height = int(self._display.height())
        cfg.setWidth_(self._width)
        cfg.setHeight_(self._height)
        cfg.setPixelFormat_(_PF_BGRA)
        cfg.setMinimumFrameInterval_(CMTimeMake(1, 30))
        cfg.setQueueDepth_(3)
        cfg.setShowsCursor_(True)

        # Stream-output delegate: keep only the latest sample buffer (retained by
        # the stored PyObjC wrapper, so its CVPixelBuffer memory stays valid until
        # the next frame replaces it).
        capture = self

        class _Output(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                if otype != _OUTPUT_TYPE_SCREEN:
                    return
                if CMSampleBufferGetImageBuffer(sbuf) is None:
                    return
                with capture._lock:
                    capture._latest = sbuf

        self._output = _Output.alloc().init()
        self._stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, cfg, None
        )
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, _OUTPUT_TYPE_SCREEN, None, None
        )
        if not ok:
            raise RuntimeError(f"addStreamOutput failed: {err}")
        self._start_stream()

    # ------------------------------------------------------------------ setup
    def _shareable_content(self):
        from ScreenCaptureKit import SCShareableContent

        box, ev = {}, threading.Event()

        def handler(content, error):
            box["content"], box["error"] = content, error
            ev.set()

        SCShareableContent.getShareableContentWithCompletionHandler_(handler)
        if not ev.wait(8) or box.get("content") is None:
            raise RuntimeError(f"SCShareableContent failed: {box.get('error')}")
        return box["content"]

    def _find_scwindow(self):
        """Locate the SCWindow for the excluded widget, retrying briefly as the
        window registers with the window server after being shown."""
        if self._exclude is None:
            return None
        for _ in range(5):
            wid = _window_id(self._exclude)
            if wid is not None:
                for w in self._content.windows():
                    if int(w.windowID()) == wid:
                        return w
            time.sleep(0.03)
            self._content = self._shareable_content()
        raise RuntimeError("could not find the glass window in shareable content")

    def _make_filter(self):
        scwin = self._find_scwindow()
        excluding = [scwin] if scwin is not None else []
        return self._SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            self._display, excluding
        )

    def _start_stream(self) -> None:
        ev = threading.Event()
        box = {}

        def handler(error):
            box["error"] = error
            ev.set()

        self._stream.startCaptureWithCompletionHandler_(handler)
        ev.wait(5)
        if box.get("error"):
            raise RuntimeError(f"startCapture failed: {box['error']}")

    # ------------------------------------------------------------------ capture
    def set_exclude(self, widget) -> None:
        """Rebuild the content filter for ``widget`` (e.g. its window changed)."""
        self._exclude = widget
        try:
            self._content = self._shareable_content()
            filt = self._make_filter()
            self._stream.updateContentFilter_completionHandler_(filt, lambda e: None)
        except Exception:
            pass

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray | None:
        """Capture the screen rect (physical px) with the excluded window removed.

        Reads the latest delivered frame; returns an (h, w, 4) RGBA uint8 array
        cropped to the rect, or None if no frame has arrived yet."""
        if w < 1 or h < 1:
            return None
        with self._lock:
            sbuf = self._latest
        if sbuf is None:
            return None

        import Quartz as CV
        from CoreMedia import CMSampleBufferGetImageBuffer

        pb = CMSampleBufferGetImageBuffer(sbuf)
        if pb is None:
            return None
        CV.CVPixelBufferLockBaseAddress(pb, _LOCK_READONLY)
        try:
            base = CV.CVPixelBufferGetBaseAddress(pb)
            stride = CV.CVPixelBufferGetBytesPerRow(pb)
            fw = CV.CVPixelBufferGetWidth(pb)
            fh = CV.CVPixelBufferGetHeight(pb)
            if base is None or stride <= 0:
                return None
            mv = base.as_buffer(stride * fh)
            full = np.frombuffer(mv, np.uint8).reshape(fh, stride // 4, 4)
            # Crop to exactly the requested rect (edge-replicated past the frame
            # bounds, so the array origin always lines up with (x, y) — matching
            # what the magnifier path returns). BGRA → RGBA on the crop only,
            # copied out of the locked buffer so it stays valid after unlock.
            xs = np.clip(np.arange(int(x), int(x) + int(w)), 0, fw - 1)
            ys = np.clip(np.arange(int(y), int(y) + int(h)), 0, fh - 1)
            crop = full[np.ix_(ys, xs)]
            return np.ascontiguousarray(crop[:, :, [2, 1, 0, 3]])
        finally:
            CV.CVPixelBufferUnlockBaseAddress(pb, _LOCK_READONLY)

    def close(self) -> None:
        if self._stream is not None:
            try:
                ev = threading.Event()
                self._stream.stopCaptureWithCompletionHandler_(lambda e: ev.set())
                ev.wait(2)
            except Exception:
                pass
            self._stream = None
        self._latest = None
