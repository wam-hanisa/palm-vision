from rest_framework.routers import DefaultRouter

from .views import ReportDefinitionViewSet, ReportRunViewSet

router = DefaultRouter()
router.register("reports", ReportDefinitionViewSet, basename="reportdefinition")
router.register("report-runs", ReportRunViewSet, basename="reportrun")

urlpatterns = router.urls
