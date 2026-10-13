"""
Compliance & due-diligence records — docs/07-compliance.md.

History (django-simple-history): docs/12-security-ops.md's audit-logging
list names ComplianceDocument, ComplianceAudit, and DeforestationBaseline
explicitly as models that need `history`. ChecklistItem/AuditResult/
ComplianceAuditTemplate/HarvestBatch/GrievanceRecord aren't on that list, so
they're left without HistoricalRecords here rather than added speculatively.

Two deliberate departures from docs/07-compliance.md's literal ERD, both
noted at the point they're made below:
  1. ComplianceDocument.status and DeforestationBaseline is_expiring/expired
     equivalents are computed properties, not stored fields — the Phase 6
     build-plan checklist item says status is "computed from expiry_date",
     which only stays true if it's derived every read, not cached.
  2. DeforestationBaseline.evidence_image is an addition beyond the ERD's
     field list. docs/12-security-ops.md's storage-lifecycle section refers
     to "any image referenced by a DeforestationBaseline" as exempt from
     the standard imagery retention/tiering policy, but 07-compliance.md's
     own field list has no image FK on DeforestationBaseline to hang that
     exemption off of. Added `evidence_image` (nullable) as the concrete
     hook so that reference is actually representable. This is an inference
     to reconcile two docs, not something either doc states outright —
     flagged here, and in BUILD_STATUS.md, rather than silently assumed.
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.farms.models import Farm, FarmBlock


class ComplianceDocument(models.Model):
    """A certificate/permit/land-title file attached to a Farm or a
    FarmBlock (exactly one of the two — see clean()/save() below).
    docs/07-compliance.md.
    """

    TYPE_LAND_TITLE = "land_title"
    TYPE_AMDAL_PERMIT = "amdal_permit"
    TYPE_ISPO_CERT = "ispo_cert"
    TYPE_RSPO_CERT = "rspo_cert"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_LAND_TITLE, "Land title"),
        (TYPE_AMDAL_PERMIT, "AMDAL permit"),
        (TYPE_ISPO_CERT, "ISPO certificate"),
        (TYPE_RSPO_CERT, "RSPO certificate"),
        (TYPE_OTHER, "Other"),
    ]

    STATUS_VALID = "valid"
    STATUS_EXPIRING_SOON = "expiring_soon"
    STATUS_EXPIRED = "expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, null=True, blank=True, on_delete=models.CASCADE, related_name="compliance_documents")
    farm_block = models.ForeignKey(
        FarmBlock, null=True, blank=True, on_delete=models.CASCADE, related_name="compliance_documents"
    )
    document_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    document_file = models.FileField(upload_to="compliance_documents/")
    issued_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, help_text="Blank for documents with no expiry, e.g. land titles.")
    issuing_authority = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_document_type_display()} — {self.farm or self.farm_block}"

    def clean(self):
        super().clean()
        if bool(self.farm_id) == bool(self.farm_block_id):
            raise ValidationError("A compliance document must be attached to exactly one of farm or farm_block.")

    def save(self, *args, **kwargs):
        if bool(self.farm_id) == bool(self.farm_block_id):
            raise ValidationError("A compliance document must be attached to exactly one of farm or farm_block.")
        super().save(*args, **kwargs)

    @property
    def status(self):
        """valid / expiring_soon / expired, computed from expiry_date —
        docs/02-build-plan.md Phase 6 checklist. No expiry_date at all
        (e.g. a land title) is always 'valid'. Window is
        settings.COMPLIANCE_EXPIRY_WARNING_DAYS (default 90).
        """
        if not self.expiry_date:
            return self.STATUS_VALID
        today = timezone.localdate()
        if self.expiry_date < today:
            return self.STATUS_EXPIRED
        warning_days = getattr(settings, "COMPLIANCE_EXPIRY_WARNING_DAYS", 90)
        if (self.expiry_date - today).days <= warning_days:
            return self.STATUS_EXPIRING_SOON
        return self.STATUS_VALID


class DeforestationBaseline(models.Model):
    """One per FarmBlock (at most) — the EUDR/no-deforestation-since-cutoff
    determination for that block. docs/07-compliance.md.
    """

    STATUS_DEFORESTATION_FREE = "deforestation_free"
    STATUS_FLAGGED = "flagged"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_CHOICES = [
        (STATUS_DEFORESTATION_FREE, "Deforestation-free"),
        (STATUS_FLAGGED, "Flagged"),
        (STATUS_UNDER_REVIEW, "Under review"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_block = models.OneToOneField(FarmBlock, on_delete=models.CASCADE, related_name="deforestation_baseline")
    cutoff_date = models.DateField(default="2020-12-31", help_text="EUDR cutoff date, unless overridden per-scheme.")
    source = models.CharField(max_length=255, blank=True, help_text="e.g. satellite imagery provider, survey report.")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_UNDER_REVIEW)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    verified_at = models.DateTimeField(null=True, blank=True)
    evidence_image = models.ForeignKey(
        "imagery.Image",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deforestation_baselines",
        help_text=(
            "Optional link to the supporting imagery for this determination. "
            "docs/12-security-ops.md: an image referenced here is exempt from "
            "the standard imagery storage lifecycle and follows the stricter "
            "5-year-minimum retention policy instead — see Image.is_compliance_exempt()."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    def __str__(self):
        return f"Deforestation baseline — {self.farm_block} — {self.status}"


class ComplianceAuditTemplate(models.Model):
    """A reusable checklist definition per certification scheme (ISPO, RSPO,
    internal, ...). docs/07-compliance.md mentions this and ChecklistItem in
    prose without an explicit field list; fields below are the minimum
    needed to make "a reusable checklist per certification scheme" concrete.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    certification_scheme = models.CharField(max_length=100, blank=True, help_text="e.g. ISPO, RSPO, internal.")
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ChecklistItem(models.Model):
    """One line item within a ComplianceAuditTemplate."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(ComplianceAuditTemplate, on_delete=models.CASCADE, related_name="checklist_items")
    text = models.CharField(max_length=500)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["template", "order"]

    def __str__(self):
        return self.text


class ComplianceAudit(models.Model):
    """One audit event against a FarmBlock, scored against a
    ComplianceAuditTemplate's checklist. docs/07-compliance.md.
    """

    AUDITOR_INTERNAL = "internal"
    AUDITOR_THIRD_PARTY = "third_party"
    AUDITOR_TYPE_CHOICES = [
        (AUDITOR_INTERNAL, "Internal"),
        (AUDITOR_THIRD_PARTY, "Third party"),
    ]

    STATUS_PASSED = "passed"
    STATUS_FAILED = "failed"
    STATUS_CONDITIONAL = "conditional"
    STATUS_CHOICES = [
        (STATUS_PASSED, "Passed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CONDITIONAL, "Conditional"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="compliance_audits")
    template = models.ForeignKey(ComplianceAuditTemplate, on_delete=models.PROTECT, related_name="audits")
    audit_date = models.DateField()
    auditor_name = models.CharField(max_length=255)
    auditor_type = models.CharField(max_length=32, choices=AUDITOR_TYPE_CHOICES)
    overall_status = models.CharField(max_length=32, choices=STATUS_CHOICES)
    next_audit_due = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-audit_date"]

    def __str__(self):
        return f"Audit — {self.farm_block} — {self.audit_date}"


class AuditResult(models.Model):
    """Per-checklist-item outcome within a ComplianceAudit."""

    RESULT_PASS = "pass"
    RESULT_FAIL = "fail"
    RESULT_NOT_APPLICABLE = "not_applicable"
    RESULT_CHOICES = [
        (RESULT_PASS, "Pass"),
        (RESULT_FAIL, "Fail"),
        (RESULT_NOT_APPLICABLE, "Not applicable"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit = models.ForeignKey(ComplianceAudit, on_delete=models.CASCADE, related_name="results")
    checklist_item = models.ForeignKey(ChecklistItem, on_delete=models.PROTECT, related_name="results")
    result = models.CharField(max_length=32, choices=RESULT_CHOICES)
    evidence_file = models.FileField(upload_to="compliance_audit_evidence/", null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ("audit", "checklist_item")

    def __str__(self):
        return f"{self.checklist_item} — {self.result}"


class HarvestBatch(models.Model):
    """Aggregates ActualHarvest records from one FarmBlock over a period,
    for export/traceability filing (docs/07-compliance.md,
    docs/09-eudr? — see § EUDR-specific functions in docs/07-compliance.md).

    total_quantity/total_weight_kg are stored snapshots, not live-computed
    properties — a due-diligence filing should reflect the batch's totals as
    of when it was closed/exported, not silently change if new ActualHarvest
    rows get linked afterwards. Call recompute_totals() explicitly (done by
    the export action) to refresh them.

    `total_weight_kg` is the authoritative figure — every harvest record's
    unit is kg now, not bunch count (see apps/operations/models.py).
    `total_quantity` is the optional, best-effort bunch-count total; see
    recompute_totals()'s own docstring for exactly how each is derived.
    """

    EXPORT_STATUS_DRAFT = "draft"
    EXPORT_STATUS_EXPORTED = "exported"
    EXPORT_STATUS_CHOICES = [
        (EXPORT_STATUS_DRAFT, "Draft"),
        (EXPORT_STATUS_EXPORTED, "Exported"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="harvest_batches")
    period_start = models.DateField()
    period_end = models.DateField()
    total_quantity = models.FloatField(default=0)
    total_weight_kg = models.FloatField(default=0)
    export_status = models.CharField(max_length=32, choices=EXPORT_STATUS_CHOICES, default=EXPORT_STATUS_DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period_end"]

    def __str__(self):
        return f"Batch — {self.farm_block} — {self.period_start}..{self.period_end}"

    def recompute_totals(self, save=True):
        """Sums across every ActualHarvest linked to this batch. See
        apps/operations/models.py — ActualHarvest gained a nullable
        `batch` FK for this purpose.

        Update — asked directly to move every harvest record from bunch
        count to kg: `total_weight_kg` is now the authoritative figure,
        summing ActualHarvest.get_effective_quantity() (the same manual-
        total-wins-with-remainder-split rule used everywhere else in this
        pipeline). This also fixes a real, separate bug this method had
        before that change: it used to sum HarvestTree.weight_kg directly,
        which silently ignored an action's own manual ActualHarvest total
        whenever no tree-level breakdown existed for it — the exact
        "tree-sum should never quietly override a real reported total"
        mistake get_effective_quantity() itself was built to prevent.
        `total_quantity` is now the bunch-count total — optional,
        best-effort, summed straight from ActualHarvest.quantity since
        that field is supplementary detail only and has no derived/
        estimated-remainder concept of its own.
        """
        actuals = self.actual_harvests.all()
        total_quantity = 0.0
        total_weight = 0.0
        for actual in actuals:
            total_quantity += actual.quantity or 0
            total_weight += actual.get_effective_quantity() or 0
        self.total_quantity = total_quantity
        self.total_weight_kg = total_weight
        if save:
            self.save(update_fields=["total_quantity", "total_weight_kg", "updated_at"])
        return self.total_quantity, self.total_weight_kg


class GrievanceRecord(models.Model):
    """A logged grievance against a Farm — docs/07-compliance.md."""

    STATUS_OPEN = "open"
    STATUS_INVESTIGATING = "investigating"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_INVESTIGATING, "Investigating"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="grievance_records")
    date_reported = models.DateField()
    description = models.TextField()
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_OPEN)
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_reported"]

    def __str__(self):
        return f"Grievance — {self.farm} — {self.date_reported}"
