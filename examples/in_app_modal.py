"""Example: a PyGlass modal over *your* PyQt6 app.

Shows the high-level API — drop a ``GlassPane`` onto any widget and it refracts
whatever your app is painting behind it. No cooperation needed from the host:
the pane captures its parent (with itself hidden) for the backdrop.

    python examples/in_app_modal.py
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pyglass import GlassMaterial, GlassPane


class MyApp(QWidget):
    """A perfectly ordinary app window — nothing glass-aware about it."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("My App")
        self.resize(900, 600)
        self._panel: GlassPane | None = None

        btn = QPushButton("Open glass panel", self)
        btn.setStyleSheet(
            "QPushButton{color:white;background:rgba(0,0,0,0.35);"
            "border:1px solid rgba(255,255,255,0.45);border-radius:22px;"
            "font-size:15px;font-weight:600;padding:10px 22px;}"
            "QPushButton:hover{background:rgba(0,0,0,0.5);}"
        )
        btn.clicked.connect(self.open_panel)
        self._btn = btn

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0.0, QColor(40, 33, 96))
        g.setColorAt(1.0, QColor(176, 64, 110))
        p.fillRect(self.rect(), g)
        p.setPen(QColor(255, 255, 255, 30))
        p.setFont(QFont("Arial", 120, QFont.Weight.Black))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "APP")
        p.end()

    def resizeEvent(self, _e) -> None:
        self._btn.move(self.width() - self._btn.width() - 30,
                       self.height() - self._btn.height() - 30)

    def open_panel(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            return
        # One line to get glass over your app. Tune the two dials to taste.
        pane = GlassPane(self, material=GlassMaterial(thickness=0.6, frost=0.25))
        lay = QVBoxLayout(pane.content)
        lay.setContentsMargins(34, 30, 34, 30)
        title = QLabel("Glass panel")
        title.setFont(QFont("Arial", 22, QFont.Weight.DemiBold))
        title.setStyleSheet("color: rgba(255,255,255,0.96);")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        body = QLabel(
            "This panel refracts the app behind it. Drag it around. "
            "Press [ ] for thickness, - + for frost, Esc to close."
        )
        body.setWordWrap(True)
        body.setStyleSheet("color: rgba(255,255,255,0.85); font-size: 13px;")
        body.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        close = QPushButton("Close")
        close.setStyleSheet(
            "QPushButton{color:white;background:rgba(255,255,255,0.16);"
            "border:1px solid rgba(255,255,255,0.3);border-radius:16px;"
            "padding:6px 16px;font-weight:600;}"
            "QPushButton:hover{background:rgba(255,255,255,0.26);}"
        )
        close.clicked.connect(pane.close)
        lay.addWidget(title)
        lay.addWidget(body)
        lay.addStretch(1)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)

        self._panel = pane
        pane.show()
        pane.raise_()
        pane.setFocus()


def main() -> int:
    app = QApplication(sys.argv)
    w = MyApp()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
