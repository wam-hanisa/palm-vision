from django.contrib import admin

from .models import SyncCheckIn, SyncTombstone


@admin.register(SyncCheckIn)
class SyncCheckInAdmin(admin.ModelAdmin):
    list_display = ("user", "device_id", "pending_count", "last_checkin_at")
    list_filter = ("pending_count",)
    search_fields = ("device_id", "user__username")


@admin.register(SyncTombstone)
class SyncTombstoneAdmin(admin.ModelAdmin):
    """Read-only — a tombstone is a system-recorded fact about a delete
    that already happened (apps/sync/signals.py), not something anyone
    should hand-edit or hand-create here.
    """

    list_display = ("resource", "object_id", "farm_id", "pic_user_id", "deleted_at")
    list_filter = ("resource",)
    search_fields = ("object_id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
