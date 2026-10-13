"""
Health classification model loading — mirrors detection.py's pattern
(stub default, lazy real-model import, single get_classifier() entry
point), but the real loader below is a genuine from-scratch-trained model,
not an adapted object detector.

Update — real ResNet-18 classifier trained (supersedes the prior "adapt a
Roboflow YOLO detector" entry in this file's history — see BUILD_STATUS.md's
matching update): the project owner trained
`trained_models/oil_palm_health_classifier.pth` directly, using the sibling
`palm-vision-ml` project's `tree_health_pipeline`, on the full 352,551-box
Roboflow "Oil-Palm-Health" dataset (CC BY 4.0) with a tile-ID-grouped 80/20
spatial split (no image or its augmented duplicate in both train and val)
and class-weighted loss for the dataset's ~413:1 class imbalance. Reported
validation metrics (18 epochs, checked directly against the checkpoint's own
embedded `validation_metrics`, not just the training log): accuracy 0.957,
macro F1 0.806 — a real, if imperfect, improvement over the 0.288
accuracy / 0.089 macro-F1 majority-class baseline for this same split, with
per-class F1 highest on HEALTHY (0.974) and weakest on YELLOW (0.658). These
are the model's own validation numbers on data drawn from the same source
dataset — not yet confirmed against this project's own farm imagery (see
"Not verified" note in BUILD_STATUS.md's matching update).

**This is a genuinely different input contract from every other model this
project has loaded so far**, and every piece of it matters for correctness,
not just performance:
  - 10 input channels, not 3: RGB + 7 RGB-derived vegetation indices
    (VARI, GLI, ExG, ExGR, NGRDI, MGRVI, RGBVI — see vegetation_indices.py,
    a faithful port of the training pipeline's own module, confirmed
    bit-for-bit equivalent on real data before being wired in here) stacked
    in that exact order. Feeding this model plain 3-channel RGB would not
    just reduce accuracy, it would silently corrupt every prediction — the
    first conv layer's weights literally have shape (64, 10, 7, 7), not
    (64, 3, 7, 7).
  - A fixed 96x96 patch size (the checkpoint's own `patch_size` field is
    trusted over the constant below — see CLASSIFICATION_PATCH_SIZE_PX's
    own docstring).
  - RGB channels normalized by dividing by 255.0 (uint8 assumed); VI
    channels are raw index values with NaN/Inf replaced by 0.0 — no
    z-score/ImageNet normalization on either.
  - ResNet-18 with its first conv layer surgically widened from 3 to 10
    input channels (`_build_health_model()` below) — this project's own
    port of `palm-vision-ml/tree_health_pipeline/health_classification.py`'s
    `build_model()`, loaded with `pretrained=False` deliberately: the
    checkpoint's own state_dict overwrites every weight anyway (including
    conv1's), so downloading ImageNet-pretrained weights first would only
    cost a network round-trip for nothing this project's local-first stance
    should pay — see CLAUDE.md's non-negotiable ground rule.

**5-class model, 3-class app schema — a deliberate, confirmed product
decision, not an oversight.** The checkpoint classifies into 5 classes
(HEALTHY, SMALL, YELLOW, DEAD, GRASS — the real distribution measured
directly from the source dataset's own annotations), but this project's
`Tree.current_health`/`TreeHealthClassification.HEALTH_CHOICES` schema only
keeps three stored values (healthy/unknown/yellow), per docs/06-ml-pipeline.md's
original scheme.

Update — SMALL retired as its own app-level label, asked directly: SMALL
predictions now map to `unknown` rather than being kept as a distinct
"small" health value — `HEALTH_SMALL` is gone entirely from both
`Tree.HEALTH_CHOICES` and `TreeHealthClassification.HEALTH_CHOICES` (see
each model's own migration for the one-time data relabel of any
already-stored "small" rows). DEAD/GRASS are unaffected by this — still
routed to human review exactly like an unmapped class from the old
Roboflow adapter, never silently forced into one of the three real labels,
never dropped. `_HEALTH_LABEL_MAP` below stays a *partial* map for that
same DEAD/GRASS reason: those two have no entry, so `classifier()` falls
through to the same "no usable prediction" branch `stub_classifier()`
already documents — SMALL is different, it's an explicit, confident
mapping to `unknown`, not a fall-through (the model's real confidence score
is preserved, only the label changes). Revisit this mapping (and the
3-class schema itself) if dead-palm tracking is ever wanted as first-class
data — the model already produces that signal, it's just not surfaced yet.

Update — opt-in exposure/color normalization, ported from the ml-repo's own
fix for a real bug found against THIS project's own imagery. The project
owner tested the trained model against a real Jonggol farm orthomosaic
(Pix4D-processed) and found every crop pushed to a spurious, scale-invariant
"SMALL" verdict at 100% confidence regardless of crop framing — traced to a
genuine per-channel color-balance mismatch between that orthomosaic's
pixel intensities (~130-150 mean RGB per crop) and what this checkpoint's
conv1/BatchNorm layers were trained to expect (~79-106, this dataset's own
training-crop reference), not a real health signal. `normalize_exposure()`
below (a faithful port of `health_classification.normalize_exposure()`,
confirmed to produce the same output as the source function on the same
input before being wired in here — same verification standard as
vegetation_indices.py's own port) corrects this with a per-channel
multiplicative gain toward that reference, clipped to avoid manufacturing
color out of a near-black/near-white crop.

**Deliberately opt-in, not applied unconditionally** — the ml-repo's own
testing against 162 real, in-distribution validation crops found that
applying this to every input measurably REGRESSES accuracy (97.5% ->
91.4%, worst on SMALL: 100% -> 84.8%): different health classes have
genuinely different characteristic brightness in the training data (e.g.
dense healthy canopy self-shadows darker than a sparser small/young
crown), so forcing every crop toward one global target erases real signal,
not just camera noise. Gated behind `settings.CLASSIFICATION_NORMALIZE_EXPOSURE`
(default `False`), matching `infer_health.HealthClassifier`'s own
constructor flag default. Enabled in this project's own `.env` — this
deployment's real imagery (Jonggol, Pix4D-processed) is the exact,
independently-verified case the ml-repo's own docstring says to turn it on
for, not a blanket default a different deployment should inherit
unverified.

A classifier is any callable: (crop: PIL.Image) -> (label: str, confidence: float).
"""
from django.conf import settings

