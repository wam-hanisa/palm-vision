from apps.notifications.models import Notification


def unread_notifications(request):
    """Unread count for the sidebar badge. One indexed COUNT, and only for
    signed-in users."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"nav_unread_count": Notification.objects.filter(user=user, read=False).count()}
