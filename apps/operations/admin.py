from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree


class ActualHarvestInline(admin.TabularInline):
    model = ActualHarvest
    extra = 0


@admin.register(Harvest)
class HarvestAdmin(admin.ModelAdmin):
    list_display = ("farm_block", "assessment_date", "assessment_quantity", "status")
    list_filter = ("status", "farm_block__farm")
    search_fields = ("farm_block__name",)
    inlines = [ActualHarvestInline]


@admin.register(ActualHarvest)
class ActualHarvestAdmin(admin.ModelAdmin):
    list_display = ("harvest", "date", "quantity", "get_effective_quantity")
    list_filter = ("harvest__farm_block__farm",)

    @admin.display(description="Effective quantity")
    def get_effective_quantity(self, obj):
        return obj.get_effective_quantity()


@admin.register(HarvestTree)
class HarvestTreeAdmin(GISModelAdmin):
    list_display = ("tree", "actual_harvest", "quantity", "weight_kg", "recorded_by", "recorded_at")
    list_filter = ("actual_harvest__harvest__farm_block__farm",)


class ActualFertilizerInline(admin.TabularInline):
    model = ActualFertilizer
    extra = 0


@admin.register(Fertilizer)
class FertilizerAdmin(admin.ModelAdmin):
    list_display = ("farm_block", "assessment_date", "status")
    list_filter = ("status", "farm_block__farm")
    search_fields = ("farm_block__name",)
    inlines = [ActualFertilizerInline]


@admin.register(ActualFertilizer)
class ActualFertilizerAdmin(admin.ModelAdmin):
    list_display = ("fertilizer", "date")
    list_filter = ("fertilizer__farm_block__farm",)


@admin.register(FertilizerTree)
class FertilizerTreeAdmin(admin.ModelAdmin):
    list_display = ("tree", "actual_fertilizer", "quantity", "recorded_by", "recorded_at")
    list_filter = ("actual_fertilizer__fertilizer__farm_block__farm",)
