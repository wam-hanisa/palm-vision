from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.common.scoping import (
    scoped_actual_fertilizers,
    scoped_actual_harvests,
    scoped_farm_blocks,
    scoped_fertilizers,
    scoped_harvests,
    scoped_trees,
)
from apps.common.serializers import ScopedPrimaryKeyFieldsMixin

from .models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree

# NOTE: id = serializers.UUIDField(required=False) is declared explicitly on
# every serializer below whose viewset uses IdempotentUUIDCreateMixin
# (apps/common/mixins.py). ModelSerializer's auto-generated pk field attaches
# a UniqueValidator, which rejects a resubmitted client UUID as "already
# exists" before the mixin's update-or-create logic ever runs — defeating
# the whole point of the idempotent offline-sync pattern. See
# apps/trees/serializers.py TreeSerializer for the fuller explanation; this
# was caught by apps/trees/tests.py::test_resubmitting_same_client_uuid_updates_in_place_not_duplicate.
#
# NOTE: scoped_pk_fields (apps/common/serializers.py) restricts every FK
# below to what the requesting user can actually reach — otherwise DRF's
# default PrimaryKeyRelatedField only checks the referenced row *exists*,
# not that it's in the requester's scope, which was a real gap (see
# BUILD_STATUS.md's "Known gaps") — e.g. a PIC Block user could reference a
# `tree` or `farm_block` outside their assigned block(s).


class SaneAmountsMixin:
    """Rejects nonsense a field worker can type: negative quantities/weights
    and dates in the future. (+1 day of slack so a device whose clock is a
    timezone ahead of the server isn't rejected for "today".) Applies to the
    API, so the PWA's offline-queued writes are checked on sync too.
    """

    def _non_negative(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Must be zero or more.")
        return value

    def validate_quantity(self, value):
        return self._non_negative(value)

    def validate_weight_kg(self, value):
        return self._non_negative(value)

    def validate_date(self, value):
        if value > timezone.localdate() + timedelta(days=1):
            raise serializers.ValidationError("Date can't be in the future.")
        return value


class HarvestTreeSerializer(SaneAmountsMixin, ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    # Read-only derived value — see HarvestTree.effective_quantity's own
    # docstring. `weight_kg` itself stays a plain writable field and can
    # be null (this tree was part of the harvest, not individually
    # measured); effective_quantity is what a client should actually
    # display. `quantity` (bunch count) is optional supplementary detail,
    # no longer part of this computation.
    effective_quantity = serializers.FloatField(read_only=True)
    scoped_pk_fields = {"actual_harvest": scoped_actual_harvests, "tree": scoped_trees}

    class Meta:
        model = HarvestTree
        fields = [
            "id", "actual_harvest", "tree", "quantity", "effective_quantity", "weight_kg",
            "recorded_location", "recorded_by", "recorded_at",
        ]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualHarvestSerializer(SaneAmountsMixin, ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    effective_quantity = serializers.SerializerMethodField()
    harvest_trees = HarvestTreeSerializer(many=True, read_only=True)
    scoped_pk_fields = {"harvest": scoped_harvests}

    class Meta:
        model = ActualHarvest
        # weight_kg is the authoritative total (see get_effective_quantity());
        # quantity (bunch count) is optional supplementary detail only —
        # asked directly to move every harvest record from bunches to kg.
        fields = ["id", "harvest", "date", "weight_kg", "quantity", "effective_quantity", "harvest_trees", "created_at"]
        read_only_fields = ["created_at"]

    def get_effective_quantity(self, obj):
        return obj.get_effective_quantity()


class HarvestSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    # See this module's top-of-file NOTE — HarvestViewSet now uses
    # IdempotentUUIDCreateMixin (a PIC Block's field app can create a plan
    # offline when none exists yet; see PWA's findOrCreatePlan()), so this
    # needs the same explicit override every other idempotent-create
    # serializer here already has.
    id = serializers.UUIDField(required=False)
    scoped_pk_fields = {"farm_block": scoped_farm_blocks}

    class Meta:
        model = Harvest
        fields = ["id", "farm_block", "assessment_date", "assessment_quantity", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]


class FertilizerTreeSerializer(SaneAmountsMixin, ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    # See HarvestTreeSerializer's matching field for why this is read-only
    # and separate from `quantity` itself.
    effective_quantity = serializers.FloatField(read_only=True)
    scoped_pk_fields = {"actual_fertilizer": scoped_actual_fertilizers, "tree": scoped_trees}

    class Meta:
        model = FertilizerTree
        fields = ["id", "actual_fertilizer", "tree", "quantity", "effective_quantity", "recorded_by", "recorded_at"]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualFertilizerSerializer(SaneAmountsMixin, ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    # Mirrors ActualHarvestSerializer's own effective_quantity exactly —
    # see ActualFertilizer.get_effective_quantity()'s docstring.
    effective_quantity = serializers.SerializerMethodField()
    fertilizer_trees = FertilizerTreeSerializer(many=True, read_only=True)
    scoped_pk_fields = {"fertilizer": scoped_fertilizers}

    class Meta:
        model = ActualFertilizer
        fields = ["id", "fertilizer", "date", "quantity", "effective_quantity", "fertilizer_trees", "created_at"]
        read_only_fields = ["created_at"]

    def get_effective_quantity(self, obj):
        return obj.get_effective_quantity()


class FertilizerSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    # See HarvestSerializer's matching field for why this is needed.
    id = serializers.UUIDField(required=False)
    scoped_pk_fields = {"farm_block": scoped_farm_blocks}

    class Meta:
        model = Fertilizer
        fields = ["id", "farm_block", "assessment_date", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]
