from django.contrib.gis.admin import GISModelAdmin

from django.contrib import admin

from .models import Tree, TreeCareRecord


@admin.register(Tree)
class TreeAdmin(GISModelAdmin):
    list_display = ("__str__", "farm_block", "source", "status", "current_health")
    list_filter = ("farm_block__farm", "source", "status", "current_health")
    search_fields = ("name",)  # required by TreeCareRecordAdmin's autocomplete_fields below


@admin.register(TreeCareRecord)
class TreeCareRecordAdmin(admin.ModelAdmin):
    list_display = ("__str__", "tree", "kind", "action", "date", "quantity", "recorded_by", "batch_id")
    list_filter = ("kind", "action")
    autocomplete_fields = ("tree",)
