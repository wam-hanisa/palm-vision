from rest_framework import serializers

from .models import ReportDefinition, ReportRun


class ReportDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportDefinition
        fields = [
            "id",
            "farm",
            "name",
            "report_type",
            "export_format",
            "filters",
            "recurrence",
            "subscribers",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["id", "created_by", "created_at"]


class ReportRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportRun
        fields = [
            "id",
            "definition",
            "status",
            "file",
            "error_message",
            "triggered_by",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields
