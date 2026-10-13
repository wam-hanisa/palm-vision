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
from apps.operations.services import advance_status, has_actual_record


@pytest.fixture
def harvest_plan(db, block_a):
    return Harvest.objects.create(farm_block=block_a, assessment_date="2026-08-01", assessment_quantity=1000.0)


@pytest.fixture
def fertilizer_plan(db, block_a):
    return Fertilizer.objects.create(farm_block=block_a, assessment_date="2026-08-01")


# --- Django admin's own change form must not be a second, unchecked way to
#     set `status` — it's a plain ModelAdmin with no readonly_fields by
#     default, so `status` there is an ordinary editable dropdown with none
#     of advance_status()'s transition/has_actual_record checks. That's the
#     actual, currently-reachable version of "a plan reaches Done with zero
#     recorded actuals" (the dashboard's own Approve button couldn't do this
#     — can_advance()/has_actual_record() already gate it correctly), so
#     it's locked down the same way the DRF serializers already treat
#     `status` as read-only.

def test_harvest_admin_status_field_is_read_only():
    from apps.operations.admin import HarvestAdmin

    assert "status" in HarvestAdmin.readonly_fields


def test_fertilizer_admin_status_field_is_read_only():
    from apps.operations.admin import FertilizerAdmin

    assert "status" in FertilizerAdmin.readonly_fields


# --- Status transition service ---

@pytest.mark.django_db
def test_advance_status_moves_created_to_created_confirmed(harvest_plan):
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    advance_status(harvest_plan)
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED_CONFIRMED


@pytest.mark.django_db
def test_advance_status_blocks_created_without_actual_harvest(harvest_plan):
    """has_actual_record() now gates every transition, not just
    done_wait -> done — reported directly: "there are no need approve for
    harvest plan[,] only approve harvest plan that already inputted by the
    pic block." A bare plan with zero actuals has nothing to approve yet.
    """
    assert has_actual_record(harvest_plan) is False
    with pytest.raises(ValidationError):
        advance_status(harvest_plan)
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.CREATED  # unchanged


@pytest.mark.django_db
def test_advance_status_raises_clean_error_on_terminal_state(harvest_plan):
    harvest_plan.status = PlanStatus.DONE
    harvest_plan.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        advance_status(harvest_plan)


# --- done_wait -> done requires a recorded actual (closes the gap where a
#     plan reached "Done" purely via repeated Approve clicks while "Yield
#     history" — which reads ActualHarvest/ActualFertilizer directly, not
#     plan status — stayed empty) ---

@pytest.mark.django_db
def test_advance_status_blocks_done_without_actual_harvest(harvest_plan):
    harvest_plan.status = PlanStatus.DONE_WAIT
    harvest_plan.save(update_fields=["status"])
    assert has_actual_record(harvest_plan) is False
    with pytest.raises(ValidationError):
        advance_status(harvest_plan)
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.DONE_WAIT  # unchanged


@pytest.mark.django_db
def test_advance_status_allows_done_once_actual_harvest_exists(harvest_plan):
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", quantity=500.0)
    harvest_plan.status = PlanStatus.DONE_WAIT
    harvest_plan.save(update_fields=["status"])
    assert has_actual_record(harvest_plan) is True
    advance_status(harvest_plan)
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.DONE


@pytest.mark.django_db
def test_advance_status_blocks_done_without_actual_fertilizer(fertilizer_plan):
    fertilizer_plan.status = PlanStatus.DONE_WAIT
    fertilizer_plan.save(update_fields=["status"])
    with pytest.raises(ValidationError):
        advance_status(fertilizer_plan)
    fertilizer_plan.refresh_from_db()
    assert fertilizer_plan.status == PlanStatus.DONE_WAIT  # unchanged


@pytest.mark.django_db
def test_advance_status_allows_done_once_actual_fertilizer_exists(fertilizer_plan):
    ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=25.0)
    fertilizer_plan.status = PlanStatus.DONE_WAIT
    fertilizer_plan.save(update_fields=["status"])
    advance_status(fertilizer_plan)
    fertilizer_plan.refresh_from_db()
    assert fertilizer_plan.status == PlanStatus.DONE


