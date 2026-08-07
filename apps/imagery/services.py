"""
The actual pipeline logic — georeferencing, clipping, detection, matching,
classification. Deliberately plain functions, not Celery tasks (see
apps/imagery/tasks.py for the thin Celery wrappers) — that's what makes
this directly unit-testable without a running worker/broker, and it's what
lets tests inject a fake detector/classifier per docs/02-build-plan.md
Phase 5: "the pipeline's correctness ... needs to be verifiable independent
of having a production-quality model."

See docs/06-ml-pipeline.md for the full narrative this implements.
"""
import json

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import GEOSGeometry, Point
from django.contrib.gis.measure import D
from django.utils import timezone

from apps.trees.models import Tree

from .ml.classification import get_classifier
from .ml.detection import get_detector
from .models import Image, ImageProcessingRun, TreeDetection, TreeHealthClassification


class ProcessingRefused(Exception):
    """Raised when a run cannot start at all — e.g. no block boundary.
    Distinct from a mid-pipeline failure (which the run's own status/
    error_message field records) because this should never even create a
    'started' run — docs/06-ml-pipeline.md: "refuse and surface ... never
    fall back to processing the unclipped image."
    """


def georeference_image(image: Image) -> Image:
    """Reads embedded CRS/bounds via rasterio and computes Image.footprint
    in EPSG:4326. docs/06-ml-pipeline.md step 3.

    NOTE: uses `image.raw_file.path`, which requires local filesystem
    storage. If/when django-storages S3 backend is configured for
    production, this needs to switch to reading via GDAL's /vsis3/ virtual
    filesystem or downloading to a temp file first — not solved here,
    flagged as a known gap for whoever wires up real cloud storage.
    """
    import rasterio
    from rasterio.warp import transform_bounds

    try:
        with rasterio.open(image.raw_file.path) as src:
            if src.crs is None:
                raise ValueError("Raster has no embedded CRS/georeferencing metadata.")
            left, bottom, right, top = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        footprint = GEOSGeometry(
            json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]],
                }
            ),
            srid=4326,
        )
        image.footprint = footprint
        image.georeference_status = Image.GEOREFERENCE_DONE
        image.georeference_error = ""
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any failure here must be recorded, not crash the Celery task
        image.georeference_status = Image.GEOREFERENCE_FAILED
        image.georeference_error = str(exc)
    image.save(update_fields=["footprint", "georeference_status", "georeference_error"])
    return image


def clip_to_block(image: Image, block):
    """rasterio.mask.mask() against the block's boundary, reprojected into
    the raster's native CRS first (rasterio.mask.mask expects geometries
    in the raster's own CRS, not necessarily 4326). Returns
    (clipped_array, transform, crs) — the exact contract every detector
    callable in apps/imagery/ml/detection.py expects.
    """
    import rasterio
    from rasterio.mask import mask
    from rasterio.warp import transform_geom

    with rasterio.open(image.raw_file.path) as src:
        boundary_geojson = json.loads(block.boundary.geojson)
        if src.crs is not None and src.crs.to_epsg() != 4326:
            boundary_geojson = transform_geom("EPSG:4326", src.crs, boundary_geojson)
        out_image, out_transform = mask(src, [boundary_geojson], crop=True)
        return out_image, out_transform, src.crs


def crop_patch(clipped_array, transform, lon, lat, patch_size_px=64):
    """Extracts a small patch around (lon, lat) from the already-clipped
    raster array, for the classifier. Returns None (never a malformed
    image) if the point falls too close to the clipped array's edge to get
    a usable patch — callers must treat that as "skip classification for
    this detection," not crash the run over one edge case.
    """
    import numpy as np
    from PIL import Image as PILImage

    col_f, row_f = ~transform * (lon, lat)
    row, col = int(row_f), int(col_f)
    half = patch_size_px // 2
    bands, height, width = clipped_array.shape
    r0, r1 = max(0, row - half), min(height, row + half)
    c0, c1 = max(0, col - half), min(width, col + half)
    if r1 <= r0 or c1 <= c0:
        return None

    patch = clipped_array[:, r0:r1, c0:c1]
    rgb = patch[:3] if bands >= 3 else np.repeat(patch[:1], 3, axis=0)
    rgb = np.transpose(rgb, (1, 2, 0))
    if rgb.dtype != np.uint8:
        ptp = rgb.ptp() or 1
        rgb = ((rgb.astype("float32") - rgb.min()) / ptp * 255).astype("uint8")
    return PILImage.fromarray(rgb)


