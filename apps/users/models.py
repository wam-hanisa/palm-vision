from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model. Kept close to Django's default rather than
    reinventing auth — see docs/01-architecture.md.

    Role model: Super Admin is represented by Django's built-in
    `is_superuser` (global, unscoped, per CLAUDE.md's decisions list).
    Manager and PIC Block are represented by apps.farms.models.UserFarmRole
    (farm-scoped) plus, for PIC Block specifically, direct assignment to a
    FarmBlock via FarmBlock.pic — see apps/common/permissions.py for how
    both are checked together.

    `nik` (Indonesian national ID number) and `phone` are carried over from
    the original app's user fields (see Palm_Vision_PDR.md § 4) — the
    original Laravel app also had `address`/`sex` fields; those are omitted
    here unless a concrete need for them shows up, since they weren't used
    anywhere in the original app's business logic beyond storage.
    """

    nik = models.CharField("national ID (NIK)", max_length=32, blank=True)
    phone = models.CharField(max_length=32, blank=True)

    def __str__(self):
        return self.get_full_name() or self.username
