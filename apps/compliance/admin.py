from django.contrib import admin

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


@admin.register(ComplianceDocument)
class ComplianceDocumentAdmin(admin.ModelAdmin):
    list_display = ("document_type", "farm", "farm_block", "expiry_date", "status")
    list_filter = ("document_type", "farm")
    search_fields = ("issuing_authority",)

    @admin.display(description="Status")
    def status(self, obj):
        return obj.status


@admin.register(DeforestationBaseline)
class DeforestationBaselineAdmin(admin.ModelAdmin):
    list_display = ("farm_block", "status", "cutoff_date", "verified_by", "verified_at")
    list_filter = ("status", "farm_block__farm")


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0


@admin.register(ComplianceAuditTemplate)
class ComplianceAuditTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "certification_scheme")
    inlines = [ChecklistItemInline]


class AuditResultInline(admin.TabularInline):
    model = AuditResult
    extra = 0


@admin.register(ComplianceAudit)
class ComplianceAuditAdmin(admin.ModelAdmin):
    list_display = ("farm_block", "template", "audit_date", "auditor_name", "overall_status", "next_audit_due")
    list_filter = ("overall_status", "auditor_type", "farm_block__farm")
    inlines = [AuditResultInline]


@admin.register(HarvestBatch)
class HarvestBatchAdmin(admin.ModelAdmin):
    list_display = ("farm_block", "period_start", "period_end", "total_quantity", "total_weight_kg", "export_status")
    list_filter = ("export_status", "farm_block__farm")


@admin.register(GrievanceRecord)
class GrievanceRecordAdmin(admin.ModelAdmin):
    list_display = ("farm", "date_reported", "status")
    list_filter = ("status", "farm")
