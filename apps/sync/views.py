"""
Offline pre-caching / incremental sync endpoint. See docs/05-offline-sync.md
step 1 ("pre-caches the PIC Block user's assigned block(s) ... into
IndexedDB") and docs/04-api-spec.md: "GET /api/sync/pull/?since={timestamp}
— Pull server-side changes since last sync — Scoped by the requesting
user's role/farm assignment."

Deliberately reuses each resource's own ViewSet.get_queryset() rather than
re-implementing scoping logic here. Scoping on this endpoint always matches
scoping on the equivalent list endpoint, by construction, because it's the
same code — duplicating the Q-filter logic here would be exactly the kind
of drift risk that caused the cross-scope bug fixed in
apps/common/mixins.py (see that file's docstring).
"""
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.scoping import managed_farm_ids
from apps.farms.serializers import FarmBlockSerializer, FarmSerializer
from apps.farms.views import FarmBlockViewSet, FarmViewSet
from apps.operations.serializers import (
    ActualFertilizerSerializer,
    ActualHarvestSerializer,
    FertilizerSerializer,
    FertilizerTreeSerializer,
    HarvestSerializer,
    HarvestTreeSerializer,
)
from apps.operations.views import (
    ActualFertilizerViewSet,
    ActualHarvestViewSet,
    FertilizerTreeViewSet,
    FertilizerViewSet,
    HarvestTreeViewSet,
    HarvestViewSet,
)
from apps.trees.serializers import TreeSerializer
from apps.trees.views import TreeViewSet

from .models import SyncCheckIn, SyncTombstone


def _scoped_queryset(viewset_cls, request):
    """Instantiate `viewset_cls` just enough to call its get_queryset() —
    the only viewset behavior this endpoint needs — so each resource's
    scoping logic lives in exactly one place (the viewset itself).
    """
    viewset = viewset_cls()
    viewset.request = request
    viewset.kwargs = {}
    viewset.format_kwarg = None
    return viewset.get_queryset()


def _scoped_tombstones(user, since):
    """Deletions of any of the 8 client-cached resources (see
    pwa/js/sync.js's SYNC_KEY_TO_STORE — "farms" is deliberately never
    cached there, so its tombstones would have nothing to reconcile and
    aren't queried at all) that this user was scoped to see, since `since`.

    Mirrors apps/common/scoping.py's scoped_to_user_blocks() rule (pic OR
    a farm they manage) against the snapshot fields SyncTombstone captured
    at delete time (apps/sync/signals.py) rather than a live FK chain,
    since the real row is gone by the time this runs.
    """
    qs = SyncTombstone.objects.filter(deleted_at__gt=since).exclude(resource="farms")
    if not user.is_superuser:
        qs = qs.filter(Q(pic_user_id=user.id) | Q(farm_id__in=managed_farm_ids(user)))
    return [
        {"resource": t.resource, "object_id": str(t.object_id), "deleted_at": t.deleted_at.isoformat()}
        for t in qs
    ]


# (viewset, serializer, response key, field to filter "changed since" on).
# Append-only models (ActualHarvest, HarvestTree, ActualFertilizer,
# FertilizerTree — see docs/05-offline-sync.md § Conflict handling) have no
# updated_at, so their creation timestamp is what "changed since" means for
# them; that's correct, not a workaround, since they're never edited in place.
_SYNC_RESOURCES = [
    (FarmViewSet, FarmSerializer, "farms", "updated_at"),
    (FarmBlockViewSet, FarmBlockSerializer, "farm_blocks", "updated_at"),
    (TreeViewSet, TreeSerializer, "trees", "updated_at"),
    (HarvestViewSet, HarvestSerializer, "harvests", "updated_at"),
    (ActualHarvestViewSet, ActualHarvestSerializer, "actual_harvests", "created_at"),
    (HarvestTreeViewSet, HarvestTreeSerializer, "harvest_trees", "recorded_at"),
    (FertilizerViewSet, FertilizerSerializer, "fertilizers", "updated_at"),
    (ActualFertilizerViewSet, ActualFertilizerSerializer, "actual_fertilizers", "created_at"),
    (FertilizerTreeViewSet, FertilizerTreeSerializer, "fertilizer_trees", "recorded_at"),
]


class SyncPullView(APIView):
    """GET /api/sync/pull/?since=<ISO-8601 timestamp, optional>

    Returns every record the requesting user is scoped to see, across every
    offline-relevant resource, changed since `since` (omit `since` entirely
    for the initial full pre-cache before a field trip — docs/05-offline-sync.md
    step 1).

    `server_time` in the response is the timestamp the client should pass as
    `since` on its *next* pull. It's captured on the server before any of
    the per-resource queries run, and deliberately NOT computed client-side
    (clock skew) or taken as "the latest updated_at seen in this response"
    (a record that changes concurrently, between this request's queries,
    would then be silently skipped forever instead of appearing next pull).

    `tombstones` — added after a real gap was reported ("in pwa log
    harvest previous harvest plan are still showing"): a hard-deleted
    Harvest/Fertilizer/etc. row simply stops appearing in the per-resource
    lists above, but nothing ever told a client that had already synced it
    to remove its own copy. Omitted entirely on the initial full pull
    (`since` absent) — there's nothing to reconcile on a from-scratch
    cache, only on an incremental one. See SyncTombstone's own docstring
    (apps/sync/models.py) for how these get created.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        since = None
        since_param = request.query_params.get("since")
        if since_param:
            since = parse_datetime(since_param)
            if since is None:
                raise ValidationError(
                    {"since": "Must be an ISO-8601 datetime, e.g. 2026-08-01T00:00:00Z."}
                )

        server_time = timezone.now()
        payload = {"server_time": server_time.isoformat()}

        for viewset_cls, serializer_cls, key, ts_field in _SYNC_RESOURCES:
            queryset = _scoped_queryset(viewset_cls, request)
            if since is not None:
                queryset = queryset.filter(**{f"{ts_field}__gt": since})
            payload[key] = serializer_cls(queryset, many=True, context={"request": request}).data

        payload["tombstones"] = _scoped_tombstones(request.user, since) if since is not None else []

        return Response(payload)


class SyncCheckInView(APIView):
    """POST /api/sync/checkin/ — body: {"device_id": str, "pending_count": int}.

    Called by pwa/js/sync.js's runSync() after every sync attempt (success
    or failure alike — a failed attempt with a nonzero outbox is exactly
    the signal apps/notifications/tasks.py's check_stalled_sync() needs).
    Upserts by (user, device_id) — see apps/sync/models.py's docstring for
    why this model exists at all.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        device_id = request.data.get("device_id")
        pending_count = request.data.get("pending_count")
        if not device_id or pending_count is None:
            raise ValidationError({"device_id": "Required.", "pending_count": "Required."})
        try:
            pending_count = int(pending_count)
        except (TypeError, ValueError):
            raise ValidationError({"pending_count": "Must be an integer."})

        SyncCheckIn.objects.update_or_create(
            user=request.user, device_id=device_id, defaults={"pending_count": pending_count}
        )
        return Response({"ok": True})