@pytest.mark.django_db
def test_approving_done_wait_without_actual_record_returns_clean_400(as_manager, harvest_plan):
    harvest_plan.status = PlanStatus.DONE_WAIT
    harvest_plan.save(update_fields=["status"])
    response = as_manager.post(f"/api/harvests/{harvest_plan.id}/approve/")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.DONE_WAIT  # unchanged


# --- Effective quantity derivation (weight_kg — asked directly to move
# every harvest record from bunch count to kg; `quantity` is now optional
# supplementary bunch-count detail only, no longer part of this math). ---

def _approve(plan):
    """Test helper — sets a plan straight to "done" without going through
    advance_status()'s own transition table, for tests that are about the
    quantity math get_effective_quantity() does once approved, not about
    the approval workflow itself (that's _TRANSITIONS'/advance_status()'s
    own test coverage above).
    """
    plan.status = PlanStatus.DONE
    plan.save(update_fields=["status"])
    return plan


@pytest.mark.django_db
def test_effective_quantity_uses_manual_total_when_no_tree_rows(harvest_plan):
    _approve(harvest_plan)
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    assert actual.get_effective_quantity() == 500.0


@pytest.mark.django_db
def test_effective_quantity_prefers_manual_total_over_tree_rows(harvest_plan, tree_in_block_a, pic_block):
    """Update: the manual total now wins whenever it's set, even with a
    partial tree-level breakdown underneath it — asked directly, reversing
    this test's original assertion (tree-sum used to win outright). See
    ActualHarvest.get_effective_quantity()'s own docstring for why: the
    old rule silently discarded a real reported total (500) the moment
    even a couple of trees out of a much larger harvest got individually
    measured (120 + 80 = 200), quietly losing 300 kg of real yield.
    """
    _approve(harvest_plan)
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=80.0,
        recorded_location=Point(101.4411, 0.5311, srid=4326), recorded_by=pic_block,
    )
    assert actual.get_effective_quantity() == 500.0


@pytest.mark.django_db
def test_effective_quantity_falls_back_to_tree_sum_without_a_manual_total(harvest_plan, tree_in_block_a, pic_block):
    """The original rule, unchanged for this specific case: no manual
    total was ever given, so the tree-level sum is all there is.
    """
    _approve(harvest_plan)
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=None)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=80.0,
        recorded_location=Point(101.4411, 0.5311, srid=4326), recorded_by=pic_block,
    )
    assert actual.get_effective_quantity() == 200.0


@pytest.mark.django_db
def test_effective_quantity_is_none_before_plan_is_approved(harvest_plan):
    """The new compliance-grade gate itself: make harvest plan → input
    actual harvest → approve → *then* it counts. A real, fully-reported
    total sits right there on the row, but get_effective_quantity() must
    not surface it until the plan is actually approved to "done" — see
    get_reported_quantity() for the always-visible figure a "pending
    approval" display (apps/dashboard/views.py's _recent_yield_by_block())
    uses instead.
    """
    assert harvest_plan.status == PlanStatus.CREATED  # sanity: fixture's own default, not yet approved
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    assert actual.get_effective_quantity() is None
    assert actual.get_reported_quantity() == 500.0

    _approve(harvest_plan)
    # Re-fetched, not actual.refresh_from_db() — this needs the *related*
    # harvest_plan's now-updated status, not actual's own fields, and a
    # fresh query is the unambiguous way to drop any cached FK.
    actual = ActualHarvest.objects.get(id=actual.id)
    assert actual.get_effective_quantity() == 500.0


@pytest.mark.django_db
def test_effective_quantity_is_none_with_neither_manual_nor_tree_data(harvest_plan):
    _approve(harvest_plan)  # so this tests "no data," not the separate approval gate
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=None)
    assert actual.get_effective_quantity() is None


# --- HarvestTree.effective_quantity — the "average from bulk when a tree
# has no individual reading" behavior. ---

@pytest.mark.django_db
def test_harvest_tree_effective_quantity_returns_its_own_measured_value(harvest_plan, tree_in_block_a, pic_block):
    _approve(harvest_plan)
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    measured = HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    assert measured.effective_quantity == 120.0


@pytest.mark.django_db
def test_harvest_tree_effective_quantity_is_none_before_plan_is_approved(harvest_plan, tree_in_block_a, pic_block):
    """Same compliance-grade gate as ActualHarvest.get_effective_quantity()
    — see test_effective_quantity_is_none_before_plan_is_approved above.
    """
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    measured = HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    assert measured.effective_quantity is None


