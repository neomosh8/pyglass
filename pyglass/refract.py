"""Refraction + Fresnel reflection through a beveled glass slab.

The panel is modelled as a slab of glass whose top is flat in the interior and
rolls off near the rim as a quarter-circle *roundover* (the bevel band). Looking
straight down (+z toward the eye):

* In the flat interior the surface normal is straight up, so light passes
  through undeviated — the background is shown 1:1 and almost nothing reflects.
* Inside the bevel the surface tilts; its slope ``tanθ`` grows toward the rim
  (→ large) and collapses to 0 at the inner edge. Two things follow:

  - **Refraction** — light bends, displacing the sampled background. The
    displacement scales with the slope *and* the index of refraction, which is
    ramped from ``IOR_INNER`` at the inner edge to ``IOR_EDGE`` at the rim. Each
    colour channel uses a slightly different IOR → a chromatic-dispersion fringe.
  - **Reflection** — the Schlick-Fresnel term rises from ~``F0`` at the flat
    centre to ~1 at the grazing rim. There we reflect a virtual environment
    (a sky gradient plus two lights) into the surface, giving bright, angle-
    dependent rim highlights.

Everything is vectorised with numpy over a rounded-rectangle signed distance
field.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PyQt6.QtGui import QImage


# --------------------------------------------------------------- QImage <-> ndarray
def qimage_to_array(img: QImage) -> np.ndarray:
    """Return an (H, W, 4) uint8 RGBA array copied from ``img``."""
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(h * bpl)
    arr = np.frombuffer(ptr, np.uint8).reshape(h, bpl)
    return arr[:, : w * 4].reshape(h, w, 4).copy()


def array_to_qimage(arr: np.ndarray) -> QImage:
    """Build a detached QImage (RGBA8888) from an (H, W, 4) uint8 array."""
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w, _ = arr.shape
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


# --------------------------------------------------------------------------- maths
def _rounded_rect_sdf(xs, ys, cx, cy, hx, hy, r):
    """Signed distance to a rounded rectangle (negative inside)."""
    qx = np.abs(xs - cx) - (hx - r)
    qy = np.abs(ys - cy) - (hy - r)
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    inside = np.minimum(np.maximum(qx, qy), 0.0)
    return outside + inside - r


def _normalize3(vec):
    v = np.asarray(vec, np.float32)
    return v / np.linalg.norm(v)


def _hue_to_rgb(h):
    """Vectorised fully-saturated HSV→RGB (value = 1). ``h`` in [0, 1)."""
    h6 = (h % 1.0) * 6.0
    i = np.floor(h6).astype(np.int32) % 6
    f = (h6 - np.floor(h6)).astype(np.float32)
    q = 1.0 - f
    conds = [i == 0, i == 1, i == 2, i == 3, i == 4, i == 5]
    rgb = np.empty(h.shape + (3,), np.float32)
    rgb[..., 0] = np.select(conds, [1.0, q, 0.0, 0.0, f, 1.0])
    rgb[..., 1] = np.select(conds, [f, 1.0, 1.0, q, 0.0, 0.0])
    rgb[..., 2] = np.select(conds, [0.0, 0.0, f, 1.0, 1.0, q])
    return rgb


def _box_radius_for_sigma(sigma: float, passes: int = 3) -> int:
    """Box-blur radius that, repeated ``passes`` times, approximates a Gaussian.

    ``passes`` boxes of half-width ``r`` have total variance ``passes·(r²+r)/3``;
    solving for the radius that matches a target std ``sigma`` (with 3 passes →
    variance ``r²+r``) gives the closed form below. Returns 0 when there's nothing
    to blur, so callers can skip the work entirely.
    """
    if sigma is None or sigma <= 0.0:
        return 0
    var = sigma * sigma * 3.0 / passes
    r = int(round((-1.0 + np.sqrt(1.0 + 4.0 * var)) / 2.0))
    return max(0, r)


def _box_blur(img: np.ndarray, radius: int, passes: int = 3) -> np.ndarray:
    """Separable box blur of a 2-D float array, ``passes`` times (≈ Gaussian).

    Uses cumulative sums so each pass is O(N) regardless of radius — fast enough
    to run every drag frame. Edges are replicated (``mode="edge"``) so the rim
    stays bright instead of darkening toward black.
    """
    if radius < 1:
        return img
    k = 2 * radius + 1
    inv = np.float32(1.0 / k)
    for _ in range(passes):
        ap = np.pad(img, ((0, 0), (radius + 1, radius)), mode="edge")
        cs = np.cumsum(ap, axis=1, dtype=np.float32)
        img = (cs[:, k:] - cs[:, :-k]) * inv
        ap = np.pad(img, ((radius + 1, radius), (0, 0)), mode="edge")
        cs = np.cumsum(ap, axis=0, dtype=np.float32)
        img = (cs[k:, :] - cs[:-k, :]) * inv
    return img


def _environment(rx, ry, rz):
    """Procedural environment sampled along reflection vector R = (rx, ry, rz).

    A faint horizon-biased ambient plus a warm key light from the top and a cool
    fill from the lower-left. Returns an (H, W, 3) float array in 0..255.
    """
    key_dir = _normalize3((0.0, -0.85, 0.52))   # top, slightly toward eye
    fill_dir = _normalize3((-0.62, 0.55, 0.56))  # lower-left

    dot_key = np.clip(rx * key_dir[0] + ry * key_dir[1] + rz * key_dir[2], 0, 1)
    dot_fill = np.clip(rx * fill_dir[0] + ry * fill_dir[1] + rz * fill_dir[2], 0, 1)
    spec_key = dot_key ** 55
    spec_fill = dot_fill ** 22

    horizon = np.clip(1.0 - np.clip(rz, 0, 1), 0, 1)  # 1 at the rim, 0 at centre

    refl = np.empty(rx.shape + (3,), np.float32)
    refl[..., 0] = horizon * 120 + spec_key * 255 + spec_fill * 150
    refl[..., 1] = horizon * 140 + spec_key * 252 + spec_fill * 195
    refl[..., 2] = horizon * 175 + spec_key * 245 + spec_fill * 255
    return refl


class GlassKernel:
    """Precomputed glass response for a fixed panel size, bevel and IOR.

    Everything that depends only on the *geometry* of the slab — the surface
    normals, the per-channel bilinear sample coordinates, the Fresnel weight and
    the reflected environment — is computed once here. :meth:`apply` then turns a
    moving backdrop slice into the finished panel with just a few gathers, which
    keeps dragging smooth.
    """

    def __init__(
        self,
        panel_w: int,
        panel_h: int,
        pad: int,
        radius: float,
        *,
        bevel: float,
        strength: float,
        ior_edge: float,
        ior_inner: float,
        chroma: float,
        reflect: float,
        f0: float,
        disp_glow: float = 0.0,
        disp_sat: float = 0.55,
        disp_cycles: float = 1.0,
        disp_phase: float = 0.0,
        disp_width: float = 3.0,
        blur_sigma: float = 0.0,
        haze: float = 0.0,
    ):
        self.h = panel_h
        self.w = panel_w
        self.pw_pad = panel_w + 2 * pad
        self.ph_pad = panel_h + 2 * pad

        ys, xs = np.mgrid[0:panel_h, 0:panel_w].astype(np.float32)
        px = xs + pad
        py = ys + pad

        cx = pad + panel_w / 2.0
        cy = pad + panel_h / 2.0
        sdf = _rounded_rect_sdf(px, py, cx, cy, panel_w / 2.0, panel_h / 2.0, radius)
        d = -sdf                       # distance to the rim, positive inside
        inside = d > 0

        gy, gx = np.gradient(sdf)      # outward in-plane normal direction
        gnorm = np.hypot(gx, gy) + 1e-6
        nx = gx / gnorm
        ny = gy / gnorm

        # Quarter-circle roundover: slope = tanθ, blowing up toward the rim.
        t = np.clip(d / bevel, 0.0, 1.0)            # 0 at rim, 1 at inner edge
        xx = np.where(inside, np.clip(1.0 - t, 0.0, 0.985), 0.0)
        slope = xx / np.sqrt(1.0 - xx * xx)         # tanθ  (0 .. ~5.7)
        cos_t = 1.0 / np.sqrt(1.0 + slope * slope)
        sin_t = slope * cos_t

        # Per-channel refraction via Snell's law through the curved roundover.
        # The incident ray is vertical; we refract it at the tilted surface and
        # project the refracted ray onto the background plane `strength` (the
        # glass thickness) below. The 1/(-T_z) projection is strongly nonlinear
        # toward the grazing rim, so the background bends into a curved lens-wrap
        # rather than a straight directional shift. Each channel uses a slightly
        # different IOR, so the colours decompose along the border.
        n_base = ior_edge + (ior_inner - ior_edge) * t
        max_disp = 0.92 * pad
        chroma_factor = (-1.0, 0.0, 1.0)            # R bends least, B most
        self._idx = []
        for c in range(3):
            n_c = n_base * (1.0 + chroma * chroma_factor[c])
            eta = 1.0 / n_c                                  # air → glass
            cos_r = np.sqrt(np.clip(1.0 - eta * eta * sin_t * sin_t, 0.0, 1.0))
            coeff = eta * cos_t - cos_r                      # T = eta*I + coeff*N
            tz = eta - coeff * cos_t                         # = -T_z  (> 0, down)
            scale = strength / np.maximum(tz, 1e-3)          # project to plane
            dx = np.clip(coeff * nx * sin_t * scale, -max_disp, max_disp)
            dy = np.clip(coeff * ny * sin_t * scale, -max_disp, max_disp)
            self._idx.append(self._precompute_sample(px + dx, py + dy))

        # Fresnel weight and reflected environment (static).
        cosv = np.clip(cos_t, 0.0, 1.0)
        fres = f0 + (1.0 - f0) * (1.0 - cosv) ** 5
        fres = np.where(inside, fres, 0.0) * reflect

        rx = 2.0 * cos_t * (nx * sin_t)
        ry = 2.0 * cos_t * (ny * sin_t)
        rz = 2.0 * cos_t * cos_t - 1.0
        refl = _environment(rx, ry, rz)

        f = fres[..., None].astype(np.float32)
        self._one_minus_f = (1.0 - f)
        self._refl_term = (refl * f).astype(np.float32)

        # Iridescent dispersion glow: a *lightened* spectral colour whose hue
        # varies around the border, drawn as a thin sharp line right at the rim
        # (falloff over `disp_width` px), so each portion of every border carries
        # its own pale dispersed colour.
        ang = np.arctan2(ny, nx)                         # direction around rim
        hue = (ang / (2.0 * np.pi) + 0.5) * disp_cycles + disp_phase
        spectral = _hue_to_rgb(hue)                      # vivid spectrum
        light = spectral * disp_sat + (1.0 - disp_sat)   # mix to white → pastel
        line = np.where(inside, np.clip(1.0 - d / disp_width, 0.0, 1.0), 0.0)
        self._disp_glow = (light * (line * disp_glow)[..., None]).astype(np.float32)

        # Frost: a rough surface scatters transmitted light. We approximate that
        # scatter as a Gaussian blur of the transmitted background (3 box passes)
        # plus a faint milky veil from multiple scattering. Both are fixed per
        # kernel; apply() skips them entirely when the glass is polished.
        self._blur_radius = _box_radius_for_sigma(blur_sigma)
        haze = float(np.clip(haze, 0.0, 1.0))
        self._haze = haze if haze >= 1e-4 else 0.0   # below this it's imperceptible — skip the work

    def _precompute_sample(self, sx, sy):
        """Cache clamped integer corners + fractional weights for a sample grid.

        Indices are clamped to the (fixed) padded size now, so :meth:`apply` does
        no per-frame bounds work.
        """
        x0 = np.floor(sx).astype(np.int32)
        y0 = np.floor(sy).astype(np.int32)
        fx = (sx - x0).astype(np.float32)
        fy = (sy - y0).astype(np.float32)
        return {
            "x0": np.clip(x0, 0, self.pw_pad - 1),
            "x1": np.clip(x0 + 1, 0, self.pw_pad - 1),
            "y0": np.clip(y0, 0, self.ph_pad - 1),
            "y1": np.clip(y0 + 1, 0, self.ph_pad - 1),
            "fx": fx,
            "omfx": 1.0 - fx,
            "fy": fy,
            "omfy": 1.0 - fy,
        }

    def apply(self, padded: np.ndarray, scatter: bool = True) -> np.ndarray:
        """Refract+reflect a backdrop slice (must match the kernel's padded size).

        ``scatter`` toggles the frost terms (transmission blur + milky haze).
        They're the only per-frame-expensive work here, so callers pass
        ``scatter=False`` during an active drag for a sharp, cheap preview and
        ``scatter=True`` on settle for the full frosted result. No-op when the
        glass is polished (blur radius 0 and haze 0).
        """
        out = np.empty((self.h, self.w, 4), np.float32)
        for c in range(3):
            d = self._idx[c]
            pc = padded[..., c]                          # uint8 view, gathered below
            top = pc[d["y0"], d["x0"]] * d["omfx"] + pc[d["y0"], d["x1"]] * d["fx"]
            bot = pc[d["y1"], d["x0"]] * d["omfx"] + pc[d["y1"], d["x1"]] * d["fx"]
            out[..., c] = top * d["omfy"] + bot * d["fy"]

        trans = out[..., :3]                             # transmitted background
        if scatter and self._blur_radius:                # frosted: scatter → blur
            for c in range(3):
                trans[..., c] = _box_blur(trans[..., c], self._blur_radius)
        if scatter and self._haze:                       # milky multiple-scatter veil
            lum = trans.mean(axis=2, keepdims=True)
            milky = 0.5 * lum + 0.5 * 255.0              # desaturate + lift to white
            trans *= (1.0 - self._haze)
            trans += milky * self._haze

        out[..., :3] = trans * self._one_minus_f + self._refl_term
        out[..., :3] += self._disp_glow        # additive iridescent rim
        np.clip(out[..., :3], 0.0, 255.0, out[..., :3])
        out[..., 3] = 255.0
        return out.astype(np.uint8)


def compute_glass(padded: np.ndarray, panel_w, panel_h, pad, radius, **params):
    """One-shot convenience: build a kernel and apply it to ``padded``."""
    return GlassKernel(panel_w, panel_h, pad, radius, **params).apply(padded)


# ----------------------------------------------------------------- material dials
def _anchored(t: float, lo: float, hi: float) -> float:
    """Dial multiplier: ``lo`` at t=0, exactly 1.0 at t=0.5, ``hi`` at t=1.

    Piecewise-linear through the three anchors, so the neutral dial position
    (0.5) reproduces the baseline constant *exactly* with no float drift, and
    every parameter stays monotonic across the dial.
    """
    t = min(max(t, 0.0), 1.0)
    if t >= 0.5:
        return 1.0 + (t - 0.5) * 2.0 * (hi - 1.0)
    return 1.0 + (t - 0.5) * 2.0 * (1.0 - lo)


@dataclass(frozen=True)
class GlassMaterial:
    """Two perceptual dials layered over the low-level :class:`GlassKernel`.

    Rather than expose a dozen physical constants, the look is driven by two
    coherent material axes — each a single scalar in ``[0, 1]`` that re-derives
    several kernel parameters at once so the pane always reads as one piece of
    glass:

    * ``thickness`` — perceived slab depth / mass, standing in for the optical
      path length *T*. A thicker slab displaces the background more
      (``strength``), is ground to a wider rounded edge (``bevel``), bends light
      through a steeper index ramp (the ``ior_inner``→``ior_edge`` span),
      decomposes colour more along the longer dispersive path (``chroma``, locked
      to ``strength``) and carries a fatter spectral rim line (``disp_width``,
      locked to ``bevel``). The captured margin (``pad``) grows with it so the
      lens-wrap never hits the displacement clamp. Neutral **0.5** == baseline.
    * ``frost`` — surface roughness (a GGX-like microfacet slope). A rough face
      scatters transmitted light into a cone that projects to a spatial blur of
      the background (``blur_sigma``, eased in and thrown wider through a thicker
      slab), adds a milky multiple-scatter veil (``haze``) and scrambles the
      crisp dispersion line (dims ``disp_glow``). It never moves the mean
      refracted ray, so frost **0** == the sharp baseline, byte-for-byte.

    The baseline fields default to the current tuned look, so ``GlassMaterial()``
    *is* the present appearance and the dials scale outward from there.
    """

    # --- the two dials --------------------------------------------------
    thickness: float = 0.5     # 0 wafer · 0.5 baseline · 1 hand-poured block
    frost: float = 0.0         # 0 polished · 1 ground / milk glass

    # --- baseline look (the neutral point: thickness 0.5, frost 0) ------
    strength: float = 42.0
    bevel: float = 56.0
    pad: float = 130.0
    ior_edge: float = 5.0
    ior_inner: float = 1.5
    chroma: float = 0.11
    disp_glow: float = 150.0
    disp_sat: float = 0.55
    disp_cycles: float = 1.0
    disp_width: float = 2.5
    reflect: float = 1.0
    f0: float = 0.035
    max_frost_sigma: float = 8.0   # logical-px transmission blur sigma at frost 1

    # --- derived --------------------------------------------------------
    def _pad_mult(self) -> float:
        # Grow the captured margin only *above* neutral: a thicker slab displaces
        # more and would otherwise slam into the max_disp = 0.92*pad clamp and go
        # flat. Thin glass displaces less and needs no extra margin. Matched to
        # ``strength``'s 1.6× high so the clamp ceiling rises in lockstep.
        t = min(max(self.thickness, 0.0), 1.0)
        return 1.0 if t <= 0.5 else 1.0 + 1.2 * (t - 0.5)

    def pad_px(self, dpr: float) -> int:
        """Capture margin in device px. Callers MUST use this for both the kernel
        ``pad`` and the backdrop-slice extents so the gather reads the right
        region — ``pad`` feeds the clamp, the padded buffer size and the slice."""
        return int(self.pad * self._pad_mult() * dpr)   # truncate (matches legacy int(PAD*dpr))

    def build_kernel(
        self, panel_w_px: int, panel_h_px: int, radius_px: float, dpr: float
    ) -> "GlassKernel":
        """Resolve the two dials into a fully-built :class:`GlassKernel`."""
        t, f = self.thickness, self.frost

        strength = self.strength * _anchored(t, 0.5, 1.6)
        bevel = self.bevel * _anchored(t, 0.6, 1.5)
        chroma = self.chroma * _anchored(t, 0.5, 1.6)        # locked to strength (path length)
        disp_width = self.disp_width * _anchored(t, 0.6, 1.5)  # locked to bevel (edge width)

        # IOR ramp driven through the *span* with the inner index as anchor, so
        # the flat-interior 1:1 region stays honest and the inner index never
        # creeps up toward the edge and collapses the gradient.
        ior_inner = self.ior_inner * _anchored(t, 0.83, 1.13)
        ior_edge = ior_inner + (self.ior_edge - self.ior_inner) * _anchored(t, 0.45, 1.55)

        # Frost is purely transmission-side: blur (eased in via f**1.5; the
        # scatter cone is thrown through depth T so it widens ~±15% with
        # thickness), a milky veil, and a softened spectral line. All vanish at
        # f == 0 so the polished look is preserved exactly.
        blur_sigma = (f ** 1.5) * self.max_frost_sigma * (0.85 + 0.3 * t) * dpr
        haze = f * 0.18
        disp_glow = self.disp_glow * (1.0 - 0.4 * f)

        return GlassKernel(
            panel_w_px,
            panel_h_px,
            self.pad_px(dpr),
            radius_px,
            bevel=bevel * dpr,
            strength=strength * dpr,
            ior_edge=ior_edge,
            ior_inner=ior_inner,
            chroma=chroma,
            reflect=self.reflect,
            f0=self.f0,
            disp_glow=disp_glow,
            disp_sat=self.disp_sat,
            disp_cycles=self.disp_cycles,
            disp_width=disp_width * dpr,
            blur_sigma=blur_sigma,
            haze=haze,
        )