def match_detection(block, point, threshold_m):
    """docs/06-ml-pipeline.md § Matching against the existing tree registry.
    Returns (match_status, tree_or_None, distance_m_or_None).

    One active match within threshold -> matched. Two or more -> ambiguous
    (the doc's "equally-close" case — with a threshold "well below actual
    inter-tree spacing" as the doc specifies, any second match within range
    is itself the ambiguity, not something to break ties on). None, but a
    *removed* tree is nearby -> ambiguous ("possible regrowth"), never
    auto-matched to the dead record or auto-created as new. None at all ->
    new_candidate.

    Uses annotate(Distance(...)).filter(distance__lte=...) rather than the
    `dwithin` lookup docs/06-ml-pipeline.md's own code sample shows —
    PostGIS's `dwithin` lookup refuses a `Distance` object on a plain
    geodetic geometry column (raises "Only numeric values of degree units
    are allowed on geographic DWithin queries" — confirmed against a real
    Postgres/PostGIS run, not just a docs assumption) unless the field is
    declared `geography=True`, which `Tree.location` isn't. The annotate
    pattern gets the same accurate meter-based result without a schema
    change, and is what this function already used for ordering anyway.
    """
    active_candidates = list(
        Tree.objects.filter(farm_block=block, status=Tree.STATUS_ACTIVE)
        .annotate(distance=Distance("location", point))
        .filter(distance__lte=D(m=threshold_m))
        .order_by("distance")
    )

    if len(active_candidates) == 1:
        tree = active_candidates[0]
        return TreeDetection.MATCH_MATCHED, tree, tree.distance.m
    if len(active_candidates) >= 2:
        return TreeDetection.MATCH_AMBIGUOUS, None, active_candidates[0].distance.m

    removed_nearby = (
        Tree.objects.filter(farm_block=block, status=Tree.STATUS_REMOVED)
        .annotate(distance=Distance("location", point))
        .filter(distance__lte=D(m=threshold_m))
        .exists()
    )
    if removed_nearby:
        return TreeDetection.MATCH_AMBIGUOUS, None, None

    return TreeDetection.MATCH_NEW_CANDIDATE, None, None


CLASSIFICATION_REVIEW_THRESHOLD = 0.70  # docs/06-ml-pipeline.md step 4: "70% as a starting point"


def classify_detection(detection: TreeDetection, clipped_array, transform, classifier=None):
    """Crops the patch and runs the classifier for one detection, writing a
    TreeHealthClassification row. Below-threshold (or unusable-patch)
    results are still written, just flagged for review — never dropped
    silently, and never auto-accepted either.
    """
    classifier = classifier or get_classifier()
    lon, lat = detection.detected_location.x, detection.detected_location.y
    patch = crop_patch(clipped_array, transform, lon, lat)
    if patch is None:
        label, confidence = "healthy", 0.0  # unusable patch -> treated exactly like a low-confidence result, always routed to review
    else:
        label, confidence = classifier(patch)

    return TreeHealthClassification.objects.create(
        tree_detection=detection,
        health_label=label,
        confidence=confidence,
        reviewed=False,
    )


