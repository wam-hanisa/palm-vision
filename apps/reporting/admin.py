from django.contrib import admin

from .models import ReportDefinition, ReportRun


class ReportRunInline(admin.TabularInline):
    model = ReportRun
    extra = 0
    readonly_fields = ("status", "file", "error_message", "triggered_by", "started_at", "completed_at", "created_at")


@admin.register(ReportDefinition)
class ReportDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "farm", "report_type", "export_format", "recurrence")
    list_filter = ("report_type", "export_format", "recurrence", "farm")
    inlines = [ReportRunInline]


@admin.register(ReportRun)
class ReportRunAdmin(admin.ModelAdmin):
    list_display = ("definition", "status", "triggered_by", "created_at")
    list_filter = ("status", "definition__farm")
