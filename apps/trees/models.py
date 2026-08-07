import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from simple_history.models import HistoricalRecords

from apps.farms.models import FarmBlock


class Tree(gis_models.Model):
    """Individual palm tree. `location` is a real PostGIS point — see
    docs/03-data-model.md.

    `id` is a client-settable UUID (editable=True, unlike Farm/FarmBlock's
    server-only UUIDs) because Tree is offline-writable — field staff create
    trees on the PWA without connectivity and the server must accept the
    client-generated ID on sync rather than assigning its own. See
    docs/05-offline-sync.md and apps/common/mixins.py.
    """

    SOURCE_MANUAL = "manual"
    SOURCE_DRONE_DETECTED = "drone_detected"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_DRONE_DETECTED, "Drone detected"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_REMOVED = "removed"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_REMOVED, "Removed"),
    ]

    HEALTH_UNKNOWN = "unknown"
    HEALTH_HEALTHY = "healthy"
    HEALTH_SMALL = "small"
    HEALTH_YELLOW = "yellow"
    HEALTH_CHOICES = [
        (HEALTH_UNKNOWN, "Unknown"),
        (HEALTH_HEALTHY, "Healthy"),
        (HEALTH_SMALL, "Small"),
        (HEALTH_YELLOW, "Yellow"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="trees")
    name = models.CharField(max_length=255, blank=True, default="")
    location = gis_models.PointField(srid=4326)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    current_health = models.CharField(max_length=32, choices=HEALTH_CHOICES, default=HEALTH_UNKNOWN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="trees_created"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="trees_updated"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["farm_block", "name"]
        indexes = [models.Index(fields=["farm_block", "status"])]

    def __str__(self):
        return self.name or str(self.id)

    def is_location_orphaned(self):
        """True if this tree's location falls outside every block boundary
        on its farm — docs/12-security-ops.md § Geometry & Data Integrity
        Validation: 'flagged for review rather than silently accepted or
        hard-rejected... GPS error in the field is real and shouldn't block
        a field worker's data entry outright.' Deliberately not enforced in
        save() — surfaced as a non-blocking warning by the serializer,
        mirroring FarmBlock.overlapping_blocks().
        """
        # boundary__isnull=False: a block with no boundary yet (see
        # FarmBlock.boundary's nullability note) can't be checked for
        # containment at all — it's neither "contains this tree" nor
        # meaningfully "doesn't," so it's excluded rather than treated as
        # an automatic non-match.
        farm_blocks = FarmBlock.objects.filter(farm_id=self.farm_block.farm_id, boundary__isnull=False)
        return not any(block.boundary.contains(self.location) for block in farm_blocks)
