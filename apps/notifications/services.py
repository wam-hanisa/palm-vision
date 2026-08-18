"""The single entry point every Beat check (apps/notifications/tasks.py)
and every event-driven trigger (e.g. apps/imagery's processing-failure
path) should call — never write to Notification or send mail directly, so
channel preference is always honored in exactly one place.
"""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import Notification, NotificationDedupState, NotificationPreference


def notify(user, category, title, message, link=""):
    """Creates an in-app Notification (if the user's preference allows it,
    or if they have no preference row at all — see NotificationPreference's
    docstring for why "no row" defaults to enabled) and/or sends an email.
    Returns the created Notification, or None if in-app delivery was
    disabled for this user/category (email may still have gone out).
    """
    try:
        preference = NotificationPreference.objects.get(user=user, category=category)
        in_app_enabled = preference.in_app_enabled
        email_enabled = preference.email_enabled
    except NotificationPreference.DoesNotExist:
        in_app_enabled = True
        email_enabled = True

    notification = None
    if in_app_enabled:
        notification = Notification.objects.create(user=user, category=category, title=title, message=message, link=link)

    if email_enabled and user.email:
        send_mail(
            subject=title,
            message=message,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            fail_silently=True,
        )

    return notification


def notify_with_snooze(user, category, dedup_key, title, message, link="", snooze_days=None):
    """Same as notify(), except it's skipped entirely (no in-app row, no
    email) if the same (user, category, dedup_key) alert already fired
    within the snooze window — settings.NOTIFICATION_SNOOZE_DAYS, default
    3. For a condition that's still true tomorrow (a still-stalled device,
    a still-open review backlog, a still-pending approval), this is the
    difference between one reminder every few days and an identical alert
    every single day the underlying Beat check happens to run — the latter
    is precisely what docs/12-security-ops.md warns against.

    Not used by every check: check_compliance_expiry already has its own,
    more precise escalation shape (exact-day thresholds — see that
    function's docstring), and check_tree_health_anomalies compares
    rolling windows, so a persistently *worsening* trend naturally keeps
    triggering while a plateaued one naturally stops — reusing this
    fixed-day snooze there would blunt a signal that's supposed to be
    urgent. Reserved for the three checks docs/12-security-ops.md itself
    doesn't describe as having any escalation shape at all.
    """
    snooze_days = snooze_days if snooze_days is not None else getattr(settings, "NOTIFICATION_SNOOZE_DAYS", 3)
    cutoff = timezone.now() - timedelta(days=snooze_days)

    state, created = NotificationDedupState.objects.get_or_create(
        user=user, category=category, dedup_key=dedup_key,
    )
    if not created and state.last_fired_at >= cutoff:
        return None  # still snoozed — skip both channels entirely

    result = notify(user, category, title=title, message=message, link=link)
    state.save(update_fields=["last_fired_at"])  # auto_now bumps last_fired_at on every save, including get_or_create's
    return result
