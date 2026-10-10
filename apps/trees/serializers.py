from rest_framework import serializers

from apps.common.scoping import scoped_farm_blocks
from apps.common.serializers import ScopedPrimaryKeyFieldsMixin

from .models import Tree


class TreeSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    """`id` is writable (not read_only) so an offline client can supply a
    client-generated UUID — see apps/common/mixins.py IdempotentUUIDCreateMixin
    and docs/05-offline-sync.md.

    `id` is declared explicitly (rather than left to ModelSerializer's
    auto-generated field) to suppress DRF's automatic UniqueValidator on
    primary-key fields. Without this, resubmitting an existing client UUID
    fails serializer validation ("this field must be unique") before
    IdempotentUUIDCreateMixin's update-or-create logic ever runs — which
    defeats the entire point of the idempotent-create pattern. `required=False`
    preserves the ability to omit `id` on a normal online create and let the
    model's own `default=uuid.uuid4` generate one.

    `scoped_pk_fields`: `farm_block` is restricted to blocks the requesting
    user can actually reach (apps/common/serializers.py) — otherwise a PIC
    Block user could create a Tree in a block they have no access to, since
    DRF's default PrimaryKeyRelatedField only checks the block *exists*.
    """

    id = serializers.UUIDField(required=False)
    location_orphaned_warning = serializers.SerializerMethodField()
    harvest_stage = serializers.SerializerMethodField()
    fertilizer_stage = serializers.SerializerMethodField()
    # Server-assigned (Tree.save()); the field app shows `label` wherever a
    # tree has no name.
    number = serializers.IntegerField(read_only=True)
    label = serializers.CharField(read_only=True)
    scoped_pk_fields = {"farm_block": scoped_farm_blocks}

    class Meta:
        model = Tree
        fields = [
            "id",
            "farm_block",
            "name",
            "number",
            "label",
            "location",
            "source",
            "status",
            "current_health",
            "last_harvested_at",
            "last_fertilized_at",
            "last_harvest_quantity",
            "last_fertilizer_quantity",
            "harvest_marked_at",
            "fertilizer_marked_at",
            "harvest_stage",
            "fertilizer_stage",
            "created_by",
            "updated_by",
            "location_orphaned_warning",
            "created_at",
            "updated_at",
        ]
        # last_harvested_at/last_fertilized_at/harvest_marked_at/
        # fertilizer_marked_at are read-only here on purpose — they're
        # never written through a plain PATCH to this endpoint, only
        # through the dedicated mark_harvested/mark_fertilized actions
        # below (PWA/PIC Block completion) or the office dashboard's own
        # Mark-trees views (direct model .save(), not this serializer).
        # Same reasoning current_health already had: a field with a
        # specific, audited write path shouldn't also be reachable through
        # a general-purpose edit.
        read_only_fields = [
            "current_health",
            "last_harvested_at",
            "last_fertilized_at",
            "last_harvest_quantity",
            "last_fertilizer_quantity",
            "harvest_marked_at",
            "fertilizer_marked_at",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def get_harvest_stage(self, obj):
        return obj.harvest_stage()

    def get_fertilizer_stage(self, obj):
        return obj.fertilizer_stage()

    def get_location_orphaned_warning(self, obj):
        if not obj.pk:
            return None
        if obj.is_location_orphaned():
            return "This tree's location falls outside every block boundary on its farm — verify the GPS reading."
        return None
