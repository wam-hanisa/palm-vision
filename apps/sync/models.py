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


class SyncTombstone(models.Model):
    """Records a hard delete of an offline-synced resource, so a PWA client
    that already cached it can find out and remove its own copy.

    Before this model existed, SyncPullView's per-resource queries
    (`filter(updated_at__gt=since)`, etc.) had no way to represent "this
    row is gone" — a deleted Harvest/Fertilizer/etc. simply stopped
    appearing in future pulls, but nothing ever told a client that had
    already synced it to remove its own copy, so it lingered in that
    device's IndexedDB forever. Reported directly: "in pwa log harvest
    previous harvest plan are still showing."

    `resource` matches a key in apps/sync/views.py's _SYNC_RESOURCES (e.g.
    "harvests") so the client knows which IndexedDB store to remove
    `object_id` from — see pwa/js/sync.js's pullChanges(). `farm_id`/
    `pic_user_id` are a snapshot of the deleted row's own farm_block
    scoping (its block's farm and pic — see apps/common/scoping.py's
    scoped_to_user_blocks(), the same rule every live queryset already
    uses), captured at delete time by apps/sync/signals.py's pre_delete
    receivers: by the time a tombstone is being recorded the real row (and
    its live FK chain) may already be gone, so scoping has to be captured
    up front rather than computed on read like every other resource.

    No expiry/pruning yet — same "known limit, not hidden" tradeoff
    documented on apps/dashboard/views.py's _recent_yield_by_block(): a
    farm running for years would eventually want old tombstones pruned
    once every device has long since synced past them, but that's not
    needed to close the gap this model exists for.
    """

    resource = models.CharField(max_length=50, db_index=True)
    object_id = models.UUIDField()
    farm_id = models.UUIDField(null=True, blank=True, db_index=True)
    pic_user_id = models.BigIntegerField(
        null=True, blank=True, db_index=True,
        help_text="Snapshot of the deleted row's farm_block.pic_id — not a live FK (see class docstring).",
    )
    deleted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-deleted_at"]

    def __str__(self):
        return f"{self.resource}/{self.object_id} deleted {self.deleted_at}"
