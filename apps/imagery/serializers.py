from rest_framework import serializers

from .models import Image, ImageProcessingRun, ModelVersion, TreeDetection, TreeHealthClassification


class ModelVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelVersion
        fields = [
            "id",
            "model_type",
            "status",
            "trained_at",
            "dataset_version",
            "trained_by",
            "validation_metrics",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class ImageSerializer(serializers.ModelSerializer):
    overlapping_block_ids = serializers.SerializerMethodField()

    class Meta:
        model = Image
        fields = [
            "id",
            "farm",
            "raw_file",
            "captured_at",
            "georeference_status",
            "georeference_error",
            "footprint",
            "uploaded_by",
            "overlapping_block_ids",
            "created_at",
        ]
        read_only_fields = ["georeference_status", "georeference_error", "footprint", "uploaded_by", "created_at"]

    def get_overlapping_block_ids(self, obj):
        if obj.georeference_status != Image.GEOREFERENCE_DONE:
            return None
        return [str(block_id) for block_id in obj.overlapping_blocks().values_list("id", flat=True)]


class TreeHealthClassificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = TreeHealthClassification
        fields = [
            "id",
            "tree_detection",
            "health_label",
            "confidence",
            "reviewed",
            "reviewed_by",
            "corrected_label",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class TreeDetectionSerializer(serializers.ModelSerializer):
    classification = TreeHealthClassificationSerializer(read_only=True)

    class Meta:
        model = TreeDetection
        fields = [
            "id",
            "processing_run",
            "tree",
            "detected_location",
            "detection_confidence",
            "match_status",
            "matched_distance_m",
            "classification",
            "created_at",
        ]
        read_only_fields = fields


class ImageProcessingRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageProcessingRun
        fields = [
            "id",
            "image",
            "farm_block",
            "status",
            "detection_model_version",
            "classification_model_version",
            "triggered_by",
            "detected_tree_count",
            "new_candidate_count",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields
