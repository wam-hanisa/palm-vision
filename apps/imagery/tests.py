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
from datetime import timedelta

import numpy as np
import pytest
import rasterio
from django.contrib.gis.geos import Point
from django.core.files import File
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image as PILImage
from rasterio.transform import from_bounds
from rest_framework import status

from django.urls import reverse
from apps.imagery.models import Image, ImageProcessingRun, ModelVersion, TreeDetection, TreeHealthClassification
from apps.imagery.services import (
    ImageDeletionRefused,
    apply_storage_lifecycle,
    classify_detection,
    clip_to_block,
    confirm_new_candidate,
    convert_to_cog,
    crop_patch,
    delete_image,
    generate_preview,
    georeference_image,
    match_detection,
    persist_clipped_block_imagery,
    run_processing,
)
from apps.notifications.models import Notification, NotificationCategory
from apps.trees.models import Tree

# Block A's boundary in conftest.py's BLOCK_A_BOUNDARY is roughly
# 101.4400-101.4450 lon, 0.5300-0.5350 lat. This raster deliberately
# extends beyond it on every side — flight overshoot is the normal case
# docs/06-ml-pipeline.md's clipping requirement exists for.
RASTER_BOUNDS_4326 = (101.4390, 0.5290, 101.4460, 0.5360)


def _write_raster(path, bounds, crs="EPSG:4326", width=100, height=100, bands=3, dtype="uint8"):
    # dtype="uint8" (the default every existing caller relies on, unchanged)
    # never exercises _to_uint8_rgb()'s dtype-normalization branch in
    # apps/imagery/services.py — see test_generate_preview_handles_a_real_
    # world_non_uint8_raster and test_crop_patch_handles_a_non_uint8_
    # clipped_array below for why that branch specifically needed a real,
    # non-uint8-producing test (real drone orthomosaics are routinely
    # 16-bit or float, not 8-bit).
    left, bottom, right, top = bounds
    transform = from_bounds(left, bottom, right, top, width, height)
    if dtype == "uint8":
        data = np.random.default_rng(42).integers(0, 255, size=(bands, height, width), dtype="uint8")
    elif dtype == "uint16":
        data = np.random.default_rng(42).integers(0, 65535, size=(bands, height, width), dtype="uint16")
    else:
        data = (np.random.default_rng(42).random(size=(bands, height, width)).astype(dtype)) * 5000
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=bands, dtype=dtype, crs=crs, transform=transform
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


# --- Preview generation (docs/06-ml-pipeline.md step 4's "simpler
# downsampled preview" option) --------------------------------------------

@pytest.mark.django_db
def test_generate_preview_creates_a_png_file(tmp_path, farm, manager):
    image = _make_image(tmp_path, farm, manager)
    generate_preview(image)
    image.refresh_from_db()
    assert image.preview_file
    assert image.preview_error == ""
    assert image.preview_file.name.endswith(".png")


@pytest.mark.django_db
def test_generate_preview_downsamples_to_max_dimension(tmp_path, farm, manager):
    # The synthetic raster is 100x100 (see _write_raster's default) — a
    # small max_dimension actually exercises the downsampling path, not
    # just "the file happened to already be small enough."
    image = _make_image(tmp_path, farm, manager)
    generate_preview(image, max_dimension=20)
    image.refresh_from_db()
    with PILImage.open(image.preview_file) as preview:
        assert max(preview.size) <= 20


@pytest.mark.django_db
def test_generate_preview_handles_grayscale_single_band_raster(tmp_path, farm, manager):
    path = tmp_path / "grayscale.tif"
    _write_raster(path, RASTER_BOUNDS_4326, bands=1)
    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("grayscale.tif", File(f), save=True)

    generate_preview(image)

    image.refresh_from_db()
    assert image.preview_file
    assert image.preview_error == ""


@pytest.mark.django_db
def test_generate_preview_handles_a_real_world_non_uint8_raster(tmp_path, farm, manager):
    """Real drone orthomosaics are routinely 16-bit or float, not 8-bit —
    every other test in this file uses _write_raster()'s uint8 default,
    which never exercises _to_uint8_rgb()'s dtype-normalization branch
    (apps/imagery/services.py). That branch previously called the
    ndarray.ptp() *method*, removed in NumPy 2.0 (this project's
    requirements.txt pins numpy>=1.24 with no upper bound, so a fresh
    install resolves to 2.x today) — a real bug, caught by a standalone
    script exercising this exact non-uint8 path during this change, fixed
    by switching to the np.ptp() function instead (works on both NumPy 1.x
    and 2.x). This test is the permanent regression coverage for it.
    """
    path = tmp_path / "float32.tif"
    _write_raster(path, RASTER_BOUNDS_4326, bands=3, dtype="float32")
    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("float32.tif", File(f), save=True)

    generate_preview(image)  # must not raise AttributeError

    image.refresh_from_db()
    assert image.preview_file
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        # RGBA, not RGB — see test_generate_preview_makes_undeclared_black_
        # padding_transparent below for why the output gained an alpha
        # channel.
        assert preview.mode == "RGBA"


