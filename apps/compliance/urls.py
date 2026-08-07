from rest_framework.routers import DefaultRouter

from .views import (
    AuditResultViewSet,
    ChecklistItemViewSet,
    ComplianceAuditTemplateViewSet,
    ComplianceAuditViewSet,
    ComplianceDocumentViewSet,
    DeforestationBaselineViewSet,
    GrievanceRecordViewSet,
    HarvestBatchViewSet,
)

router = DefaultRouter()
router.register("compliance-documents", ComplianceDocumentViewSet, basename="compliancedocument")
router.register("deforestation-baselines", DeforestationBaselineViewSet, basename="deforestationbaseline")
router.register("audit-templates", ComplianceAuditTemplateViewSet, basename="complianceaudittemplate")
router.register("checklist-items", ChecklistItemViewSet, basename="checklistitem")
router.register("compliance-audits", ComplianceAuditViewSet, basename="complianceaudit")
router.register("audit-results", AuditResultViewSet, basename="auditresult")
router.register("harvest-batches", HarvestBatchViewSet, basename="harvestbatch")
router.register("grievances", GrievanceRecordViewSet, basename="grievancerecord")

urlpatterns = router.urls
