import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from simple_history.models import HistoricalRecords

from apps.farms.models import FarmBlock
from apps.trees.models import Tree


class PlanStatus(models.TextChoices):
    """Shared status machine for Harvest and Fertilizer plans — mirrors the
    original app's HarvestStatus/FertilizerStatus enums (Palm_Vision_PDR.md
    § 4.2), collapsed into one shared enum since the transitions are
    structurally identical. See PlanApprovalService below for the actual
    transition logic — the original app's biggest flaw was approving with
    no enforced permission check and a `dd()` debug crash on an unexpected
    status; this rebuild fixes both (CLAUDE.md's non-negotiable rule).
    """

    CREATED = "created", "Created"
    CREATED_CONFIRMED = "created_confirmed", "Created (confirmed)"
    ON_PROGRESS_WAIT = "on_progress_wait", "On progress (awaiting approval)"
    ON_PROGRESS = "on_progress", "On progress"
    DONE_WAIT = "done_wait", "Done (awaiting approval)"
    DONE = "done", "Done"


class Harvest(models.Model):
    """A planned/assessed harvest for a block. See docs/03-data-model.md."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="harvests")
    assessment_date = models.DateField()
    assessment_quantity = models.FloatField()
    status = models.CharField(max_length=32, choices=PlanStatus.choices, default=PlanStatus.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-assessment_date"]

    def __str__(self):
        return f"Harvest — {self.farm_block} — {self.assessment_date}"


class ActualHarvest(models.Model):
    """One field visit's worth of actual harvest execution against a
    Harvest plan. `quantity` is the manually-entered block-level total,
    used only when no tree-level HarvestTree rows exist for this event —
    see get_effective_quantity() and docs/05-offline-sync.md § Yield can be
    logged at block level or tree level.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    harvest = models.ForeignKey(Harvest, on_delete=models.CASCADE, related_name="actual_harvests")
    date = models.DateField()
    quantity = models.FloatField(
        null=True, blank=True, help_text="Manual block-level total. Ignored if tree-level HarvestTree rows exist."
    )
    batch = models.ForeignKey(
        "compliance.HarvestBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="actual_harvests",
        help_text=(
            "Optional link into a compliance HarvestBatch for due-diligence "
            "export aggregation — docs/07-compliance.md 'HarvestBatch aggregates "
            "ActualHarvest'. Null until an office user assigns this record to a batch."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Actual harvest — {self.harvest} — {self.date}"

    def get_effective_quantity(self):
        """Derived (summed) quantity when tree-level entries exist,
        otherwise the manually-entered total. Never let both disagree —
        this is the single source of truth for "what was actually
        harvested" on this record. See docs/03-data-model.md.
        """
        tree_total = self.harvest_trees.aggregate(total=models.Sum("quantity"))["total"]
        if tree_total is not None:
            return tree_total
        return self.quantity


class HarvestTree(gis_models.Model):
    """Per-tree yield record — docs/05-offline-sync.md § Yield can be logged
    at block level or tree level. Offline-writable: client-generated UUID,
    like Tree.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    actual_harvest = models.ForeignKey(ActualHarvest, on_delete=models.CASCADE, related_name="harvest_trees")
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name="harvest_records")
    quantity = models.FloatField(help_text="e.g. fruit bunch count")
    weight_kg = models.FloatField(null=True, blank=True)
    recorded_location = gis_models.PointField(srid=4326, null=True, blank=True, help_text="GPS at capture time")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tree} — {self.quantity}"


class Fertilizer(models.Model):
    """A planned/assessed fertilizer application for a block."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_block = models.ForeignKey(FarmBlock, on_delete=models.CASCADE, related_name="fertilizers")
    assessment_date = models.DateField()
    status = models.CharField(max_length=32, choices=PlanStatus.choices, default=PlanStatus.CREATED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-assessment_date"]

    def __str__(self):
        return f"Fertilizer — {self.farm_block} — {self.assessment_date}"


class ActualFertilizer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    fertilizer = models.ForeignKey(Fertilizer, on_delete=models.CASCADE, related_name="actual_fertilizers")
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Actual fertilizer — {self.fertilizer} — {self.date}"


class FertilizerTree(models.Model):
    """Per-tree fertilizer application record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    actual_fertilizer = models.ForeignKey(ActualFertilizer, on_delete=models.CASCADE, related_name="fertilizer_trees")
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name="fertilizer_records")
    quantity = models.FloatField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tree} — {self.quantity}"
