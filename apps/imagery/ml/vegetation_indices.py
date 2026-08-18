"""
RGB-only greenness/vigor indices used as extra input channels for the real
health classifier (apps/imagery/ml/classification.py's `_load_real_classifier()`).

This is a faithful port of `vegetation_indices.py` from the sibling
`palm-vision-ml` project (the repo the health classifier was actually
trained in) — not a reimplementation from the formulas alone. Bit-for-bit
parity with the original was confirmed standalone (same inputs -> same
outputs, NaN-safe) before wiring this in, since the whole point of porting
it is that the model's first conv layer was trained on these exact 7
channels in this exact order; any drift here would silently corrupt every
prediction rather than raising an error.

CRITICAL DOMAIN CONSTRAINT, carried over from the source module: with
RGB-only sensors there is no near-infrared or red-edge band, so the
standard reflectance-based indices used in agricultural literature for
stress detection (NDVI, NDRE, EVI2, OSAVI, MCARI) are NOT computable here.
These seven are a weaker, visible-light proxy for greenness — engineered
features for the learned classifier below, not a stress diagnostic on
their own.

Formulas (as commonly cited in the precision-agriculture literature — the
source module's own docstring flags these as not independently re-derived
from the original papers, so spot-check the exact coefficients, e.g. the
1.4 constant in ExR, against your source of truth if this ever feeds a
regulatory deliverable):
  - VARI  (Gitelson et al., 2002)   : (G - R) / (G + R - B)
  - GLI   (Louhaichi et al., 2001)  : (2G - R - B) / (2G + R + B)
  - ExG   (Woebbecke et al., 1995)  : 2g - r - b   (normalized chromaticity)
  - ExGR  (Meyer & Neto, 2008)      : ExG - 1.4r + g  == ExG - ExR
  - NGRDI (Tucker, 1979 visible analogue): (G - R) / (G + R)
  - MGRVI (Bendig et al., 2015)     : (G^2 - R^2) / (G^2 + R^2)
  - RGBVI (Bendig et al., 2015)     : (G^2 - B*R) / (G^2 + B*R)
"""
from __future__ import annotations

import dataclasses
from typing import Optional

import numpy as np

_EPS = 1e-8  # avoids divide-by-zero on pure-black/saturated-white pixels

# NOT the same as _EPS. VARI/NGRDI/MGRVI/RGBVI are ratios whose denominator
# can pass arbitrarily close to zero without being exactly zero (e.g. VARI's
# denominator is near-zero on gray/neutral-toned pixels even when no channel
# is saturated or black) — a naive "denominator != 0" guard lets these
# through and produces individual-pixel blow-ups that dominate downstream
# aggregation. 0.05 (5% of full normalized scale) is the "too close to zero,
# treat as undefined" cutoff; the result is additionally clipped to
# +/-_RATIO_CLIP_BOUND as a defensive bound. Both are engineering safety
# nets carried over from the source module, not values from the original
# index papers.
_RATIO_DENOM_FLOOR = 0.05
_RATIO_CLIP_BOUND = 3.0


def _to_float01(rgb: np.ndarray) -> np.ndarray:
    """Normalize an (H, W, 3) array of arbitrary integer/float dtype to
    float64 in [0, 1] per-channel, using the dtype's theoretical max rather
    than the image's observed max — using the observed max would make
    indices non-comparable across crops with different actual brightness
    ranges.
    """
    if rgb.ndim != 3 or rgb.shape[-1] != 3:
        raise ValueError(f"Expected (H, W, 3) array, got shape {rgb.shape}")
    if np.issubdtype(rgb.dtype, np.floating):
        if rgb.max() > 1.0001:
            return rgb.astype(np.float64) / max(rgb.max(), _EPS)
        return rgb.astype(np.float64)
    info = np.iinfo(rgb.dtype)
    return rgb.astype(np.float64) / float(info.max)


@dataclasses.dataclass
class VegetationIndices:
    """Container for all computed per-pixel indices, each (H, W) float64."""

    vari: np.ndarray
    gli: np.ndarray
    exg: np.ndarray
    exgr: np.ndarray
    ngrdi: np.ndarray
    mgrvi: np.ndarray
    rgbvi: np.ndarray

    def stack(self, names: Optional[list[str]] = None) -> np.ndarray:
        """Stack requested indices into an (H, W, K) array, in the given
        order. Defaults to all seven, in the exact order the classifier's
        conv1 was trained on — changing this default without also
        retraining the model would silently feed it channels in the wrong
        order.
        """
        names = names or ["vari", "gli", "exg", "exgr", "ngrdi", "mgrvi", "rgbvi"]
        arrays = [getattr(self, n) for n in names]
        return np.stack(arrays, axis=-1)


def compute_indices(rgb: np.ndarray) -> VegetationIndices:
    """Compute all seven RGB-based indices from an (H, W, 3) RGB array of any
    common dtype (uint8/uint16/float). See _RATIO_DENOM_FLOOR/_RATIO_CLIP_BOUND
    above for the numerical-stability handling on the four ratio indices.
    """
    f = _to_float01(rgb)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]

    def _safe_div(numer: np.ndarray, denom: np.ndarray, denom_floor: float = _RATIO_DENOM_FLOOR) -> np.ndarray:
        out = np.full_like(numer, np.nan, dtype=np.float64)
        valid = np.abs(denom) > denom_floor
        out[valid] = numer[valid] / denom[valid]
        return np.clip(out, -_RATIO_CLIP_BOUND, _RATIO_CLIP_BOUND)

    vari = _safe_div(g - r, g + r - b)
    gli = _safe_div(2 * g - r - b, 2 * g + r + b)

    total = r + g + b
    total_safe = np.where(total > _EPS, total, np.nan)
    r_n, g_n, b_n = r / total_safe, g / total_safe, b / total_safe
    exg = 2 * g_n - r_n - b_n
    exr = 1.4 * r_n - g_n
    exgr = exg - exr

    ngrdi = _safe_div(g - r, g + r)
    mgrvi = _safe_div(g**2 - r**2, g**2 + r**2)
    rgbvi = _safe_div(g**2 - b * r, g**2 + b * r)

    return VegetationIndices(vari=vari, gli=gli, exg=exg, exgr=exgr, ngrdi=ngrdi, mgrvi=mgrvi, rgbvi=rgbvi)
