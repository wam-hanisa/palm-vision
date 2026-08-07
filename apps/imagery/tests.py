"""
docs/02-build-plan.md Phase 5 verification gate: "upload a small test
image ... against a seeded block with a boundary polygon, run it through
the full chain with stub models, and confirm: the run's status progresses
through each stage, detections outside the block boundary don't appear,
and the review queue shows unmatched detections correctly."

Uses real rasterio-written GeoTIFFs (small synthetic rasters, not real
drone imagery) so the georeferencing/clipping code paths are exercised
against actual raster I/O, not mocked out. Detection/classification use
injected fake callables rather than the stub or a real model — this is
what makes "does the pipeline route detections correctly" testable
independent of "is there a trained model," per the build plan's framing.
"""
import numpy as np
import pytest
import rasterio
from django.contrib.gis.geos import Point
from django.core.files import File
from rasterio.transform import from_bounds
from rest_framework import status

from apps.imagery.models import Image, ImageProcessingRun, ModelVersion, TreeDetection, TreeHealthClassification
from apps.imagery.services import (
    clip_to_block,
    confirm_new_candidate,
    georeference_image,
    match_detection,
    run_processing,
)
from apps.trees.models import Tree

# Block A's boundary in conftest.py's BLOCK_A_BOUNDARY is roughly
# 101.4400-101.4450 lon, 0.5300-0.5350 lat. This raster deliberately
# extends beyond it on every side — flight overshoot is the normal case
# docs/06-ml-pipeline.md's clipping requirement exists for.
RASTER_BOUNDS_4326 = (101.4390, 0.5290, 101.4460, 0.5360)


def _write_raster(path, bounds, crs="EPSG:4326", width=100, height=100, bands=3):
    left, bottom, right, top = bounds
    transform = from_bounds(left, bottom, right, top, width, height)
    data = np.random.default_rng(42).integers(0, 255, size=(bands, height, width), dtype="uint8")
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=bands, dtype="uint8", crs=crs, transform=transform
    ) as dst:
        dst.write(data)
    return transform


def _make_image(tmp_path, farm, uploader, bounds=RASTER_BOUNDS_4326, crs="EPSG:4326", filename="ortho.tif"):
    path = tmp_path / filename
    _write_raster(path, bounds, crs=crs)
    image = Image(farm=farm, uploaded_by=uploader)
    with open(path, "rb") as f:
        image.raw_file.save(filename, File(f), save=True)
    return image


# --- Georeferencing ---------------------------------------------------

@pytest.mark.django_db
def test_georeference_image_sets_footprint_from_4326_raster(tmp_path, farm, manager):
    image = _make_image(tmp_path, farm, manager)
    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_DONE
    assert image.footprint is not None
    # Footprint should roughly match the raster's own bounds.
    left, bottom, right, top = RASTER_BOUNDS_4326
    assert image.footprint.extent == pytest.approx((left, bottom, right, top), abs=1e-4)


@pytest.mark.django_db
def test_georeference_image_reprojects_utm_raster(tmp_path, farm, manager):
    # EPSG:32648 = UTM zone 48N, covers Sumatra — a plausible real drone
    # orthomosaic CRS, unlike 4326 which is what most of this test file
    # uses for simplicity elsewhere.
    utm_bounds = (200000, 58000, 205000, 63000)  # meters — lands in/near the same Sumatra region
    image = _make_image(tmp_path, farm, manager, bounds=utm_bounds, crs="EPSG:32648", filename="ortho_utm.tif")
    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_DONE
    left, bottom, right, top = image.footprint.extent
    # Reprojected footprint should land in plausible lon/lat ranges, not
    # still be in raw UTM meter values (which would be wildly out of range
    # for lon/lat, e.g. left=200000).
    assert 90 < left < 110
    assert -10 < bottom < 10


@pytest.mark.django_db
def test_georeference_image_handles_missing_crs(tmp_path, farm, manager):
    path = tmp_path / "no_crs.tif"
    _write_raster(path, RASTER_BOUNDS_4326, crs=None)
    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("no_crs.tif", File(f), save=True)

    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_FAILED
    assert image.georeference_error  # non-empty, some explanation recorded


@pytest.mark.django_db
def test_overlapping_blocks_suggested_after_georeferencing(tmp_path, farm, manager, block_a, block_b):
    image = _make_image(tmp_path, farm, manager)
    georeference_image(image)
    image.refresh_from_db()
    overlapping_ids = set(image.overlapping_blocks().values_list("id", flat=True))
    # The raster deliberately covers both blocks (see RASTER_BOUNDS_4326).
    assert block_a.id in overlapping_ids
    assert block_b.id in overlapping_ids


