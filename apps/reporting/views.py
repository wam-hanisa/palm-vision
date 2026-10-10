"""docs/04-api-spec.md: POST /api/reports/{definition_id}/run/ — 'Trigger
on-demand report generation | Async — returns a ReportRun to poll/download
once ready.' Creating a ReportDefinition is an office/Manager action (same
reasoning as apps/compliance/views.py); reading definitions/runs and
triggering an on-demand run follow ordinary farm scoping — a PIC Block user
should be able to pull a production report for their own block.
"""
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import user_role_on_farm
from apps.farms.models import FarmBlock, UserFarmRole

from .models import ReportDefinition, ReportRun
from .serializers import ReportDefinitionSerializer, ReportRunSerializer
from .tasks import generate_report_task


def _managed_farm_ids(user):
    return UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).values_list("farm_id", flat=True)


def _scoped_to_user_farms(queryset, user, farm_field="farm"):
    """Manager-of-the-farm OR PIC-of-any-block-on-that-farm — read access
    for reports follows the same "can see this farm at all" rule as
    everywhere else, wider than the Manager-only *create* permission below.
    """
    if user.is_superuser:
        return queryset
    # A PIC Block only reaches reports restricted to one of their own blocks
    # (a farm-wide or other-block report would expose figures they can't see
    # anywhere else).
    pic_block_ids = [
        str(pk) for pk in FarmBlock.objects.filter(pic=user).values_list("id", flat=True)
    ]
    prefix = "filters__" if farm_field == "farm" else "definition__filters__"
    return queryset.filter(
        Q(**{f"{farm_field}_id__in": _managed_farm_ids(user)})
        | Q(**{f"{prefix}farm_block_id__in": pic_block_ids})
    ).distinct()


class ReportDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = ReportDefinitionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ReportDefinition.objects.select_related("farm").prefetch_related("subscribers")
        return _scoped_to_user_farms(qs, self.request.user)

    def _require_manager_or_superadmin_for_farm(self, farm):
        if not (self.request.user.is_superuser or user_role_on_farm(self.request.user, farm) == UserFarmRole.ROLE_MANAGER):
            raise PermissionDenied("Only a Manager or Super Admin can define or edit a report.")

    def perform_create(self, serializer):
        self._require_manager_or_superadmin_for_farm(serializer.validated_data["farm"])
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        # Also covers a `farm` reassignment on update — check the new farm,
        # not just the one the definition already belonged to (was missing
        # entirely before this fix: only perform_create() checked anything,
        # so PATCHing an existing definition bypassed the Manager-only rule).
        farm = serializer.validated_data.get("farm", serializer.instance.farm)
        self._require_manager_or_superadmin_for_farm(farm)
        serializer.save()

    def perform_destroy(self, instance):
        self._require_manager_or_superadmin_for_farm(instance.farm)
        instance.delete()

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        definition = self.get_object()
        report_run = ReportRun.objects.create(definition=definition, triggered_by=request.user)
        generate_report_task.delay(str(report_run.id))
        report_run.refresh_from_db()
        return Response(ReportRunSerializer(report_run).data)


class ReportRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Read/poll/download only — a run is never created directly, only via
    ReportDefinitionViewSet.run() or the scheduled task.
    """

    serializer_class = ReportRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ReportRun.objects.select_related("definition__farm")
        return _scoped_to_user_farms(qs, self.request.user, farm_field="definition__farm")