HEALTH_LABELS = ["healthy", "yellow"]

# palm-vision-ml/tree_health_pipeline/health_classification.py's
# REFERENCE_MEAN_RGB, computed once from 1,762 randomly-sampled 96x96 crops
# spanning the full health-condition TRAIN split — the brightness/color-
# balance level this checkpoint's conv1/BatchNorm layers actually learned
# to expect. A plain tuple, not a numpy array, at module level — this
# module must stay importable without numpy installed (same reasoning as
# torch/PIL being imported lazily inside _load_real_classifier() below,
# not at module level); normalize_exposure() converts it when actually
# used. Recompute this (in the ml-repo) if the model is ever retrained on
# a meaningfully different image source.
REFERENCE_MEAN_RGB = (91.0, 106.4, 79.3)

# Bounds how aggressively a single crop's exposure can be corrected —
# without this, a crop that's already near-black or near-white (deep
# shadow, or a blown-out glare patch) would get an extreme gain that
# manufactures color information out of near-zero signal, rather than
# leaving it as the uninformative crop it actually is.
_EXPOSURE_GAIN_CLIP = (0.3, 3.0)


def normalize_exposure(rgb, reference_mean=REFERENCE_MEAN_RGB):
    """Corrects a per-crop, per-channel brightness/color-balance mismatch
    toward the training distribution's reference level, via a simple
    multiplicative gain per channel (gain = reference_mean / this crop's
    mean, clipped to `_EXPOSURE_GAIN_CLIP`). Expects and returns an
    (H, W, 3) uint8 RGB array.

    NOT SAFE TO APPLY UNCONDITIONALLY — see this module's own docstring's
    "Update" section for the full reasoning and the real bug this was
    found fixing. Only reached when `_load_real_classifier()`'s
    `normalize_exposure` is truthy.
    """
    import numpy as np

    if rgb.dtype != np.uint8:
        raise ValueError(f"Expected uint8 RGB input, got dtype {rgb.dtype}")
    reference = np.asarray(reference_mean, dtype=np.float64)
    src_mean = rgb.reshape(-1, 3).astype(np.float64).mean(axis=0)
    gain = np.clip(reference / np.maximum(src_mean, 1.0), *_EXPOSURE_GAIN_CLIP)
    return np.clip(rgb.astype(np.float32) * gain, 0, 255).astype(np.uint8)

