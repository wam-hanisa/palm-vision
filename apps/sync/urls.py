from django.urls import path

from .views import SyncCheckInView, SyncPullView

urlpatterns = [
    path("sync/pull/", SyncPullView.as_view(), name="sync-pull"),
    path("sync/checkin/", SyncCheckInView.as_view(), name="sync-checkin"),
]
