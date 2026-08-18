"""
Tree detection model loading. docs/02-build-plan.md Phase 5: "it's
acceptable to stub these with a trivial placeholder ... if no trained
model exists yet — the pipeline's correctness (clipping, matching, status
transitions) needs to be verifiable independent of having a
production-quality model."

Update — real Faster R-CNN crown detector trained (supersedes this file's
prior recommendation of a downloaded Roboflow YOLO model — see
BUILD_STATUS.md's matching update): the project owner trained a real
crown detector directly, via the sibling `palm-vision-ml` project's
`tree_health_pipeline` — a single-class ("oilpalm") Faster R-CNN
(ResNet50-FPN-v2), fine-tuned from `torchvision`'s detection API, on the
Roboflow "tree-detection" dataset (197,332 boxes). One fold of 5-fold
cross-validation actually ran (6 epochs; the checkpoint reflects the state
after epoch 5 — 5-fold CV was configured but `max_folds=1` stopped the run
after the first fold, so this is validated on one train/val split, not
full k-fold). Held-out metrics from that fold: detection precision 0.989,
recall 0.961 (mean per-image), tree-count MAE 6.11 vs. a naive-baseline MAE
of 11.10. These are the model's own held-out numbers from the source
dataset, not yet confirmed against this project's own farm imagery.

`torch`/`torchvision` imports are deliberately deferred inside
`_load_real_detector()`, not at module level — importing this module (which
happens on every Celery worker boot and every test run) must not require
torch to be installed just because DETECTION_MODEL_PATH happens to be
unset, which is the expected state for local dev without a trained model.

**Tiling is load-bearing here, not an optimization.** Faster R-CNN was
trained on tiles around 1024x1024px (the source dataset's own median image
size); a real block-clipped orthomosaic region can be far larger than
that. Handing the whole clipped array to the model directly would, at
best, get silently downsampled by the model's own internal resize
transform (destroying small-object detail — a palm crown a few dozen
pixels wide becomes a handful of pixels) and at worst reproduce the exact
failure already hit once during this model's own training: a CUDA
out-of-memory crash from running Faster R-CNN's region-proposal network
over one huge feature map (see BUILD_STATUS.md's matching update for the
real traceback this is based on, not a hypothetical). `_load_real_detector()`
below ports the sibling project's own tiling design (`ingestion.py`'s
`OrthomosaicLoader.tile_generator()`, `crown_detection.py`'s
`merge_detections_world_nms()`) for in-memory arrays: fixed-size
overlapping tiles, per-tile inference, then a world-coordinate NMS pass to
undo the double-counting that tile overlap would otherwise cause for any
crown straddling a tile seam.

**CRS reprojection — a real fix, not just new code.** A detector is any
callable: (clipped_array, transform, crs) -> list of (lon, lat, confidence)
tuples in TRUE EPSG:4326 geographic coordinates. `transform * (px, py)`
only ever produces a point in the raster's own NATIVE CRS (see
`clip_to_block()`'s own docstring) — for a UTM-projected orthomosaic
(common in practice; `georeference_image()` elsewhere in this app already
handles a real EPSG:32648 case), that's meters, not degrees. The previous
YOLO-based loader here fed that native-CRS point straight into the
`(lon, lat, confidence)` contract unreprojected — silently wrong for
anything not already in EPSG:4326, even though this function's own
docstring always promised "already converted to geographic coordinates."
Never exercised end-to-end before now (DETECTION_MODEL_PATH was unset the
entire time that code existed), but worth fixing here rather than carrying
forward into the first detector that actually runs — see
`_reproject_point_to_lonlat()` below.

**Ground-sample-distance normalization — a real, measured problem, not a
hypothetical.** Reported directly, with real detections attached: against
this project's own farm imagery, the detector was firing many confident
boxes on individual palm FRONDS, not whole crowns. Root-caused, not
guessed: the training dataset's own annotations
(`00-dataset/tree-detection/train/_annotations.csv`) have a median
crown box of ~100x100px; for any realistic oil-palm crown diameter
(4-9m), that implies the training imagery was captured at roughly
4-9cm/pixel. A real uploaded orthomosaic checked directly (rasterio
`transform`) came in at 1.843cm/pixel — 2-5x finer than what the detector
was trained to recognize a crown's scale at. Faster R-CNN's anchors and
FPN levels are tuned to the object-size distribution they were trained
on; handed a crown 2-5x larger in pixels than expected, the model doesn't
fail to fire, it fires *repeatedly* on locally-crown-shaped sub-regions of
one real crown — exactly "detects the leaf, not the tree."

Fixed by resampling the clipped array to approximately the training
data's implied ground sample distance *before* tiling — see
`_native_gsd_m()` and `_load_real_detector()`'s own docstring for where
this happens. `DETECTION_TARGET_GSD_M` (6cm/pixel, the middle of the
4-9cm range above) is a starting estimate, not a calibrated value — asked
directly before implementing this (three options: pick a default and
apply it now, add the setting without a default, or leave detection
alone and flag the finding for later): the project owner chose to apply
a default now. Configurable per-block, same override-with-global-default
pattern as `crown_detection_iou_threshold`, since actual crown size (and
therefore the right target GSD) genuinely varies by planting age/density.
The real, durable fix remains retraining/fine-tuning on this project's
own higher-resolution imagery once there's confirmed ground truth to
train on — out of scope here (a data/ML-training task, not a code one,
per CLAUDE.md's own "model retraining is the project owner's call, not
this tooling's job" boundary) — resampling is a mitigation, not a
replacement for that.
"""
from django.conf import settings


