"""Gaussian blur for QPixmaps.

The frosted-glass look depends on a genuine separable Gaussian blur of the
pixels sitting behind the panel. We lean on Qt's own ``QGraphicsBlurEffect``
(rendered through an offscreen scene) so the result is fast and high quality,
while taking care to keep the device-pixel-ratio correct on Retina displays.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
)


def blur_pixmap(src: QPixmap, radius: float) -> QPixmap:
    """Return a Gaussian-blurred copy of ``src``.

    ``radius`` is expressed in *logical* pixels and is scaled internally by the
    source's device-pixel-ratio so the perceived blur is identical across
    standard and Retina displays.
    """
    if src.isNull() or radius <= 0:
        return src

    dpr = src.devicePixelRatio() or 1.0

    # Work in raw device pixels (dpr = 1) so the blur radius maths is unambiguous.
    raw = src.toImage()
    raw.setDevicePixelRatio(1.0)
    base = QPixmap.fromImage(raw)

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(base)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius * dpr)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)

    out = QImage(base.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    scene.render(painter)
    painter.end()

    out.setDevicePixelRatio(dpr)
    return QPixmap.fromImage(out)
