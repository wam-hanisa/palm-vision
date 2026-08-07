from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.common.mixins import IdempotentUUIDCreateMixin
from apps.farms.models import UserFarmRole

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
    """

    serializer_class = TreeSerializer
    permission_classes = [IsAuthenticated]
    queryset = Tree.objects.select_related("farm_block", "farm_block__farm")

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.is_superuser:
            return qs
        managed_farm_ids = UserFarmRole.objects.filter(
            user=user, role=UserFarmRole.ROLE_MANAGER
        ).values_list("farm_id", flat=True)
        return qs.filter(Q(farm_block__pic=user) | Q(farm_block__farm_id__in=managed_farm_ids))

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
