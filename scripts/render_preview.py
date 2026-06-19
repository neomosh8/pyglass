"""Render the demo scene + opened glass popup to a PNG (offscreen).

Used for visual verification without needing a visible display:

    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/render_preview.py preview.png
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from pyglass.demo import DemoBackground


def render(path: str, size: tuple[int, int] = (960, 600)) -> None:
    app = QApplication(sys.argv)

    host = DemoBackground()
    host.resize(*size)
    host.show()
    host.resizeEvent(None)  # position the button + size the overlay

    popup = host.popup
    popup.setGeometry(host.rect())
    popup.show()
    popup._center_panel()
    popup._capture_backdrop()
    popup.set_reveal(1.0)
    # QWidget.render() + a child QGraphicsEffect misplaces the child tree; at
    # full reveal the opacity effect is a visual no-op, so disable it here.
    popup._opacity.setEnabled(False)

    final = QPixmap(host.size())
    final.fill(host.palette().window().color())
    painter = QPainter(final)
    painter.drawPixmap(0, 0, host.scene_pixmap())
    # Render the button, then the overlay (scrim + glass + content) on top.
    host._button.render(painter, host._button.pos())
    popup.render(painter, QRect(0, 0, host.width(), host.height()).topLeft())
    painter.end()

    final.save(path)
    print(f"wrote {path} ({final.width()}x{final.height()})")
    app.quit()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "preview.png"
    render(out)
