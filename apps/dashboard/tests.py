"""
Office dashboard tests — session-auth Django views, not the JWT API, so
these use django.test.Client (via the `client` fixture pytest-django
provides) and real .login(username=, password=) rather than the
authed_client()/as_* JWT fixtures conftest.py defines for the API tests.

docs/02-build-plan.md's Phase 8 verification gate framing ("a fresh
developer ... ends up with a fully working local instance") is the bar
this aims at for the dashboard specifically: can a Manager sign in, see
correctly-scoped data, and can a PIC Block user (who has no dashboard
journey per docs/10-user-journeys.md) not get in at all.
"""
import pathlib
import numpy as np
import pytest
import rasterio
from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.files import File
from django.urls import reverse
from rasterio.transform import from_bounds

from apps.compliance.models import (
    AuditResult,
    ChecklistItem,
    ComplianceAudit,
    ComplianceAuditTemplate,
    ComplianceDocument,
    DeforestationBaseline,
    GrievanceRecord,
    HarvestBatch,
)
from apps.farms.models import Farm, FarmBlock
from apps.imagery.models import Image, ImageProcessingRun, TreeDetection
from apps.notifications.models import Notification, NotificationCategory
from apps.operations.models import Fertilizer, Harvest
from apps.reporting.models import ReportDefinition, ReportRun
from apps.trees.models import Tree

# Same synthetic-raster technique as apps/imagery/tests.py (see that file's
# module docstring for why: real rasterio I/O, not mocked, without needing
# real drone imagery). Duplicated rather than imported across test modules
# to keep each app's test suite self-contained.
RASTER_BOUNDS_4326 = (101.4390, 0.5290, 101.4460, 0.5360)


