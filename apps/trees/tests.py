"""Tree CRUD + offline-sync idempotency tests — docs/02-build-plan.md
Phase 1 verification gate ("field staff can CRUD the tree by himself") and
docs/05-offline-sync.md ("resubmitting the same UUID is idempotent").
"""
import uuid

import pytest
from rest_framework import status

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


@pytest.mark.django_db
def test_resubmitting_out_of_scope_tree_uuid_is_forbidden(as_other_pic_block, tree_in_block_a):
    """A user with no access to Block A must not be able to overwrite an
    existing Block A tree just by replaying its UUID — regression test for
    apps/common/mixins.py IdempotentUUIDCreateMixin's scope check.
    """
    payload = {
        "id": str(tree_in_block_a.id),
        "farm_block": str(tree_in_block_a.farm_block_id),
        "name": "Hijacked",
        "location": {"type": "Point", "coordinates": [101.4415, 0.5315]},
    }
    response = as_other_pic_block.post("/api/trees/", payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    tree_in_block_a.refresh_from_db()
    assert tree_in_block_a.name != "Hijacked"


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
