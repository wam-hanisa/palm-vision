"""
Tombstone recording for hard deletes of any offline-synced resource — see
SyncTombstone's own docstring (apps/sync/models.py) for why this exists.
Connected once, via SyncConfig.ready() importing this module (apps/sync/apps.py)
— apps/sync is the one app that's supposed to know about every other domain
app's models for sync purposes (apps/sync/views.py's _SYNC_RESOURCES already
does the same thing), so hooking these signals here rather than scattering a
receiver into each domain app keeps that direction of dependency the same as
everywhere else in this file. Importing apps.operations.models directly at
module level here is safe despite "apps/sync" loading after "apps/operations"
in INSTALLED_APPS (palmvision/settings/base.py): AppConfig.ready() — which is
what imports this module — only ever runs after every app's models are
already loaded, for every app, regardless of INSTALLED_APPS order.

pre_delete (not post_delete) deliberately: a receiver here needs to
traverse each instance's own FK chain (e.g. HarvestTree -> actual_harvest
-> harvest -> farm_block) to snapshot its farm/pic scoping. For a plain
single-row delete that chain is intact either way, but for a *cascaded*
delete (e.g. deleting a FarmBlock cascades to every Harvest/Tree/etc. row
in it), Django's collector sends pre_delete for children before actually
removing any rows from the database, so every FK lookup below is
guaranteed to still resolve; post_delete would risk the parent row already
being gone by the time a child's signal fires.

Every receiver is a plain top-level function, not a closure — @receiver's
default `weak=True` only keeps a *weak* reference to the function it
connects, and a closure with no other referent (as an earlier version of
this module had, defining these inside a function called once from
SyncConfig.ready()) gets garbage-collected the moment that enclosing call
returns, silently disconnecting the receiver with no error anywhere. A
receiver bound to a module-level name stays referenced for the life of the
process, same as FarmBlock/Tree's below.

Farm itself is deliberately not covered: no dashboard delete path creates
one to begin with, and pwa/js/sync.js's SYNC_KEY_TO_STORE never caches
Farm records client-side at all (see its own comment), so a Farm tombstone
would have nothing to reconcile.
"""
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.farms.models import FarmBlock
from apps.operations.models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree
from apps.trees.models import Tree

from .models import SyncTombstone


def _record(resource, instance, block):
    """`block` is the FarmBlock the deleted row was scoped to (or None,
    which just means an orphaned row is deleted with no scoping snapshot
    — harmless since it'll then only ever be visible to a Super Admin).
    """
    SyncTombstone.objects.create(
        resource=resource,
        object_id=instance.pk,
        farm_id=block.farm_id if block is not None else None,
        pic_user_id=block.pic_id if block is not None else None,
    )


@receiver(pre_delete, sender=FarmBlock)
def _tombstone_farm_block(sender, instance, **kwargs):
    _record("farm_blocks", instance, instance)


@receiver(pre_delete, sender=Tree)
def _tombstone_tree(sender, instance, **kwargs):
    _record("trees", instance, instance.farm_block)


@receiver(pre_delete, sender=Harvest)
def _tombstone_harvest(sender, instance, **kwargs):
    _record("harvests", instance, instance.farm_block)


@receiver(pre_delete, sender=Fertilizer)
def _tombstone_fertilizer(sender, instance, **kwargs):
    _record("fertilizers", instance, instance.farm_block)


@receiver(pre_delete, sender=ActualHarvest)
def _tombstone_actual_harvest(sender, instance, **kwargs):
    _record("actual_harvests", instance, instance.harvest.farm_block)


@receiver(pre_delete, sender=ActualFertilizer)
def _tombstone_actual_fertilizer(sender, instance, **kwargs):
    _record("actual_fertilizers", instance, instance.fertilizer.farm_block)


@receiver(pre_delete, sender=HarvestTree)
def _tombstone_harvest_tree(sender, instance, **kwargs):
    _record("harvest_trees", instance, instance.actual_harvest.harvest.farm_block)


@receiver(pre_delete, sender=FertilizerTree)
def _tombstone_fertilizer_tree(sender, instance, **kwargs):
    _record("fertilizer_trees", instance, instance.actual_fertilizer.fertilizer.farm_block)
