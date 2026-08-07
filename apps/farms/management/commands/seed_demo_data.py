"""
Seeds a minimal local-dev demo dataset: one farm, a few blocks with real
(simplified) boundary polygons, some trees, and one user per role.

See docs/11-local-development.md § Seed data and docs/02-build-plan.md
Phase 1's verification gate, which assumes this command exists and stays
current as later phases add required fields.

Usage: python manage.py seed_demo_data
"""
from django.contrib.gis.geos import Point, Polygon
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.farms.models import Farm, FarmBlock, UserFarmRole
from apps.trees.models import Tree
from apps.users.models import User


class Command(BaseCommand):
    help = "Seed a minimal demo dataset for local development."

    @transaction.atomic
    def handle(self, *args, **options):
        if Farm.objects.filter(name="Demo Farm").exists():
            self.stdout.write(self.style.WARNING("Demo data already exists — skipping. Delete the 'Demo Farm' farm to reseed."))
            return

        super_admin, _ = User.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@example.com", "is_staff": True, "is_superuser": True},
        )
        super_admin.set_password("admin12345")
        super_admin.save()

        manager, _ = User.objects.get_or_create(
            username="manager", defaults={"email": "manager@example.com", "first_name": "Demo", "last_name": "Manager"}
        )
        manager.set_password("manager12345")
        manager.save()

        pic_block, _ = User.objects.get_or_create(
            username="pic_block", defaults={"email": "pic@example.com", "first_name": "Demo", "last_name": "PIC Block"}
        )
        pic_block.set_password("picblock12345")
        pic_block.save()

        farm = Farm.objects.create(name="Demo Farm")
        UserFarmRole.objects.create(user=manager, farm=farm, role=UserFarmRole.ROLE_MANAGER)
        UserFarmRole.objects.create(user=pic_block, farm=farm, role=UserFarmRole.ROLE_PIC_BLOCK)

        # Simplified rectangular boundaries — not real plantation geometry,
        # just enough to exercise the spatial queries (contains, dwithin,
        # intersects) described throughout docs/06-ml-pipeline.md and
        # docs/12-security-ops.md. Coordinates are illustrative (Riau,
        # Sumatra region, a common Indonesian oil-palm area) — not a real
        # plantation boundary.
        block_a = FarmBlock.objects.create(
            farm=farm,
            name="Block A",
            boundary=Polygon((
                (101.4400, 0.5300),
                (101.4450, 0.5300),
                (101.4450, 0.5350),
                (101.4400, 0.5350),
                (101.4400, 0.5300),
            ), srid=4326),
            commodity="Oil Palm",
            area=25.0,
            year=2015,
            pic=pic_block,
        )
        FarmBlock.objects.create(
            farm=farm,
            name="Block B",
            boundary=Polygon((
                (101.4460, 0.5300),
                (101.4510, 0.5300),
                (101.4510, 0.5350),
                (101.4460, 0.5350),
                (101.4460, 0.5300),
            ), srid=4326),
            commodity="Oil Palm",
            area=25.0,
            year=2016,
            pic=pic_block,
        )

        # A handful of trees inside Block A, on a rough grid — enough to
        # exercise dwithin matching thresholds in the (later) imagery pipeline.
        for i, (lon, lat) in enumerate([
            (101.4410, 0.5310),
            (101.4420, 0.5310),
            (101.4410, 0.5320),
            (101.4420, 0.5320),
        ]):
            Tree.objects.create(
                farm_block=block_a,
                name=f"Tree A{i+1}",
                location=Point(lon, lat, srid=4326),
                source=Tree.SOURCE_MANUAL,
                created_by=pic_block,
                updated_by=pic_block,
            )

        self.stdout.write(self.style.SUCCESS(
            "Seeded demo data: 1 farm, 2 blocks, 4 trees, 3 users "
            "(admin/admin12345, manager/manager12345, pic_block/picblock12345)."
        ))
