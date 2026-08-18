"""Tree CRUD + offline-sync idempotency tests — docs/02-build-plan.md
Phase 1 verification gate ("field staff can CRUD the tree by himself") and
docs/05-offline-sync.md ("resubmitting the same UUID is idempotent").
"""
import uuid
from datetime import timedelta

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework import status

from apps.farms.models import FarmBlock
from apps.trees.models import Tree


@pytest.mark.django_db
def test_pic_block_can_create_tree_in_their_own_block(as_pic_block, block_a):
    payload = {
        "farm_block": str(block_a.id),
        "name": "New Tree",
        "location": {"type": "Point", "coordinates": [101.4415, 0.5315]},
    }
    response = as_pic_block.post("/api/trees/", payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED
    assert Tree.objects.filter(name="New Tree", farm_block=block_a).exists()


@pytest.mark.django_db
def test_pic_block_cannot_see_trees_outside_their_block(as_pic_block, as_other_pic_block, block_a, tree_in_block_a):
    response = as_other_pic_block.get("/api/trees/")
    assert response.status_code == status.HTTP_200_OK
    ids = [t["id"] for t in response.data["results"]]
    assert str(tree_in_block_a.id) not in ids


@pytest.mark.django_db
def test_resubmitting_same_client_uuid_updates_in_place_not_duplicate(as_pic_block, block_a):
    tree_id = str(uuid.uuid4())
    payload = {
        "id": tree_id,
        "farm_block": str(block_a.id),
        "name": "Offline Tree",
        "location": {"type": "Point", "coordinates": [101.4415, 0.5315]},
    }
    first = as_pic_block.post("/api/trees/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    payload["name"] = "Offline Tree (edited while still offline)"
    second = as_pic_block.post("/api/trees/", payload, format="json")
    assert second.status_code == status.HTTP_200_OK  # updated, not created again

    assert Tree.objects.filter(id=tree_id).count() == 1
    assert Tree.objects.get(id=tree_id).name == "Offline Tree (edited while still offline)"


@pytest.fixture
def other_pic_blocks_own_block(db, farm, other_pic_block):
    """A block other_pic_block *does* legitimately have access to — needed
    to isolate IdempotentUUIDCreateMixin's own id-scope check (403) from
    ScopedPrimaryKeyFieldsMixin's separate farm_block-scope check (400,
    apps/common/serializers.py), below. other_pic_block otherwise has zero
    blocks in scope (see conftest.py), so without this fixture *any*
    farm_block value they submit — including tree_in_block_a's own — trips
    the FK-scope check first, which masks the id-scope check this test is
    actually about. No boundary needed (nullable — see FarmBlock model).
    """
    return FarmBlock.objects.create(farm=farm, name="Other PIC's own block", pic=other_pic_block)


@pytest.mark.django_db
def test_resubmitting_out_of_scope_tree_uuid_is_forbidden(as_other_pic_block, tree_in_block_a, other_pic_blocks_own_block):
    """A user with no access to Block A must not be able to overwrite an
    existing Block A tree just by replaying its UUID — regression test for
    apps/common/mixins.py IdempotentUUIDCreateMixin's scope check.

    farm_block is deliberately set to a block this user *does* have access
    to (see other_pic_blocks_own_block's docstring) — this test is
    specifically about the id-scope check, not the separate FK-scope check
    (already covered by test_pic_block_cannot_create_tree_in_a_block_they_are_not_assigned_to
    above). Discovered live (against real Postgres, not this sandbox): with
    the original payload — reusing tree_in_block_a's own out-of-scope
    farm_block — DRF's serializer validation raises its own 400 for the
    out-of-scope farm_block before IdempotentUUIDCreateMixin.create() ever
    reaches its id-scope check, so the test was asserting a status code its
    own payload couldn't actually reach. Both checks are real and correct;
    they just needed to be tested independently of each other.
    """
    payload = {
        "id": str(tree_in_block_a.id),
        "farm_block": str(other_pic_blocks_own_block.id),
        "name": "Hijacked",
        "location": {"type": "Point", "coordinates": [101.4415, 0.5315]},
    }
    response = as_other_pic_block.post("/api/trees/", payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.name != "Hijacked"
    assert tree_in_block_a.farm_block_id != other_pic_blocks_own_block.id  # not reparented either


@pytest.mark.django_db
def test_idempotent_create_still_sets_created_by(as_pic_block, block_a, pic_block):
    tree_id = str(uuid.uuid4())
    payload = {
        "id": tree_id,
        "farm_block": str(block_a.id),
        "name": "Offline Tree",
        "location": {"type": "Point", "coordinates": [101.4415, 0.5315]},
    }
    as_pic_block.post("/api/trees/", payload, format="json")
    tree = Tree.objects.get(id=tree_id)
    assert tree.created_by_id == pic_block.id
    assert tree.updated_by_id == pic_block.id


@pytest.mark.django_db
def test_delete_is_a_soft_delete(as_pic_block, tree_in_block_a):
    response = as_pic_block.delete(f"/api/trees/{tree_in_block_a.id}/")
    assert response.status_code == status.HTTP_204_NO_CONTENT
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.status == Tree.STATUS_REMOVED  # row still exists, not hard-deleted


@pytest.mark.django_db
def test_tree_orphaned_outside_all_block_boundaries(block_a, block_b, pic_block):
    from django.contrib.gis.geos import Point

    far_away_tree = Tree.objects.create(
        farm_block=block_a,
        name="Wandered off",
        location=Point(102.0, 1.0, srid=4326),  # nowhere near either block
        created_by=pic_block,
        updated_by=pic_block,
    )
    assert far_away_tree.is_location_orphaned() is True


@pytest.mark.django_db
def test_tree_not_orphaned_when_inside_its_block(tree_in_block_a):
    assert tree_in_block_a.is_location_orphaned() is False


# --- FK write-scope validation (BUILD_STATUS.md "Known gaps": DRF's default
# PrimaryKeyRelatedField only checks a referenced row *exists*, not that the
# requester can reach it — apps/common/scoping.py + apps/common/serializers.py
# now restrict `farm_block` to the requesting user's actual scope.) ---

@pytest.mark.django_db
def test_pic_block_cannot_create_tree_in_a_block_they_are_not_assigned_to(as_pic_block, block_b):
    """block_a's PIC Block user (as_pic_block, per conftest.py) has no
    access to block_b (same farm, no pic, not their assignment, and they're
    not a Manager) — before the FK-scope fix, this would have succeeded
    since DRF only checked that block_b exists.
    """
    payload = {
        "farm_block": str(block_b.id),
        "name": "Should be rejected",
        "location": {"type": "Point", "coordinates": [101.4470, 0.5320]},
    }
    response = as_pic_block.post("/api/trees/", payload, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not Tree.objects.filter(name="Should be rejected").exists()


@pytest.mark.django_db
def test_manager_can_create_tree_in_any_block_on_a_farm_they_manage(as_manager, block_b):
    payload = {
        "farm_block": str(block_b.id),
        "name": "Manager-created tree",
        "location": {"type": "Point", "coordinates": [101.4470, 0.5320]},
    }
    response = as_manager.post("/api/trees/", payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED


# --- PWA tree-edit form (PATCH, not idempotent-create resubmit) ---

@pytest.mark.django_db
def test_editing_a_tree_via_patch_does_not_reset_omitted_fields_to_their_defaults(as_pic_block, block_a, pic_block):
    """Regression test for the exact reasoning documented in pwa/js/app.js's
    treeDetailScreen() docstring and pwa/js/api.js's onlineOnlyWrite(): tree
    edits must go through DRF's partial_update (PATCH), never resubmitted as
    a full create/update through IdempotentUUIDCreateMixin's POST-same-UUID
    path. A *non-partial* pass through TreeSerializer would silently inject
    the model-level default for any field the edit payload omits — here,
    `source` (default "manual") — which would wrongly erase real drone
    provenance every time a field worker only meant to rename a tree or mark
    it removed. PATCH's partial validation only touches submitted fields, so
    this must pass.
    """
    tree = Tree.objects.create(
        farm_block=block_a,
        name="Drone Tree",
        location=Point(101.4410, 0.5310, srid=4326),
        source=Tree.SOURCE_DRONE_DETECTED,
        current_health=Tree.HEALTH_HEALTHY,
        created_by=pic_block,
        updated_by=pic_block,
    )
    # Mirrors the exact payload shape app.js's #tree-edit-form submit handler
    # sends — name + status only, never source or current_health.
    payload = {"name": "Drone Tree (renamed)", "status": Tree.STATUS_ACTIVE}
    response = as_pic_block.patch(f"/api/trees/{tree.id}/", payload, format="json")
    assert response.status_code == status.HTTP_200_OK

    tree.refresh_from_db()
    assert tree.name == "Drone Tree (renamed)"
    assert tree.source == Tree.SOURCE_DRONE_DETECTED  # not silently reset to "manual"
    assert tree.current_health == Tree.HEALTH_HEALTHY  # read-only field, untouched either way
    assert tree.updated_by_id == pic_block.id  # perform_update() still ran


@pytest.mark.django_db
def test_editing_a_tree_via_patch_can_update_its_location(as_pic_block, block_a, pic_block):
    """The "update_location" checkbox path — app.js only includes `location`
    in the payload when the field worker opts in and a GPS fix is available.
    """
    tree = Tree.objects.create(
        farm_block=block_a,
        name="Tree to relocate",
        location=Point(101.4410, 0.5310, srid=4326),
        created_by=pic_block,
        updated_by=pic_block,
    )
    payload = {
        "name": tree.name,
        "status": Tree.STATUS_ACTIVE,
        "location": {"type": "Point", "coordinates": [101.4420, 0.5320]},
    }
    response = as_pic_block.patch(f"/api/trees/{tree.id}/", payload, format="json")
    assert response.status_code == status.HTTP_200_OK

    tree.refresh_from_db()
    assert tree.location == Point(101.4420, 0.5320, srid=4326)


@pytest.mark.django_db
def test_pic_block_cannot_edit_a_tree_outside_their_scope(as_other_pic_block, tree_in_block_a):
    """Scoping applies to the edit path the same as every other TreeViewSet
    action — get_queryset() already filters to scoped_to_user_blocks(), so a
    PATCH to an out-of-scope tree's detail URL 404s (not 403 — the row isn't
    even visible to this user, matching DRF's standard "hide, don't leak
    existence of" behavior for get_queryset()-filtered detail views).
    """
    response = as_other_pic_block.patch(
        f"/api/trees/{tree_in_block_a.id}/", {"name": "Hijacked via edit"}, format="json"
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.name != "Hijacked via edit"


# --- harvest_status()/fertilizer_status() — the per-tree "harvested/
# fertilized indicator" driving the Farm Map's marker ring (see
# Tree.INTERVAL_* and last_harvested_at's help_text). Pure logic, no DB
# writes needed — an unsaved Tree/FarmBlock pair is enough, so no
# @pytest.mark.django_db here (matches how the rest of this file only
# reaches for it when a fixture or query actually needs the database). ---

def _tree_with_history(block, harvested_days_ago=None, fertilized_days_ago=None):
    today = timezone.localdate()
    tree = Tree(farm_block=block)
    tree.last_harvested_at = today - timedelta(days=harvested_days_ago) if harvested_days_ago is not None else None
    tree.last_fertilized_at = today - timedelta(days=fertilized_days_ago) if fertilized_days_ago is not None else None
    return tree


def test_harvest_status_never_when_no_date_recorded(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 14
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block)
    assert tree.harvest_status() == Tree.INTERVAL_NEVER


def test_harvest_status_ok_within_interval(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 14
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block, harvested_days_ago=2)
    assert tree.harvest_status() == Tree.INTERVAL_OK


def test_harvest_status_due_soon_past_80_percent_of_interval(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 10
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block, harvested_days_ago=8)  # 80% of 10
    assert tree.harvest_status() == Tree.INTERVAL_DUE_SOON


def test_harvest_status_overdue_past_interval(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 10
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block, harvested_days_ago=10)
    assert tree.harvest_status() == Tree.INTERVAL_OVERDUE


def test_harvest_status_uses_block_override_not_global_default(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 100  # would read "ok" if this were used
    block = FarmBlock(harvest_interval_days=5)
    tree = _tree_with_history(block, harvested_days_ago=6)
    assert tree.harvest_status() == Tree.INTERVAL_OVERDUE


def test_fertilizer_status_is_independent_of_harvest_status(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 14
    settings.FERTILIZER_DEFAULT_INTERVAL_DAYS = 120
    block = FarmBlock(harvest_interval_days=None, fertilizer_interval_days=None)
    # Overdue on harvest, but nowhere near due on fertilizer — the two
    # statuses must not leak into each other.
    tree = _tree_with_history(block, harvested_days_ago=20, fertilized_days_ago=5)
    assert tree.harvest_status() == Tree.INTERVAL_OVERDUE
    assert tree.fertilizer_status() == Tree.INTERVAL_OK


def test_harvest_stage_is_marked_when_flagged_regardless_of_interval(settings):
    """A freshly-marked tree reads "marked", not "overdue" — even if it's
    also, coincidentally, overdue by date math. See Tree.harvest_stage()'s
    docstring: the marked flag deliberately takes priority.
    """
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 5
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block, harvested_days_ago=30)  # would be "overdue" on its own
    assert tree.harvest_status() == Tree.INTERVAL_OVERDUE
    tree.harvest_marked_at = timezone.now()
    assert tree.harvest_stage() == Tree.STAGE_MARKED


def test_harvest_stage_falls_back_to_interval_status_when_not_marked(settings):
    settings.HARVEST_DEFAULT_INTERVAL_DAYS = 14
    block = FarmBlock(harvest_interval_days=None)
    tree = _tree_with_history(block, harvested_days_ago=2)
    assert tree.harvest_marked_at is None
    assert tree.harvest_stage() == Tree.INTERVAL_OK


# --- Two-stage mark/harvest workflow — TreeViewSet.mark_harvested()/
# mark_fertilized() (apps/trees/views.py), the PIC Block field app's
# "confirm the work assigned to me is done" action (stage 2). Stage 1 (a
# Manager flagging which trees need it) is dashboard-only and covered by
# apps/dashboard tests, not here — see MarkTreesNeedHarvestView. ---

@pytest.mark.django_db
def test_pic_block_can_mark_their_own_block_tree_harvested(as_pic_block, tree_in_block_a, pic_block):
    tree_in_block_a.harvest_marked_at = timezone.now()
    tree_in_block_a.harvest_marked_by = pic_block
    tree_in_block_a.save(update_fields=["harvest_marked_at", "harvest_marked_by"])

    response = as_pic_block.post(f"/api/trees/{tree_in_block_a.id}/mark_harvested/", {}, format="json")
    assert response.status_code == status.HTTP_200_OK

    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.last_harvested_at == timezone.localdate()
    assert tree_in_block_a.harvest_marked_at is None  # cleared on completion
    assert tree_in_block_a.harvest_marked_by is None


@pytest.mark.django_db
def test_mark_harvested_accepts_an_explicit_date(as_pic_block, tree_in_block_a):
    backdated = (timezone.localdate() - timedelta(days=2)).isoformat()
    response = as_pic_block.post(
        f"/api/trees/{tree_in_block_a.id}/mark_harvested/", {"date": backdated}, format="json"
    )
    assert response.status_code == status.HTTP_200_OK
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.last_harvested_at.isoformat() == backdated


@pytest.mark.django_db
def test_mark_fertilized_is_independent_of_mark_harvested(as_pic_block, tree_in_block_a):
    response = as_pic_block.post(f"/api/trees/{tree_in_block_a.id}/mark_fertilized/", {}, format="json")
    assert response.status_code == status.HTTP_200_OK
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.last_fertilized_at == timezone.localdate()
    assert tree_in_block_a.last_harvested_at is None  # untouched


@pytest.mark.django_db
def test_other_pic_block_cannot_mark_a_tree_outside_their_scope(as_other_pic_block, tree_in_block_a):
    """Same "hide, don't leak existence of" shape as the existing PATCH
    scoping test above — get_queryset() already filters tree_in_block_a out
    for a PIC Block user with no access to block_a, so get_object() 404s
    before IsAssignedPicBlockOrManagerOrSuperAdmin is ever consulted.
    """
    response = as_other_pic_block.post(f"/api/trees/{tree_in_block_a.id}/mark_harvested/", {}, format="json")
    assert response.status_code == status.HTTP_404_NOT_FOUND
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.last_harvested_at is None


@pytest.mark.django_db
def test_manager_can_also_mark_a_tree_harvested(as_manager, tree_in_block_a):
    response = as_manager.post(f"/api/trees/{tree_in_block_a.id}/mark_harvested/", {}, format="json")
    assert response.status_code == status.HTTP_200_OK
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.last_harvested_at == timezone.localdate()


@pytest.mark.django_db
def test_mark_harvested_response_reflects_new_stage(as_pic_block, tree_in_block_a):
    """The serialized response's harvest_stage should already read "ok"
    (not "marked" or "never") right after a fresh completion — a client
    acting on this response shouldn't need a second round-trip to know the
    tree is no longer pending.
    """
    response = as_pic_block.post(f"/api/trees/{tree_in_block_a.id}/mark_harvested/", {}, format="json")
    assert response.data["harvest_stage"] == Tree.INTERVAL_OK
    assert response.data["harvest_marked_at"] is None


# --- sync_tree_health management command — the one-time backfill for the
# Tree.current_health-never-actually-written bug (see apps/imagery/
# services.py's classify_detection()/confirm_new_candidate() fix and
# BUILD_STATUS.md's matching update). Reported directly, via a screenshot
# of every tree on the Farm Map rendering gray regardless of its real
# health. ---

@pytest.mark.django_db
def test_sync_tree_health_command_backfills_from_the_most_recent_classification(block_a, tree_in_block_a, manager):
    from io import StringIO

    from django.core.management import call_command

    from apps.imagery.models import Image, ImageProcessingRun, TreeDetection, TreeHealthClassification

    # Simulates the real pre-fix state directly — a matched detection with
    # a real classification already on record, but current_health never
    # synced from it (exactly what every tree matched/confirmed before this
    # fix landed looks like).
    assert tree_in_block_a.current_health == Tree.HEALTH_UNKNOWN
    image = Image.objects.create(farm=block_a.farm, uploaded_by=manager, raw_file="imagery/raw/fake.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)
    detection = TreeDetection.objects.create(
        processing_run=run,
        tree=tree_in_block_a,
        detected_location=tree_in_block_a.location,
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_MATCHED,
    )
    TreeHealthClassification.objects.create(tree_detection=detection, health_label="yellow", confidence=0.8)

    call_command("sync_tree_health", stdout=StringIO())

    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.current_health == "yellow"


@pytest.mark.django_db
def test_sync_tree_health_command_uses_the_most_recently_created_detection(block_a, tree_in_block_a, manager):
    """A tree can have several TreeDetection rows over time (a new one per
    processing run) — "current" health must come from the most recent
    classification, not an arbitrary or earliest one.
    """
    from io import StringIO

    from django.core.management import call_command

    from apps.imagery.models import Image, ImageProcessingRun, TreeDetection, TreeHealthClassification

    image = Image.objects.create(farm=block_a.farm, uploaded_by=manager, raw_file="imagery/raw/fake.tif")
    run = ImageProcessingRun.objects.create(image=image, farm_block=block_a, triggered_by=manager)

    older = TreeDetection.objects.create(
        processing_run=run,
        tree=tree_in_block_a,
        detected_location=tree_in_block_a.location,
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_MATCHED,
    )
    # created_at is auto_now_add=True (ignores any value passed to
    # .create()) — explicitly backdated via .update() (which bypasses
    # auto_now_add's save-time override) so the "most recent" ordering
    # below is deterministic, not dependent on two real timestamps a few
    # microseconds apart landing in the order this test assumes.
    TreeDetection.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=1))
    TreeHealthClassification.objects.create(tree_detection=older, health_label="healthy", confidence=0.9)

    newer = TreeDetection.objects.create(
        processing_run=run,
        tree=tree_in_block_a,
        detected_location=tree_in_block_a.location,
        detection_confidence=0.9,
        match_status=TreeDetection.MATCH_MATCHED,
    )
    TreeHealthClassification.objects.create(tree_detection=newer, health_label="yellow", confidence=0.8)

    call_command("sync_tree_health", stdout=StringIO())

    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.current_health == "yellow"  # the newer detection's classification wins