def stub_detector(clipped_array, transform, crs):
    """Returns no detections. This is a deliberate, honest default — a
    detector that invents fake tree positions would be worse than one that
    admits it has nothing to offer yet. Tests that need to exercise the
    matching/status-transition logic downstream of detection should inject
    a fake detector directly (see apps/imagery/tests.py), not rely on this
    stub returning anything.
    """
    return []


# Tiling/scoring parameters — ported from palm-vision-ml/tree_health_pipeline/
# ingestion.py's OrthomosaicLoader.tile_generator() defaults (tile_size=1024,
# overlap=128) and crown_detection.py's TreeCrownDetector/merge_detections_
# world_nms() defaults (score_threshold=0.5, iou_threshold=0.3) — kept in
# sync with the training pipeline's own values deliberately, not re-tuned
# here, since inference-time behavior should match what the model was
# actually validated against until there's real farm-imagery evidence to
# tune from.
DETECTION_TILE_SIZE_PX = 1024
DETECTION_TILE_OVERLAP_PX = 128
DETECTION_SCORE_THRESHOLD = 0.5
DETECTION_TILE_NMS_IOU_THRESHOLD = 0.3

# Ground sample distance (meters/pixel) the detector's tiling is normalized
# to before inference — see this module's docstring "Ground-sample-distance
# normalization" section for the measured mismatch this addresses. 0.06 =
# 6cm/px, the middle of the 4-9cm/px range implied by the training data's
# own annotated crown sizes across a plausible real crown-diameter range —
# a starting estimate, not a value validated against this project's own
# imagery yet.
DETECTION_TARGET_GSD_M = 0.06

# If the real (native) and target GSD are already within this fraction of
# each other, skip resampling entirely rather than paying for a resize
# (and its small quality cost) to correct a difference too small to matter.
_GSD_RESAMPLE_SKIP_TOLERANCE = 0.15

# Hard safety bound on the resample factor in either direction — found
# necessary while testing this feature, not a hypothetical: a bad GSD
# estimate (e.g. a raster whose CRS/transform doesn't mean what
# _native_gsd_m() assumes) can produce a wildly wrong native_gsd_m, and an
# UNBOUNDED resample_factor = target_gsd_m / native_gsd_m turns that into
# an attempt to allocate a catastrophically huge array — resizing a
# 1024px tile by a factor of 100x is a ~10,000x memory blowup, not a
# recoverable slowdown. Clamping means a bad GSD estimate degrades to "not
# much normalization happened," never to an OOM or multi-minute hang.
_GSD_RESAMPLE_MAX_FACTOR = 10.0