@pytest.mark.django_db
def test_harvest_tree_effective_quantity_estimates_the_remainder_when_unmeasured(
    harvest_plan, tree_in_block_a, pic_block
):
    """10 HarvestTree rows' worth of kg reported as one total (500); 2 are
    individually measured (120 + 80 = 200 real kg); the other 8 should
    each show an equal share of the 300 left over (37.5), not 500/10 = 50
    — diluting the measured trees' own real numbers into the average
    would be wrong. All rows reference the same Tree fixture — HarvestTree
    isn't unique per tree, and only the quantity math is under test here,
    not block/tree scoping.
    """
    _approve(harvest_plan)
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=80.0,
        recorded_location=Point(101.4411, 0.5311, srid=4326), recorded_by=pic_block,
    )
    unmeasured = [
        HarvestTree.objects.create(actual_harvest=actual, tree=tree_in_block_a, weight_kg=None, recorded_by=pic_block)
        for _ in range(8)
    ]
    for tree in unmeasured:
        assert tree.effective_quantity == pytest.approx(37.5)


@pytest.mark.django_db
def test_harvest_tree_effective_quantity_is_none_without_a_manual_total(harvest_plan, tree_in_block_a, pic_block):
    _approve(harvest_plan)  # so this tests "no sound data," not the separate approval gate
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=None)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    unmeasured = HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=None, recorded_by=pic_block
    )
    # No manual total anywhere -- nothing sound to estimate an unmeasured
    # tree's share from, even though a sibling tree does have a real
    # number (that number alone isn't "the whole harvest's total").
    assert unmeasured.effective_quantity is None


@pytest.mark.django_db
def test_harvest_tree_effective_quantity_is_none_when_measured_trees_exceed_the_total(
    harvest_plan, tree_in_block_a, pic_block
):
    """The manual total (100) is smaller than what's already individually
    measured (120) -- the reported total is incomplete or wrong, so an
    unmeasured sibling gets no fabricated negative/zero-looking estimate.
    """
    _approve(harvest_plan)  # so this tests "measured exceeds total," not the separate approval gate
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=100.0)
    HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=120.0,
        recorded_location=Point(101.4410, 0.5310, srid=4326), recorded_by=pic_block,
    )
    unmeasured = HarvestTree.objects.create(
        actual_harvest=actual, tree=tree_in_block_a, weight_kg=None, recorded_by=pic_block
    )
    assert unmeasured.effective_quantity is None


# --- ActualFertilizer/FertilizerTree — same manual-total-authoritative
# rule and same derived-remainder-average as ActualHarvest/HarvestTree
# above (asked directly to fill this exact gap: ActualFertilizer had no
# manual block-level total field at all before this). ---

@pytest.mark.django_db
def test_fertilizer_effective_quantity_prefers_manual_total_over_tree_rows(fertilizer_plan, tree_in_block_a, pic_block):
    _approve(fertilizer_plan)
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=50.0)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=8.0, recorded_by=pic_block)
    assert actual.get_effective_quantity() == 50.0


@pytest.mark.django_db
def test_fertilizer_effective_quantity_falls_back_to_tree_sum_without_a_manual_total(
    fertilizer_plan, tree_in_block_a, pic_block
):
    _approve(fertilizer_plan)
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=None)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=8.0, recorded_by=pic_block)
    assert actual.get_effective_quantity() == 20.0


@pytest.mark.django_db
def test_fertilizer_effective_quantity_is_none_with_neither_manual_nor_tree_data(fertilizer_plan):
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=None)
    assert actual.get_effective_quantity() is None


@pytest.mark.django_db
def test_fertilizer_tree_effective_quantity_returns_its_own_measured_value(fertilizer_plan, tree_in_block_a, pic_block):
    _approve(fertilizer_plan)
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=50.0)
    measured = FertilizerTree.objects.create(
        actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block
    )
    assert measured.effective_quantity == 12.0


