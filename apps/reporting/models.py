"""
Report generation — docs/08-reporting.md. `ReportDefinition` (what to
generate, how often) + `ReportRun` (one generated instance), the same
async-job pattern `apps/imagery/models.py`'s `ImageProcessingRun` uses —
generation happens in a Celery task, this model just tracks its progress
and holds the resulting file.

docs/02-build-plan.md Phase 7 scopes report *generation* down to two of the
five types described in docs/08-reporting.md: "PDF export (WeasyPrint) and
Excel export (openpyxl) for at least the production report and tree-health
report." `report_type` still lists all five from the doc (input, compliance,
and model_performance are real, named report types a `ReportDefinition` can
declare) — but `apps/reporting/services.py` only implements data-building
for `PRODUCTION` and `TREE_HEALTH`; the other three raise a clear
`NotImplementedError` that lands in `ReportRun.error_message` rather than
silently producing an empty or wrong file. See that file's docstring.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.farms.models import Farm


class ReportDefinition(models.Model):
    TYPE_PRODUCTION = "production"
    TYPE_INPUT = "input"
    TYPE_TREE_HEALTH = "tree_health"
    TYPE_COMPLIANCE = "compliance"
    TYPE_MODEL_PERFORMANCE = "model_performance"
    TYPE_CHOICES = [
        (TYPE_PRODUCTION, "Production"),
        (TYPE_INPUT, "Input (fertilizer)"),
        (TYPE_TREE_HEALTH, "Tree health"),
        (TYPE_COMPLIANCE, "Compliance"),
        (TYPE_MODEL_PERFORMANCE, "Model performance"),
    ]

    FORMAT_PDF = "pdf"
    FORMAT_XLSX = "xlsx"
    FORMAT_CHOICES = [
        (FORMAT_PDF, "PDF"),
        (FORMAT_XLSX, "Excel"),
    ]

    RECURRENCE_NONE = "none"
    RECURRENCE_DAILY = "daily"
    RECURRENCE_WEEKLY = "weekly"
    RECURRENCE_MONTHLY = "monthly"
    RECURRENCE_CHOICES = [
        (RECURRENCE_NONE, "None (on-demand only)"),
        (RECURRENCE_DAILY, "Daily"),
        (RECURRENCE_WEEKLY, "Weekly"),
        (RECURRENCE_MONTHLY, "Monthly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="report_definitions")
    name = models.CharField(max_length=255)
    report_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    export_format = models.CharField(max_length=8, choices=FORMAT_CHOICES, default=FORMAT_PDF)
    filters = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Optional keys: farm_block_id (restrict to one block, else the "
            "whole farm), date_from / date_to (ISO date strings; defaults to "
            "the trailing 30 days if omitted — see apps/reporting/services.py)."
        ),
    )
    recurrence = models.CharField(max_length=16, choices=RECURRENCE_CHOICES, default=RECURRENCE_NONE)
    subscribers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="subscribed_reports",
        help_text="Who gets emailed the generated file when this runs on a schedule.",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_report_type_display()})"


class ReportRun(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(ReportDefinition, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    file = models.FileField(upload_to="reports/%Y/%m/", null=True, blank=True)
    error_message = models.TextField(blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Null means this run was triggered by a Celery Beat schedule, not a user's on-demand request.",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run {str(self.id)[:8]} — {self.definition} ({self.status})"
