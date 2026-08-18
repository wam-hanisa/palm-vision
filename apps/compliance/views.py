"""
docs/07-compliance.md + docs/04-api-spec.md. Compliance filing is treated as
an office/management function, not a field-staff one — read access follows
the same farm/block scoping as everywhere else in this project (Super Admin:
all; Manager: farms they manage; PIC Block: blocks they're assigned to), but
writes require Manager or Super Admin, checked server-side, never trusted
from the client — the same non-negotiable rule CLAUDE.md states for
approvals elsewhere in this app.
"""
from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import user_role_on_farm
from apps.farms.models import UserFarmRole

from . import services
from .models import (
    AuditResult,
    ChecklistItem,
    ComplianceAudit,
    ComplianceAuditTemplate,
    ComplianceDocument,
    DeforestationBaseline,
    GrievanceRecord,
    HarvestBatch,
)
from .serializers import (
    AuditResultSerializer,
    ChecklistItemSerializer,
    ComplianceAuditSerializer,
    ComplianceAuditTemplateSerializer,
    ComplianceDocumentSerializer,
    DeforestationBaselineSerializer,
    GrievanceRecordSerializer,
    HarvestBatchSerializer,
)


def _managed_farm_ids(user):
    return UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).values_list("farm_id", flat=True)


def _scoped_to_user_farms(queryset, user, farm_field="farm"):
    """Farm-only scoping (no block-level PIC concept applies — GrievanceRecord
    is farm-level; ComplianceDocument is scoped separately below since it can
    attach to either a farm or a block).
    """
    if user.is_superuser:
        return queryset
    return queryset.filter(**{f"{farm_field}_id__in": _managed_farm_ids(user)})


def _scoped_to_user_blocks(queryset, user, block_field="farm_block"):
    if user.is_superuser:
        return queryset
    return queryset.filter(
        Q(**{f"{block_field}__pic": user}) | Q(**{f"{block_field}__farm_id__in": _managed_farm_ids(user)})
    )


def _is_manager_or_superadmin_for_farm(user, farm):
    if user.is_superuser:
        return True
    if farm is None:
        return False
    return user_role_on_farm(user, farm) == UserFarmRole.ROLE_MANAGER


def _require_manager_or_superadmin_for_farm(user, farm):
    if not _is_manager_or_superadmin_for_farm(user, farm):
        raise PermissionDenied("Only a Manager or Super Admin scoped to this farm can do this.")


class ComplianceDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = ComplianceDocumentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = ComplianceDocument.objects.select_related("farm", "farm_block__farm")
        if user.is_superuser:
            return qs
        managed = _managed_farm_ids(user)
        return qs.filter(
            Q(farm_id__in=managed)
            | Q(farm_block__farm_id__in=managed)
            | Q(farm_block__pic=user)
        )

    def _target_farm(self, serializer_or_instance):
        farm = serializer_or_instance.validated_data.get("farm") if hasattr(serializer_or_instance, "validated_data") else None
        if farm:
            return farm
        farm_block = (
            serializer_or_instance.validated_data.get("farm_block")
            if hasattr(serializer_or_instance, "validated_data")
            else None
        )
        if farm_block:
            return farm_block.farm
        instance = serializer_or_instance.instance if hasattr(serializer_or_instance, "instance") else serializer_or_instance
        if instance is not None:
            return instance.farm or (instance.farm_block.farm if instance.farm_block_id else None)
        return None

    def perform_create(self, serializer):
        farm = self._target_farm(serializer)
        _require_manager_or_superadmin_for_farm(self.request.user, farm)
        serializer.save()

    def perform_update(self, serializer):
        farm = self._target_farm(serializer) or self._target_farm(serializer.instance)
        _require_manager_or_superadmin_for_farm(self.request.user, farm)
        serializer.save()

    def perform_destroy(self, instance):
        farm = instance.farm or (instance.farm_block.farm if instance.farm_block_id else None)
        _require_manager_or_superadmin_for_farm(self.request.user, farm)
        instance.delete()


