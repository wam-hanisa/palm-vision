"""
docs/12-security-ops.md § Alerting & notifications: "Everything else in this
system is pull-based by default ... A Notification model with pluggable
delivery channels (in-app, email; SMS optional and not committed to) closes
this." `Notification` is the in-app record; email is a side effect of
apps/notifications/services.py's notify(), not a separate stored model —
there's nothing useful to keep querying after an email is sent, unlike an
in-app notification a user marks read later.

`NotificationPreference` — "Each user chooses which categories/channels
they receive." One row per (user, category); a category with no row for a
user defaults to both channels enabled (see notify()'s docstring) — opt-out,
not opt-in, so nobody misses a real problem just because they never visited
a settings screen that doesn't exist yet (Phase 7 is API-only, same as
Phases 5/6).
"""
import uuid

from django.conf import settings
from django.db import models


class NotificationCategory(models.TextChoices):
    """Matches docs/12-security-ops.md's Alerting & notifications trigger
    list exactly — one enum shared by Notification.category and
    NotificationPreference.category so the two can never drift apart.
    """

    COMPLIANCE_EXPIRY = "compliance_expiry", "Compliance document/certification expiring"
    TREE_HEALTH_ANOMALY = "tree_health_anomaly", "Tree health anomaly"
    STALLED_SYNC = "stalled_sync", "Stalled offline sync"
    REVIEW_BACKLOG = "review_backlog", "Detection review queue backlog"
    PROCESSING_FAILURE = "processing_failure", "Imagery processing run failure"
    APPROVAL_PENDING = "approval_pending", "Approval pending too long"


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    category = models.CharField(max_length=32, choices=NotificationCategory.choices)
    title = models.CharField(max_length=255)
    message = models.TextField()
    link = models.CharField(
        max_length=500,
        blank=True,
        help_text="Relative API path to the record this notification is about, e.g. /api/compliance-documents/<id>/.",
    )
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read"])]

    def __str__(self):
        return f"{self.get_category_display()} — {self.user}"


class NotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    category = models.CharField(max_length=32, choices=NotificationCategory.choices)
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ("user", "category")

    def __str__(self):
        return f"{self.user} — {self.get_category_display()}"


class NotificationDedupState(models.Model):
    """Tracks the last time a given (user, category, dedup_key) alert
    actually fired — used by apps/notifications/services.py's
    notify_with_snooze() to stop the non-compliance Beat checks
    (check_stalled_sync, check_review_backlog, check_approvals_pending)
    from re-notifying identically every single day a condition stays true,
    which docs/12-security-ops.md explicitly warns against ("sending
    everyone everything is how alerts stop getting read").

    Deliberately a separate model from `Notification`, not "the most recent
    Notification row with this category/link" — a user can have in-app
    delivery disabled for a category (NotificationPreference), which means
    no Notification row is ever created for them at all; deriving the
    snooze window from Notification's own rows would silently defeat the
    snooze for exactly those users (email would still be sent, so it would
    re-fire every day regardless of how recently it last fired). This model
    records "did this alert fire" independent of which channels were
    actually enabled for it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    category = models.CharField(max_length=32, choices=NotificationCategory.choices)
    dedup_key = models.CharField(
        max_length=255,
        help_text="Identifies the specific condition being alerted on, e.g. 'review-backlog:<block-id>' — "
        "not necessarily the same as the Notification's own `link`.",
    )
    last_fired_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "category", "dedup_key")

    def __str__(self):
        return f"{self.user} — {self.category} — {self.dedup_key}"
