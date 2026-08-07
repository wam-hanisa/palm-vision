"""
Shared pytest fixtures. Requires a real Postgres+PostGIS connection (see
palmvision/settings/test.py) — GeoDjango PointField/PolygonField don't work
against SQLite. Run via `docker compose exec web pytest` once the `db`
service is up (docs/11-local-development.md).
"""
import pytest
from django.contrib.gis.geos import Point, Polygon
from rest_framework.test import APIClient

from apps.farms.models import Farm, FarmBlock, UserFarmRole
from apps.trees.models import Tree
from apps.users.models import User

BLOCK_A_BOUNDARY = Polygon((
    (101.4400, 0.5300),
    (101.4450, 0.5300),
    (101.4450, 0.5350),
    (101.4400, 0.5350),
    (101.4400, 0.5300),
), srid=4326)

BLOCK_B_BOUNDARY = Polygon((
    (101.4460, 0.5300),
    (101.4510, 0.5300),
    (101.4510, 0.5350),
    (101.4460, 0.5350),
    (101.4460, 0.5300),
), srid=4326)

# Overlaps BLOCK_A_BOUNDARY by well over 1% of its area — for
# overlapping_blocks() tests.
BLOCK_A_OVERLAPPING_BOUNDARY = Polygon((
    (101.4420, 0.5300),
    (101.4470, 0.5300),
    (101.4470, 0.5350),
    (101.4420, 0.5350),
    (101.4420, 0.5300),
), srid=4326)

# Self-intersecting "bowtie" — geometrically invalid.
INVALID_BOUNDARY_COORDS = (
    (101.4400, 0.5300),
    (101.4450, 0.5350),
    (101.4450, 0.5300),
    (101.4400, 0.5350),
    (101.4400, 0.5300),
)


@pytest.fixture
def super_admin(db):
    return User.objects.create_superuser(username="admin", email="admin@example.com", password="admin12345")


@pytest.fixture
def manager(db):
    return User.objects.create_user(username="manager", email="manager@example.com", password="manager12345")


@pytest.fixture
def pic_block(db):
    return User.objects.create_user(username="pic_block", email="pic@example.com", password="picblock12345")


@pytest.fixture
def other_pic_block(db):
    """A second PIC Block user with no assignment on the demo farm — used to
    assert scoping actually excludes people, not just includes them.
    """
    return User.objects.create_user(username="pic_block_2", email="pic2@example.com", password="picblock12345")


@pytest.fixture
def farm(db):
    return Farm.objects.create(name="Demo Farm")


@pytest.fixture
def farm_role_manager(db, farm, manager):
    return UserFarmRole.objects.create(user=manager, farm=farm, role=UserFarmRole.ROLE_MANAGER)


@pytest.fixture
def block_a(db, farm, pic_block):
    return FarmBlock.objects.create(
        farm=farm, name="Block A", boundary=BLOCK_A_BOUNDARY, commodity="Oil Palm", area=25.0, year=2015, pic=pic_block,
    )


@pytest.fixture
def block_b(db, farm):
    return FarmBlock.objects.create(
        farm=farm, name="Block B", boundary=BLOCK_B_BOUNDARY, commodity="Oil Palm", area=25.0, year=2016,
    )


@pytest.fixture
def tree_in_block_a(db, block_a, pic_block):
    return Tree.objects.create(
        farm_block=block_a,
        name="Tree A1",
        location=Point(101.4410, 0.5310, srid=4326),
        created_by=pic_block,
        updated_by=pic_block,
    )


@pytest.fixture
def api_client():
    return APIClient()


def authed_client(user):
    """Returns an APIClient carrying a real JWT access token for `user`,
    obtained the same way a client would via POST /api/token/ — not a
    force_authenticate() shortcut — so these tests also exercise
    SIMPLE_JWT/TokenObtainPairView end to end.
    """
    from rest_framework_simplejwt.tokens import RefreshToken

    client = APIClient()
    token = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return client


@pytest.fixture
def as_super_admin(super_admin):
    return authed_client(super_admin)


@pytest.fixture
def as_manager(manager, farm_role_manager):
    return authed_client(manager)


@pytest.fixture
def as_pic_block(pic_block):
    return authed_client(pic_block)


@pytest.fixture
def as_other_pic_block(other_pic_block):
    return authed_client(other_pic_block)
