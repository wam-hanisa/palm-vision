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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey("farms.Farm", on_delete=models.CASCADE, related_name="images")
    raw_file = models.FileField(upload_to="imagery/raw/%Y/%m/")
    captured_at = models.DateTimeField(null=True, blank=True)
    georeference_status = models.CharField(
        max_length=16, choices=GEOREFERENCE_STATUS_CHOICES, default=GEOREFERENCE_PENDING
    )
    georeference_error = models.TextField(blank=True)
    footprint = gis_models.PolygonField(srid=4326, null=True, blank=True, help_text="Computed after georeferencing")
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
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run {str(self.id)[:8]} — {self.farm_block} ({self.status})"


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
    HEALTH_SMALL = "small"
    HEALTH_YELLOW = "yellow"
    HEALTH_CHOICES = [
        (HEALTH_HEALTHY, "Healthy"),
        (HEALTH_SMALL, "Small"),
        (HEALTH_YELLOW, "Yellow"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tree_detection = models.OneToOneField(TreeDetection, on_delete=models.CASCADE, related_name="classification")
    health_label = models.CharField(max_length=16, choices=HEALTH_CHOICES)
    confidence = models.FloatField()
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