# The checkpoint's own patch_size (currently 96) is always trusted at load
# time over this constant — `_load_real_classifier()` reads it from the
# checkpoint and resizes any incoming crop to match, so a future retrained
# checkpoint with a different patch size keeps working correctly even if
# this constant drifts. This is only used by apps/imagery/services.py to
# size the *initial* crop extracted around each detection, so it's close to
# the model's native resolution rather than needlessly small/large before
# the (lossy) resize step.
CLASSIFICATION_PATCH_SIZE_PX = 96

# Ground-sample-distance normalization for the classification patch crop —
# the classification-side counterpart to detection.py's own
# DETECTION_TARGET_GSD_M/_native_gsd_m() fix (see that module's docstring
# for the original diagnosis: this project's real Jonggol orthomosaic is
# 1.843cm/pixel, far finer than what the training data implied).
#
# Found the SAME way, after the exposure-normalization fix above still
# left every classification patch looking like extreme close-up leaf/frond
# texture, not a recognizable crown — reported directly via a screenshot of
# the review queue. Root cause: apps/imagery/services.py's crop_patch() has
# always cropped a *fixed pixel count* (CLASSIFICATION_PATCH_SIZE_PX)
# directly out of the clipped array at whatever its NATIVE resolution
# happens to be — unlike detection, which resamples its own local copy of
# the array before tiling, classification never got that treatment, so a
# 96-native-pixel crop at Jonggol's 1.843cm/px covers only ~1.77m of real
# ground.
#
# Implied training GSD computed the same way DETECTION_TARGET_GSD_M was:
# palm-vision-ml/00-dataset/health-condition/{train,valid,test}/
# _annotations.csv (352,551 boxes total — matches this module's own
# provenance writeup above, confirming this is the right dataset) has a
# median crown bounding box of ~43x43px; for a plausible real oil-palm
# crown diameter (4-9m, same range used for the detection dataset), that
# implies training imagery at roughly 9.3-20.9cm/pixel — coarser than
# detection's own 4-9cm/pixel implied range, and closer to 2x coarser than
# that, so this needed its own separate target, not a reused constant.
# 15cm/pixel is roughly the middle of that range — a reasoned estimate,
# explicitly not a calibrated value, same caveat DETECTION_TARGET_GSD_M's
# own docstring already carries.
CLASSIFICATION_TARGET_GSD_M = 0.15

# Same reasoning and same bound as detection.py's own
# _GSD_RESAMPLE_MAX_FACTOR — a defensive clamp against a wildly wrong GSD
# estimate (bad/missing raster metadata) blowing the native crop window up
# to an unreasonable pixel count, not just a performance nicety.
_CLASSIFICATION_GSD_RESAMPLE_MAX_FACTOR = 10.0