@pytest.mark.django_db
def test_generate_preview_makes_undeclared_black_padding_transparent(tmp_path, farm, manager):
    """Common case for drone orthomosaic exports: the raster's own
    rectangular bounding box is padded with plain black pixels wherever the
    actual flight path doesn't reach, with no nodata value declared at
    all. generate_preview() must still treat that padding as transparent
    (_preview_alpha_channel()'s heuristic branch) rather than paint it as
    an opaque black rectangle laid over the map — reported directly: "it
    contain white/black area, while it should be only the picture."
    """
    path = tmp_path / "black_padded.tif"
    width, height = 40, 20
    transform = from_bounds(*RASTER_BOUNDS_4326, width, height)
    # Values kept away from 0/255 in the "real" half so this test can't
    # accidentally trip the same heuristic it's trying to prove — a false
    # positive here would make the test meaningless.
    data = np.random.default_rng(7).integers(10, 245, size=(3, height, width), dtype="uint8")
    data[:, :, : width // 2] = 0  # left half is padding — pure black, no nodata declared
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=3, dtype="uint8",
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data)

    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("black_padded.tif", File(f), save=True)

    generate_preview(image, max_dimension=width)
    image.refresh_from_db()
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        assert preview.mode == "RGBA"
        arr = np.array(preview)
        padding_alpha = arr[:, : width // 2, 3]
        real_alpha = arr[:, width // 2 :, 3]
        assert padding_alpha.mean() < 10  # effectively fully transparent
        assert real_alpha.mean() > 245  # effectively fully opaque — not falsely punched through


@pytest.mark.django_db
def test_generate_preview_makes_undeclared_white_padding_transparent(tmp_path, farm, manager):
    """Same as the black-padding case above, but white — some orthomosaic
    tools pad with white instead of black. Both must be caught.
    """
    path = tmp_path / "white_padded.tif"
    width, height = 40, 20
    transform = from_bounds(*RASTER_BOUNDS_4326, width, height)
    data = np.random.default_rng(9).integers(10, 245, size=(3, height, width), dtype="uint8")
    data[:, :, : width // 2] = 255  # left half is padding — pure white
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=3, dtype="uint8",
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data)

    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("white_padded.tif", File(f), save=True)

    generate_preview(image, max_dimension=width)
    image.refresh_from_db()
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        arr = np.array(preview)
        padding_alpha = arr[:, : width // 2, 3]
        real_alpha = arr[:, width // 2 :, 3]
        assert padding_alpha.mean() < 10
        assert real_alpha.mean() > 245


@pytest.mark.django_db
def test_generate_preview_undeclared_padding_alpha_stays_fully_binary_under_decimation(tmp_path, farm, manager):
    """Regression test: the undeclared-padding heuristic used to classify
    purity on `rgb` — already read with Resampling.average for visual
    quality — instead of a separate, never-blending Resampling.nearest
    read. A decimation window straddling real content and pure-black
    padding averages into something like (65, 70, 62): visibly darker than
    real content but no longer *exactly* black, so the old exact-purity
    test missed it and left it fully opaque — a visible dark fringe right
    at the image edge. Confirmed with a real decimated rasterio read
    against a synthetic black-padded raster before fixing (not assumed).
    Like the declared-nodata regression test above, this needs genuine
    decimation to exercise the bug — the two undeclared-padding tests
    above use max_dimension == source width (1:1, nothing to blend).
    """
    path = tmp_path / "black_padded_decimated.tif"
    width, height = 200, 100
    transform = from_bounds(*RASTER_BOUNDS_4326, width, height)
    data = np.random.default_rng(17).integers(80, 245, size=(3, height, width), dtype="uint8")
    boundary_col = 98  # not a multiple of the 4x decimation factor (200 / 50) — straddles a cell
    data[:, :, :boundary_col] = 0  # pure black padding, no nodata declared
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=3, dtype="uint8",
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data)

    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("black_padded_decimated.tif", File(f), save=True)

    generate_preview(image, max_dimension=50)  # forces real 4x downsampling
    image.refresh_from_db()
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        alpha = np.array(preview)[:, :, 3]
        distinct_values = set(np.unique(alpha).tolist())
        assert distinct_values <= {0, 255}, f"alpha must be fully binary, got intermediate values: {distinct_values}"


@pytest.mark.django_db
def test_generate_preview_uses_declared_nodata_over_heuristic(tmp_path, farm, manager):
    """When the source raster declares a real nodata value, that's used
    directly (rasterio's own dataset_mask()) rather than the pure-black/
    white heuristic — proven here with a nodata value (7) the heuristic
    would never catch on its own, confirming the authoritative path is the
    one actually being exercised, not a coincidental heuristic match.
    """
    path = tmp_path / "nodata7.tif"
    width, height = 40, 20
    transform = from_bounds(*RASTER_BOUNDS_4326, width, height)
    data = np.random.default_rng(11).integers(10, 245, size=(3, height, width), dtype="uint8")
    data[:, :, : width // 2] = 7  # padding tagged via nodata, not black/white
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=3, dtype="uint8",
        crs="EPSG:4326", transform=transform, nodata=7,
    ) as dst:
        dst.write(data)

    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("nodata7.tif", File(f), save=True)

    generate_preview(image, max_dimension=width)
    image.refresh_from_db()
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        arr = np.array(preview)
        padding_alpha = arr[:, : width // 2, 3]
        real_alpha = arr[:, width // 2 :, 3]
        assert padding_alpha.mean() < 10
        assert real_alpha.mean() > 245


@pytest.mark.django_db
def test_generate_preview_declared_nodata_alpha_stays_fully_binary_under_decimation(tmp_path, farm, manager):
    """Regression test: _preview_alpha_channel()'s declared-nodata path
    used `Resampling.average` for the decimated mask read — correct for
    continuous imagery, wrong for a binary 0/255 mask. Averaging pixels
    straddling the real/nodata boundary produced intermediate alpha values
    (a soft, partially-see-through fringe) instead of a clean cut —
    reported directly against real Pix4D exports ("make the white/black
    area fully transparent"). The previous nodata test above never caught
    this because it used max_dimension == source width, meaning no actual
    decimation ever happened (1:1 output, nothing to blend) — this test
    deliberately downsamples 4x with a nodata/real boundary placed so it
    does NOT land on a decimation cell edge, forcing at least one output
    pixel to aggregate a source block straddling both regions.
    """
    path = tmp_path / "nodata_decimated.tif"
    width, height = 200, 100
    transform = from_bounds(*RASTER_BOUNDS_4326, width, height)
    data = np.random.default_rng(13).integers(10, 245, size=(3, height, width), dtype="uint8")
    boundary_col = 98  # not a multiple of the 4x decimation factor (200 / 50) — straddles a cell
    data[:, :, :boundary_col] = 7  # padding tagged via nodata
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=3, dtype="uint8",
        crs="EPSG:4326", transform=transform, nodata=7,
    ) as dst:
        dst.write(data)

    image = Image(farm=farm, uploaded_by=manager)
    with open(path, "rb") as f:
        image.raw_file.save("nodata_decimated.tif", File(f), save=True)

    generate_preview(image, max_dimension=50)  # forces real 4x downsampling
    image.refresh_from_db()
    assert image.preview_error == ""
    with PILImage.open(image.preview_file) as preview:
        alpha = np.array(preview)[:, :, 3]
        distinct_values = set(np.unique(alpha).tolist())
        assert distinct_values <= {0, 255}, f"alpha must be fully binary, got intermediate values: {distinct_values}"


@pytest.mark.django_db
def test_generate_preview_records_error_without_raising_on_unreadable_file(farm, manager):
    image = Image(farm=farm, uploaded_by=manager)
    image.raw_file.save("not_a_raster.tif", ContentFile(b"not actually a tiff"), save=True)

    result = generate_preview(image)  # must not raise

    image.refresh_from_db()
    assert not image.preview_file
    assert image.preview_error  # non-empty, some explanation recorded
    assert result is image


# --- Cloud Optimized GeoTIFF conversion (convert_to_cog()) -----------------

@pytest.mark.django_db
def test_convert_to_cog_reencodes_raw_file_as_a_valid_cog(tmp_path, farm, manager):
    """The synthetic test raster is a plain (untiled, no-overview) GeoTIFF —
    exactly what _write_raster() produces and what a real drone
    orthomosaic export routinely looks like too. After conversion, the
    same raw_file path must open as a real COG: tiled, GDAL's own
    IMAGE_STRUCTURE/LAYOUT=COG tag present, and pixel data + georeferencing
    unchanged. Overview levels aren't asserted — this test raster (100x100)
    is too small to get any, by design (GDAL's COG driver correctly skips
    them for an image smaller than one tile); that's exercised by the
    real-world-sized standalone verification this fix was built against,
    not by this DB-backed test.
    """
    image = _make_image(tmp_path, farm, manager)
    with rasterio.open(image.raw_file.path) as before:
        assert before.is_tiled is False  # sanity: this test raster is genuinely plain, not already COG-like

    result = convert_to_cog(image)
    image.refresh_from_db()

    assert result is image
    assert image.is_cog is True
    assert image.cog_error == ""
    with rasterio.open(image.raw_file.path) as after:
        assert after.is_tiled is True
        assert after.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert after.crs.to_epsg() == 4326
        left, bottom, right, top = RASTER_BOUNDS_4326
        assert after.bounds == pytest.approx((left, bottom, right, top), abs=1e-6)


@pytest.mark.django_db
def test_convert_to_cog_records_error_without_raising_on_unreadable_file(farm, manager):
    image = Image(farm=farm, uploaded_by=manager)
    image.raw_file.save("not_a_raster.tif", ContentFile(b"not actually a tiff"), save=True)

    result = convert_to_cog(image)  # must not raise

    image.refresh_from_db()
    assert image.is_cog is False
    assert image.cog_error  # non-empty, some explanation recorded
    assert result is image
    # raw_file is left exactly as it was — conversion failure must not
    # corrupt or delete the original upload.
    assert image.raw_file.read() == b"not actually a tiff"


# --- Chunked-upload reassembly (apps/imagery/models.py's ImageUploadSession)

@pytest.mark.django_db
def test_finalize_upload_session_reassembles_chunks_byte_exact(tmp_path, farm, manager, monkeypatch):
    """Isolated from convert_to_cog()/georeferencing entirely, by design —
    see the comment on apps/dashboard/tests.py's chunked-upload end-to-end
    test for why that test can no longer assert raw byte equality once COG
    conversion runs as part of the same pipeline it exercises (COG
    re-encoding legitimately changes the byte layout — tiling, embedded
    overviews, GDAL structural metadata — while preserving what the raster
    actually represents, which that other test now checks instead, via
    rasterio, not raw bytes).

    This test asserts the thing that one no longer isolates: chunk
    reassembly itself — arbitrary chunk size, chunks stored out of arrival
    order — produces byte-for-byte identical output to the source file.
    georeference_image_task's dispatch is stubbed out so nothing rewrites
    raw_file between finalize_upload_session() returning and this
    assertion running (CELERY_TASK_ALWAYS_EAGER would otherwise run the
    full pipeline, including convert_to_cog(), synchronously and
    immediately).
    """
    from apps.imagery import tasks as imagery_tasks
    from apps.imagery.services import create_upload_session, finalize_upload_session, store_chunk

    monkeypatch.setattr(imagery_tasks.georeference_image_task, "delay", lambda *a, **kw: None)

    path = tmp_path / "source.tif"
    _write_raster(path, RASTER_BOUNDS_4326)
    file_bytes = path.read_bytes()

    chunk_size = 37  # deliberately not a clean divisor of the file size, and small enough to force many chunks
    total_chunks = -(-len(file_bytes) // chunk_size)  # ceil division, no extra import needed

    session = create_upload_session(farm, manager, "source.tif", total_chunks)
    # Stored in reverse order deliberately — store_chunk()'s own docstring
    # says arrival order must not matter, only assembly order; this
    # exercises that claim instead of just trusting it.
    for index in reversed(range(total_chunks)):
        start = index * chunk_size
        store_chunk(session, index, file_bytes[start:start + chunk_size])

    session.refresh_from_db()
    image = finalize_upload_session(session)

    assert image.raw_file.read() == file_bytes


# --- Storage lifecycle (docs/12-security-ops.md § Image storage lifecycle
# & cost policy) ------------------------------------------------------------

@pytest.mark.django_db
def test_apply_storage_lifecycle_marks_old_images_cold(tmp_path, farm, manager):
    image = _make_image(tmp_path, farm, manager)
    Image.objects.filter(id=image.id).update(created_at=timezone.now() - timedelta(days=91))

    moved = apply_storage_lifecycle(retention_days=90)

    image.refresh_from_db()
    assert moved == 1
    assert image.storage_tier == Image.STORAGE_TIER_COLD
    assert image.cold_storage_since is not None


@pytest.mark.django_db
def test_apply_storage_lifecycle_leaves_recent_images_hot(tmp_path, farm, manager):
    image = _make_image(tmp_path, farm, manager)  # created_at defaults to now — well inside the window

    moved = apply_storage_lifecycle(retention_days=90)

    image.refresh_from_db()
    assert moved == 0
    assert image.storage_tier == Image.STORAGE_TIER_HOT


@pytest.mark.django_db
def test_apply_storage_lifecycle_exempts_compliance_linked_images(tmp_path, farm, manager, block_a):
    from apps.compliance.models import DeforestationBaseline

    image = _make_image(tmp_path, farm, manager)
    Image.objects.filter(id=image.id).update(created_at=timezone.now() - timedelta(days=91))
    DeforestationBaseline.objects.create(farm_block=block_a, evidence_image=image)

    moved = apply_storage_lifecycle(retention_days=90)

    image.refresh_from_db()
    assert moved == 0
    assert image.storage_tier == Image.STORAGE_TIER_HOT  # untouched — is_compliance_exempt() protected it


@pytest.mark.django_db
def test_apply_storage_lifecycle_does_not_re_stamp_already_cold_images(tmp_path, farm, manager):
    image = _make_image(tmp_path, farm, manager)
    Image.objects.filter(id=image.id).update(created_at=timezone.now() - timedelta(days=200))
    first_pass_moved = apply_storage_lifecycle(retention_days=90)
    image.refresh_from_db()
    first_cold_since = image.cold_storage_since
    assert first_pass_moved == 1

    second_pass_moved = apply_storage_lifecycle(retention_days=90)

    image.refresh_from_db()
    assert second_pass_moved == 0
    assert image.cold_storage_since == first_cold_since  # not re-stamped on a later run


# --- delete_image() ---------------------------------------------------

@pytest.mark.django_db
def test_delete_image_removes_the_image_run_detections_and_their_files(tmp_path, farm, manager, block_a):
    from django.core.files.storage import default_storage

    image = _make_image(tmp_path, farm, manager)
    generate_preview(image, max_dimension=40)
    image.refresh_from_db()
    raw_name, preview_name = image.raw_file.name, image.preview_file.name
    assert default_storage.exists(raw_name)
    assert default_storage.exists(preview_name)

    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)
    classification = classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("healthy", 0.8)
    )
    patch_name = classification.patch_image.name
    assert patch_name and default_storage.exists(patch_name)

    delete_image(image)

    assert not Image.objects.filter(id=image.id).exists()
    assert not ImageProcessingRun.objects.filter(id=run.id).exists()
    assert not TreeDetection.objects.filter(id=detection.id).exists()
    assert not TreeHealthClassification.objects.filter(id=classification.id).exists()
    # Physical files gone too — a plain cascade DELETE never touches the
    # filesystem, so this is the specific behavior that needed a real
    # assertion, not just "the DB rows are gone."
    assert not default_storage.exists(raw_name)
    assert not default_storage.exists(preview_name)
    assert not default_storage.exists(patch_name)


@pytest.mark.django_db
def test_delete_image_refuses_when_compliance_exempt(tmp_path, farm, manager, block_a):
    from apps.compliance.models import DeforestationBaseline

    image = _make_image(tmp_path, farm, manager)
    DeforestationBaseline.objects.create(farm_block=block_a, evidence_image=image)

    with pytest.raises(ImageDeletionRefused):
        delete_image(image)

    assert Image.objects.filter(id=image.id).exists()  # untouched


@pytest.mark.django_db
def test_delete_image_keeps_confirmed_tree_created_from_its_detection(tmp_path, farm, manager, block_a):
    """A Tree confirmed from this image's detection is real farm history —
    deleting the source image must not delete the tree, only the
    provenance link to it (TreeDetection.tree is SET_NULL, but the
    TreeDetection row itself is gone via the run's CASCADE regardless).
    """
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    tree = confirm_new_candidate(detection, manager, classifier=lambda c: ("healthy", 0.8))

    delete_image(image)

    assert Tree.objects.filter(id=tree.id).exists()
    assert not TreeDetection.objects.filter(id=detection.id).exists()


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


@pytest.mark.django_db
def test_persist_clipped_block_imagery_writes_cog_preview_and_footprint(tmp_path, farm, manager, block_a):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    # The same crop run_processing() would hand the detector — this function
    # no longer re-derives its own clip via a second mask() call.
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)

    result = persist_clipped_block_imagery(run, clipped_array, clipped_transform, clip_crs)

    assert result.clip_persist_error == ""
    assert result.clipped_cog_file
    assert result.clipped_preview_file
    assert result.clipped_footprint is not None

    # The clipped COG must actually be a smaller extent than the whole
    # uploaded raster — RASTER_BOUNDS_4326 deliberately overshoots block_a,
    # same setup test_clip_to_block_crops_to_smaller_extent_than_source
    # already relies on — and must be a real, readable georeferenced file,
    # not just bytes that happen to exist.
    with rasterio.open(result.clipped_cog_file.path) as clipped:
        assert clipped.width < 100
        assert clipped.height < 100
        assert clipped.crs is not None
        clipped.read(1, window=((0, 1), (0, 1)))  # proves real pixel data, not a truncated write

    # clipped_footprint should land inside (or very close to) block_a's own
    # boundary — it's block_a.boundary intersected with the raster's real
    # coverage, reprojected to 4326 from the clipped raster's own transform.
    block_left, block_bottom, block_right, block_top = block_a.boundary.extent
    footprint_left, footprint_bottom, footprint_right, footprint_top = result.clipped_footprint.extent
    assert footprint_left >= block_left - 1e-3
    assert footprint_right <= block_right + 1e-3
    assert footprint_bottom >= block_bottom - 1e-3
    assert footprint_top <= block_top + 1e-3


@pytest.mark.django_db
def test_persist_clipped_block_imagery_records_error_without_raising(farm, manager, block_a):
    # An Image whose raw_file doesn't exist on disk — same simulated-storage-
    # problem setup test_run_processing_clip_failure_marks_run_failed_not_crash
    # already uses for clip_to_block()'s own failure path. persist_clipped_
    # block_imagery() no longer masks the raster itself, but it still needs
    # a cheap metadata-only reopen of image.raw_file (dtype/colorinterp/
    # nodata) to write a valid file, so this exact scenario still exercises
    # a real failure path — a plausible one too (the file could be removed
    # from storage in the window between clip_to_block() succeeding and this
    # running).
    image = Image.objects.create(farm=farm, uploaded_by=manager, raw_file="imagery/raw/does-not-exist.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    fake_array = np.zeros((3, 10, 10), dtype="uint8")
    fake_transform = from_bounds(101.4400, 0.5300, 101.4450, 0.5350, 10, 10)

    result = persist_clipped_block_imagery(run, fake_array, fake_transform, None)  # must not raise

    assert result.clip_persist_error != ""
    assert not result.clipped_cog_file
    assert not result.clipped_preview_file
    assert result.clipped_footprint is None


@pytest.mark.django_db
def test_run_processing_persists_clipped_imagery_alongside_the_in_memory_clip(tmp_path, farm, manager, block_a):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    run_processing(run, detector=_fake_detector_factory([]), classifier=lambda c: ("healthy", 0.9))
    run.refresh_from_db()

    assert run.status == ImageProcessingRun.STATUS_READY
    assert run.clipped_cog_file
    assert run.clipped_preview_file
    assert run.clipped_footprint is not None
    assert run.clip_persist_error == ""


def test_crop_patch_handles_a_non_uint8_clipped_array():
    """crop_patch() feeds the classifier for every matched detection
    (classify_detection() -> the real per-tree health-classification path)
    — every existing test that reaches it does so via clip_to_block() on a
    _make_image()-created raster, which is always uint8 (see _write_raster's
    default), so this specific branch (a real drone orthomosaic's raw pixel
    values, which are routinely 16-bit or float, not 8-bit) was never
    actually exercised before. Constructs the clipped_array directly
    (no file I/O needed — crop_patch() only takes an array + transform) so
    this is fast and doesn't need a real raster on disk. See
    test_generate_preview_handles_a_real_world_non_uint8_raster's docstring
    for the actual bug this — and that test — caught and fixed.
    """
    transform = from_bounds(101.4400, 0.5300, 101.4450, 0.5350, 50, 50)
    clipped_array = (np.random.default_rng(7).random((3, 50, 50)).astype("float32")) * 5000
    lon, lat = 101.4425, 0.5325  # roughly the center of the array

    patch = crop_patch(clipped_array, transform, lon, lat, patch_size_px=16)  # must not raise AttributeError

    assert patch is not None
    assert patch.mode == "RGB"
    arr = np.asarray(patch)
    assert arr.dtype == np.uint8


def test_crop_patch_reprojects_lon_lat_for_a_projected_native_crs():
    """The bug this guards against: TreeDetection.detected_location is
    always true EPSG:4326 (SRID 4326 by construction — see
    run_processing()'s `Point(lon, lat, srid=4326)`), but `transform` maps
    pixel <-> the CLIPPED RASTER'S OWN native CRS, which for a UTM-projected
    orthomosaic is meters, not degrees. Calling `~transform * (lon, lat)`
    with a true-degrees point and a meters-based transform (crs=None, the
    old behavior) produces a wildly out-of-bounds pixel coordinate — this
    test constructs exactly that scenario and confirms crop_patch() returns
    None without the fix and finds the real patch with it.
    """
    import rasterio.crs
    from rasterio.warp import transform as warp_transform

    utm_crs = rasterio.crs.CRS.from_epsg(32648)  # UTM zone 48N — meters, not degrees
    # A plausible UTM transform: 0.1m/px, origin at a real Sumatra-region UTM point.
    transform = rasterio.transform.Affine(0.1, 0.0, 370000.0, 0.0, -0.1, 60000.0)
    clipped_array = np.random.default_rng(3).integers(0, 256, size=(3, 50, 50), dtype="uint8")

    # The true lon/lat for the array's center pixel (row=25, col=25) — this
    # is what detection.py's _reproject_point_to_lonlat() would actually
    # produce and store on TreeDetection.detected_location for a detection
    # at this pixel.
    center_x_native, center_y_native = transform * (25, 25)
    (center_lon,), (center_lat,) = warp_transform(utm_crs, "EPSG:4326", [center_x_native], [center_y_native])

    # Without crs (the pre-fix behavior): treats true lon/lat degrees as if
    # they were already UTM meters — nowhere near the array's actual pixel
    # bounds, so no usable patch.
    broken = crop_patch(clipped_array, transform, center_lon, center_lat, patch_size_px=16)
    assert broken is None

    # With crs (the fix): reprojects back to native UTM meters first, lands
    # squarely in the array, finds a real patch.
    fixed = crop_patch(clipped_array, transform, center_lon, center_lat, crs=utm_crs, patch_size_px=16)
    assert fixed is not None
    assert fixed.mode == "RGB"


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
    # Regression check: this refusal used to set STATUS_FAILED without ever
    # calling _notify_processing_failure() — inconsistent with every other
    # failure path in run_processing(), and silent since this always runs
    # inside an async Celery task the triggering Manager isn't watching.
    assert Notification.objects.filter(
        user=manager, category=NotificationCategory.PROCESSING_FAILURE, link__contains=str(run.id)
    ).exists()


@pytest.mark.django_db
def test_run_processing_full_chain_matched_new_candidate_and_out_of_bounds(tmp_path, farm, manager, block_a, tree_in_block_a):
    image = _make_image(tmp_path, farm, manager)

    inside_matched = (tree_in_block_a.location.x, tree_in_block_a.location.y, 0.95)
    inside_new = (101.4440, 0.5340, 0.88)  # inside block_a, nowhere near tree_in_block_a
    outside_block = (101.0, 0.0, 0.99)  # nowhere near block_a at all — must be filtered out

    fake_detector = _fake_detector_factory([inside_matched, inside_new, outside_block])
    fake_classifier = lambda crop: ("yellow", 0.92)  # noqa: E731 — test-local, fine

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

    # Every detection gets classified in the main pipeline now, not just
    # matched ones — see run_processing()'s own docstring for why: a fresh
    # block's first-ever pass has no existing trees to match against, so
    # "matched only" meant nothing was ever classified until a human
    # confirmed each new_candidate one at a time.
    assert TreeHealthClassification.objects.filter(tree_detection=matched[0]).exists()
    assert TreeHealthClassification.objects.filter(tree_detection=new_candidates[0]).exists()

    # Regression: Tree.current_health used to never get written from a real
    # classification result anywhere — a matched detection's tree stayed at
    # the model default ("unknown") forever, which is why the Farm Map
    # rendered every tree gray regardless of its real health.
    # classify_detection() must sync the ALREADY-matched tree directly
    # (unlike a new_candidate, which has no Tree yet until confirmed).
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.current_health == "yellow"

    # progress_current/progress_total reflect the LAST stage that ran
    # (classification, the final one before STATUS_READY) — 2 detections,
    # both classified, so progress finishes at 2/2. progress_percent is
    # None once the run is done (ready/failed aren't "in progress" states
    # — see ImageProcessingRun.progress_percent's own docstring).
    assert run.progress_current == 2
    assert run.progress_total == 2
    assert run.progress_percent is None


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
    assert Notification.objects.filter(user=manager, category=NotificationCategory.PROCESSING_FAILURE).exists()


@pytest.mark.django_db
def test_run_processing_detection_failure_marks_run_failed_not_stuck(tmp_path, farm, manager, block_a):
    """Regression test: the detector() call had no error handling at all
    before this fix — an exception here used to propagate straight out of
    run_processing(), leaving the run stuck at STATUS_DETECTING forever
    with no error_message and no notification. A real (non-stub) detection
    model can legitimately raise (OOM, corrupt weights, unexpected input
    shape), so this isn't a hypothetical path.
    """
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    def broken_detector(*args, **kwargs):
        raise RuntimeError("model weights failed to load")

    run_processing(run, detector=broken_detector, classifier=lambda c: ("healthy", 0.9))
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_FAILED
    assert "detection failed" in run.error_message.lower()
    assert "model weights failed to load" in run.error_message
    assert Notification.objects.filter(user=manager, category=NotificationCategory.PROCESSING_FAILURE).exists()


@pytest.mark.django_db
def test_run_processing_classification_failure_marks_run_failed_but_keeps_detections(
    tmp_path, farm, manager, block_a, tree_in_block_a
):
    """Regression test: classify_detection() calls inside run_processing()'s
    main loop had no error handling either — same "stuck forever, silent"
    bug as the detection stage, but with an extra wrinkle: detections are
    already written to the DB by the time classification runs, and a
    classification failure shouldn't erase them (they're real matches; only
    the health label is missing) — same trade-off confirm_new_candidate()
    already makes, for the same reason.
    """
    image = _make_image(tmp_path, farm, manager)
    matched_point = (tree_in_block_a.location.x, tree_in_block_a.location.y, 0.95)
    fake_detector = _fake_detector_factory([matched_point])
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    def broken_classifier(patch):
        raise RuntimeError("classifier crashed on this patch")

    run_processing(run, detector=fake_detector, classifier=broken_classifier)
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_FAILED
    assert "classification failed" in run.error_message.lower()
    assert Notification.objects.filter(user=manager, category=NotificationCategory.PROCESSING_FAILURE).exists()
    # The detection itself is kept — only classification failed.
    assert TreeDetection.objects.filter(processing_run=run, match_status=TreeDetection.MATCH_MATCHED).count() == 1


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
    tree = confirm_new_candidate(detection, manager, classifier=lambda crop: ("yellow", 0.9))
    detection.refresh_from_db()
    tree.refresh_from_db()
    assert tree.source == Tree.SOURCE_DRONE_DETECTED
    assert tree.farm_block_id == block_a.id
    assert detection.tree_id == tree.id
    assert TreeHealthClassification.objects.filter(tree_detection=detection, health_label="yellow").exists()
    # Regression: Tree.current_health used to never get written from a real
    # classification result anywhere — every tree stayed at the model
    # default ("unknown") forever, which is why the Farm Map rendered every
    # tree gray regardless of its real health. confirm_new_candidate() must
    # sync it from the classification it just created (this detection had
    # no Tree yet when run_processing() would normally do this sync).
    assert tree.current_health == "yellow"


@pytest.mark.django_db
def test_classify_detection_is_idempotent_does_not_duplicate_or_crash(tmp_path, farm, manager, block_a):
    """Guards the exact bug confirm_new_candidate() used to be silently
    exposed to: TreeHealthClassification is a OneToOneField on
    tree_detection — calling classify_detection() a second time for an
    already-classified detection must return the existing row, not attempt
    a second insert (which would raise IntegrityError, previously masked
    by confirm_new_candidate()'s broad except-and-ignore).
    """
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)

    first = classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("yellow", 0.5)
    )
    second = classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("healthy", 0.9)
    )

    assert first.id == second.id
    assert second.health_label == "yellow"  # the second call's classifier result is never used
    assert TreeHealthClassification.objects.filter(tree_detection=detection).count() == 1


@pytest.mark.django_db
def test_classify_detection_saves_patch_image_when_patch_is_usable(tmp_path, farm, manager, block_a):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),  # well inside the raster
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)

    classification = classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("healthy", 0.8)
    )
    assert classification.patch_image  # truthy FieldFile means a real file was saved
    assert classification.patch_image.name.endswith(".jpg")


@pytest.mark.django_db
def test_classify_detection_leaves_patch_image_empty_when_patch_is_unusable(monkeypatch, tmp_path, farm, manager, block_a):
    """crop_patch() returning None (detection too close to the clipped
    array's edge) must still write a TreeHealthClassification — routed to
    review via the existing "unusable patch" convention — but with no
    patch_image, since there's no real crop to show. Monkeypatches
    crop_patch() directly rather than trying to engineer a real edge-of-
    raster coordinate, which would be fragile to get exactly right.
    """
    import apps.imagery.services as imagery_services

    monkeypatch.setattr(imagery_services, "crop_patch", lambda *args, **kwargs: None)

    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)

    classification = classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("unknown", 0.8)
    )
    assert classification.health_label == "unknown"
    assert classification.confidence == 0.0  # unusable patch -> forced to review, never a real classifier call
    assert not classification.patch_image


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
    # georeference_image_task (apps/imagery/tasks.py) chains generate_preview()
    # right after a successful georeference — this is the real upload path
    # a Manager actually uses, so it's the strongest place to confirm that
    # chaining actually happens, not just that generate_preview() works in
    # isolation (covered separately above).
    assert image.preview_file
    # Same reasoning for convert_to_cog(), which runs even earlier in the
    # same task (before georeferencing) — see the dedicated convert_to_cog
    # tests above for the detailed COG-structure assertions.
    assert image.is_cog is True


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
        services_module, "get_detector", lambda **kwargs: _fake_detector_factory([(101.4410, 0.5310, 0.9)])
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