def _make_image(tmp_path, farm, uploader, bounds=RASTER_BOUNDS_4326, filename="ortho.tif"):
    path = tmp_path / filename
    left, bottom, right, top = bounds
    transform = from_bounds(left, bottom, right, top, 100, 100)
    data = np.random.default_rng(42).integers(0, 255, size=(3, 100, 100), dtype="uint8")
    with rasterio.open(
        path, "w", driver="GTiff", height=100, width=100, count=3, dtype="uint8", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(data)
    image = Image(farm=farm, uploaded_by=uploader)
    with open(path, "rb") as f:
        image.raw_file.save(filename, File(f), save=True)
    return image


@pytest.mark.django_db
def test_manager_can_log_in_and_reach_overview(client, manager, farm_role_manager):
    logged_in = client.login(username="manager", password="manager12345")
    assert logged_in
    response = client.get(reverse("dashboard:home"))
    assert response.status_code == 200
    assert b"Overview" in response.content


@pytest.mark.django_db
def test_super_admin_can_log_in_and_reach_overview(client, super_admin):
    logged_in = client.login(username="admin", password="admin12345")
    assert logged_in
    response = client.get(reverse("dashboard:home"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_pic_block_only_user_is_refused_at_login(client, pic_block, block_a):
    """block_a's assigned PIC Block user (per conftest.py) has no
    UserFarmRole(role=manager) row and isn't a superuser — this dashboard
    is Manager/Super-Admin only (docs/10-user-journeys.md has no PIC Block
    dashboard journey), so login itself should be refused, not merely a
    later 403.

    This must go through an actual POST to the login view, not the
    Client.login() test shortcut — that shortcut calls Django's auth
    backend directly and never touches DashboardAuthenticationForm, so it
    can't see confirm_login_allowed()'s check at all (confirmed by running
    this against live Postgres: Client.login() returned True for this same
    user, which looked like a real bug until tracing it to the shortcut
    bypassing the form rather than the view's actual login path being
    broken — the POST-based assertions below are what actually exercises
    the code this test is meant to guard).
    """
    response = client.post(
        reverse("dashboard:login"), {"username": "pic_block", "password": "picblock12345"}
    )
    assert response.status_code == 200  # re-rendered login form, not a redirect
    assert b"Managers and Super Admins" in response.content

    # And confirm the session genuinely never got established — not just
    # that the response looked like a rejection.
    assert not response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_unauthenticated_request_redirects_to_login(client):
    response = client.get(reverse("dashboard:home"))
    assert response.status_code == 302
    assert reverse("dashboard:login") in response.url


@pytest.mark.django_db
def test_overview_counts_pending_harvest_approval(client, manager, farm_role_manager, block_a):
    Harvest.objects.create(farm_block=block_a, assessment_date="2026-08-01", assessment_quantity=100.0)
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:home"))
    assert response.context["pending_harvests"] == 1
    assert response.context["pending_approvals_total"] == 1


@pytest.mark.django_db
def test_overview_counts_compliance_documents_needing_attention(client, manager, farm_role_manager, farm):
    from datetime import date, timedelta

    from django.core.files.base import ContentFile

    ComplianceDocument.objects.create(
        farm=farm,
        document_type=ComplianceDocument.TYPE_ISPO_CERT,
        document_file=ContentFile(b"dummy", name="cert.pdf"),
        expiry_date=date.today() + timedelta(days=10),  # within the default 90-day warning window
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:home"))
    assert response.context["compliance_attention_count"] == 1


@pytest.mark.django_db
def test_overview_counts_only_this_managers_unread_notifications(client, manager, farm_role_manager, other_pic_block):
    Notification.objects.create(
        user=manager, category=NotificationCategory.APPROVAL_PENDING, title="A", message="A", read=False,
    )
    Notification.objects.create(
        user=other_pic_block, category=NotificationCategory.APPROVAL_PENDING, title="B", message="B", read=False,
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:home"))
    assert response.context["unread_notifications"] == 1


@pytest.mark.django_db
def test_farms_view_shows_only_scoped_blocks(client, manager, farm_role_manager, block_a, block_b):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    assert response.status_code == 200
    assert b"Block A" in response.content
    assert b"Block B" in response.content  # same farm the manager manages — both in scope


@pytest.mark.django_db
def test_notifications_view_shows_only_the_logged_in_users_notifications(client, manager, farm_role_manager, other_pic_block):
    Notification.objects.create(
        user=manager, category=NotificationCategory.APPROVAL_PENDING, title="Mine", message="Mine", read=False,
    )
    Notification.objects.create(
        user=other_pic_block, category=NotificationCategory.APPROVAL_PENDING, title="Not mine", message="Not mine", read=False,
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:notifications"))
    assert b"Mine" in response.content
    assert b"Not mine" not in response.content


# --- Imagery: upload, block detail, review queue ---

@pytest.mark.django_db
def test_upload_form_farm_choices_restricted_to_managed_farms(client, manager, farm_role_manager, farm):
    other_farm = Farm.objects.create(name="A farm this manager doesn't manage")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery"))
    farm_ids_offered = {str(f.id) for f in response.context["form"].fields["farm"].queryset}
    assert str(farm.id) in farm_ids_offered
    assert str(other_farm.id) not in farm_ids_offered


@pytest.mark.django_db
def test_manager_can_upload_image_and_it_georeferences_synchronously(client, manager, farm_role_manager, farm, tmp_path):
    # CELERY_TASK_ALWAYS_EAGER=True in settings.test means the georeference
    # task actually runs inline here — a real rasterio-written GeoTIFF is
    # required for it to succeed, same reasoning as apps/imagery/tests.py.
    path = tmp_path / "ortho.tif"
    left, bottom, right, top = RASTER_BOUNDS_4326
    transform = from_bounds(left, bottom, right, top, 100, 100)
    data = np.random.default_rng(1).integers(0, 255, size=(3, 100, 100), dtype="uint8")
    with rasterio.open(
        path, "w", driver="GTiff", height=100, width=100, count=3, dtype="uint8", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(data)

    client.login(username="manager", password="manager12345")
    with open(path, "rb") as f:
        response = client.post(
            reverse("dashboard:imagery"), {"farm": str(farm.id), "raw_file": f}, format="multipart"
        )
    assert response.status_code == 302

    image = Image.objects.get(farm=farm)
    assert image.uploaded_by_id == manager.id
    assert image.georeference_status == Image.GEOREFERENCE_DONE


@pytest.mark.django_db
def test_block_imagery_view_lists_overlapping_images_and_runs(client, manager, farm_role_manager, block_a, tmp_path):
    image = _make_image(tmp_path, block_a.farm, manager)
    from apps.imagery.services import georeference_image

    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_DONE  # sanity check on the fixture itself

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery-block", kwargs={"block_id": block_a.id}))
    assert response.status_code == 200
    assert image in response.context["overlapping_images"]


@pytest.mark.django_db
def test_manager_cannot_view_a_block_on_a_farm_they_do_not_manage(client, manager, farm_role_manager, other_pic_block, farm):
    from apps.farms.models import FarmBlock

    unrelated_farm = Farm.objects.create(name="Unrelated farm")
    unrelated_block = FarmBlock.objects.create(farm=unrelated_farm, name="Unrelated block")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery-block", kwargs={"block_id": unrelated_block.id}))
    assert response.status_code == 404  # not in this manager's scoped FarmBlockViewSet queryset at all


@pytest.mark.django_db
def test_trigger_processing_creates_run_and_redirects_to_review(client, manager, farm_role_manager, block_a, tmp_path):
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    image.refresh_from_db()

    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:imagery-process", kwargs={"block_id": block_a.id, "image_id": image.id})
    )
    run = ImageProcessingRun.objects.get(image=image, farm_block=block_a)
    assert response.status_code == 302
    assert response.url == reverse("dashboard:imagery-run", kwargs={"run_id": run.id})
    assert run.triggered_by_id == manager.id
    # CELERY_TASK_ALWAYS_EAGER means run_processing() already ran by the
    # time the response comes back — status has moved off "pending" even
    # with the stub detector (no DETECTION_MODEL_PATH configured in tests).
    run.refresh_from_db()
    assert run.status == ImageProcessingRun.STATUS_READY


@pytest.mark.django_db
def test_image_delete_get_shows_confirm_page(client, manager, farm_role_manager, farm, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery-delete", kwargs={"image_id": image.id}))
    assert response.status_code == 200
    assert response.context["image"].id == image.id
    assert response.context["compliance_exempt"] is False


@pytest.mark.django_db
def test_image_delete_post_deletes_and_redirects_to_list(client, manager, farm_role_manager, farm, tmp_path):
    image = _make_image(tmp_path, farm, manager)
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:imagery-delete", kwargs={"image_id": image.id}))
    assert response.status_code == 302
    assert response.url == reverse("dashboard:imagery")
    assert not Image.objects.filter(id=image.id).exists()


@pytest.mark.django_db
def test_image_delete_post_refused_when_compliance_exempt(client, manager, farm_role_manager, block_a, tmp_path):
    image = _make_image(tmp_path, block_a.farm, manager)
    DeforestationBaseline.objects.create(farm_block=block_a, evidence_image=image)
    client.login(username="manager", password="manager12345")

    response = client.post(reverse("dashboard:imagery-delete", kwargs={"image_id": image.id}))

    assert response.status_code == 302
    assert response.url == reverse("dashboard:imagery-delete", kwargs={"image_id": image.id})
    assert Image.objects.filter(id=image.id).exists()  # refused, not deleted


@pytest.mark.django_db
def test_image_delete_requires_manager_of_that_farm(client, pic_block, farm, tmp_path):
    """A PIC Block user (not a Manager/Super Admin at all — this dashboard
    has no PIC Block journey) must be refused, same as every other
    Manager-only dashboard action — ManagerOrSuperAdminRequiredMixin raises
    PermissionDenied (403) for an authenticated session with the wrong
    role, not a redirect.
    """
    image = _make_image(tmp_path, farm, pic_block)
    client.login(username="pic_block", password="picblock12345")
    response = client.get(reverse("dashboard:imagery-delete", kwargs={"image_id": image.id}))
    assert response.status_code == 403
    assert Image.objects.filter(id=image.id).exists()


@pytest.mark.django_db
def test_imagery_confirm_blocks_ajax_post_success(client, manager, farm_role_manager, block_a, tmp_path):
    """ImageConfirmBlocksView's AJAX branch — added so this step (previously
    a real `<form method="post">` submit on farm_map.html, causing a full
    page navigation) can be confirmed via fetch() instead. Mirrors the
    non-AJAX assertions this view already had (a run gets created per
    selected block), just checking the JSON contract instead of the redirect.
    """
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_DONE

    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:imagery-confirm-blocks", kwargs={"image_id": image.id}),
        {"block_id": [str(block_a.id)]},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "farm", "id": str(block_a.farm_id)}
    assert "farm_map_data" in payload
    assert ImageProcessingRun.objects.filter(image=image, farm_block=block_a).exists()


@pytest.mark.django_db
def test_imagery_confirm_blocks_ajax_post_no_blocks_selected_returns_error_json(client, manager, farm_role_manager, block_a, tmp_path):
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    image.refresh_from_db()

    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:imagery-confirm-blocks", kwargs={"image_id": image.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "message" in payload
    assert not ImageProcessingRun.objects.filter(image=image).exists()


@pytest.mark.django_db
def test_farm_map_ajax_get_polls_upload_image_status(client, manager, farm_role_manager, block_a, tmp_path):
    """FarmMapView.get()'s AJAX branch — added specifically for the in-page
    imagery upload flow's own poll (farm_map.html's pollUploadStatus()),
    called every few seconds after an in-page chunked upload finishes,
    entirely without a page reload. Once georeferencing is done, the same
    upload_confirm payload showUploadConfirm() renders should be present.
    """
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    image.refresh_from_db()
    assert image.georeference_status == Image.GEOREFERENCE_DONE

    client.login(username="manager", password="manager12345")
    response = client.get(
        reverse("dashboard:farms") + f"?upload_image={image.id}",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["upload_confirm_image"]["id"] == str(image.id)
    assert payload["upload_confirm_image"]["georeference_status"] == Image.GEOREFERENCE_DONE
    assert payload["farm_map_data"]["upload_confirm"]["image"]["id"] == str(image.id)
    suggested_ids = {b["id"] for b in payload["farm_map_data"]["upload_confirm"]["blocks"] if b["suggested"]}
    assert str(block_a.id) in suggested_ids


@pytest.mark.django_db
def test_farm_map_ajax_get_without_upload_image_param_omits_upload_confirm(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert response.status_code == 200
    payload = response.json()
    assert payload["upload_confirm_image"] is None
    assert "upload_confirm" not in payload["farm_map_data"]


@pytest.mark.django_db
def test_review_queue_shows_only_new_candidate_and_ambiguous_detections(client, manager, farm_role_manager, block_a, tmp_path):
    image = _make_image(tmp_path, block_a.farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    new_candidate = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4411, 0.5311, srid=4326),
        detection_confidence=0.95, match_status=TreeDetection.MATCH_MATCHED,
    )

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery-run", kwargs={"run_id": run.id}))
    shown_ids = {d.id for d in response.context["detections"]}
    assert new_candidate.id in shown_ids
    assert len(shown_ids) == 1


@pytest.mark.django_db
def test_confirm_detection_creates_tree_and_leaves_the_queue(client, manager, farm_role_manager, block_a, tmp_path):
    """apps/imagery/services.py's confirm_new_candidate() deliberately only
    sets `.tree` — match_status stays "new_candidate" (it's a historical
    record of the pipeline's own automatic-match outcome, not a "handled"
    flag; see that function's docstring). This test originally asserted
    match_status flips to "matched", which was simply wrong — confirmed
    against the existing, already-passing
    apps/imagery/tests.py::test_confirm_reject_link_api_actions, which only
    ever asserts a Tree gets created on confirm, never a status change.
    What actually matters for the dashboard is that a *confirmed* detection
    stops showing up in the review queue — asserted directly here, since
    that's the real behavior a Manager would notice (and the thing that was
    actually broken before ReviewQueueView added tree__isnull=True to its
    filter).
    """
    image = _make_image(tmp_path, block_a.farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:imagery-detection-confirm", kwargs={"detection_id": detection.id}))
    assert response.status_code == 302
    detection.refresh_from_db()
    assert detection.match_status == TreeDetection.MATCH_NEW_CANDIDATE  # unchanged by design, not a bug
    assert detection.tree_id is not None
    assert detection.tree.source == detection.tree.SOURCE_DRONE_DETECTED

    queue_response = client.get(reverse("dashboard:imagery-run", kwargs={"run_id": run.id}))
    shown_ids = {d.id for d in queue_response.context["detections"]}
    assert detection.id not in shown_ids


@pytest.mark.django_db
def test_review_queue_link_candidates_are_the_nearest_few_not_every_tree(
    client, manager, farm_role_manager, block_a, pic_block, tmp_path
):
    """The "Link to existing tree" dropdown used to render every active
    tree in the block inside every detection card — (trees x detections)
    <option> elements, and a list nobody could pick correctly out of. It
    now offers only the nearest REVIEW_LINK_CANDIDATE_COUNT trees to each
    specific detection.
    """
    from apps.dashboard.views import REVIEW_LINK_CANDIDATE_COUNT

    image = _make_image(tmp_path, block_a.farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_AMBIGUOUS,
    )

    # More trees than the cap, at increasing distance from the detection,
    # so both "is it capped" and "is it actually sorted by nearness" are
    # real assertions rather than incidentally-true ones.
    trees = [
        Tree.objects.create(
            farm_block=block_a, name=f"T{i}",
            location=Point(101.4410 + (i * 0.0001), 0.5310, srid=4326),
            created_by=pic_block, updated_by=pic_block,
        )
        for i in range(REVIEW_LINK_CANDIDATE_COUNT + 4)
    ]

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:imagery-run", kwargs={"run_id": run.id}))
    shown = [d for d in response.context["detections"] if d.id == detection.id][0]

    assert len(shown.link_candidates) == REVIEW_LINK_CANDIDATE_COUNT
    # T0 is exactly on the detection, T1 next-nearest, and so on.
    assert [t.id for t in shown.link_candidates] == [t.id for t in trees[:REVIEW_LINK_CANDIDATE_COUNT]]


@pytest.mark.django_db
def test_reject_detection_sets_rejected_status(client, manager, farm_role_manager, block_a, tmp_path):
    image = _make_image(tmp_path, block_a.farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.4, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    client.login(username="manager", password="manager12345")
    client.post(reverse("dashboard:imagery-detection-reject", kwargs={"detection_id": detection.id}))
    detection.refresh_from_db()
    assert detection.match_status == TreeDetection.MATCH_REJECTED


@pytest.mark.django_db
def test_link_detection_to_existing_tree(client, manager, farm_role_manager, block_a, tree_in_block_a, tmp_path):
    image = _make_image(tmp_path, block_a.farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_AMBIGUOUS,
    )
    client.login(username="manager", password="manager12345")
    client.post(
        reverse("dashboard:imagery-detection-link", kwargs={"detection_id": detection.id}),
        {"tree_id": str(tree_in_block_a.id)},
    )
    detection.refresh_from_db()
    assert detection.match_status == TreeDetection.MATCH_MATCHED
    assert detection.tree_id == tree_in_block_a.id


@pytest.mark.django_db
def test_unrelated_manager_cannot_act_on_a_detection_outside_their_scope(client, manager, farm_role_manager, tmp_path):
    other_farm = Farm.objects.create(name="Someone else's farm")
    from apps.farms.models import FarmBlock

    other_block = FarmBlock.objects.create(farm=other_farm, name="Other block")
    image = _make_image(tmp_path, other_farm, manager)
    run = ImageProcessingRun.objects.create(image=image, farm_block=other_block)
    detection = TreeDetection.objects.create(
        processing_run=run, detected_location=Point(101.4410, 0.5310, srid=4326),
        detection_confidence=0.9, match_status=TreeDetection.MATCH_NEW_CANDIDATE,
    )
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:imagery-detection-confirm", kwargs={"detection_id": detection.id}))
    assert response.status_code == 403
    detection.refresh_from_db()
    assert detection.match_status == TreeDetection.MATCH_NEW_CANDIDATE  # unchanged


# --- Chunked/resumable upload (apps/imagery/models.py's ImageUploadSession) ---

def _synthetic_tif_bytes(tmp_path, bounds=RASTER_BOUNDS_4326, filename="chunked.tif"):
    """Same real-rasterio-I/O technique as _make_image(), but returns raw
    bytes instead of an already-saved Image — the chunked-upload tests need
    to slice a real file's bytes into chunks and POST them, not start from
    an already-persisted model instance.
    """
    path = tmp_path / filename
    left, bottom, right, top = bounds
    transform = from_bounds(left, bottom, right, top, 100, 100)
    data = np.random.default_rng(7).integers(0, 255, size=(3, 100, 100), dtype="uint8")
    with rasterio.open(
        path, "w", driver="GTiff", height=100, width=100, count=3, dtype="uint8", crs="EPSG:4326", transform=transform
    ) as dst:
        dst.write(data)
    return path.read_bytes()


def _upload_in_chunks(client, farm_id, file_bytes, chunk_size, filename="chunked.tif"):
    """Test helper mirroring imagery_list.html's JS orchestration: create a
    session, POST every chunk, then complete. Returns the raw responses so
    each test can assert on whichever step it cares about.
    """
    import math

    total_chunks = max(1, math.ceil(len(file_bytes) / chunk_size))
    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm_id), "filename": filename, "total_chunks": total_chunks},
        content_type="application/json",
    )
    if create_response.status_code != 200:
        return create_response, None, None
    session_id = create_response.json()["session_id"]

    chunk_responses = []
    for i in range(total_chunks):
        start = i * chunk_size
        chunk = file_bytes[start:start + chunk_size]
        chunk_responses.append(
            client.post(
                reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": i}),
                data=chunk,
                content_type="application/octet-stream",
            )
        )
    complete_response = client.post(reverse("dashboard:imagery-upload-complete", kwargs={"session_id": session_id}))
    return create_response, chunk_responses, complete_response


@pytest.mark.django_db
def test_chunked_upload_end_to_end_creates_image_and_georeferences(client, manager, farm_role_manager, farm, tmp_path):
    file_bytes = _synthetic_tif_bytes(tmp_path)
    client.login(username="manager", password="manager12345")

    # Small chunk size relative to the fixture file, so this genuinely
    # exercises multiple chunks, not just one — the fixture GeoTIFF is a
    # few KB, so 2KB chunks give several real round trips.
    create_response, chunk_responses, complete_response = _upload_in_chunks(client, farm.id, file_bytes, chunk_size=2048)

    assert create_response.status_code == 200
    assert all(r.status_code == 200 for r in chunk_responses)
    assert complete_response.status_code == 200

    image = Image.objects.get(id=complete_response.json()["image_id"])
    assert image.farm_id == farm.id
    assert image.uploaded_by_id == manager.id

    # CELERY_TASK_ALWAYS_EAGER means georeference_image_task already ran —
    # same real GeoTIFF fixture georeference_image() successfully reads
    # elsewhere in this suite, so this should genuinely succeed, not just
    # "not crash."
    assert image.georeference_status == Image.GEOREFERENCE_DONE

    # convert_to_cog() (the first step in georeference_image_task, see
    # apps/imagery/services.py) re-encodes raw_file in place — so by this
    # point it is deliberately NOT byte-identical to the uploaded bytes
    # anymore (this test used to assert exact byte equality here; that
    # assertion broke, correctly, the moment COG conversion was wired in).
    # What still must hold is that reassembly preserved the actual raster
    # *content* — same pixel data, same georeferencing — through both the
    # chunk-reassembly step and the COG re-encode. Compared via rasterio,
    # not raw bytes, since COG re-encoding legitimately changes the byte
    # layout (tiling, embedded overviews, GDAL structural metadata) while
    # preserving what the file actually represents.
    assert image.is_cog is True
    with rasterio.io.MemoryFile(file_bytes) as original_memfile, original_memfile.open() as original:
        with rasterio.open(image.raw_file.path) as reencoded:
            assert reencoded.crs == original.crs
            assert reencoded.bounds == pytest.approx(tuple(original.bounds), abs=1e-9)
            assert (reencoded.read() == original.read()).all()

    # Byte-exact chunk reassembly itself (independent of COG conversion)
    # has its own dedicated, isolated test — see apps/imagery/tests.py's
    # test_finalize_upload_session_reassembles_chunks_byte_exact.


@pytest.mark.django_db
def test_chunked_upload_single_chunk_when_file_smaller_than_chunk_size(client, manager, farm_role_manager, farm, tmp_path):
    file_bytes = _synthetic_tif_bytes(tmp_path)
    client.login(username="manager", password="manager12345")
    create_response, chunk_responses, complete_response = _upload_in_chunks(
        client, farm.id, file_bytes, chunk_size=len(file_bytes) * 10
    )
    assert len(chunk_responses) == 1
    assert complete_response.status_code == 200


@pytest.mark.django_db
def test_chunk_at_real_configured_chunk_size_does_not_400(client, manager, farm_role_manager, farm):
    """Regression test — reported live: 'Chunk 1 of 142 failed (status
    400)'. Root cause: UploadChunkView reads the chunk via request.body
    (imagery_list.html's JS sends each chunk as a raw, non-multipart body),
    and Django's *default* DATA_UPLOAD_MAX_MEMORY_SIZE (2.5MB) is smaller
    than settings.IMAGE_UPLOAD_CHUNK_SIZE_BYTES (8MB) — accessing
    request.body on an oversized request raises RequestDataTooBig before
    the view even runs, which Django turns into an unconditional 400. Every
    prior chunked-upload test used tiny (2KB-to-a-few-KB) test chunk sizes,
    nowhere near either the old 2.5MB Django default or the real 8MB
    configured chunk size — so none of them could have caught this. This
    test deliberately posts a chunk at the *actual configured* chunk size
    (not a token-sized stand-in) to prove the real-world size succeeds; the
    chunk's content doesn't need to be valid raster data for this — only
    the request-size handling is under test here, not reassembly/
    georeferencing (already covered by the end-to-end test above).
    """
    from django.conf import settings

    client.login(username="manager", password="manager12345")
    real_chunk_size = settings.IMAGE_UPLOAD_CHUNK_SIZE_BYTES
    assert real_chunk_size > 2 * 1024 * 1024  # sanity: this test is meaningless if that ever shrinks below the old Django default

    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm.id), "filename": "big.tif", "total_chunks": 1},
        content_type="application/json",
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["session_id"]

    chunk = b"x" * real_chunk_size
    chunk_response = client.post(
        reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": 0}),
        data=chunk,
        content_type="application/octet-stream",
    )
    assert chunk_response.status_code == 200


@pytest.mark.django_db
def test_create_upload_session_scoped_to_managed_farms(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="A farm this manager doesn't manage")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(other_farm.id), "filename": "x.tif", "total_chunks": 1},
        content_type="application/json",
    )
    assert response.status_code == 404  # not in this manager's scoped FarmViewSet queryset at all


@pytest.mark.django_db
def test_complete_upload_refused_when_chunks_missing(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm.id), "filename": "x.tif", "total_chunks": 3},
        content_type="application/json",
    )
    session_id = create_response.json()["session_id"]

    # Only send chunk 0 of 3 — 1 and 2 are still missing.
    client.post(
        reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": 0}),
        data=b"first-chunk-bytes",
        content_type="application/octet-stream",
    )
    complete_response = client.post(reverse("dashboard:imagery-upload-complete", kwargs={"session_id": session_id}))
    assert complete_response.status_code == 400
    assert not Image.objects.exists()


