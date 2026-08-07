from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import Farm, FarmBlock, UserFarmRole


@admin.register(Farm)
class FarmAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(FarmBlock)
class FarmBlockAdmin(GISModelAdmin):
    list_display = ("name", "farm", "commodity", "area", "year", "pic")
    list_filter = ("farm", "commodity")
    search_fields = ("name",)


@admin.register(UserFarmRole)
class UserFarmRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "farm", "role")
    list_filter = ("farm", "role")
