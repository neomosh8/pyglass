"""Reusable glass rendering core — the engine layer of PyGlass.

Two pieces, both UI-agnostic so you can wire them into your own widget:

* :class:`GlassRenderer` turns a *backdrop* (an RGBA numpy array of whatever is
  behind the glass) into a refracted :class:`~PyQt6.QtGui.QPixmap`. It owns a
  :class:`~pyglass.refract.GlassMaterial`, builds and caches the matching
  :class:`~pyglass.refract.GlassKernel` for the current size/dpr, and re-slices
  the backdrop at an arbitrary panel position (cheap enough per drag frame).
* :func:`paint_glass` composites the finished surface — drop shadow, the
  refracted backdrop clipped to a rounded rect, a faint tint + sheen and a
  Fresnel rim — into any :class:`~PyQt6.QtGui.QPainter`.

:class:`GlassStyle` collects the non-physical styling knobs (shadow, tint, rim)
so the look can be tuned without touching the physics dials on the material.

The high-level :class:`pyglass.pane.GlassPane` widget is built on these; use
them directly when you want the glass inside your own ``paintEvent``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .refract import GlassMaterial


@dataclass
class GlassStyle:
    """Non-physical styling for :func:`paint_glass` (shadow, tint, sheen, rim).

    Defaults reproduce the tuned PyGlass look. All alphas are 0..255 and are
    multiplied by the paint-time ``reveal`` factor so a panel can fade in.
    """

    # Soft drop shadow: ``shadow_layers`` stacked rounded rects of growing spread.
    shadow_layers: int = 14
    shadow_step: float = 3.2
    shadow_alpha: int = 5
    shadow_top: float = 10.0       # vertical offset of the shadow's top edge
    shadow_bottom: float = 16.0    # ...and its bottom edge (shadow sits below)

    # Interior tint (top → bottom white alpha) so the pane reads as a surface.
    tint_top: int = 16
    tint_bottom: int = 6

    # Body sheen — a soft highlight fading down from the top. 0 disables it.
    sheen_alpha: int = 16
    sheen_extent: float = 0.55     # fraction of the panel height the sheen covers

    # Thin crisp edge line that defines the shape (top / mid / bottom alpha).
    rim_top: int = 150
    rim_mid: int = 35
    rim_bottom: int = 110
    rim_width: float = 1.3


def paint_glass(
    painter: QPainter,
    rect: QRectF,
    radius: float,
    refracted: QPixmap | None,
    *,
    style: GlassStyle | None = None,
    reveal: float = 1.0,
) -> None:
    """Composite a finished glass panel into ``painter`` over ``rect``.

    ``refracted`` is the pixmap produced by :meth:`GlassRenderer.refract` (its
    device-pixel-ratio is honoured, so pass it through unscaled). ``reveal`` in
    ``[0, 1]`` fades the whole surface in for open/close animations.
    """
    style = style or GlassStyle()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    # Drop shadow — stacked translucent rounded rects, growing outward.
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(style.shadow_layers, 0, -1):
        spread = i * style.shadow_step
        painter.setBrush(QColor(0, 0, 0, int(style.shadow_alpha * reveal)))
        painter.drawRoundedRect(
            rect.adjusted(
                -spread, -spread + style.shadow_top, spread, spread + style.shadow_bottom
            ),
            radius + spread,
            radius + spread,
        )
    painter.restore()

    # Refracted backdrop + tint + sheen, all clipped to the rounded rect.
    painter.save()
    painter.setClipPath(path)
    if refracted is not None:
        painter.setOpacity(reveal)
        painter.drawPixmap(rect.topLeft(), refracted)
        painter.setOpacity(1.0)

    tint = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    tint.setColorAt(0.0, QColor(255, 255, 255, int(style.tint_top * reveal)))
    tint.setColorAt(1.0, QColor(255, 255, 255, int(style.tint_bottom * reveal)))
    painter.fillRect(rect, QBrush(tint))

    if style.sheen_alpha > 0:
        sheen = QLinearGradient(
            rect.topLeft(),
            QPointF(rect.left(), rect.top() + rect.height() * style.sheen_extent),
        )
        sheen.setColorAt(0.0, QColor(255, 255, 255, int(style.sheen_alpha * reveal)))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, QBrush(sheen))
    painter.restore()

    # Crisp Fresnel-ish rim line.
    rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
    rim.setColorAt(0.0, QColor(255, 255, 255, int(style.rim_top * reveal)))
    rim.setColorAt(0.5, QColor(255, 255, 255, int(style.rim_mid * reveal)))
    rim.setColorAt(1.0, QColor(255, 255, 255, int(style.rim_bottom * reveal)))
    painter.setPen(QPen(QBrush(rim), style.rim_width))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.7, 0.7, -0.7, -0.7), radius, radius)


class GlassRenderer:
    """Builds + caches a :class:`GlassKernel` and refracts a moving backdrop.

    The panel size and corner radius are given in *logical* pixels; the kernel
    is (re)built lazily for whatever device-pixel-ratio the backdrop reports and
    is rebuilt automatically when the material changes (e.g. a dial moves) or the
    dpr changes. :meth:`refract` then does just a slice + gather per call.
    """

    def __init__(
        self,
        material: GlassMaterial,
        panel_w: float,
        panel_h: float,
        radius: float,
    ) -> None:
        self.material = material
        self._panel_w = panel_w
        self._panel_h = panel_h
        self._radius = radius
        self._dpr = 0.0
        self._pad = 0
        self._pw = self._ph = 0
        self._kernel = None
        self._out: np.ndarray | None = None   # keeps the QImage buffer alive

    def set_material(self, material: GlassMaterial) -> None:
        """Swap the material; the kernel rebuilds on the next :meth:`refract`."""
        self.material = material
        self._kernel = None

    def set_geometry(self, panel_w: float, panel_h: float, radius: float) -> None:
        """Change the panel size/radius; forces a kernel rebuild."""
        self._panel_w, self._panel_h, self._radius = panel_w, panel_h, radius
        self._kernel = None

    def pad_px(self, dpr: float) -> int:
        return self.material.pad_px(dpr)

    def _ensure_kernel(self, dpr: float) -> None:
        if self._kernel is None or abs(dpr - self._dpr) > 1e-3:
            self._dpr = dpr
            self._pad = self.material.pad_px(dpr)
            self._pw = int(self._panel_w * dpr)
            self._ph = int(self._panel_h * dpr)
            self._kernel = self.material.build_kernel(
                self._pw, self._ph, self._radius * dpr, dpr
            )

    def refract(
        self,
        backdrop: np.ndarray,
        origin: QPoint,
        dpr: float,
        *,
        fast: bool = False,
    ) -> QPixmap | None:
        """Refract the slice of ``backdrop`` behind a panel at ``origin``.

        ``origin`` is the panel's top-left in the backdrop's *logical* pixel
        space (i.e. before multiplying by ``dpr``). ``fast=True`` skips the
        frost scatter for a cheap sharp preview during a drag.
        """
        self._ensure_kernel(dpr)
        pad, pw, ph = self._pad, self._pw, self._ph
        gw, gh = pw + 2 * pad, ph + 2 * pad
        gx = int(origin.x() * dpr) - pad
        gy = int(origin.y() * dpr) - pad

        h, w = backdrop.shape[:2]
        if 0 <= gx <= w - gw and 0 <= gy <= h - gh:
            padded = backdrop[gy:gy + gh, gx:gx + gw]
        else:
            xs = np.clip(np.arange(gx, gx + gw), 0, w - 1)
            ys = np.clip(np.arange(gy, gy + gh), 0, h - 1)
            padded = backdrop[np.ix_(ys, xs)]

        self._out = self._kernel.apply(padded, scatter=not fast)
        img = QImage(self._out.data, pw, ph, pw * 4, QImage.Format.Format_RGBA8888)
        img.setDevicePixelRatio(dpr)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        return pm
