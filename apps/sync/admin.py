from django.contrib import admin

from .models import SyncCheckIn


@admin.register(SyncCheckIn)
class SyncCheckInAdmin(admin.ModelAdmin):
    list_display = ("user", "device_id", "pending_count", "last_checkin_at")
    list_filter = ("pending_count",)
    search_fields = ("device_id", "user__username")
