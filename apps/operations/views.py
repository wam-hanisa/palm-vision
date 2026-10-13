from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import IdempotentUUIDCreateMixin
from apps.common.permissions import IsSuperAdminOrManager, ManagerOnlyChangesMixin
from apps.common.scoping import scoped_to_user_blocks as _scoped_to_user_blocks

from .models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree, PlanStatus
from .serializers import (
    ActualFertilizerSerializer,
    ActualHarvestSerializer,
    FertilizerSerializer,
    FertilizerTreeSerializer,
    HarvestSerializer,
    HarvestTreeSerializer,
)
from .services import advance_status, record_actual_progress

# _scoped_to_user_blocks is now apps/common/scoping.py's scoped_to_user_blocks
# (imported under its old local name to keep every call site below
# unchanged) — centralized so this app's read scoping can't drift from the
# same function apps/operations/serializers.py's FK write-scoping uses.


class _LockedOnceApprovedMixin:
    """Once a plan is approved to Done, nothing under it changes through the
    API: resubmits of existing records return them unchanged
    (IdempotentUUIDCreateMixin.is_resubmit_locked()), and brand-new actuals
    or per-tree rows are refused — otherwise a record added after approval
    would count toward yield without ever being approved (Manager
    corrections to existing records still go through, with history).
    `plan_path` is the dotted path from the record to its Harvest/Fertilizer
    plan ("" for the plan itself); its first segment is also the record's
    own FK field in the submitted data.
    """

    plan_path = ""

    def _resolve_plan(self, start, attrs):
        plan = start
        for attr in attrs:
            plan = getattr(plan, attr)
        return plan

    def is_resubmit_locked(self, instance):
        return self._resolve_plan(instance, filter(None, self.plan_path.split("."))).status == PlanStatus.DONE

    def validate_new_record(self, validated_data):
        if not self.plan_path:
            return  # a new plan can't already be approved — status is read-only
        first, *rest = self.plan_path.split(".")
        plan = self._resolve_plan(validated_data[first], rest)
        if plan.status == PlanStatus.DONE:
            raise DRFValidationError({
                first: "This plan has already been approved, so nothing more can be added to it. "
                "Log this against a new plan instead."
            })


class _AdvancesPlanOnCreateMixin:
    """A new actual moves its plan to done_wait — ready for a Manager to
    approve — through the same rule the dashboard's "Record actual" uses
    (services.record_actual_progress()). The PWA has no notion of partial
    progress, so a field log is always "finished": one Manager approval
    then moves it into yield, which is the flow asked for (PIC inputs,
    Manager approves, it counts).
    """

    def on_record_created(self, instance):
        record_actual_progress(self._resolve_plan(instance, self.plan_path.split(".")), finished=True)


class _CompletesTreeOnCreateMixin:
    """A per-tree row logged from the field ("Log harvest here") finishes
    that tree's stage-1 flag the same way "Mark harvested" does: last-done
    date/quantity set, "needs ..." cleared. Previously the tree stayed
    flagged as needing work it had just been logged against.
    """

    tree_stamp = None  # (date_field, quantity_field, marked_at_field, marked_by_field, value_field, actual_attr)

    def on_record_created(self, instance):
        date_f, qty_f, at_f, by_f, value_f, actual_attr = self.tree_stamp
        tree = instance.tree
        setattr(tree, date_f, getattr(instance, actual_attr).date)
        setattr(tree, qty_f, getattr(instance, value_f))
        setattr(tree, at_f, None)
        setattr(tree, by_f, None)
        tree.save(update_fields=[date_f, qty_f, at_f, by_f, "updated_at"])


