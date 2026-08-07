from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import Image, ImageProcessingRun, ModelVersion, TreeDetection, TreeHealthClassification


@admin.register(ModelVersion)
class ModelVersionAdmin(admin.ModelAdmin):
    list_display = ("model_type", "status", "trained_at", "dataset_version", "trained_by")
    list_filter = ("model_type", "status")
    actions = ["promote_selected_to_production"]

    @admin.action(description="Promote selected model(s) to production")
    def promote_selected_to_production(self, request, queryset):
        for model_version in queryset:
            model_version.promote_to_production()


@admin.register(Image)
class ImageAdmin(GISModelAdmin):
    list_display = ("id", "farm", "georeference_status", "uploaded_by", "created_at")
    list_filter = ("georeference_status", "farm")


class TreeDetectionInline(admin.TabularInline):
    model = TreeDetection
    extra = 0
    fields = ("detected_location", "detection_confidence", "match_status", "tree")
    readonly_fields = fields


@admin.register(ImageProcessingRun)
class ImageProcessingRunAdmin(admin.ModelAdmin):
    list_display = ("id", "image", "farm_block", "status", "detected_tree_count", "new_candidate_count", "created_at")
    list_filter = ("status", "farm_block__farm")
    inlines = [TreeDetectionInline]


@admin.register(TreeDetection)
class TreeDetectionAdmin(GISModelAdmin):
    list_display = ("id", "processing_run", "match_status", "detection_confidence", "tree")
    list_filter = ("match_status", "processing_run__farm_block__farm")


@admin.register(TreeHealthClassification)
class TreeHealthClassificationAdmin(admin.ModelAdmin):
    list_display = ("id", "tree_detection", "health_label", "confidence", "reviewed", "corrected_label")
    list_filter = ("health_label", "reviewed")
