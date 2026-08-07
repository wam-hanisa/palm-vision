from rest_framework import serializers

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


class ComplianceDocumentSerializer(serializers.ModelSerializer):
    status = serializers.ReadOnlyField()

    class Meta:
        model = ComplianceDocument
        fields = [
            "id",
            "farm",
            "farm_block",
            "document_type",
            "document_file",
            "issued_date",
            "expiry_date",
            "issuing_authority",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        farm = attrs.get("farm", getattr(self.instance, "farm", None))
        farm_block = attrs.get("farm_block", getattr(self.instance, "farm_block", None))
        if bool(farm) == bool(farm_block):
            raise serializers.ValidationError("Attach a compliance document to exactly one of farm or farm_block.")
        return attrs


class DeforestationBaselineSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeforestationBaseline
        fields = [
            "id",
            "farm_block",
            "cutoff_date",
            "source",
            "status",
            "verified_by",
            "verified_at",
            "evidence_image",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "verified_by", "verified_at"]


class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistItem
        fields = ["id", "template", "text", "order"]
        read_only_fields = ["id"]


class ComplianceAuditTemplateSerializer(serializers.ModelSerializer):
    checklist_items = ChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = ComplianceAuditTemplate
        fields = ["id", "name", "certification_scheme", "description", "checklist_items", "created_at"]
        read_only_fields = ["id", "created_at"]


class AuditResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditResult
        fields = ["id", "audit", "checklist_item", "result", "evidence_file", "notes"]
        read_only_fields = ["id"]


class ComplianceAuditSerializer(serializers.ModelSerializer):
    results = AuditResultSerializer(many=True, read_only=True)

    class Meta:
        model = ComplianceAudit
        fields = [
            "id",
            "farm_block",
            "template",
            "audit_date",
            "auditor_name",
            "auditor_type",
            "overall_status",
            "next_audit_due",
            "results",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class HarvestBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = HarvestBatch
        fields = [
            "id",
            "farm_block",
            "period_start",
            "period_end",
            "total_quantity",
            "total_weight_kg",
            "export_status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "total_quantity", "total_weight_kg", "export_status", "created_at", "updated_at"]


class GrievanceRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = GrievanceRecord
        fields = [
            "id",
            "farm",
            "date_reported",
            "description",
            "status",
            "resolution_notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
