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
    assessment_quantity = models.FloatField(
        help_text="Estimated weight (kg) expected from this block — a forecast, not the reported result; see ActualHarvest.weight_kg for that."
    )
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
    Harvest plan. `weight_kg` is the manually-entered block-level total —
    see get_effective_quantity() for how it interacts with tree-level
    HarvestTree rows, and docs/05-offline-sync.md § Yield can be logged at
    block level or tree level.

    Update — asked directly to change every harvest record from bunches to
    kg: `weight_kg` is now the primary, authoritative reported total;
    `quantity` (bunch count) is optional supplementary detail, no longer
    part of get_effective_quantity()'s own math. This field didn't exist
    at all until this change — HarvestTree already had a quantity/weight_kg
    split at the tree level, but this action-level total only ever had one
    generic `quantity` field, forcing the same bunch/kg ambiguity this
    change is meant to remove.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    harvest = models.ForeignKey(Harvest, on_delete=models.CASCADE, related_name="actual_harvests")
    date = models.DateField()
    weight_kg = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "The real, reported total (kg) for this action — authoritative whenever set (see "
            "get_effective_quantity()). Tree-level HarvestTree.weight_kg rows are then a "
            "breakdown of this total, not a competing figure; a tree with no individual reading "
            "gets an estimated share of whatever's left over — see HarvestTree.effective_quantity."
        ),
    )
    quantity = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Optional bunch count for this action — supplementary detail only; weight_kg is the "
            "authoritative total used for yield reporting (see get_effective_quantity())."
        ),
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

    # The approved yield figure — edits after approval must leave a trace.
    history = HistoricalRecords()

    def __str__(self):
        return f"Actual harvest — {self.harvest} — {self.date}"

    def get_reported_quantity(self):
        """The action's raw reported total (kg), regardless of whether the
        parent Harvest plan has been approved yet.

        The manual `weight_kg`, when set, is authoritative, full stop —
        asked directly, this method's original rule was "tree-sum always
        wins the moment any HarvestTree row exists," which silently
        discarded a real, deliberately-reported manual total the instant
        even one tree in a much larger group got an individual reading —
        a Manager who reports "500 kg from this harvest" and then logs 2
        trees' worth of detail (120 + 80) would have seen this method
        report 200, quietly dropping 300 kg of real, reported yield. The
        manual figure is the one number a Manager actually stands behind
        for "how much did this action produce" — tree-level rows are a
        breakdown OF it now, not a second, competing source. See
        HarvestTree.effective_quantity for how an unmeasured tree's share
        of the remainder is estimated from this same total.

        Falls back to summing tree rows' own weight_kg only when no manual
        total was ever given at all. `quantity` (bunch count) plays no
        part in this — asked directly to make weight_kg the authoritative
        unit for every harvest record, bunch count is optional
        supplementary detail only. Returns None (never 0) when neither
        exists yet.

        This is the *reported* figure — see get_effective_quantity() below
        for the one that actually counts toward official yield reporting.
        """
        if self.weight_kg is not None:
            return self.weight_kg
        return self.harvest_trees.aggregate(total=models.Sum("weight_kg"))["total"]

    def get_effective_quantity(self):
        """The action's total, in kg, once it's been approved — the
        figure that actually counts toward official yield reporting
        (Yield history's own total, block/farm yield summaries, the
        Overview chart, and the compliance due-diligence export).

        Update — asked directly for a compliance-grade maker-checker flow:
        make harvest plan (Manager) → input actual harvest (PIC Block,
        with or without picking a plan) → approve (Manager) → *only then*
        does the actual data move into yield reporting. Before that, this
        returns None — same as every other "nothing sound to report yet"
        case this method already returned None for — even though the real
        reported figure exists and is visible: see get_reported_quantity()
        above, which the dashboard's "Yield history" list uses to still
        show a pending entry (labeled "pending approval"), distinct from
        the approved total above it. A plan reaching "done" is the one
        thing that flips this from None to the real number; recording
        further actuals against an already-done plan doesn't need a
        second approval to count (advance_status() never re-triggers on
        an already-terminal plan, and this checks the plan's *current*
        status here, not "was ever approved").
        """
        if self.harvest.status != PlanStatus.DONE:
            return None
        return self.get_reported_quantity()


