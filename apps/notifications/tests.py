"""
docs/02-build-plan.md Phase 7 verification gate (notification half):
"manually set a ComplianceDocument.expiry_date into the near-future window
and confirm the next Beat check produces a notification (visible in-app and
in the console email backend's output)." palmvision/settings/test.py sets
EMAIL_BACKEND to Django's locmem backend, so "the console email backend's
output" becomes django.core.mail.outbox here — same signal, inspectable in
a test instead of read off a log.
"""
import datetime

import pytest
from django.contrib.gis.geos import Point
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status

from apps.compliance.models import ComplianceDocument
from apps.imagery.models import Image, ImageProcessingRun, TreeDetection, TreeHealthClassification
from apps.imagery.services import run_processing
from apps.notifications.models import Notification, NotificationCategory, NotificationPreference
from apps.notifications.services import notify
from apps.notifications.tasks import (
    check_approvals_pending,
    check_compliance_expiry,
    check_review_backlog,
    check_stalled_sync,
    check_tree_health_anomalies,
)
from apps.operations.models import Fertilizer, PlanStatus
from apps.sync.models import SyncCheckIn


def _today_plus_date(days):
    return timezone.localdate() + datetime.timedelta(days=days)


# --- notify() / preferences --------------------------------------------

@pytest.mark.django_db
def test_notify_creates_in_app_and_sends_email_by_default(manager):
    notification = notify(manager, NotificationCategory.APPROVAL_PENDING, "Title", "Body")
    assert notification is not None
    assert Notification.objects.filter(user=manager, title="Title").exists()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [manager.email]


@pytest.mark.django_db
def test_notify_respects_in_app_disabled_preference(manager):
    NotificationPreference.objects.create(
        user=manager, category=NotificationCategory.APPROVAL_PENDING, in_app_enabled=False, email_enabled=True
    )
    notification = notify(manager, NotificationCategory.APPROVAL_PENDING, "Title", "Body")
    assert notification is None
    assert not Notification.objects.filter(user=manager).exists()
    assert len(mail.outbox) == 1  # email still sent


@pytest.mark.django_db
def test_notify_respects_email_disabled_preference(manager):
    NotificationPreference.objects.create(
        user=manager, category=NotificationCategory.APPROVAL_PENDING, in_app_enabled=True, email_enabled=False
    )
    notify(manager, NotificationCategory.APPROVAL_PENDING, "Title", "Body")
    assert Notification.objects.filter(user=manager).exists()
    assert len(mail.outbox) == 0


# --- check_compliance_expiry ---------------------------------------------

@pytest.mark.django_db
def test_compliance_expiry_notifies_manager_at_exact_90_day_threshold(manager, farm_role_manager, block_a):
    ComplianceDocument.objects.create(
        farm_block=block_a,
        document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=SimpleUploadedFile("doc.pdf", b"%PDF fake"),
        expiry_date=_today_plus_date(90),
    )
    check_compliance_expiry()
    assert Notification.objects.filter(user=manager, category=NotificationCategory.COMPLIANCE_EXPIRY).exists()
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_compliance_expiry_does_not_fire_off_threshold(manager, farm_role_manager, block_a):
    ComplianceDocument.objects.create(
        farm_block=block_a,
        document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=SimpleUploadedFile("doc.pdf", b"%PDF fake"),
        expiry_date=_today_plus_date(45),  # not one of the [90, 60, 30] thresholds
    )
    check_compliance_expiry()
    assert not Notification.objects.filter(user=manager, category=NotificationCategory.COMPLIANCE_EXPIRY).exists()
    assert len(mail.outbox) == 0


# --- check_tree_health_anomalies -----------------------------------------

