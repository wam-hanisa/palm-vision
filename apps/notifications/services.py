"""The single entry point every Beat check (apps/notifications/tasks.py)
and every event-driven trigger (e.g. apps/imagery's processing-failure
path) should call — never write to Notification or send mail directly, so
channel preference is always honored in exactly one place.
"""
from django.core.mail import send_mail

from .models import Notification, NotificationPreference


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
