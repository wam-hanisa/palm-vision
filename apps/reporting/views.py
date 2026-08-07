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
from apps.farms.models import UserFarmRole

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
    return queryset.filter(
        Q(**{f"{farm_field}_id__in": _managed_farm_ids(user)}) | Q(**{f"{farm_field}__blocks__pic": user})
    ).distinct()


class ReportDefinitionViewSet(viewsets.ModelViewSet):
    serializer_class = ReportDefinitionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ReportDefinition.objects.select_related("farm").prefetch_related("subscribers")
        return _scoped_to_user_farms(qs, self.request.user)

    def perform_create(self, serializer):
        farm = serializer.validated_data["farm"]
        if not (self.request.user.is_superuser or user_role_on_farm(self.request.user, farm) == UserFarmRole.ROLE_MANAGER):
            raise PermissionDenied("Only a Manager or Super Admin can define a new report.")
        serializer.save(created_by=self.request.user)

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
