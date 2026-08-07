"""docs/02-build-plan.md Phase 3 verification gate: "you can authenticate,
create a tree with a client-supplied UUID, create an actual-harvest record
referencing it, and pull it back via the sync endpoint."
"""
import pytest
from rest_framework import status


@pytest.mark.django_db
def test_pull_returns_scoped_data_on_full_pull(as_pic_block, block_a, block_b, tree_in_block_a):
    response = as_pic_block.get("/api/sync/pull/")
    assert response.status_code == status.HTTP_200_OK
    block_ids = [b["id"] for b in response.data["farm_blocks"]]
    tree_ids = [t["id"] for t in response.data["trees"]]
    assert str(block_a.id) in block_ids
    assert str(block_b.id) not in block_ids  # not assigned to this PIC Block user
    assert str(tree_in_block_a.id) in tree_ids
    assert "server_time" in response.data


@pytest.mark.django_db
def test_pull_unaffiliated_user_gets_empty_scoped_data(as_other_pic_block, block_a, tree_in_block_a):
    response = as_other_pic_block.get("/api/sync/pull/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["farm_blocks"] == []
    assert response.data["trees"] == []


@pytest.mark.django_db
def test_pull_with_since_only_returns_changes_after_that_time(as_pic_block, block_a, tree_in_block_a):
    first = as_pic_block.get("/api/sync/pull/")
    cursor = first.data["server_time"]
    assert str(tree_in_block_a.id) in [t["id"] for t in first.data["trees"]]

    # Nothing changed yet — pulling again with the returned cursor should
    # come back empty for trees.
    second = as_pic_block.get("/api/sync/pull/", {"since": cursor})
    assert second.data["trees"] == []

    # Now change the tree — it must appear on the next pull.
    tree_in_block_a.name = "Renamed after first sync"
    tree_in_block_a.save(update_fields=["name", "updated_at"])
    third = as_pic_block.get("/api/sync/pull/", {"since": cursor})
    tree_ids = [t["id"] for t in third.data["trees"]]
    assert str(tree_in_block_a.id) in tree_ids


@pytest.mark.django_db
def test_pull_rejects_malformed_since(as_pic_block):
    response = as_pic_block.get("/api/sync/pull/", {"since": "not-a-real-timestamp"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_pull_includes_new_actual_harvest(as_pic_block, block_a):
    from apps.operations.models import Harvest

    harvest = Harvest.objects.create(farm_block=block_a, assessment_date="2026-08-01", assessment_quantity=1000.0)
    first = as_pic_block.get("/api/sync/pull/")
    cursor = first.data["server_time"]

    response = as_pic_block.post(
        "/api/actual-harvests/", {"harvest": str(harvest.id), "date": "2026-08-02", "quantity": 300.0}, format="json"
    )
    assert response.status_code == status.HTTP_201_CREATED

    second = as_pic_block.get("/api/sync/pull/", {"since": cursor})
    ids = [a["id"] for a in second.data["actual_harvests"]]
    assert response.data["id"] in ids


@pytest.mark.django_db
def test_farm_block_trees_precache_action(as_pic_block, block_a, block_b, tree_in_block_a):
    response = as_pic_block.get(f"/api/farm-blocks/{block_a.id}/trees/")
    assert response.status_code == status.HTTP_200_OK
    assert [t["id"] for t in response.data] == [str(tree_in_block_a.id)]


@pytest.mark.django_db
def test_farm_block_trees_precache_action_404s_out_of_scope_block(as_other_pic_block, block_a):
    response = as_other_pic_block.get(f"/api/farm-blocks/{block_a.id}/trees/")
    assert response.status_code == status.HTTP_404_NOT_FOUND