class HarvestViewSet(ManagerOnlyChangesMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    """docs/04-api-spec.md. `approve` is a separate action, Manager/Super
    Admin only, enforced server-side — see apps/operations/services.py for
    why this matters (it's fixing a real bug from the original app).

    IdempotentUUIDCreateMixin: a PIC Block logging a harvest in the field
    with no existing plan for the block auto-creates one client-side (see
    the PWA's findOrCreatePlan(), pwa/js/app.js) with a client-generated
    UUID — same offline-safe-retry requirement every other record in this
    pipeline already has, so a plan-creation write that partially succeeds
    and gets replayed on reconnect updates in place instead of erroring.
    """

    serializer_class = HarvestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Harvest.objects.select_related("farm_block", "farm_block__farm")
        return _scoped_to_user_blocks(qs, self.request.user)

    def perform_create(self, serializer):
        serializer.save(status="created")

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsSuperAdminOrManager])
    def approve(self, request, pk=None):
        harvest = self.get_object()
        self.check_object_permissions(request, harvest)
        try:
            advance_status(harvest)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.messages if hasattr(exc, "messages") else str(exc))
        return Response(HarvestSerializer(harvest).data)


class ActualHarvestViewSet(
    ManagerOnlyChangesMixin, _AdvancesPlanOnCreateMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin,
    viewsets.ModelViewSet,
):
    serializer_class = ActualHarvestSerializer
    permission_classes = [IsAuthenticated]
    plan_path = "harvest"

    def get_queryset(self):
        qs = ActualHarvest.objects.select_related("harvest__farm_block__farm").prefetch_related("harvest_trees")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="harvest__farm_block")


class HarvestTreeViewSet(ManagerOnlyChangesMixin, _CompletesTreeOnCreateMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    """Per-tree yield entries — the offline-writable, GPS-tagged records
    described in docs/05-offline-sync.md § Yield can be logged at block
    level or tree level.
    """

    serializer_class = HarvestTreeSerializer
    permission_classes = [IsAuthenticated]
    plan_path = "actual_harvest.harvest"
    tree_stamp = ("last_harvested_at", "last_harvest_quantity", "harvest_marked_at", "harvest_marked_by", "weight_kg", "actual_harvest")

    def get_queryset(self):
        qs = HarvestTree.objects.select_related("actual_harvest__harvest__farm_block__farm", "tree")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="actual_harvest__harvest__farm_block")

    def get_idempotent_create_defaults(self, request):
        return {"recorded_by": request.user}

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)


class FertilizerViewSet(ManagerOnlyChangesMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    """See HarvestViewSet's own docstring for why this is idempotent-create
    too — the Fertilizer counterpart of the same PWA auto-create-a-plan
    behavior.
    """

    serializer_class = FertilizerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Fertilizer.objects.select_related("farm_block", "farm_block__farm")
        return _scoped_to_user_blocks(qs, self.request.user)

    def perform_create(self, serializer):
        serializer.save(status="created")

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsSuperAdminOrManager])
    def approve(self, request, pk=None):
        fertilizer = self.get_object()
        self.check_object_permissions(request, fertilizer)
        try:
            advance_status(fertilizer)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.messages if hasattr(exc, "messages") else str(exc))
        return Response(FertilizerSerializer(fertilizer).data)


class ActualFertilizerViewSet(
    ManagerOnlyChangesMixin, _AdvancesPlanOnCreateMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin,
    viewsets.ModelViewSet,
):
    serializer_class = ActualFertilizerSerializer
    permission_classes = [IsAuthenticated]
    plan_path = "fertilizer"

    def get_queryset(self):
        qs = ActualFertilizer.objects.select_related("fertilizer__farm_block__farm").prefetch_related("fertilizer_trees")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="fertilizer__farm_block")


class FertilizerTreeViewSet(ManagerOnlyChangesMixin, _CompletesTreeOnCreateMixin, _LockedOnceApprovedMixin, IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    serializer_class = FertilizerTreeSerializer
    permission_classes = [IsAuthenticated]
    plan_path = "actual_fertilizer.fertilizer"
    tree_stamp = ("last_fertilized_at", "last_fertilizer_quantity", "fertilizer_marked_at", "fertilizer_marked_by", "quantity", "actual_fertilizer")

    def get_queryset(self):
        qs = FertilizerTree.objects.select_related("actual_fertilizer__fertilizer__farm_block__farm", "tree")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="actual_fertilizer__fertilizer__farm_block")

    def get_idempotent_create_defaults(self, request):
        return {"recorded_by": request.user}

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)
