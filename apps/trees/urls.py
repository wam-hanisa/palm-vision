from rest_framework.routers import DefaultRouter

from .views import TreeViewSet

router = DefaultRouter()
router.register("trees", TreeViewSet, basename="tree")

urlpatterns = router.urls