def _native_gsd_m(transform, crs):
    """Approximate meters-per-pixel for `transform`, in whatever CRS `crs`
    is.

    Projected CRS (the common case for a real drone orthomosaic — this
    project's own uploaded imagery is EPSG:32748, UTM): the transform's
    own pixel scale already IS meters, no conversion needed.

    Geographic CRS (degrees/pixel): converted via the standard
    111,320 m/degree approximation for latitude, adjusted by cos(latitude)
    for longitude compression. Good enough for choosing a resample factor
    — this project's own established bar for this kind of derived
    estimate (see e.g. FarmBlock.overlapping_blocks()'s tolerance_ratio)
    — not precision surveying.
    """
    import math

    pixel_w = abs(transform.a)
    if crs is None or not crs.is_geographic:
        return pixel_w
    latitude = transform.f  # the raster's own northern-edge latitude — close enough for this approximation
    meters_per_degree_lon = 111_320 * math.cos(math.radians(latitude))
    return pixel_w * meters_per_degree_lon


def _reproject_point_to_lonlat(x, y, crs):
    """`x, y` are already in `crs`'s native units (the result of
    `transform * (px, py)`) — reprojects to true EPSG:4326 lon/lat if `crs`
    isn't already geographic. See this module's docstring for why this
    matters: silently treating native-CRS meters as degrees would place
    every detection at a wildly wrong location, not raise an error.
    """
    if crs is None or crs.to_epsg() == 4326:
        return x, y

    from rasterio.warp import transform as warp_transform

    (lon,), (lat,) = warp_transform(crs, "EPSG:4326", [x], [y])
    return lon, lat


def _to_uint8_rgb_hwc(clipped_array):
    """(bands, H, W) -> (H, W, 3) uint8 RGB. Deliberately a small local
    duplicate of apps/imagery/services.py's `_to_uint8_rgb()` (same "first
    3 bands, or replicate band 1; min-max stretch to uint8 if not already
    8-bit" logic) rather than importing it from there — services.py already
    imports from this module at module level (`from .ml.detection import
    get_detector`), so importing back the other way would be a circular
    import. Two small, easily-kept-in-sync copies beat that coupling.
    """
    import numpy as np

    bands = clipped_array.shape[0]
    rgb = clipped_array[:3] if bands >= 3 else np.repeat(clipped_array[:1], 3, axis=0)
    rgb = np.transpose(rgb, (1, 2, 0))
    if rgb.dtype != np.uint8:
        ptp = np.ptp(rgb) or 1
        rgb = ((rgb.astype("float32") - rgb.min()) / ptp * 255).astype("uint8")
    return rgb


def _box_iou(box_a, box_b):
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    inter_x0, inter_y0 = max(ax0, bx0), max(ay0, by0)
    inter_x1, inter_y1 = min(ax1, bx1), min(ay1, by1)
    inter_w, inter_h = max(0.0, inter_x1 - inter_x0), max(0.0, inter_y1 - inter_y0)
    inter_area = inter_w * inter_h
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter_area
    return inter_area / union if union > 0 else 0.0


def _world_nms(boxes_with_scores, iou_threshold):
    """Greedy NMS over every tile's detections, in the raster's native-CRS
    world coordinates (not pixel space, and not lon/lat degrees either —
    IoU in degrees would be spatially non-uniform, distorted by latitude;
    the native CRS's linear units, typically meters for a UTM-projected
    orthomosaic, are what the IoU threshold was actually tuned against in
    the source project). Without this step, every crown whose box straddles
    a tile overlap region gets counted once per tile it appears in — ported
    from palm-vision-ml/tree_health_pipeline/crown_detection.py's
    merge_detections_world_nms(), same algorithm, adapted for plain
    (box_world, score) tuples instead of that project's CrownDetection
    dataclass.
    """
    ordered = sorted(boxes_with_scores, key=lambda item: item[1], reverse=True)
    kept = []
    for box, score in ordered:
        if all(_box_iou(box, kept_box) < iou_threshold for kept_box, _ in kept):
            kept.append((box, score))
    return kept


