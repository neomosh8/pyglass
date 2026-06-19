"""Desktop mode — a floating glass pane that refracts your real, live screen.

This is a *demo* of the high-level API: ``DesktopGlass`` is just a
:class:`pyglass.pane.GlassPane` (parentless, so it defaults to a
:class:`pyglass.backdrop.ScreenBackdrop`) with some content and a status hint.
All the refraction, dragging, dial handling and flicker-free live capture come
from the package.

On macOS the window excludes itself from screen capture
(``NSWindowSharingNone``) so the backdrop auto-refreshes live without flicker.
Elsewhere the window can't be excluded from Qt's screen grab, so it captures
once and stays paused (press ``R`` to refresh) — avoiding a periodic
hide-grab-show flicker.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .pane import GlassPane, ui_font


class DesktopGlass(GlassPane):
    """A frameless, always-on-top glass pane floating over the live desktop."""

    PANEL_W = 460
    PANEL_H = 300
    RADIUS = 30
    MARGIN = 70

    def __init__(self) -> None:
        super().__init__(parent=None)        # parentless → ScreenBackdrop
        self.setWindowTitle("PyGlass · desktop")
        self._build_content()

    # ------------------------------------------------------------------ content
    def _build_content(self) -> None:
        lay = QVBoxLayout(self.content)
        lay.setContentsMargins(34, 30, 34, 28)
        lay.setSpacing(10)

        title = QLabel("PyGlass", self.content)
        title.setFont(ui_font(28, QFont.Weight.DemiBold))
        title.setStyleSheet("color: rgba(255,255,255,0.97);")

        body = QLabel(
            "This pane refracts your live desktop. Drag it over your windows; "
            "the bevelled rim bends and disperses whatever is behind it.",
            self.content,
        )
        body.setWordWrap(True)
        body.setFont(ui_font(14))
        body.setStyleSheet("color: rgba(255,255,255,0.88);")

        self._hint = QLabel("", self.content)
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
        got_it = QPushButton("Got it", self.content)
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

        self._close_btn = QPushButton("✕", self.content)
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
        self._update_hint()

    # ------------------------------------------------------------------ hint
    def _on_dials_changed(self) -> None:
        self._update_hint()

    def _update_hint(self) -> None:
        live = "live ●" if getattr(self.backdrop, "live", False) else "paused"
        m = self.material
        self._hint.setText(
            f"drag · L: {live} · R: refresh · "
            f"[ ] thick {m.thickness:.1f} · − + frost {m.frost:.1f} · Esc"
        )

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        QApplication.quit()