def run_processing(run: ImageProcessingRun, detector=None, classifier=None) -> ImageProcessingRun:
    """Full pipeline sequence — docs/06-ml-pipeline.md § Full pipeline
    sequence. `detector`/`classifier` are injectable purely for tests; real
    callers should leave them unset and get whatever apps/imagery/ml/*.py's
    get_detector()/get_classifier() currently resolve to (stub or real,
    depending on settings.DETECTION_MODEL_PATH/CLASSIFICATION_MODEL_PATH).
    """
    block = run.farm_block

    if not block.boundary:
        run.status = ImageProcessingRun.STATUS_FAILED
        run.error_message = "This block has no boundary polygon — draw it before processing imagery against it."
        run.started_at = run.started_at or timezone.now()
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "started_at", "completed_at"])
        return run

    run.status = ImageProcessingRun.STATUS_CLIPPING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    try:
        clipped_array, clipped_transform, clip_crs = clip_to_block(run.image, block)
    except Exception as exc:  # noqa: BLE001 — any clip failure must land in error_message, not crash the task
        run.status = ImageProcessingRun.STATUS_FAILED
        run.error_message = f"Clipping failed: {exc}"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at"])
        _notify_processing_failure(run)
        return run

    run.status = ImageProcessingRun.STATUS_DETECTING
    run.save(update_fields=["status"])

    detector = detector or get_detector()
    raw_detections = detector(clipped_array, clipped_transform, clip_crs)

    threshold_m = block.effective_tree_match_threshold_m()
    detected_count = 0
    new_candidate_count = 0
    matched_for_classification = []

    for lon, lat, confidence in raw_detections:
        point = Point(lon, lat, srid=4326)
        # Belt-and-suspenders check — docs/06-ml-pipeline.md: catches any
        # clipping edge artifacts. Clipping is still primary; this is not
        # a substitute for it.
        if not block.boundary.contains(point):
            continue

        detected_count += 1
        match_status, tree, distance_m = match_detection(block, point, threshold_m)
        if match_status == TreeDetection.MATCH_NEW_CANDIDATE:
            new_candidate_count += 1

        detection = TreeDetection.objects.create(
            processing_run=run,
            tree=tree,
            detected_location=point,
            detection_confidence=confidence,
            match_status=match_status,
            matched_distance_m=distance_m,
        )
        if match_status == TreeDetection.MATCH_MATCHED:
            matched_for_classification.append(detection)

    run.detected_tree_count = detected_count
    run.new_candidate_count = new_candidate_count

    if matched_for_classification:
        run.status = ImageProcessingRun.STATUS_CLASSIFYING
        run.save(update_fields=["status", "detected_tree_count", "new_candidate_count"])
        classifier_fn = classifier or get_classifier()
        for detection in matched_for_classification:
            classify_detection(detection, clipped_array, clipped_transform, classifier=classifier_fn)

    run.status = ImageProcessingRun.STATUS_READY
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "detected_tree_count", "new_candidate_count", "completed_at"])
    return run


def _notify_processing_failure(run: ImageProcessingRun):
    """docs/12-security-ops.md § Alerting & notifications: a failed
    processing run is explicitly listed as something that "shouldn't wait"
    for a daily digest — event-driven, right here, not left to
    apps/notifications/tasks.py's periodic checks (which cover the other
    five trigger types, all genuinely digest-shaped). Notifies whoever
    triggered the run, if known (Beat-triggered/system runs currently don't
    set triggered_by, so there's deliberately no user to notify in that
    case — no fallback to "notify everyone").  Local import: apps/imagery
    doesn't otherwise depend on apps/notifications, and importing at module
    load time isn't needed for a call that only happens on this one
    failure path.
    """
    if run.triggered_by_id is None:
        return
    from apps.notifications.models import NotificationCategory
    from apps.notifications.services import notify

    notify(
        run.triggered_by,
        NotificationCategory.PROCESSING_FAILURE,
        title=f"Imagery processing failed — {run.farm_block.name}",
        message=run.error_message or "Processing run failed with no error message recorded.",
        # No standalone processing-run detail endpoint exists — see
        # apps/imagery/urls.py — so this points at the nearest real one
        # (that block's run list) rather than a URL that would 404.
        link=f"/api/farm-blocks/{run.farm_block_id}/processing-runs/",
    )


def confirm_new_candidate(detection: TreeDetection, user, classifier=None) -> Tree:
    """Review-queue action: docs/04-api-spec.md POST /api/detections/{id}/confirm/
    — "Creates a Tree row with source=drone_detected." Also runs
    classification for the newly-confirmed tree's detection, since
    docs/06-ml-pipeline.md step 3 says classification runs on matched
    detections "and any new_candidate once confirmed" — but only if the
    original clipped raster is still available; if the processing run's
    source image has since been deleted, classification is skipped rather
    than failing the confirmation itself (confirming the tree is the
    important action; a missing health label can be backfilled later).
    """
    tree = Tree.objects.create(
        farm_block=detection.processing_run.farm_block,
        location=detection.detected_location,
        source=Tree.SOURCE_DRONE_DETECTED,
        created_by=user,
        updated_by=user,
    )
    detection.tree = tree
    detection.save(update_fields=["tree"])

    try:
        clipped_array, clipped_transform, _crs = clip_to_block(detection.processing_run.image, detection.processing_run.farm_block)
        classify_detection(detection, clipped_array, clipped_transform, classifier=classifier)
    except Exception:  # noqa: BLE001 — see docstring: classification failure must not block confirmation
        pass

    return tree