def classification_native_patch_size_px(
    patch_size_px, native_gsd_m, target_gsd_m=CLASSIFICATION_TARGET_GSD_M
):
    """How many NATIVE pixels crop_patch() should crop so that, once
    resized down to `patch_size_px` (the checkpoint's own input size, via
    `_load_real_classifier()`'s existing "resize a crop that doesn't match
    patch_size" step — no separate resize needed here), the patch covers
    roughly the same real-world ground the classifier's training crops did.
    Returns `patch_size_px` unchanged if `native_gsd_m`/`target_gsd_m` is
    falsy (no GSD info available — e.g. a raster with no CRS) rather than
    guessing.
    """
    if not target_gsd_m or not native_gsd_m:
        return patch_size_px
    resample_factor = target_gsd_m / native_gsd_m
    resample_factor = max(
        1.0 / _CLASSIFICATION_GSD_RESAMPLE_MAX_FACTOR,
        min(_CLASSIFICATION_GSD_RESAMPLE_MAX_FACTOR, resample_factor),
    )
    return max(1, round(patch_size_px * resample_factor))


# palm-vision-ml/tree_health_pipeline/health_classification.py's HealthClass
# enum, lowercased, mapped to this project's own three stored labels.
# "small" maps to "unknown" (see this module's docstring's "Update" section)
# — an explicit mapping, not a fall-through, so the model's real confidence
# is kept. "dead" and "grass" are intentionally absent so callers fall
# through to the "unmapped class" branch in _load_real_classifier() below,
# per this module's own docstring and the product decision it documents.
_HEALTH_LABEL_MAP = {
    "healthy": "healthy",
    "small": "unknown",
    "yellow": "yellow",
}


def stub_classifier(crop):
    """Deliberately NOT a random/fixed-label guess presented as a real
    prediction — returns the lowest-confidence-looking answer (0.0) so
    downstream code that filters on a confidence threshold
    (docs/06-ml-pipeline.md step 4: "results below a confidence threshold
    ... surface in the review queue rather than being auto-accepted")
    correctly routes every stub result to human review instead of treating
    it as a real automated classification.
    """
    return "unknown", 0.0


def _build_health_model(num_classes, in_channels):
    """Port of palm-vision-ml/tree_health_pipeline/health_classification.py's
    build_model(), with `pretrained=False` always — see this module's
    docstring for why: the checkpoint's own state_dict overwrites every
    weight, including conv1's, so there is nothing to gain from an
    ImageNet-weights download and a real local-first cost to paying for one.
    """
    from torch import nn
    import torchvision

    model = torchvision.models.resnet18(weights=None)
    old_conv = model.conv1
    model.conv1 = nn.Conv2d(
        in_channels=in_channels,
        out_channels=old_conv.out_channels,
        kernel_size=old_conv.kernel_size,
        stride=old_conv.stride,
        padding=old_conv.padding,
        bias=old_conv.bias is not None,
    )
    model.fc = nn.Linear(model.fc.in_features, num_classes)
    return model


