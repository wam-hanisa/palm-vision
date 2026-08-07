"""docs/04-api-spec.md:
  GET   /api/notifications/               — list current user's notifications
  PATCH /api/notification-preferences/    — update which categories/channels a user receives

Both endpoints are inherently per-request-user, not farm/block-scoped —
"read whichever category/channel *I* want" isn't a Manager/PIC Block
distinction, it's just account settings.
"""
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification, NotificationCategory, NotificationPreference
from .serializers import NotificationPreferenceSerializer, NotificationSerializer


class NotificationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """List/retrieve/mark-read only — a Notification is never created or
    deleted through the API, only by apps/notifications/services.py's
    notify(). `read` is the only field PATCH can actually change (see
    NotificationSerializer's read_only_fields).
    """

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)


class NotificationPreferenceView(APIView):
    """GET returns the effective preference for every category (a category
    with no stored row shows the true default — both channels enabled, per
    NotificationPreference's docstring — rather than omitting it, so a
    client can render a complete settings screen without knowing that
    default itself). PATCH accepts a list of partial updates and upserts
    each by (user, category); unmentioned categories are left untouched.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        existing = {
            pref.category: pref
            for pref in NotificationPreference.objects.filter(user=request.user)
        }
        results = []
        for category, label in NotificationCategory.choices:
            pref = existing.get(category)
            results.append(
                {
                    "category": category,
                    "category_display": label,
                    "in_app_enabled": pref.in_app_enabled if pref else True,
                    "email_enabled": pref.email_enabled if pref else True,
                }
            )
        return Response(results)

    def patch(self, request, *args, **kwargs):
        updates = request.data if isinstance(request.data, list) else [request.data]
        valid_categories = dict(NotificationCategory.choices)
        results = []
        for update in updates:
            category = update.get("category")
            if category not in valid_categories:
                return Response({"category": f"'{category}' is not a valid category."}, status=400)
            defaults = {}
            if "in_app_enabled" in update:
                defaults["in_app_enabled"] = bool(update["in_app_enabled"])
            if "email_enabled" in update:
                defaults["email_enabled"] = bool(update["email_enabled"])
            preference, _ = NotificationPreference.objects.update_or_create(
                user=request.user, category=category, defaults=defaults
            )
            results.append(NotificationPreferenceSerializer(preference).data)
        return Response(results)