# --- Clipping -----------------------------------------------------------

@pytest.mark.django_db
def test_clip_to_block_crops_to_smaller_extent_than_source(tmp_path, farm, manager, block_a):
    image = _make_image(tmp_path, farm, manager)
    clipped_array, transform, crs = clip_to_block(image, block_a)
    # block_a is smaller than the full raster (RASTER_BOUNDS_4326
    # deliberately overshoots it), so the clipped output must be smaller
    # in pixel dimensions than the full 100x100 source.
    _, height, width = clipped_array.shape
    assert height < 100
    assert width < 100


# --- Matching -------------------------------------------------------------

@pytest.mark.django_db
def test_match_detection_single_nearby_active_tree_is_matched(block_a, tree_in_block_a):
    point = Point(tree_in_block_a.location.x + 0.00001, tree_in_block_a.location.y, srid=4326)  # ~1m away
    status_, tree, distance_m = match_detection(block_a, point, threshold_m=3.0)
    assert status_ == TreeDetection.MATCH_MATCHED
    assert tree.id == tree_in_block_a.id
    assert distance_m is not None


@pytest.mark.django_db
def test_match_detection_no_nearby_tree_is_new_candidate(block_a):
    far_point = Point(101.4499, 0.5399, srid=4326)  # nowhere near any seeded tree
    status_, tree, distance_m = match_detection(block_a, far_point, threshold_m=3.0)
    assert status_ == TreeDetection.MATCH_NEW_CANDIDATE
    assert tree is None


@pytest.mark.django_db
def test_match_detection_two_nearby_trees_is_ambiguous(block_a, pic_block):
    p1 = Point(101.4420, 0.5320, srid=4326)
    p2 = Point(101.44201, 0.5320, srid=4326)  # ~1m from p1 — both within a 5m threshold of the midpoint
    Tree.objects.create(farm_block=block_a, name="T1", location=p1, created_by=pic_block, updated_by=pic_block)
    Tree.objects.create(farm_block=block_a, name="T2", location=p2, created_by=pic_block, updated_by=pic_block)
    midpoint = Point(101.442005, 0.5320, srid=4326)
    status_, tree, distance_m = match_detection(block_a, midpoint, threshold_m=5.0)
    assert status_ == TreeDetection.MATCH_AMBIGUOUS
    assert tree is None


@pytest.mark.django_db
def test_match_detection_near_removed_tree_is_ambiguous_not_new_or_matched(block_a, pic_block):
    removed = Tree.objects.create(
        farm_block=block_a, name="Dead tree", location=Point(101.4430, 0.5330, srid=4326),
        status=Tree.STATUS_REMOVED, created_by=pic_block, updated_by=pic_block,
    )
    nearby = Point(101.44301, 0.5330, srid=4326)
    status_, tree, distance_m = match_detection(block_a, nearby, threshold_m=3.0)
    assert status_ == TreeDetection.MATCH_AMBIGUOUS
    assert tree is None  # never auto-matched to the dead record
    assert removed.status == Tree.STATUS_REMOVED  # unchanged, just confirming the fixture


# --- Full pipeline (run_processing) with an injected fake detector --------

def _fake_detector_factory(points):
    """points: list of (lon, lat, confidence). Ignores the raster args
    entirely — this is purely for exercising the matching/status logic
    downstream of detection, not detection itself.
    """

    def detector(clipped_array, transform, crs):
        return points

    return detector


@pytest.mark.django_db
def test_run_processing_refuses_when_block_has_no_boundary(tmp_path, farm, manager):
    from apps.farms.models import FarmBlock

    block_no_boundary = FarmBlock.objects.create(farm=farm, name="Unsurveyed block", boundary=None)
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_no_boundary, triggered_by=manager)

    run_processing(run, detector=_fake_detector_factory([]), classifier=lambda crop: ("healthy", 0.9))
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_FAILED
    assert "boundary" in run.error_message.lower()
    assert TreeDetection.objects.filter(processing_run=run).count() == 0


