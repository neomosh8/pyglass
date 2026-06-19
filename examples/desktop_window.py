"""Example: a glass window over the live desktop, in a few lines.

A parentless ``GlassPane`` defaults to a frameless, always-on-top window backed
by a ``ScreenBackdrop`` — so it refracts your real screen. On macOS it excludes
itself from capture and refreshes live; elsewhere it grabs once and stays
paused (press ``R`` to refresh).

    python examples/desktop_window.py
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout

from pyglass import GlassMaterial, GlassPane


def main() -> int:
    app = QApplication(sys.argv)

    pane = GlassPane(material=GlassMaterial(thickness=0.7, frost=0.15))
    lay = QVBoxLayout(pane.content)
    lay.setContentsMargins(34, 30, 34, 30)
    label = QLabel("Drag me over your desktop.\n[ ] thickness · - + frost · R refresh · Esc")
    label.setWordWrap(True)
    label.setFont(QFont("Arial", 14))
    label.setStyleSheet("color: rgba(255,255,255,0.9);")
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    lay.addWidget(label)

    pane.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
