from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import IdempotentUUIDCreateMixin
from apps.common.permissions import IsAssignedPicBlockOrManagerOrSuperAdmin
from apps.common.scoping import scoped_to_user_blocks

from .models import Tree
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

    def _mark_done(self, request, date_field, marked_at_field, marked_by_field):
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

        setattr(tree, date_field, marked_date)
        setattr(tree, marked_at_field, None)
        setattr(tree, marked_by_field, None)
        tree.updated_by = request.user
        tree.save(update_fields=[date_field, marked_at_field, marked_by_field, "updated_by", "updated_at"])
        return Response(TreeSerializer(tree).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsAssignedPicBlockOrManagerOrSuperAdmin])
    def mark_harvested(self, request, pk=None):
        """Confirms this tree's harvest is actually done — the PIC Block
        field app's counterpart to a Manager's stage-1 "mark needing
        harvest" (see this class's own docstring). Optional `date` in the
        POST body (defaults to today); always clears harvest_marked_at/
        harvest_marked_by regardless of whether they were set — completing
        the work is valid even if no one explicitly flagged it first.
        """
        return self._mark_done(request, "last_harvested_at", "harvest_marked_at", "harvest_marked_by")

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, IsAssignedPicBlockOrManagerOrSuperAdmin])
    def mark_fertilized(self, request, pk=None):
        """Fertilizer counterpart to mark_harvested() above."""
        return self._mark_done(request, "last_fertilized_at", "fertilizer_marked_at", "fertilizer_marked_by")
