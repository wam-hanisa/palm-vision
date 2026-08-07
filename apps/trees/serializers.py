from rest_framework import serializers

from .models import Tree


class TreeSerializer(serializers.ModelSerializer):
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
    """

    id = serializers.UUIDField(required=False)
    location_orphaned_warning = serializers.SerializerMethodField()

    class Meta:
        model = Tree
        fields = [
            "id",
            "farm_block",
            "name",
            "location",
            "source",
            "status",
            "current_health",
            "created_by",
            "updated_by",
            "location_orphaned_warning",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["current_health", "created_by", "updated_by", "created_at", "updated_at"]

    def get_location_orphaned_warning(self, obj):
        if not obj.pk:
            return None
        if obj.is_location_orphaned():
            return "This tree's location falls outside every block boundary on its farm — verify the GPS reading."
        return None
