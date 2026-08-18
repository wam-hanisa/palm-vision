"""
Imagery upload, detection, classification, and model governance.
See docs/06-ml-pipeline.md and docs/03-data-model.md (source of truth for
field names — this file follows that ERD).
"""
import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models

from apps.farms.models import FarmBlock
from apps.trees.models import Tree


class ModelVersion(models.Model):
    """One row per trained model — detection and classification versioned
    independently. docs/06-ml-pipeline.md § Model governance & promotion
    workflow: staging -> production -> retired, never deleted (a bad
    promotion is a status change, not a redeployment scramble).
    """

    TYPE_DETECTION = "detection"
    TYPE_CLASSIFICATION = "classification"
    TYPE_CHOICES = [
        (TYPE_DETECTION, "Detection"),
        (TYPE_CLASSIFICATION, "Classification"),
    ]

    STATUS_STAGING = "staging"
    STATUS_PRODUCTION = "production"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_STAGING, "Staging"),
        (STATUS_PRODUCTION, "Production"),
        (STATUS_RETIRED, "Retired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_STAGING)
    trained_at = models.DateField()
    dataset_version = models.CharField(max_length=255, blank=True)
    trained_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    validation_metrics = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-trained_at"]

    def __str__(self):
        return f"{self.get_model_type_display()} v{str(self.id)[:8]} ({self.status})"

    def promote_to_production(self):
        """Flips the current production model of the same type to retired,
        then promotes this one. Not enforced as the only way to change
        status (admin/API can still set status directly) — this is the
        convenience path that keeps the "only one production model per
        type at a time" invariant, per docs/06-ml-pipeline.md's rollback
        description.
        """
        ModelVersion.objects.filter(model_type=self.model_type, status=self.STATUS_PRODUCTION).update(
            status=self.STATUS_RETIRED
        )
        self.status = self.STATUS_PRODUCTION
        self.save(update_fields=["status"])


class Image(models.Model):
    """A drone-captured raw image/orthomosaic, uploaded at the Farm level —
    docs/06-ml-pipeline.md: "one file can cover several blocks." Never
    belongs to a single FarmBlock; that association happens per
    ImageProcessingRun instead.
    """

    GEOREFERENCE_PENDING = "pending"
    GEOREFERENCE_DONE = "done"
    GEOREFERENCE_FAILED = "failed"
    GEOREFERENCE_STATUS_CHOICES = [
        (GEOREFERENCE_PENDING, "Pending"),
        (GEOREFERENCE_DONE, "Done"),
        (GEOREFERENCE_FAILED, "Failed"),
    ]

    STORAGE_TIER_HOT = "hot"
    STORAGE_TIER_COLD = "cold"
    STORAGE_TIER_CHOICES = [
        (STORAGE_TIER_HOT, "Hot"),
        (STORAGE_TIER_COLD, "Cold"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey("farms.Farm", on_delete=models.CASCADE, related_name="images")
    raw_file = models.FileField(upload_to="imagery/raw/%Y/%m/")
    captured_at = models.DateTimeField(null=True, blank=True)
    georeference_status = models.CharField(
        max_length=16, choices=GEOREFERENCE_STATUS_CHOICES, default=GEOREFERENCE_PENDING
    )
    georeference_error = models.TextField(blank=True)
    footprint = gis_models.PolygonField(srid=4326, null=True, blank=True, help_text="Computed after georeferencing")
    preview_file = models.ImageField(
        upload_to="imagery/previews/%Y/%m/",
        null=True,
        blank=True,
        help_text=(
            "Downsampled web-viewable PNG generated after georeferencing — "
            "docs/06-ml-pipeline.md step 4's 'simpler downsampled preview' "
            "option (rather than a full rio-tiler/titiler tile pyramid) so "
            "the office dashboard's map can actually render the image, not "
            "just its footprint outline. See generate_preview() in "
            "apps/imagery/services.py. This is 'derived data' for "
            "docs/12-security-ops.md's storage lifecycle policy — it stays "
            "hot indefinitely even after raw_file moves to cold storage."
        ),
    )
    preview_error = models.TextField(
        blank=True, help_text="Set if generate_preview() failed — never blocks georeferencing or upload itself."
    )
    storage_tier = models.CharField(
        max_length=8,
        choices=STORAGE_TIER_CHOICES,
        default=STORAGE_TIER_HOT,
        help_text="docs/12-security-ops.md § Image storage lifecycle & cost policy — see apply_storage_lifecycle().",
    )
    cold_storage_since = models.DateTimeField(null=True, blank=True)
    is_cog = models.BooleanField(
        default=False,
        help_text=(
            "True once raw_file has been re-encoded in place as a Cloud "
            "Optimized GeoTIFF (internally tiled, with baked-in overview "
            "levels) — see convert_to_cog() in apps/imagery/services.py, "
            "run automatically right after upload, before georeferencing. "
            "A plain (non-COG) GeoTIFF is still fully usable — every read "
            "in this app works identically either way — this just makes "
            "generate_preview()/clip_to_block()/crop_patch()'s windowed "
            "reads faster, and is the standard input format if this "
            "project ever adds real tile serving (docs/06-ml-pipeline.md's "
            "other stated alternative to a downsampled preview)."
        ),
    )
    cog_error = models.TextField(
        blank=True,
        help_text="Set if convert_to_cog() failed — never blocks georeferencing or preview generation; raw_file is left as originally uploaded.",
    )
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Image {str(self.id)[:8]} — {self.farm.name}"

    def overlapping_blocks(self):
        """docs/04-api-spec.md: GET /api/images/{id}/overlapping-blocks/ —
        "suggests which blocks a file's footprint intersects." Only
        meaningful once georeferencing has produced a footprint.
        """
        if not self.footprint:
            return FarmBlock.objects.none()
        return FarmBlock.objects.filter(farm=self.farm, boundary__intersects=self.footprint)

    def is_compliance_exempt(self):
        """True if this image is referenced as evidence by a
        DeforestationBaseline (or, in future, any other compliance record
        that comes to reference images) — docs/12-security-ops.md § Image
        storage lifecycle & cost policy: such images are exempt from the
        standard tiering/deletion lifecycle and follow the stricter
        5-year-minimum retention policy instead. The general lifecycle job
        itself isn't built yet (not part of Phase 6's scope per
        docs/02-build-plan.md); this is the flag that job will need to check.
        """
        return self.deforestation_baselines.exists()


class ImageUploadSession(models.Model):
    """Tracks an in-progress *chunked* imagery upload — docs/06-ml-pipeline.md:
    "upload happens once back on real bandwidth, from the office dashboard."
    A single synchronous multipart POST (still what `ImageViewSet.create()`
    accepts, unchanged) works fine for this project's small test rasters,
    but a real drone orthomosaic is routinely hundreds of MB to a few GB —
    over office WiFi that's minutes of upload with no way to resume if it
    drops partway. This model is the server-side bookkeeping for a
    chunked-upload flow: the dashboard's upload form (`apps/dashboard/views.py`)
    slices the file client-side and PUTs each chunk separately; this row
    tracks which chunk indices have actually landed, so a browser
    reload/crash mid-upload can resume from where it left off instead of
    restarting the whole file.

    Deliberately not exposed on the JWT API — chunked upload is a
    dashboard-only concern (the office-only upload path this project
    already restricts to Managers/Super Admins), not something the mobile
    PWA or third-party API clients need. Session lifecycle functions
    (create_upload_session, store_chunk, finalize_upload_session) live in
    apps/imagery/services.py alongside georeference_image() and
    run_processing() — same module, since finalizing a session ends by
    creating a real Image and kicking off the same georeference task those
    functions already own.
    """

    STATUS_UPLOADING = "uploading"
    STATUS_COMPLETE = "complete"
    STATUS_CHOICES = [
        (STATUS_UPLOADING, "Uploading"),
        (STATUS_COMPLETE, "Complete"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey("farms.Farm", on_delete=models.CASCADE, related_name="image_upload_sessions")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    filename = models.CharField(max_length=255)
    captured_at = models.DateTimeField(null=True, blank=True)
    total_chunks = models.PositiveIntegerField()
    received_chunk_indexes = models.JSONField(
        default=list, help_text="Chunk indexes (0-based) whose bytes have been stored so far — see store_chunk()."
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UPLOADING)
    image = models.ForeignKey(
        Image, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="Set once finalize_upload_session() successfully assembles the chunks into a real Image.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Upload {str(self.id)[:8]} — {self.filename} ({len(self.received_chunk_indexes)}/{self.total_chunks})"

    @property
    def is_complete_upload(self):
        return len(set(self.received_chunk_indexes)) == self.total_chunks


class ImageProcessingRun(models.Model):
    """Ties one Image to one FarmBlock for a single detect+classify pass.
    Status tracked per-run, not per-Image, so "Block 12: ready, Block 13:
    still processing" can coexist for the same uploaded file
    (docs/06-ml-pipeline.md step 6).
    """

    STATUS_PENDING = "pending"
    STATUS_CLIPPING = "clipping"
    STATUS_DETECTING = "detecting"
    STATUS_CLASSIFYING = "classifying"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CLIPPING, "Clipping"),
        (STATUS_DETECTING, "Detecting"),
        (STATUS_CLASSIFYING, "Classifying"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="processing_runs")
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="processing_runs")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    detection_model_version = models.ForeignKey(
        ModelVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="detection_runs"
    )
    classification_model_version = models.ForeignKey(
        ModelVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="classification_runs"
    )
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    detected_tree_count = models.PositiveIntegerField(default=0)
    new_candidate_count = models.PositiveIntegerField(default=0)
    progress_current = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Reused across whichever stage is currently running — orthomosaic tiles processed so far "
            "during 'detecting' (see apps/imagery/ml/detection.py's on_tile_progress callback), or "
            "detections classified so far during 'classifying'. Meaningless outside those two statuses; "
            "see progress_percent."
        ),
    )
    progress_total = models.PositiveIntegerField(
        default=0, help_text="The denominator progress_current counts up to for the current stage — see progress_current."
    )
    clipped_cog_file = models.FileField(
        upload_to="imagery/clipped/%Y/%m/",
        null=True,
        blank=True,
        help_text=(
            "image.raw_file physically clipped to farm_block.boundary and re-encoded as a standalone "
            "Cloud Optimized GeoTIFF — see apps/imagery/services.py's persist_clipped_block_imagery(). "
            "Distinct from clip_to_block()'s own in-memory clip (used transiently for detection/"
            "classification and then discarded): this is a real, reusable file, generated so a real "
            "orthomosaic that spans several blocks can be displayed/re-processed one block at a time "
            "without ever showing (or re-reading) ground that belongs to a different block."
        ),
    )
    clipped_preview_file = models.ImageField(
        upload_to="imagery/clipped_previews/%Y/%m/",
        null=True,
        blank=True,
        help_text=(
            "Downsampled web-viewable PNG rendered from clipped_cog_file — the block-scoped counterpart "
            "to Image.preview_file (which always shows the WHOLE uploaded file's footprint). This is what "
            "the Farm Map actually overlays for a selected block, so a block's imagery view no longer "
            "shows real pixels belonging to a neighboring block."
        ),
    )
    clipped_footprint = gis_models.PolygonField(
        srid=4326,
        null=True,
        blank=True,
        help_text=(
            "The real geographic extent of clipped_cog_file/clipped_preview_file — farm_block.boundary "
            "intersected with image.footprint (an image doesn't always fully cover the block it's being "
            "processed against), reprojected to 4326. Used as the Leaflet overlay bounds for "
            "clipped_preview_file — using farm_block.boundary's own (rectangular bounding box of the) "
            "extent instead would stretch the image over ground the clip never actually covered."
        ),
    )
    clip_persist_error = models.TextField(
        blank=True,
        help_text=(
            "Set if persist_clipped_block_imagery() failed — never blocks detection/classification, which "
            "use clip_to_block()'s own in-memory clip directly and don't depend on this at all."
        ),
    )
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run {str(self.id)[:8]} — {self.farm_block} ({self.status})"

    @property
    def progress_percent(self):
        """An honest percentage, not a fake per-stage guess — reported
        directly by apps/imagery/services.py's run_processing() as it
        works through the real unit of work for whichever stage is active
        (tiles for detecting, detections for classifying). None outside
        those two statuses: 'pending'/'clipping' haven't started counting
        anything yet, and 'ready'/'failed' are already done — a caller
        showing a progress bar should treat None as "show the status label
        instead," not silently render 0%.
        """
        if self.status not in (self.STATUS_DETECTING, self.STATUS_CLASSIFYING):
            return None
        if not self.progress_total:
            return 0
        return round(100 * self.progress_current / self.progress_total)


class TreeDetection(gis_models.Model):
    """One detected tree crown from one processing run. Never overwritten —
    a new run produces new rows, which is what gives a real history per
    tree over time (docs/03-data-model.md note).
    """

    MATCH_MATCHED = "matched"
    MATCH_NEW_CANDIDATE = "new_candidate"
    MATCH_AMBIGUOUS = "ambiguous"
    MATCH_REJECTED = "rejected"
    MATCH_STATUS_CHOICES = [
        (MATCH_MATCHED, "Matched"),
        (MATCH_NEW_CANDIDATE, "New candidate"),
        (MATCH_AMBIGUOUS, "Ambiguous"),
        (MATCH_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    processing_run = models.ForeignKey(ImageProcessingRun, on_delete=models.CASCADE, related_name="detections")
    tree = models.ForeignKey(Tree, null=True, blank=True, on_delete=models.SET_NULL, related_name="detections")
    detected_location = gis_models.PointField(srid=4326)
    detection_confidence = models.FloatField()
    match_status = models.CharField(max_length=16, choices=MATCH_STATUS_CHOICES, default=MATCH_NEW_CANDIDATE)
    matched_distance_m = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["processing_run", "match_status"])]

    def __str__(self):
        return f"Detection {str(self.id)[:8]} ({self.match_status})"

    @property
    def farm_block(self):
        """Lets apps.common.permissions.IsAssignedPicBlockOrManagerOrSuperAdmin
        (which checks `obj.farm_block`) work on a TreeDetection directly,
        without duplicating its scope-check logic here or on ModelVersion/
        ImageProcessingRun-specific permission classes.
        """
        return self.processing_run.farm_block


class TreeHealthClassification(models.Model):
    """Health classification for one TreeDetection's cropped patch. At most
    one per detection (docs/03-data-model.md ERD: zero-or-one) — history
    over time comes from new TreeDetection rows across processing runs,
    not from overwriting this row.
    """

    HEALTH_HEALTHY = "healthy"
    HEALTH_UNKNOWN = "unknown"
    HEALTH_YELLOW = "yellow"
    HEALTH_CHOICES = [
        (HEALTH_HEALTHY, "Healthy"),
        (HEALTH_UNKNOWN, "Unknown"),
        (HEALTH_YELLOW, "Yellow"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tree_detection = models.OneToOneField(TreeDetection, on_delete=models.CASCADE, related_name="classification")
    health_label = models.CharField(max_length=16, choices=HEALTH_CHOICES)
    confidence = models.FloatField()
    patch_image = models.ImageField(
        upload_to="imagery/classification_patches/%Y/%m/",
        null=True,
        blank=True,
        help_text=(
            "The exact cropped patch classify_detection() fed to the "
            "classifier, saved at classification time so the review queue "
            "can show what the model actually saw — see "
            "apps/imagery/services.py classify_detection(). Never "
            "regenerated on read: the source Image may later move to cold "
            "storage or be deleted (see Image delete), but this patch "
            "shouldn't disappear along with it. Null only when the patch "
            "itself was unusable (crop_patch() returned None, e.g. a "
            "detection too close to the clipped raster's edge) — same "
            "case that already forces health_label to the review-routing "
            "default rather than a real prediction."
        ),
    )
    reviewed = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    corrected_label = models.CharField(max_length=16, choices=HEALTH_CHOICES, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.health_label} ({self.confidence:.2f})"

    @property
    def effective_label(self):
        """What should actually drive Tree.current_health — the human
        correction if one was made, otherwise the model's prediction.
        """
        return self.corrected_label or self.health_label