# --- ML model loading (apps/imagery/ml/*.py) --------------------------------
#
# get_detector()/get_classifier() themselves (the settings.DETECTION_MODEL_
# PATH/CLASSIFICATION_MODEL_PATH branch) were already exercised implicitly
# by every run_processing() test above via dependency injection. torch/
# torchvision are now unconditional real dependencies (both detection and
# classification have real trained checkpoints — see each module's own
# docstring), so the tests below exercise the real `torch.load()`/
# `nn.Module`/`state_dict` path directly, not a stubbed-out library.


# --- Crown detector (apps/imagery/ml/detection.py, real torchvision Faster
#     R-CNN) --------------------------------------------------------------
#
# Unlike the health classifier (a single softmax head — easy to make fully
# deterministic for a test by zeroing fc.weight and setting a target-class
# bias), Faster R-CNN's output depends on the region-proposal network
# finding candidate regions on a from-scratch/untrained backbone's feature
# maps first — there's no equivalent one-line trick to force a specific,
# guaranteed detection count out of an untrained model. These tests
# therefore cover the parts that ARE deterministic and don't depend on the
# actual 331MB checkpoint file being present: architecture construction,
# the pure geometry/reprojection helpers, and the tiling loop's contract
# (runs without error over a multi-tile input; whatever it returns, if
# anything, satisfies the (lon, lat, confidence) shape with confidence
# above threshold and lon/lat in valid geographic ranges).


def test_build_crown_detector_sets_correct_foreground_class_count():
    from apps.imagery.ml.detection import _build_crown_detector

    model = _build_crown_detector(num_classes=2)  # background + "oilpalm"
    assert model.roi_heads.box_predictor.cls_score.out_features == 2


def test_box_iou_identical_boxes_is_one_disjoint_boxes_is_zero():
    from apps.imagery.ml.detection import _box_iou

    box = (0.0, 0.0, 10.0, 10.0)
    assert _box_iou(box, box) == pytest.approx(1.0)
    disjoint = (100.0, 100.0, 110.0, 110.0)
    assert _box_iou(box, disjoint) == pytest.approx(0.0)


def test_world_nms_drops_the_lower_scoring_of_two_overlapping_boxes():
    from apps.imagery.ml.detection import _world_nms

    a = ((0.0, 0.0, 10.0, 10.0), 0.9)
    b = ((1.0, 1.0, 11.0, 11.0), 0.4)  # heavily overlapping with a, lower score
    c = ((100.0, 100.0, 110.0, 110.0), 0.5)  # disjoint from both — always kept
    kept = _world_nms([a, b, c], iou_threshold=0.3)
    kept_scores = sorted(score for _box, score in kept)
    assert kept_scores == [0.5, 0.9]  # b (0.4, overlapping, lower-scored) is dropped


def test_reproject_point_to_lonlat_is_identity_for_already_geographic_crs():
    import rasterio.crs

    from apps.imagery.ml.detection import _reproject_point_to_lonlat

    lon, lat = _reproject_point_to_lonlat(101.44, 0.535, rasterio.crs.CRS.from_epsg(4326))
    assert (lon, lat) == (101.44, 0.535)


def test_reproject_point_to_lonlat_converts_projected_meters_to_degrees():
    """The bug this guards against: silently treating a UTM point's meters
    as if they were already degrees would put a detection thousands of
    kilometers from where it actually is — this must never round-trip
    unchanged for a real projected CRS.
    """
    import rasterio.crs

    from apps.imagery.ml.detection import _reproject_point_to_lonlat

    # A plausible EPSG:32648 (UTM zone 48N, covers Sumatra/Riau) point.
    lon, lat = _reproject_point_to_lonlat(370000.0, 60000.0, rasterio.crs.CRS.from_epsg(32648))
    assert -180 <= lon <= 180
    assert -90 <= lat <= 90
    assert (lon, lat) != (370000.0, 60000.0)


def test_to_uint8_rgb_hwc_converts_band_first_to_pixel_last_uint8():
    from apps.imagery.ml.detection import _to_uint8_rgb_hwc

    band_first = np.zeros((3, 4, 5), dtype="uint8")
    band_first[0] = 255  # red channel fully on
    hwc = _to_uint8_rgb_hwc(band_first)
    assert hwc.shape == (4, 5, 3)
    assert hwc.dtype == np.uint8
    assert (hwc[..., 0] == 255).all()
    assert (hwc[..., 1] == 0).all()


def test_load_real_detector_tiling_and_reprojection_contract(tmp_path):
    """No real checkpoint needed — builds a small, deterministically-seeded
    (but genuinely untrained) model through _build_crown_detector() itself,
    saves it in the exact {"model_state", ...} shape
    train_from_local_dataset.py actually produces, and confirms the real
    _load_real_detector() loads it and runs its full tiling/NMS/
    reprojection pipeline over a multi-tile input without error, with every
    returned detection satisfying the (lon, lat, confidence) contract.
    """
    import rasterio.crs
    import torch

    from apps.imagery.ml.detection import (
        DETECTION_SCORE_THRESHOLD,
        DETECTION_TILE_SIZE_PX,
        _build_crown_detector,
        _load_real_detector,
    )

    torch.manual_seed(0)
    model = _build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    # target_gsd_m=None: this test's Affine(0.0001, ...) is an arbitrary
    # coordinate-math fixture, not a real ground sample distance — with GSD
    # normalization defaulted on, _native_gsd_m() would read it as ~11m/px
    # (0.0001 degrees), wildly mismatched against DETECTION_TARGET_GSD_M's
    # 6cm/px, and even after _GSD_RESAMPLE_MAX_FACTOR's clamp that's still
    # an unwanted 10x upsample this test isn't about. Explicitly disabled
    # so this test exercises tiling/reprojection at the array's real size,
    # which is what it's actually asserting about.
    detector = _load_real_detector(str(checkpoint_path), target_gsd_m=None)

    # Deliberately larger than one tile on both axes, so the tiling loop's
    # multi-row/multi-column path (not just the single-tile edge case)
    # actually runs.
    height, width = DETECTION_TILE_SIZE_PX + 200, DETECTION_TILE_SIZE_PX + 150
    clipped_array = np.random.default_rng(0).integers(0, 256, size=(3, height, width), dtype=np.uint8)
    transform = rasterio.transform.Affine(0.0001, 0.0, 101.4400, 0.0, -0.0001, 0.5350)

    # A real rasterio CRS object — the actual type clip_to_block() always
    # hands the detector (src.crs), not the plain string a bare "EPSG:4326"
    # would be. Caught by this test itself during development: passing a
    # string here raised AttributeError inside _reproject_point_to_lonlat()
    # ('str' object has no attribute 'to_epsg'), which is exactly the kind
    # of input-shape mismatch worth getting right in the test, not just in
    # production code.
    crs = rasterio.crs.CRS.from_epsg(4326)
    detections = detector(clipped_array, transform, crs)  # must not raise

    for lon, lat, confidence in detections:
        assert -180 <= lon <= 180
        assert -90 <= lat <= 90
        assert confidence >= DETECTION_SCORE_THRESHOLD


def test_load_real_detector_handles_a_block_smaller_than_one_tile(tmp_path):
    """Edge case the tiling loop must not choke on: a small block whose
    clipped array is smaller than DETECTION_TILE_SIZE_PX in both dimensions
    — exactly one partial tile, not zero.
    """
    import rasterio.crs
    import torch

    from apps.imagery.ml.detection import _build_crown_detector, _load_real_detector

    torch.manual_seed(0)
    model = _build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_small.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    # target_gsd_m=None — see the previous test's comment for why (this
    # array is already smaller than one tile; GSD normalization would
    # upsample it further, defeating the point of this specific case).
    detector = _load_real_detector(str(checkpoint_path), target_gsd_m=None)
    clipped_array = np.random.default_rng(1).integers(0, 256, size=(3, 200, 150), dtype=np.uint8)
    transform = rasterio.transform.Affine(0.0001, 0.0, 101.4400, 0.0, -0.0001, 0.5350)

    detections = detector(clipped_array, transform, rasterio.crs.CRS.from_epsg(4326))  # must not raise
    assert isinstance(detections, list)


def test_load_real_detector_threads_custom_iou_threshold_into_world_nms(tmp_path, monkeypatch):
    """FarmBlock.effective_crown_detection_iou_threshold() flows into
    get_detector() -> _load_real_detector() -> the detector closure's own
    _world_nms() call — this confirms the plumbing actually carries a
    non-default value through, not just that _world_nms() itself honors
    whatever threshold it's given directly (already covered by
    test_world_nms_drops_the_lower_scoring_of_two_overlapping_boxes above).
    """
    import torch

    from apps.imagery.ml import detection as detection_module

    torch.manual_seed(0)
    model = detection_module._build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_iou.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    seen_thresholds = []
    real_world_nms = detection_module._world_nms

    def spy_world_nms(boxes_with_scores, iou_threshold):
        seen_thresholds.append(iou_threshold)
        return real_world_nms(boxes_with_scores, iou_threshold)

    monkeypatch.setattr(detection_module, "_world_nms", spy_world_nms)

    # target_gsd_m=None — this test is about IoU-threshold plumbing, not
    # GSD normalization; see the tiling test's comment for why the
    # arbitrary Affine(0.0001, ...) fixture needs it disabled explicitly.
    detector = detection_module._load_real_detector(str(checkpoint_path), iou_threshold=0.85, target_gsd_m=None)
    clipped_array = np.random.default_rng(2).integers(0, 256, size=(3, 200, 150), dtype=np.uint8)
    transform = rasterio.transform.Affine(0.0001, 0.0, 101.4400, 0.0, -0.0001, 0.5350)
    detector(clipped_array, transform, rasterio.crs.CRS.from_epsg(4326))

    assert seen_thresholds == [0.85]


def test_load_real_detector_reports_real_tile_progress(tmp_path):
    """on_tile_progress(tiles_done, tiles_total) — what
    ImageProcessingRun.progress_current/progress_total (see
    apps/imagery/services.py's run_processing()) is actually driven by —
    must be called once per tile, with a correct, unchanging total and a
    strictly increasing count ending at that total. `expected_total` is
    computed the same way _load_real_detector()'s own tiling loop computes
    row/column offsets (range(0, dim, stride)), from a real, fixed array
    size — cross-checking the callback's actual call count against that
    arithmetic, not just asserting "some positive number of calls."
    """
    import torch

    from apps.imagery.ml.detection import (
        DETECTION_TILE_OVERLAP_PX,
        DETECTION_TILE_SIZE_PX,
        _build_crown_detector,
        _load_real_detector,
    )

    torch.manual_seed(0)
    model = _build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_progress.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    # target_gsd_m=None — this test's expected_total is computed from the
    # array's OWN pixel size; GSD normalization resizing it first would
    # invalidate that arithmetic. See the tiling test's comment for the
    # full reasoning.
    calls = []
    detector = _load_real_detector(
        str(checkpoint_path), on_tile_progress=lambda done, total: calls.append((done, total)), target_gsd_m=None
    )

    height = DETECTION_TILE_SIZE_PX
    width = DETECTION_TILE_SIZE_PX + 200
    clipped_array = np.random.default_rng(4).integers(0, 256, size=(3, height, width), dtype=np.uint8)
    transform = rasterio.transform.Affine(0.0001, 0.0, 101.4400, 0.0, -0.0001, 0.5350)
    detector(clipped_array, transform, rasterio.crs.CRS.from_epsg(4326))

    stride = DETECTION_TILE_SIZE_PX - DETECTION_TILE_OVERLAP_PX
    expected_total = len(range(0, height, stride)) * len(range(0, width, stride))
    assert len(calls) == expected_total
    assert [c[1] for c in calls] == [expected_total] * expected_total  # total never changes mid-run
    assert [c[0] for c in calls] == list(range(1, expected_total + 1))  # done counts up 1, 2, ..., total


