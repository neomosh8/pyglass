"""PyGlass — physically-grounded refractive glass for PyQt6.

Two layers, use whichever fits:

* **High-level** — :class:`GlassPane`: a drop-in frameless glass widget. As a
  child it's an in-app modal/panel that refracts your app; parentless it's a
  top-level window that refracts the live desktop. Draggable, with live
  ``thickness`` / ``frost`` dials.

      from pyglass import GlassPane, GlassMaterial
      pane = GlassPane(parent=my_window, material=GlassMaterial(thickness=0.6, frost=0.3))
      pane.show()

* **Low-level** — compose the engine yourself inside any ``paintEvent``:
  :class:`GlassRenderer` (backdrop → refracted pixmap), :func:`paint_glass`
  (compositing), and the :mod:`~pyglass.backdrop` providers. See
  :class:`pyglass.glass.GlassPopup` for a worked example.

The look is driven by :class:`GlassMaterial`'s two dials — ``thickness`` (slab
depth/mass) and ``frost`` (surface roughness) — over the physical
:class:`GlassKernel`. :class:`GlassStyle` tunes the non-physical chrome.
"""

from __future__ import annotations

__version__ = "0.2.1"

from .backdrop import Backdrop, ScreenBackdrop, WidgetBackdrop, exclude_from_capture
from .effect import GlassRenderer, GlassStyle, paint_glass
from .glass import GlassPopup
from .pane import GlassPane, ui_font
from .refract import (
    GlassKernel,
    GlassMaterial,
    array_to_qimage,
    compute_glass,
    qimage_to_array,
)

__all__ = [
    "__version__",
    # high-level widget
    "GlassPane",
    "GlassPopup",
    # material / physics
    "GlassMaterial",
    "GlassKernel",
    "compute_glass",
    # rendering core
    "GlassRenderer",
    "GlassStyle",
    "paint_glass",
    # backdrops
    "Backdrop",
    "WidgetBackdrop",
    "ScreenBackdrop",
    "exclude_from_capture",
    # helpers
    "ui_font",
    "qimage_to_array",
    "array_to_qimage",
]
