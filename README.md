# PyGlass

Physically-grounded refractive **glass** for **PyQt6** — drop it onto any app.

PyGlass renders glass the way glass behaves: refraction through a beveled slab,
chromatic dispersion, Fresnel reflectance, an iridescent rim and an optional
frosted (rough-surface) blur. It ships as a reusable package with two layers —
a one-line widget for the common case, and the raw engine for custom widgets.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # PyQt6 + numpy
```

## Use it in your app

**High level — `GlassPane`.** A frameless glass widget. Give it a parent and it
becomes an in-app modal/panel that refracts your app; leave it parentless and it
becomes a top-level window that refracts the live desktop. Draggable, with live
dials built in.

```python
from pyglass import GlassPane, GlassMaterial
from PyQt6.QtWidgets import QVBoxLayout, QLabel

# A glass modal over your existing window — refracts whatever's behind it.
pane = GlassPane(my_window, material=GlassMaterial(thickness=0.6, frost=0.3))
QVBoxLayout(pane.content).addWidget(QLabel("Hello from glass"))
pane.show()

# …or a glass window over the live desktop:
desk = GlassPane(material=GlassMaterial(thickness=0.7, frost=0.15))
desk.show()
```

Put your widgets in `pane.content`. The pane captures its parent (with itself
hidden) for the backdrop, so **no cooperation from the host is needed** — it
works on any widget. See [`examples/`](examples/).

**Low level — compose it yourself.** Build the glass inside your own
`paintEvent` with the engine pieces: a backdrop provider →
[`GlassRenderer`](pyglass/effect.py) (backdrop array → refracted pixmap) →
[`paint_glass`](pyglass/effect.py) (shadow + refraction + tint + rim). See
[`pyglass/glass.py`](pyglass/glass.py) (`GlassPopup`) for a full worked example
with a scrim and an open/close animation.

```python
from pyglass import GlassRenderer, paint_glass, WidgetBackdrop, GlassMaterial

backdrop = WidgetBackdrop(host)                 # or ScreenBackdrop(window)
renderer = GlassRenderer(GlassMaterial(), w, h, radius)
backdrop.changed.connect(lambda: self.update())
# in paintEvent:
pm = renderer.refract(backdrop.array(), origin, backdrop.dpr())
paint_glass(painter, panel_rect, radius, pm)
```

## The two dials

The entire look is driven by [`GlassMaterial`](pyglass/refract.py) — two
perceptual dials in `[0, 1]` that re-derive a dozen physical parameters so the
pane always reads as one coherent piece of glass. The neutral pair
(`thickness=0.5, frost=0`) reproduces the tuned baseline exactly.

| Dial | What it means | What it drives |
| --- | --- | --- |
| **`thickness`** | perceived slab depth / mass (optical path length) | displacement (`strength`), the curved lens-wrap width (`bevel`), the IOR range / rim bend, chromatic dispersion (`chroma`), the spectral rim-line width, and the capture margin so the wrap never clamps |
| **`frost`** | surface roughness (ground / milk glass) | a transmission blur (scatter), a milky multiple-scatter haze, and a softened dispersion line — transmission-side only, so `frost=0` is byte-for-byte the sharp look |

`thickness` is a single scalar standing in for *T*: a thicker slab bends light
more, has a bigger rounded edge, disperses colour more (longer optical path) and
casts a thicker rim — all slaved together. `frost` is microfacet roughness: a
rough face scatters transmitted light into a cone that projects to a blur, plus
a faint milky veil.

`GlassStyle` separately tunes the non-physical chrome (shadow, tint, sheen, rim).

## Run the demos

```bash
.venv/bin/python main.py            # in-app frosted refractive modal
.venv/bin/python main.py --desktop  # glass window over your live desktop
.venv/bin/python examples/in_app_modal.py
.venv/bin/python examples/desktop_window.py
```

In any of them: **drag** the panel; **`[`** / **`]`** adjust thickness;
**`-`** / **`=`** adjust frost; **`R`** refreshes the backdrop; on the desktop
pane **`L`** toggles live auto-refresh; **`Esc`** closes.

## Desktop mode — glass over your real screen

A parentless `GlassPane` (or `python main.py --desktop`) floats over your **live**
desktop and refracts whatever is behind it — all your windows, not just the
wallpaper.

* **macOS:** it shells out to the system **`screencapture`** (which, unlike Qt's
  `grabWindow`, returns the full screen with every window) and excludes *itself*
  from capture via `NSWindowSharingNone`. Because the window is excluded, the
  backdrop **auto-refreshes live** with no hide/flicker, and dragging stays
  smooth (it re-slices the last capture each frame).

  > Needs Screen Recording permission (System Settings → Privacy & Security →
  > Screen Recording) for the terminal/app running Python. If only the wallpaper
  > shows, grant it and relaunch.

* **Windows / Linux:** Qt's `grabWindow` can't be told to exclude the window, so
  a periodic re-grab would flicker. PyGlass therefore captures **once and stays
  paused** (press **`R`** to refresh) — no flicker. The dials still work live
  against the cached frame.

## Platform support

Cross-platform — **macOS, Windows, Linux**. PyQt6 + numpy only. The in-app glass
reads the app's *own* rendered scene (no OS screen-capture permission needed);
fonts fall back gracefully (SF Pro → Segoe UI → Arial) and device-pixel-ratio is
handled, so it renders correctly on Windows HiDPI and Retina alike.

## Render a preview without a display

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/render_preview.py preview.png
```