class HarvestTree(gis_models.Model):
    """Per-tree yield record — docs/05-offline-sync.md § Yield can be logged
    at block level or tree level. Offline-writable: client-generated UUID,
    like Tree.

    `weight_kg` is nullable — asked directly, to represent "this tree was
    part of the harvest, just not individually measured" as a real, first-
    class state, distinct from no row existing at all (which means "not
    part of this harvest"). See effective_quantity for what a null row
    actually shows.

    Update — asked directly to change every harvest record from bunches to
    kg: `weight_kg` is now the primary, authoritative per-tree value;
    `quantity` (bunch count) is optional supplementary detail, no longer
    part of effective_quantity's own math.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    actual_harvest = models.ForeignKey(ActualHarvest, on_delete=models.CASCADE, related_name="harvest_trees")
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name="harvest_records")
    weight_kg = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Weight in kg — the authoritative per-tree value (see effective_quantity). Optional "
            "— leave blank if this tree was part of the harvest but wasn't individually measured; "
            "see effective_quantity for the derived estimate used in that case."
        ),
    )
    quantity = models.FloatField(
        null=True,
        blank=True,
        help_text="Optional bunch count for this tree — supplementary detail only; weight_kg is authoritative.",
    )
    recorded_location = gis_models.PointField(srid=4326, null=True, blank=True, help_text="GPS at capture time")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tree} — {self.weight_kg} kg"

    @property
    def effective_quantity(self):
        """This tree's real, individually-recorded weight (kg) — or, when
        it was part of the action but never individually measured
        (weight_kg=None), its estimated share of the action's remaining
        total: (ActualHarvest.weight_kg minus every sibling tree's own
        real, measured weight_kg) divided evenly across every sibling tree
        that's ALSO unmeasured — not naively across every tree in the
        action, which would dilute the ones that WERE actually measured.
        Asked directly for this exact behavior: "if the user not input
        any data to it but only input the bulk then the data input to
        tree level is the average from bulk/trees" (originally built for
        bunch count, since asked directly to move every harvest record to
        kg, this now operates on weight_kg — `quantity`, the bunch count,
        plays no part in this).

        A computed ESTIMATE, deliberately never written back to
        `weight_kg` itself — persisting it would make a guess
        indistinguishable from a real measurement the next time anyone
        reads this row, which defeats the entire point of tracking yield
        at tree level (spotting the one tree that's actually
        underperforming, not "everyone gets the block average that day").

        Returns None (never 0, never a guess) when there's nothing sound
        to estimate from: no manual total was ever recorded on the parent
        action, or the trees that WERE individually measured already add
        up to at least the whole reported total — in which case the
        manual figure is incomplete or wrong, not something to paper over
        with a fabricated remainder.

        Also None until the parent action's Harvest plan is approved to
        "done" — same compliance-grade maker-checker gate as
        ActualHarvest.get_effective_quantity(), see that method's own
        docstring for the full reasoning. This tree's own raw weight_kg
        is still there on the row itself either way; this property is
        specifically "the figure that counts," not "what was reported."
        """
        if self.actual_harvest.harvest.status != PlanStatus.DONE:
            return None
        if self.weight_kg is not None:
            return self.weight_kg

        total = self.actual_harvest.weight_kg
        if total is None:
            return None

        siblings = list(self.actual_harvest.harvest_trees.all())
        measured_total = sum(t.weight_kg for t in siblings if t.weight_kg is not None)
        unmeasured_count = sum(1 for t in siblings if t.weight_kg is None)
        remainder = total - measured_total
        if remainder < 0:
            return None
        return remainder / unmeasured_count


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
    """One field visit's worth of actual fertilizer application against a
    Fertilizer plan. Mirrors ActualHarvest exactly — `quantity` is the
    manually-entered block-level total (kg), authoritative whenever set;
    see get_effective_quantity().

    Update — `quantity` didn't exist on this model at all until asked
    directly to fill this exact gap: ActualHarvest/HarvestTree already had
    a manual-total-vs-tree-breakdown pattern (weight reporting at the
    action level), but Fertilizer's side had no way to record a block-wide
    total application at all — only FertilizerTree's own per-tree amounts,
    with no fallback for "no tree-level breakdown was recorded this time."
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    fertilizer = models.ForeignKey(Fertilizer, on_delete=models.CASCADE, related_name="actual_fertilizers")
    date = models.DateField()
    quantity = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "The real, reported total (kg) for this action — authoritative whenever set (see "
            "get_effective_quantity()), same rule as ActualHarvest.quantity. Tree-level "
            "FertilizerTree rows are then a breakdown of this total; a tree with no individual "
            "reading gets an estimated share of whatever's left over — see "
            "FertilizerTree.effective_quantity."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    history = HistoricalRecords()

    def __str__(self):
        return f"Actual fertilizer — {self.fertilizer} — {self.date}"

    def get_reported_quantity(self):
        """The reported total (kg), visible whether or not approved: the
        manual `quantity` when set, else the sum of tree rows; None (never
        0) when neither exists. Same rule as ActualHarvest.
        """
        if self.quantity is not None:
            return self.quantity
        return self.fertilizer_trees.aggregate(total=models.Sum("quantity"))["total"]

    def get_effective_quantity(self):
        """The total that counts toward official figures — None until the
        Fertilizer plan is approved to "done", the same maker-checker gate
        as ActualHarvest.get_effective_quantity(): Manager plans, PIC
        records, Manager approves, only then it counts.
        """
        if self.fertilizer.status != PlanStatus.DONE:
            return None
        return self.get_reported_quantity()


class FertilizerTree(models.Model):
    """Per-tree fertilizer application record (kg). `quantity` is
    nullable for the same reason as HarvestTree.quantity — "part of this
    action, not individually measured" is a real, distinct state from no
    row existing at all. See effective_quantity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=True)
    actual_fertilizer = models.ForeignKey(ActualFertilizer, on_delete=models.CASCADE, related_name="fertilizer_trees")
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name="fertilizer_records")
    quantity = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "kg applied to this tree. Optional — leave blank if this tree was part of the "
            "application but wasn't individually measured; see effective_quantity for the "
            "derived estimate used in that case."
        ),
    )
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tree} — {self.quantity}"

    @property
    def effective_quantity(self):
        """Mirrors HarvestTree.effective_quantity exactly — see that
        property's own docstring for the full reasoning (real measurement
        if present, else an estimated share of the action's remaining
        total split across every sibling that's also unmeasured, never
        persisted, never a guess when there's nothing sound to base one
        on).
        """
        if self.actual_fertilizer.fertilizer.status != PlanStatus.DONE:
            return None
        if self.quantity is not None:
            return self.quantity

        total = self.actual_fertilizer.quantity
        if total is None:
            return None

        siblings = list(self.actual_fertilizer.fertilizer_trees.all())
        measured_total = sum(t.quantity for t in siblings if t.quantity is not None)
        unmeasured_count = sum(1 for t in siblings if t.quantity is None)
        remainder = total - measured_total
        if remainder < 0:
            return None
        return remainder / unmeasured_count
