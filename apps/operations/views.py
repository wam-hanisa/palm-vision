from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.mixins import IdempotentUUIDCreateMixin
from apps.common.permissions import IsSuperAdminOrManager
from apps.common.scoping import scoped_to_user_blocks as _scoped_to_user_blocks

from .models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree
from .serializers import (
    ActualFertilizerSerializer,
    ActualHarvestSerializer,
    FertilizerSerializer,
    FertilizerTreeSerializer,
    HarvestSerializer,
    HarvestTreeSerializer,
)
from .services import advance_status

# _scoped_to_user_blocks is now apps/common/scoping.py's scoped_to_user_blocks
# (imported under its old local name to keep every call site below
# unchanged) — centralized so this app's read scoping can't drift from the
# same function apps/operations/serializers.py's FK write-scoping uses.


class HarvestViewSet(viewsets.ModelViewSet):
    """docs/04-api-spec.md. `approve` is a separate action, Manager/Super
    Admin only, enforced server-side — see apps/operations/services.py for
    why this matters (it's fixing a real bug from the original app).
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


class ActualHarvestViewSet(IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    serializer_class = ActualHarvestSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ActualHarvest.objects.select_related("harvest__farm_block__farm").prefetch_related("harvest_trees")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="harvest__farm_block")


class HarvestTreeViewSet(IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    """Per-tree yield entries — the offline-writable, GPS-tagged records
    described in docs/05-offline-sync.md § Yield can be logged at block
    level or tree level.
    """

    serializer_class = HarvestTreeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = HarvestTree.objects.select_related("actual_harvest__harvest__farm_block__farm", "tree")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="actual_harvest__harvest__farm_block")

    def get_idempotent_create_defaults(self, request):
        return {"recorded_by": request.user}

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)


class FertilizerViewSet(viewsets.ModelViewSet):
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


class ActualFertilizerViewSet(IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    serializer_class = ActualFertilizerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ActualFertilizer.objects.select_related("fertilizer__farm_block__farm").prefetch_related("fertilizer_trees")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="fertilizer__farm_block")


class FertilizerTreeViewSet(IdempotentUUIDCreateMixin, viewsets.ModelViewSet):
    serializer_class = FertilizerTreeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = FertilizerTree.objects.select_related("actual_fertilizer__fertilizer__farm_block__farm", "tree")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="actual_fertilizer__fertilizer__farm_block")

    def get_idempotent_create_defaults(self, request):
        return {"recorded_by": request.user}

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)
