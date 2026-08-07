from rest_framework.routers import DefaultRouter

from .views import FarmBlockViewSet, FarmViewSet, UserFarmRoleViewSet

router = DefaultRouter()
router.register("farms", FarmViewSet, basename="farm")
router.register("farm-blocks", FarmBlockViewSet, basename="farmblock")
router.register("user-farm-roles", UserFarmRoleViewSet, basename="userfarmrole")

urlpatterns = router.urls
