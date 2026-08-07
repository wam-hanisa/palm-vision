"""Farm/FarmBlock model + API tests — docs/02-build-plan.md Phase 1
verification gate and docs/12-security-ops.md § Geometry & Data Integrity
Validation, § Multi-Farm Role Scoping.
"""
import pytest
from django.core.exceptions import ValidationError
from rest_framework import status

from apps.farms.models import FarmBlock
from conftest import BLOCK_A_OVERLAPPING_BOUNDARY, INVALID_BOUNDARY_COORDS


# --- Model-level ---

@pytest.mark.django_db
def test_farm_block_rejects_self_intersecting_boundary(farm):
    from django.contrib.gis.geos import Polygon

    block = FarmBlock(farm=farm, name="Bowtie", boundary=Polygon(INVALID_BOUNDARY_COORDS, srid=4326))
    with pytest.raises(ValidationError):
        block.save()


@pytest.mark.django_db
def test_farm_block_overlapping_blocks_detects_significant_overlap(farm, block_a):
    overlapping = FarmBlock.objects.create(
        farm=farm, name="Overlapper", boundary=BLOCK_A_OVERLAPPING_BOUNDARY,
    )
    assert block_a in overlapping.overlapping_blocks()


@pytest.mark.django_db
def test_farm_block_overlapping_blocks_ignores_non_overlapping(block_a, block_b):
    assert block_b not in block_a.overlapping_blocks()


@pytest.mark.django_db
def test_effective_tree_match_threshold_falls_back_to_setting(block_a, settings):
    settings.TREE_MATCH_DEFAULT_THRESHOLD_M = 4.5
    block_a.tree_match_threshold_m = None
    assert block_a.effective_tree_match_threshold_m() == 4.5


@pytest.mark.django_db
def test_effective_tree_match_threshold_uses_override_when_set(block_a):
    block_a.tree_match_threshold_m = 2.0
    assert block_a.effective_tree_match_threshold_m() == 2.0


# --- API scoping ---

@pytest.mark.django_db
def test_manager_sees_farms_they_manage(as_manager, farm, block_a):
    response = as_manager.get("/api/farms/")
    assert response.status_code == status.HTTP_200_OK
    ids = [f["id"] for f in response.data["results"]]
    assert str(farm.id) in ids


@pytest.mark.django_db
def test_unaffiliated_user_sees_no_farms(as_other_pic_block, farm, block_a):
    response = as_other_pic_block.get("/api/farms/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["results"] == []


@pytest.mark.django_db
def test_pic_block_sees_only_their_assigned_block(as_pic_block, block_a, block_b):
    response = as_pic_block.get("/api/farm-blocks/")
    assert response.status_code == status.HTTP_200_OK
    ids = [b["id"] for b in response.data["results"]]
    assert str(block_a.id) in ids
    assert str(block_b.id) not in ids


@pytest.mark.django_db
def test_super_admin_sees_all_blocks_regardless_of_assignment(as_super_admin, block_a, block_b):
    response = as_super_admin.get("/api/farm-blocks/")
    ids = [b["id"] for b in response.data["results"]]
    assert str(block_a.id) in ids
    assert str(block_b.id) in ids


@pytest.mark.django_db
def test_farm_block_geojson_action_returns_geo_feature(as_manager, block_a):
    response = as_manager.get(f"/api/farm-blocks/{block_a.id}/geojson/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["type"] == "Feature"
    assert response.data["geometry"]["type"] == "Polygon"
