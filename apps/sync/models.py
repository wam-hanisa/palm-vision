"""
apps/sync's first model. Added for Phase 7's "stalled sync" notification
check (docs/12-security-ops.md: "a device with unsynced queued items for
more than N days"). This is a real gap the offline-first design in
docs/05-offline-sync.md leaves open: the client's outbox lives in the
browser's IndexedDB, which the server has no visibility into by default —
there was no signal a Beat task could check at all. SyncCheckIn is a small,
deliberate addition to close that: the PWA's sync engine
(pwa/js/sync.js's runSync()) now reports its outbox size after every sync
attempt, giving the server a real (if slightly stale — only as fresh as the
last time the device was online long enough to check in) signal to alert
on. See apps/notifications/tasks.py's check_stalled_sync().
"""
import uuid

from django.conf import settings
from django.db import models


class SyncCheckIn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sync_checkins")
    device_id = models.CharField(max_length=64, help_text="Client-generated, stable per browser/device — see pwa/js/sync.js.")
    pending_count = models.PositiveIntegerField(default=0, help_text="Outbox size as of this check-in.")
    last_checkin_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "device_id")

    def __str__(self):
        return f"{self.user} / {self.device_id} — {self.pending_count} pending"
