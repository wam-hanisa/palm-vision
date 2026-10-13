"""
Celery Beat periodic checks — docs/12-security-ops.md § Alerting &
notifications: "Delivered via Celery Beat periodic checks (most are
daily-digest material) plus event-driven triggers for anything that
shouldn't wait." The one trigger in that doc's list that genuinely needs to
be event-driven — a failed processing run — is NOT here; it's called
directly from apps/imagery/services.py's run_processing() the moment it
happens. Everything else below is legitimately digest-shaped (a daily
"here's what needs attention" scan), which is what Celery Beat's schedule
in palmvision/settings/base.py's CELERY_BEAT_SCHEDULE runs these as.

Each check notifies farm Managers (the office-facing role compliance/
approval/backlog problems are actually actionable by), except
check_stalled_sync, which notifies the affected field user directly — a
Manager can't do anything about someone else's phone being offline; the
person who needs to know is the one holding it.
"""
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.compliance.models import ComplianceDocument
from apps.farms.models import FarmBlock, UserFarmRole
from apps.imagery.models import TreeDetection, TreeHealthClassification
from apps.operations.models import Fertilizer, Harvest
from apps.operations.services import awaiting_approval
from apps.sync.models import SyncCheckIn

from .models import NotificationCategory
from .services import notify, notify_with_snooze


def _block_link(block_id, section=None):
    """Farm Map, opened on this block (and its Harvest/Fertilizer section).
    Notification links used to point at raw API URLs, which aren't anywhere
    a Manager can act — the notifications page now renders these."""
    link = f"{reverse('dashboard:farms')}?block={block_id}"
    return f"{link}&section={section}" if section else link


def _managers_for_farm(farm):
    manager_ids = UserFarmRole.objects.filter(farm=farm, role=UserFarmRole.ROLE_MANAGER).values_list(
        "user_id", flat=True
    )
    from apps.users.models import User

    return User.objects.filter(id__in=manager_ids)


@shared_task
def check_compliance_expiry():
    """Fires on exact-day matches against each configured threshold
    (settings.COMPLIANCE_NOTIFICATION_THRESHOLDS_DAYS, default [90, 60, 30])
    rather than "expiry_date within N days" continuously — the latter would
    re-notify every single day for the entire window, which is exactly the
    "sending everyone everything is how alerts stop getting read" failure
    docs/12-security-ops.md warns against. Three notifications per document
    (at 90, 60, 30 days out) is the intended escalation, not a daily nag.
    """
    thresholds = getattr(settings, "COMPLIANCE_NOTIFICATION_THRESHOLDS_DAYS", [90, 60, 30])
    today = timezone.localdate()
    for threshold in thresholds:
        target_date = today + timedelta(days=threshold)
        documents = ComplianceDocument.objects.filter(expiry_date=target_date).select_related(
            "farm", "farm_block__farm"
        )
        for document in documents:
            farm = document.farm or document.farm_block.farm
            for manager in _managers_for_farm(farm):
                notify(
                    manager,
                    NotificationCategory.COMPLIANCE_EXPIRY,
                    title=f"{document.get_document_type_display()} expires in {threshold} days",
                    message=(
                        f"{document.get_document_type_display()} for "
                        f"{document.farm_block or document.farm} expires on {document.expiry_date}."
                    ),
                    link=reverse("dashboard:compliance-documents"),
                )


@shared_task
def check_tree_health_anomalies():
    """docs/12-security-ops.md: 'a meaningful week-over-week increase in
    yellow/unhealthy classifications for a block.' Compares the trailing
    window (settings.TREE_HEALTH_ANOMALY_WINDOW_DAYS, default 7 days)
    against the equal-length window immediately before it, per block. Uses
    `effective_label` — a reviewed correction counts as the real answer,
    same reasoning as apps/reporting/services.py's tree-health report.
    """
    window_days = getattr(settings, "TREE_HEALTH_ANOMALY_WINDOW_DAYS", 7)
    min_increase = getattr(settings, "TREE_HEALTH_ANOMALY_MIN_INCREASE", 5)
    today = timezone.localdate()
    window_start = today - timedelta(days=window_days)
    prior_start = window_start - timedelta(days=window_days)

    # HEALTH_SMALL no longer exists (SMALL predictions now map to
    # HEALTH_UNKNOWN — see classification.py's own docstring): "unknown" is
    # an absence of a real signal, not itself an unhealthy finding, so only
    # yellow counts toward this anomaly trigger now.
    unhealthy_labels = [TreeHealthClassification.HEALTH_YELLOW]

    for block in FarmBlock.objects.filter(boundary__isnull=False).select_related("farm"):
        base_qs = TreeHealthClassification.objects.filter(
            tree_detection__processing_run__farm_block=block,
        )
        # effective_label isn't a DB field (it's corrected_label-or-health_label
        # in Python), so this counts both label fields' matches directly —
        # equivalent for classifications that have no correction, which is
        # the common case; a corrected classification is counted by its
        # corrected value, not double-counted.
        current_count = base_qs.filter(
            created_at__date__gte=window_start, created_at__date__lt=today
        ).filter(
            Q(corrected_label__in=unhealthy_labels)
            | (Q(corrected_label="") & Q(health_label__in=unhealthy_labels))
        ).count()
        prior_count = base_qs.filter(
            created_at__date__gte=prior_start, created_at__date__lt=window_start
        ).filter(
            Q(corrected_label__in=unhealthy_labels)
            | (Q(corrected_label="") & Q(health_label__in=unhealthy_labels))
        ).count()

        if current_count - prior_count >= min_increase:
            for manager in _managers_for_farm(block.farm):
                notify(
                    manager,
                    NotificationCategory.TREE_HEALTH_ANOMALY,
                    title=f"Tree health anomaly — {block.name}",
                    message=(
                        f"Unhealthy classifications for {block.name} rose from {prior_count} to "
                        f"{current_count} over the last {window_days} days."
                    ),
                    link=_block_link(block.id),
                )


