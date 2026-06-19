"""Desktop mode — a floating glass pane that refracts your real, live screen.

It captures the actual desktop (all windows, not just the wallpaper) with the
macOS ``screencapture`` tool, and refracts the slice of that capture sitting
behind the pane. Drag it anywhere; it auto-refreshes so the backdrop stays live.

To avoid the glass capturing *itself*, the window is excluded from screen
capture via ``NSWindowSharingNone`` (so no hide/flicker is needed). If that
exclusion can't be set, it falls back to a hide-grab-show snapshot you refresh
with ``R``.

Why ``screencapture`` instead of Qt's ``grabWindow``? On modern macOS Qt's grab
only returns the wallpaper, while ``screencapture`` returns the full screen with
every window (given Screen Recording permission for the app running Python).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import gettempdir

import numpy as np

from PyQt6.QtCore import QEvent, QPoint, QProcess, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .glass import ui_font
from .refract import GlassKernel, GlassMaterial, qimage_to_array

_SCREENCAPTURE = shutil.which("screencapture") if sys.platform == "darwin" else None
_SNAP_PATH = Path(gettempdir()) / "pyglass_live.png"
_LIVE_INTERVAL_MS = 900


def _exclude_from_capture(widget) -> bool:
    """Set the widget's NSWindow sharingType to None so screen capture skips it.

    Returns True on success (macOS only). Uses the Obj-C runtime via ctypes so we
    don't need PyObjC.
    """
    if sys.platform != "darwin":
        return False
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend

        view = ctypes.c_void_p(int(widget.winId()))     # NSView* on macOS
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


class DesktopGlass(QWidget):
    """A frameless, always-on-top glass pane floating over the live desktop."""

    PANEL_W = 460
    PANEL_H = 300
    RADIUS = 30
    MARGIN = 70           # window padding around the panel (room for the shadow)

    # Two perceptual dials over the glass look (see refract.GlassMaterial).
    THICKNESS = 0.5       # 0 wafer · 0.5 baseline · 1 deep block
    FROST = 0.0           # 0 polished · 1 ground / milk glass
    DIAL_STEP = 0.1       # keyboard nudge per keypress

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyGlass · desktop")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(self.PANEL_W + 2 * self.MARGIN, self.PANEL_H + 2 * self.MARGIN)

        self._snap: np.ndarray | None = None
        self._screen_origin = QPoint(0, 0)
        self._dpr = 0.0
        self._pad = 0
        self._material = GlassMaterial(thickness=self.THICKNESS, frost=self.FROST)
        self._kernel: GlassKernel | None = None
        self._refracted: QPixmap | None = None
        self._out = None
        self._dragging = False
        self._drag_offset = QPoint(0, 0)
        self._excluded = False
        self._use_screencapture = _SCREENCAPTURE is not None
        self._live = True

        self._proc = QProcess(self)
        self._proc.finished.connect(self._on_grab_done)

        self._timer = QTimer(self)
        self._timer.setInterval(_LIVE_INTERVAL_MS)
        self._timer.timeout.connect(self._start_grab)

        self._build_content()
        self._center_on_screen()

    # ------------------------------------------------------------------ content
    def _build_content(self) -> None:
        self.panel = QWidget(self)
        self.panel.setGeometry(self.MARGIN, self.MARGIN, self.PANEL_W, self.PANEL_H)
        self.panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.panel.setCursor(Qt.CursorShape.OpenHandCursor)
        self.panel.installEventFilter(self)

        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(34, 30, 34, 28)
        lay.setSpacing(10)

        title = QLabel("PyGlass", self.panel)
        title.setFont(ui_font(28, QFont.Weight.DemiBold))
        title.setStyleSheet("color: rgba(255,255,255,0.97);")

        body = QLabel(
            "This pane refracts your live desktop. Drag it over your windows; "
            "the bevelled rim bends and disperses whatever is behind it.",
            self.panel,
        )
        body.setWordWrap(True)
        body.setFont(ui_font(14))
        body.setStyleSheet("color: rgba(255,255,255,0.88);")

        self._hint = QLabel("", self.panel)
        self._hint.setFont(ui_font(11))
        self._hint.setStyleSheet("color: rgba(255,255,255,0.6); letter-spacing: 0.3px;")

        for w in (title, body, self._hint):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay.addWidget(title)
        lay.addSpacing(2)
        lay.addWidget(body)
        lay.addStretch(1)
        lay.addWidget(self._hint)

        row = QHBoxLayout()
        row.addStretch(1)
        got_it = QPushButton("Got it", self.panel)
        got_it.setCursor(Qt.CursorShape.PointingHandCursor)
        got_it.setFixedHeight(36)
        got_it.setMinimumWidth(96)
        got_it.setStyleSheet(
            """
            QPushButton {
                color: rgba(255,255,255,0.95);
                background: rgba(255,255,255,0.16);
                border: 1px solid rgba(255,255,255,0.30);
                border-radius: 18px; padding: 0 18px;
                font-size: 13px; font-weight: 600;
            }
            QPushButton:hover { background: rgba(255,255,255,0.26); }
            QPushButton:pressed { background: rgba(255,255,255,0.12); }
            """
        )
        got_it.clicked.connect(self.close)
        row.addWidget(got_it)
        lay.addLayout(row)

        self._close_btn = QPushButton("✕", self.panel)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(255,255,255,0.8); background: rgba(255,255,255,0.12);
                border: none; border-radius: 13px; font-size: 13px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.24); }
            """
        )
        self._close_btn.clicked.connect(self.close)
        self._close_btn.move(self.PANEL_W - 26 - 14, 14)

    def _center_on_screen(self) -> None:
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center() - self.rect().center())

    def _update_hint(self) -> None:
        live = "live ●" if (self._live and self._excluded) else "paused"
        m = self._material
        self._hint.setText(
            f"drag · L: {live} · R: refresh · "
            f"[ ] thick {m.thickness:.1f} · − + frost {m.frost:.1f} · Esc"
        )

    # ----------------------------------------------------------------- capture
    def _load_snapshot(self) -> QImage | None:
        img = QImage(str(_SNAP_PATH))
        return None if img.isNull() else img

    def _start_grab(self) -> None:
        """Kick off an async full-screen capture (window excluded, no flicker)."""
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            return
        if self._use_screencapture and _SCREENCAPTURE is not None:
            self._proc.start(_SCREENCAPTURE, ["-x", "-t", "png", str(_SNAP_PATH)])
        else:
            self._grab_sync()

    def _on_grab_done(self, *_args) -> None:
        img = self._load_snapshot()
        if img is not None:
            self._ingest(img)

    def _grab_sync(self) -> None:
        """Fallback grab when the window can't be excluded: hide, shoot, show."""
        was_visible = self.isVisible()
        if was_visible:
            self.hide()
            QApplication.processEvents()
        try:
            if self._use_screencapture and _SCREENCAPTURE is not None:
                subprocess.run(
                    [_SCREENCAPTURE, "-x", "-t", "png", str(_SNAP_PATH)],
                    check=False, timeout=5,
                )
                img = self._load_snapshot()
            else:
                screen = QApplication.primaryScreen()
                img = None if screen is None else screen.grabWindow(0).toImage()
        except Exception:
            img = None
        if was_visible:
            self.show()
            self.raise_()
        if img is not None and not img.isNull():
            self._ingest(img)

    def _ingest(self, img: QImage) -> None:
        screen = QApplication.primaryScreen()
        self._screen_origin = screen.geometry().topLeft()
        dpr = img.width() / max(1, screen.geometry().width())
        if self._kernel is None or abs(dpr - self._dpr) > 1e-3:
            self._dpr = dpr
            self._build_kernel()
        self._snap = qimage_to_array(img)
        self._refract()

    def refresh(self) -> None:
        if self._excluded:
            self._start_grab()
        else:
            self._grab_sync()

    # ----------------------------------------------------------------- refraction
    def _build_kernel(self) -> None:
        dpr = self._dpr
        self._pad = self._material.pad_px(dpr)
        self._kernel = self._material.build_kernel(
            int(self.PANEL_W * dpr), int(self.PANEL_H * dpr), self.RADIUS * dpr, dpr
        )

    def _refract(self, fast: bool = False) -> None:
        snap = self._snap
        if snap is None or self._kernel is None:
            return
        dpr = self._dpr
        pad = self._pad
        pw = int(self.PANEL_W * dpr)
        ph = int(self.PANEL_H * dpr)

        panel_x = self.x() + self.MARGIN - self._screen_origin.x()
        panel_y = self.y() + self.MARGIN - self._screen_origin.y()
        gx = int(panel_x * dpr) - pad
        gy = int(panel_y * dpr) - pad

        h, w = snap.shape[:2]
        xs = np.clip(np.arange(gx, gx + pw + 2 * pad), 0, w - 1)
        ys = np.clip(np.arange(gy, gy + ph + 2 * pad), 0, h - 1)
        padded = snap[np.ix_(ys, xs)]

        self._out = self._kernel.apply(padded, scatter=not fast)
        img = QImage(self._out.data, pw, ph, pw * 4, QImage.Format.Format_RGBA8888)
        img.setDevicePixelRatio(dpr)
        self._refracted = QPixmap.fromImage(img)
        self._refracted.setDevicePixelRatio(dpr)
        self.update()

    # -------------------------------------------------------------------- events
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dpr == 0.0:  # first show
            if self._use_screencapture:
                self._excluded = _exclude_from_capture(self)
            else:
                self._excluded = False
            self._update_hint()
            # First snapshot: async if excluded, otherwise hide/grab/show.
            QTimer.singleShot(0, self.refresh)
            # Only auto-refresh when the window is excluded from capture. Without
            # exclusion the live grab must hide/show the pane every tick, which
            # reads as a periodic flicker — so we grab once and stay paused.
            if self._live and self._excluded:
                self._timer.start()

    def paintEvent(self, _event) -> None:
        if self._refracted is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        panel = QRectF(self.MARGIN, self.MARGIN, self.PANEL_W, self.PANEL_H)
        path = QPainterPath()
        path.addRoundedRect(panel, self.RADIUS, self.RADIUS)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(14, 0, -1):
            spread = i * 3.2
            p.setBrush(QColor(0, 0, 0, 5))
            p.drawRoundedRect(
                panel.adjusted(-spread, -spread + 10, spread, spread + 16),
                self.RADIUS + spread,
                self.RADIUS + spread,
            )

        p.save()
        p.setClipPath(path)
        p.drawPixmap(panel.topLeft(), self._refracted)

        tint = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        tint.setColorAt(0.0, QColor(255, 255, 255, 16))
        tint.setColorAt(1.0, QColor(255, 255, 255, 6))
        p.fillRect(panel, QBrush(tint))
        p.restore()

        rim = QLinearGradient(panel.topLeft(), panel.bottomRight())
        rim.setColorAt(0.0, QColor(255, 255, 255, 150))
        rim.setColorAt(0.5, QColor(255, 255, 255, 35))
        rim.setColorAt(1.0, QColor(255, 255, 255, 110))
        p.setPen(QPen(QBrush(rim), 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(panel.adjusted(0.7, 0.7, -0.7, -0.7), self.RADIUS, self.RADIUS)
        p.end()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.panel:
            et = event.type()
            if (
                et == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._dragging = True
                self._drag_offset = event.globalPosition().toPoint() - self.pos()
                self.panel.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if et == QEvent.Type.MouseMove and self._dragging:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                self._refract(fast=True)        # cheap sharp preview while moving
                return True
            if et == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                self.panel.setCursor(Qt.CursorShape.OpenHandCursor)
                # When excluded, an async re-grab picks up fresh content with no
                # flicker. Otherwise the snapshot is already pane-free and the
                # drag's last _refract() is current — re-grabbing would only hide/
                # show the pane and glitch, so just settle on the final position.
                if self._excluded:
                    self.refresh()
                else:
                    self._refract()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_R:
            self.refresh()
        elif key == Qt.Key.Key_L and self._excluded:
            self._live = not self._live
            self._timer.start() if self._live else self._timer.stop()
            self._update_hint()
        elif key == Qt.Key.Key_BracketLeft:
            self._nudge_dials(dt=-self.DIAL_STEP)
        elif key == Qt.Key.Key_BracketRight:
            self._nudge_dials(dt=+self.DIAL_STEP)
        elif key == Qt.Key.Key_Minus:
            self._nudge_dials(df=-self.DIAL_STEP)
        elif key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self._nudge_dials(df=+self.DIAL_STEP)
        else:
            super().keyPressEvent(event)

    def _nudge_dials(self, *, dt: float = 0.0, df: float = 0.0) -> None:
        """Turn a dial, rebuild the glass and re-refract the current snapshot.

        Thickness changes the slab geometry (and the capture margin), so the
        kernel is rebuilt; both dials then re-refract the existing snapshot —
        no re-grab, so it stays flicker-free even when paused.
        """
        # Snap to the step grid so repeated ±0.1 nudges stay clean (no 0.8999…
        # drift, and frost returns to *exactly* 0 so its scatter path truly stops).
        t = round(min(1.0, max(0.0, self._material.thickness + dt)), 4)
        f = round(min(1.0, max(0.0, self._material.frost + df)), 4)
        if (t, f) == (self._material.thickness, self._material.frost):
            return
        self._material = replace(self._material, thickness=t, frost=f)
        if self._dpr > 0.0 and self._kernel is not None:
            self._build_kernel()
            self._refract()
        self._update_hint()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
        QApplication.quit()
