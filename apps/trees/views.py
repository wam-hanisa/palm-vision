import uuid

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import IdempotentUUIDCreateMixin
from apps.common.permissions import IsAssignedPicBlockOrManagerOrSuperAdmin
from apps.common.scoping import scoped_to_user_blocks
from apps.operations.services import record_field_completion

from .models import Tree, TreeCareRecord
from .serializers import TreeSerializer


class TreeViewSet(IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    """Field-staff tree CRUD — docs/04-api-spec.md, docs/05-offline-sync.md
    § Field Staff Tree CRUD. Scoped so a PIC Block user only sees/edits
    trees in blocks they're assigned to (FarmBlock.pic); Manager/Super Admin
    see everything in their scope.

    Soft delete only — `destroy()` is overridden to set status=removed
    rather than actually deleting the row, since detection matching and
    history both depend on removed trees still existing (docs/03-data-model.md).

    `mark_harvested`/`mark_fertilized` below are stage 2 of the two-stage
    harvest/fertilizer workflow (see Tree.harvest_marked_at's help_text) —
    the PIC Block field app calls these once the assigned work is actually
    done. Stage 1 (a Manager flagging which trees need it) is dashboard-only
    (apps/dashboard/views.py's MarkTreesNeedHarvestView/
    MarkTreesNeedFertilizeView) — there's no API-side "mark needed" action,
    since only the office side of this workflow is asked to do that step.

    `bulk_mark_harvested`/`bulk_mark_fertilized` are the same stage-2
    completion, for several trees in one request — added after direct
    feedback that the PWA had no equivalent to the office dashboard's
    bulk "Mark trees harvested" action (apps/dashboard/views.py's
    MarkTreesHarvestedView), forcing a field worker to tap through a
    whole block's trees one at a time. Deliberately still stage 2 only —
    stage-1 assignment stays dashboard-only, same reasoning as above; a
    PIC Block user confirming a batch of work as done doesn't change who
    is allowed to assign it in the first place.
    """

    serializer_class = TreeSerializer
    permission_classes = [IsAuthenticated]
    queryset = Tree.objects.select_related("farm_block", "farm_block__farm")

    def get_queryset(self):
        qs = super().get_queryset()
        return scoped_to_user_blocks(qs, self.request.user, block_field="farm_block")

    def get_idempotent_create_defaults(self, request):
        # See apps/common/mixins.py — the idempotent (client-UUID) create
        # path doesn't go through perform_create(), so request-derived
        # defaults are supplied here instead.
        return {"created_by": request.user, "updated_by": request.user}

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        instance.status = Tree.STATUS_REMOVED
        instance.updated_by = self.request.user
        instance.save(update_fields=["status", "updated_by", "updated_at"])

    def _mark_done(self, request, date_field, marked_at_field, marked_by_field, quantity_field, kind):
        # Shared body for mark_harvested/mark_fertilized below — get_object()
        # already runs get_queryset()'s scoping plus
        # IsAssignedPicBlockOrManagerOrSuperAdmin.has_object_permission()
        # (DRF calls it internally); the explicit check_object_permissions()
        # call mirrors apps/operations/views.py's HarvestViewSet.approve()
        # for consistency, even though it's redundant here.
        tree = self.get_object()
        self.check_object_permissions(request, tree)

        date_str = request.data.get("date")
        marked_date = parse_date(date_str) if date_str else None
        if marked_date is None:
            marked_date = timezone.localdate()

        # Optional — this completion action has never required a quantity
        # (see Tree.last_harvest_quantity's help_text), so a blank/missing
        # value is a normal input, not an error. Anything that doesn't
        # parse as a number is treated the same as "not provided" rather
        # than 400ing the whole request over what's an optional field.
        quantity_raw = request.data.get("quantity")
        quantity = None
        if quantity_raw not in (None, ""):
            try:
                quantity = float(quantity_raw)
            except (TypeError, ValueError):
                quantity = None

        setattr(tree, date_field, marked_date)
        setattr(tree, quantity_field, quantity)
        setattr(tree, marked_at_field, None)
        setattr(tree, marked_by_field, None)
        tree.updated_by = request.user
        tree.save(update_fields=[
            date_field, quantity_field, marked_at_field, marked_by_field, "updated_by", "updated_at",
        ])
        TreeCareRecord.objects.create(
            tree=tree, kind=kind, action=TreeCareRecord.ACTION_COMPLETED,
            date=marked_date, quantity=quantity, recorded_by=request.user,
        )
        record_field_completion(
            tree.farm_block, kind, {tree: quantity}, marked_date, request.user,
        )
        return Response(TreeSerializer(tree).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsAssignedPicBlockOrManagerOrSuperAdmin])
    def mark_harvested(self, request, pk=None):
        """Confirms this tree's harvest is actually done — the PIC Block
        field app's counterpart to a Manager's stage-1 "mark needing
        harvest" (see this class's own docstring). Optional `date` in the
        POST body (defaults to today) and optional `quantity` (kg);
        always clears harvest_marked_at/harvest_marked_by regardless of
        whether they were set — completing the work is valid even if no
        one explicitly flagged it first.
        """
        return self._mark_done(
            request, "last_harvested_at", "harvest_marked_at", "harvest_marked_by",
            "last_harvest_quantity", TreeCareRecord.KIND_HARVEST,
        )

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsAssignedPicBlockOrManagerOrSuperAdmin])
    def mark_fertilized(self, request, pk=None):
        """Fertilizer counterpart to mark_harvested() above."""
        return self._mark_done(
            request, "last_fertilized_at", "fertilizer_marked_at", "fertilizer_marked_by",
            "last_fertilizer_quantity", TreeCareRecord.KIND_FERTILIZER,
        )

    def _bulk_mark_done(self, request, date_field, marked_at_field, marked_by_field, quantity_field, kind):
        """Shared body for bulk_mark_harvested/bulk_mark_fertilized below —
        same per-tree logic as _mark_done() above, applied to every tree in
        one request under a single shared TreeCareRecord.batch_id (mirrors
        apps/dashboard/views.py's MarkTreesHarvestedView, the office
        equivalent of this action).

        This is a collection-level action (detail=False), so there's no
        single object for DRF to run has_object_permission() against —
        scoping instead comes from get_queryset() itself: filtering the
        requested tree_ids against self.get_queryset() (already restricted
        by scoped_to_user_blocks() in get_queryset() above) silently drops
        any id the requesting user isn't allowed to touch, exactly the same
        rule IsAssignedPicBlockOrManagerOrSuperAdmin.has_object_permission()
        would apply one tree at a time. Dropped ids are reported back in
        `skipped_tree_ids` rather than raising, matching the dashboard's own
        MarkTreesActionMixin._get_selected_trees() convention (silently
        filter to what's valid; the caller can still see what didn't apply).
        """
        tree_ids = request.data.get("tree_ids")
        if not isinstance(tree_ids, list) or not tree_ids:
            raise ValidationError({"tree_ids": "Provide a non-empty list of tree ids."})

        quantities = request.data.get("quantities") or {}
        if not isinstance(quantities, dict):
            raise ValidationError({"quantities": "Must be an object mapping tree id to quantity."})

        date_str = request.data.get("date")
        marked_date = parse_date(date_str) if date_str else None
        if marked_date is None:
            marked_date = timezone.localdate()

        trees = list(self.get_queryset().filter(id__in=tree_ids))
        found_ids = {str(tree.id) for tree in trees}
        skipped_tree_ids = [str(tid) for tid in tree_ids if str(tid) not in found_ids]

        batch_id = uuid.uuid4()
        updated_trees = []
        tree_quantities = {}
        for tree in trees:
            # Same optional/best-effort quantity parsing as _mark_done()
            # above — a bulk completion has never required a quantity per
            # tree any more than the single-tree action does.
            quantity_raw = quantities.get(str(tree.id))
            quantity = None
            if quantity_raw not in (None, ""):
                try:
                    quantity = float(quantity_raw)
                except (TypeError, ValueError):
                    quantity = None

            tree_quantities[tree.id] = quantity
            setattr(tree, date_field, marked_date)
            setattr(tree, quantity_field, quantity)
            setattr(tree, marked_at_field, None)
            setattr(tree, marked_by_field, None)
            tree.updated_by = request.user
            tree.save(update_fields=[
                date_field, quantity_field, marked_at_field, marked_by_field, "updated_by", "updated_at",
            ])
            TreeCareRecord.objects.create(
                tree=tree, kind=kind, action=TreeCareRecord.ACTION_COMPLETED,
                date=marked_date, quantity=quantity, recorded_by=request.user, batch_id=batch_id,
            )
            updated_trees.append(tree)

        by_block = {}
        for tree in updated_trees:
            by_block.setdefault(tree.farm_block_id, (tree.farm_block, {}))[1][tree] = tree_quantities[tree.id]
        for block, tree_qty in by_block.values():
            record_field_completion(block, kind, tree_qty, marked_date, request.user)

        return Response({
            "updated": TreeSerializer(updated_trees, many=True).data,
            "skipped_tree_ids": skipped_tree_ids,
            "batch_id": str(batch_id),
        })

    @action(detail=False, methods=["post"], url_path="bulk_mark_harvested", permission_classes=[IsAuthenticated])
    def bulk_mark_harvested(self, request):
        """Bulk counterpart to mark_harvested() above — see this class's
        own docstring and _bulk_mark_done()'s. Body: `tree_ids` (required,
        non-empty list), `quantities` (optional `{tree_id: kg}`),
        `date` (optional, defaults to today).
        """
        return self._bulk_mark_done(
            request, "last_harvested_at", "harvest_marked_at", "harvest_marked_by",
            "last_harvest_quantity", TreeCareRecord.KIND_HARVEST,
        )

    @action(detail=False, methods=["post"], url_path="bulk_mark_fertilized", permission_classes=[IsAuthenticated])
    def bulk_mark_fertilized(self, request):
        """Fertilizer counterpart to bulk_mark_harvested() above."""
        return self._bulk_mark_done(
            request, "last_fertilized_at", "fertilizer_marked_at", "fertilizer_marked_by",
            "last_fertilizer_quantity", TreeCareRecord.KIND_FERTILIZER,
        )
