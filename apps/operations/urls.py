from rest_framework.routers import DefaultRouter

from .views import (
    ActualFertilizerViewSet,
    ActualHarvestViewSet,
    FertilizerTreeViewSet,
    FertilizerViewSet,
    HarvestTreeViewSet,
    HarvestViewSet,
)

router = DefaultRouter()
router.register("harvests", HarvestViewSet, basename="harvest")
router.register("actual-harvests", ActualHarvestViewSet, basename="actualharvest")
router.register("harvest-trees", HarvestTreeViewSet, basename="harvesttree")
router.register("fertilizers", FertilizerViewSet, basename="fertilizer")
router.register("actual-fertilizers", ActualFertilizerViewSet, basename="actualfertilizer")
router.register("fertilizer-trees", FertilizerTreeViewSet, basename="fertilizertree")

urlpatterns = router.urls