@pytest.mark.django_db
def test_chunk_index_out_of_range_refused(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm.id), "filename": "x.tif", "total_chunks": 1},
        content_type="application/json",
    )
    session_id = create_response.json()["session_id"]
    response = client.post(
        reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": 5}),
        data=b"bytes",
        content_type="application/octet-stream",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_upload_session_status_reports_received_chunks(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm.id), "filename": "x.tif", "total_chunks": 2},
        content_type="application/json",
    )
    session_id = create_response.json()["session_id"]
    client.post(
        reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": 0}),
        data=b"chunk-zero", content_type="application/octet-stream",
    )
    status_response = client.get(reverse("dashboard:imagery-upload-status", kwargs={"session_id": session_id}))
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["received_chunk_indexes"] == [0]
    assert body["total_chunks"] == 2
    assert body["status"] == "uploading"


@pytest.mark.django_db
def test_re_sending_the_same_chunk_does_not_duplicate_it(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    create_response = client.post(
        reverse("dashboard:imagery-upload-create"),
        data={"farm": str(farm.id), "filename": "x.tif", "total_chunks": 1},
        content_type="application/json",
    )
    session_id = create_response.json()["session_id"]
    url = reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": session_id, "chunk_index": 0})
    client.post(url, data=b"first-attempt", content_type="application/octet-stream")
    second = client.post(url, data=b"retried-attempt-different-bytes", content_type="application/octet-stream")
    assert second.json()["received_chunk_indexes"] == [0]  # still just one entry, not two


@pytest.mark.django_db
def test_unrelated_manager_cannot_touch_a_chunked_upload_session_outside_their_scope(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    from apps.imagery.models import ImageUploadSession

    other_session = ImageUploadSession.objects.create(farm=other_farm, filename="x.tif", total_chunks=1)
    client.login(username="manager", password="manager12345")

    chunk_response = client.post(
        reverse("dashboard:imagery-upload-chunk", kwargs={"session_id": other_session.id, "chunk_index": 0}),
        data=b"bytes", content_type="application/octet-stream",
    )
    assert chunk_response.status_code == 403
    complete_response = client.post(
        reverse("dashboard:imagery-upload-complete", kwargs={"session_id": other_session.id})
    )
    assert complete_response.status_code == 403
    status_response = client.get(
        reverse("dashboard:imagery-upload-status", kwargs={"session_id": other_session.id})
    )
    assert status_response.status_code == 403


# --- Compliance: document filing + due-diligence export ---

@pytest.mark.django_db
def test_compliance_document_form_farm_choices_restricted_to_managed_farms(client, manager, farm_role_manager, farm):
    other_farm = Farm.objects.create(name="A farm this manager doesn't manage")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-documents"))
    farm_ids_offered = {str(f.id) for f in response.context["form"].fields["farm"].queryset}
    assert str(farm.id) in farm_ids_offered
    assert str(other_farm.id) not in farm_ids_offered


@pytest.mark.django_db
def test_manager_can_upload_compliance_document_attached_to_farm(client, manager, farm_role_manager, farm):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-documents"),
        {
            "farm": str(farm.id),
            "document_type": ComplianceDocument.TYPE_ISPO_CERT,
            "document_file": SimpleUploadedFile("cert.pdf", b"dummy", content_type="application/pdf"),
        },
    )
    assert response.status_code == 302
    doc = ComplianceDocument.objects.get(farm=farm)
    assert doc.document_type == ComplianceDocument.TYPE_ISPO_CERT


@pytest.mark.django_db
def test_manager_cannot_upload_compliance_document_for_a_farm_they_do_not_manage(client, manager, farm_role_manager):
    """The form's own queryset (ComplianceDocumentForm.__init__) already
    excludes farms this manager doesn't manage, so posting one anyway is a
    normal "invalid choice" form-validation error (200, re-rendered form),
    not a 500 or a silently created row. _require_manager_for_farm() in the
    view is the real server-side backstop for a request that bypassed the
    widget entirely — same belt-and-suspenders reasoning as
    ImageUploadForm/ImageListUploadView.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    other_farm = Farm.objects.create(name="Someone else's farm")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-documents"),
        {
            "farm": str(other_farm.id),
            "document_type": ComplianceDocument.TYPE_ISPO_CERT,
            "document_file": SimpleUploadedFile("cert.pdf", b"dummy", content_type="application/pdf"),
        },
    )
    assert response.status_code == 200
    assert not ComplianceDocument.objects.filter(farm=other_farm).exists()


@pytest.mark.django_db
def test_compliance_documents_view_scoped_to_managed_farms_and_blocks(client, manager, farm_role_manager, farm, block_a):
    from django.core.files.base import ContentFile

    mine = ComplianceDocument.objects.create(
        farm_block=block_a, document_type=ComplianceDocument.TYPE_LAND_TITLE,
        document_file=ContentFile(b"dummy", name="title.pdf"),
    )
    other_farm = Farm.objects.create(name="Unrelated farm")
    not_mine = ComplianceDocument.objects.create(
        farm=other_farm, document_type=ComplianceDocument.TYPE_LAND_TITLE,
        document_file=ContentFile(b"dummy", name="title2.pdf"),
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-documents"))
    shown_ids = {d.id for d in response.context["documents"]}
    assert mine.id in shown_ids
    assert not_mine.id not in shown_ids


@pytest.fixture
def harvest_batch(db, block_a):
    return HarvestBatch.objects.create(farm_block=block_a, period_start="2026-07-01", period_end="2026-07-31")


@pytest.mark.django_db
def test_harvest_batches_view_lists_scoped_batches(client, manager, farm_role_manager, harvest_batch):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-batches"))
    assert response.status_code == 200
    assert harvest_batch.id in {b.id for b in response.context["batches"]}


@pytest.mark.django_db
def test_export_due_diligence_from_dashboard_shows_package_and_marks_exported(client, manager, farm_role_manager, harvest_batch, block_a):
    DeforestationBaseline.objects.create(
        farm_block=block_a, source="Satellite provider X", status=DeforestationBaseline.STATUS_DEFORESTATION_FREE,
    )
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:compliance-batches"), {"batch_id": str(harvest_batch.id)})
    assert response.status_code == 200
    result = response.context["export_result"]
    assert result is not None
    assert result["deforestation_baseline"]["status"] == DeforestationBaseline.STATUS_DEFORESTATION_FREE
    assert b"deforestation_free" in response.content

    harvest_batch.refresh_from_db()
    assert harvest_batch.export_status == HarvestBatch.EXPORT_STATUS_EXPORTED


@pytest.mark.django_db
def test_export_due_diligence_refused_when_block_has_no_boundary_shows_error(client, manager, farm_role_manager, farm):
    block_no_boundary = FarmBlock.objects.create(farm=farm, name="Unsurveyed block")
    batch = HarvestBatch.objects.create(
        farm_block=block_no_boundary, period_start="2026-07-01", period_end="2026-07-31",
    )
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:compliance-batches"), {"batch_id": str(batch.id)})
    assert response.status_code == 200
    assert response.context["export_result"] is None
    assert b"no boundary polygon" in response.content

    batch.refresh_from_db()
    assert batch.export_status == HarvestBatch.EXPORT_STATUS_DRAFT  # unchanged


@pytest.mark.django_db
def test_unrelated_manager_cannot_export_a_batch_outside_their_scope(client, manager, farm_role_manager, tmp_path):
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_block = FarmBlock.objects.create(farm=other_farm, name="Other block")
    batch = HarvestBatch.objects.create(farm_block=other_block, period_start="2026-07-01", period_end="2026-07-31")
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:compliance-batches"), {"batch_id": str(batch.id)})
    assert response.status_code == 404  # not in this manager's scoped HarvestBatchViewSet queryset at all


# --- Reporting: definitions and runs ---

@pytest.mark.django_db
def test_report_definition_form_farm_choices_restricted_to_managed_farms(client, manager, farm_role_manager, farm):
    other_farm = Farm.objects.create(name="A farm this manager doesn't manage")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:reports"))
    farm_ids_offered = {str(f.id) for f in response.context["form"].fields["farm"].queryset}
    assert str(farm.id) in farm_ids_offered
    assert str(other_farm.id) not in farm_ids_offered


@pytest.mark.django_db
def test_manager_can_create_report_definition(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:reports"),
        {
            "farm": str(farm.id), "name": "Monthly production", "report_type": ReportDefinition.TYPE_PRODUCTION,
            "export_format": ReportDefinition.FORMAT_PDF, "recurrence": ReportDefinition.RECURRENCE_NONE,
        },
    )
    assert response.status_code == 302
    definition = ReportDefinition.objects.get(farm=farm)
    assert definition.created_by_id == manager.id


@pytest.mark.django_db
def test_manager_cannot_create_report_definition_for_a_farm_they_do_not_manage(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:reports"),
        {
            "farm": str(other_farm.id), "name": "Sneaky", "report_type": ReportDefinition.TYPE_PRODUCTION,
            "export_format": ReportDefinition.FORMAT_PDF, "recurrence": ReportDefinition.RECURRENCE_NONE,
        },
    )
    # The form's own queryset already excludes other_farm, so this is a normal
    # form validation error (invalid choice), not a 500 or a silently created row.
    assert response.status_code == 200
    assert not ReportDefinition.objects.filter(name="Sneaky").exists()


@pytest.fixture
def report_definition(db, farm, manager):
    return ReportDefinition.objects.create(
        farm=farm, name="Monthly production", report_type=ReportDefinition.TYPE_PRODUCTION,
        export_format=ReportDefinition.FORMAT_PDF, created_by=manager,
    )


@pytest.mark.django_db
def test_trigger_report_run_creates_run_and_redirects(client, manager, farm_role_manager, report_definition):
    client.login(username="manager", password="manager12345")
    response = client.post(reverse("dashboard:report-run", kwargs={"definition_id": report_definition.id}))
    run = ReportRun.objects.get(definition=report_definition)
    assert response.status_code == 302
    assert response.url == reverse("dashboard:report-runs", kwargs={"definition_id": report_definition.id})
    assert run.triggered_by_id == manager.id
    # CELERY_TASK_ALWAYS_EAGER means generate_report_task already ran by the
    # time the response comes back.
    run.refresh_from_db()
    assert run.status == ReportRun.STATUS_READY, run.error_message


@pytest.mark.django_db
def test_report_runs_view_lists_runs_for_that_definition_only(client, manager, farm_role_manager, report_definition, farm):
    other_definition = ReportDefinition.objects.create(
        farm=farm, name="Other report", report_type=ReportDefinition.TYPE_PRODUCTION,
        export_format=ReportDefinition.FORMAT_PDF, created_by=manager,
    )
    mine = ReportRun.objects.create(definition=report_definition, triggered_by=manager)
    ReportRun.objects.create(definition=other_definition, triggered_by=manager)

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:report-runs", kwargs={"definition_id": report_definition.id}))
    shown_ids = {r.id for r in response.context["runs"]}
    assert shown_ids == {mine.id}


@pytest.mark.django_db
def test_unrelated_manager_cannot_view_runs_for_a_definition_outside_their_scope(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_definition = ReportDefinition.objects.create(
        farm=other_farm, name="Not mine", report_type=ReportDefinition.TYPE_PRODUCTION,
        export_format=ReportDefinition.FORMAT_PDF,
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:report-runs", kwargs={"definition_id": other_definition.id}))
    assert response.status_code == 404


# --- Farm Map (FarmMapView) — the unified map + contextual management
# panel that replaced the separate Map / Farms & Blocks / block-trees
# pages (see FarmMapView's own docstring in apps/dashboard/views.py for
# the full "GIS-first" redesign reasoning). dashboard:map is now just a
# redirect to dashboard:farms (tested separately below), and
# dashboard:farms is FarmMapView itself — these tests exercise the one
# farm_map_data JSON payload every browsing feature on that page reads
# from, the same way the old Map page's tests exercised map_data. ---

@pytest.mark.django_db
def test_farm_map_shows_farms_blocks_trees_and_images_in_scope(client, manager, farm_role_manager, block_a, tree_in_block_a, tmp_path):
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    image.refresh_from_db()
    assert image.footprint is not None  # sanity check on the fixture itself

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    assert response.status_code == 200
    farm_map_data = response.context["farm_map_data"]

    farm_ids = {f["id"] for f in farm_map_data["farms"]}
    block_ids = {b["id"] for b in farm_map_data["blocks"]}
    tree_ids = {t["id"] for t in farm_map_data["trees"]}
    image_ids = {i["id"] for i in farm_map_data["images"]}
    assert str(block_a.farm_id) in farm_ids
    assert str(block_a.id) in block_ids
    assert str(tree_in_block_a.id) in tree_ids
    assert str(image.id) in image_ids

    block_entry = next(b for b in farm_map_data["blocks"] if b["id"] == str(block_a.id))
    assert block_entry["geometry"]["type"] == "Polygon"
    assert block_entry["has_boundary"] is True
    assert block_entry["farm_id"] == str(block_a.farm_id)
    assert block_entry["tree_count"] == 1

    tree_entry = next(t for t in farm_map_data["trees"] if t["id"] == str(tree_in_block_a.id))
    assert tree_entry["geometry"]["type"] == "Point"
    assert tree_entry["health"] == tree_in_block_a.current_health
    assert tree_entry["farm_id"] == str(block_a.farm_id)  # tree_in_block_a's farm, via its farm_block
    assert tree_entry["block_id"] == str(block_a.id)

    image_entry = next(i for i in farm_map_data["images"] if i["id"] == str(image.id))
    assert image_entry["farm_id"] == str(block_a.farm_id)

    # The action-URL templates every write action in the side panel is
    # built from (see FarmMapView's docstring / farm_map.html's buildUrl())
    # — a placeholder-UUID string that JS substitutes at click time.
    assert farm_map_data["url_placeholder"] in farm_map_data["urls"]["block_edit"]
    assert farm_map_data["urls"]["block_add"] == reverse("dashboard:block-add")


@pytest.mark.django_db
def test_farm_map_image_entry_carries_preview_url_once_generated(client, manager, farm_role_manager, block_a, tmp_path):
    """Regression coverage for the map showing an uploaded image as *only*
    its dashed footprint outline, never the actual picture — the fix was
    generate_preview() (apps/imagery/services.py) plus this preview_url
    field on each farm_map_data image entry (farm_map.html renders it as
    a Leaflet ImageOverlay when present).
    """
    from apps.imagery.services import generate_preview, georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)
    generate_preview(image)
    image.refresh_from_db()
    assert image.preview_file  # sanity check on the fixture itself

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    image_entry = next(i for i in response.context["farm_map_data"]["images"] if i["id"] == str(image.id))
    assert image_entry["preview_url"] == image.preview_file.url


@pytest.mark.django_db
def test_farm_map_image_entry_preview_url_is_none_before_preview_generated(client, manager, farm_role_manager, block_a, tmp_path):
    from apps.imagery.services import georeference_image

    image = _make_image(tmp_path, block_a.farm, manager)
    georeference_image(image)  # footprint set, but generate_preview() deliberately not called

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    image_entry = next(i for i in response.context["farm_map_data"]["images"] if i["id"] == str(image.id))
    assert image_entry["preview_url"] is None


@pytest.mark.django_db
def test_farm_map_farms_scoped_to_managed_farms(client, manager, farm_role_manager, farm):
    other_farm = Farm.objects.create(name="Someone else's farm")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    farm_ids = {f["id"] for f in response.context["farm_map_data"]["farms"]}
    assert str(farm.id) in farm_ids
    assert str(other_farm.id) not in farm_ids


@pytest.mark.django_db
def test_farm_map_flags_blocks_without_a_boundary_but_still_lists_them(client, manager, farm_role_manager, farm):
    """Unlike the old read-only Map page (which just left a boundary-less
    block off the map entirely), FarmMapView's side panel is also where a
    Manager manages blocks — including drawing a first boundary — so a
    block with none yet must still appear in farm_map_data (for the panel's
    block list), just flagged has_boundary=False with no geometry to plot.
    """
    block_no_boundary = FarmBlock.objects.create(farm=farm, name="Unsurveyed block")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    blocks_by_id = {b["id"]: b for b in response.context["farm_map_data"]["blocks"]}
    entry = blocks_by_id[str(block_no_boundary.id)]
    assert entry["has_boundary"] is False
    assert entry["geometry"] is None


@pytest.mark.django_db
def test_farm_map_excludes_removed_trees(client, manager, farm_role_manager, tree_in_block_a):
    tree_in_block_a.status = Tree.STATUS_REMOVED
    tree_in_block_a.save(update_fields=["status"])
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    tree_ids = {t["id"] for t in response.context["farm_map_data"]["trees"]}
    assert str(tree_in_block_a.id) not in tree_ids


@pytest.mark.django_db
def test_farm_map_only_shows_data_in_the_managers_scope(client, manager, farm_role_manager, block_a):
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_block = FarmBlock.objects.create(
        farm=other_farm, name="Other block", boundary=block_a.boundary, area=10.0,
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:farms"))
    block_ids = {b["id"] for b in response.context["farm_map_data"]["blocks"]}
    assert str(other_block.id) not in block_ids


@pytest.mark.django_db
def test_farm_map_renders_with_nothing_in_scope(client, super_admin):
    """No farms/blocks/trees/images at all is still a normal 200 — the
    "nothing here yet" messaging is rendered client-side by farm_map.html's
    own JS from an empty farm_map_data, not a server-rendered empty state
    (unlike the old Map page, which had one) — so this only asserts the
    payload itself is genuinely empty, not any particular HTML string.

    Uses `super_admin`, not `manager` — a plain Manager's dashboard access
    is itself granted BY a UserFarmRole on some real Farm (see
    ManagerOrSuperAdminRequiredMixin._require_office_staff()), so "a
    Manager with nothing in scope" is a contradiction: granting the role
    needed to reach this page at all necessarily puts at least one Farm in
    scope. Only a Super Admin can reach the dashboard without being tied to
    any farm — genuinely empty here since this test creates no Farm at all.
    Found live: the original version used `manager` + the `farm_role_manager`
    fixture, which grants exactly the UserFarmRole (and its real Farm) this
    test's own assertions say shouldn't exist — a self-contradicting test
    that was silently failing until caught by a real CI run.
    """
    client.login(username="admin", password="admin12345")
    response = client.get(reverse("dashboard:farms"))
    assert response.status_code == 200
    farm_map_data = response.context["farm_map_data"]
    assert farm_map_data["farms"] == []
    assert farm_map_data["blocks"] == []
    assert farm_map_data["trees"] == []
    assert farm_map_data["images"] == []


@pytest.mark.django_db
def test_pic_block_only_user_cannot_reach_the_farm_map(client, pic_block, block_a):
    """Client.login() bypasses DashboardAuthenticationForm.confirm_login_allowed()
    entirely (see test_pic_block_only_user_is_refused_at_login's docstring
    above), so this test's login itself succeeds — it's exercising the
    other half of the defense: ManagerOrSuperAdminRequiredMixin re-checks
    office-staff standing on every dispatch(), not just at login, and
    raises PermissionDenied (403), not a redirect, for an authenticated
    session that reached this app without going through the login form's
    own check.
    """
    client.login(username="pic_block", password="picblock12345")
    response = client.get(reverse("dashboard:farms"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_old_map_url_redirects_to_farm_map_preserving_query_string(client, manager, farm_role_manager, block_a):
    """dashboard:map (the old standalone Map page's URL) is kept as a
    redirect rather than removed outright — it may still be bookmarked —
    landing on the unified Farm Map with any ?farm=/?block= query string
    intact (RedirectView(query_string=True) in apps/dashboard/urls.py).
    """
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:map") + f"?farm={block_a.farm_id}")
    assert response.status_code == 302
    assert response.url == reverse("dashboard:farms") + f"?farm={block_a.farm_id}"


# --- Deforestation baselines, compliance audits, grievances — the four
# models BUILD_STATUS.md flagged as still missing dashboard UI
# (ComplianceAudit/ChecklistItem/DeforestationBaseline/GrievanceRecord).
# ChecklistItem has no list of its own — it's shown inline as each audit's
# per-item results (see compliance_audits.html), so there's no separate
# ChecklistItem view to test here; its behavior is exercised through the
# audit tests below. ---

@pytest.mark.django_db
def test_deforestation_baselines_view_lists_scoped_baselines(client, manager, farm_role_manager, block_a):
    mine = DeforestationBaseline.objects.create(farm_block=block_a, source="Survey A")
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_block = FarmBlock.objects.create(farm=other_farm, name="Other block")
    not_mine = DeforestationBaseline.objects.create(farm_block=other_block, source="Survey B")

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-baselines"))
    assert response.status_code == 200
    shown_ids = {b.id for b in response.context["baselines"]}
    assert mine.id in shown_ids
    assert not_mine.id not in shown_ids


@pytest.mark.django_db
def test_manager_can_verify_a_deforestation_baseline(client, manager, farm_role_manager, block_a):
    baseline = DeforestationBaseline.objects.create(farm_block=block_a, status=DeforestationBaseline.STATUS_UNDER_REVIEW)
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-baselines"),
        {"baseline_id": str(baseline.id), "status": DeforestationBaseline.STATUS_DEFORESTATION_FREE},
    )
    assert response.status_code == 302
    baseline.refresh_from_db()
    assert baseline.status == DeforestationBaseline.STATUS_DEFORESTATION_FREE
    assert baseline.verified_by_id == manager.id
    assert baseline.verified_at is not None


@pytest.mark.django_db
def test_unrelated_manager_cannot_verify_a_baseline_outside_their_scope(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_block = FarmBlock.objects.create(farm=other_farm, name="Other block")
    baseline = DeforestationBaseline.objects.create(farm_block=other_block)
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-baselines"),
        {"baseline_id": str(baseline.id), "status": DeforestationBaseline.STATUS_DEFORESTATION_FREE},
    )
    assert response.status_code == 404  # not in this manager's scoped queryset at all
    baseline.refresh_from_db()
    assert baseline.status == DeforestationBaseline.STATUS_UNDER_REVIEW  # unchanged


@pytest.fixture
def compliance_audit(db, block_a):
    template = ComplianceAuditTemplate.objects.create(name="ISPO annual checklist", certification_scheme="ISPO")
    item = ChecklistItem.objects.create(template=template, text="No burning practiced", order=1)
    audit = ComplianceAudit.objects.create(
        farm_block=block_a, template=template, audit_date="2026-06-01",
        auditor_name="Jane Auditor", auditor_type=ComplianceAudit.AUDITOR_THIRD_PARTY,
        overall_status=ComplianceAudit.STATUS_PASSED,
    )
    AuditResult.objects.create(audit=audit, checklist_item=item, result=AuditResult.RESULT_PASS, notes="Confirmed on site.")
    return audit


@pytest.mark.django_db
def test_compliance_audits_view_shows_scoped_audits_with_per_item_results(client, manager, farm_role_manager, compliance_audit):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-audits"))
    assert response.status_code == 200
    shown_ids = {a.id for a in response.context["audits"]}
    assert compliance_audit.id in shown_ids
    assert b"No burning practiced" in response.content
    assert b"Confirmed on site." in response.content


@pytest.mark.django_db
def test_compliance_audits_view_excludes_audits_outside_scope(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    other_block = FarmBlock.objects.create(farm=other_farm, name="Other block")
    template = ComplianceAuditTemplate.objects.create(name="RSPO checklist")
    other_audit = ComplianceAudit.objects.create(
        farm_block=other_block, template=template, audit_date="2026-06-01",
        auditor_name="Someone", auditor_type=ComplianceAudit.AUDITOR_INTERNAL,
        overall_status=ComplianceAudit.STATUS_PASSED,
    )
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-audits"))
    shown_ids = {a.id for a in response.context["audits"]}
    assert other_audit.id not in shown_ids


@pytest.mark.django_db
def test_grievance_form_farm_choices_restricted_to_managed_farms(client, manager, farm_role_manager, farm):
    other_farm = Farm.objects.create(name="A farm this manager doesn't manage")
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-grievances"))
    farm_ids_offered = {str(f.id) for f in response.context["form"].fields["farm"].queryset}
    assert str(farm.id) in farm_ids_offered
    assert str(other_farm.id) not in farm_ids_offered


@pytest.mark.django_db
def test_manager_can_log_a_grievance(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-grievances"),
        {"farm": str(farm.id), "date_reported": "2026-08-01", "description": "Boundary dispute reported by neighbor."},
    )
    assert response.status_code == 302
    grievance = GrievanceRecord.objects.get(farm=farm)
    assert grievance.status == GrievanceRecord.STATUS_OPEN  # model default — not settable at creation, see form docstring
    assert grievance.description == "Boundary dispute reported by neighbor."


@pytest.mark.django_db
def test_manager_cannot_log_a_grievance_for_a_farm_they_do_not_manage(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-grievances"),
        {"farm": str(other_farm.id), "date_reported": "2026-08-01", "description": "Sneaky."},
    )
    assert response.status_code == 200  # form's own queryset rejects it — invalid choice, not a 500
    assert not GrievanceRecord.objects.filter(farm=other_farm).exists()


@pytest.mark.django_db
def test_grievances_view_scoped_to_managed_farms(client, manager, farm_role_manager, farm):
    mine = GrievanceRecord.objects.create(farm=farm, date_reported="2026-08-01", description="Mine")
    other_farm = Farm.objects.create(name="Someone else's farm")
    not_mine = GrievanceRecord.objects.create(farm=other_farm, date_reported="2026-08-01", description="Not mine")

    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:compliance-grievances"))
    shown_ids = {g.id for g in response.context["grievances"]}
    assert mine.id in shown_ids
    assert not_mine.id not in shown_ids


@pytest.mark.django_db
def test_manager_can_update_grievance_status(client, manager, farm_role_manager, farm):
    grievance = GrievanceRecord.objects.create(farm=farm, date_reported="2026-08-01", description="Boundary dispute")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-grievance-update", kwargs={"grievance_id": grievance.id}),
        {"status": GrievanceRecord.STATUS_RESOLVED, "resolution_notes": "Boundary re-surveyed, resolved amicably."},
    )
    assert response.status_code == 302
    grievance.refresh_from_db()
    assert grievance.status == GrievanceRecord.STATUS_RESOLVED
    assert grievance.resolution_notes == "Boundary re-surveyed, resolved amicably."


@pytest.mark.django_db
def test_unrelated_manager_cannot_update_a_grievance_outside_their_scope(client, manager, farm_role_manager):
    other_farm = Farm.objects.create(name="Someone else's farm")
    grievance = GrievanceRecord.objects.create(farm=other_farm, date_reported="2026-08-01", description="Not this manager's")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:compliance-grievance-update", kwargs={"grievance_id": grievance.id}),
        {"status": GrievanceRecord.STATUS_RESOLVED},
    )
    assert response.status_code == 404  # not in this manager's scoped GrievanceRecordViewSet queryset at all
    grievance.refresh_from_db()
    assert grievance.status == GrievanceRecord.STATUS_OPEN  # unchanged


# --- ReportDefinition.subscribers — see forms.py's ReportDefinitionForm
# docstring for why choices are narrowed to office staff. ---

@pytest.mark.django_db
def test_report_definition_subscriber_choices_are_managers_and_superadmins_only(client, manager, farm_role_manager, farm, pic_block, super_admin):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:reports"))
    offered_ids = {u.id for u in response.context["form"].fields["subscribers"].queryset}
    assert manager.id in offered_ids
    assert super_admin.id in offered_ids
    assert pic_block.id not in offered_ids


@pytest.mark.django_db
def test_manager_can_create_report_definition_with_subscribers(client, manager, farm_role_manager, farm, super_admin):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:reports"),
        {
            "farm": str(farm.id), "name": "Monthly production", "report_type": ReportDefinition.TYPE_PRODUCTION,
            "export_format": ReportDefinition.FORMAT_PDF, "recurrence": ReportDefinition.RECURRENCE_MONTHLY,
            "subscribers": [str(manager.id), str(super_admin.id)],
        },
    )
    assert response.status_code == 302
    definition = ReportDefinition.objects.get(farm=farm)
    assert set(definition.subscribers.values_list("id", flat=True)) == {manager.id, super_admin.id}


# --- Farm Map write actions, AJAX contract — the second round of the
# GIS-first redesign. Direct feedback on the first round ("all activities
# must be in that same page by changing the panel, not redirected into
# different page") meant every one of these views had to grow a second,
# JSON-shaped response path alongside its original full-page redirect
# path — these tests exercise that JSON path specifically (the
# X-Requested-With header farm_map.html's fetch() calls always send),
# since it's genuinely new behavior this round, not just a retarget.
# The full-page fallback path (no header) is exercised by the tests
# above/elsewhere in this file already, unchanged. ---

@pytest.mark.django_db
def test_block_add_ajax_get_returns_form_fragment(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    response = client.get(reverse("dashboard:block-add"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    assert "data-panel-form" in response.json()["html"]


@pytest.mark.django_db
def test_block_add_ajax_post_success_returns_fresh_payload_and_focus(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-add"),
        {"farm": str(farm.id), "name": "New Block"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    block = FarmBlock.objects.get(name="New Block")
    assert payload["focus"] == {"type": "block", "id": str(block.id), "then": "boundary"}
    assert str(block.id) in {b["id"] for b in payload["farm_map_data"]["blocks"]}


@pytest.mark.django_db
def test_block_add_ajax_post_failure_returns_html_with_errors_not_a_redirect(client, manager, farm_role_manager, farm):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-add"),
        {"farm": str(farm.id)},  # missing required "name"
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "data-panel-form" in payload["html"]


@pytest.mark.django_db
def test_block_edit_ajax_post_success(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-edit", kwargs={"block_id": block_a.id}),
        {"farm": str(block_a.farm_id), "name": "Renamed Block"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "block", "id": str(block_a.id)}
    block_a.refresh_from_db()
    assert block_a.name == "Renamed Block"


@pytest.mark.django_db
def test_block_boundary_ajax_post_success(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    geometry = (
        '{"type":"Polygon","coordinates":[[[101.44,0.53],[101.445,0.53],'
        '[101.445,0.535],[101.44,0.535],[101.44,0.53]]]}'
    )
    response = client.post(
        reverse("dashboard:block-boundary", kwargs={"block_id": block_a.id}),
        {"geometry": geometry},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "block", "id": str(block_a.id)}
    block_entry = next(b for b in payload["farm_map_data"]["blocks"] if b["id"] == str(block_a.id))
    assert block_entry["has_boundary"] is True


@pytest.mark.django_db
def test_block_boundary_ajax_post_missing_geometry_returns_error_json_not_a_redirect(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-boundary", kwargs={"block_id": block_a.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "message" in payload


@pytest.mark.django_db
def test_block_delete_ajax_get_returns_confirmation_fragment(client, manager, farm_role_manager, farm):
    empty_block = FarmBlock.objects.create(farm=farm, name="Empty block")
    client.login(username="manager", password="manager12345")
    response = client.get(
        reverse("dashboard:block-delete", kwargs={"block_id": empty_block.id}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert "data-panel-form" in response.json()["html"]


@pytest.mark.django_db
def test_block_delete_ajax_post_success_focuses_the_farm(client, manager, farm_role_manager, farm):
    empty_block = FarmBlock.objects.create(farm=farm, name="Empty block")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-delete", kwargs={"block_id": empty_block.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "farm", "id": str(farm.id)}
    assert not FarmBlock.objects.filter(id=empty_block.id).exists()


@pytest.mark.django_db
def test_block_delete_ajax_post_refused_when_blockers_exist(client, manager, farm_role_manager, block_a, tree_in_block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-delete", kwargs={"block_id": block_a.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert FarmBlock.objects.filter(id=block_a.id).exists()


@pytest.mark.django_db
def test_tree_add_ajax_get_returns_form_fragment(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.get(
        reverse("dashboard:tree-add", kwargs={"block_id": block_a.id}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert "tree-lat" in response.json()["html"]


@pytest.mark.django_db
def test_tree_add_ajax_post_success_returns_tree_focus(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:tree-add", kwargs={"block_id": block_a.id}),
        {"farm_block": str(block_a.id), "source": "manual", "status": "active", "current_health": "healthy",
         "lat": "0.5310", "lng": "101.4410"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    tree = Tree.objects.get(farm_block=block_a, source="manual")
    assert payload["focus"] == {"type": "tree", "id": str(tree.id)}


@pytest.mark.django_db
def test_tree_add_ajax_post_without_placement_returns_error_not_a_redirect(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:tree-add", kwargs={"block_id": block_a.id}),
        {"farm_block": str(block_a.id), "source": "manual", "status": "active", "current_health": "healthy"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["message"] == "Click the map to place this tree before saving."


@pytest.mark.django_db
def test_tree_edit_ajax_get_includes_existing_point_for_map_placement(client, manager, farm_role_manager, tree_in_block_a):
    client.login(username="manager", password="manager12345")
    response = client.get(
        reverse("dashboard:tree-edit", kwargs={"tree_id": tree_in_block_a.id}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["existing_point"]["type"] == "Point"


@pytest.mark.django_db
def test_tree_remove_ajax_post_returns_block_focus(client, manager, farm_role_manager, tree_in_block_a, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:tree-remove", kwargs={"tree_id": tree_in_block_a.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "block", "id": str(block_a.id)}
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.status == Tree.STATUS_REMOVED
    # Removed trees are filtered out of farm_map_data (active-only, same
    # rule the browse payload has always used) — confirms the refreshed
    # payload the client swaps in won't still show the removed tree.
    assert str(tree_in_block_a.id) not in {t["id"] for t in payload["farm_map_data"]["trees"]}


@pytest.mark.django_db
def test_harvest_plan_add_ajax_post_success(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:harvest-plan-add", kwargs={"block_id": block_a.id}),
        {"assessment_date": "2026-08-01", "assessment_quantity": "12.5"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "block", "id": str(block_a.id)}
    assert Harvest.objects.filter(farm_block=block_a, assessment_quantity=12.5).exists()


@pytest.mark.django_db
def test_fertilizer_plan_add_ajax_post_success(client, manager, farm_role_manager, block_a):
    """FertilizerPlanCreateView — Fertilizer's counterpart to
    test_harvest_plan_add_ajax_post_success above, added after direct
    feedback asking where the fertilizer process was on the Farm Map page.
    Same block-level-only shape as Harvest, minus assessment_quantity
    (Fertilizer has no such field — see FertilizerPlanForm's docstring).
    """
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:fertilizer-plan-add", kwargs={"block_id": block_a.id}),
        {"assessment_date": "2026-08-01"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["focus"] == {"type": "block", "id": str(block_a.id)}
    assert Fertilizer.objects.filter(farm_block=block_a, assessment_date="2026-08-01").exists()


@pytest.mark.django_db
def test_fertilizer_plan_add_ajax_get_returns_form_fragment(client, manager, farm_role_manager, block_a):
    client.login(username="manager", password="manager12345")
    response = client.get(
        reverse("dashboard:fertilizer-plan-add", kwargs={"block_id": block_a.id}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert "data-panel-form" in response.json()["html"]


@pytest.mark.django_db
def test_ajax_write_actions_still_enforce_scope(client, manager, farm_role_manager):
    """None of the AJAX branches above are a new authorization surface —
    they reuse the exact same _get_block()/_get_tree()/_require_manager_for_farm()
    checks the full-page path already used, just before branching on
    _is_ajax() for the response shape. Spot-checked here for the create
    endpoint (block-add has no object to scope against, so this checks
    the *save* itself refuses a farm the manager doesn't manage) and the
    delete endpoint (object-level scoping via get_object_or_404 on the
    already-scoped queryset).
    """
    other_farm = Farm.objects.create(name="Someone else's farm")
    client.login(username="manager", password="manager12345")
    response = client.post(
        reverse("dashboard:block-add"),
        {"farm": str(other_farm.id), "name": "Sneaky block"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400  # form's own queryset rejects the farm choice, same as the non-AJAX path
    assert not FarmBlock.objects.filter(name="Sneaky block").exists()

    other_block = FarmBlock.objects.create(farm=other_farm, name="Not managed")
    response = client.post(
        reverse("dashboard:block-delete", kwargs={"block_id": other_block.id}),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 404  # not in this manager's scoped FarmBlockViewSet queryset at all
    assert FarmBlock.objects.filter(id=other_block.id).exists()


# --- Vendored front-end assets -------------------------------------------
# The dashboard's Leaflet/Chart.js/webfont assets are served from this app's
# own static files rather than a CDN (CLAUDE.md's local-first rule). Nothing
# else would catch a regression here: if a URL breaks, the Farm Map silently
# falls back to its "Leaflet failed to load" no-op and renders an empty box,
# and the page still returns 200.

def _dashboard_static(*parts):
    return pathlib.Path(settings.BASE_DIR, "apps", "dashboard", "static", "dashboard", *parts)


def test_vendored_frontend_assets_exist_on_disk():
    for rel in [
        ("vendor", "leaflet", "leaflet.js"),
        ("vendor", "leaflet", "leaflet.css"),
        ("vendor", "leaflet", "leaflet.draw.js"),
        ("vendor", "leaflet", "leaflet.draw.css"),
        ("vendor", "leaflet", "leaflet.markercluster.js"),
        ("vendor", "leaflet", "MarkerCluster.css"),
        ("vendor", "leaflet", "MarkerCluster.Default.css"),
        ("vendor", "chartjs", "chart.umd.js"),
        ("fonts", "public-sans-latin.woff2"),
    ]:
        path = _dashboard_static(*rel)
        assert path.exists(), f"missing vendored asset: {path}"
        assert path.stat().st_size > 0, f"empty vendored asset: {path}"


def test_no_dashboard_template_loads_frontend_code_from_a_cdn():
    """A CDN <script>/<link> makes the map and charts depend on internet
    reachability, which CLAUDE.md's local-first rule rules out. Prose inside
    {% comment %} blocks explaining the decision is fine; live tags are not.
    """
    template_dir = pathlib.Path(settings.BASE_DIR, "apps", "dashboard", "templates", "dashboard")
    offenders = []
    for path in template_dir.rglob("*.html"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("<script") or stripped.startswith("<link")):
                continue
            if "cdnjs.cloudflare.com" in line or "unpkg.com" in line or "fonts.googleapis.com" in line:
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"CDN-loaded front-end code found at: {offenders}"