class DeforestationBaselineViewSet(viewsets.ModelViewSet):
    serializer_class = DeforestationBaselineSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = DeforestationBaseline.objects.select_related("farm_block__farm")
        return _scoped_to_user_blocks(qs, self.request.user)

    def perform_create(self, serializer):
        farm_block = serializer.validated_data["farm_block"]
        _require_manager_or_superadmin_for_farm(self.request.user, farm_block.farm)
        serializer.save()

    def perform_update(self, serializer):
        farm_block = serializer.validated_data.get("farm_block", serializer.instance.farm_block)
        _require_manager_or_superadmin_for_farm(self.request.user, farm_block.farm)
        serializer.save()

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """Manager/Super Admin marks a baseline as reviewed — sets
        verified_by/verified_at server-side (never trusts a client-supplied
        verifier), and optionally the resulting status.
        """
        baseline = self.get_object()
        _require_manager_or_superadmin_for_farm(request.user, baseline.farm_block.farm)
        new_status = request.data.get("status", baseline.status)
        if new_status not in dict(DeforestationBaseline.STATUS_CHOICES):
            raise ValidationError({"status": "Invalid status."})
        baseline.status = new_status
        baseline.verified_by = request.user
        baseline.verified_at = timezone.now()
        baseline.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
        return Response(DeforestationBaselineSerializer(baseline).data)


class ComplianceAuditTemplateViewSet(viewsets.ModelViewSet):
    """Templates aren't farm-scoped (they're reusable across farms), but
    creating/editing one is still a management action, not open to anyone
    authenticated — Manager-on-any-farm or Super Admin.
    """

    serializer_class = ComplianceAuditTemplateSerializer
    permission_classes = [IsAuthenticated]
    queryset = ComplianceAuditTemplate.objects.prefetch_related("checklist_items")

    def _require_any_manager(self):
        user = self.request.user
        if user.is_superuser:
            return
        if not UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).exists():
            raise PermissionDenied("Only a Manager or Super Admin can manage audit templates.")

    def perform_create(self, serializer):
        self._require_any_manager()
        serializer.save()

    def perform_update(self, serializer):
        self._require_any_manager()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_any_manager()
        instance.delete()


class ChecklistItemViewSet(viewsets.ModelViewSet):
    serializer_class = ChecklistItemSerializer
    permission_classes = [IsAuthenticated]
    queryset = ChecklistItem.objects.select_related("template")


class ComplianceAuditViewSet(viewsets.ModelViewSet):
    serializer_class = ComplianceAuditSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = ComplianceAudit.objects.select_related("farm_block__farm", "template").prefetch_related("results")
        return _scoped_to_user_blocks(qs, self.request.user)

    def perform_create(self, serializer):
        farm_block = serializer.validated_data["farm_block"]
        _require_manager_or_superadmin_for_farm(self.request.user, farm_block.farm)
        serializer.save()

    def perform_update(self, serializer):
        farm_block = serializer.validated_data.get("farm_block", serializer.instance.farm_block)
        _require_manager_or_superadmin_for_farm(self.request.user, farm_block.farm)
        serializer.save()


class AuditResultViewSet(viewsets.ModelViewSet):
    serializer_class = AuditResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AuditResult.objects.select_related("audit__farm_block__farm", "checklist_item")
        return _scoped_to_user_blocks(qs, self.request.user, block_field="audit__farm_block")

    def perform_create(self, serializer):
        audit = serializer.validated_data["audit"]
        _require_manager_or_superadmin_for_farm(self.request.user, audit.farm_block.farm)
        serializer.save()


class HarvestBatchViewSet(viewsets.ModelViewSet):
    """docs/07-compliance.md § EUDR-specific functions — the
    export-due-diligence action assembles a package for a batch: the
    block's boundary as GeoJSON, the block's DeforestationBaseline status,
    and the batch's aggregated totals. See export_due_diligence() below.
    """

    serializer_class = HarvestBatchSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = HarvestBatch.objects.select_related("farm_block__farm")
        return _scoped_to_user_blocks(qs, self.request.user)

    def perform_create(self, serializer):
        farm_block = serializer.validated_data["farm_block"]
        _require_manager_or_superadmin_for_farm(self.request.user, farm_block.farm)
        serializer.save()

    @action(detail=True, methods=["post"], url_path="export-due-diligence")
    def export_due_diligence(self, request, pk=None):
        batch = self.get_object()
        _require_manager_or_superadmin_for_farm(request.user, batch.farm_block.farm)
        try:
            package = services.assemble_due_diligence_package(batch)
        except services.DueDiligenceExportRefused as exc:
            raise ValidationError(str(exc))
        return Response(package)


class GrievanceRecordViewSet(viewsets.ModelViewSet):
    serializer_class = GrievanceRecordSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = GrievanceRecord.objects.select_related("farm")
        return _scoped_to_user_farms(qs, self.request.user)

    def perform_create(self, serializer):
        farm = serializer.validated_data["farm"]
        _require_manager_or_superadmin_for_farm(self.request.user, farm)
        serializer.save()

    def perform_update(self, serializer):
        farm = serializer.validated_data.get("farm", serializer.instance.farm)
        _require_manager_or_superadmin_for_farm(self.request.user, farm)
        serializer.save()
