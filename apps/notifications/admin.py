from django.contrib import admin

from .models import Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "category", "title", "read", "created_at")
    list_filter = ("category", "read")
    search_fields = ("title", "user__username")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "category", "in_app_enabled", "email_enabled")
    list_filter = ("category",)
