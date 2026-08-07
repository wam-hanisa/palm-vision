from rest_framework import serializers

from .models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree

# NOTE: id = serializers.UUIDField(required=False) is declared explicitly on
# every serializer below whose viewset uses IdempotentUUIDCreateMixin
# (apps/common/mixins.py). ModelSerializer's auto-generated pk field attaches
# a UniqueValidator, which rejects a resubmitted client UUID as "already
# exists" before the mixin's update-or-create logic ever runs — defeating
# the whole point of the idempotent offline-sync pattern. See
# apps/trees/serializers.py TreeSerializer for the fuller explanation; this
# was caught by apps/trees/tests.py::test_resubmitting_same_client_uuid_updates_in_place_not_duplicate.


class HarvestTreeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)

    class Meta:
        model = HarvestTree
        fields = ["id", "actual_harvest", "tree", "quantity", "weight_kg", "recorded_location", "recorded_by", "recorded_at"]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualHarvestSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    effective_quantity = serializers.SerializerMethodField()
    harvest_trees = HarvestTreeSerializer(many=True, read_only=True)

    class Meta:
        model = ActualHarvest
        fields = ["id", "harvest", "date", "quantity", "effective_quantity", "harvest_trees", "created_at"]
        read_only_fields = ["created_at"]

    def get_effective_quantity(self, obj):
        return obj.get_effective_quantity()


class HarvestSerializer(serializers.ModelSerializer):
    class Meta:
        model = Harvest
        fields = ["id", "farm_block", "assessment_date", "assessment_quantity", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]


class FertilizerTreeSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)

    class Meta:
        model = FertilizerTree
        fields = ["id", "actual_fertilizer", "tree", "quantity", "recorded_by", "recorded_at"]
        read_only_fields = ["recorded_by", "recorded_at"]


class ActualFertilizerSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(required=False)
    fertilizer_trees = FertilizerTreeSerializer(many=True, read_only=True)

    class Meta:
        model = ActualFertilizer
        fields = ["id", "fertilizer", "date", "fertilizer_trees", "created_at"]
        read_only_fields = ["created_at"]


class FertilizerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fertilizer
        fields = ["id", "farm_block", "assessment_date", "status", "created_at", "updated_at"]
        read_only_fields = ["status", "created_at", "updated_at"]
