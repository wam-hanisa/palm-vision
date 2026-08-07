from rest_framework import serializers
from rest_framework_gis.serializers import GeoFeatureModelSerializer

from .models import Farm, FarmBlock, UserFarmRole


class FarmSerializer(serializers.ModelSerializer):
    class Meta:
        model = Farm
        fields = ["id", "name", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class FarmBlockSerializer(serializers.ModelSerializer):
    """Plain (non-GeoJSON) serializer for list/detail use in contexts that
    don't need the map-ready GeoJSON shape — e.g. offline pre-caching
    payloads where the client already knows how to parse WKT/GeoJSON from
    a plain field. Use FarmBlockGeoSerializer for anything rendered
    directly on a Leaflet map.
    """

    overlap_warning = serializers.SerializerMethodField()

    class Meta:
        model = FarmBlock
        fields = [
            "id",
            "farm",
            "name",
            "boundary",
            "commodity",
            "area",
            "year",
            "pic",
            "tree_match_threshold_m",
            "overlap_warning",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_overlap_warning(self, obj):
        # Non-blocking warning, not a validation error — see
        # docs/12-security-ops.md § Geometry & Data Integrity Validation and
        # FarmBlock.overlapping_blocks(). Only computed for existing
        # instances; a not-yet-saved block has no pk to compare against.
        if not obj.pk:
            return None
        overlaps = obj.overlapping_blocks()
        if not overlaps:
            return None
        return f"Boundary overlaps {len(overlaps)} other block(s) on this farm by more than the tolerance."


class FarmBlockGeoSerializer(GeoFeatureModelSerializer):
    """GeoJSON FeatureCollection-ready serializer, for map views (farm-level
    and block-level navigation — docs/05-offline-sync.md). Requires
    djangorestframework-gis; add it to requirements.txt when the map UI
    is built (Phase 4) if not already present.
    """

    class Meta:
        model = FarmBlock
        geo_field = "boundary"
        fields = ["id", "farm", "name", "commodity", "area", "year", "pic"]


class UserFarmRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserFarmRole
        fields = ["id", "user", "farm", "role", "created_at"]
        read_only_fields = ["created_at"]
