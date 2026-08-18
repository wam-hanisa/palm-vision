"""
Centralized "what farm/block-scoped data can this user reach" queries — the
single source of truth for read scoping (every viewset's get_queryset())
and, since this module was added, write scoping too (apps/common/serializers.py's
ScopedPrimaryKeyFieldsMixin restricts a PrimaryKeyRelatedField's queryset to
these same functions). This closes the gap flagged in BUILD_STATUS.md's
"Known gaps": DRF's default PrimaryKeyRelatedField only checks that a
referenced row *exists*, not that the requesting user is allowed to
reference it — so, before this module existed, a PIC Block user could POST
a Tree naming a farm_block they have no access to (or a HarvestTree naming
another farm's tree) and it would succeed. The read-side scoping every
get_queryset() already enforced was meaningless on the write path.

Centralizing this logic (rather than each app re-implementing an identical
Q-filter locally, which is how several of these existed before this module)
is deliberate — see apps/sync/views.py's docstring and apps/common/mixins.py's
docstring for the same principle applied elsewhere in this project: two
copies of the same scoping rule are a drift risk waiting to happen the
first time one gets updated and the other doesn't.

Deliberately NOT unified further into a single "scoped_to_user_farms"
covering every app: farm-level scoping actually differs by model in ways
that are correct, not accidental — e.g. GrievanceRecord (compliance) is
Manager-only since it has no PIC-block concept, while ReportDefinition
allows any PIC-of-a-block-on-that-farm too. Those stay as local,
app-specific helpers; only the genuinely identical block-level pattern
(pic OR managed-farm) is centralized here.
"""
from django.db.models import Q

from apps.farms.models import Farm, FarmBlock, UserFarmRole


def managed_farm_ids(user):
    """Farm ids where `user` holds the Manager role. Super Admin isn't
    represented here — callers should check user.is_superuser separately,
    same convention as apps/common/permissions.py's user_role_on_farm().
    """
    return UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).values_list("farm_id", flat=True)


def member_farm_ids(user):
    """Farm ids where `user` holds *any* UserFarmRole — the broadest
    "can see this farm at all" check, used by FarmViewSet/ImageViewSet.
    """
    return UserFarmRole.objects.filter(user=user).values_list("farm_id", flat=True)


def scoped_farms(user, queryset=None):
    """Farms `user` can see/reference at all — Super Admin: every farm;
    everyone else: farms they hold any UserFarmRole on.
    """
    qs = Farm.objects.all() if queryset is None else queryset
    if user.is_superuser:
        return qs
    return qs.filter(id__in=member_farm_ids(user))


def scoped_farm_blocks(user, queryset=None):
    """Blocks `user` can see/reference: Super Admin — all; PIC Block — the
    block(s) they're directly assigned via FarmBlock.pic; Manager — every
    block on a farm they manage.
    """
    qs = FarmBlock.objects.all() if queryset is None else queryset
    if user.is_superuser:
        return qs
    return qs.filter(Q(pic=user) | Q(farm_id__in=managed_farm_ids(user)))


def scoped_to_user_blocks(queryset, user, block_field="farm_block"):
    """Generic version of scoped_farm_blocks() for models that reference a
    FarmBlock via FK rather than being FarmBlock itself — `block_field` is
    the lookup path to that relation (e.g. 'farm_block', 'harvest__farm_block').
    """
    if user.is_superuser:
        return queryset
    return queryset.filter(
        Q(**{f"{block_field}__pic": user}) | Q(**{f"{block_field}__farm_id__in": managed_farm_ids(user)})
    )


def scoped_trees(user):
    from apps.trees.models import Tree

    return scoped_to_user_blocks(Tree.objects.all(), user, block_field="farm_block")


def scoped_harvests(user):
    from apps.operations.models import Harvest

    return scoped_to_user_blocks(Harvest.objects.all(), user, block_field="farm_block")


def scoped_fertilizers(user):
    from apps.operations.models import Fertilizer

    return scoped_to_user_blocks(Fertilizer.objects.all(), user, block_field="farm_block")


def scoped_actual_harvests(user):
    from apps.operations.models import ActualHarvest

    return scoped_to_user_blocks(ActualHarvest.objects.all(), user, block_field="harvest__farm_block")


def scoped_actual_fertilizers(user):
    from apps.operations.models import ActualFertilizer

    return scoped_to_user_blocks(ActualFertilizer.objects.all(), user, block_field="fertilizer__farm_block")