def _build_crown_detector(num_classes):
    """Port of palm-vision-ml/tree_health_pipeline/crown_detection.py's
    TreeCrownDetector.__init__ model construction. `weights=None,
    weights_backbone=None` — both explicit, not just the first left at its
    default — avoids downloading COCO-pretrained detection weights AND
    ImageNet-pretrained backbone weights at load time: the checkpoint's own
    state_dict overwrites every weight regardless, so either download would
    cost a network round-trip for nothing, the same reasoning
    classification.py's `_build_health_model()` documents for its own
    `pretrained=False`.
    """
    from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
    from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

    model = fasterrcnn_resnet50_fpn_v2(weights=None, weights_backbone=None)
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
    return model


def _load_real_detector(
    model_path,
    iou_threshold=DETECTION_TILE_NMS_IOU_THRESHOLD,
    on_tile_progress=None,
    target_gsd_m=DETECTION_TARGET_GSD_M,
):
    """Loads the real trained Faster R-CNN checkpoint — see this module's
    docstring for the full input contract and the tiling/reprojection/
    GSD-normalization this wraps around it. Unlike the health classifier's
    checkpoint (a self-describing export via palm-vision-ml's
    export_model.py), this one is a raw training checkpoint
    (`{"model_state", "optimizer_state", "epoch"}` — see
    palm-vision-ml/tree_health_pipeline/train_from_local_dataset.py's
    `torch.save()` call), with no embedded class-count/metadata —
    `num_classes=2` (background + the single "oilpalm" foreground class)
    is hardcoded here to match how that training run was actually
    configured for the "tree-detection" dataset, not read from the
    checkpoint itself.

    `iou_threshold`, `on_tile_progress`, and `target_gsd_m` are all
    captured once here, at closure-construction time — get_detector() is
    already called fresh on every run_processing() invocation (no caching
    across runs), so per-block overrides
    (FarmBlock.effective_crown_detection_iou_threshold()/
    effective_detection_target_gsd_m()) and a per-run progress callback
    all just flow through as plain arguments rather than needing the
    returned `detector(clipped_array, transform, crs)` callable's own
    3-argument contract to grow more parameters for them.

    `on_tile_progress(tiles_done, tiles_total)`, if given, is called once
    after every tile — this is what lets ImageProcessingRun.progress_current/
    progress_total (see apps/imagery/services.py's run_processing()) report
    real, measured progress through detection's actual unit of work,
    instead of a fixed guessed percentage per pipeline stage.
    """
    import numpy as np
    import torch
    from rasterio import Affine

    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)

    model = _build_crown_detector(num_classes=2)
    model.load_state_dict(checkpoint["model_state"], strict=True)
    model.eval()

    def detector(clipped_array, transform, crs):
        _bands, height, width = clipped_array.shape
        rgb_full = _to_uint8_rgb_hwc(clipped_array)

        # GSD normalization — see this module's docstring "Ground-sample-
        # distance normalization" section for the measured problem this
        # fixes. Resamples the WHOLE clipped array once, before tiling,
        # rather than per-tile: cheaper (one resize, not many), and it
        # means DETECTION_TILE_SIZE_PX tiles cover roughly the same
        # real-world ground area the training data's own tiles did,
        # restoring the tiling grid's intended scale too, not just the
        # object scale within it.
        if target_gsd_m:
            native_gsd_m = _native_gsd_m(transform, crs)
            if native_gsd_m > 0:
                resample_factor = target_gsd_m / native_gsd_m
                # See _GSD_RESAMPLE_MAX_FACTOR's own comment: bounds how
                # far a single bad GSD estimate can push this, in either
                # direction, before it ever reaches an array-resize call.
                resample_factor = max(
                    1.0 / _GSD_RESAMPLE_MAX_FACTOR, min(_GSD_RESAMPLE_MAX_FACTOR, resample_factor)
                )
                if abs(resample_factor - 1.0) > _GSD_RESAMPLE_SKIP_TOLERANCE:
                    from PIL import Image as PILImage

                    new_height = max(1, round(height / resample_factor))
                    new_width = max(1, round(width / resample_factor))
                    rgb_full = np.array(
                        PILImage.fromarray(rgb_full).resize((new_width, new_height), PILImage.Resampling.LANCZOS)
                    )
                    # Pixel (col', row') in the resampled array corresponds
                    # to (col'*resample_factor, row'*resample_factor) in
                    # the ORIGINAL array — Affine.scale() converts
                    # resampled-space pixel coords to original-space pixel
                    # coords, which `transform` then already knows how to
                    # map to world coords, exactly as it did before.
                    transform = transform * Affine.scale(resample_factor)
                    height, width = new_height, new_width

        stride = DETECTION_TILE_SIZE_PX - DETECTION_TILE_OVERLAP_PX
        row_offsets = list(range(0, height, stride))
        col_offsets = list(range(0, width, stride))
        tiles_total = len(row_offsets) * len(col_offsets)
        tiles_done = 0

        raw_boxes_with_scores = []
        for row_off in row_offsets:
            for col_off in col_offsets:
                win_h = min(DETECTION_TILE_SIZE_PX, height - row_off)
                win_w = min(DETECTION_TILE_SIZE_PX, width - col_off)
                if win_h <= 0 or win_w <= 0:
                    tiles_done += 1
                    if on_tile_progress is not None:
                        on_tile_progress(tiles_done, tiles_total)
                    continue

                tile_rgb = rgb_full[row_off : row_off + win_h, col_off : col_off + win_w, :]
                tile_transform = transform * Affine.translation(col_off, row_off)

                tensor = torch.from_numpy(tile_rgb).permute(2, 0, 1).float() / 255.0
                with torch.no_grad():
                    output = model([tensor])[0]

                scores = output["scores"].numpy()
                boxes = output["boxes"].numpy()
                keep = scores >= DETECTION_SCORE_THRESHOLD
                for (xmin, ymin, xmax, ymax), score in zip(boxes[keep], scores[keep]):
                    x0, y0 = tile_transform * (xmin, ymin)
                    x1, y1 = tile_transform * (xmax, ymax)
                    box_world = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                    raw_boxes_with_scores.append((box_world, float(score)))

                tiles_done += 1
                if on_tile_progress is not None:
                    on_tile_progress(tiles_done, tiles_total)

        detections = []
        for box_world, score in _world_nms(raw_boxes_with_scores, iou_threshold):
            center_x = (box_world[0] + box_world[2]) / 2.0
            center_y = (box_world[1] + box_world[3]) / 2.0
            lon, lat = _reproject_point_to_lonlat(center_x, center_y, crs)
            detections.append((lon, lat, score))
        return detections

    return detector