## Layout

| File | Purpose |
| --- | --- |
| [`pyglass/refract.py`](pyglass/refract.py) | Engine — `GlassKernel` (refraction + Fresnel over a beveled SDF) and `GlassMaterial` (the two dials) |
| [`pyglass/effect.py`](pyglass/effect.py) | `GlassRenderer`, `paint_glass`, `GlassStyle` — the reusable rendering core |
| [`pyglass/backdrop.py`](pyglass/backdrop.py) | `WidgetBackdrop` / `ScreenBackdrop` — *what* the glass refracts |
| [`pyglass/pane.py`](pyglass/pane.py) | `GlassPane` — the drop-in glass widget (+ `ui_font`) |
| [`pyglass/glass.py`](pyglass/glass.py) | `GlassPopup` — in-app modal demo built on the low-level core |
| [`pyglass/desktop.py`](pyglass/desktop.py) | `DesktopGlass` — desktop-window demo, a thin `GlassPane` subclass |
| [`pyglass/demo.py`](pyglass/demo.py) | `DemoBackground` — colourful host scene + launch button |
| [`examples/`](examples/) | Standalone third-party usage of `GlassPane` |
| [`main.py`](main.py) | Entry point (`--desktop` for desktop mode) |

## How the refraction works

The panel is a **beveled glass slab** over a rounded-rectangle signed distance
field. The flat centre passes light straight through; the rim is a quarter-circle
**roundover** whose slope grows toward the edge. The vertical incident ray is
refracted there with **Snell's law** and projected through the glass thickness,
so the `1/(-T_z)` term curls the background into a curved lens-*wrap* (not a flat
shift). Each colour channel uses its own IOR → a **chromatic-dispersion** fringe.
The Schlick–**Fresnel** term rises from ~`F0` at the centre to ~1 at the grazing
rim, where the surface reflects a virtual environment (horizon ambient + a warm
key and cool fill light). A lightened **iridescent** spectral line is added along
the border. Frost adds a fast separable blur of the transmitted background.

All geometry-dependent work (normals, per-channel sample coordinates, Fresnel
weight, reflected environment) is precomputed once into a `GlassKernel`; each
frame only runs the bilinear gather (+ the box blur when frosted), so dragging
stays smooth.
