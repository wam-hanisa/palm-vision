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


class HarvestTreeSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    scoped_pk_fields = {"actual_harvest": scoped_actual_harvests, "tree": scoped_trees}

    class Meta:
        model = HarvestTree
        fields = ["id", "actual_harvest", "tree", "quantity", "weight_kg", "recorded_location", "recorded_by", "recorded_at"]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualHarvestSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    effective_quantity = serializers.SerializerMethodField()
    harvest_trees = HarvestTreeSerializer(many=True, read_only=True)
    scoped_pk_fields = {"harvest": scoped_harvests}

    class Meta:
        model = ActualHarvest
        fields = ["id", "harvest", "date", "quantity", "effective_quantity", "harvest_trees", "created_at"]
        read_only_fields = ["created_at"]

    def get_effective_quantity(self, obj):
        return obj.get_effective_quantity()


class HarvestSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    scoped_pk_fields = {"farm_block": scoped_farm_blocks}

    class Meta:
        model = Harvest
        fields = ["id", "farm_block", "assessment_date", "assessment_quantity", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]


class FertilizerTreeSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    scoped_pk_fields = {"actual_fertilizer": scoped_actual_fertilizers, "tree": scoped_trees}

    class Meta:
        model = FertilizerTree
        fields = ["id", "actual_fertilizer", "tree", "quantity", "recorded_by", "recorded_at"]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualFertilizerSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    fertilizer_trees = FertilizerTreeSerializer(many=True, read_only=True)
    scoped_pk_fields = {"fertilizer": scoped_fertilizers}

    class Meta:
        model = ActualFertilizer
        fields = ["id", "fertilizer", "date", "fertilizer_trees", "created_at"]
        read_only_fields = ["created_at"]


class FertilizerSerializer(ScopedPrimaryKeyFieldsMixin, serializers.ModelSerializer):
    scoped_pk_fields = {"farm_block": scoped_farm_blocks}

    class Meta:
        model = Fertilizer
        fields = ["id", "farm_block", "assessment_date", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]