def get_detector(iou_threshold=None, on_tile_progress=None, target_gsd_m=None):
    """The single entry point apps/imagery/services.py should call —
    never import stub_detector or _load_real_detector directly, so
    swapping in a real model later is a one-place change.

    `iou_threshold`: the cross-tile NMS overlap threshold to use — pass
    `FarmBlock.effective_crown_detection_iou_threshold()` from the caller;
    `None` (the default) falls back to this module's own
    DETECTION_TILE_NMS_IOU_THRESHOLD, same as leaving it unset always did
    before this became configurable.

    `on_tile_progress`: optional `(tiles_done, tiles_total) -> None`
    callback — see `_load_real_detector()`'s own docstring.

    `target_gsd_m`: the ground sample distance (meters/pixel) to resample
    to before tiling — pass `FarmBlock.effective_detection_target_gsd_m()`
    from the caller; `None` falls back to DETECTION_TARGET_GSD_M. See this
    module's docstring "Ground-sample-distance normalization" section.
    """
    if not settings.DETECTION_MODEL_PATH:
        return stub_detector
    if iou_threshold is None:
        iou_threshold = DETECTION_TILE_NMS_IOU_THRESHOLD
    if target_gsd_m is None:
        target_gsd_m = DETECTION_TARGET_GSD_M
    return _load_real_detector(
        settings.DETECTION_MODEL_PATH,
        iou_threshold=iou_threshold,
        on_tile_progress=on_tile_progress,
        target_gsd_m=target_gsd_m,
    )
