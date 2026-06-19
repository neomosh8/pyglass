# PyGlass

A physically-grounded glass UI experiment built on **PyQt6**.

The long-term goal is a window that renders glass the way glass actually
behaves — refraction, dispersion, Fresnel reflectance, the works. This repo
grows one step at a time.

## Step 1 — frosted-glass popup ✅

A frameless, translucent popup that overlays its host window and:

1. **Samples the scene behind it** directly from the host widget — no macOS
   screen-recording permission required.
2. **Gaussian-blurs** the slice of that scene sitting behind the panel
   (Retina-correct, via `QGraphicsBlurEffect`).
3. **Composites a glass surface** on top: a frost tint, a top specular sheen,
   a Fresnel-ish rim highlight, a soft drop shadow and a dimming scrim.

A single `reveal` property drives a short open/close animation.

## Step 2 — refractive clear glass ✅

The panel is now modelled as a **beveled glass slab** instead of frost:

1. The **flat centre** passes light straight through — the background is shown
   undistorted.
2. The **beveled rim** refracts the background. The index of refraction ramps
   across the bevel band — `1.5` at the inner edge up to `5` at the very rim —
   so the bend grows sharply toward the edge.
3. Each colour channel is refracted with a slightly different IOR (shorter
   wavelengths bend more), producing a **chromatic-dispersion fringe** along the
   rim.
4. The frost is dropped to a barely-there tint, so the glass is **almost
   transparent**.

The refraction is computed by resampling the (padded) backdrop through a
rounded-rectangle signed distance field — see [`pyglass/refract.py`](pyglass/refract.py).

## Step 4 — Fresnel reflections, environment & dragging ✅

* **Beveled roundover + Snell refraction** — the rim is a quarter-circle
  roundover. The vertical incident ray is refracted at the tilted surface with
  **Snell's law** and projected through the glass thickness, so the `1/(-T_z)`
  term curls the background into a curved lens-*wrap* near the border (not a flat
  directional shift). Each colour channel uses its own IOR, so the colours
  visibly decompose into a prismatic fringe along the edge.
* **Fresnel reflection** — the Schlick term rises from ~`F0` at the flat centre
  to ~1 at the grazing rim. There the surface reflects a **virtual environment**
  (a horizon-biased ambient plus a warm top key light and a cool lower-left
  fill), giving bright, angle-dependent rim glints.
* **Iridescent border glow** — on top of the per-channel fringe, a *lightened*
  spectral colour is added along the rim, its hue cycling around the border so
  each portion of every edge carries its own pale dispersed colour.
* **Draggable** — grab the panel anywhere and move it; the refraction and
  reflections re-sample whatever is now behind it, in real time.

To keep dragging smooth, all geometry-dependent work (normals, per-channel
sample coordinates, Fresnel weight, reflected environment) is precomputed once
into a `GlassKernel`; each frame only runs the bilinear gather.

Tunable knobs live on `GlassPopup`: `BEVEL`, `STRENGTH`, `IOR_EDGE`,
`IOR_INNER`, `CHROMA`, `REFLECT`, `F0`, `DISP_GLOW`, `DISP_SAT`, `DISP_CYCLES`.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Click **Reveal glass panel**, then **drag the panel around** to watch the
background refract live. Click outside the panel, press <kbd>Esc</kbd>, or hit
**Got it** to dismiss.

## Platform support

Cross-platform — **macOS, Windows and Linux**. It uses only PyQt6 + numpy with
no OS-specific APIs: the blur/refraction reads the app's *own* rendered scene
rather than doing an OS screen-capture, so no screen-recording permission is
needed anywhere. Fonts fall back gracefully (SF Pro → Segoe UI → Arial), and
device-pixel-ratio is handled, so it renders correctly on Windows HiDPI and
Retina alike.

## Desktop mode — glass over your real screen

```bash
.venv/bin/python main.py --desktop
```

A frameless, always-on-top pane floats over your **live** desktop and refracts
whatever is behind it — all your windows, not just the wallpaper. **Drag** it
around; **L** toggles live auto-refresh; **R** forces a refresh; **Esc** /
**Got it** to close.

How it captures the real screen (macOS):

* It shells out to the system **`screencapture`** tool, which returns the full
  screen *with every window* — unlike Qt's `grabWindow`, which on modern macOS
  only returns the wallpaper.
* The pane excludes **itself** from capture via `NSWindowSharingNone` (set on its
  `NSWindow` through the Obj-C runtime), so the glass never refracts itself and
  no hide/flicker is needed — which lets it auto-refresh live (~1 fps backdrop)
  while dragging stays smooth (it re-slices the last capture each frame).

> **Screen Recording permission:** `screencapture` still needs Screen Recording
> permission (System Settings → Privacy & Security → Screen Recording) for the
> terminal/app running Python. If only the wallpaper shows through, grant it and
> relaunch. (On Windows/Linux it falls back to Qt's grab.)

## Render a preview without a display

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/render_preview.py preview.png
```

## Layout

| File | Purpose |
| --- | --- |
| `pyglass/refract.py` | `GlassKernel` — refraction + Fresnel reflection over a beveled SDF |
| `pyglass/blur.py` | Retina-correct Gaussian blur for `QPixmap`s (step 1) |
| `pyglass/glass.py` | `GlassPopup` — the refractive glass overlay panel |
| `pyglass/demo.py` | `DemoBackground` — colourful host scene + launch button |
| `pyglass/desktop.py` | `DesktopGlass` — floating glass pane over the real screen |
| `main.py` | Entry point (`--desktop` for desktop mode) |
| `scripts/render_preview.py` | Offscreen PNG render for verification |

## Roadmap

- [x] Step 2 — refraction: warp the backdrop through the panel's beveled rim
- [x] Step 3 — chromatic dispersion at the edges
- [x] Step 4 — Fresnel-weighted reflections + environment map (+ dragging)
- [ ] Step 5 — live backdrop (re-sample as the scene animates)