# --- Ground-sample-distance normalization (apps/imagery/ml/detection.py) --
#
# Added after a real report, with real detections attached: the detector
# was firing on individual palm fronds instead of whole crowns. Root cause,
# confirmed against real numbers (see BUILD_STATUS.md's matching update
# and this module's own docstring): this project's real orthomosaic
# (1.843cm/px) is 2-5x finer than the crown-detector training data's own
# implied ground sample distance (its annotated crown boxes average
# ~100x100px, which for any realistic palm crown diameter is 4-9cm/px).


def test_native_gsd_m_is_a_direct_passthrough_for_a_projected_crs():
    import rasterio.crs

    from apps.imagery.ml.detection import _native_gsd_m

    # EPSG:32748 (UTM zone 48S) — the actual CRS this project's real
    # uploaded orthomosaic came in as. Projected CRS pixel scale already
    # IS meters, no conversion needed.
    transform = rasterio.transform.Affine(0.01843, 0.0, 500000.0, 0.0, -0.01843, 9800000.0)
    crs = rasterio.crs.CRS.from_epsg(32748)
    assert _native_gsd_m(transform, crs) == pytest.approx(0.01843)


def test_native_gsd_m_converts_geographic_degrees_to_meters():
    import rasterio.crs

    from apps.imagery.ml.detection import _native_gsd_m

    transform = rasterio.transform.Affine(0.00001, 0.0, 101.44, 0.0, -0.00001, 0.535)
    crs = rasterio.crs.CRS.from_epsg(4326)
    # ~111,320 m/degree at the equator, negligible cos(latitude) correction
    # this close to it.
    assert _native_gsd_m(transform, crs) == pytest.approx(1.1132, abs=0.001)


def test_load_real_detector_resamples_before_tiling_when_native_gsd_is_too_fine(tmp_path, monkeypatch):
    """The actual bug report, reproduced with real numbers: a clipped
    array at this project's real native GSD (1.843cm/px) handed to the
    detector with the default target GSD (6cm/px) must be resampled down
    before tiling — spied on by capturing the pixel shape of every tensor
    actually fed to the model, since that's the one thing that can't lie
    about whether resampling really happened.
    """
    import torch

    from apps.imagery.ml import detection as detection_module

    torch.manual_seed(0)
    model = detection_module._build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_gsd.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    seen_tile_shapes = []
    real_from_numpy = torch.from_numpy

    def spy_from_numpy(arr):
        seen_tile_shapes.append(arr.shape)
        return real_from_numpy(arr)

    monkeypatch.setattr(torch, "from_numpy", spy_from_numpy)

    detector = detection_module._load_real_detector(str(checkpoint_path))  # target_gsd_m defaults to 0.06
    native_transform = rasterio.transform.Affine(0.01843, 0.0, 500000.0, 0.0, -0.01843, 9800000.0)
    clipped_array = np.random.default_rng(5).integers(0, 256, size=(3, 2000, 2000), dtype=np.uint8)
    crs = rasterio.crs.CRS.from_epsg(32748)
    detector(clipped_array, native_transform, crs)

    # resample_factor = 0.06 / 0.01843 ~= 3.26 -> a 2000px array becomes
    # ~614px, which is smaller than one 1024px tile: exactly one tile, not
    # the 3x3=9 tiles a native-resolution 2000px array would need.
    assert len(seen_tile_shapes) == 1
    assert seen_tile_shapes[0][1] < detection_module.DETECTION_TILE_SIZE_PX
    assert seen_tile_shapes[0][2] < detection_module.DETECTION_TILE_SIZE_PX


def test_load_real_detector_skips_resampling_when_gsd_already_matches(tmp_path, monkeypatch):
    import torch

    from apps.imagery.ml import detection as detection_module

    torch.manual_seed(0)
    model = detection_module._build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_gsd_match.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    seen_tile_shapes = []
    real_from_numpy = torch.from_numpy

    def spy_from_numpy(arr):
        seen_tile_shapes.append(arr.shape)
        return real_from_numpy(arr)

    monkeypatch.setattr(torch, "from_numpy", spy_from_numpy)

    native_gsd = 0.01843
    detector = detection_module._load_real_detector(str(checkpoint_path), target_gsd_m=native_gsd)
    native_transform = rasterio.transform.Affine(native_gsd, 0.0, 500000.0, 0.0, -native_gsd, 9800000.0)
    clipped_array = np.random.default_rng(6).integers(0, 256, size=(3, 2000, 2000), dtype=np.uint8)
    crs = rasterio.crs.CRS.from_epsg(32748)
    detector(clipped_array, native_transform, crs)

    # No resampling -> full-resolution 1024px tiles, a 3x3 grid for a
    # 2000px array (stride 896: offsets 0, 896, 1792 in each dimension —
    # range(0, 2000, 896) has 3 elements, not 2; a 2000px span needs a
    # third, mostly-overlapping tile to reach the far edge).
    assert len(seen_tile_shapes) == 9
    assert seen_tile_shapes[0][1] == detection_module.DETECTION_TILE_SIZE_PX


def test_load_real_detector_clamps_a_wildly_wrong_gsd_estimate_instead_of_exploding(tmp_path, monkeypatch):
    """Regression test for a bug caught before it shipped: an unbounded
    resample_factor = target_gsd_m / native_gsd_m, fed a native GSD wildly
    different from the target (e.g. a geographic-CRS transform whose
    pixel scale doesn't mean what it's assumed to — exactly what every
    OTHER detector test's Affine(0.0001, ...) fixture produces, ~11m/px,
    against a 6cm/px target), computed a >180x upsample — attempting to
    allocate a catastrophically large array. _GSD_RESAMPLE_MAX_FACTOR
    clamps this; this test proves the clamp is real, not just documented,
    by using exactly that dangerous input and asserting the actual
    resulting tile size stays bounded rather than reaching for a real
    (slow, memory-heavy) multi-gigapixel resize to prove a negative.
    """
    import torch

    from apps.imagery.ml import detection as detection_module

    torch.manual_seed(0)
    model = detection_module._build_crown_detector(num_classes=2)
    checkpoint_path = tmp_path / "fake_detector_gsd_clamp.pt"
    torch.save({"model_state": model.state_dict(), "optimizer_state": {}, "epoch": 0}, checkpoint_path)

    detector = detection_module._load_real_detector(str(checkpoint_path))  # target_gsd_m defaults to 0.06
    # The exact dangerous fixture every other detector test in this file
    # deliberately avoids feeding to a GSD-normalizing detector.
    dangerous_transform = rasterio.transform.Affine(0.0001, 0.0, 101.4400, 0.0, -0.0001, 0.5350)
    crs = rasterio.crs.CRS.from_epsg(4326)

    small_array = np.random.default_rng(7).integers(0, 256, size=(3, 100, 100), dtype=np.uint8)
    # Must complete quickly and without raising (MemoryError/timeout) — the
    # real assertion IS that this returns at all in a test-appropriate
    # amount of time.
    detections = detector(small_array, dangerous_transform, crs)
    assert isinstance(detections, list)


# --- Health classifier (apps/imagery/ml/classification.py, real torch model) -
#
# Same reasoning as the detector tests above for why these don't depend on
# the actual 44MB trained_models/oil_palm_health_classifier.pth file being
# present (that file's own real-world accuracy is a training-pipeline
# concern, not this adapter code's) — instead each test builds a tiny
# synthetic checkpoint with the exact same architecture/metadata shape, via
# _build_health_model() itself, so a shape mismatch here would fail the
# same way a real one would.


def _fake_health_checkpoint(tmp_path, class_names, target_idx, patch_size=8, input_channels=10, filename="fake.pth"):
    """Builds a real (tiny, untrained-in-any-meaningful-sense) checkpoint
    through the same _build_health_model() the real loader uses, then
    forces its output to be deterministic regardless of input content: zero
    every fc weight so the conv trunk's actual features contribute nothing,
    and set the target class's fc bias high enough that softmax puts
    >99.99% of the probability mass there. This tests this project's own
    adapter code (checkpoint metadata handling, resize, VI-stacking,
    label-mapping) without depending on what the real trained weights
    happen to predict for an arbitrary input.
    """
    import torch

    from apps.imagery.ml.classification import _build_health_model

    model = _build_health_model(num_classes=len(class_names), in_channels=input_channels)
    with torch.no_grad():
        model.fc.weight.zero_()
        model.fc.bias.zero_()
        model.fc.bias[target_idx] = 20.0
    path = tmp_path / filename
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "class_names": class_names,
            "patch_size": patch_size,
            "input_channels": input_channels,
        },
        path,
    )
    return str(path)


def test_build_health_model_widens_conv1_to_ten_channels_and_matches_class_count():
    from apps.imagery.ml.classification import _build_health_model

    model = _build_health_model(num_classes=5, in_channels=10)
    assert model.conv1.in_channels == 10
    assert model.conv1.out_channels == 64  # unchanged from stock ResNet-18
    assert model.fc.out_features == 5


