"""Harvest/Fertilizer approval + yield-derivation tests — docs/02-build-plan.md
Phase 2 verification gate: "create a harvest plan, log a tree-level actual
harvest, confirm the block total is derived correctly, approve it as a
Manager-role user, confirm a PIC Block-role user cannot approve it, and
confirm the approval shows up in that record's history."
"""
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from rest_framework import status

from apps.operations.models import (
    ActualFertilizer,
    ActualHarvest,
    Fertilizer,
    FertilizerTree,
    Harvest,
    HarvestTree,
    PlanStatus,
)
from apps.operations.services import advance_status


@pytest.fixture
def harvest_plan(db, block_a):
    return Harvest.objects.create(farm_block=block_a, assessment_date="2026-08-01", assessment_quantity=1000.0)


@pytest.fixture
def fertilizer_plan(db, block_a):
    return Fertilizer.objects.create(farm_block=block_a, assessment_date="2026-08-01")


# --- Status transition service ---

@pytest.mark.django_db
def test_advance_status_moves_created_to_created_confirmed(harvest_plan):
    advance_status(harvest_plan)
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED_CONFIRMED


@pytest.mark.django_db
def test_advance_status_raises_clean_error_on_terminal_state(harvest_plan):
    harvest_plan.status = PlanStatus.DONE
    harvest_plan.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        advance_status(harvest_plan)


# --- Effective quantity derivation ---

@pytest.mark.django_db
def test_effective_quantity_uses_manual_total_when_no_tree_rows(harvest_plan):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", quantity=500.0)
    assert actual.get_effective_quantity() == 500.0


@pytest.mark.django_db
def test_effective_quantity_sums_tree_rows_when_present(harvest_plan, tree_in_block_a, pic_block):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", quantity=500.0)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, quantity=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, quantity=80.0,
        recorded_location=Point(101.4411, 0.5311, srid=4326), recorded_by=pic_block,
    )
    # Tree-level total (200) wins over the manual block-level figure (500) —
    # this is the "single source of truth" rule from ActualHarvest docstring.
    assert actual.get_effective_quantity() == 200.0


# --- Approval permission enforcement (fixes the original app's commented-out
#     authorize() bug — see apps/operations/services.py docstring) ---

@pytest.mark.django_db
def test_manager_can_approve_harvest(as_manager, harvest_plan):
    response = as_manager.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code == status.HTTP_200_OK
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED_CONFIRMED


@pytest.mark.django_db
def test_pic_block_cannot_approve_harvest(as_pic_block, harvest_plan):
    response = as_pic_block.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED  # unchanged


@pytest.mark.django_db
def test_unrelated_manager_cannot_approve_other_farms_harvest(as_other_pic_block, harvest_plan):
    response = as_other_pic_block.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
def test_approval_is_recorded_in_history(as_manager, harvest_plan):
    as_manager.post(f"/api/harvests/{harvest_plan.id}/approve/")
    history = harvest_plan.history.all().order_by("history_date")
    assert history.count() >= 2  # initial create + the approval update
    assert history.last().status == PlanStatus.CREATED_CONFIRMED


@pytest.mark.django_db
def test_approving_a_terminal_status_returns_clean_400_not_500(as_manager, harvest_plan):
    harvest_plan.status = PlanStatus.DONE
    harvest_plan.save(update_fields=["status"])
    response = as_manager.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code == status.HTTP_400_BAD_REQUEST  # not a 500 crash — see services.py docstring


# --- Fertilizer approval — same view/permission code as Harvest's approve
#     action (apps/operations/views.py both use IsSuperAdminOrManager +
#     advance_status()), but untested at the API level until now. docs/02-
#     build-plan.md Phase 8 explicitly calls out "approval authorization
#     (the highest-risk area)" for coverage — a shared implementation is
#     still worth its own test per model, since a future edit to one
#     ViewSet and not the other is exactly the kind of drift this guards
#     against. ---

@pytest.mark.django_db
def test_manager_can_approve_fertilizer(as_manager, fertilizer_plan):
    response = as_manager.post(f"/api/fertilizers/{fertilizer_plan.id}/approve/")
    assert response.status_code == status.HTTP_200_OK
    fertilizer_plan.refresh_from_db()
    assert fertilizer_plan.status == PlanStatus.CREATED_CONFIRMED


@pytest.mark.django_db
def test_pic_block_cannot_approve_fertilizer(as_pic_block, fertilizer_plan):
    response = as_pic_block.post(f"/api/fertilizers/{fertilizer_plan.id}/approve/")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    fertilizer_plan.refresh_from_db()
    assert fertilizer_plan.status == PlanStatus.CREATED


@pytest.mark.django_db
def test_unrelated_manager_cannot_approve_other_farms_fertilizer(as_other_pic_block, fertilizer_plan):
    response = as_other_pic_block.post(f"/api/fertilizers/{fertilizer_plan.id}/approve/")
    assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
def test_unauthenticated_user_cannot_approve_harvest(api_client, harvest_plan):
    response = api_client.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED


# --- Idempotent create on HarvestTree (same offline-sync pattern as
#     apps/trees/tests.py — regression test for the DRF auto-UniqueValidator-
#     on-writable-pk bug found there and fixed in every serializer that uses
#     IdempotentUUIDCreateMixin, see apps/operations/serializers.py header). ---

@pytest.mark.django_db
def test_resubmitting_same_harvest_tree_uuid_updates_in_place(as_pic_block, harvest_plan, tree_in_block_a, pic_block):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", quantity=500.0)
    harvest_tree_id = str(uuid.uuid4())
    payload = {
        "id": harvest_tree_id,
        "actual_harvest": str(actual.id),
        "tree": str(tree_in_block_a.id),
        "quantity": 50.0,
    }
    first = as_pic_block.post("/api/harvest-trees/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    payload["quantity"] = 75.0
    second = as_pic_block.post("/api/harvest-trees/", payload, format="json")
    assert second.status_code == status.HTTP_200_OK  # updated, not rejected as duplicate

    assert HarvestTree.objects.filter(id=harvest_tree_id).count() == 1
    assert HarvestTree.objects.get(id=harvest_tree_id).quantity == 75.0


@pytest.mark.django_db
def test_resubmitting_same_fertilizer_tree_uuid_updates_in_place(as_pic_block, fertilizer_plan, tree_in_block_a, pic_block):
    """Same IdempotentUUIDCreateMixin path as HarvestTree above, exercised
    on FertilizerTree too — docs/02-build-plan.md Phase 8 calls out
    "offline sync idempotency" as a coverage target, and until now only the
    Harvest side of this shared mixin had an API-level regression test.
    """
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02")
    fertilizer_tree_id = str(uuid.uuid4())
    payload = {
        "id": fertilizer_tree_id,
        "actual_fertilizer": str(actual.id),
        "tree": str(tree_in_block_a.id),
        "quantity": 2.5,
    }
    first = as_pic_block.post("/api/fertilizer-trees/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    payload["quantity"] = 3.0
    second = as_pic_block.post("/api/fertilizer-trees/", payload, format="json")
    assert second.status_code == status.HTTP_200_OK  # updated, not rejected as duplicate

    assert FertilizerTree.objects.filter(id=fertilizer_tree_id).count() == 1
    assert FertilizerTree.objects.get(id=fertilizer_tree_id).quantity == 3.0