@pytest.mark.django_db
def test_run_processing_full_chain_matched_new_candidate_and_out_of_bounds(tmp_path, farm, manager, block_a, tree_in_block_a):
    image = _make_image(tmp_path, farm, manager)

    inside_matched = (tree_in_block_a.location.x, tree_in_block_a.location.y, 0.95)
    inside_new = (101.4440, 0.5340, 0.88)  # inside block_a, nowhere near tree_in_block_a
    outside_block = (101.0, 0.0, 0.99)  # nowhere near block_a at all — must be filtered out

    fake_detector = _fake_detector_factory([inside_matched, inside_new, outside_block])
    fake_classifier = lambda crop: ("yellow", 0.42)  # noqa: E731 — test-local, fine

    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    run_processing(run, detector=fake_detector, classifier=fake_classifier)
    run.refresh_from_db()

    assert run.status == ImageProcessingRun.STATUS_READY
    assert run.started_at is not None and run.completed_at is not None

    detections = list(TreeDetection.objects.filter(processing_run=run))
    # Exactly 2 — the out-of-block point must never produce a row at all,
    # per the belt-and-suspenders `within` check (clipping should already
    # exclude it; this asserts the safety net too).
    assert len(detections) == 2
    assert run.detected_tree_count == 2
    assert run.new_candidate_count == 1

    matched = [d for d in detections if d.match_status == TreeDetection.MATCH_MATCHED]
    new_candidates = [d for d in detections if d.match_status == TreeDetection.MATCH_NEW_CANDIDATE]
    assert len(matched) == 1
    assert matched[0].tree_id == tree_in_block_a.id
    assert len(new_candidates) == 1

    # Classification only runs for matched detections in the main pipeline
    # (docs/06-ml-pipeline.md: "and any new_candidate once confirmed" —
    # i.e. later, via confirm_new_candidate(), not here).
    assert TreeHealthClassification.objects.filter(tree_detection=matched[0]).exists()
    assert not TreeHealthClassification.objects.filter(tree_detection=new_candidates[0]).exists()


@pytest.mark.django_db
def test_run_processing_clip_failure_marks_run_failed_not_crash(tmp_path, farm, manager, block_a):
    # An Image whose raw_file doesn't actually exist on disk (simulates a
    # storage problem) — clip_to_block() should raise, and run_processing()
    # must turn that into a failed status, not propagate the exception.
    image = Image.objects.create(farm=farm, uploaded_by=manager, raw_file="imagery/raw/does-not-exist.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    run_processing(run, detector=_fake_detector_factory([]), classifier=lambda c: ("healthy", 0.9))
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_FAILED
    assert "clipping failed" in run.error_message.lower()


# --- confirm/reject/link (review queue) -----------------------------------

@pytest.mark.django_db
def test_confirm_new_candidate_creates_tree_and_classifies(tmp_path, farm, manager, block_a):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4440, 0.5340, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    tree = confirm_new_candidate(detection, manager, classifier=lambda crop: ("small", 0.6))
    detection.refresh_from_db()
    assert tree.source == Tree.SOURCE_DRONE_DETECTED
    assert tree.farm_block_id == block_a.id
    assert detection.tree_id == tree.id
    assert TreeHealthClassification.objects.filter(tree_detection=detection, health_label="small").exists()


# --- API-level tests -------------------------------------------------------

@pytest.mark.django_db
def test_pic_block_cannot_upload_imagery(as_pic_block, farm, tmp_path):
    path = tmp_path / "upload.tif"
    _write_raster(path, RASTER_BOUNDS_4326)
    with open(path, "rb") as f:
        response = as_pic_block.post("/api/images/", {"farm": str(farm.id), "raw_file": f}, format="multipart")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_manager_can_upload_imagery_and_it_georeferences_synchronously(as_manager, farm, tmp_path):
    path = tmp_path / "upload.tif"
    _write_raster(path, RASTER_BOUNDS_4326)
    with open(path, "rb") as f:
        response = as_manager.post("/api/images/", {"farm": str(farm.id), "raw_file": f}, format="multipart")
    assert response.status_code == status.HTTP_201_CREATED
    image = Image.objects.get(id=response.data["id"])
    # CELERY_TASK_ALWAYS_EAGER=True in settings.test — the georeference
    # task has already run synchronously by the time .delay() returns.
    assert image.georeference_status == Image.GEOREFERENCE_DONE


@pytest.mark.django_db
def test_process_endpoint_requires_manager(as_pic_block, farm, block_a, manager, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    response = as_pic_block.post(f"/api/farm-blocks/{block_a.id}/images/{image.id}/process/")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_process_endpoint_runs_pipeline_end_to_end(as_manager, farm, block_a, manager, tmp_path, monkeypatch):
    image = _make_image(tmp_path, farm, manager)
    georeference_image(image)  # must be georeferenced first, per the view's own check

    # Patch the stub detector to return one deterministic in-block point —
    # this exercises the real view -> task -> services chain end to end
    # (CELERY_TASK_ALWAYS_EAGER=True), not just services.run_processing()
    # called directly like the tests above.
    import apps.imagery.services as services_module

    monkeypatch.setattr(
        services_module, "get_detector", lambda: _fake_detector_factory([(101.4410, 0.5310, 0.9)])
    )

    response = as_manager.post(f"/api/farm-blocks/{block_a.id}/images/{image.id}/process/")
    assert response.status_code == status.HTTP_201_CREATED
    run = ImageProcessingRun.objects.get(id=response.data["id"])
    assert run.status == ImageProcessingRun.STATUS_READY
    assert run.detected_tree_count == 1


@pytest.mark.django_db
def test_review_queue_lists_only_requested_match_status(as_manager, farm, block_a, manager, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4420, 0.5320, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_AMBIGUOUS,
    )
    response = as_manager.get(f"/api/processing-runs/{run.id}/detections/", {"status": "new_candidate"})
    assert response.status_code == status.HTTP_200_OK
    assert len(response.data) == 1
    assert response.data[0]["match_status"] == "new_candidate"


@pytest.mark.django_db
def test_confirm_reject_link_api_actions(as_manager, farm, block_a, manager, tmp_path, tree_in_block_a):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    new_candidate = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4440, 0.5340, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    confirm_response = as_manager.post(f"/api/detections/{new_candidate.id}/confirm/")
    assert confirm_response.status_code == status.HTTP_201_CREATED
    assert Tree.objects.filter(id=confirm_response.data["tree_id"]).exists()

    to_reject = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4441, 0.5341, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    reject_response = as_manager.post(f"/api/detections/{to_reject.id}/reject/")
    assert reject_response.status_code == status.HTTP_200_OK
    assert reject_response.data["match_status"] == "rejected"

    ambiguous = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_AMBIGUOUS,
    )
    link_response = as_manager.post(f"/api/detections/{ambiguous.id}/link/", {"tree_id": str(tree_in_block_a.id)})
    assert link_response.status_code == status.HTTP_200_OK
    assert link_response.data["match_status"] == "matched"
    # response.data holds the pre-render serializer output (real UUID
    # objects), not JSON-serialized strings — only .content after
    # rendering would be a string. Comparing the actual UUID objects here.
    assert link_response.data["tree"] == tree_in_block_a.id


@pytest.mark.django_db
def test_pic_block_can_confirm_detection_on_their_own_block(as_pic_block, farm, block_a, manager, tmp_path):
    # docs/06-ml-pipeline.md doesn't restrict review-queue actions to
    # Manager-only — a PIC Block user reviewing their own block's
    # detections is the expected everyday flow.
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4440, 0.5340, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    response = as_pic_block.post(f"/api/detections/{detection.id}/confirm/")
    assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.django_db
def test_unaffiliated_user_cannot_act_on_detection(as_other_pic_block, farm, block_a, manager, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4440, 0.5340, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    response = as_other_pic_block.post(f"/api/detections/{detection.id}/confirm/")
    assert response.status_code == status.HTTP_403_FORBIDDEN


# --- ModelVersion governance ------------------------------------------------

@pytest.mark.django_db
def test_promote_to_production_retires_previous_production_of_same_type(manager):
    old_prod = ModelVersion.objects.create(
        model_type=ModelVersion.TYPE_DETECTION, status=ModelVersion.STATUS_PRODUCTION,
        trained_at="2026-01-01", trained_by=manager,
    )
    new_staging = ModelVersion.objects.create(
        model_type=ModelVersion.TYPE_DETECTION, status=ModelVersion.STATUS_STAGING,
        trained_at="2026-06-01", trained_by=manager,
    )
    new_staging.promote_to_production()
    old_prod.refresh_from_db()
    new_staging.refresh_from_db()
    assert old_prod.status == ModelVersion.STATUS_RETIRED
    assert new_staging.status == ModelVersion.STATUS_PRODUCTION


@pytest.mark.django_db
def test_promote_to_production_does_not_affect_other_model_type(manager):
    detection_prod = ModelVersion.objects.create(
        model_type=ModelVersion.TYPE_DETECTION, status=ModelVersion.STATUS_PRODUCTION,
        trained_at="2026-01-01", trained_by=manager,
    )
    classification_staging = ModelVersion.objects.create(
        model_type=ModelVersion.TYPE_CLASSIFICATION, status=ModelVersion.STATUS_STAGING,
        trained_at="2026-06-01", trained_by=manager,
    )
    classification_staging.promote_to_production()
    detection_prod.refresh_from_db()
    assert detection_prod.status == ModelVersion.STATUS_PRODUCTION  # untouched — different model_type
