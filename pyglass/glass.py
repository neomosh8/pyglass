"""The refractive glass popup panel.

``GlassPopup`` is a frameless, near-transparent overlay that covers its host
widget. When opened it:

1. samples the host's rendered scene (no OS screen-capture permission needed),
2. refracts the slice of that scene behind the panel through a beveled glass
   slab — the flat centre is undistorted, while the rim bends light with the
   IOR ramping from ``IOR_INNER`` to ``IOR_EDGE`` and dispersing per colour
   channel (see :mod:`pyglass.refract`),
3. composites a very faint tint, a top specular sheen and a Fresnel rim
   highlight on top, with a soft drop shadow and a light dimming scrim behind.

A single ``reveal`` property (0 → 1) drives a short open/close animation.
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    pyqtProperty,
)
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
from PyQt6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import numpy as np

from .refract import GlassKernel, GlassMaterial, qimage_to_array


def ui_font(point_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """A UI font with cross-platform fallbacks (macOS / Windows / Linux)."""
    f = QFont()
    f.setFamilies(
        ["SF Pro Display", "Segoe UI", "Helvetica Neue", "Roboto", "Arial"]
    )
    f.setPointSize(point_size)
    f.setWeight(weight)
    return f


class GlassPopup(QWidget):
    """A near-transparent refractive glass modal rendered over its ``host``."""

    PANEL_W = 400
    PANEL_H = 280
    RADIUS = 28

    # The whole glass look is driven by two perceptual dials (see
    # :class:`pyglass.refract.GlassMaterial`), which re-derive the dozen
    # low-level refraction constants. The neutral pair below reproduces the
    # original tuned appearance exactly.
    THICKNESS = 0.5   # 0 wafer · 0.5 baseline · 1 deep block
    FROST = 0.0       # 0 polished · 1 ground / milk glass
    DIAL_STEP = 0.1   # keyboard nudge per keypress

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self.material = GlassMaterial(thickness=self.THICKNESS, frost=self.FROST)
        self._scene_arr: np.ndarray | None = None  # cached host scene (static)
        self._kernel: GlassKernel | None = None    # cached glass response
        self._dpr = 1.0
        self._pad = self._pw = self._ph = 0
        self._out: np.ndarray | None = None    # keeps the QImage buffer alive
        self._refracted: QPixmap | None = None
        self._reveal = 0.0
        self._panel_home = QPoint(0, 0)
        self._closing = False
        self._dragging = False
        self._drag_offset = QPoint(0, 0)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(host.rect())

        self._build_content()
        self.hide()

        self._anim = QPropertyAnimation(self, b"reveal", self)
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ------------------------------------------------------------------ content
    def _build_content(self) -> None:
        self.panel = QWidget(self)
        self.panel.setFixedSize(self.PANEL_W, self.PANEL_H)
        self.panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.panel.setCursor(Qt.CursorShape.OpenHandCursor)
        # Drag the whole modal by grabbing anywhere on the panel.
        self.panel.installEventFilter(self)

        self._opacity = QGraphicsOpacityEffect(self.panel)
        self._opacity.setOpacity(0.0)
        self.panel.setGraphicsEffect(self._opacity)

        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(30, 26, 30, 26)
        lay.setSpacing(10)

        title = QLabel("PyGlass", self.panel)
        title.setFont(ui_font(26, QFont.Weight.DemiBold))
        title.setStyleSheet("color: rgba(255,255,255,0.96);")

        tag = self._tag = QLabel("", self.panel)
        tag.setFont(ui_font(12))
        tag.setStyleSheet("color: rgba(255,255,255,0.62); letter-spacing: 0.4px;")
        self._update_dial_readout()

        body = QLabel(
            "Drag me around. The bevelled rim refracts the background (IOR 1.5→5, "
            "dispersed per wavelength) and catches Fresnel-weighted reflections "
            "from a virtual environment — strongest at the grazing edge.",
            self.panel,
        )
        body.setWordWrap(True)
        body.setFont(ui_font(13))
        body.setStyleSheet("color: rgba(255,255,255,0.82);")

        # Labels ignore the mouse so a drag anywhere on the panel moves the modal.
        for w in (title, tag, body):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay.addWidget(title)
        lay.addWidget(tag)
        lay.addSpacing(2)
        lay.addWidget(body)
        lay.addStretch(1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
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
                border-radius: 18px;
                padding: 0 18px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(255,255,255,0.26); }
            QPushButton:pressed { background: rgba(255,255,255,0.12); }
            """
        )
        got_it.clicked.connect(self.close_popup)
        row.addWidget(got_it)
        lay.addLayout(row)

        # Small close glyph, manually positioned in the top-right corner.
        self._close_btn = QPushButton("✕", self.panel)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setStyleSheet(
            """
            QPushButton {
                color: rgba(255,255,255,0.8);
                background: rgba(255,255,255,0.12);
                border: none;
                border-radius: 13px;
                font-size: 13px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.24); }
            """
        )
        self._close_btn.clicked.connect(self.close_popup)
        self._close_btn.move(self.PANEL_W - 26 - 14, 14)

    # ----------------------------------------------------------------- geometry
    def _center_panel(self) -> None:
        x = (self.width() - self.panel.width()) // 2
        y = (self.height() - self.panel.height()) // 2
        self._panel_home = QPoint(x, y)
        self._apply_reveal()

    def _apply_reveal(self) -> None:
        # Settle the panel upward and fade content in as `reveal` -> 1.
        dy = int((1.0 - self._reveal) * 18)
        self.panel.move(self._panel_home.x(), self._panel_home.y() + dy)
        self._opacity.setOpacity(max(0.0, min(1.0, self._reveal)))

    # ----------------------------------------------------------------- backdrop
    def _capture_backdrop(self) -> None:
        """Cache the (static) host scene + glass kernel, then refract."""
        scene = self._host.scene_pixmap()
        if scene.isNull():
            self._scene_arr = None
            self._refract()
            return

        self._dpr = scene.devicePixelRatio() or 1.0
        self._scene_arr = qimage_to_array(scene.toImage())
        self._build_kernel()
        self._refract()

    def _build_kernel(self) -> None:
        """(Re)build the glass kernel for the current material + dpr against the
        already-cached scene — no re-capture, so a dial change is cheap."""
        dpr = self._dpr
        self._pad = self.material.pad_px(dpr)
        self._pw = int(self.PANEL_W * dpr)
        self._ph = int(self.PANEL_H * dpr)
        self._kernel = self.material.build_kernel(self._pw, self._ph, self.RADIUS * dpr, dpr)

    def _refract(self, fast: bool = False) -> None:
        """Apply the cached kernel to the scene slice behind the panel.

        Cheap enough to call on every drag frame — only a few gathers run. Pass
        ``fast=True`` to skip the frost scatter (blur/haze) for a sharp, cheap
        preview while dragging; the full frosted result is rendered on release.
        """
        arr = self._scene_arr
        if arr is None or self._kernel is None:
            self._refracted = None
            return

        dpr = self._dpr
        pad, pw, ph = self._pad, self._pw, self._ph
        gw, gh = pw + 2 * pad, ph + 2 * pad
        gx = int(self._panel_home.x() * dpr) - pad
        gy = int(self._panel_home.y() * dpr) - pad

        # Slice the panel region plus `pad` margin. When fully in-bounds (the
        # common case) take a cheap view; near the window edge fall back to
        # clamped indexing so the rim samples via edge replication (no black).
        h, w = arr.shape[:2]
        if 0 <= gx <= w - gw and 0 <= gy <= h - gh:
            padded = arr[gy:gy + gh, gx:gx + gw]
        else:
            xs = np.clip(np.arange(gx, gx + gw), 0, w - 1)
            ys = np.clip(np.arange(gy, gy + gh), 0, h - 1)
            padded = arr[np.ix_(ys, xs)]

        # Keep the result buffer alive while QImage wraps it (no extra copy).
        self._out = self._kernel.apply(padded, scatter=not fast)
        img = QImage(
            self._out.data, pw, ph, pw * 4, QImage.Format.Format_RGBA8888
        )
        img.setDevicePixelRatio(dpr)
        self._refracted = QPixmap.fromImage(img)
        self._refracted.setDevicePixelRatio(dpr)

    # ------------------------------------------------------------ open / close
    def open_popup(self) -> None:
        self._closing = False
        self.setGeometry(self._host.rect())
        self.raise_()
        self.show()
        self.setFocus()
        self._center_panel()
        self._capture_backdrop()

        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.stop()
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def close_popup(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.stop()
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._on_closed)
        self._anim.start()

    def _on_closed(self) -> None:
        if self._reveal <= 0.001:
            self.hide()
        self._closing = False

    # -------------------------------------------------------------------- paint
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        r = self._reveal

        # Light dimming scrim across the whole host (kept faint — glass is clear).
        p.fillRect(self.rect(), QColor(8, 10, 18, int(46 * r)))

        panel = QRectF(self.panel.geometry())
        path = QPainterPath()
        path.addRoundedRect(panel, self.RADIUS, self.RADIUS)

        self._paint_shadow(p, panel, r)

        p.save()
        p.setClipPath(path)
        if self._refracted is not None:
            p.setOpacity(r)
            p.drawPixmap(panel.topLeft(), self._refracted)
            p.setOpacity(1.0)

        # Barely-there tint so the panel still reads as a surface, not a hole.
        tint = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        tint.setColorAt(0.0, QColor(255, 255, 255, int(16 * r)))
        tint.setColorAt(1.0, QColor(255, 255, 255, int(6 * r)))
        p.fillRect(panel, QBrush(tint))

        # Faint body sheen (the environment reflection supplies the rim glints).
        sheen = QLinearGradient(
            panel.topLeft(), QPointF(panel.left(), panel.top() + panel.height() * 0.55)
        )
        sheen.setColorAt(0.0, QColor(255, 255, 255, int(16 * r)))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(panel, QBrush(sheen))
        p.restore()

        # Thin crisp edge line to define the shape.
        rim = QLinearGradient(panel.topLeft(), panel.bottomRight())
        rim.setColorAt(0.0, QColor(255, 255, 255, int(150 * r)))
        rim.setColorAt(0.5, QColor(255, 255, 255, int(35 * r)))
        rim.setColorAt(1.0, QColor(255, 255, 255, int(110 * r)))
        pen = QPen(QBrush(rim), 1.3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(
            panel.adjusted(0.7, 0.7, -0.7, -0.7), self.RADIUS, self.RADIUS
        )
        p.end()

    def _paint_shadow(self, p: QPainter, panel: QRectF, r: float) -> None:
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(12, 0, -1):
            spread = i * 3
            p.setBrush(QColor(0, 0, 0, int(6 * r)))
            rr = panel.adjusted(-spread, -spread + 8, spread, spread + 12)
            p.drawRoundedRect(rr, self.RADIUS + spread, self.RADIUS + spread)
        p.restore()

    # --------------------------------------------------------------- interaction
    def _set_home(self, top_left: QPoint) -> None:
        """Move the panel to ``top_left``, clamped inside the overlay."""
        x = max(0, min(top_left.x(), self.width() - self.panel.width()))
        y = max(0, min(top_left.y(), self.height() - self.panel.height()))
        self._panel_home = QPoint(x, y)
        self._apply_reveal()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.panel:
            et = event.type()
            if (
                et == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._dragging = True
                self._drag_offset = event.position().toPoint()
                self.panel.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if et == QEvent.Type.MouseMove and self._dragging:
                gp = event.globalPosition().toPoint()
                self._set_home(self.mapFromGlobal(gp) - self._drag_offset)
                self._refract(fast=True)   # cheap sharp preview at the new spot
                self.update()
                return True
            if et == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                self.panel.setCursor(Qt.CursorShape.OpenHandCursor)
                self._refract()            # settle: full-quality frosted result
                self.update()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_popup()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_popup()
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

    # ------------------------------------------------------------------ dials
    def _update_dial_readout(self) -> None:
        m = self.material
        self._tag.setText(
            f"thickness {m.thickness:.2f}   ·   frost {m.frost:.2f}      [ ]  − +"
        )

    def _nudge_dials(self, *, dt: float = 0.0, df: float = 0.0) -> None:
        """Turn a dial, rebuild the glass and re-refract the *cached* scene.

        Snaps to the step grid so repeated ±0.1 nudges stay clean and frost
        returns to exactly 0. Only the kernel is rebuilt — the host scene is not
        re-captured (it's static), matching DesktopGlass.
        """
        t = round(min(1.0, max(0.0, self.material.thickness + dt)), 4)
        f = round(min(1.0, max(0.0, self.material.frost + df)), 4)
        if (t, f) == (self.material.thickness, self.material.frost):
            return
        self.material = replace(self.material, thickness=t, frost=f)
        self._update_dial_readout()
        if self._scene_arr is not None:
            self._build_kernel()
            self._refract()
        self.update()

    def resizeEvent(self, _event) -> None:
        if self.isVisible():
            self._set_home(self._panel_home)   # keep position, re-clamp to bounds
            self._capture_backdrop()

    # ------------------------------------------------------------------ property
    def get_reveal(self) -> float:
        return self._reveal

    def set_reveal(self, value: float) -> None:
        self._reveal = value
        self._apply_reveal()
        self.update()

    reveal = pyqtProperty(float, fget=get_reveal, fset=set_reveal)