@pytest.mark.django_db
def test_fertilizer_tree_effective_quantity_estimates_the_remainder_when_unmeasured(
    fertilizer_plan, tree_in_block_a, pic_block
):
    """50kg total; 2 trees individually measured (12 + 8 = 20kg real); the
    other 3 should each show an equal share of the 30kg left over (10),
    not 50/5 = 10 coincidentally matching here — verified against a case
    where that coincidence doesn't happen (see the harvest-side test).
    """
    _approve(fertilizer_plan)
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=50.0)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=8.0, recorded_by=pic_block)
    unmeasured = [
        FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=None, recorded_by=pic_block)
        for _ in range(3)
    ]
    for tree in unmeasured:
        assert tree.effective_quantity == pytest.approx(10.0)


@pytest.mark.django_db
def test_fertilizer_tree_effective_quantity_is_none_without_a_manual_total(fertilizer_plan, tree_in_block_a, pic_block):
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=None)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block)
    unmeasured = FertilizerTree.objects.create(
        actual_fertilizer=actual, tree=tree_in_block_a, quantity=None, recorded_by=pic_block
    )
    assert unmeasured.effective_quantity is None


@pytest.mark.django_db
def test_fertilizer_tree_effective_quantity_is_none_when_measured_trees_exceed_the_total(
    fertilizer_plan, tree_in_block_a, pic_block
):
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=10.0)
    FertilizerTree.objects.create(actual_fertilizer=actual, tree=tree_in_block_a, quantity=12.0, recorded_by=pic_block)
    unmeasured = FertilizerTree.objects.create(
        actual_fertilizer=actual, tree=tree_in_block_a, quantity=None, recorded_by=pic_block
    )
    assert unmeasured.effective_quantity is None


# --- Approval permission enforcement (fixes the original app's commented-out
#     authorize() bug — see apps/operations/services.py docstring) ---

@pytest.mark.django_db
def test_manager_can_approve_harvest(as_manager, harvest_plan):
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
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
    ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
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
    ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=25.0)
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