@pytest.mark.django_db
def test_tree_health_anomaly_detected_on_meaningful_increase(settings, manager, farm_role_manager, block_a):
    settings.TREE_HEALTH_ANOMALY_MIN_INCREASE = 2  # keep the fixture data small
    image = Image.objects.create(farm=block_a.farm, uploaded_by=manager, raw_file="imagery/raw/x.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    now = timezone.now()

    def _classification(days_ago, label):
        detection = TreeDetection.objects.create(
            processing_run=run,
            detected_location=Point(101.4410, 0.5310, srid=4326),
            detection_confidence=0.9,
            match_status=TreeDetection.MATCH_MATCHED,
        )
        classification = TreeHealthClassification.objects.create(
            tree_detection=detection, health_label=label, confidence=0.9
        )
        TreeHealthClassification.objects.filter(id=classification.id).update(created_at=now - datetime.timedelta(days=days_ago))

    # Prior week: 0 unhealthy. Current week: 3 unhealthy — an increase of 3, above the threshold of 2.
    for _ in range(3):
        _classification(days_ago=2, label=TreeHealthClassification.HEALTH_YELLOW)

    check_tree_health_anomalies()
    assert Notification.objects.filter(user=manager, category=NotificationCategory.TREE_HEALTH_ANOMALY).exists()


# --- check_stalled_sync / SyncCheckIn ------------------------------------

@pytest.mark.django_db
def test_stalled_sync_notifies_affected_user(settings, pic_block):
    settings.STALLED_SYNC_DAYS = 3
    checkin = SyncCheckIn.objects.create(user=pic_block, device_id="device-1", pending_count=5)
    SyncCheckIn.objects.filter(id=checkin.id).update(last_checkin_at=timezone.now() - datetime.timedelta(days=5))

    check_stalled_sync()
    assert Notification.objects.filter(user=pic_block, category=NotificationCategory.STALLED_SYNC).exists()


@pytest.mark.django_db
def test_stalled_sync_ignores_recent_or_fully_synced_devices(pic_block):
    SyncCheckIn.objects.create(user=pic_block, device_id="device-recent", pending_count=5)  # just checked in
    SyncCheckIn.objects.create(user=pic_block, device_id="device-synced", pending_count=0)  # nothing pending
    check_stalled_sync()
    assert not Notification.objects.filter(user=pic_block, category=NotificationCategory.STALLED_SYNC).exists()


@pytest.mark.django_db
def test_sync_checkin_api_upserts(as_pic_block):
    first = as_pic_block.post("/api/sync/checkin/", {"device_id": "device-1", "pending_count": 3}, format="json")
    assert first.status_code == status.HTTP_200_OK
    assert SyncCheckIn.objects.get(device_id="device-1").pending_count == 3

    second = as_pic_block.post("/api/sync/checkin/", {"device_id": "device-1", "pending_count": 0}, format="json")
    assert second.status_code == status.HTTP_200_OK
    assert SyncCheckIn.objects.filter(device_id="device-1").count() == 1  # updated in place, not duplicated
    assert SyncCheckIn.objects.get(device_id="device-1").pending_count == 0


# --- check_review_backlog ------------------------------------------------

@pytest.mark.django_db
def test_review_backlog_notifies_manager_above_threshold(settings, manager, farm_role_manager, block_a):
    settings.REVIEW_BACKLOG_THRESHOLD = 2
    image = Image.objects.create(farm=block_a.farm, uploaded_by=manager, raw_file="imagery/raw/x.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    for _ in range(3):
        TreeDetection.objects.create(
            processing_run=run,
            detected_location=Point(101.4410, 0.5310, srid=4326),
            detection_confidence=0.9,
            match_status=TreeDetection.MATCH_NEW_CANDIDATE,
        )
    check_review_backlog()
    assert Notification.objects.filter(user=manager, category=NotificationCategory.REVIEW_BACKLOG).exists()


# --- check_approvals_pending ----------------------------------------------

@pytest.mark.django_db
def test_approvals_pending_notifies_manager_after_stale_period(settings, manager, farm_role_manager, block_a):
    settings.APPROVAL_PENDING_DAYS = 1
    plan = Fertilizer.objects.create(farm_block=block_a, assessment_date="2026-08-01")
    # advance_status() (apps/operations/services.py) only defines the
    # approve-action transitions (created->created_confirmed,
    # on_progress_wait->on_progress, done_wait->done) — CREATED_CONFIRMED ->
    # ON_PROGRESS_WAIT happens some other way in the real workflow (e.g. a
    # PIC Block starting field execution), not something this notification
    # check needs to exercise. Set the status directly instead of chaining
    # advance_status() calls that would raise on the undefined transition.
    stale_time = timezone.now() - datetime.timedelta(days=5)
    Fertilizer.objects.filter(id=plan.id).update(status=PlanStatus.ON_PROGRESS_WAIT, updated_at=stale_time)

    check_approvals_pending()
    assert Notification.objects.filter(user=manager, category=NotificationCategory.APPROVAL_PENDING).exists()


# --- event-driven processing-failure notification -------------------------

@pytest.mark.django_db
def test_processing_failure_notifies_triggering_user(manager, block_a):
    image = Image.objects.create(farm=block_a.farm, uploaded_by=manager, raw_file="imagery/raw/does-not-exist.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    run_processing(run, detector=lambda *a: [], classifier=lambda c: ("healthy", 0.9))
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_FAILED
    assert Notification.objects.filter(user=manager, category=NotificationCategory.PROCESSING_FAILURE).exists()


# --- API: notifications list/mark-read, preferences ------------------------

@pytest.mark.django_db
def test_notification_list_scoped_to_requesting_user(as_manager, manager, pic_block):
    notify(manager, NotificationCategory.APPROVAL_PENDING, "For manager", "Body")
    notify(pic_block, NotificationCategory.APPROVAL_PENDING, "For pic block", "Body")

    response = as_manager.get("/api/notifications/")
    assert response.status_code == status.HTTP_200_OK
    titles = [row["title"] for row in response.data["results"]]
    assert "For manager" in titles
    assert "For pic block" not in titles


@pytest.mark.django_db
def test_mark_notification_read_via_patch(as_manager, manager):
    notification = notify(manager, NotificationCategory.APPROVAL_PENDING, "Title", "Body")
    response = as_manager.patch(f"/api/notifications/{notification.id}/", {"read": True}, format="json")
    assert response.status_code == status.HTTP_200_OK
    notification.refresh_from_db()
    assert notification.read is True


@pytest.mark.django_db
def test_notification_preferences_get_shows_defaults_when_no_row_exists(as_manager):
    response = as_manager.get("/api/notification-preferences/")
    assert response.status_code == status.HTTP_200_OK
    row = next(r for r in response.data if r["category"] == NotificationCategory.APPROVAL_PENDING)
    assert row["in_app_enabled"] is True
    assert row["email_enabled"] is True


@pytest.mark.django_db
def test_notification_preferences_patch_upserts(as_manager, manager):
    response = as_manager.patch(
        "/api/notification-preferences/",
        [{"category": NotificationCategory.APPROVAL_PENDING, "email_enabled": False}],
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    pref = NotificationPreference.objects.get(user=manager, category=NotificationCategory.APPROVAL_PENDING)
    assert pref.email_enabled is False
    assert pref.in_app_enabled is True  # untouched field keeps its default
