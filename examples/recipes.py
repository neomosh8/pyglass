"""Recipes for embedding PyGlass glass in a real app.

Run it:  ``python examples/recipes.py``

A draggable glass card floats over a **live, animated** background and refracts it
in real time. The comments below are the techniques that make glass feel right
in a production UI — the same ones you'd reach for to float a glass tool window
or modal over a busy, changing app.

Techniques shown:

1. **Live refraction of changing content.** A ``GlassPane`` captures the content
   behind it once (on show, and after a drag). If that content *keeps changing*
   — an animation, a video, a scrolling list, a plot that updates — drive
   ``pane.refresh()`` on a timer so the glass tracks it instead of freezing on a
   stale frame. Pick a rate that balances liveness vs. the cost of re-grabbing
   the backdrop (here ~15 fps; 4–8 fps is plenty for a glanceable panel).

2. **Content-friendly material (thin rim).** The dramatic default look has a wide
   refracting bevel — great for a full-window pane, but on a small card it pushes
   the lens-wrap/dispersion *into* your text. For a panel that holds UI, use a
   small ``bevel`` so the interior stays a clean, legible 1:1 surface and only a
   fine rim refracts.

3. **Thick vs. frosted, on two dials.** ``thickness`` is the glassy depth
   (lens-wrap, dispersion, rim); ``frost`` is surface roughness (a milky blur).
   A clear *thick* slab (high thickness, low frost) reads as a glass block you
   see through; raise frost for a ground-glass / privacy look.

4. **Legibility tint.** ``GlassStyle`` controls the frosted tint over the
   refraction. Clear glass (low tint) shows the content vividly but can fight
   dark text; nudge the tint up for a calmer, more readable surface.

5. **Dragging is free.** ``GlassPane`` re-slices the cached backdrop as you move
   it (sharp, cheap preview) and renders the full-quality frost on release — so
   the glass reveals different parts of the app beneath it while you drag, with
   no extra work from you.
"""

from __future__ import annotations

import math
import sys

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QRadialGradient
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from pyglass import GlassMaterial, GlassPane, GlassStyle


class LiveScene(QWidget):
    """A perfectly ordinary app with a moving, colourful background to refract."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyGlass recipes — live glass over a moving scene")
        self.resize(960, 620)
        self._t = 0.0
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(33)                      # ~30 fps animation

    def _tick(self) -> None:
        self._t += 0.033
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(18, 18, 28))
        # drifting bokeh discs — plenty of contrast for the glass to bend
        blobs = [
            (0.30, 0.35, 230, (255, 90, 150)),
            (0.72, 0.30, 260, (90, 170, 255)),
            (0.58, 0.74, 300, (255, 200, 90)),
            (0.20, 0.78, 220, (120, 255, 190)),
        ]
        p.setPen(Qt.PenStyle.NoPen)
        for i, (fx, fy, r, (cr, cg, cb)) in enumerate(blobs):
            cx = (fx + 0.06 * math.sin(self._t + i)) * self.width()
            cy = (fy + 0.06 * math.cos(self._t * 0.8 + i)) * self.height()
            g = QRadialGradient(cx, cy, r)
            g.setColorAt(0.0, QColor(cr, cg, cb, 200))
            g.setColorAt(1.0, QColor(cr, cg, cb, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), r, r)
        p.end()


def main() -> int:
    app = QApplication(sys.argv)
    scene = LiveScene()
    scene.show()

    # --- Technique 2 + 3 + 4: a content-friendly, clearer glass card ----------
    material = GlassMaterial(
        thickness=0.62,    # a chunky, see-through slab
        frost=0.10,        # just a touch of ground-glass softening
        bevel=26.0,        # THIN rim → clean interior for the text below
        strength=20.0,
        pad=44.0,
        ior_edge=4.6,
        chroma=0.13,
        disp_glow=95.0,
        disp_width=2.4,
    )
    style = GlassStyle(tint_top=26, tint_bottom=14)   # a bit more tint for legibility

    pane = GlassPane(scene, panel_size=(380, 240), radius=26,
                     material=material, style=style)
    lay = QVBoxLayout(pane.content)
    lay.setContentsMargins(30, 26, 30, 26)
    title = QLabel("Live glass")
    title.setFont(QFont("Arial", 20, QFont.Weight.DemiBold))
    title.setStyleSheet("color: rgba(255,255,255,0.96);")
    title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    body = QLabel("Drag me over the moving scene — the glass refracts it live.\n"
                  "[ ] thickness · - + frost")
    body.setWordWrap(True)
    body.setStyleSheet("color: rgba(255,255,255,0.85); font-size: 13px;")
    body.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    lay.addWidget(title)
    lay.addWidget(body)
    lay.addStretch(1)
    pane.show()

    # --- Technique 1: keep the refraction live as the scene animates ----------
    # GlassPane only re-grabs its backdrop on show / drag-release; the scene here
    # changes every frame, so refresh it on a timer (skip while dragging — the
    # pane is already re-slicing the cached grab as it moves).
    live = QTimer(scene)

    def _refresh_if_live():
        if not pane._dragging:        # don't fight the drag's cheap re-slice
            pane.refresh()
    live.timeout.connect(_refresh_if_live)
    live.start(66)                    # ~15 fps backdrop — smooth enough, cheap enough

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
