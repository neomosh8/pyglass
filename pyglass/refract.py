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

    def apply(self, padded: np.ndarray) -> np.ndarray:
        """Refract+reflect a backdrop slice (must match the kernel's padded size)."""
        out = np.empty((self.h, self.w, 4), np.float32)
        for c in range(3):
            d = self._idx[c]
            pc = padded[..., c]                          # uint8 view, gathered below
            top = pc[d["y0"], d["x0"]] * d["omfx"] + pc[d["y0"], d["x1"]] * d["fx"]
            bot = pc[d["y1"], d["x0"]] * d["omfx"] + pc[d["y1"], d["x1"]] * d["fx"]
            out[..., c] = top * d["omfy"] + bot * d["fy"]

        out[..., :3] = out[..., :3] * self._one_minus_f + self._refl_term
        out[..., :3] += self._disp_glow        # additive iridescent rim
        np.clip(out[..., :3], 0.0, 255.0, out[..., :3])
        out[..., 3] = 255.0
        return out.astype(np.uint8)


def compute_glass(padded: np.ndarray, panel_w, panel_h, pad, radius, **params):
    """One-shot convenience: build a kernel and apply it to ``padded``."""
    return GlassKernel(panel_w, panel_h, pad, radius, **params).apply(padded)