@pytest.mark.django_db
def test_resubmitting_same_harvest_plan_uuid_updates_in_place(as_pic_block, block_a):
    """Same IdempotentUUIDCreateMixin path as HarvestTree/FertilizerTree
    above, now also on Harvest/Fertilizer plan creation itself — added
    once the PWA's findOrCreatePlan() (pwa/js/app.js) started letting a
    PIC Block create a plan client-side when a block has none yet, which
    needs the exact same offline-retry-safety every other record in this
    pipeline already has (HarvestViewSet/FertilizerViewSet,
    apps/operations/views.py).
    """
    plan_id = str(uuid.uuid4())
    payload = {"id": plan_id, "farm_block": str(block_a.id), "assessment_date": "2026-08-01", "assessment_quantity": 0}
    first = as_pic_block.post("/api/harvests/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    payload["assessment_quantity"] = 250.0
    second = as_pic_block.post("/api/harvests/", payload, format="json")
    assert second.status_code == status.HTTP_200_OK  # updated, not rejected as duplicate

    assert Harvest.objects.filter(id=plan_id).count() == 1
    plan = Harvest.objects.get(id=plan_id)
    assert plan.assessment_quantity == 250.0
    assert plan.status == PlanStatus.CREATED  # untouched by the resubmit


@pytest.mark.django_db
def test_resubmitting_same_fertilizer_plan_uuid_updates_in_place(as_pic_block, block_a):
    """Fertilizer's counterpart to test_resubmitting_same_harvest_plan_uuid_updates_in_place above."""
    plan_id = str(uuid.uuid4())
    payload = {"id": plan_id, "farm_block": str(block_a.id), "assessment_date": "2026-08-01"}
    first = as_pic_block.post("/api/fertilizers/", payload, format="json")
    assert first.status_code == status.HTTP_201_CREATED

    payload["assessment_date"] = "2026-08-02"
    second = as_pic_block.post("/api/fertilizers/", payload, format="json")
    assert second.status_code == status.HTTP_200_OK

    assert Fertilizer.objects.filter(id=plan_id).count() == 1
    plan = Fertilizer.objects.get(id=plan_id)
    assert plan.assessment_date.isoformat() == "2026-08-02"
    assert plan.status == PlanStatus.CREATED


@pytest.mark.django_db
def test_pic_block_can_log_a_harvest_for_a_block_with_no_existing_plan(as_pic_block, block_a, tree_in_block_a):
    """The exact two-request sequence findOrCreatePlan()/submitTreeLevelActual()
    (pwa/js/app.js) issue when a PIC Block logs a harvest against a block
    that has zero Harvest plans yet: create the plan first (client UUID),
    then the actual harvest referencing it — proving the whole feature
    works end to end at the API level, not just each piece in isolation.
    Reported directly: this used to be impossible from the field app at
    all ("No harvest plan found — create one from the office dashboard
    first"), forcing a PIC Block to wait on a Manager before logging
    anything against a brand-new block.
    """
    assert not Harvest.objects.filter(farm_block=block_a).exists()

    plan_id = str(uuid.uuid4())
    plan_response = as_pic_block.post(
        "/api/harvests/",
        {"id": plan_id, "farm_block": str(block_a.id), "assessment_date": "2026-09-20", "assessment_quantity": 45.0},
        format="json",
    )
    assert plan_response.status_code == status.HTTP_201_CREATED

    actual_response = as_pic_block.post(
        "/api/actual-harvests/",
        {"id": str(uuid.uuid4()), "harvest": plan_id, "date": "2026-09-20", "weight_kg": 45.0},
        format="json",
    )
    assert actual_response.status_code == status.HTTP_201_CREATED
    assert ActualHarvest.objects.get(harvest_id=plan_id).weight_kg == 45.0
    assert Harvest.objects.get(id=plan_id).farm_block_id == block_a.id


@pytest.mark.django_db
def test_harvest_plan_status_cannot_be_smuggled_in_through_a_resubmit(as_pic_block, block_a):
    """`status` is read_only on HarvestSerializer — the idempotent-create
    path (which bypasses HarvestViewSet.perform_create()'s own
    status="created" override) must not give a client a second way to set
    an arbitrary status. The original Laravel app's core flaw was exactly
    this kind of silently-bypassable status write (see PlanStatus's own
    docstring, apps/operations/models.py).
    """
    plan_id = str(uuid.uuid4())
    payload = {
        "id": plan_id, "farm_block": str(block_a.id), "assessment_date": "2026-08-01",
        "assessment_quantity": 0, "status": "done",
    }
    as_pic_block.post("/api/harvests/", payload, format="json")
    assert Harvest.objects.get(id=plan_id).status == PlanStatus.CREATED


# --- FK write-scope validation (BUILD_STATUS.md "Known gaps" — DRF's
# default PrimaryKeyRelatedField only checks a referenced row *exists*, not
# that the requester can reach it. apps/common/scoping.py +
# apps/common/serializers.py now restrict farm_block/harvest/fertilizer/tree
# fields to the requesting user's actual scope.) ---

@pytest.mark.django_db
def test_pic_block_cannot_create_harvest_plan_for_a_block_they_are_not_assigned_to(as_pic_block, block_b):
    response = as_pic_block.post(
        "/api/harvests/",
        {"farm_block": str(block_b.id), "assessment_date": "2026-08-01", "assessment_quantity": 500.0},
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not Harvest.objects.filter(farm_block=block_b).exists()


@pytest.mark.django_db
def test_pic_block_cannot_log_actual_harvest_against_an_out_of_scope_harvest_plan(as_pic_block, block_b):
    """block_b's harvest plan belongs to a block as_pic_block has no access
    to — logging an actual against it should be rejected the same way
    referencing the block directly is, since ActualHarvest.harvest is the
    write path into that same out-of-scope data.
    """
    other_plan = Harvest.objects.create(farm_block=block_b, assessment_date="2026-08-01", assessment_quantity=500.0)
    response = as_pic_block.post(
        "/api/actual-harvests/",
        {"harvest": str(other_plan.id), "date": "2026-08-02", "quantity": 100.0},
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not ActualHarvest.objects.filter(harvest=other_plan).exists()


@pytest.mark.django_db
def test_pic_block_cannot_log_harvest_tree_for_a_tree_outside_their_block(as_pic_block, harvest_plan, block_b, pic_block):
    from django.contrib.gis.geos import Point

    from apps.trees.models import Tree

    tree_in_block_b = Tree.objects.create(
        farm_block=block_b, name="Block B tree", location=Point(101.4470, 0.5320, srid=4326),
        created_by=pic_block, updated_by=pic_block,
    )
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", quantity=500.0)
    response = as_pic_block.post(
        "/api/harvest-trees/",
        {"actual_harvest": str(actual.id), "tree": str(tree_in_block_b.id), "quantity": 50.0},
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not HarvestTree.objects.filter(tree=tree_in_block_b).exists()


# --- Editing or deleting an existing operations record is a Manager action.
#     A PIC Block user creates these offline (append-only sync), but could
#     previously PATCH an approved weight (500 -> 9999) or DELETE records
#     outright through the API. ---

@pytest.mark.django_db
def test_pic_block_cannot_edit_or_delete_an_actual_harvest(as_pic_block, harvest_plan):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    patch = as_pic_block.patch(f"/api/actual-harvests/{actual.id}/", {"weight_kg": 9999}, format="json")
    delete = as_pic_block.delete(f"/api/actual-harvests/{actual.id}/")
    assert patch.status_code == status.HTTP_403_FORBIDDEN
    assert delete.status_code == status.HTTP_403_FORBIDDEN
    actual.refresh_from_db()
    assert actual.weight_kg == 500.0


@pytest.mark.django_db
def test_pic_block_cannot_edit_or_delete_a_plan(as_pic_block, harvest_plan, fertilizer_plan):
    assert as_pic_block.patch(f"/api/harvests/{harvest_plan.id}/", {"assessment_quantity": 1}, format="json").status_code == 403
    assert as_pic_block.delete(f"/api/harvests/{harvest_plan.id}/").status_code == 403
    assert as_pic_block.delete(f"/api/fertilizers/{fertilizer_plan.id}/").status_code == 403
    assert Harvest.objects.filter(id=harvest_plan.id).exists()


@pytest.mark.django_db
def test_pic_block_cannot_delete_a_harvest_tree_row(as_pic_block, harvest_plan, tree_in_block_a, pic_block):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    row = HarvestTree.objects.create(actual_harvest=actual, tree=tree_in_block_a, weight_kg=10.0, recorded_by=pic_block)
    assert as_pic_block.delete(f"/api/harvest-trees/{row.id}/").status_code == 403
    assert HarvestTree.objects.filter(id=row.id).exists()


@pytest.mark.django_db
def test_manager_can_still_correct_an_actual_harvest_and_it_is_audited(as_manager, harvest_plan):
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02", weight_kg=500.0)
    response = as_manager.patch(f"/api/actual-harvests/{actual.id}/", {"weight_kg": 480.0}, format="json")
    assert response.status_code == status.HTTP_200_OK
    actual.refresh_from_db()
    assert actual.weight_kg == 480.0
    assert [h.weight_kg for h in actual.history.order_by("history_date")] == [500.0, 480.0]


@pytest.mark.django_db
def test_resubmitting_an_approved_actual_harvest_does_not_change_it(as_pic_block, harvest_plan):
    """The idempotent-create path is a second write route — once the plan
    is Done, a replay returns the stored record unchanged (200, so a real
    offline replay still clears from the outbox) instead of applying the
    new values.
    """
    actual_id = str(uuid.uuid4())
    payload = {"id": actual_id, "harvest": str(harvest_plan.id), "date": "2026-08-02", "weight_kg": 500.0}
    assert as_pic_block.post("/api/actual-harvests/", payload, format="json").status_code == status.HTTP_201_CREATED
    _approve(harvest_plan)

    payload["weight_kg"] = 9999.0
    replay = as_pic_block.post("/api/actual-harvests/", payload, format="json")
    assert replay.status_code == status.HTTP_200_OK
    assert replay.data["weight_kg"] == 500.0
    assert ActualHarvest.objects.get(id=actual_id).weight_kg == 500.0


@pytest.mark.django_db
def test_resubmitting_an_approved_plan_does_not_change_it(as_pic_block, block_a):
    plan_id = str(uuid.uuid4())
    payload = {"id": plan_id, "farm_block": str(block_a.id), "assessment_date": "2026-08-01", "assessment_quantity": 10}
    as_pic_block.post("/api/harvests/", payload, format="json")
    _approve(Harvest.objects.get(id=plan_id))

    payload["assessment_quantity"] = 1
    assert as_pic_block.post("/api/harvests/", payload, format="json").status_code == status.HTTP_200_OK
    assert Harvest.objects.get(id=plan_id).assessment_quantity == 10


# --- Actuals created through the API (the PWA's path) move their plan to
#     done_wait, same as the dashboard's "Record actual"; and nothing new can
#     be added under an already-approved plan. ---

@pytest.mark.django_db
def test_api_created_actual_moves_plan_to_done_wait(as_pic_block, harvest_plan, fertilizer_plan):
    as_pic_block.post("/api/actual-harvests/", {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id),
                                                "date": "2026-08-02", "weight_kg": 50}, format="json")
    as_pic_block.post("/api/actual-fertilizers/", {"id": str(uuid.uuid4()), "fertilizer": str(fertilizer_plan.id),
                                                   "date": "2026-08-02", "quantity": 5}, format="json")
    harvest_plan.refresh_from_db()
    fertilizer_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.DONE_WAIT
    assert fertilizer_plan.status == PlanStatus.DONE_WAIT


@pytest.mark.django_db
def test_api_actual_approves_straight_to_done_and_counts(as_pic_block, as_manager, harvest_plan):
    as_pic_block.post("/api/actual-harvests/", {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id),
                                                "date": "2026-08-02", "weight_kg": 50}, format="json")
    assert as_manager.post(f"/api/harvests/{harvest_plan.id}/approve/").status_code == status.HTTP_200_OK
    harvest_plan.refresh_from_db()
    assert harvest_plan.status == PlanStatus.DONE
    assert ActualHarvest.objects.get(harvest=harvest_plan).get_effective_quantity() == 50


@pytest.mark.django_db
def test_new_actual_on_an_approved_plan_is_refused(as_pic_block, harvest_plan):
    """It would count toward yield immediately, without ever being approved."""
    _approve(harvest_plan)
    response = as_pic_block.post("/api/actual-harvests/", {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id),
                                                           "date": "2026-08-02", "weight_kg": 50}, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already been approved" in str(response.data["harvest"])
    assert not ActualHarvest.objects.filter(harvest=harvest_plan).exists()


@pytest.mark.django_db
def test_new_tree_row_under_an_approved_plan_is_refused(as_pic_block, harvest_plan, tree_in_block_a):
    """With no manual total, the tree rows ARE the approved figure — a row
    added after approval would change it."""
    actual = ActualHarvest.objects.create(harvest=harvest_plan, date="2026-08-02")
    _approve(harvest_plan)
    response = as_pic_block.post("/api/harvest-trees/", {"id": str(uuid.uuid4()), "actual_harvest": str(actual.id),
                                                         "tree": str(tree_in_block_a.id), "weight_kg": 9}, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not HarvestTree.objects.filter(actual_harvest=actual).exists()


@pytest.mark.django_db
def test_replaying_an_actual_created_before_approval_still_succeeds(as_pic_block, harvest_plan):
    """The approved-plan guard applies to brand-new records only — a queued
    write that reached the server before approval, then got replayed after
    it (lost response), must still clear from the device's outbox."""
    payload = {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id), "date": "2026-08-02", "weight_kg": 50}
    as_pic_block.post("/api/actual-harvests/", payload, format="json")
    _approve(harvest_plan)
    assert as_pic_block.post("/api/actual-harvests/", payload, format="json").status_code == status.HTTP_200_OK


# --- Sanity checks on typed amounts/dates ---

@pytest.mark.django_db
def test_actual_harvest_rejects_negative_weight_and_future_date(as_pic_block, harvest_plan):
    import uuid
    from datetime import timedelta

    from django.utils import timezone

    negative = as_pic_block.post(
        "/api/actual-harvests/",
        {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id), "date": timezone.localdate().isoformat(), "weight_kg": -5},
        format="json",
    )
    assert negative.status_code == 400
    assert "weight_kg" in negative.json()

    future = as_pic_block.post(
        "/api/actual-harvests/",
        {"id": str(uuid.uuid4()), "harvest": str(harvest_plan.id), "date": (timezone.localdate() + timedelta(days=30)).isoformat(), "weight_kg": 5},
        format="json",
    )
    assert future.status_code == 400
    assert "date" in future.json()


@pytest.mark.django_db
def test_fertilizer_quantity_is_pending_until_the_plan_is_approved(fertilizer_plan):
    actual = ActualFertilizer.objects.create(fertilizer=fertilizer_plan, date="2026-08-02", quantity=50.0)
    assert actual.get_reported_quantity() == 50.0
    assert actual.get_effective_quantity() is None
    _approve(fertilizer_plan)
    assert actual.get_effective_quantity() == 50.0
