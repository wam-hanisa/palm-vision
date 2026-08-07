"""
Shared role/farm-scoping permission classes. See docs/12-security-ops.md
§ Multi-Farm Role Scoping — every DRF permission class in this project should
check "does this user hold a role granting this permission on farm X," not
just "does this user hold role Y" globally, except for Super Admin which is
global by definition (Django is_superuser).

Two levels of scoping are used across this project, matching apps/farms/models.py:
- UserFarmRole (user, farm, role) — farm-level: is this user a Manager (or
  recognized as PIC Block staff) on this farm at all.
- FarmBlock.pic — block-level: which single user is the Person In Charge of
  this specific block (mirrors the original Laravel app's farm_blocks.user_id
  assignment — see Palm_Vision_PDR.md). PIC Block CRUD permissions check this
  directly, not just farm-level role membership, since one PIC Block user can
  be assigned to some blocks on a farm and not others.
"""
from rest_framework.permissions import BasePermission

from apps.farms.models import UserFarmRole


def user_role_on_farm(user, farm):
    """Return 'manager', 'pic_block', or None for how (if at all) `user`
    is scoped to `farm` at the farm level. Super Admins should be checked
    via user.is_superuser before calling this — Super Admin is global and
    not represented as a UserFarmRole row.
    """
    return (
        UserFarmRole.objects.filter(user=user, farm=farm)
        .values_list("role", flat=True)
        .first()
    )


class IsSuperAdminOrManager(BasePermission):
    """Allows Super Admin (global) or a Manager scoped to the object's farm.
    Use for approval actions and other Manager-only state changes — see
    CLAUDE.md: approval must check the actor's role server-side, always,
    unlike the original app's commented-out authorize() calls.
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True
        farm = getattr(obj, "farm", None) or getattr(obj.farm_block, "farm", None)
        if farm is None:
            return False
        return user_role_on_farm(request.user, farm) == "manager"


class IsAssignedPicBlockOrManagerOrSuperAdmin(BasePermission):
    """Read/write access for the PIC Block user directly assigned to a
    FarmBlock (FarmBlock.pic), or any Manager/Super Admin scoped to that
    block's farm. This is what tree CRUD and harvest/fertilizer actuals use —
    see docs/05-offline-sync.md § Field Staff Tree CRUD.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        farm_block = getattr(obj, "farm_block", None)
        if farm_block is None:
            return False
        if request.user.is_superuser:
            return True
        if farm_block.pic_id == request.user.id:
            return True
        return user_role_on_farm(request.user, farm_block.farm) == "manager"
