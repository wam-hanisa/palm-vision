from django.contrib.gis.admin import GISModelAdmin

from django.contrib import admin

from .models import Tree


@admin.register(Tree)
class TreeAdmin(GISModelAdmin):
    list_display = ("__str__", "farm_block", "source", "status", "current_health")
    list_filter = ("farm_block__farm", "source", "status", "current_health")
