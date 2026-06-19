"""Backdrop providers — *what* the glass refracts.

A backdrop hands the renderer an RGBA array of whatever sits behind the glass,
plus the device-pixel-ratio and the global screen position of that array's
origin (so a panel anywhere on screen can be mapped into it).

Two providers ship:

* :class:`WidgetBackdrop` — the rendered content of a host :class:`QWidget`
  (for an in-app glass modal/panel). Static: it re-captures only when asked.
* :class:`ScreenBackdrop` — the live OS desktop behind a frameless top-level
  window (for a desktop glass). It owns the screen-capture machinery, excludes
  its own window from the capture where the OS allows it, and auto-refreshes on
  a timer **only** when that exclusion succeeds (otherwise a periodic
  hide-grab-show would flicker — so it grabs once and stays paused).

Both expose ``changed`` (emitted when a fresh frame is ready) so a widget can
just connect and re-paint.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import gettempdir

import numpy as np
from PyQt6.QtCore import QObject, QPoint, QProcess, QRect, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QWidget

from .refract import qimage_to_array

_SCREENCAPTURE = shutil.which("screencapture") if sys.platform == "darwin" else None


def exclude_from_capture(widget: QWidget, exclude: bool = True) -> bool:
    """Exclude (or re-include) ``widget``'s native window from OS screen capture.

    When excluded, a live screen grab skips the window, so the backdrop can
    refresh *without* first hiding it — live, with no flicker. Pass
    ``exclude=False`` to undo it (e.g. so the window can be screen-recorded).
    Returns ``True`` on success.

    * **macOS** — ``NSWindow.sharingType`` (None to exclude, ReadOnly to allow).
    * **Windows** — ``SetWindowDisplayAffinity`` with ``WDA_EXCLUDEFROMCAPTURE``
      (Win10 2004+/Win11, honoured by DWM even for Qt's ``grabWindow``) or
      ``WDA_NONE``.

    Returns ``False`` elsewhere (or if the call fails — e.g. older Windows), so
    the caller falls back to a paused, grab-on-demand snapshot rather than a
    flickering periodic hide-grab-show.

    Note: an excluded window is invisible to *all* screen capture — recording,
    sharing, OBS, Snipping Tool — not just our own grab; the OS exclusion is
    global. Toggle ``exclude=False`` when you need to capture it.
    """
    try:
        if sys.platform == "darwin":
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            send = objc.objc_msgSend

            view = ctypes.c_void_p(int(widget.winId()))      # NSView* on macOS
            send.restype = ctypes.c_void_p
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            window = send(view, objc.sel_registerName(b"window"))
            if not window:
                return False
            send.restype = None
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            # NSWindowSharingNone = 0, NSWindowSharingReadOnly = 1
            send(ctypes.c_void_p(window), objc.sel_registerName(b"setSharingType:"),
                 0 if exclude else 1)
            return True

        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
            WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x00000000, 0x00000011
            return bool(user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(int(widget.winId())),
                WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))
    except Exception:
        return False
    return False


class Backdrop(QObject):
    """Source of the pixels behind the glass.

    Subclasses fill :attr:`_array` / :attr:`_dpr` / :attr:`_origin` and emit
    :attr:`changed` when a new frame is ready.
    """

    changed = pyqtSignal()
    is_live = False     # True if the backdrop updates on its own (e.g. screen)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._array: np.ndarray | None = None
        self._dpr: float = 1.0
        self._origin = QPoint(0, 0)

    def array(self) -> np.ndarray | None:
        """RGBA (H, W, 4) uint8 array of the backdrop, or None if unavailable."""
        return self._array

    def dpr(self) -> float:
        return self._dpr

    def global_origin(self) -> QPoint:
        """Global-screen position of the array's (0, 0) pixel."""
        return self._origin

    def refresh(self) -> None:                       # pragma: no cover - interface
        """(Re)capture the backdrop. Emits :attr:`changed` on success."""
        raise NotImplementedError

    def start(self) -> None:
        """Begin providing frames (and live updates, if any)."""
        self.refresh()

    def stop(self) -> None:
        """Stop any live updates."""

    def cleanup(self) -> None:
        """Release native resources (called when the pane closes). Default: stop."""
        self.stop()

    def prepare_drag(self) -> None:
        """Called when the pane starts being dragged. Default: nothing (the
        cached frame already covers the whole source, so re-slicing is smooth)."""

    def end_drag(self) -> None:
        """Called when the drag ends. Default: nothing."""


class WidgetBackdrop(Backdrop):
    """Refract the rendered content of a host :class:`QWidget`.

    By default the host is grabbed with the glass widget (``exclude``)
    temporarily hidden, so the glass never refracts itself. Cooperative hosts
    can instead pass ``scene_provider`` — a no-arg callable returning a
    ``QPixmap`` of just the background — to avoid the hide/grab entirely.
    """

    def __init__(
        self,
        host: QWidget,
        *,
        exclude: QWidget | None = None,
        scene_provider=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._exclude = exclude
        self._scene_provider = scene_provider

    def set_exclude(self, widget: QWidget | None) -> None:
        self._exclude = widget

    def global_origin(self) -> QPoint:
        return self._host.mapToGlobal(QPoint(0, 0))

    def refresh(self) -> None:
        if self._scene_provider is not None:
            pm = self._scene_provider()
        else:
            ex = self._exclude
            hide = ex is not None and ex.isVisible()
            if hide:
                ex.setVisible(False)         # keep the glass out of its own backdrop
            pm = self._host.grab()
            if hide:
                ex.setVisible(True)
        if pm is None or pm.isNull():
            self._array = None
            return
        self._dpr = pm.devicePixelRatio() or 1.0
        self._array = qimage_to_array(pm.toImage())
        self.changed.emit()


class ScreenBackdrop(Backdrop):
    """Refract the live OS desktop behind a frameless top-level window.

    ``widget`` is that window — it is excluded from / hidden during capture so
    the glass doesn't refract itself. Call :meth:`configure` once the window has
    a native handle (e.g. in its ``showEvent``) to set up exclusion, then
    :meth:`start`. Auto-refresh runs only when exclusion succeeded.
    """

    is_live = True

    def __init__(
        self, widget: QWidget, *, interval_ms: int | None = None,
        capture_margin: int = 150, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._widget = widget
        self._excluded = False
        self._live = True
        self._use_screencapture = _SCREENCAPTURE is not None
        self._capture_margin = capture_margin   # logical px grabbed around the window
        self._prev = None                        # last grabbed array, for change-skip
        # Per-instance snapshot file so multiple desktop panes don't clobber /
        # capture each other through a shared path.
        self._snap_path = Path(gettempdir()) / f"pyglass_live_{id(self)}.png"

        # The grabWindow path (Windows/Linux) is cheap enough to run live; the
        # macOS path spawns a `screencapture` process each frame, so it stays slow.
        if interval_ms is None:
            interval_ms = 900 if self._use_screencapture else 120
        # Adaptive cadence: poll fast while the desktop is changing, ease off when
        # it's static (so an idle glass doesn't peg the CPU), snap back on change.
        self._fast_ms = interval_ms
        self._slow_ms = max(interval_ms * 2, 300)
        self._idle_grabs = 0
        self._idle_backoff = 8

        self._mag = None        # Windows Magnification capturer (set in configure)
        self._proc = QProcess(self)
        self._proc.finished.connect(self._on_grab_done)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._start_grab)

    # ---- liveness state -------------------------------------------------
    @property
    def excluded(self) -> bool:
        return self._excluded

    @property
    def live(self) -> bool:
        """True when frames are actually auto-refreshing (live AND excluded)."""
        return self._live and self._excluded

    def configure(self) -> bool:
        """Set up clean live capture. Returns whether live (flicker-free) is possible.

        Preferred on Windows: the Magnification API, which excludes the glass from
        *our* capture only — so the pane refracts the live desktop without grabbing
        itself, AND stays visible to screen recording. Otherwise fall back to the
        global capture exclusion (macOS ``screencapture``; or none → paused)."""
        if sys.platform == "win32":
            try:
                from ._magnifier import MagnifierCapture, available
                if available():
                    self._mag = MagnifierCapture(self._widget)
                    self._excluded = True   # capture is clean (no hide, no global hide)
                    return True
            except Exception:
                self._mag = None
        self._excluded = exclude_from_capture(self._widget)
        return self._excluded

    def set_live(self, on: bool) -> None:
        self._live = on
        if self.live:
            self._timer.start()
        else:
            self._timer.stop()

    @property
    def recordable(self) -> bool:
        """True if the window is visible to screen recording (Snipping Tool, OBS).
        With the Magnification capturer this is always true *and* it stays live."""
        return self._mag is not None or not self._excluded

    @property
    def capturable(self) -> bool:
        """True when the backdrop is NOT live (the toggle's paused state). With the
        magnifier the window is recordable while still live, so there's nothing to
        toggle — see :attr:`recordable`."""
        return self._mag is None and not self._excluded

    def set_capturable(self, capturable: bool) -> None:
        """Toggle the global capture-exclusion fallback (no magnifier): capturable
        → visible to recording but paused (``R`` re-grabs); not → hidden but live.
        No-op when the magnifier is active (already recordable *and* live)."""
        if self._mag is not None:
            return
        ok = exclude_from_capture(self._widget, exclude=not capturable)
        if ok:
            self._excluded = not capturable
        if self.live:
            self.start()       # excluded again → resume cheap live capture
        else:
            self.stop()        # capturable → pause; keep the last frame on screen

    def toggle_live(self) -> None:
        # Only meaningful when excluded; without exclusion a live grab flickers.
        if self._excluded:
            self.set_live(not self._live)

    # ---- capture --------------------------------------------------------
    def start(self) -> None:
        self.refresh()
        if self.live:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def refresh(self) -> None:
        if self._mag is not None:
            self._grab_mag()
        elif self._excluded:
            self._start_grab()
        else:
            self._grab_sync()

    def cleanup(self) -> None:
        self._timer.stop()
        if self._mag is not None:
            try:
                self._mag.close()
            except Exception:
                pass
            self._mag = None

    def _grab_mag(self, full: bool = False) -> None:
        """Capture via the Magnification API (Windows): the live screen with the
        glass filtered out — no hide, no global exclusion, stays recordable."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        dpr = screen.devicePixelRatio() or 1.0
        r = screen.virtualGeometry() if full else self._capture_rect()
        arr = self._mag.grab(
            int(r.x() * dpr), int(r.y() * dpr),
            int(r.width() * dpr), int(r.height() * dpr),
        )
        if arr is not None:
            self._ingest_array(arr, r.topLeft(), dpr)

    def _load_snapshot(self) -> QImage | None:
        img = QImage(str(self._snap_path))
        return None if img.isNull() else img

    def _start_grab(self) -> None:
        """Async full-screen capture (window excluded, so no hide → no flicker)."""
        if self._mag is not None:
            self._grab_mag()
            return
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            return
        if self._use_screencapture and _SCREENCAPTURE is not None:
            self._proc.start(_SCREENCAPTURE, ["-x", "-t", "png", str(self._snap_path)])
        else:
            self._grab_sync()

    def _on_grab_done(self, *_args) -> None:
        # macOS `screencapture` path: a full-virtual-desktop PNG.
        img = self._load_snapshot()
        if img is not None:
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            vgeo = screen.virtualGeometry()
            self._ingest(img, vgeo.topLeft(), vgeo.width())

    def _capture_rect(self) -> QRect:
        """Just the region behind the window (+margin for the refraction reach),
        so the per-frame grab is a fraction of a full-screen capture."""
        m = self._capture_margin
        rect = self._widget.frameGeometry().adjusted(-m, -m, m, m)
        screen = QApplication.primaryScreen()
        if screen is not None:
            rect = rect.intersected(screen.virtualGeometry())
        return rect

    def _grab_sync(self, full: bool = False) -> None:
        """Grab via Qt. Hides the window first only when it isn't excluded from
        capture (otherwise it would grab itself); excluded → no hide → no flicker.

        ``full`` forces a whole-virtual-desktop grab even when live — used at the
        start of a drag so the move can re-slice it smoothly without per-frame
        grabs (the cheap sub-rect only covers the region right behind the pane)."""
        w = self._widget
        must_hide = w.isVisible() and not self._excluded
        if must_hide:
            w.hide()
            QApplication.processEvents()
        img = origin = logical_w = None
        try:
            if self._use_screencapture and _SCREENCAPTURE is not None:
                subprocess.run(
                    [_SCREENCAPTURE, "-x", "-t", "png", str(self._snap_path)],
                    check=False, timeout=5,
                )
                img = self._load_snapshot()
                screen = QApplication.primaryScreen()
                if img is not None and screen is not None:
                    vgeo = screen.virtualGeometry()
                    origin, logical_w = vgeo.topLeft(), vgeo.width()
            else:
                screen = QApplication.primaryScreen()
                if screen is not None and self._excluded and not full:
                    # Live: grab only the region behind the window (cheap, tracks
                    # the window as it moves) — the window is excluded, so this
                    # shows the desktop, not the glass.
                    r = self._capture_rect()
                    img = screen.grabWindow(0, r.x(), r.y(), r.width(), r.height()).toImage()
                    origin, logical_w = r.topLeft(), r.width()
                elif screen is not None:
                    # Full virtual-desktop grab: the paused fallback, and the
                    # drag cache (so a move re-slices it smoothly, no clamp).
                    vgeo = screen.virtualGeometry()
                    img = screen.grabWindow(0).toImage()
                    origin, logical_w = vgeo.topLeft(), vgeo.width()
        except Exception:
            img = None
        if must_hide:
            w.show()
            w.raise_()
        if img is not None and not img.isNull() and origin is not None:
            self._ingest(img, origin, logical_w)

    # ---- dragging -------------------------------------------------------
    def prepare_drag(self) -> None:
        """Pause live capture and cache the whole desktop, so dragging re-slices
        it smoothly (no per-frame grab hitching the motion)."""
        self._timer.stop()
        # A sub-rect/stale cache would clamp during a drag; grab the whole desktop
        # so the move re-slices it smoothly. macOS already caches full-screen.
        self._prev = None
        if self._mag is not None:
            self._grab_mag(full=True)
        elif not self._use_screencapture:
            self._grab_sync(full=True)

    def end_drag(self) -> None:
        """Resume cheap live sub-rect capture after the drag settles."""
        self._prev = None
        self.start()

    def _ingest(self, img: QImage, origin: QPoint, logical_w: int) -> None:
        self._ingest_array(qimage_to_array(img), origin, img.width() / max(1, logical_w))

    def _ingest_array(self, arr: np.ndarray, origin: QPoint, dpr: float) -> None:
        # Skip the (expensive) refract when the captured region is unchanged —
        # a static desktop costs only the grab + this compare, no re-render —
        # and ease the poll rate off once it's been still for a while.
        if (self._prev is not None and self._prev.shape == arr.shape
                and np.array_equal(self._prev, arr)):
            self._idle_grabs += 1
            if self._idle_grabs == self._idle_backoff and self._timer.interval() != self._slow_ms:
                self._timer.setInterval(self._slow_ms)
            return
        self._idle_grabs = 0
        if self._timer.interval() != self._fast_ms:
            self._timer.setInterval(self._fast_ms)      # change seen → snap responsive
        self._prev = arr
        self._origin = origin
        self._dpr = dpr
        self._array = arr
        self.changed.emit()
