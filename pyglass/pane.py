"""``GlassPane`` — a drop-in refractive glass panel for any PyQt6 app.

Add it as a child of a widget to get an in-app glass modal/panel that refracts
your app, or create it parentless to get a frameless top-level window that
refracts the live desktop. It wires together a backdrop provider
(:mod:`pyglass.backdrop`), the refraction engine (:class:`GlassRenderer`) and
the compositing (:func:`paint_glass`), and adds optional dragging and live
``thickness`` / ``frost`` dials.

Minimal use::

    pane = GlassPane(parent=my_window)          # in-app glass over my_window
    QVBoxLayout(pane.content).addWidget(QLabel("Hello"))
    pane.show()

    desk = GlassPane(material=GlassMaterial(frost=0.2))   # over the live desktop
    desk.show()

Subclass it to add your own content/chrome (see :class:`pyglass.desktop.DesktopGlass`),
or skip it entirely and drive :class:`GlassRenderer` + :func:`paint_glass`
yourself (see :class:`pyglass.glass.GlassPopup`).

Notes:

* Put your widgets in :attr:`GlassPane.content`. The pane is dragged by its
  background, so a press that lands on an *interactive* child (a button) is
  consumed by that child. Make non-interactive children (labels, icons) pass
  through with ``setAttribute(WA_TransparentForMouseEvents, True)`` so the whole
  panel stays draggable.
* The pane auto-centers on first show; call ``move()`` (any time) to place it
  yourself. ``Esc`` closes it and the ``[`` ``]`` / ``-`` ``+`` keys drive the
  dials, so call :meth:`setFocus` (done on show) for keyboard control.
* A child pane only refracts widgets that are painted *behind* it; make sure the
  parent is large enough to sit behind the whole panel + shadow margin.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QFont, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from .backdrop import Backdrop, ScreenBackdrop, WidgetBackdrop
from .effect import GlassRenderer, GlassStyle, paint_glass
from .refract import GlassMaterial


def ui_font(point_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """A UI font with cross-platform fallbacks (macOS / Windows / Linux)."""
    f = QFont()
    f.setFamilies(["SF Pro Display", "Segoe UI", "Helvetica Neue", "Roboto", "Arial"])
    f.setPointSize(point_size)
    f.setWeight(weight)
    return f


class GlassPane(QWidget):
    """A frameless refractive glass panel; child overlay or top-level window."""

    # Geometry (logical px). Override per-instance via the constructor.
    PANEL_W = 400
    PANEL_H = 280
    RADIUS = 28
    MARGIN = 60            # padding around the slab, leaving room for the shadow

    # Default dials and keyboard step.
    THICKNESS = 0.5
    FROST = 0.0
    DIAL_STEP = 0.1

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        panel_size: tuple[int, int] | None = None,
        radius: float | None = None,
        margin: int | None = None,
        material: GlassMaterial | None = None,
        style: GlassStyle | None = None,
        backdrop: Backdrop | None = None,
        draggable: bool = True,
        dials: bool = True,
    ) -> None:
        super().__init__(parent)
        pw, ph = panel_size or (self.PANEL_W, self.PANEL_H)
        self._panel_w, self._panel_h = pw, ph
        self._radius = self.RADIUS if radius is None else radius
        self._margin = self.MARGIN if margin is None else margin
        self._top_level = parent is None
        self._draggable = draggable
        self._dials_enabled = dials

        self.material = material or GlassMaterial(thickness=self.THICKNESS, frost=self.FROST)
        self.style = style or GlassStyle()
        self._renderer = GlassRenderer(self.material, pw, ph, self._radius)
        self._refracted = None
        self._reveal = 1.0
        self._dragging = False
        self._drag_offset = QPoint(0, 0)
        self._configured = False
        self._positioned = False        # set once the caller/drag places it

        if self._top_level:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(pw + 2 * self._margin, ph + 2 * self._margin)

        # The slab interior — add your widgets/layout here.
        self.content = QWidget(self)
        self.content.setGeometry(self._margin, self._margin, pw, ph)
        self.content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if self._draggable:
            self.content.setCursor(Qt.CursorShape.OpenHandCursor)
            self.content.installEventFilter(self)

        # Default backdrop: the parent's content in-app, else the live screen.
        if backdrop is None:
            backdrop = (
                WidgetBackdrop(parent, exclude=self)
                if parent is not None
                else ScreenBackdrop(self)
            )
        self._backdrop = backdrop
        self._backdrop.changed.connect(self._on_backdrop_changed)

    # ------------------------------------------------------------------ API
    @property
    def backdrop(self) -> Backdrop:
        return self._backdrop

    def panel_rect(self) -> QRectF:
        """The slab rectangle within this widget (where the glass is drawn)."""
        return QRectF(self._margin, self._margin, self._panel_w, self._panel_h)

    def set_material(self, material: GlassMaterial) -> None:
        self.material = material
        self._renderer.set_material(material)
        self._refract()
        self.update()
        self._on_dials_changed()

    def set_reveal(self, reveal: float) -> None:
        """Set the fade-in factor (0..1) used by the open/close animation."""
        self._reveal = max(0.0, min(1.0, reveal))
        self.update()

    def refresh(self) -> None:
        """Re-capture the backdrop (e.g. the app repainted, or `R` was pressed)."""
        self._backdrop.refresh()

    # --------------------------------------------------------------- layout
    def move(self, *args) -> None:
        """Track explicit positioning so :meth:`showEvent` won't auto-center."""
        self._positioned = True
        super().move(*args)

    def center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            super().move(geo.center() - self.rect().center())   # not "user positioned"

    def center_in_parent(self) -> None:
        p = self.parentWidget()
        if p is not None:
            super().move(
                (p.width() - self.width()) // 2, (p.height() - self.height()) // 2
            )

    # -------------------------------------------------------------- backdrop
    def _panel_origin_in_backdrop(self) -> QPoint:
        """Panel top-left in the backdrop's logical pixel space."""
        panel_global = self.mapToGlobal(QPoint(self._margin, self._margin))
        return panel_global - self._backdrop.global_origin()

    def _refract(self, *, fast: bool = False) -> None:
        arr = self._backdrop.array()
        if arr is None:
            return
        self._refracted = self._renderer.refract(
            arr, self._panel_origin_in_backdrop(), self._backdrop.dpr(), fast=fast
        )

    def _on_backdrop_changed(self) -> None:
        self._refract()
        self.update()

    # ---------------------------------------------------------------- events
    def showEvent(self, event) -> None:
        super().showEvent(event)
        # First show ONLY. The (non-excluded) screen grab works by hide→grab→show,
        # and a WidgetBackdrop hides this pane to grab its parent — both re-fire
        # showEvent. Re-starting the backdrop here would then grab → hide/show →
        # showEvent → grab … an endless hide/show loop (flicker). So configure and
        # kick off the capture exactly once.
        if self._configured:
            return
        self._configured = True
        if self._top_level:
            self.center_on_screen()
        elif not self._positioned:
            self.center_in_parent()             # sensible default; move() to override
        if isinstance(self._backdrop, ScreenBackdrop):
            self._backdrop.configure()          # set up capture exclusion if possible
        # Deferred so geometry / native handle settle before the first grab.
        QTimer.singleShot(0, self._backdrop.start)
        self.setFocus()
        self._on_dials_changed()

    def hideEvent(self, event) -> None:
        self._backdrop.stop()                   # pause live capture while hidden
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        paint_glass(
            painter,
            self.panel_rect(),
            self._radius,
            self._refracted,
            style=self.style,
            reveal=self._reveal,
        )
        painter.end()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.content and self._draggable:
            et = event.type()
            if (
                et == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._dragging = True
                self._drag_offset = event.globalPosition().toPoint() - self._top_left_global()
                self.content.setCursor(Qt.CursorShape.ClosedHandCursor)
                # Pause live capture and cache the full backdrop, so the move
                # re-slices it cheaply (no per-frame grab → no heavy/janky drag).
                self._backdrop.prepare_drag()
                return True
            if et == QEvent.Type.MouseMove and self._dragging:
                self._move_to(event.globalPosition().toPoint() - self._drag_offset)
                self._refract(fast=True)        # cheap sharp preview while moving
                self.update()
                return True
            if et == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                self.content.setCursor(Qt.CursorShape.OpenHandCursor)
                self._backdrop.end_drag()       # resume live updates
                self._refract()                 # settle: full-quality at final spot
                self.update()
                return True
        return super().eventFilter(obj, event)

    def _top_left_global(self) -> QPoint:
        return self.mapToGlobal(QPoint(0, 0)) if not self._top_level else self.pos()

    def _move_to(self, top_left_global: QPoint) -> None:
        if self._top_level:
            self.move(top_left_global)          # top-level pos is in global coords
            return
        # Child overlay: drag math is in global coords, but a child's move() is
        # parent-relative — convert first, then clamp the slab inside the parent.
        p = self.parentWidget()
        if p is None:
            self.move(top_left_global)
            return
        tl = p.mapFromGlobal(top_left_global)
        x = max(-self._margin, min(tl.x(), p.width() - self.width() + self._margin))
        y = max(-self._margin, min(tl.y(), p.height() - self.height() + self._margin))
        self.move(x, y)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_R:
            self.refresh()
        elif key == Qt.Key.Key_L and isinstance(self._backdrop, ScreenBackdrop):
            self._backdrop.toggle_live()
            self._on_dials_changed()
        elif self._dials_enabled and key == Qt.Key.Key_BracketLeft:
            self._nudge_dials(dt=-self.DIAL_STEP)
        elif self._dials_enabled and key == Qt.Key.Key_BracketRight:
            self._nudge_dials(dt=+self.DIAL_STEP)
        elif self._dials_enabled and key == Qt.Key.Key_Minus:
            self._nudge_dials(df=-self.DIAL_STEP)
        elif self._dials_enabled and key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self._nudge_dials(df=+self.DIAL_STEP)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------ dials
    def _nudge_dials(self, *, dt: float = 0.0, df: float = 0.0) -> None:
        """Turn a dial and re-refract. Snaps to the step grid so repeated ±0.1
        nudges stay clean and frost returns to exactly 0."""
        from dataclasses import replace

        t = round(min(1.0, max(0.0, self.material.thickness + dt)), 4)
        f = round(min(1.0, max(0.0, self.material.frost + df)), 4)
        if (t, f) == (self.material.thickness, self.material.frost):
            return
        self.set_material(replace(self.material, thickness=t, frost=f))

    def _on_dials_changed(self) -> None:
        """Hook for subclasses to update a readout. No-op by default."""

    def closeEvent(self, event) -> None:
        self._backdrop.stop()
        super().closeEvent(event)
