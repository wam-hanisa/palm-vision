import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils import timezone
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
    HEALTH_YELLOW = "yellow"
    HEALTH_CHOICES = [
        (HEALTH_UNKNOWN, "Unknown"),
        (HEALTH_HEALTHY, "Healthy"),
        (HEALTH_YELLOW, "Yellow"),
    ]

    # Interval-status labels returned by harvest_status()/fertilizer_status()
    # below — "never" (no date recorded yet), "ok" (within the interval),
    # "due_soon" (past 80% of the interval), "overdue" (past the interval).
    INTERVAL_NEVER = "never"
    INTERVAL_OK = "ok"
    INTERVAL_DUE_SOON = "due_soon"
    INTERVAL_OVERDUE = "overdue"

    # Fifth stage label, returned by harvest_stage()/fertilizer_stage() below
    # in place of any INTERVAL_* value when a Manager has flagged this tree
    # as needing the work — see harvest_marked_at's help_text for the full
    # two-stage workflow this supports.
    STAGE_MARKED = "marked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="trees")
    name = models.CharField(max_length=255, blank=True, default="")
    location = gis_models.PointField(srid=4326)
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    current_health = models.CharField(max_length=32, choices=HEALTH_CHOICES, default=HEALTH_UNKNOWN)
    last_harvested_at = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Last date this specific tree was marked harvested — set via the Farm Map's "
            "'Mark trees harvested' action (select trees by click or lasso, then confirm), not by the "
            "block-level Harvest/ActualHarvest/HarvestTree yield-recording pipeline in "
            "apps/operations/models.py. That pipeline records *how much* was harvested against an "
            "approved plan, for compliance/yield reporting; this field is a much lighter, standalone "
            "'when was this tree last serviced' marker, whose only job is driving harvest_status() below."
        ),
    )
    last_fertilized_at = models.DateField(
        null=True,
        blank=True,
        help_text="Fertilizer counterpart to last_harvested_at above — see that field's help_text.",
    )
    harvest_marked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when a Manager/Super Admin flags this tree as needing harvest — the Farm Map's "
            "'Mark trees for harvest' action (apps/dashboard/views.py MarkTreesForHarvestView). This is "
            "stage 1 of a two-stage, two-actor workflow: a Manager assigns the work here from the office, "
            "then the PIC Block user assigned to this tree's block sees it as a to-do in the PWA field app "
            "and, once actually done, confirms completion (apps.trees.views.TreeViewSet's mark_harvested "
            "action) — which sets last_harvested_at above and clears this field back to null. A tree with "
            "this field set is 'marked, pending PIC action' regardless of what harvest_status() would "
            "otherwise say from last_harvested_at/the interval — see harvest_stage() below, which is what "
            "the map's indicator actually reads."
        ),
    )
    harvest_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="trees_marked_for_harvest",
        help_text="Who flagged this tree for harvest — see harvest_marked_at's help_text.",
    )
    fertilizer_marked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Fertilizer counterpart to harvest_marked_at above — see that field's help_text.",
    )
    fertilizer_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="trees_marked_for_fertilizer",
        help_text="Fertilizer counterpart to harvest_marked_by above — see harvest_marked_at's help_text.",
    )
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

    def _interval_status(self, last_at, interval_days):
        """Shared logic behind harvest_status()/fertilizer_status() below.
        "due_soon" starts at 80% of the interval elapsed — a fixed,
        unconfigurable fraction chosen as a reasonable early-warning
        margin, not a value derived from any real operational data; a
        future round could make this configurable too if it turns out to
        matter in practice.
        """
        if last_at is None:
            return self.INTERVAL_NEVER
        days_since = (timezone.localdate() - last_at).days
        if days_since >= interval_days:
            return self.INTERVAL_OVERDUE
        if days_since >= interval_days * 0.8:
            return self.INTERVAL_DUE_SOON
        return self.INTERVAL_OK

    def harvest_status(self):
        """One of INTERVAL_NEVER/OK/DUE_SOON/OVERDUE, computed against this
        tree's own farm_block.effective_harvest_interval_days() — the Farm
        Map uses this to draw the per-tree overdue indicator ring. See
        last_harvested_at's help_text for why this is separate from the
        Harvest/ActualHarvest/HarvestTree yield pipeline.
        """
        return self._interval_status(self.last_harvested_at, self.farm_block.effective_harvest_interval_days())

    def fertilizer_status(self):
        """Fertilizer counterpart to harvest_status() above."""
        return self._interval_status(self.last_fertilized_at, self.farm_block.effective_fertilizer_interval_days())

    def harvest_stage(self):
        """The 5-state value the Farm Map's marker ring and tree detail
        panel actually read — STAGE_MARKED (pending PIC action, regardless
        of what the interval says) if harvest_marked_at is set, otherwise
        whatever harvest_status() returns. "Marked" deliberately overrides
        interval status rather than combining with it: once a Manager has
        explicitly assigned the work, what matters to whoever's looking at
        the map is "this needs doing," not whether the tree also happens to
        be technically overdue by date math.
        """
        if self.harvest_marked_at:
            return self.STAGE_MARKED
        return self.harvest_status()

    def fertilizer_stage(self):
        """Fertilizer counterpart to harvest_stage() above."""
        if self.fertilizer_marked_at:
            return self.STAGE_MARKED
        return self.fertilizer_status()