# --- Exposure normalization (apps/imagery/ml/classification.py's
# normalize_exposure()) — ported from the ml-repo's own fix for a real
# per-channel color-balance mismatch found against a real Jonggol farm
# orthomosaic. Pure numpy, no torch/Django dependency for the function
# itself, matching vegetation_indices.py's own testing style.

def test_normalize_exposure_shifts_a_brighter_crop_toward_the_reference_mean():
    from apps.imagery.ml.classification import REFERENCE_MEAN_RGB, normalize_exposure

    # A crop noticeably brighter than the reference — the same shape of
    # mismatch reported for real Jonggol crops (~130-150 mean RGB against
    # this reference's ~79-106) — should be pulled toward the reference,
    # not left untouched or over-corrected past it.
    bright = np.full((32, 32, 3), (140, 150, 130), dtype=np.uint8)
    corrected = normalize_exposure(bright)

    assert corrected.dtype == np.uint8
    assert corrected.shape == bright.shape
    reference = np.asarray(REFERENCE_MEAN_RGB)
    original_gap = np.abs(bright.reshape(-1, 3).mean(axis=0) - reference)
    corrected_gap = np.abs(corrected.reshape(-1, 3).mean(axis=0) - reference)
    assert np.all(corrected_gap < original_gap)


def test_normalize_exposure_clips_gain_for_a_near_black_crop():
    from apps.imagery.ml.classification import _EXPOSURE_GAIN_CLIP, normalize_exposure

    # An unclipped gain (reference / ~1) would be roughly 80-100x — the clip
    # must cap this rather than manufacturing color out of near-zero signal.
    near_black = np.full((16, 16, 3), 1, dtype=np.uint8)
    corrected = normalize_exposure(near_black)
    assert np.all(corrected <= int(_EXPOSURE_GAIN_CLIP[1]) + 1)  # gain capped at 3.0x


def test_normalize_exposure_rejects_non_uint8_input():
    from apps.imagery.ml.classification import normalize_exposure

    with pytest.raises(ValueError):
        normalize_exposure(np.zeros((8, 8, 3), dtype=np.float32))


def _spy_on_normalize_exposure(monkeypatch):
    """Wraps the real normalize_exposure() with a call-counting spy —
    verifying the *wiring* (was it called at all for this crop) is what
    matters for these tests, not re-deriving the exact corrected pixel
    values normalize_exposure()'s own tests above already cover.
    """
    import apps.imagery.ml.classification as classification_module

    calls = []
    real_fn = classification_module.normalize_exposure

    def spy(rgb, *args, **kwargs):
        calls.append(True)
        return real_fn(rgb, *args, **kwargs)

    monkeypatch.setattr(classification_module, "normalize_exposure", spy)
    return calls


def test_load_real_classifier_applies_exposure_normalization_when_enabled(tmp_path, monkeypatch):
    from apps.imagery.ml.classification import _load_real_classifier

    calls = _spy_on_normalize_exposure(monkeypatch)
    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=0)
    classifier = _load_real_classifier(checkpoint_path, normalize_exposure_enabled=True)

    classifier(PILImage.new("RGB", (8, 8), color=(140, 150, 130)))
    assert len(calls) == 1


def test_load_real_classifier_skips_exposure_normalization_when_disabled(tmp_path, monkeypatch):
    from apps.imagery.ml.classification import _load_real_classifier

    calls = _spy_on_normalize_exposure(monkeypatch)
    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=0)
    classifier = _load_real_classifier(checkpoint_path, normalize_exposure_enabled=False)

    classifier(PILImage.new("RGB", (8, 8), color=(140, 150, 130)))
    assert len(calls) == 0


def test_get_classifier_reads_normalize_exposure_from_settings(tmp_path, settings, monkeypatch):
    """The real production wiring — get_classifier() called with no
    arguments, exactly like classify_detection() calls it, must forward
    settings.CLASSIFICATION_NORMALIZE_EXPOSURE (this project's .env sets it
    true specifically for this deployment's real Jonggol imagery — see
    classification.py's own module docstring) into _load_real_classifier()
    rather than silently defaulting to off regardless of the setting.
    """
    from apps.imagery.ml.classification import get_classifier

    calls = _spy_on_normalize_exposure(monkeypatch)
    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=0)
    settings.CLASSIFICATION_MODEL_PATH = checkpoint_path
    settings.CLASSIFICATION_NORMALIZE_EXPOSURE = True

    classifier = get_classifier()
    classifier(PILImage.new("RGB", (8, 8), color=(140, 150, 130)))
    assert len(calls) == 1


def test_load_real_classifier_maps_known_class_to_project_label(tmp_path):
    """SMALL is an explicit mapping to "unknown" (not a fall-through like
    DEAD/GRASS below) — see classification.py's own docstring for the
    product decision: "small" was retired as its own app-level label, but
    the model's real confidence should still be kept, unlike the DEAD/GRASS
    0.0-confidence review-routing case test_load_real_classifier_routes_
    unmapped_class_to_review_not_a_guess covers.
    """
    from apps.imagery.ml.classification import _load_real_classifier

    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=1)  # SMALL
    classifier = _load_real_classifier(checkpoint_path)

    label, confidence = classifier(PILImage.new("RGB", (64, 64)))
    assert label == "unknown"
    assert confidence > 0.999  # real confidence kept, not forced to 0.0 like an unmapped class


def test_load_real_classifier_routes_unmapped_class_to_review_not_a_guess(tmp_path):
    """DEAD and GRASS are real predictions from the trained model but
    aren't part of this project's own 3-class health scheme (HEALTH_LABELS)
    — see classification.py's own docstring for the confirmed product
    decision behind this. Must never be silently forced into one of the
    three real labels.
    """
    from apps.imagery.ml.classification import _load_real_classifier

    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=3)  # DEAD
    classifier = _load_real_classifier(checkpoint_path)

    label, confidence = classifier(PILImage.new("RGB", (64, 64)))
    assert label == "unknown"
    assert confidence == 0.0  # always routed to review — same convention as stub_classifier()


def test_load_real_classifier_resizes_a_crop_that_does_not_match_patch_size(tmp_path):
    """crop_patch() can return a smaller-than-requested array near a
    clipped raster's edge (see its own docstring) — the real loader must
    resize rather than crash or silently misalign the VI computation.
    """
    from apps.imagery.ml.classification import _load_real_classifier

    class_names = ["HEALTHY", "SMALL", "YELLOW", "DEAD", "GRASS"]
    checkpoint_path = _fake_health_checkpoint(tmp_path, class_names, target_idx=0, patch_size=8)
    classifier = _load_real_classifier(checkpoint_path)

    label, confidence = classifier(PILImage.new("RGB", (5, 3)))  # neither dimension matches patch_size=8
    assert label == "healthy"
    assert confidence > 0.999


# --- Classification patch GSD normalization (apps/imagery/ml/
# classification.py's classification_native_patch_size_px(), and its
# wiring into apps/imagery/services.py's classify_detection()) — the
# classification-side counterpart to detection.py's own target-GSD
# resampling. Reported directly, via a screenshot of the review queue
# showing every classification patch as extreme close-up leaf/frond
# texture (not a crown) after re-processing a real block: crop_patch()
# always cropped a fixed CLASSIFICATION_PATCH_SIZE_PX pixels straight out
# of the native-resolution clipped array, so a 96px window at this
# project's real ~1.843cm/px Jonggol imagery covered only ~1.77m of real
# ground — a small fraction of one frond, let alone a whole crown.

def test_classification_native_patch_size_px_upscales_for_a_finer_than_target_gsd():
    from apps.imagery.ml.classification import classification_native_patch_size_px

    # Real Jonggol-shaped case: native GSD much finer than the 15cm target
    # -> a much larger native pixel window is needed to cover the same
    # real-world ground the model was trained on.
    size = classification_native_patch_size_px(96, native_gsd_m=0.01843, target_gsd_m=0.15)
    assert size == round(96 * 0.15 / 0.01843)
    assert size > 96 * 5  # roughly an 8x scale-up, not a rounding-noise difference


def test_classification_native_patch_size_px_unchanged_when_gsd_already_matches():
    from apps.imagery.ml.classification import classification_native_patch_size_px

    assert classification_native_patch_size_px(96, native_gsd_m=0.15, target_gsd_m=0.15) == 96


def test_classification_native_patch_size_px_clamps_an_extreme_ratio():
    from apps.imagery.ml.classification import (
        _CLASSIFICATION_GSD_RESAMPLE_MAX_FACTOR,
        classification_native_patch_size_px,
    )

    # An unrealistically coarse native GSD would otherwise shrink the
    # requested crop toward (or past) zero pixels — the clamp must cap the
    # DOWNSCALE side of the ratio too, not just the upscale side.
    size = classification_native_patch_size_px(96, native_gsd_m=100.0, target_gsd_m=0.15)
    assert size == max(1, round(96 / _CLASSIFICATION_GSD_RESAMPLE_MAX_FACTOR))


def test_classification_native_patch_size_px_unchanged_without_gsd_info():
    from apps.imagery.ml.classification import classification_native_patch_size_px

    assert classification_native_patch_size_px(96, native_gsd_m=0, target_gsd_m=0.15) == 96
    assert classification_native_patch_size_px(96, native_gsd_m=0.15, target_gsd_m=0) == 96