@shared_task
def check_stalled_sync():
    """See apps/sync/models.py's SyncCheckIn docstring for why this signal
    exists at all — without the PWA's check-in call, this check would have
    nothing to query. Snoozed per-device (notify_with_snooze) rather than
    firing on every run this check finds a still-stalled device on — a
    phone that's been offline for two weeks doesn't need fourteen identical
    notifications, one per day, to make the point.
    """
    stalled_days = getattr(settings, "STALLED_SYNC_DAYS", 3)
    cutoff = timezone.now() - timedelta(days=stalled_days)
    stalled = SyncCheckIn.objects.filter(pending_count__gt=0, last_checkin_at__lt=cutoff).select_related("user")
    for checkin in stalled:
        notify_with_snooze(
            checkin.user,
            NotificationCategory.STALLED_SYNC,
            dedup_key=f"stalled-sync:{checkin.id}",
            title="Unsynced data on your device",
            message=(
                f"Your device last checked in on {checkin.last_checkin_at:%Y-%m-%d} with "
                f"{checkin.pending_count} item(s) still queued. Reconnect to sync."
            ),
        )


@shared_task
def check_review_backlog():
    """Unresolved detections (new_candidate/ambiguous, never confirmed,
    rejected, or linked — i.e. still sitting with no `tree` and an
    unresolved match_status) per block, above
    settings.REVIEW_BACKLOG_THRESHOLD (default 20). Snoozed per block
    (notify_with_snooze) — a still-open backlog doesn't need a fresh
    notification every single day this check happens to run.
    """
    threshold = getattr(settings, "REVIEW_BACKLOG_THRESHOLD", 20)
    unresolved = [TreeDetection.MATCH_NEW_CANDIDATE, TreeDetection.MATCH_AMBIGUOUS]

    backlog_by_block = (
        TreeDetection.objects.filter(tree__isnull=True, match_status__in=unresolved)
        .values("processing_run__farm_block")
        .annotate(count=Count("id"))
        .filter(count__gte=threshold)
    )
    for row in backlog_by_block:
        block = FarmBlock.objects.select_related("farm").get(id=row["processing_run__farm_block"])
        for manager in _managers_for_farm(block.farm):
            notify_with_snooze(
                manager,
                NotificationCategory.REVIEW_BACKLOG,
                dedup_key=f"review-backlog:{block.id}",
                title=f"Detection review backlog — {block.name}",
                message=f"{row['count']} detections awaiting review on {block.name}.",
                link=reverse("dashboard:imagery-block", kwargs={"block_id": block.id}),
            )


@shared_task
def check_approvals_pending():
    """Harvest/Fertilizer plans sitting in an awaiting-approval status
    (docs/03-data-model.md's PlanStatus machine) longer than
    settings.APPROVAL_PENDING_DAYS (default 3). Snoozed per plan
    (notify_with_snooze) — a still-pending approval doesn't need a fresh
    notification every single day this check happens to run.
    """
    pending_days = getattr(settings, "APPROVAL_PENDING_DAYS", 3)
    cutoff = timezone.now() - timedelta(days=pending_days)

    for model, label in [(Harvest, "Harvest"), (Fertilizer, "Fertilizer")]:
        pending = awaiting_approval(model.objects.filter(updated_at__lt=cutoff)).select_related("farm_block__farm")
        for plan in pending:
            for manager in _managers_for_farm(plan.farm_block.farm):
                notify_with_snooze(
                    manager,
                    NotificationCategory.APPROVAL_PENDING,
                    dedup_key=f"approval-pending:{label.lower()}:{plan.id}",
                    title=f"{label} approval pending — {plan.farm_block.name}",
                    message=f"{label} plan for {plan.farm_block.name} has been awaiting approval since {plan.updated_at:%Y-%m-%d}.",
                    link=_block_link(plan.farm_block_id, section=label.lower()),
                )
