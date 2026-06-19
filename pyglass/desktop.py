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
import subprocess
import sys

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

from .glass import GlassPopup, ui_font
from .refract import GlassKernel, qimage_to_array

_SCREENCAPTURE = "/usr/sbin/screencapture"
_SNAP_PATH = "/tmp/pyglass_live.png"
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
        self._kernel: GlassKernel | None = None
        self._refracted: QPixmap | None = None
        self._out = None
        self._dragging = False
        self._drag_offset = QPoint(0, 0)
        self._excluded = False
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
        self._hint.setText(f"drag · L: {live} · R: refresh · Esc: close")

    # ----------------------------------------------------------------- capture
    def _start_grab(self) -> None:
        """Kick off an async full-screen capture (window excluded, no flicker)."""
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            return
        self._proc.start(_SCREENCAPTURE, ["-x", "-t", "png", _SNAP_PATH])

    def _on_grab_done(self, *_args) -> None:
        img = QImage(_SNAP_PATH)
        if not img.isNull():
            self._ingest(img)

    def _grab_sync(self) -> None:
        """Fallback grab when the window can't be excluded: hide, shoot, show."""
        was_visible = self.isVisible()
        if was_visible:
            self.hide()
            QApplication.processEvents()
        try:
            subprocess.run(
                [_SCREENCAPTURE, "-x", "-t", "png", _SNAP_PATH],
                check=False, timeout=5,
            )
        except Exception:
            pass
        if was_visible:
            self.show()
            self.raise_()
        img = QImage(_SNAP_PATH)
        if not img.isNull():
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
        g = GlassPopup  # reuse the tuned parameters
        self._kernel = GlassKernel(
            int(self.PANEL_W * dpr),
            int(self.PANEL_H * dpr),
            int(g.PAD * dpr),
            self.RADIUS * dpr,
            bevel=g.BEVEL * dpr,
            strength=g.STRENGTH * dpr,
            ior_edge=g.IOR_EDGE,
            ior_inner=g.IOR_INNER,
            chroma=g.CHROMA,
            reflect=g.REFLECT,
            f0=g.F0,
            disp_glow=g.DISP_GLOW,
            disp_sat=g.DISP_SAT,
            disp_cycles=g.DISP_CYCLES,
            disp_width=g.DISP_WIDTH * dpr,
        )

    def _refract(self) -> None:
        snap = self._snap
        if snap is None or self._kernel is None:
            return
        dpr = self._dpr
        pad = int(GlassPopup.PAD * dpr)
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

        self._out = self._kernel.apply(padded)
        img = QImage(self._out.data, pw, ph, pw * 4, QImage.Format.Format_RGBA8888)
        img.setDevicePixelRatio(dpr)
        self._refracted = QPixmap.fromImage(img)
        self._refracted.setDevicePixelRatio(dpr)
        self.update()

    # -------------------------------------------------------------------- events
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dpr == 0.0:  # first show
            self._excluded = _exclude_from_capture(self)
            self._update_hint()
            # First snapshot: async if excluded, otherwise hide/grab/show.
            if self._excluded:
                self._start_grab()
                if self._live:
                    self._timer.start()
            else:
                self._grab_sync()

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
                self._refract()
                return True
            if et == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                self.panel.setCursor(Qt.CursorShape.OpenHandCursor)
                self.refresh()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_R:
            self.refresh()
        elif event.key() == Qt.Key.Key_L and self._excluded:
            self._live = not self._live
            self._timer.start() if self._live else self._timer.stop()
            self._update_hint()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
        QApplication.quit()
