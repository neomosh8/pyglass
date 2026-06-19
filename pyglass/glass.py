"""``GlassPopup`` — a frosted refractive modal rendered over its host widget.

This is a *demo* of the low-level API: instead of using :class:`GlassPane`, it
drives the engine pieces directly — a :class:`~pyglass.backdrop.WidgetBackdrop`
for the host scene, a :class:`~pyglass.effect.GlassRenderer` for the refraction
and :func:`~pyglass.effect.paint_glass` for the compositing — so it can add its
own modal chrome (a dimming scrim, an open/close reveal animation and a
click-outside-to-close). It shows how to embed the glass in a bespoke widget
when :class:`GlassPane` isn't the right shape.

A single ``reveal`` property (0 → 1) drives the open/close animation; the live
``thickness`` / ``frost`` dials are bound to ``[ ]`` and ``- +``.
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .backdrop import WidgetBackdrop
from .effect import GlassRenderer, GlassStyle, paint_glass
from .pane import ui_font  # re-exported for backward compatibility
from .refract import GlassMaterial

__all__ = ["GlassPopup", "ui_font"]


class GlassPopup(QWidget):
    """A near-transparent refractive glass modal rendered over its ``host``."""

    PANEL_W = 400
    PANEL_H = 280
    RADIUS = 28

    # Glass dials (see refract.GlassMaterial); neutral pair == the tuned look.
    THICKNESS = 0.5
    FROST = 0.0
    DIAL_STEP = 0.1

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self.material = GlassMaterial(thickness=self.THICKNESS, frost=self.FROST)
        self.style = GlassStyle()
        self._renderer = GlassRenderer(self.material, self.PANEL_W, self.PANEL_H, self.RADIUS)
        # Cooperative host: render just the scene (no children) for the backdrop.
        self._backdrop = WidgetBackdrop(host, scene_provider=host.scene_pixmap)
        self._backdrop.changed.connect(self._on_backdrop_changed)

        self._refracted = None
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

        self._tag = QLabel("", self.panel)
        self._tag.setFont(ui_font(12))
        self._tag.setStyleSheet("color: rgba(255,255,255,0.62); letter-spacing: 0.4px;")
        self._update_dial_readout()

        body = QLabel(
            "Drag me around. The bevelled rim refracts the background and catches "
            "Fresnel reflections — strongest at the grazing edge. Use [ ] to set "
            "thickness and - + for frost.",
            self.panel,
        )
        body.setWordWrap(True)
        body.setFont(ui_font(13))
        body.setStyleSheet("color: rgba(255,255,255,0.82);")

        for w in (title, self._tag, body):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay.addWidget(title)
        lay.addWidget(self._tag)
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
        dy = int((1.0 - self._reveal) * 18)
        self.panel.move(self._panel_home.x(), self._panel_home.y() + dy)
        self._opacity.setOpacity(max(0.0, min(1.0, self._reveal)))

    # ----------------------------------------------------------------- backdrop
    def _capture_backdrop(self) -> None:
        """Cache the host scene (triggers a refract via the `changed` signal)."""
        self._backdrop.refresh()

    def _on_backdrop_changed(self) -> None:
        self._refract()
        self.update()

    def _refract(self, *, fast: bool = False) -> None:
        arr = self._backdrop.array()
        if arr is None:
            self._refracted = None
            return
        origin = self.mapToGlobal(self._panel_home) - self._backdrop.global_origin()
        self._refracted = self._renderer.refract(
            arr, origin, self._backdrop.dpr(), fast=fast
        )

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
        r = self._reveal
        # Light dimming scrim across the whole host (kept faint — glass is clear).
        p.fillRect(self.rect(), QColor(8, 10, 18, int(46 * r)))
        paint_glass(
            p,
            QRectF(self.panel.geometry()),
            self.RADIUS,
            self._refracted,
            style=self.style,
            reveal=r,
        )
        p.end()

    # --------------------------------------------------------------- interaction
    def _set_home(self, top_left: QPoint) -> None:
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
                self._refract(fast=True)   # cheap sharp preview while moving
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

    def resizeEvent(self, _event) -> None:
        if self.isVisible():
            self._set_home(self._panel_home)   # keep position, re-clamp to bounds
            self._capture_backdrop()

    # ------------------------------------------------------------------ dials
    def _update_dial_readout(self) -> None:
        m = self.material
        self._tag.setText(
            f"thickness {m.thickness:.2f}   ·   frost {m.frost:.2f}      [ ]  − +"
        )

    def _nudge_dials(self, *, dt: float = 0.0, df: float = 0.0) -> None:
        t = round(min(1.0, max(0.0, self.material.thickness + dt)), 4)
        f = round(min(1.0, max(0.0, self.material.frost + df)), 4)
        if (t, f) == (self.material.thickness, self.material.frost):
            return
        self.material = replace(self.material, thickness=t, frost=f)
        self._renderer.set_material(self.material)
        self._update_dial_readout()
        self._refract()
        self.update()

    # ------------------------------------------------------------------ property
    def get_reveal(self) -> float:
        return self._reveal

    def set_reveal(self, value: float) -> None:
        self._reveal = value
        self._apply_reveal()
        self.update()

    reveal = pyqtProperty(float, fget=get_reveal, fset=set_reveal)
