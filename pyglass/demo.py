"""A colourful demo host so the frosted-glass blur has something to chew on.

The window paints a vivid diagonal gradient, a few soft "bokeh" discs and some
large background text. The glass popup then blurs this scene behind itself,
which is where the frosted effect becomes obvious.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QPushButton, QWidget

from .glass import GlassPopup, ui_font


class DemoBackground(QWidget):
    """Host widget that renders a scene and owns the glass popup overlay."""

    BOKEH = [
        # (x%, y%, radius_px, r, g, b)
        (0.18, 0.30, 150, 255, 120, 180),
        (0.78, 0.22, 190, 120, 200, 255),
        (0.62, 0.74, 220, 255, 210, 120),
        (0.30, 0.80, 130, 150, 255, 190),
        (0.88, 0.62, 110, 200, 160, 255),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyGlass — frosted popup")
        self.resize(960, 600)
        self.setMinimumSize(640, 420)

        self.popup = GlassPopup(self)

        self._button = QPushButton("Reveal glass panel", self)
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setFixedSize(200, 46)
        self._button.setStyleSheet(
            """
            QPushButton {
                color: white;
                background: rgba(0,0,0,0.35);
                border: 1px solid rgba(255,255,255,0.45);
                border-radius: 23px;
                font-size: 15px;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(0,0,0,0.50); }
            QPushButton:pressed { background: rgba(0,0,0,0.25); }
            """
        )
        self._button.clicked.connect(self.popup.open_popup)

    # ------------------------------------------------------------------- scene
    def _paint_scene(self, p: QPainter, rect: QRectF) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor(36, 31, 84))
        base.setColorAt(0.5, QColor(86, 41, 120))
        base.setColorAt(1.0, QColor(180, 64, 96))
        p.fillRect(rect, base)

        p.setPen(Qt.PenStyle.NoPen)
        for fx, fy, radius, r, g, b in self.BOKEH:
            cx = rect.left() + fx * rect.width()
            cy = rect.top() + fy * rect.height()
            grad = QRadialGradient(cx, cy, radius)
            grad.setColorAt(0.0, QColor(r, g, b, 170))
            grad.setColorAt(1.0, QColor(r, g, b, 0))
            p.setBrush(grad)
            p.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        # Thin grid lines — straight references that make the rim's refraction
        # and chromatic dispersion easy to read.
        pen = QPen(QColor(255, 255, 255, 64))
        pen.setWidthF(1.4)
        p.setPen(pen)
        step = 44
        x = rect.left()
        while x <= rect.right():
            p.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            x += step
        y = rect.top()
        while y <= rect.bottom():
            p.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += step

        p.setPen(QColor(255, 255, 255, 30))
        p.setFont(ui_font(120, QFont.Weight.Black))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "GLASS")

    def scene_pixmap(self) -> QPixmap:
        """Render *only* the scene (no child widgets) to a pixmap."""
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        self._paint_scene(p, QRectF(self.rect()))
        p.end()
        return pm

    # ------------------------------------------------------------------ events
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        self._paint_scene(p, QRectF(self.rect()))
        p.end()

    def resizeEvent(self, _event) -> None:
        self.popup.setGeometry(self.rect())
        self._button.move(
            (self.width() - self._button.width()) // 2,
            self.height() - self._button.height() - 40,
        )
