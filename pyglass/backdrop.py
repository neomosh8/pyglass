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
from PyQt6.QtCore import QObject, QPoint, QProcess, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QWidget

from .refract import qimage_to_array

_SCREENCAPTURE = shutil.which("screencapture") if sys.platform == "darwin" else None


def exclude_from_capture(widget: QWidget) -> bool:
    """Exclude ``widget``'s native window from OS screen capture (macOS only).

    Sets ``NSWindowSharingNone`` via the Obj-C runtime so ``screencapture``
    skips the window — meaning a live grab can run *without* first hiding it, so
    there's no flicker. Returns ``True`` on success.

    Returns ``False`` everywhere else, so the caller falls back to a paused,
    hide-grab-show snapshot (no periodic flicker). Windows has
    ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)``, but it is not reliably
    honoured by Qt's GDI ``grabWindow`` path, so we don't depend on it for live
    refresh — enabling it there risks the glass capturing itself instead.
    """
    if sys.platform != "darwin":
        return False
    try:
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
        send(ctypes.c_void_p(window), objc.sel_registerName(b"setSharingType:"), 0)
        return True
    except Exception:
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
        self, widget: QWidget, *, interval_ms: int = 900, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._widget = widget
        self._excluded = False
        self._live = True
        self._use_screencapture = _SCREENCAPTURE is not None
        # Per-instance snapshot file so multiple desktop panes don't clobber /
        # capture each other through a shared path.
        self._snap_path = Path(gettempdir()) / f"pyglass_live_{id(self)}.png"

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
        """Attempt to exclude the window from capture. Returns whether it stuck.

        When excluded, live auto-refresh can run flicker-free; otherwise the
        backdrop stays paused (one grab, refresh on demand)."""
        self._excluded = exclude_from_capture(self._widget)
        return self._excluded

    def set_live(self, on: bool) -> None:
        self._live = on
        if self.live:
            self._timer.start()
        else:
            self._timer.stop()

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
        if self._excluded:
            self._start_grab()
        else:
            self._grab_sync()

    def _load_snapshot(self) -> QImage | None:
        img = QImage(str(self._snap_path))
        return None if img.isNull() else img

    def _start_grab(self) -> None:
        """Async full-screen capture (window excluded, so no hide → no flicker)."""
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            return
        if self._use_screencapture and _SCREENCAPTURE is not None:
            self._proc.start(_SCREENCAPTURE, ["-x", "-t", "png", str(self._snap_path)])
        else:
            self._grab_sync()

    def _on_grab_done(self, *_args) -> None:
        img = self._load_snapshot()
        if img is not None:
            self._ingest(img)

    def _grab_sync(self) -> None:
        """Fallback grab when the window can't be excluded: hide, shoot, show."""
        w = self._widget
        must_hide = w.isVisible() and not self._excluded
        if must_hide:
            w.hide()
            QApplication.processEvents()
        try:
            if self._use_screencapture and _SCREENCAPTURE is not None:
                subprocess.run(
                    [_SCREENCAPTURE, "-x", "-t", "png", str(self._snap_path)],
                    check=False, timeout=5,
                )
                img = self._load_snapshot()
            else:
                screen = QApplication.primaryScreen()
                img = None if screen is None else screen.grabWindow(0).toImage()
        except Exception:
            img = None
        if must_hide:
            w.show()
            w.raise_()
        if img is not None and not img.isNull():
            self._ingest(img)

    def _ingest(self, img: QImage) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        # `screencapture` and grabWindow(0) both capture the whole *virtual*
        # desktop (all monitors), with the image origin at the virtual top-left.
        # Anchor origin + scale to that virtual geometry, not just the primary
        # screen, or the slice is offset / mis-scaled on multi-monitor setups.
        vgeo = screen.virtualGeometry()
        self._origin = vgeo.topLeft()
        self._dpr = img.width() / max(1, vgeo.width())
        self._array = qimage_to_array(img)
        self.changed.emit()
