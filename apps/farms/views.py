from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Farm, FarmBlock, UserFarmRole
from .serializers import (
    FarmBlockGeoSerializer,
    FarmBlockSerializer,
    FarmSerializer,
    UserFarmRoleSerializer,
)

# Imported lazily-ish at module scope (not inside the method) since there's
# no circular import risk here: apps.trees.serializers only imports from
# apps.trees.models, nothing from apps.farms.
from apps.trees.models import Tree
from apps.trees.serializers import TreeSerializer


class FarmViewSet(viewsets.ModelViewSet):
    """See docs/04-api-spec.md. List is scoped: Super Admin sees every farm;
    everyone else sees only farms they hold a UserFarmRole on.
    """

    serializer_class = FarmSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return Farm.objects.all()
        farm_ids = UserFarmRole.objects.filter(user=user).values_list("farm_id", flat=True)
        return Farm.objects.filter(id__in=farm_ids)


class FarmBlockViewSet(viewsets.ModelViewSet):
    """docs/04-api-spec.md: 'GET /api/farm-blocks/ — List blocks visible to
    the current user. PIC Block sees only blocks they're assigned via
    UserFarmRole; Manager sees blocks in their scoped farm(s); Super Admin
    sees all.' (PIC Block scoping here is actually via FarmBlock.pic, the
    more precise block-level assignment — see apps/common/permissions.py
    docstring for why.)
    """

    serializer_class = FarmBlockSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = FarmBlock.objects.select_related("farm", "pic")
        if user.is_superuser:
            return qs
        managed_farm_ids = UserFarmRole.objects.filter(
            user=user, role=UserFarmRole.ROLE_MANAGER
        ).values_list("farm_id", flat=True)
        return qs.filter(Q(pic=user) | Q(farm_id__in=managed_farm_ids))

    @action(detail=True, methods=["get"])
    def geojson(self, request, pk=None):
        """Map-ready GeoJSON for a single block — docs/05-offline-sync.md
        geo-navigation. Kept as a separate action rather than the default
        representation so plain API consumers aren't forced into GeoJSON
        shape.
        """
        block = self.get_object()
        return Response(FarmBlockGeoSerializer(block).data)

    @action(detail=True, methods=["get"])
    def trees(self, request, pk=None):
        """GET /api/farm-blocks/{id}/trees/ — docs/04-api-spec.md: "For
        offline pre-caching before heading to the field." get_object()
        already enforces the same block-level scoping as the list endpoint
        (404s a block outside the requester's access rather than leaking
        its existence), so no extra permission check is needed here.
        Excludes soft-deleted trees (status=removed) — a field worker
        pre-caching for a trip has no use for trees no longer on the ground.
        """
        block = self.get_object()
        active_trees = block.trees.filter(status=Tree.STATUS_ACTIVE)
        return Response(TreeSerializer(active_trees, many=True, context={"request": request}).data)


class UserFarmRoleViewSet(viewsets.ModelViewSet):
    """Super Admin only in practice — enforced by settings.REST_FRAMEWORK
    DEFAULT_PERMISSION_CLASSES (IsAuthenticated) plus the queryset below;
    a stricter IsSuperAdmin-only permission class can be added here once
    the admin UI for role assignment is built (not yet — this exists so
    the API surface is complete per docs/04-api-spec.md, not because the
    UI for managing it exists this phase).
    """

    serializer_class = UserFarmRoleSerializer
    permission_classes = [IsAuthenticated]
    queryset = UserFarmRole.objects.select_related("user", "farm")
