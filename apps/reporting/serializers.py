from rest_framework import serializers

from .models import ReportDefinition, ReportRun
from .services import eligible_subscribers


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

    def validate(self, attrs):
        farm = attrs.get("farm", getattr(self.instance, "farm", None))
        subscribers = attrs.get("subscribers")
        if farm is not None and subscribers:
            allowed = set(eligible_subscribers(farm).values_list("id", flat=True))
            if any(user.id not in allowed for user in subscribers):
                raise serializers.ValidationError(
                    {"subscribers": "Subscribers must be a Manager of this report's farm or a Super Admin."}
                )
        return attrs


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