@pytest.mark.django_db
def test_classify_detection_requests_a_gsd_scaled_native_crop(tmp_path, farm, manager, block_a, monkeypatch):
    """Wiring test: classify_detection() must actually ask crop_patch() for
    a GSD-adjusted pixel count, not the raw CLASSIFICATION_PATCH_SIZE_PX,
    for a raster whose native GSD doesn't match the target. Spies on
    services.crop_patch() (imported into the module under test) rather than
    re-deriving the exact expected pixel count a second time here — that
    arithmetic is exactly what classification_native_patch_size_px()'s own
    tests above already cover.
    """
    import apps.imagery.services as services_module

    image = _make_image(tmp_path, farm, manager)  # EPSG:4326, ~7.8m/px at this raster's real extent
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        detected_location=Point(101.4425, 0.5325, srid=4326),
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    clipped_array, clipped_transform, clip_crs = clip_to_block(image, block_a)

    calls = []
    real_crop_patch = services_module.crop_patch

    def spy(clipped_array, transform, lon, lat, crs=None, patch_size_px=64):
        calls.append(patch_size_px)
        return real_crop_patch(clipped_array, transform, lon, lat, crs=crs, patch_size_px=patch_size_px)

    monkeypatch.setattr(services_module, "crop_patch", spy)

    services_module.classify_detection(
        detection, clipped_array, clipped_transform, crs=clip_crs, classifier=lambda c: ("healthy", 0.9)
    )

    assert len(calls) == 1
    from apps.imagery.ml.classification import CLASSIFICATION_PATCH_SIZE_PX

    # This test raster's real GSD (~7.8m/px, coarser than the 15cm target)
    # means the clamp kicks in — the requested native crop shrinks toward
    # (not away from) CLASSIFICATION_PATCH_SIZE_PX, but must still differ
    # from the unscaled constant, proving the GSD-aware path actually ran
    # rather than silently falling back to the old fixed-size behavior.
    assert calls[0] != CLASSIFICATION_PATCH_SIZE_PX


# --- Vegetation indices (apps/imagery/ml/vegetation_indices.py) -------------
#
# Pure numpy functions, no DB/Django dependency — ported from the training
# pipeline's own module and confirmed bit-for-bit equivalent to it standalone
# before being wired in (see classification.py's module docstring). These
# tests cover this project's own copy in isolation, independent of that
# one-time equivalence check.


def test_compute_indices_returns_seven_channels_in_the_trained_order():
    from apps.imagery.ml.vegetation_indices import compute_indices

    rgb = np.random.default_rng(0).integers(0, 256, size=(10, 10, 3), dtype=np.uint8)
    stacked = compute_indices(rgb).stack()
    assert stacked.shape == (10, 10, 7)


def test_compute_indices_handles_a_pure_black_patch_without_raising():
    """A pure-black patch (all-zero pixels) makes every ratio index's
    denominator zero or near-zero — must produce NaN (per the
    _RATIO_DENOM_FLOOR guard), not raise or silently return a bogus huge
    number.
    """
    from apps.imagery.ml.vegetation_indices import compute_indices

    rgb = np.zeros((4, 4, 3), dtype=np.uint8)
    indices = compute_indices(rgb)
    assert np.all(np.isnan(indices.vari))
    assert np.all(np.isnan(indices.ngrdi))


def test_clip_to_block_reads_at_reduced_resolution_when_asked(tmp_path, farm, manager, block_a):
    """A native-resolution clip of a very fine orthomosaic exhausted the
    worker's memory (every B3 run was SIGKILLed while clipping)."""
    from apps.imagery.ml.detection import _native_gsd_m
    from apps.imagery.services import clip_to_block

    image = _make_image(tmp_path, farm, manager)
    native, native_transform, crs = clip_to_block(image, block_a)
    gsd = _native_gsd_m(native_transform, crs)

    reduced, reduced_transform, _ = clip_to_block(image, block_a, max_gsd_m=gsd * 2)
    assert reduced.shape[0] == native.shape[0]  # same bands
    assert reduced.size < native.size / 3  # ~4x fewer pixels at 2x the pixel size
    assert abs(_native_gsd_m(reduced_transform, crs) - gsd * 2) < gsd * 0.2
    assert reduced.any()  # real pixels inside the block, not an empty clip

    same, _, _ = clip_to_block(image, block_a, max_gsd_m=gsd)  # already fine enough: untouched
    assert same.shape == native.shape


# --- Confirming a detection must never create a second tree for the same
#     palm: a double-click, a retried request, or a second run over the same
#     area used to each add another one. ---

@pytest.mark.django_db
def test_confirming_the_same_detection_twice_creates_one_tree(as_manager, farm, manager, block_a, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4440, 0.5340, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    first = as_manager.post(f"/api/detections/{detection.id}/confirm/")
    second = as_manager.post(f"/api/detections/{detection.id}/confirm/")
    assert first.data["tree_id"] == second.data["tree_id"]
    assert Tree.objects.filter(farm_block=block_a).count() == 1


@pytest.mark.django_db
def test_second_run_over_the_same_palm_links_to_the_existing_tree(farm, manager, block_a, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    point = Point(101.4440, 0.5340, srid=4326)
    runs = [ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager) for _ in range(2)]
    detections = [
        TreeDetection.objects.create(
            processing_run=run, detected_location=point, detection_confidence=0.9,
            match_status=TreeDetection.MATCH_NEW_CANDIDATE,
        )
        for run in runs
    ]  # both were "new" when their runs finished, before either was reviewed
    trees = [confirm_new_candidate(d, manager) for d in detections]
    assert trees[0].id == trees[1].id
    assert Tree.objects.filter(farm_block=block_a).count() == 1
    detections[1].refresh_from_db()
    assert detections[1].match_status == TreeDetection.MATCH_MATCHED


# --- Health labels: no guesses, and low-confidence results don't colour the map. ---

@pytest.mark.django_db
def test_low_confidence_result_does_not_change_tree_health_until_reviewed(client, farm, manager, farm_role_manager, block_a, tree_in_block_a, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    run_processing(run, detector=_fake_detector_factory([(101.4410, 0.5310, 0.9)]),
                   classifier=lambda c: ("yellow", 0.4))
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.current_health == Tree.HEALTH_UNKNOWN  # not trusted yet

    classification = TreeHealthClassification.objects.get()
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:imagery-classification-review", kwargs={"classification_id": classification.id}),
        {"label": "healthy"},
    )
    assert response.status_code == 302
    classification.refresh_from_db()
    tree_in_block_a.refresh_from_db()
    assert (classification.reviewed, classification.corrected_label) == (True, "healthy")
    assert tree_in_block_a.current_health == "healthy"


def test_unusable_patch_is_unknown_not_healthy():
    from apps.imagery.ml.classification import stub_classifier

    assert stub_classifier(None) == ("unknown", 0.0)


@pytest.mark.django_db
def test_second_run_for_the_same_image_and_block_is_not_started(as_manager, farm, manager, block_a, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    georeference_image(image)
    active = ImageProcessingRun.objects.create(
        image=image, farm_block=block_a, triggered_by=manager, status=ImageProcessingRun.STATUS_CLIPPING
    )
    response = as_manager.post(f"/api/farm-blocks/{block_a.id}/images/{image.id}/process/")
    assert response.status_code == 200
    assert response.data["id"] == str(active.id)
    assert ImageProcessingRun.objects.filter(farm_block=block_a).count() == 1


@pytest.mark.django_db
def test_delete_image_removes_run_clipped_files(tmp_path, farm, manager, block_a, settings):
    from django.core.files.base import ContentFile

    from apps.imagery.services import delete_image

    settings.MEDIA_ROOT = tmp_path / "media"
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    run.clipped_cog_file.save("clip.tif", ContentFile(b"x"), save=True)
    path = run.clipped_cog_file.path
    delete_image(image)
    import os
    assert not os.path.exists(path)


@pytest.mark.django_db
def test_cleanup_command_finds_only_unreferenced_old_files(tmp_path, farm, manager, settings):
    import os
    import time

    from django.core.management import call_command

    from apps.imagery.management.commands.cleanup_orphaned_imagery import find_orphans

    settings.MEDIA_ROOT = tmp_path / "media"
    raw = settings.MEDIA_ROOT / "imagery" / "raw"
    raw.mkdir(parents=True)
    orphan, fresh = raw / "old.tif", raw / "new.tif"
    orphan.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(orphan, (old, old))
    assert [n for n, _, _ in find_orphans()] == ["imagery/raw/old.tif"]  # new one is too recent to touch
    call_command("cleanup_orphaned_imagery")  # dry run
    assert orphan.exists()
    call_command("cleanup_orphaned_imagery", "--delete")
    assert not orphan.exists() and fresh.exists()


@pytest.mark.django_db
def test_cannot_reject_a_confirmed_detection_or_link_to_a_removed_tree(as_manager, farm, manager, block_a, tree_in_block_a, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    linked = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326), detection_confidence=0.9,
        match_status=TreeDetection.MATCH_MATCHED, tree=tree_in_block_a,
    )
    assert as_manager.post(f"/api/detections/{linked.id}/reject/").status_code == 400

    tree_in_block_a.status = Tree.STATUS_REMOVED
    tree_in_block_a.save()
    open_detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4411, 0.5311, srid=4326), detection_confidence=0.9,
        match_status=TreeDetection.MATCH_AMBIGUOUS,
    )
    assert as_manager.post(f"/api/detections/{open_detection.id}/link/", {"tree_id": str(tree_in_block_a.id)}).status_code == 404


@pytest.mark.django_db
def test_failure_notification_links_to_the_review_page(farm, manager, block_a, tmp_path):
    from apps.imagery.services import _notify_processing_failure
    from apps.notifications.models import Notification

    image = _make_image(tmp_path, farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager, error_message="boom")
    _notify_processing_failure(run)
    assert Notification.objects.get(user=manager).link == f"/dashboard/imagery/runs/{run.id}/"
