from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = "dashboard"

urlpatterns = [
    path("login/", views.DashboardLoginView.as_view(), name="login"),
    path("logout/", views.DashboardLogoutView.as_view(), name="logout"),
    path("", views.HomeView.as_view(), name="home"),
    path("farms/", views.FarmMapView.as_view(), name="farms"),
    path("farms/blocks/add/", views.FarmBlockCreateView.as_view(), name="block-add"),
    path("farms/blocks/<uuid:block_id>/edit/", views.FarmBlockUpdateView.as_view(), name="block-edit"),
    path(
        "farms/blocks/<uuid:block_id>/boundary/",
        views.FarmBlockBoundaryEditView.as_view(),
        name="block-boundary",
    ),
    path("farms/blocks/<uuid:block_id>/delete/", views.FarmBlockDeleteView.as_view(), name="block-delete"),
    path("farms/blocks/<uuid:block_id>/trees/add/", views.TreeCreateView.as_view(), name="tree-add"),
    path(
        "farms/blocks/<uuid:block_id>/harvest-plan/add/",
        views.HarvestPlanCreateView.as_view(),
        name="harvest-plan-add",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest-plan/<uuid:harvest_id>/edit/",
        views.HarvestPlanUpdateView.as_view(),
        name="harvest-plan-edit",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest-plan/<uuid:harvest_id>/delete/",
        views.HarvestPlanDeleteView.as_view(),
        name="harvest-plan-delete",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest-plan/<uuid:harvest_id>/approve/",
        views.HarvestPlanApproveView.as_view(),
        name="harvest-plan-approve",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer-plan/add/",
        views.FertilizerPlanCreateView.as_view(),
        name="fertilizer-plan-add",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer-plan/<uuid:fertilizer_id>/edit/",
        views.FertilizerPlanUpdateView.as_view(),
        name="fertilizer-plan-edit",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer-plan/<uuid:fertilizer_id>/delete/",
        views.FertilizerPlanDeleteView.as_view(),
        name="fertilizer-plan-delete",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer-plan/<uuid:fertilizer_id>/approve/",
        views.FertilizerPlanApproveView.as_view(),
        name="fertilizer-plan-approve",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest/record/",
        views.RecordActualHarvestView.as_view(),
        name="harvest-record",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest/<uuid:actual_id>/edit/",
        views.ActualHarvestUpdateView.as_view(),
        name="harvest-actual-edit",
    ),
    path(
        "farms/blocks/<uuid:block_id>/harvest/<uuid:actual_id>/delete/",
        views.ActualHarvestDeleteView.as_view(),
        name="harvest-actual-delete",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer/record/",
        views.RecordActualFertilizerView.as_view(),
        name="fertilizer-record",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer/<uuid:actual_id>/edit/",
        views.ActualFertilizerUpdateView.as_view(),
        name="fertilizer-actual-edit",
    ),
    path(
        "farms/blocks/<uuid:block_id>/fertilizer/<uuid:actual_id>/delete/",
        views.ActualFertilizerDeleteView.as_view(),
        name="fertilizer-actual-delete",
    ),
    path(
        "farms/blocks/<uuid:block_id>/trees/mark-harvested/",
        views.MarkTreesHarvestedView.as_view(),
        name="trees-mark-harvested",
    ),
    path(
        "farms/blocks/<uuid:block_id>/trees/mark-fertilized/",
        views.MarkTreesFertilizedView.as_view(),
        name="trees-mark-fertilized",
    ),
    path(
        "farms/blocks/<uuid:block_id>/trees/mark-need-harvest/",
        views.MarkTreesNeedHarvestView.as_view(),
        name="trees-mark-need-harvest",
    ),
    path(
        "farms/blocks/<uuid:block_id>/trees/mark-need-fertilize/",
        views.MarkTreesNeedFertilizeView.as_view(),
        name="trees-mark-need-fertilize",
    ),
    path("trees/<uuid:tree_id>/edit/", views.TreeUpdateView.as_view(), name="tree-edit"),
    path("trees/<uuid:tree_id>/remove/", views.TreeRemoveView.as_view(), name="tree-remove"),
    path(
        "trees/<uuid:tree_id>/health-history/",
        views.TreeHealthHistoryView.as_view(),
        name="tree-health-history",
    ),
    path(
        "trees/<uuid:tree_id>/care-history/",
        views.TreeCareHistoryView.as_view(),
        name="tree-care-history",
    ),
    path(
        "farms/blocks/<uuid:block_id>/care-history/",
        views.BlockCareHistoryView.as_view(),
        name="block-care-history",
    ),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path("imagery/", views.ImageListUploadView.as_view(), name="imagery"),
    path(
        "imagery/<uuid:image_id>/confirm-blocks/",
        views.ImageConfirmBlocksView.as_view(),
        name="imagery-confirm-blocks",
    ),
    path("imagery/<uuid:image_id>/delete/", views.ImageDeleteView.as_view(), name="imagery-delete"),
    path("imagery/uploads/", views.CreateUploadSessionView.as_view(), name="imagery-upload-create"),
    path("imagery/uploads/<uuid:session_id>/", views.UploadSessionStatusView.as_view(), name="imagery-upload-status"),
    path(
        "imagery/uploads/<uuid:session_id>/chunks/<int:chunk_index>/",
        views.UploadChunkView.as_view(),
        name="imagery-upload-chunk",
    ),
    path(
        "imagery/uploads/<uuid:session_id>/complete/",
        views.CompleteUploadSessionView.as_view(),
        name="imagery-upload-complete",
    ),
    path("imagery/blocks/<uuid:block_id>/", views.BlockImageryView.as_view(), name="imagery-block"),
    path(
        "imagery/blocks/<uuid:block_id>/process/<uuid:image_id>/",
        views.TriggerProcessingView.as_view(),
        name="imagery-process",
    ),
    path("imagery/runs/<uuid:run_id>/", views.ReviewQueueView.as_view(), name="imagery-run"),
    path("imagery/runs/<uuid:run_id>/status/", views.RunStatusView.as_view(), name="imagery-run-status"),
    path(
        "imagery/runs/<uuid:run_id>/confirm-all/",
        views.ConfirmAllDetectionsView.as_view(),
        name="imagery-run-confirm-all",
    ),
    path(
        "imagery/detections/<uuid:detection_id>/confirm/",
        views.ConfirmDetectionView.as_view(),
        name="imagery-detection-confirm",
    ),
    path(
        "imagery/classifications/<uuid:classification_id>/review/",
        views.ReviewClassificationView.as_view(),
        name="imagery-classification-review",
    ),
    path(
        "imagery/detections/<uuid:detection_id>/reject/",
        views.RejectDetectionView.as_view(),
        name="imagery-detection-reject",
    ),
    path(
        "imagery/detections/<uuid:detection_id>/link/",
        views.LinkDetectionView.as_view(),
        name="imagery-detection-link",
    ),
    path("compliance/documents/", views.ComplianceDocumentListView.as_view(), name="compliance-documents"),
    path("compliance/batches/", views.HarvestBatchListView.as_view(), name="compliance-batches"),
    path("compliance/baselines/", views.DeforestationBaselineListView.as_view(), name="compliance-baselines"),
    path("compliance/audits/", views.ComplianceAuditListView.as_view(), name="compliance-audits"),
    path("compliance/grievances/", views.GrievanceListView.as_view(), name="compliance-grievances"),
    path(
        "compliance/grievances/<uuid:grievance_id>/update/",
        views.GrievanceUpdateStatusView.as_view(),
        name="compliance-grievance-update",
    ),
    path("reports/", views.ReportDefinitionListView.as_view(), name="reports"),
    path("reports/<uuid:definition_id>/run/", views.TriggerReportRunView.as_view(), name="report-run"),
    path("reports/<uuid:definition_id>/runs/", views.ReportRunListView.as_view(), name="report-runs"),
    # Old standalone Map page — folded into FarmMapView (dashboard:farms).
    # Kept as a redirect (not just removed) since it may still be
    # bookmarked; query_string=True preserves any ?farm=/?block= params a
    # bookmark might carry.
    path("map/", RedirectView.as_view(pattern_name="dashboard:farms", query_string=True), name="map"),
]