def _load_real_classifier(model_path, normalize_exposure_enabled=None):
    """Loads the real trained ResNet-18-10ch checkpoint — see this module's
    docstring for the full input contract. `torch`/`torchvision` are
    imported lazily inside this function, not at module level — same
    reasoning detection.py's module docstring documents for ultralytics:
    importing this module (every Celery worker boot, every test run) must
    not require torch to be installed just because CLASSIFICATION_MODEL_PATH
    happens to be unset, which is the expected state for local dev without a
    trained model.

    `normalize_exposure_enabled`: whether to apply this module's
    `normalize_exposure()` to each crop before computing vegetation indices
    — see this module's own docstring's "Update" section for the real bug
    this fixes and why it's opt-in, not automatic. `None` (the default for
    every real caller — see get_classifier()) falls back to
    `settings.CLASSIFICATION_NORMALIZE_EXPOSURE`; the parameter exists
    mainly so tests can force either branch directly, same pattern
    detection.py's `target_gsd_m` already uses.
    """
    import numpy as np
    import torch
    from PIL import Image as PILImage

    from .vegetation_indices import compute_indices

    if normalize_exposure_enabled is None:
        normalize_exposure_enabled = getattr(settings, "CLASSIFICATION_NORMALIZE_EXPOSURE", False)

    # weights_only=True (the torch>=2.6 default, made explicit here rather
    # than relying on the installed version's default): torch.load() without
    # this restriction can in principle execute arbitrary code embedded in
    # the file (it's pickle-based). This checkpoint was produced by this
    # project's own training pipeline, not a downloaded third-party binary,
    # but there's no reason to relax the restriction for a checkpoint that's
    # plain tensors + JSON-shaped metadata anyway.
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)

    class_names = checkpoint["class_names"]
    patch_size = checkpoint["patch_size"]
    input_channels = checkpoint["input_channels"]

    model = _build_health_model(num_classes=len(class_names), in_channels=input_channels)
    missing, unexpected = model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    if missing or unexpected:
        # load_state_dict(strict=True) already raises on a real mismatch —
        # this is defensive, not the primary guard, kept for a clearer
        # error than a bare RuntimeError if that ever changes upstream.
        raise ValueError(f"Checkpoint/model architecture mismatch: missing={missing} unexpected={unexpected}")
    model.eval()

    def classifier(crop):
        rgb = np.array(crop.convert("RGB"))
        if rgb.shape[0] != patch_size or rgb.shape[1] != patch_size:
            # Resize (not re-crop) to the checkpoint's own patch size —
            # matches palm-vision-ml/tree_health_pipeline/infer_health.py's
            # own "minimal inference entry point for deploying this
            # checkpoint in a website backend" reference implementation,
            # except using PIL instead of that script's cv2.resize(...,
            # INTER_AREA) — confirmed to agree on real inputs before this
            # swap (see BUILD_STATUS.md's matching update), and avoids
            # adding opencv as a new project dependency for one resize call.
            resized = PILImage.fromarray(rgb).resize((patch_size, patch_size), PILImage.Resampling.LANCZOS)
            rgb = np.array(resized)

        if normalize_exposure_enabled:
            # Matches infer_health.py's _prepare_tensor() ordering exactly:
            # after resize, before compute_indices() — the VI channels must
            # be computed from the color-corrected pixels, not the raw ones,
            # or the RGB and VI channels the model sees would disagree with
            # each other about which color-balance regime they're in.
            rgb = normalize_exposure(rgb)

        vi_stack = compute_indices(rgb).stack()
        vi_stack = np.nan_to_num(vi_stack, nan=0.0, posinf=0.0, neginf=0.0)
        rgb_float = rgb.astype(np.float32) / 255.0
        combined = np.concatenate([rgb_float, vi_stack.astype(np.float32)], axis=-1)
        tensor = torch.from_numpy(combined).permute(2, 0, 1).contiguous().unsqueeze(0)

        with torch.no_grad():
            probs = torch.softmax(model(tensor), dim=1).squeeze(0)

        pred_idx = int(probs.argmax())
        confidence = float(probs[pred_idx])
        source_class = class_names[pred_idx]

        label = _HEALTH_LABEL_MAP.get(source_class.lower())
        if label is None:
            # A real prediction, but not one of this project's three health
            # classes (DEAD/GRASS — see module docstring) — treated exactly
            # like no usable prediction at all rather than forced into the
            # closest-sounding real label. Stored as "unknown" (never
            # "healthy": a dead palm used to be saved as healthy at 0.0
            # confidence and colour the map green).
            return "unknown", 0.0

        return label, confidence

    return classifier


def get_classifier(normalize_exposure_enabled=None):
    if not settings.CLASSIFICATION_MODEL_PATH:
        return stub_classifier
    return _load_real_classifier(settings.CLASSIFICATION_MODEL_PATH, normalize_exposure_enabled=normalize_exposure_enabled)
