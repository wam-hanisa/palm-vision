from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BlockProcessingRunsView,
    DetectionActionViewSet,
    ImageViewSet,
    ModelVersionViewSet,
    ProcessImageView,
    ProcessingRunDetectionsView,
)

router = DefaultRouter()
router.register("images", ImageViewSet, basename="image")
router.register("model-versions", ModelVersionViewSet, basename="modelversion")
router.register("detections", DetectionActionViewSet, basename="detection")

urlpatterns = router.urls + [
    path("farm-blocks/<uuid:block_id>/images/<uuid:image_id>/process/", ProcessImageView.as_view(), name="process-image"),
    path("farm-blocks/<uuid:block_id>/processing-runs/", BlockProcessingRunsView.as_view(), name="block-processing-runs"),
    path("processing-runs/<uuid:run_id>/detections/", ProcessingRunDetectionsView.as_view(), name="processing-run-detections"),
]
