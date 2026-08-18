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

from .ml.classification import CLASSIFICATION_PATCH_SIZE_PX, classification_native_patch_size_px, get_classifier
from .ml.detection import _native_gsd_m, get_detector
from .models import Image, ImageProcessingRun, ImageUploadSession, TreeDetection, TreeHealthClassification


class ProcessingRefused(Exception):
    """Raised when a run cannot start at all — e.g. no block boundary.
    Distinct from a mid-pipeline failure (which the run's own status/
    error_message field records) because this should never even create a
    'started' run — docs/06-ml-pipeline.md: "refuse and surface ... never
    fall back to processing the unclipped image."
    """


class UploadSessionIncomplete(Exception):
    """Raised by finalize_upload_session() when one or more chunk indexes
    haven't arrived yet — mirrors ProcessingRefused's pattern: a plain,
    named exception the caller (apps/dashboard/views.py) turns into
    whatever response makes sense for that surface, rather than a 500.
    """


class EmptyChunkUpload(Exception):
    """Raised by store_chunk() when a chunk's body is 0 bytes.

    Found while investigating a real corrupted-upload report: store_chunk()
    previously wrote and marked-received whatever bytes arrived with no
    length check at all — a network hiccup that silently truncates one
    chunk's request body (possible with some proxies/browsers even without
    the request itself erroring) would still get recorded as "received,"
    producing a reassembled file that's structurally a valid-looking
    GeoTIFF (correct header) but missing real pixel data partway through —
    exactly the "TIFFFillStrip: Read error ... got 244 bytes, expected
    48365" symptom seen live. This only catches the simplest, most obvious
    case (a fully empty body); it does not verify that a non-final chunk
    is exactly the client's chosen chunk size, since the server doesn't
    currently track expected per-chunk size or total file size on the
    session — that would need a schema change and is a known remaining
    gap, not solved here.
    """


def _chunk_storage_path(session: ImageUploadSession, chunk_index: int) -> str:
    return f"imagery/upload_chunks/{session.id}/{chunk_index:06d}.part"


def create_upload_session(farm, uploaded_by, filename, total_chunks, captured_at=None) -> ImageUploadSession:
    """Starts a new chunked-upload session — see ImageUploadSession's
    docstring for the "why chunked at all" reasoning (real drone
    orthomosaics are routinely hundreds of MB to a few GB; a single
    synchronous multipart POST has no way to resume if it drops partway
    over office WiFi).
    """
    return ImageUploadSession.objects.create(
        farm=farm, uploaded_by=uploaded_by, filename=filename,
        total_chunks=total_chunks, captured_at=captured_at,
    )


def store_chunk(session: ImageUploadSession, chunk_index: int, chunk_bytes: bytes) -> ImageUploadSession:
    """Writes one chunk's bytes to storage and marks its index received.

    Chunks are stored and tracked by index, not assumed to arrive in
    order — a resumed upload might re-send a chunk the server already has
    (safe: overwritten in place, not duplicated), or a client might fire
    a few requests concurrently. This is what makes "which chunks do you
    still need" a meaningful question the client can ask on resume,
    rather than the server only ever accepting one specific next index.

    Raises EmptyChunkUpload for a 0-byte body rather than silently
    recording it as received — see that exception's docstring for why.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    if not chunk_bytes:
        raise EmptyChunkUpload(f"Chunk {chunk_index} for session {session.id} arrived with an empty body.")

    path = _chunk_storage_path(session, chunk_index)
    if default_storage.exists(path):
        default_storage.delete(path)  # re-sent chunk — overwrite, don't duplicate
    default_storage.save(path, ContentFile(chunk_bytes))

    if chunk_index not in session.received_chunk_indexes:
        session.received_chunk_indexes = [*session.received_chunk_indexes, chunk_index]
        session.save(update_fields=["received_chunk_indexes", "updated_at"])
    return session


def finalize_upload_session(session: ImageUploadSession) -> Image:
    """Concatenates every chunk (strictly in index order — arrival order
    doesn't matter, assembly order does) into one file, creates the real
    Image exactly like the existing single-POST ImageViewSet.create() path
    does, deletes the temporary chunk files, and dispatches the same
    georeference_image_task the non-chunked path already uses. From this
    point on a chunked-uploaded image is completely indistinguishable from
    one uploaded in a single request — same model, same task, same
    downstream pipeline.

    Streams the concatenation through a bounded 1MB buffer into a real
    temp file, rather than accumulating the whole reassembled file in a
    Python bytearray (the previous implementation here) — a real drone
    orthomosaic is routinely hundreds of MB to a few GB (see
    ImageUploadSession's own docstring), and the old approach held the
    entire file in memory, then made a full second in-memory copy of it
    (`bytes(buffer)`) just to satisfy ContentFile's constructor — roughly
    2x the upload size in peak Python memory, for no reason other than
    that being the simplest code to write for the small test fixtures
    this was originally exercised against. That's exactly the kind of gap
    that only shows up against a real-sized file, in a memory-constrained
    worker container, not in a test with a few-KB synthetic raster.
    Verified end to end in a standalone script (chunk files on disk ->
    streamed reassembly -> byte-for-byte identical output, no full-file
    Python bytes/bytearray object involved) before being wired in here.

    Raises UploadSessionIncomplete if any chunk index is still missing —
    checked directly (not trusted from the client's own "I'm done" claim).
    """
    import tempfile

    from django.core.files import File
    from django.core.files.storage import default_storage

    if not session.is_complete_upload:
        missing = sorted(set(range(session.total_chunks)) - set(session.received_chunk_indexes))
        raise UploadSessionIncomplete(f"Missing chunk index(es): {missing}")

    tmp = tempfile.TemporaryFile()
    try:
        for index in range(session.total_chunks):
            with default_storage.open(_chunk_storage_path(session, index), "rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    tmp.write(block)
        tmp.seek(0)
        image = Image(farm=session.farm, uploaded_by=session.uploaded_by, captured_at=session.captured_at)
        image.raw_file.save(session.filename, File(tmp), save=True)
    finally:
        tmp.close()

    for index in range(session.total_chunks):
        default_storage.delete(_chunk_storage_path(session, index))

    session.status = ImageUploadSession.STATUS_COMPLETE
    session.image = image
    session.save(update_fields=["status", "image", "updated_at"])

    # Imported here, not at module level — apps/imagery/tasks.py imports
    # this module (`from . import services`), so importing tasks.py back
    # at the top of this file would be a circular import.
    from .tasks import georeference_image_task

    georeference_image_task.delay(str(image.id))
    return image


def convert_to_cog(image: Image) -> Image:
    """Re-encodes image.raw_file in place as a Cloud Optimized GeoTIFF —
    internally tiled, with baked-in overview (pyramid) levels — using
    GDAL's native COG driver via rasterio.shutil.copy(driver="COG").
    Deliberately not rio-cogeo: this project already hard-depends on
    rasterio/GDAL, and GDAL's own COG driver (available since GDAL 3.1;
    this project's GDAL is well past that) does the same job without a new
    dependency. Verified end to end in a standalone script before wiring
    this in — a plain untiled 2000x2000 test GeoTIFF came back tiled
    (512x512 blocks), with real overview levels, correct CRS/bounds, and
    byte-identical pixel data on read-back.

    Runs first in georeference_image_task, before georeference_image()
    and generate_preview() — neither of those needs any change for this:
    both already just call rasterio.open(image.raw_file.path), and a COG
    reads exactly like any other GeoTIFF through that API, just faster for
    windowed/decimated reads (generate_preview()'s decimated read,
    clip_to_block()'s masked read, crop_patch()'s patch extraction) once
    overviews exist. Also the standard input format if this project ever
    adds real tile serving — docs/06-ml-pipeline.md's other stated
    alternative to a downsampled preview.

    Never blocks the rest of the pipeline on failure, same as
    generate_preview() (unlike georeference_image() itself, which is
    load-bearing for the footprint the whole app depends on): if
    conversion fails, raw_file is simply left as whatever was originally
    uploaded, and georeferencing/processing continue against that
    unconverted file exactly as they did before this existed.

    Two-phase swap (write to a new storage path, verify it actually opens
    and reads, THEN commit the DB pointer, THEN delete the old file) —
    found live, not by inspection: a real worker process got SIGKILL'd
    (OOM, per `docker compose logs worker`: "Process 'ForkPoolWorker-1'
    exited with 'signal 9 (SIGKILL)'") partway through a run, and a
    separate real image on the same farm was left with a truncated
    GeoTIFF on disk (GDAL: "TIFFFillStrip: Read error at scanline 12734;
    got 244 bytes, expected 48365") — every later read of that file
    (clipping, preview, even re-opening it) failed identically, with no
    way to recover except a fresh re-upload, because the original upload
    was already gone. The previous version of this function deleted the
    original raw_file and then wrote the new one straight over it
    (`default_storage.delete()` then `.save()`) — if the process dies
    anywhere in that window, including mid-write of a multi-hundred-MB
    file, there is no valid copy of the image left at all. This version
    never deletes the original until a fully-written, independently
    re-opened-and-read-back copy already exists at a different path and
    the database has been updated to point at it — so a crash at any
    point before that leaves the original completely untouched, and a
    crash after it leaves the new file already committed and just an
    orphaned (harmless, cheap-to-clean-up-later) copy of the old one on
    disk. Also drops the old implementation's `cog_bytes = fh.read()` —
    a second full in-memory copy of the entire re-encoded raster just to
    build a ContentFile — in favor of streaming the already-on-disk temp
    file straight into storage.
    """
    import os
    import tempfile

    import rasterio
    import rasterio.shutil as rio_shutil
    from django.conf import settings
    from django.core.files import File
    from django.core.files.storage import default_storage

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tif")
        os.close(tmp_fd)
        try:
            with rasterio.open(image.raw_file.path) as src:
                rio_shutil.copy(
                    src,
                    tmp_path,
                    driver="COG",
                    compress=getattr(settings, "IMAGE_COG_COMPRESSION", "DEFLATE"),
                    blocksize=getattr(settings, "IMAGE_COG_BLOCKSIZE", 512),
                    overview_resampling="average",
                )
            # Actually touch real pixel data, not just the header/metadata —
            # a truncated file (exactly the failure mode this function is
            # now guarding against) can still open and report valid CRS/
            # bounds; only a real read proves the bytes are actually there.
            with rasterio.open(tmp_path) as check:
                check.read(1, window=((0, 1), (0, 1)))
            with open(tmp_path, "rb") as fh:
                new_name = default_storage.save(f"{image.raw_file.name}.cogswap", File(fh))
        finally:
            os.unlink(tmp_path)
    except Exception as exc:  # noqa: BLE001 — must never block georeferencing/preview generation; original raw_file untouched
        image.is_cog = False
        image.cog_error = str(exc)
        image.save(update_fields=["is_cog", "cog_error"])
        return image

    old_name = image.raw_file.name
    image.raw_file.name = new_name
    image.is_cog = True
    image.cog_error = ""
    # Commit the swap before touching the old file at all — if the process
    # dies before this line, the DB still points at old_name, which still
    # physically exists, so nothing is lost.
    image.save(update_fields=["raw_file", "is_cog", "cog_error"])
    default_storage.delete(old_name)
    return image


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


def persist_clipped_block_imagery(
    run: ImageProcessingRun, clipped_array, clipped_transform, clip_crs
) -> ImageProcessingRun:
    """Physically writes the clip already computed by clip_to_block() as a
    standalone COG (ImageProcessingRun.clipped_cog_file) plus a web-viewable
    preview PNG (clipped_preview_file) with real, block-accurate overlay
    bounds (clipped_footprint) — requested directly: a real orthomosaic that
    spans several blocks previously always displayed its WHOLE footprint on
    the Farm Map (farm_map.html's L.imageOverlay uses image.geometry/
    image.preview_url unconditionally), so a block's imagery view showed real
    pixels belonging to a neighboring block.

    Takes `clipped_array`/`clipped_transform`/`clip_crs` — the exact output
    of the SAME clip_to_block() call run_processing() already made for
    detection/classification — rather than re-deriving its own clip via a
    second rasterio.mask.mask() call. Asked directly to make this a single
    crop-then-pipeline step, not two independently-computed clips of the
    same block that could in principle disagree (e.g. if the source
    declares a nodata value clip_to_block()'s default-nodata mask() picks up
    one way and an explicit second call picks up another) — this also
    halves the cost of a full-resolution masked read against what can be a
    very large orthomosaic, since that read now only ever happens once.

    Still needs one cheap, metadata-only reopen of image.raw_file (dtype/
    band count/colorinterp/declared nodata) — clip_to_block()'s return
    contract is deliberately just the array a detector/classifier needs, not
    enough on its own to write a valid standalone georeferenced file.

    Never blocks the pipeline it runs alongside — same convention as
    convert_to_cog()/generate_preview(): any failure is recorded in
    run.clip_persist_error, and run_processing()'s own detection/
    classification stages proceed against clipped_array regardless of
    whether this succeeds.

    Two-phase-safe write, mirroring convert_to_cog(): both the raw clipped
    GeoTIFF and its COG re-encode are built in temp files and verified with
    a real pixel read *before* anything is saved to run — a mid-write crash
    leaves run's existing fields (if any, from a previous run against the
    same block) untouched rather than half-overwritten.
    """
    import io
    import os
    import tempfile

    import numpy as np
    import rasterio
    import rasterio.shutil as rio_shutil
    from django.conf import settings
    from django.core.files import File
    from django.core.files.base import ContentFile
    from PIL import Image as PILImage
    from rasterio.enums import Resampling
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds

    image = run.image

    try:
        # Metadata only — no pixel read, no second mask() call. Built
        # explicitly, not `src.profile.copy()` — image.raw_file has very
        # likely already gone through convert_to_cog() by the time this
        # runs (georeference_image_task always runs that first), so
        # src.profile carries COG-specific creation options (blockxsize/
        # blockysize, tiled, compress, predictor, interleave, ...). This
        # write is a throwaway plain GeoTIFF that only exists to feed
        # rio_shutil.copy(driver="COG") a moment later, so none of that
        # baggage is wanted — GDAL's GTiff driver rejects a profile that
        # carries BLOCKXSIZE/BLOCKYSIZE without also carrying TILED=True
        # ("BLOCKXSIZE can only be used with TILED=YES"), which is exactly
        # what copying a COG source's full profile onto a driver="GTiff"
        # write does. Found live: this exact scenario — process an image
        # through the real upload -> georeference -> process API chain (not
        # run_processing() called directly on a plain raster, which never
        # hit this) — via a pre-existing end-to-end test, not by inspection.
        with rasterio.open(image.raw_file.path) as src:
            # Mirrors rasterio.mask.mask()'s own default nodata resolution
            # (what clip_to_block() implicitly used, since it doesn't pass
            # an explicit nodata=) — so the value declared here actually
            # matches what clipped_array's masked-out pixels were filled
            # with, which is what lets _preview_alpha_channel()'s declared-
            # nodata strategy correctly find them below.
            nodata_value = src.nodata if src.nodata is not None else 0
            profile = {
                "driver": "GTiff",
                "dtype": src.dtypes[0],
                "count": src.count,
                "height": clipped_array.shape[1],
                "width": clipped_array.shape[2],
                "crs": clip_crs,
                "transform": clipped_transform,
                "nodata": nodata_value,
            }
            colorinterp = src.colorinterp

            # The clipped raster's real pixel-accurate extent — from the
            # transform/shape actually written, not a GEOS intersection of
            # block.boundary/image.footprint, which would only approximate
            # the rectangular window rasterio's crop=True actually
            # produced. Same technique georeference_image() already uses
            # for Image.footprint.
            left, bottom, right, top = array_bounds(
                clipped_array.shape[1], clipped_array.shape[2], clipped_transform
            )
            if clip_crs is not None and clip_crs.to_epsg() != 4326:
                left, bottom, right, top = transform_bounds(clip_crs, "EPSG:4326", left, bottom, right, top)

        raw_fd, raw_path = tempfile.mkstemp(suffix=".tif")
        os.close(raw_fd)
        cog_fd, cog_path = tempfile.mkstemp(suffix=".tif")
        os.close(cog_fd)
        try:
            with rasterio.open(raw_path, "w", **profile) as dst:
                dst.write(clipped_array)
                dst.colorinterp = colorinterp

            with rasterio.open(raw_path) as raw_src:
                rio_shutil.copy(
                    raw_src,
                    cog_path,
                    driver="COG",
                    compress=getattr(settings, "IMAGE_COG_COMPRESSION", "DEFLATE"),
                    blocksize=getattr(settings, "IMAGE_COG_BLOCKSIZE", 512),
                    overview_resampling="average",
                )
            # Same reasoning as convert_to_cog(): a truncated file can still
            # open and report valid CRS/bounds; only a real read proves the
            # bytes are actually there.
            with rasterio.open(cog_path) as check:
                check.read(1, window=((0, 1), (0, 1)))

            with rasterio.open(cog_path) as clipped_src:
                max_dimension = getattr(settings, "IMAGE_PREVIEW_MAX_DIMENSION_PX", 1024)
                scale = min(1.0, max_dimension / max(clipped_src.width, clipped_src.height))
                out_width = max(1, round(clipped_src.width * scale))
                out_height = max(1, round(clipped_src.height * scale))
                read_indexes = (
                    list(range(1, min(clipped_src.count, 3) + 1)) if clipped_src.count >= 3 else [1]
                )
                preview_data = clipped_src.read(
                    indexes=read_indexes,
                    out_shape=(len(read_indexes), out_height, out_width),
                    resampling=Resampling.average,
                )
                preview_rgb = _to_uint8_rgb(preview_data)
                preview_alpha = _preview_alpha_channel(clipped_src, out_width, out_height)
            preview_rgba = np.dstack([preview_rgb, preview_alpha])
            buffer = io.BytesIO()
            PILImage.fromarray(preview_rgba, mode="RGBA").save(buffer, format="PNG")
            preview_bytes = buffer.getvalue()

            footprint = GEOSGeometry(
                json.dumps(
                    {
                        "type": "Polygon",
                        "coordinates": [
                            [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]
                        ],
                    }
                ),
                srid=4326,
            )

            # Streamed straight off the already-on-disk temp file, not a
            # `cog_bytes = fh.read()` in-memory copy — same reasoning as
            # convert_to_cog()'s own docstring: a real clipped orthomosaic
            # region can still be a substantial file, and there's no reason
            # to hold a second full copy in Python memory just to build a
            # ContentFile. Has to happen before the `finally` below deletes
            # the temp file.
            with open(cog_path, "rb") as fh:
                run.clipped_cog_file.save(f"{run.id}.tif", File(fh), save=False)
        finally:
            os.unlink(raw_path)
            os.unlink(cog_path)
    except Exception as exc:  # noqa: BLE001 — must never block detection/classification, which don't depend on this at all
        run.clip_persist_error = str(exc)
        run.save(update_fields=["clip_persist_error"])
        return run

    run.clipped_preview_file.save(f"{run.id}.png", ContentFile(preview_bytes), save=False)
    run.clipped_footprint = footprint
    run.clip_persist_error = ""
    run.save(
        update_fields=["clipped_cog_file", "clipped_preview_file", "clipped_footprint", "clip_persist_error"]
    )
    return run


def _to_uint8_rgb(band_array):
    """CHW array (any band count, any dtype) -> HWC uint8 RGB. Shared by
    crop_patch() (classifier input) and generate_preview() (map/dashboard
    display) — both need the same "use the first 3 bands, or replicate
    band 1 across all 3 channels for anything with fewer" and "min-max
    stretch to uint8 if the source isn't already 8-bit" handling.

    Update: uses the np.ptp(array) *function*, not the older array.ptp()
    *method* — ndarray.ptp() was removed in NumPy 2.0 ("`ptp` was removed
    from the ndarray class in NumPy 2.0. Use np.ptp(arr, ...) instead.",
    confirmed by actually hitting this — see BUILD_STATUS.md). This
    project's requirements.txt pins `numpy>=1.24` with no upper bound, so
    a fresh `pip install -r requirements.txt` today resolves to NumPy 2.x,
    meaning the previous `.ptp()` call here (this function was extracted
    from crop_patch(), which used the exact same line) would have raised
    AttributeError the first time a non-uint8-dtype raster ever reached
    classification — a real, pre-existing bug, not something new in this
    change.
    """
    import numpy as np

    bands = band_array.shape[0]
    rgb = band_array[:3] if bands >= 3 else np.repeat(band_array[:1], 3, axis=0)
    rgb = np.transpose(rgb, (1, 2, 0))
    if rgb.dtype != np.uint8:
        ptp = np.ptp(rgb) or 1
        rgb = ((rgb.astype("float32") - rgb.min()) / ptp * 255).astype("uint8")
    return rgb


def _preview_alpha_channel(src, out_width, out_height):
    """Builds the alpha channel for generate_preview()'s output PNG — 255
    (opaque) for real image data, 0 (fully transparent) for nodata/padding.

    Drone orthomosaic exports are almost never a perfect axis-aligned
    rectangle of real imagery — the raster's own bounding box is padded out
    with nodata pixels wherever the actual flight path doesn't reach.
    generate_preview() used to emit a plain opaque RGB PNG, and
    farm_map.html's L.imageOverlay stretches that whole rectangle across
    the image's footprint — so that padding rendered as a solid black or
    white block sitting on top of the map, reported directly: "it contain
    white/black area, while it should be only the picture."

    Two strategies, in priority order:

    1. If the source raster declares a real nodata value, or already has
       its own alpha band, that's an authoritative answer — use rasterio's
       own dataset_mask() (decimated to the same output size the RGB read
       in generate_preview() used) rather than guessing.
    2. Otherwise — the common case for drone orthomosaic exports that pad
       their bounding box with unlabeled black or white pixels rather than
       a declared nodata value — fall back to a pure-black/pure-white
       heuristic. Deliberately conservative — real plantation imagery
       essentially never has a pixel that's exactly 0 or exactly 255 in
       every channel simultaneously except deliberate padding, so this
       shouldn't punch holes in genuinely dark shadow or bright glare areas.

    Update, investigated after a direct report that real Pix4D exports
    still showed a visible white/black-ish area even after the original
    version of this function shipped:

    - Strategy 1's decimated mask read was switched to `Resampling.nearest`
      in an earlier pass, reasoning "categorical 0/255 data should never
      use a continuous-blend resampling method" — plausible-sounding, but
      wrong, and shipped without checking it against a real file. Checked
      directly against a real uploaded Pix4D orthomosaic (4-band RGBA,
      genuine embedded alpha, 28029x25552px) once a report came back that
      the white/black area was *still* visible: at the decimation factor a
      preview actually reads at (~31x for this file), `nearest` samples a
      sparse grid of single native pixels per output pixel — for a
      typically-thin diagonal padding border, that sparse grid steps over
      the border entirely almost everywhere, so the decimated mask came
      back **0% fully-transparent** (measured, not assumed) — nearest
      didn't just fail to help, it silently discarded the alpha channel's
      real content. `Resampling.average` on the same file and output size
      gave 43% fully-transparent pixels, matching the real silhouette (a
      rotated rectangle with an irregular flight-path edge — confirmed by
      rendering the mask to a PNG and looking at it, not just the numbers).
      Reverted to `average`, but average alone reintroduces the original
      soft-edge problem (a block straddling the border decimates to some
      value between 0 and 255, not a clean cut) — fixed by adding a hard
      threshold (`>= 128` -> opaque, else transparent) on top of the
      average, which keeps average's accurate coverage detection while
      still producing a strictly binary result. The 128 cutoff is a
      reasonable midpoint, not calibrated against anything.
    - Strategy 2's heuristic was checking purity on `rgb` — which
      generate_preview() already read with `Resampling.average` for visual
      quality. That's backwards for a boundary pixel: a decimation window
      straddling real image content and pure-black padding averages into
      something like (65, 70, 62) — clearly polluted by the padding it's
      touching, but no longer *exactly* 0, so the exact-purity test misses
      it and marks it fully opaque. Confirmed with a real rasterio decimated
      read against a synthetic black-padded raster before fixing, not
      assumed. Fixed by classifying purity on a *separate*
      `Resampling.nearest` decimated read instead of the average-resampled
      display `rgb` — nearest-neighbor never blends, so it reports exactly
      which single native pixel a given output location corresponds to,
      immune to this specific failure mode.
    - The most likely actual cause of what was reported, though, isn't
      either of the above: every image uploaded *before* this function's
      alpha logic existed at all has an already-generated, fully opaque
      preview file sitting in storage untouched by any of these fixes —
      flagged as a known gap when the alpha logic first shipped, and still
      unresolved. See the new `regenerate_previews` management command
      (apps/imagery/management/commands/) for the backfill this always
      needed; run it once after deploying any of the changes above.
    """
    import numpy as np
    from rasterio.enums import Resampling

    has_declared_nodata = src.nodata is not None
    has_alpha_band = src.count >= 4 and any(ci.name == "alpha" for ci in src.colorinterp)
    if has_declared_nodata or has_alpha_band:
        # Resampling.average, not .nearest — see the docstring's "Update"
        # section above for the real measurement that justifies this
        # (nearest silently under-samples thin padding borders at typical
        # preview decimation factors). Thresholded to stay strictly binary
        # rather than leaving average's raw fractional edge values in.
        mask = src.dataset_mask(out_shape=(out_height, out_width), resampling=Resampling.average)
        return np.where(mask >= 128, 255, 0).astype("uint8")

    read_indexes = list(range(1, min(src.count, 3) + 1)) if src.count >= 3 else [1]
    nearest = src.read(
        indexes=read_indexes, out_shape=(len(read_indexes), out_height, out_width), resampling=Resampling.nearest
    )
    nearest_rgb = _to_uint8_rgb(nearest)
    is_pure_black = np.all(nearest_rgb == 0, axis=-1)
    is_pure_white = np.all(nearest_rgb == 255, axis=-1)
    return np.where(is_pure_black | is_pure_white, 0, 255).astype("uint8")


def crop_patch(clipped_array, transform, lon, lat, crs=None, patch_size_px=64):
    """Extracts a small patch around (lon, lat) from the already-clipped
    raster array, for the classifier. Returns None (never a malformed
    image) if the point falls too close to the clipped array's edge to get
    a usable patch — callers must treat that as "skip classification for
    this detection," not crash the run over one edge case.

    `lon, lat` are true EPSG:4326 geographic coordinates (e.g. straight off
    `TreeDetection.detected_location` — always SRID 4326 by construction),
    but `transform` maps pixel <-> `crs`'s NATIVE units, which for a
    UTM-projected orthomosaic is meters, not degrees — `~transform * (lon,
    lat)` only produces a sane pixel coordinate if `crs` is already
    geographic. `crs` defaults to None (assume already EPSG:4326, no
    reprojection) only because every existing caller/test raster happens to
    already be in EPSG:4326; real callers (crop_patch()'s only real
    consumer, classify_detection()) should always pass the clipped raster's
    actual CRS. See apps/imagery/ml/detection.py's
    `_reproject_point_to_lonlat()` for the mirror-image version of this same
    fix on the detection side of this pipeline.
    """
    from PIL import Image as PILImage

    if crs is not None and crs.to_epsg() != 4326:
        from rasterio.warp import transform as warp_transform

        (lon,), (lat,) = warp_transform("EPSG:4326", crs, [lon], [lat])

    col_f, row_f = ~transform * (lon, lat)
    row, col = int(row_f), int(col_f)
    half = patch_size_px // 2
    _bands, height, width = clipped_array.shape
    r0, r1 = max(0, row - half), min(height, row + half)
    c0, c1 = max(0, col - half), min(width, col + half)
    if r1 <= r0 or c1 <= c0:
        return None

    patch = clipped_array[:, r0:r1, c0:c1]
    return PILImage.fromarray(_to_uint8_rgb(patch))


def generate_preview(image: Image, max_dimension: int = None) -> Image:
    """Downsampled web-viewable PNG so the office dashboard's map (and any
    other "show this image" surface) can actually render the picture, not
    just its footprint outline — docs/06-ml-pipeline.md step 4's explicitly
    -allowed lighter alternative to a full tile pyramid: "a background job
    generates web map tiles (rio-tiler/titiler, **or a simpler downsampled
    preview**)". This is that simpler option, not full multi-zoom tiling —
    see BUILD_STATUS.md for why, and what real tiling would additionally
    need.

    Never raises — a preview is secondary to the upload/georeference
    pipeline it runs after, so a failure here is recorded in
    Image.preview_error (same pattern as georeference_image()'s own
    failure handling) rather than allowed to break anything upstream.

    Deliberately reads directly from the source file with band/size
    selection at read time (rasterio's decimated read), not by loading the
    full-resolution array into memory first — a real drone orthomosaic can
    be hundreds of megapixels; this project's ML pipeline already loads
    the full array for clip_to_block()'s per-block clip, but a preview is
    generated once for the *whole* uploaded file regardless of which (if
    any) blocks it's later processed against, so it shouldn't pay that
    same full-resolution cost.

    The output PNG is displayed via Leaflet's plain L.imageOverlay(url,
    bounds) stretched across the same rectangular footprint
    georeference_image() already computes — not reprojected to Web
    Mercator, not tiled at multiple zoom levels. Fine at the sizes this
    project's map panel actually needs; a real zoomable slippy-map tile
    layer for very large farms is the genuine scope gap this doesn't
    close.

    Update: the output is now RGBA, not plain RGB — see
    _preview_alpha_channel()'s own docstring for why an opaque preview was
    showing nodata/padding as a solid black or white rectangle laid over
    the map instead of transparently showing just the real picture.
    L.imageOverlay renders PNG alpha natively, so this needed no change on
    the map/JS side at all — only what this function writes into the file.
    """
    import io

    import numpy as np
    import rasterio
    from django.conf import settings
    from django.core.files.base import ContentFile
    from PIL import Image as PILImage
    from rasterio.enums import Resampling

    max_dimension = max_dimension if max_dimension is not None else getattr(
        settings, "IMAGE_PREVIEW_MAX_DIMENSION_PX", 1024
    )

    # Captured before the save below overwrites the field. Django's storage
    # never overwrites in place — it appends a random suffix when the target
    # name already exists — so regenerating a preview writes a NEW file and
    # silently strands the old one. Harmless once, but the whole point of
    # the regenerate_previews management command is to re-run this across
    # every image, potentially more than once, which turns that into real
    # accumulating waste (the same category of orphaned-file bug an earlier
    # audit had to clean multiple GB of out of media/ — see BUILD_STATUS.md).
    previous_preview_name = image.preview_file.name or None

    try:
        with rasterio.open(image.raw_file.path) as src:
            scale = min(1.0, max_dimension / max(src.width, src.height))
            out_width = max(1, round(src.width * scale))
            out_height = max(1, round(src.height * scale))
            read_indexes = list(range(1, min(src.count, 3) + 1)) if src.count >= 3 else [1]
            data = src.read(
                indexes=read_indexes,
                out_shape=(len(read_indexes), out_height, out_width),
                resampling=Resampling.average,
            )
            rgb = _to_uint8_rgb(data)
            alpha = _preview_alpha_channel(src, out_width, out_height)
        rgba = np.dstack([rgb, alpha])
        buffer = io.BytesIO()
        PILImage.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
        image.preview_file.save(f"{image.id}.png", ContentFile(buffer.getvalue()), save=False)
        image.preview_error = ""
    except Exception as exc:  # noqa: BLE001 — must never break the pipeline it runs after; recorded, not raised
        image.preview_error = str(exc)
    image.save(update_fields=["preview_file", "preview_error"])

    # Only after the new preview is fully written AND the DB points at it —
    # same two-phase ordering convert_to_cog() uses, for the same reason: a
    # crash before this line leaves the old preview intact and still
    # referenced, rather than deleting the only copy and leaving a broken
    # image link behind.
    if previous_preview_name and previous_preview_name != image.preview_file.name:
        from django.core.files.storage import default_storage

        try:
            default_storage.delete(previous_preview_name)
        except Exception:  # noqa: BLE001 — a stale leftover file is not worth failing a successful regeneration over
            pass

    return image


def apply_storage_lifecycle(retention_days=None) -> int:
    """docs/12-security-ops.md § Image storage lifecycle & cost policy:
    "after processing completes and a retention window passes (90 days as
    a starting point), the raw file moves to cold storage while derived
    data (tiles, thumbnails, all TreeDetection/TreeHealthClassification
    rows) stays hot indefinitely." Compliance-linked images
    (Image.is_compliance_exempt() — referenced by a DeforestationBaseline)
    are excluded, per that same section's stated exception and
    docs/02-build-plan.md Phase 6's checklist item.

    Interpreting "after processing completes": this project has no
    reliable way to say a given Image will *never* be processed again (a
    Manager can trigger a new ImageProcessingRun against any georeferenced
    image at any time — see BlockImageryView), and the 90-day default
    window is far longer than any realistic processing turnaround, so this
    uses a pure time-since-upload cutoff rather than gating on run status.
    Read as "processing normally finishes long before this window closes,"
    not as a strict precondition — flagged here as an inference (not
    something either doc states outright), same as
    DeforestationBaseline.evidence_image's own flagged inference in
    apps/compliance/models.py.

    What this does, and deliberately doesn't do: this project has no
    production object storage configured (.env.example's AWS_* vars are
    blank; local dev uses filesystem storage) — there is no real "cold"
    tier to physically relocate raw_file's bytes into. This marks eligible
    images Image.STORAGE_TIER_COLD (stamping cold_storage_since) rather
    than moving any bytes; that flag is what a real deployment's storage
    backend would act on (e.g. an S3 Lifecycle Configuration keyed off it,
    or a follow-up job that copies to Glacier/IA and deletes the hot
    copy) — consistent with this project's existing "policy vs. mechanism"
    split for the backup policy (docs/14-backup-restore.md: the backup/
    restore *mechanism* is built and drilled; a production backup
    *schedule* isn't, because no production deployment target exists yet).
    Not reversible here either (a cold image reprocessed later doesn't
    reset to hot) — a real storage backend wouldn't need to know or care,
    since nothing in this local-filesystem-storage setup actually restricts
    access to a "cold" file; it's bookkeeping for a policy this project's
    infrastructure doesn't fully exist to enforce yet, tracked honestly
    rather than half-implemented.

    Returns the count of images just marked cold (for the Celery task's
    logging and for tests).
    """
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    retention_days = (
        retention_days if retention_days is not None else getattr(settings, "IMAGE_STORAGE_COLD_TIER_DAYS", 90)
    )
    cutoff = timezone.now() - timedelta(days=retention_days)
    now = timezone.now()

    eligible = Image.objects.filter(storage_tier=Image.STORAGE_TIER_HOT, created_at__lt=cutoff)

    moved = 0
    for image in eligible:
        # is_compliance_exempt() is the single source of truth for this
        # check (apps/compliance/models.py points back to it too) — not
        # re-derived as a queryset filter here, so there's exactly one
        # place this rule can drift from the model's own definition.
        if image.is_compliance_exempt():
            continue
        image.storage_tier = Image.STORAGE_TIER_COLD
        image.cold_storage_since = now
        image.save(update_fields=["storage_tier", "cold_storage_since"])
        moved += 1
    return moved


class ImageDeletionRefused(Exception):
    """Raised when an Image can't be deleted at all — see delete_image()."""


def delete_image(image: Image) -> None:
    """Deletes an Image and everything derived from it: ImageProcessingRun
    rows (CASCADE), their TreeDetection rows (CASCADE), and their
    TreeHealthClassification rows (CASCADE) — but never the Tree rows a
    confirmed detection created (TreeDetection.tree is SET_NULL, and a
    confirmed Tree is real farm history independent of which image first
    suggested it — deleting the source image loses provenance/history for
    that tree, never the tree itself).

    Refuses outright — never silently cascades through it — if
    Image.is_compliance_exempt() (referenced by a DeforestationBaseline):
    the same rule apply_storage_lifecycle() already enforces for the
    cold-storage tier above, extended here to actual deletion, a much
    higher-stakes version of the same "this is compliance evidence, don't
    touch it" policy.

    Physically deletes every file this Image (and its cascade) owns from
    storage — raw_file, preview_file, and every TreeHealthClassification's
    patch_image — not just the DB rows. A plain cascade DELETE never
    touches the filesystem (FileField/ImageField don't hook into
    on_delete), so skipping this would silently orphan real files on disk
    — exactly the kind of accumulating waste a recent audit found and
    cleaned up from an unrelated bug (see BUILD_STATUS.md); this function
    deliberately doesn't reintroduce the same category of problem.
    """
    if image.is_compliance_exempt():
        raise ImageDeletionRefused(
            "This image is referenced as deforestation-baseline evidence and can't be deleted — "
            "see docs/12-security-ops.md's storage lifecycle policy."
        )

    patch_image_names = [
        classification.patch_image.name
        for classification in TreeHealthClassification.objects.filter(
            tree_detection__processing_run__image=image
        ).only("patch_image")
        if classification.patch_image
    ]

    if image.raw_file:
        image.raw_file.delete(save=False)
    if image.preview_file:
        image.preview_file.delete(save=False)
    if patch_image_names:
        from django.core.files.storage import default_storage

        for name in patch_image_names:
            default_storage.delete(name)

    image.delete()


def mark_stale_processing_runs_failed(stale_after_minutes=None) -> int:
    """Recovers ImageProcessingRun rows abandoned by a worker crash.

    Found live, not by inspection: a real worker process was SIGKILL'd
    (`docker compose logs worker`: "Process 'ForkPoolWorker-1' exited with
    'signal 9 (SIGKILL)'", almost certainly the OOM killer) while a run was
    mid-STATUS_CLIPPING. run_processing()'s own try/except is correct and
    does catch every *raised* failure (bad geometry, corrupt raster,
    detector exceptions — see that function's own docstring for the audit
    that made every stage consistent about this) — but it cannot catch the
    process being killed outright; there's no Python code running to catch
    anything with. The row is simply left exactly where it last saved,
    which is STATUS_CLIPPING/DETECTING/CLASSIFYING, forever — indistinguishable
    from "still legitimately working on a large file" by looking at the
    database alone, which is exactly what made this so hard to diagnose
    live: nothing about the row itself said "this will never finish."

    This is a periodic sweep (see CELERY_BEAT_SCHEDULE), not a fix at the
    source — there is no general way to make a SIGKILL catchable, so the
    only honest fix is detecting the resulting stuck state after the fact.
    A run is considered stale if it's in a non-terminal status and
    `started_at` is older than the threshold; on a real deployment a
    genuinely large orthomosaic's clip stage might legitimately take
    longer than the default, so this is a setting
    (IMAGE_PROCESSING_STALE_AFTER_MINUTES), not a hardcoded assumption.

    Reuses the exact same STATUS_FAILED + error_message + notification
    path _fail() uses inside run_processing() — from the review queue's
    point of view this looks identical to any other failure, just with an
    error message that says what's actually different about this one.

    Returns the count of runs just marked failed (for the Celery task's
    logging and for tests).
    """
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    stale_after_minutes = (
        stale_after_minutes
        if stale_after_minutes is not None
        else getattr(settings, "IMAGE_PROCESSING_STALE_AFTER_MINUTES", 60)
    )
    cutoff = timezone.now() - timedelta(minutes=stale_after_minutes)

    stale_runs = ImageProcessingRun.objects.filter(
        status__in=[
            ImageProcessingRun.STATUS_CLIPPING,
            ImageProcessingRun.STATUS_DETECTING,
            ImageProcessingRun.STATUS_CLASSIFYING,
        ],
        started_at__lt=cutoff,
    )

    marked = 0
    for run in stale_runs:
        stale_stage = run.get_status_display().lower()  # capture before overwriting status below
        run.status = ImageProcessingRun.STATUS_FAILED
        run.error_message = (
            f"No progress for over {stale_after_minutes} minutes while {stale_stage} — "
            "the worker process most likely crashed or was killed (e.g. out of memory) partway through, "
            "which this app has no way to detect except by timeout. Re-trigger processing for this image; "
            "if it happens again for the same image, check the worker's logs and memory limits."
        )
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at"])
        _notify_processing_failure(run)
        marked += 1
    return marked


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


def classify_detection(detection: TreeDetection, clipped_array, transform, crs=None, classifier=None):
    """Crops the patch and runs the classifier for one detection, writing a
    TreeHealthClassification row. Below-threshold (or unusable-patch)
    results are still written, just flagged for review — never dropped
    silently, and never auto-accepted either.

    `crs` is the clipped raster's own CRS (from clip_to_block()) — passed
    straight through to crop_patch() so it can reproject
    `detected_location`'s true EPSG:4326 coordinates back into the
    raster's native pixel-transform space. See crop_patch()'s own
    docstring for why this matters for any non-EPSG:4326 orthomosaic.

    Crops more (or fewer) NATIVE pixels than CLASSIFICATION_PATCH_SIZE_PX
    when `transform`'s real ground-sample-distance doesn't match
    CLASSIFICATION_TARGET_GSD_M — see classification.py's own module
    docstring for the real bug this fixes (every classification patch
    looking like close-up leaf/frond texture, not a crown, against this
    project's real Jonggol imagery). `_load_real_classifier()`'s existing
    "resize a crop that doesn't match the checkpoint's patch_size" step
    handles scaling the result back down to what the model actually
    expects — no separate resize needed here.

    Idempotent: if `detection` already has a TreeHealthClassification (the
    OneToOneField this row lives on), returns that existing row instead of
    trying to create a second one. run_processing() always calls this on a
    just-created TreeDetection, so this branch never fires there — it's
    what lets confirm_new_candidate() call this unconditionally too, now
    that run_processing() classifies every detection up front (not just
    matched ones — see that function's own docstring): confirming a
    candidate whose detection was already classified during the original
    run should reuse that result, not attempt a duplicate insert that
    would violate the OneToOne constraint (and, silently, do nothing useful
    — that failure was previously masked by confirm_new_candidate()'s own
    broad except-and-ignore).
    """
    existing = TreeHealthClassification.objects.filter(tree_detection=detection).first()
    if existing is not None:
        return existing

    classifier = classifier or get_classifier()
    lon, lat = detection.detected_location.x, detection.detected_location.y
    native_gsd_m = _native_gsd_m(transform, crs)
    native_patch_size_px = classification_native_patch_size_px(CLASSIFICATION_PATCH_SIZE_PX, native_gsd_m)
    patch = crop_patch(clipped_array, transform, lon, lat, crs=crs, patch_size_px=native_patch_size_px)
    if patch is None:
        label, confidence = "healthy", 0.0  # unusable patch -> treated exactly like a low-confidence result, always routed to review
    else:
        label, confidence = classifier(patch)

    classification = TreeHealthClassification(
        tree_detection=detection,
        health_label=label,
        confidence=confidence,
        reviewed=False,
    )
    if patch is not None:
        # Persisted so the review queue can show what the classifier
        # actually saw — the source image may later move to cold storage
        # or be deleted (see the new "delete imagery" action), but the
        # patch a human is reviewing shouldn't disappear along with it.
        import io

        from django.core.files.base import ContentFile

        buffer = io.BytesIO()
        patch.save(buffer, format="JPEG", quality=85)
        classification.patch_image.save(f"{detection.id}.jpg", ContentFile(buffer.getvalue()), save=False)
    classification.save()

    # Found live, not by inspection: reported directly — every tree on the
    # Farm Map rendered gray (healthColors.unknown) regardless of its real
    # classification, because Tree.current_health was never actually
    # written from a classification result anywhere in this codebase; it
    # just sat at its model default ("unknown") forever. Only reached for
    # an already-matched detection (detection.tree_id set) — a
    # new_candidate detection has no Tree yet at classification time; see
    # confirm_new_candidate()'s matching sync for that case.
    if detection.tree_id:
        detection.tree.current_health = classification.effective_label
        detection.tree.save(update_fields=["current_health"])

    return classification


def run_processing(run: ImageProcessingRun, detector=None, classifier=None) -> ImageProcessingRun:
    """Full pipeline sequence — docs/06-ml-pipeline.md § Full pipeline
    sequence. `detector`/`classifier` are injectable purely for tests; real
    callers should leave them unset and get whatever apps/imagery/ml/*.py's
    get_detector()/get_classifier() currently resolve to (stub or real,
    depending on settings.DETECTION_MODEL_PATH/CLASSIFICATION_MODEL_PATH).

    Every stage — the no-boundary refusal, clip, detect, classify — fails
    the same way: STATUS_FAILED, a stage-labeled error_message, and
    _notify_processing_failure(run). This function always runs inside an
    async Celery task (apps/imagery/tasks.py), so whoever triggered it
    isn't watching a response come back; the notification is the only way
    they find out something went wrong.

    Found while auditing this function fresh (not reported by a user):
    originally only the clip stage did all three of those things. The
    no-boundary refusal set STATUS_FAILED but never called
    _notify_processing_failure — inconsistent with the clip-failure path
    a few lines below it. Worse, the detection call (`detector(...)`) and
    the classification loop (`classify_detection(...)`) had no error
    handling at all — an exception from either (a real ML model can
    legitimately raise: OOM, a corrupt weights file, an unexpected patch
    shape) would propagate straight out of the Celery task, leaving the
    run stuck at STATUS_DETECTING/STATUS_CLASSIFYING forever with no
    error_message and no notification — worse than "failed," because
    nothing ever tells anyone to go look. Fixed by giving every stage the
    same try/except-and-notify treatment the clip stage already had,
    rather than just flagging it, per this project's usual rule.
    """
    block = run.farm_block

    def _fail(message):
        run.status = ImageProcessingRun.STATUS_FAILED
        run.error_message = message
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at"])
        _notify_processing_failure(run)
        return run

    if not block.boundary:
        run.started_at = run.started_at or timezone.now()
        run.save(update_fields=["started_at"])
        return _fail("This block has no boundary polygon — draw it before processing imagery against it.")

    run.status = ImageProcessingRun.STATUS_CLIPPING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    try:
        clipped_array, clipped_transform, clip_crs = clip_to_block(run.image, block)
    except Exception as exc:  # noqa: BLE001 — any clip failure must land in error_message, not crash the task
        return _fail(f"Clipping failed: {exc}")

    # Persists the SAME crop just computed above as a standalone block-scoped
    # COG/preview — requested directly: crop once, feed the ML pipeline
    # below from that exact crop, rather than clipping twice (once here,
    # once for detection) from two independent mask() calls. Best-effort —
    # never blocks detection/classification, which proceed against
    # clipped_array regardless of whether this succeeds; failure is recorded
    # on run.clip_persist_error by persist_clipped_block_imagery() itself,
    # not raised here.
    persist_clipped_block_imagery(run, clipped_array, clipped_transform, clip_crs)

    run.status = ImageProcessingRun.STATUS_DETECTING
    run.progress_current = 0
    run.progress_total = 0
    run.save(update_fields=["status", "progress_current", "progress_total"])

    def _on_tile_progress(tiles_done, tiles_total):
        # Cheap, frequent updates (once per tile — a large block can be a
        # few hundred tiles) — a plain two-field save, not a full model
        # save, and deliberately not wrapped in the same try/except as the
        # detection call below: a progress-reporting failure shouldn't be
        # indistinguishable from an actual detection failure in
        # error_message. Only reached for the real detector (see
        # get_detector()'s own docstring) — injected fake detectors used
        # in tests never call this.
        run.progress_current = tiles_done
        run.progress_total = tiles_total
        run.save(update_fields=["progress_current", "progress_total"])

    try:
        detector = detector or get_detector(
            iou_threshold=block.effective_crown_detection_iou_threshold(),
            on_tile_progress=_on_tile_progress,
            target_gsd_m=block.effective_detection_target_gsd_m(),
        )
        raw_detections = detector(clipped_array, clipped_transform, clip_crs)
    except Exception as exc:  # noqa: BLE001 — same reasoning as the clip stage above
        return _fail(f"Detection failed: {exc}")

    threshold_m = block.effective_tree_match_threshold_m()
    detected_count = 0
    new_candidate_count = 0
    created_detections = []

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
        created_detections.append(detection)

    run.detected_tree_count = detected_count
    run.new_candidate_count = new_candidate_count

    if created_detections:
        # Every detection gets classified here, not just matched ones —
        # asked directly: a fresh block's first-ever detection pass has no
        # existing trees to match against, so "only classify matched
        # detections" meant nothing ever got a health label until a human
        # confirmed each new_candidate one at a time in the review queue.
        # Classifying everything up front means a label (and the patch
        # image it was based on) is already there the moment a Manager
        # opens the queue — the small cost is a genuine false-positive
        # crown (rare: this project's detector precision is 0.989 on its
        # own held-out evaluation) getting classified for nothing before
        # a human rejects it.
        run.status = ImageProcessingRun.STATUS_CLASSIFYING
        run.progress_current = 0
        run.progress_total = len(created_detections)
        run.save(update_fields=["status", "detected_tree_count", "new_candidate_count", "progress_current", "progress_total"])
        try:
            classifier_fn = classifier or get_classifier()
            for i, detection in enumerate(created_detections, start=1):
                classify_detection(detection, clipped_array, clipped_transform, crs=clip_crs, classifier=classifier_fn)
                run.progress_current = i
                run.save(update_fields=["progress_current"])
        except Exception as exc:  # noqa: BLE001 — same reasoning as the clip stage above. The TreeDetection
            # rows already created above are kept (they're real detections,
            # not invalidated by a classification failure) — only
            # classification itself is marked failed, same trade-off
            # confirm_new_candidate() already makes for the same reason.
            return _fail(f"Classification failed: {exc}")

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


def confirm_new_candidate(detection: TreeDetection, user, classifier=None, clipped=None) -> Tree:
    """Review-queue action: docs/04-api-spec.md POST /api/detections/{id}/confirm/
    — "Creates a Tree row with source=drone_detected." Also (re-)runs
    classification for the newly-confirmed tree's detection, but only if
    the original clipped raster is still available; if the processing
    run's source image has since been deleted, classification is skipped
    rather than failing the confirmation itself (confirming the tree is
    the important action; a missing health label can be backfilled later).

    In practice this almost always no-ops on the classification side now:
    run_processing() classifies every detection up front (matched and
    new_candidate alike — see that function's own docstring), so by the
    time a detection reaches this confirm action it typically already has
    a TreeHealthClassification, and classify_detection()'s own idempotency
    check returns that existing row instead of re-running the model. This
    call is kept, not removed, for the case that still needs it: a
    detection confirmed from a run whose classification stage itself
    failed (STATUS_FAILED detections still keep their TreeDetection rows —
    see run_processing()'s docstring — just never got classified).

    `clipped`: an optional, already-computed `(array, transform, crs)`
    triple from clip_to_block(). Exists for the bulk-confirm path
    (apps/dashboard/views.py's ConfirmAllDetectionsView): clipping is a
    full masked rasterio read of a real multi-GB orthomosaic region, so a
    loop that let each detection clip independently would redo that entire
    read once per detection — for a run whose classification stage failed
    (exactly the case that reaches the clip at all), that's dozens of
    full-raster reads inside one synchronous request. Passing the clip in
    once collapses that to a single read for the whole batch. Left
    optional rather than required so the single-detection callers (the
    DRF confirm action, ConfirmDetectionView) stay unchanged — for one
    detection there's nothing to amortize.
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

    # Cheap existence check before paying for a real rasterio clip — per
    # this function's own docstring, that's the common case now that
    # run_processing() classifies every detection up front.
    classification = TreeHealthClassification.objects.filter(tree_detection=detection).first()
    if classification is None:
        try:
            if clipped is None:
                clipped = clip_to_block(detection.processing_run.image, detection.processing_run.farm_block)
            clipped_array, clipped_transform, clip_crs = clipped
            classification = classify_detection(
                detection, clipped_array, clipped_transform, crs=clip_crs, classifier=classifier
            )
        except Exception:  # noqa: BLE001 — see docstring: classification failure must not block confirmation
            pass

    # Same fix as classify_detection()'s own matched-detection sync above —
    # this Tree didn't exist yet when run_processing() originally
    # classified this (new_candidate) detection, so that sync couldn't
    # reach it; this is the other half of the same fix for the
    # new_candidate path.
    if classification is not None:
        tree.current_health = classification.effective_label
        tree.save(update_fields=["current_health"])

    return tree
