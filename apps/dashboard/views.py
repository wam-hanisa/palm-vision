"""
Office dashboard — docs/09-design-system.md's "Office dashboard (Manager/
Super Admin, mostly desktop): sidebar nav, metric cards, map panel" and
docs/10-user-journeys.md's Manager journey. Server-rendered Django views
(session auth via django.contrib.auth, not the JWT API) — deliberately a
third, distinct surface from both the DRF API (apps/*/views.py) and the
offline-first PWA (pwa/, /field/), matching docs/01-architecture.md's
three-client model. Every prior build phase left this API-only; this is
the first real frontend for it.

Every metric/list below reuses the *exact same* get_queryset() scoping the
DRF API already enforces for that resource, by instantiating the relevant
viewset just enough to call get_queryset() — the same technique
apps/sync/views.py's SyncPullView already uses (see that file's docstring),
for the same reason: two copies of the same scoping rule are a drift risk,
one function used from two places is not.

The map panel (`MapView`, docs/09-design-system.md) is read-only by
design, mirroring docs/06-ml-pipeline.md's UI rule that there is "no
global/bare map view for reviewing imagery or triggering detection" —
every action endpoint in this app already requires a block id in the URL
(BlockImageryView, TriggerProcessingView, etc.); the map's popups link
into those existing per-block pages rather than adding any new bare
"act on whatever's under the pin" shortcut.
"""
import json as _json
import uuid
from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.gis.geos import GEOSGeometry, Point
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.generic import TemplateView, View

from apps.common.geo import geometry_to_6dp_geojson
from apps.common.permissions import user_role_on_farm
from apps.common.scoping import scoped_farm_blocks, scoped_to_user_blocks
from apps.compliance import services as compliance_services
from apps.compliance.models import ComplianceDocument, DeforestationBaseline, GrievanceRecord
from apps.compliance.views import (
    ComplianceAuditViewSet,
    ComplianceDocumentViewSet,
    DeforestationBaselineViewSet,
    GrievanceRecordViewSet,
    HarvestBatchViewSet,
)
from apps.farms.models import FarmBlock, UserFarmRole
from apps.farms.views import FarmBlockViewSet, FarmViewSet
from apps.imagery import services as imagery_services
from apps.imagery.models import Image, ImageProcessingRun, ImageUploadSession, TreeDetection, TreeHealthClassification
from apps.imagery.tasks import georeference_image_task
from apps.imagery.views import ImageViewSet
from apps.notifications.models import Notification
from apps.operations.models import ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree, PlanStatus
from apps.operations.services import (
    advance_status,
    awaiting_approval,
    can_advance,
    has_actual_record,
    record_actual_progress,
)
from apps.operations.views import ActualHarvestViewSet, FertilizerViewSet, HarvestViewSet
from apps.reporting.models import ReportRun
from apps.reporting.tasks import generate_report_task
from apps.reporting.views import ReportDefinitionViewSet
from apps.trees.models import Tree, TreeCareRecord
from apps.trees.views import TreeViewSet

from .forms import (
    ComplianceDocumentForm,
    DashboardAuthenticationForm,
    FarmBlockForm,
    FertilizerPlanForm,
    GrievanceRecordForm,
    HarvestPlanForm,
    ImageUploadForm,
    ReportDefinitionForm,
    TreeForm,
)


# Used to build URL *templates* for JS to substitute a real id into at
# runtime — reverse() needs a real-shaped UUID to resolve the <uuid:...>
# path converter, but the actual id isn't known until something is clicked
# on the map. Same trick imagery_list.html's own JS already uses for the
# confirm-blocks redirect (see that file's comment) — a template string
# built from a real reverse() call, not a hardcoded path, so it still
# points at the right place if a URL pattern ever changes. Never used as
# an actual id; always replaced before a request is made.
_URL_PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"

# Second placeholder, for the handful of URLs below with two dynamic UUID
# segments (block_id + a plan/actual id) — JS's buildUrl() only replaces
# one placeholder, so a two-segment URL needs a second, distinct token to
# substitute against (see buildUrl2() in farm_map.html).
_URL_PLACEHOLDER_UUID_2 = "11111111-1111-1111-1111-111111111111"


def _build_image_confirm_data(image):
    """The image-footprint-vs-block-boundaries payload for the imagery
    upload-confirm step — shared by ImageConfirmBlocksView (the original,
    still-reachable standalone page) and FarmMapView (which now shows the
    same step inline on the main map, per the "GIS-first" redesign — see
    FarmMapView's own docstring). Pulled out into one function specifically
    so those two call sites can't quietly drift apart on what "suggested"
    means or what fields the payload has.

    Returns None if the image isn't georeferenced yet — both call sites
    already handle "still waiting" as a distinct state and don't need map
    data for it.
    """
    if image.georeference_status != Image.GEOREFERENCE_DONE:
        return None
    blocks = FarmBlock.objects.filter(farm=image.farm, boundary__isnull=False)
    return {
        "image": {
            "id": str(image.id),
            "farm_id": str(image.farm_id),
            "preview_url": image.preview_file.url if image.preview_file else None,
            # image.footprint can in principle still be null even once
            # georeference_status is Done (it's a separately-nullable
            # field — see Image.footprint's own help_text) — guarded
            # rather than assumed, since geometry_to_6dp_geojson() would
            # raise on None (it calls straight into geometry.geojson).
            "geometry": geometry_to_6dp_geojson(image.footprint) if image.footprint else None,
        },
        "blocks": [
            {
                "id": str(block.id),
                "name": block.name,
                "geometry": geometry_to_6dp_geojson(block.boundary),
                "suggested": image.footprint is not None and block.boundary.intersects(image.footprint),
            }
            for block in blocks
        ],
    }


def _viewset_queryset(viewset_cls, request):
    """Instantiate `viewset_cls` just enough to call its get_queryset() —
    see module docstring for why this dashboard leans on the API's own
    scoping rather than re-deriving it.
    """
    viewset = viewset_cls()
    viewset.request = request
    viewset.kwargs = {}
    viewset.format_kwarg = None
    return viewset.get_queryset()


class DashboardLoginView(LoginView):
    form_class = DashboardAuthenticationForm
    template_name = "dashboard/login.html"
    redirect_authenticated_user = True


class DashboardLogoutView(LogoutView):
    next_page = "dashboard:login"


class ManagerOrSuperAdminRequiredMixin(LoginRequiredMixin):
    """Every dashboard view re-checks office-staff standing, not just the
    login form — defense in depth against a session reaching this app some
    other way (e.g. a browser profile also signed into /admin/ as a
    PIC-Block-only user, which /admin/ itself would refuse via is_staff,
    but this app has its own, separate gate — see apps/dashboard/forms.py).
    """

    login_url = "dashboard:login"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self._require_office_staff(request.user)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _require_office_staff(user):
        if user.is_superuser:
            return
        if UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).exists():
            return
        raise PermissionDenied("This dashboard is for Managers and Super Admins.")


class HomeView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """docs/10-user-journeys.md Manager journey step 1: "Morning digest.
    Notification summarizing pending approvals, health anomalies,
    compliance expiry — pull becomes push." This is the pull side of that —
    a Manager opening the dashboard sees the same things the push
    notifications (apps/notifications/tasks.py) already alert on, in one
    place, scoped to what they're actually responsible for.
    """

    template_name = "dashboard/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request

        pending_harvests = awaiting_approval(_viewset_queryset(HarvestViewSet, request)).count()
        pending_fertilizers = awaiting_approval(_viewset_queryset(FertilizerViewSet, request)).count()

        review_queryset = scoped_to_user_blocks(
            TreeDetection.objects.filter(
                match_status__in=[TreeDetection.MATCH_NEW_CANDIDATE, TreeDetection.MATCH_AMBIGUOUS],
                tree__isnull=True,  # same filter ReviewQueueView uses; without it, confirmed detections count forever
            ),
            request.user,
            block_field="processing_run__farm_block",
        )
        review_queue_count = review_queryset.count()
        # Link the card straight to a run that has something to review,
        # not the Imagery list, which shows no sign of the waiting items.
        review_run_id = (
            review_queryset.order_by("-processing_run__created_at")
            .values_list("processing_run_id", flat=True).first()
        )

        # ComplianceDocument.status is a computed property, not a DB field
        # (apps/compliance/models.py — deliberate, so it's always current,
        # never stale from a save-time snapshot), so it's counted in Python
        # against the already-scoped queryset rather than filtered in SQL.
        compliance_attention_count = sum(
            1
            for doc in _viewset_queryset(ComplianceDocumentViewSet, request)
            if doc.status in (ComplianceDocument.STATUS_EXPIRING_SOON, ComplianceDocument.STATUS_EXPIRED)
        )

        unread_notifications = Notification.objects.filter(user=request.user, read=False).count()

        farms = list(_viewset_queryset(FarmViewSet, request))
        blocks = _viewset_queryset(FarmBlockViewSet, request)

        context.update(
            {
                "pending_harvests": pending_harvests,
                "pending_fertilizers": pending_fertilizers,
                "pending_approvals_total": pending_harvests + pending_fertilizers,
                "review_queue_count": review_queue_count,
                "review_run_id": review_run_id,
                "compliance_attention_count": compliance_attention_count,
                "unread_notifications": unread_notifications,
                "farm_count": len(farms),
                "block_count": blocks.count(),
                "chart_data": self._chart_data(request),
            }
        )
        return context

    def _chart_data(self, request):
        """Yield, tree-health, and tree-quantity charts — this page used to
        be nothing but plain metric-card numbers (pending approvals, review
        queue, compliance, notifications, farm/block counts); a Manager
        asked for real charts covering yield, health condition, and tree
        quantities, which this page had none of at all. Rendered client-side
        with Chart.js (CDN — same accepted exception this app already makes
        for Leaflet/tile imagery, see map.html's own comment).

        Yield is deliberately aggregated in Python, not a single nested SQL
        aggregate query — calls `ActualHarvest.get_effective_quantity()`
        itself per row (the model's own authoritative "manual total wins
        whenever set, tree-sum is only a fallback when no total was ever
        given" rule) rather than annotating a `Sum("harvest_trees__quantity")`
        and picking between the two here. That annotate-then-override shape
        looks equivalent but isn't: Django's Sum silently ignores NULL rows
        and still returns a real, non-None number the moment even one
        sibling HarvestTree has a quantity, which would make a manually-
        reported total get quietly overridden by a partial tree-sum again —
        exactly the bug get_effective_quantity() exists to prevent (see its
        own docstring). This was a real, confirmed bug in this exact chart:
        typing a 900-total action with only 2 of 3 trees individually
        measured (500+200) summed to 700 here before this fix.
        Flagged here as a known scaling limit, not a hidden trade-off: if
        this dashboard is ever used somewhere with years of dense per-tree
        harvest history, this should move to a database-side rollup
        (e.g. a materialized monthly-yield table) instead.
        """
        from datetime import timedelta

        from django.db.models import Count

        cutoff = timezone.now().date() - timedelta(days=180)
        actual_harvests = _viewset_queryset(ActualHarvestViewSet, request).filter(date__gte=cutoff)
        yield_by_month = defaultdict(float)
        for ah in actual_harvests:
            effective = ah.get_effective_quantity()
            if effective is not None:
                yield_by_month[ah.date.replace(day=1).isoformat()] += effective
        yield_months = sorted(yield_by_month)

        active_trees = _viewset_queryset(TreeViewSet, request).filter(status=Tree.STATUS_ACTIVE)
        health_counts = dict(
            active_trees.values_list("current_health").annotate(count=Count("id")).order_by()
        )
        health_labels = dict(Tree.HEALTH_CHOICES)

        # Top 10 blocks by tree count, not every block unconditionally — a
        # farm with dozens of blocks would make an unreadable bar chart
        # otherwise; "top 10" is a display choice for this chart only, it
        # doesn't affect farm_count/block_count above or anything else.
        blocks_by_tree_count = list(
            active_trees.values("farm_block__name", "farm_block__farm__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        return {
            "yield": {
                "labels": yield_months,
                "values": [round(yield_by_month[m], 1) for m in yield_months],
            },
            "health": {
                "labels": [health_labels.get(key, key) for key in health_counts],
                "values": list(health_counts.values()),
            },
            "tree_quantities": {
                "labels": [
                    f"{row['farm_block__farm__name']} — {row['farm_block__name']}" for row in blocks_by_tree_count
                ],
                "values": [row["count"] for row in blocks_by_tree_count],
            },
        }


def _bad_amounts_message(date, quantities):
    """A message if the chosen date is in the future or any typed quantity is
    negative — checked server-side because the form's min/max attributes are
    only a convenience."""
    if date > timezone.localdate():
        return "The date can't be in the future."
    for raw in (quantities or {}).values():
        try:
            if raw not in (None, "") and float(raw) < 0:
                return "Quantities can't be negative."
        except (TypeError, ValueError):
            continue
    return None


def _is_ajax(request):
    """The one signal every write-action fetch() call on the Farm Map page
    sets (see farm_map.html's postJson()/getJson() helpers) to ask for a
    JSON response instead of a full page — a plain header, not a query
    param, so a direct URL visit (the fallback path every CRUD view below
    still supports) always gets the real page. Same convention jQuery/HTMX
    popularized; nothing framework-specific needed for one header check.
    """
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _recent_yield_by_block(block_ids):
    """Recent per-block yield history (date + total), grouped in Python —
    surfaces data that was already being recorded by the harvest-logging
    pipeline (ActualHarvest/HarvestTree, apps/operations/models.py) but had
    no dashboard visibility until now; no new model, this is read-only.

    Bounded two ways: an overall [:500] cap on the query (most recent
    across every requested block combined) before the per-block grouping,
    then 10 rows kept per block after grouping — same "read everything in
    Python, flagged as a known scaling limit rather than a hidden one"
    trade-off HomeView._chart_data() already documents for yield
    aggregation, not a new pattern. A farm with years of dense per-block
    harvest history would eventually want this pushed to a database-side
    query instead.

    Update — asked directly for a compliance-grade maker-checker flow:
    ActualHarvest.get_effective_quantity() now returns None until the
    parent Harvest plan is approved to "done" (see that method's own
    docstring). This list shows every entry either way — using
    get_reported_quantity() (the real, always-visible figure) rather than
    the possibly-None effective one — with `pending_approval` marking the
    ones that don't count toward official yield yet, so a Manager can see
    what's waiting on them instead of it silently disappearing. The
    per-block/farm "All-time" totals (_yield_totals_by_block() below)
    still use the gated get_effective_quantity(), so this list and that
    total deliberately don't always agree — the gap between them IS the
    "pending approval" backlog.

    Update — "in yield history there are no approve button": a pending
    entry's parent plan needed approving from the "Harvest plans" list
    instead, which isn't where a Manager scanning this list would look.
    Each entry now also carries `harvest_id` and `can_approve` (same
    can_advance() rule _plan_entries() uses — has_actual_record() is
    skipped here since this *is* an actual record for that plan, so it's
    always already satisfied) so the template can render an Approve
    action right on the row itself, posting to the same
    HarvestPlanApproveView either list already uses.
    """
    by_block = {}
    actual_harvests = (
        ActualHarvest.objects.filter(harvest__farm_block_id__in=block_ids)
        .select_related("harvest")
        .order_by("-date")[:500]
    )
    for actual in actual_harvests:
        bucket = by_block.setdefault(actual.harvest.farm_block_id, [])
        if len(bucket) < 10:
            pending = actual.harvest.status != PlanStatus.DONE
            bucket.append({
                "id": str(actual.id),
                "date": actual.date.isoformat(),
                "quantity": actual.get_reported_quantity(),
                "pending_approval": pending,
                "harvest_id": str(actual.harvest_id),
                "can_approve": pending and can_advance(actual.harvest.status),
            })
    return by_block


def _recent_fertilizer_by_block(block_ids):
    """Fertilizer's counterpart to _recent_yield_by_block() above — same
    shape, same bounding, so the new "Fertilizer applications" list (and
    its Edit/Delete actions) has something to read from, the same way the
    harvest side already did before this round.
    """
    by_block = {}
    actual_fertilizers = (
        ActualFertilizer.objects.filter(fertilizer__farm_block_id__in=block_ids)
        .select_related("fertilizer")
        .order_by("-date")[:500]
    )
    for actual in actual_fertilizers:
        bucket = by_block.setdefault(actual.fertilizer.farm_block_id, [])
        if len(bucket) < 10:
            pending = actual.fertilizer.status != PlanStatus.DONE
            bucket.append({
                "id": str(actual.id),
                "date": actual.date.isoformat(),
                "pending_approval": pending,
                "fertilizer_id": str(actual.fertilizer_id),
                "can_approve": pending and can_advance(actual.fertilizer.status),
                "quantity": actual.get_reported_quantity(),
            })
    return by_block


def _yield_totals_by_block(block_ids):
    """All-time per-block harvest total (sum of every ActualHarvest's own
    get_effective_quantity()) — powers the new yield-summary line on the
    Farm Map's block/farm panels.

    Calls get_effective_quantity() itself (the model's own authoritative
    "manual total wins whenever set, tree-sum is only a fallback" rule —
    apps/operations/models.py) per row, rather than annotating a
    `Sum("harvest_trees__quantity")` and picking between the two here —
    that annotate-then-override shape looks equivalent but isn't: Django's
    Sum silently ignores NULL rows and still returns a real (non-None)
    number the moment even ONE sibling HarvestTree has a quantity, which
    would make a manually-reported total get quietly overridden by a
    partial tree-sum again — exactly the bug get_effective_quantity() was
    written to fix. Confirmed against this bug directly: 500+200 typed on
    two trees, a manual total of 900 on the action itself, summed to 700
    before this fix. Same Python-side "read what's needed, don't try to
    re-aggregate an already-derived value in SQL" trade-off
    _recent_yield_by_block() above already uses and documents.

    Update — get_effective_quantity() now also gates on plan approval
    (returns None until the Harvest plan is "done" — see that method's
    own docstring), so this total only ever reflects approved yield. This
    is deliberate, not a side effect to work around: _recent_yield_by_block()
    above shows every entry, approved or not, specifically so this total
    and that list visibly disagree while something is still pending.
    """
    totals = defaultdict(float)
    actual_harvests = ActualHarvest.objects.filter(harvest__farm_block_id__in=block_ids).select_related("harvest")
    for actual in actual_harvests:
        effective = actual.get_effective_quantity()
        if effective is not None:
            totals[actual.harvest.farm_block_id] += effective
    return totals


def _fertilizer_totals_by_block(block_ids):
    """Fertilizer's counterpart to _yield_totals_by_block() above — same
    reasoning, mirrored for ActualFertilizer/FertilizerTree.
    """
    totals = defaultdict(float)
    actual_fertilizers = (
        ActualFertilizer.objects.filter(fertilizer__farm_block_id__in=block_ids).select_related("fertilizer")
    )
    for actual in actual_fertilizers:
        effective = actual.get_effective_quantity()
        if effective is not None:
            totals[actual.fertilizer.farm_block_id] += effective
    return totals


def _rate_summary(total, area_ha, rate_total=None):
    """Shared shape for every yield/fertilizer summary sent to the Farm Map
    (block-level and farm-level alike) — total, the area it's measured
    against (None if never recorded), and the derived per-hectare rate
    (None, never a fabricated 0, when there's no area to divide by — same
    "don't guess from missing data" principle effective_quantity already
    follows).
    """
    return {
        "total": round(total, 2),
        "area_ha": area_ha,
        # rate_total: only the part of `total` from blocks that have an area
        # (farm rollups), so blocks without one don't inflate the per-ha rate.
        "per_ha": round((total if rate_total is None else rate_total) / area_ha, 2) if area_ha else None,
    }


def _plan_has_actual(plan):
    """Whether `plan` already has a recorded actual harvest/fertilizer —
    see apps.operations.services.has_actual_record() for why this matters
    (it's what gates the done_wait -> done transition there). Prefers the
    `actual_count` annotation set on harvest_plans_by_block/
    fertilizer_plans_by_block's querysets below, to avoid one extra query
    per plan; falls back to a direct .exists() check for a plan that
    wasn't annotated (e.g. reused from somewhere else in the future).
    """
    count = getattr(plan, "actual_count", None)
    if count is not None:
        return count > 0
    return has_actual_record(plan)


def _plan_entries(plans):
    return [
        {
            "id": str(plan.id),
            "assessment_date": plan.assessment_date.isoformat(),
            "assessment_quantity": getattr(plan, "assessment_quantity", None),
            "status": plan.status,
            "status_display": plan.get_status_display(),
            # can_advance(status) alone isn't enough — advance_status()
            # (apps/operations/services.py) now refuses EVERY approval
            # step, not just done_wait -> done, with no actual record yet
            # (services.has_actual_record()), so the button here has to
            # reflect the same rule or it'd show as clickable and then
            # fail server-side. Reported directly: a bare plan with zero
            # actuals could still be approved created -> created_confirmed
            # — "there are no need approve for harvest plan, only approve
            # harvest plan that already inputted by the pic block."
            "can_approve": can_advance(plan.status) and _plan_has_actual(plan),
            # Lets the template explain *why* the button is missing,
            # rather than a Manager just not seeing it with no context.
            "needs_actual_to_approve": can_advance(plan.status) and not _plan_has_actual(plan),
        }
        for plan in plans
    ]


def _build_farm_map_data(request):
    """Every farm/block/tree/image the requesting user has scope over,
    shaped for the Farm Map page — the single source of truth both
    FarmMapView's initial page load AND every write action below
    (FarmBlockCreateView, TreeCreateView, etc.) call after a successful
    AJAX save, so the map/panel can refresh from one consistent payload
    instead of trying to patch individual layers in place. Pulled out of
    FarmMapView.get_context_data() specifically so those two call sites
    can't drift apart on what a "block"/"tree"/"image" entry looks like —
    same reasoning as _build_image_confirm_data() above.
    """
    farms = list(_viewset_queryset(FarmViewSet, request).order_by("name"))
    blocks = (
        _viewset_queryset(FarmBlockViewSet, request)
        .select_related("farm", "pic")
        .annotate(
            tree_count=Count("trees", filter=Q(trees__status=Tree.STATUS_ACTIVE), distinct=True)
        )
        .order_by("farm__name", "name")
    )
    trees = (
        _viewset_queryset(TreeViewSet, request)
        .filter(status=Tree.STATUS_ACTIVE)
        .select_related("farm_block", "harvest_marked_by", "fertilizer_marked_by")
    )
    images = list(
        _viewset_queryset(ImageViewSet, request)
        .filter(footprint__isnull=False)
        .select_related("farm")
        .order_by("-captured_at")
    )
    block_ids = [b.id for b in blocks]
    recent_yield_by_block = _recent_yield_by_block(block_ids)
    recent_fertilizer_by_block = _recent_fertilizer_by_block(block_ids)
    yield_totals_by_block = _yield_totals_by_block(block_ids)
    fertilizer_totals_by_block = _fertilizer_totals_by_block(block_ids)

    harvest_plans_by_block = defaultdict(list)
    harvest_plans_qs = (
        Harvest.objects.filter(farm_block_id__in=block_ids)
        # actual_count feeds _plan_has_actual() in _plan_entries() below —
        # annotated here so it's one query for every plan in this request
        # rather than one .exists() query per plan.
        .annotate(actual_count=Count("actual_harvests", distinct=True))
        .order_by("-assessment_date")
    )
    for plan in harvest_plans_qs:
        harvest_plans_by_block[plan.farm_block_id].append(plan)
    fertilizer_plans_by_block = defaultdict(list)
    fertilizer_plans_qs = (
        Fertilizer.objects.filter(farm_block_id__in=block_ids)
        .annotate(actual_count=Count("actual_fertilizers", distinct=True))
        .order_by("-assessment_date")
    )
    for plan in fertilizer_plans_qs:
        fertilizer_plans_by_block[plan.farm_block_id].append(plan)

    # Which uploaded images have actually been PROCESSED against each
    # block, and (when the clip succeeded) their block-scoped clipped
    # preview/geometry — both driven by real ImageProcessingRun rows in one
    # combined query, not a footprint-overlap guess.
    #
    # Update: this used to be a boundary.intersects(image.footprint) check
    # — reported directly, with real data, that this "signed" an image into
    # every block its whole footprint merely reaches, whether or not
    # anything was ever processed there: one Pix4D flight's footprint
    # legitimately touches several blocks, but only the block(s) a Manager
    # actually chose to process it against should ever treat it as
    # relevant. Powers the block panel's imagery picker (farm_map.html) —
    # a Manager now only ever sees images actually processed against this
    # specific block, never a geometry-based suggestion. (Upload-time block
    # *suggestions* — ImageConfirmBlocksView's own overlap-based
    # pre-checking — are a different, still-overlap-based concern
    # deliberately left alone: suggesting what to process next isn't the
    # same as treating an image as already belonging to a block.)
    processed_image_ids_by_block = defaultdict(list)
    seen_images_by_block = defaultdict(set)
    clipped_images_by_block = defaultdict(list)
    for run in (
        ImageProcessingRun.objects.filter(farm_block_id__in=block_ids)
        .select_related("image")
        .order_by("-completed_at")
    ):
        block_key = str(run.farm_block_id)
        if run.image_id not in seen_images_by_block[block_key]:
            seen_images_by_block[block_key].add(run.image_id)
            processed_image_ids_by_block[block_key].append(str(run.image_id))
        # Excludes runs where the clip-persist step never produced a file
        # (run.clip_persist_error set, or the run predates this feature) —
        # renderMap() (farm_map.html) shows nothing for those rather than
        # falling back to the whole image, per its own updated docstring.
        if run.clipped_preview_file and run.clipped_footprint:
            clipped_images_by_block[block_key].append(
                {
                    "image_id": str(run.image_id),
                    "preview_url": run.clipped_preview_file.url,
                    "geometry": geometry_to_6dp_geojson(run.clipped_footprint),
                }
            )

    # Farm-level yield/fertilizer rollups — summed from each farm's own
    # blocks, not a separate query. area_ha only counts blocks that have one
    # recorded (a block with no area contributes to the total but not the
    # denominator, same "don't dilute/fabricate from missing data"
    # principle _rate_summary() above already documents).
    farm_yield_total = defaultdict(float)
    farm_fertilizer_total = defaultdict(float)
    farm_area_ha = defaultdict(float)
    farm_yield_rated = defaultdict(float)
    farm_fertilizer_rated = defaultdict(float)
    for block in blocks:
        farm_yield_total[block.farm_id] += yield_totals_by_block.get(block.id, 0.0)
        farm_fertilizer_total[block.farm_id] += fertilizer_totals_by_block.get(block.id, 0.0)
        if block.area:
            farm_area_ha[block.farm_id] += block.area
            farm_yield_rated[block.farm_id] += yield_totals_by_block.get(block.id, 0.0)
            farm_fertilizer_rated[block.farm_id] += fertilizer_totals_by_block.get(block.id, 0.0)

    return {
        "farms": [
            {
                "id": str(farm.id),
                "name": farm.name,
                "yield_summary": _rate_summary(
                    farm_yield_total.get(farm.id, 0.0), farm_area_ha.get(farm.id) or None,
                    farm_yield_rated.get(farm.id, 0.0),
                ),
                "fertilizer_summary": _rate_summary(
                    farm_fertilizer_total.get(farm.id, 0.0), farm_area_ha.get(farm.id) or None,
                    farm_fertilizer_rated.get(farm.id, 0.0),
                ),
            }
            for farm in farms
        ],
        "blocks": [
            {
                "id": str(block.id),
                "farm_id": str(block.farm_id),
                "name": block.name,
                "commodity": block.commodity,
                "area": block.area,
                "year": block.year,
                "pic_name": block.pic.get_full_name() or block.pic.username if block.pic else None,
                "tree_match_threshold_m": block.tree_match_threshold_m,
                "harvest_interval_days": block.harvest_interval_days,
                "fertilizer_interval_days": block.fertilizer_interval_days,
                "effective_harvest_interval_days": block.effective_harvest_interval_days(),
                "effective_fertilizer_interval_days": block.effective_fertilizer_interval_days(),
                "tree_count": block.tree_count,
                "has_boundary": block.boundary is not None,
                "geometry": geometry_to_6dp_geojson(block.boundary) if block.boundary else None,
                # Read-only yield-by-date history — see _recent_yield_by_block()
                # above. Newest first, capped at 10 rows.
                "recent_yield": recent_yield_by_block.get(block.id, []),
                # Fertilizer's counterpart — see _recent_fertilizer_by_block()
                # above.
                "recent_fertilizer": recent_fertilizer_by_block.get(block.id, []),
                # All-time yield/fertilizer totals — see _rate_summary()/
                # _yield_totals_by_block()/_fertilizer_totals_by_block() above.
                "yield_summary": _rate_summary(yield_totals_by_block.get(block.id, 0.0), block.area),
                "fertilizer_summary": _rate_summary(fertilizer_totals_by_block.get(block.id, 0.0), block.area),
                # Harvest/Fertilizer plan (assessment) rows for this block —
                # see _plan_entries() above. Newest first, unbounded (a
                # block realistically has a handful of plans, not hundreds).
                "harvest_plans": _plan_entries(harvest_plans_by_block.get(block.id, [])),
                "fertilizer_plans": _plan_entries(fertilizer_plans_by_block.get(block.id, [])),
                # IDs into the top-level "images" list below that have
                # actually been processed against this block — see
                # processed_image_ids_by_block above.
                "processed_image_ids": processed_image_ids_by_block.get(str(block.id), []),
                # Block-scoped clipped preview/geometry per image — see
                # clipped_images_by_block above. Keyed by image_id so
                # farm_map.html can swap in the clipped version of whichever
                # image the imagery picker has selected.
                "clipped_images": clipped_images_by_block.get(str(block.id), []),
            }
            for block in blocks
        ],
        "trees": [
            {
                "id": str(tree.id),
                "farm_id": str(tree.farm_block.farm_id),
                "block_id": str(tree.farm_block_id),
                "name": tree.label,
                "source": tree.source,
                "source_display": tree.get_source_display(),
                "health": tree.current_health,
                "health_display": tree.get_current_health_display(),
                "last_harvested_at": tree.last_harvested_at.isoformat() if tree.last_harvested_at else None,
                "last_fertilized_at": tree.last_fertilized_at.isoformat() if tree.last_fertilized_at else None,
                "harvest_marked_at": tree.harvest_marked_at.isoformat() if tree.harvest_marked_at else None,
                "harvest_marked_by_name": (
                    tree.harvest_marked_by.get_full_name() or tree.harvest_marked_by.username
                ) if tree.harvest_marked_by else None,
                "fertilizer_marked_at": tree.fertilizer_marked_at.isoformat() if tree.fertilizer_marked_at else None,
                "fertilizer_marked_by_name": (
                    tree.fertilizer_marked_by.get_full_name() or tree.fertilizer_marked_by.username
                ) if tree.fertilizer_marked_by else None,
                # See Tree.harvest_status()/fertilizer_status() — one of
                # "never"/"ok"/"due_soon"/"overdue". harvest_stage/
                # fertilizer_stage layer a 5th value, "marked", on top when
                # a Manager has flagged the tree — see Tree.harvest_stage().
                # farm_map.html's marker ring reads *_stage, not *_status,
                # so a pending-but-not-yet-overdue tree still shows as
                # needing attention.
                "harvest_status": tree.harvest_status(),
                "fertilizer_status": tree.fertilizer_status(),
                "harvest_stage": tree.harvest_stage(),
                "fertilizer_stage": tree.fertilizer_stage(),
                "geometry": geometry_to_6dp_geojson(tree.location),
            }
            for tree in trees
        ],
        "images": [
            {
                "id": str(image.id),
                "farm_id": str(image.farm_id),
                "captured_at": image.captured_at.isoformat() if image.captured_at else None,
                "geometry": geometry_to_6dp_geojson(image.footprint),
                "preview_url": image.preview_file.url if image.preview_file else None,
            }
            for image in images
        ],
        # URL *templates* (placeholder UUID, substituted by JS) so the
        # panel's action links/forms point at real reverse()-derived URLs
        # instead of anything hardcoded — see _URL_PLACEHOLDER_UUID.
        "urls": {
            "block_add": reverse("dashboard:block-add"),
            "block_edit": reverse("dashboard:block-edit", kwargs={"block_id": _URL_PLACEHOLDER_UUID}),
            "block_boundary": reverse("dashboard:block-boundary", kwargs={"block_id": _URL_PLACEHOLDER_UUID}),
            "block_delete": reverse("dashboard:block-delete", kwargs={"block_id": _URL_PLACEHOLDER_UUID}),
            "tree_add": reverse("dashboard:tree-add", kwargs={"block_id": _URL_PLACEHOLDER_UUID}),
            "tree_edit": reverse("dashboard:tree-edit", kwargs={"tree_id": _URL_PLACEHOLDER_UUID}),
            "tree_remove": reverse("dashboard:tree-remove", kwargs={"tree_id": _URL_PLACEHOLDER_UUID}),
            "imagery_block": reverse("dashboard:imagery-block", kwargs={"block_id": _URL_PLACEHOLDER_UUID}),
            "imagery_upload": reverse("dashboard:imagery"),
            "imagery_confirm_blocks": reverse(
                "dashboard:imagery-confirm-blocks", kwargs={"image_id": _URL_PLACEHOLDER_UUID}
            ),
            "harvest_plan_add": reverse(
                "dashboard:harvest-plan-add", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "harvest_plan_edit": reverse(
                "dashboard:harvest-plan-edit",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "harvest_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "harvest_plan_delete": reverse(
                "dashboard:harvest-plan-delete",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "harvest_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "harvest_plan_approve": reverse(
                "dashboard:harvest-plan-approve",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "harvest_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "fertilizer_plan_add": reverse(
                "dashboard:fertilizer-plan-add", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "fertilizer_plan_edit": reverse(
                "dashboard:fertilizer-plan-edit",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "fertilizer_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "fertilizer_plan_delete": reverse(
                "dashboard:fertilizer-plan-delete",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "fertilizer_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "fertilizer_plan_approve": reverse(
                "dashboard:fertilizer-plan-approve",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "fertilizer_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "harvest_record": reverse(
                "dashboard:harvest-record", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "harvest_actual_edit": reverse(
                "dashboard:harvest-actual-edit",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "actual_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "harvest_actual_delete": reverse(
                "dashboard:harvest-actual-delete",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "actual_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "fertilizer_record": reverse(
                "dashboard:fertilizer-record", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "fertilizer_actual_edit": reverse(
                "dashboard:fertilizer-actual-edit",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "actual_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "fertilizer_actual_delete": reverse(
                "dashboard:fertilizer-actual-delete",
                kwargs={"block_id": _URL_PLACEHOLDER_UUID, "actual_id": _URL_PLACEHOLDER_UUID_2},
            ),
            "trees_mark_harvested": reverse(
                "dashboard:trees-mark-harvested", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "trees_mark_fertilized": reverse(
                "dashboard:trees-mark-fertilized", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "trees_mark_need_harvest": reverse(
                "dashboard:trees-mark-need-harvest", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "trees_mark_need_fertilize": reverse(
                "dashboard:trees-mark-need-fertilize", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "tree_health_history": reverse(
                "dashboard:tree-health-history", kwargs={"tree_id": _URL_PLACEHOLDER_UUID}
            ),
            "tree_care_history": reverse(
                "dashboard:tree-care-history", kwargs={"tree_id": _URL_PLACEHOLDER_UUID}
            ),
            "block_care_history": reverse(
                "dashboard:block-care-history", kwargs={"block_id": _URL_PLACEHOLDER_UUID}
            ),
            "farm_map": reverse("dashboard:farms"),
        },
        "url_placeholder": _URL_PLACEHOLDER_UUID,
        "url_placeholder_2": _URL_PLACEHOLDER_UUID_2,
        # Read by farm_map.html's own in-page upload form — same setting
        # ImageListUploadView's full-page form already uses, so the two
        # surfaces can't quietly disagree on chunk size.
        "upload_chunk_size_bytes": getattr(settings, "IMAGE_UPLOAD_CHUNK_SIZE_BYTES", 8 * 1024 * 1024),
    }


class FarmMapView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """The one map-first hub for every spatial workflow — replaces what
    used to be three separate pages (a read-only Map, a read-only Farms &
    Blocks list, and BlockTreeListView's per-block tree table), per a
    direct planning conversation with the project owner: this app's
    farms/blocks/trees/imagery are all fundamentally geographic, and
    splitting "look at the map" from "manage what's on it" from "see this
    block's trees" across separate pages — each with its own Leaflet
    instance, each a full page navigation away from the others — was
    exactly the clunky, document-management-shaped UX driving the
    complaint that led to this rebuild. Compliance/reports/notifications
    are deliberately NOT folded into this — those are genuinely
    document/list-shaped, not spatial, and forcing them onto a map would
    trade one kind of awkwardness for another (also discussed and agreed,
    not assumed).

    One map, one JSON payload (farm_map_data, from _build_farm_map_data()
    — every farm/block/tree/image the Manager has scope over, same
    _viewset_queryset() scoping every other read in this app already
    uses), and a side panel whose content is entirely client-side,
    depending on what's selected: nothing → farm list; a farm → its
    blocks; a block → its details/boundary status/trees/imagery; a tree →
    its details. No page reload for any of that.

    Update: writes (create/edit/delete a block, draw a boundary, add/
    edit/remove a tree, create a harvest plan) are now ALSO fully
    in-page — a first version of this shipped writes as full Django form
    pages that redirected back here (see BUILD_STATUS.md's "Farm Map
    unification" entry for that reasoning), but direct, explicit feedback
    was that this didn't match the request: "all activities must be in
    that same page by changing the panel, not redirected into different
    page... adding tree, selecting tree, or editing block are using the
    same leaflet map in same page." So every one of those views now
    detects an AJAX request (_is_ajax()) and returns JSON instead of
    redirecting — a rendered form-fragment for GET (see
    templates/dashboard/_partials/), and either a fresh
    farm_map_data payload + a "focus" hint for a successful POST, or the
    re-rendered fragment (with errors) for a failed one. farm_map.html's
    JS injects those fragments into this page's own panel and, for
    boundary drawing and tree placement, drives Leaflet.draw / a
    draggable marker directly on this page's own map instance — never a
    second map, never a navigation. The full-page version of each of
    those views (FarmBlockCreateView, TreeCreateView, etc.) still exists
    and still works for a direct URL visit — same progressive-enhancement
    convention this app already uses for chunked upload vs. the plain
    upload form — it's just no longer how the Farm Map page itself
    reaches them.

    `?farm=<id>` / `?block=<id>` / `?tree=<id>` preselect on load (read by
    this page's own JS, not Django — these are for the browser's
    benefit on a fresh page load / bookmark, not server-side filtering).
    `?upload_image=<id>` is a fourth, different mode: shows the imagery
    upload-confirm step (image footprint + block checklist) inline on
    this same map — see _build_image_confirm_data() and
    FarmMapView.get_context_data() below.
    """

    template_name = "dashboard/farm_map.html"

    def _upload_confirm_image(self, farm_map_data):
        """Resolves ?upload_image=<id> against this request's own scope
        (never a bare Image.objects.get with no ownership check) — shared
        by get_context_data() (full-page load) and get()'s AJAX branch
        (polling — see farm_map.html's pollUploadStatus(), used once an
        in-page upload's georeferencing might have finished).
        """
        upload_image_id = self.request.GET.get("upload_image")
        if not upload_image_id:
            return None
        try:
            return Image.objects.get(id=upload_image_id, farm__in=[f["id"] for f in farm_map_data["farms"]])
        except (Image.DoesNotExist, ValueError):
            return None

    def get(self, request, *args, **kwargs):
        if _is_ajax(request):
            # Polling endpoint — farm_map.html's own in-page upload flow
            # calls this (GET ?upload_image=<id>, AJAX) every few seconds
            # until georeferencing finishes, entirely without a page
            # reload. Same underlying data get_context_data() below
            # builds for the full-page load; just JSON instead of HTML.
            farm_map_data = _build_farm_map_data(request)
            image = self._upload_confirm_image(farm_map_data)
            upload_confirm_image = None
            if image is not None:
                farm_map_data["upload_confirm"] = _build_image_confirm_data(image)
                upload_confirm_image = {
                    "id": str(image.id),
                    "georeference_status": image.georeference_status,
                    "georeference_error": image.georeference_error,
                }
            return JsonResponse({"farm_map_data": farm_map_data, "upload_confirm_image": upload_confirm_image})
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request

        farm_map_data = _build_farm_map_data(request)

        image = self._upload_confirm_image(farm_map_data)
        if image is not None:
            farm_map_data["upload_confirm"] = _build_image_confirm_data(image)
            context["upload_confirm_image"] = image  # for the "still georeferencing" / "failed" states

        context["farm_map_data"] = farm_map_data
        return context


# --- Block CRUD — the gap FarmBlockListView's own docstring used to flag:
# no dashboard UI touched FarmBlock or Tree at all, only /admin/ and the
# API. `boundary` (FarmBlock) and `location` (Tree) are real PostGIS
# geometry — docs/01-architecture.md's "no text-string coordinates
# anywhere" rule — so they're never form fields; they're drawn/placed on a
# Leaflet map (FarmBlockBoundaryEditView, TreeCreateView/TreeUpdateView),
# same library MapView and ImageConfirmBlocksView already use, no new
# frontend dependency except Leaflet.draw for the polygon-editing tool
# itself (same CDN-exception reasoning map.html's own comment already
# documents for Leaflet/tile imagery).
#
# Both delete-shaped actions below are deliberately conservative rather
# than a plain destructive button:
#
# - FarmBlockDeleteView refuses outright if the block has *any* trees,
#   processing runs, or compliance records linked — checked directly, not
#   assumed. FarmBlock.trees/harvests/fertilizers/processing_runs/
#   compliance_audits/harvest_batches/deforestation_baseline are all
#   on_delete=CASCADE (confirmed by reading every model that FKs to
#   FarmBlock, not guessed) — and Tree itself cascades further into real
#   yield/fertilizer history (Harvest.tree, Fertilizer.tree are also
#   CASCADE). A block delete button with no such check would be one click
#   away from silently destroying a farm's actual production history, not
#   just an empty administrative record. Deleting a block that's actually
#   been used isn't offered here at all — only a block created by mistake,
#   with nothing recorded against it yet, is a true delete candidate.
# - "Deleting" a Tree never removes the row — it sets status=removed, the
#   soft-delete convention docs/06-ml-pipeline.md's own matching logic
#   already expects ("a detection near a soft-deleted (status=removed)
#   tree's old location should be flagged ambiguous"). A hard delete would
#   also cascade into that tree's own Harvest/Fertilizer history — the
#   exact same real-data-loss risk as the block case, just one level down.

class FarmBlockCreateView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Update: now AJAX-first — see FarmMapView's own docstring for why.
    A direct visit to this URL (no X-Requested-With header) still gets the
    full standalone page (block_form.html), unchanged; the Farm Map page
    itself now opens this inline in its side panel instead. `?farm=<id>`
    on the AJAX GET preselects the farm dropdown to whichever farm was
    selected on the map when "+ Add block" was clicked — a UX nicety only,
    still re-validated server-side on save like every other field.
    """

    template_name = "dashboard/block_form.html"

    def get(self, request, *args, **kwargs):
        initial = {}
        farm_id = request.GET.get("farm")
        if farm_id:
            initial["farm"] = farm_id
        self.form = FarmBlockForm(initial=initial, user=request.user)
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/block_form.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.form = FarmBlockForm(request.POST, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data["farm"]
            _require_manager_for_farm(request.user, farm)  # belt-and-suspenders, same pattern as every form above
            block = self.form.save()
            message = f"Block “{block.name}” created. Next, draw its boundary."
            if _is_ajax(request):
                return JsonResponse({
                    "ok": True,
                    "message": message,
                    "farm_map_data": _build_farm_map_data(request),
                    "focus": {"type": "block", "id": str(block.id), "then": "boundary"},
                })
            messages.success(request, message)
            return redirect("dashboard:block-boundary", block_id=block.id)
        if _is_ajax(request):
            return JsonResponse({"ok": False, "html": render_to_string(
                "dashboard/_partials/block_form.html", self.get_context_data(), request=request,
            )}, status=400)
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["is_create"] = True
        return context


class FarmBlockUpdateView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Update: AJAX-first — see FarmMapView's own docstring."""

    template_name = "dashboard/block_form.html"

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def get(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = FarmBlockForm(instance=self.block, user=request.user)
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/block_form.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = FarmBlockForm(request.POST, instance=self.block, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data["farm"]
            _require_manager_for_farm(request.user, farm)
            self.form.save()
            message = f"Block “{self.block.name}” updated."
            if _is_ajax(request):
                return JsonResponse({
                    "ok": True,
                    "message": message,
                    "farm_map_data": _build_farm_map_data(request),
                    "focus": {"type": "block", "id": str(self.block.id)},
                })
            messages.success(request, message)
            return redirect(f"{reverse('dashboard:farms')}?block={self.block.id}")
        if _is_ajax(request):
            return JsonResponse({"ok": False, "html": render_to_string(
                "dashboard/_partials/block_form.html", self.get_context_data(), request=request,
            )}, status=400)
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["is_create"] = False
        context["farm_block"] = self.block
        return context


class FarmBlockBoundaryEditView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Draws/edits a block's `boundary` polygon on a Leaflet + Leaflet.draw
    map — the one field FarmBlockForm above deliberately leaves out. Shows
    the farm's *other* blocks (read-only, dimmed) for spatial context, so a
    Manager can see they're not accidentally drawing over a neighboring
    block. Submits the drawn polygon as a GeoJSON string in a hidden field;
    `GEOSGeometry` parses GeoJSON directly, so no custom parsing is needed
    server-side.

    Update: the Farm Map page now drives this directly on its own shared
    map (Leaflet.draw enabled in place — see farm_map.html's
    startBoundaryEdit()) rather than navigating to this view's own
    standalone page — it doesn't even need this view's GET at all anymore
    for that path, since farm_map_data already carries every block's
    current geometry client-side. Only the POST gained an AJAX branch,
    below. This page (GET + full-page POST fallback) still works unchanged
    for a direct URL visit.
    """

    template_name = "dashboard/block_boundary.html"

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        block = self._get_block()
        other_blocks = FarmBlock.objects.filter(farm=block.farm, boundary__isnull=False).exclude(id=block.id)
        context["farm_block"] = block
        context["boundary_data"] = {
            "block": {"geometry": geometry_to_6dp_geojson(block.boundary) if block.boundary else None},
            "other_blocks": [
                {"name": b.name, "geometry": geometry_to_6dp_geojson(b.boundary)} for b in other_blocks
            ],
        }
        return context

    def post(self, request, *args, **kwargs):
        block = self._get_block()

        def fail(message):
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": message}, status=400)
            messages.error(request, message)
            return redirect("dashboard:block-boundary", block_id=block.id)

        geometry_json = request.POST.get("geometry")
        if not geometry_json:
            return fail("Draw a boundary polygon before saving.")
        try:
            geometry = GEOSGeometry(geometry_json)
        except Exception:  # noqa: BLE001 — any malformed-GeoJSON shape from the client surfaces the same way
            return fail("That boundary couldn't be read — try drawing it again.")
        if geometry.geom_type != "Polygon":
            return fail("A block boundary must be a single polygon.")
        geometry.srid = 4326
        block.boundary = geometry
        block.save(update_fields=["boundary", "updated_at"])
        message = f"Boundary saved for “{block.name}”."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")


class FarmBlockDeleteView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """See the module-level comment above this section for why this
    refuses to delete anything that's actually been used — only a block
    with zero trees, zero processing runs, and zero compliance records is
    a real delete candidate; everything else is real farm history a
    misclick shouldn't be able to cascade-destroy.
    """

    template_name = "dashboard/block_delete.html"

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def _blockers(self, block):
        # DeforestationBaseline is queried directly (Model.objects.filter),
        # not via the reverse OneToOne accessor (block.deforestation_baseline)
        # — that accessor raises RelatedObjectDoesNotExist when absent, which
        # plain hasattr()/try-except-AttributeError does NOT catch (it isn't
        # an AttributeError), so reaching for it here would crash this check
        # for the common case of a block with no baseline at all. A filtered
        # query sidesteps that trap entirely.
        checks = [
            ("tree", block.trees.count()),
            ("processing run", block.processing_runs.count()),
            ("compliance audit", block.compliance_audits.count()),
            ("harvest batch", block.harvest_batches.count()),
            ("deforestation baseline", DeforestationBaseline.objects.filter(farm_block=block).count()),
        ]
        return [(label, count) for label, count in checks if count]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        block = self._get_block()
        context["farm_block"] = block
        context["blockers"] = self._blockers(block)
        return context

    def get(self, request, *args, **kwargs):
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/block_delete.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        block = self._get_block()
        blockers = self._blockers(block)
        if blockers:
            summary = ", ".join(f"{count} {label}{'s' if count != 1 else ''}" for label, count in blockers)
            message = f"Can't delete “{block.name}” — it still has {summary} linked."
            if _is_ajax(request):
                return JsonResponse({"ok": False, "html": render_to_string(
                    "dashboard/_partials/block_delete.html",
                    {"farm_block": block, "blockers": blockers}, request=request,
                )}, status=400)
            messages.error(request, message)
            return redirect("dashboard:block-delete", block_id=block.id)
        name, farm_id = block.name, block.farm_id
        block.delete()
        message = f"Block “{name}” deleted."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "farm", "id": str(farm_id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?farm={farm_id}")


class TreeCreateView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """`location` is required on the model (unlike FarmBlock.boundary,
    which can genuinely be null) — so unlike the boundary flow above,
    which can be added as a separate step after the block already exists,
    a Tree can't be half-created without a point. Refuses to save without
    one rather than defaulting to some placeholder coordinate, which would
    be silently wrong data (see docs/01-architecture.md's coordinate rule
    again — a fake point is arguably worse than a text-string one, since
    nothing about it looks fake).
    """

    template_name = "dashboard/tree_form.html"

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def get(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = TreeForm(initial={"farm_block": self.block}, user=request.user)
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/tree_form.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = TreeForm(request.POST, user=request.user)
        lat, lng = request.POST.get("lat"), request.POST.get("lng")

        def fail(message=None):
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": message, "html": render_to_string(
                    "dashboard/_partials/tree_form.html", self.get_context_data(), request=request,
                )}, status=400)
            if message:
                messages.error(request, message)
            return self.render_to_response(self.get_context_data())

        if not self.form.is_valid():
            return fail()
        farm_block = self.form.cleaned_data["farm_block"]
        _require_manager_for_farm(request.user, farm_block.farm)
        if not lat or not lng:
            return fail("Click the map to place this tree before saving.")
        tree = self.form.save(commit=False)
        tree.location = Point(float(lng), float(lat), srid=4326)
        tree.created_by = request.user
        tree.updated_by = request.user
        tree.save()
        message = f"Tree added to “{farm_block.name}”."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "tree", "id": str(tree.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={farm_block.id}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["farm_block"] = self.block
        context["is_create"] = True
        context["tree_map_data"] = {
            "boundary": geometry_to_6dp_geojson(self.block.boundary) if self.block.boundary else None,
            "existing_point": None,
            "other_trees": [
                {"name": t.label, "geometry": geometry_to_6dp_geojson(t.location)}
                for t in self.block.trees.filter(status=Tree.STATUS_ACTIVE).select_related("farm_block")
            ],
        }
        return context


class TreeUpdateView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Editing an existing tree's location is optional here — lat/lng are
    only applied if the map was actually clicked again (see the `if lat
    and lng` guard below), so correcting just the name/health/status
    doesn't require re-placing a perfectly correct point.
    """

    template_name = "dashboard/tree_form.html"

    def _get_tree(self):
        tree = get_object_or_404(_viewset_queryset(TreeViewSet, self.request), id=self.kwargs["tree_id"])
        _require_manager_for_farm(self.request.user, tree.farm_block.farm)
        return tree

    def get(self, request, *args, **kwargs):
        self.tree = self._get_tree()
        self.block = self.tree.farm_block
        self.form = TreeForm(instance=self.tree, user=request.user)
        if _is_ajax(request):
            return JsonResponse({
                "html": render_to_string("dashboard/_partials/tree_form.html", self.get_context_data(), request=request),
                # So farm_map.html can pre-place the draggable marker at the
                # tree's current location — the shared map has no other way
                # to know it, unlike tree_form.html's own standalone map,
                # which read it from tree_map_data.existing_point directly.
                "existing_point": geometry_to_6dp_geojson(self.tree.location),
            })
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.tree = self._get_tree()
        self.block = self.tree.farm_block
        self.form = TreeForm(request.POST, instance=self.tree, user=request.user)
        if not self.form.is_valid():
            if _is_ajax(request):
                return JsonResponse({"ok": False, "html": render_to_string(
                    "dashboard/_partials/tree_form.html", self.get_context_data(), request=request,
                )}, status=400)
            return self.render_to_response(self.get_context_data())
        farm_block = self.form.cleaned_data["farm_block"]
        _require_manager_for_farm(request.user, farm_block.farm)
        tree = self.form.save(commit=False)
        lat, lng = request.POST.get("lat"), request.POST.get("lng")
        if lat and lng:
            tree.location = Point(float(lng), float(lat), srid=4326)
        tree.updated_by = request.user
        tree.save()
        message = f"Tree “{tree.label}” updated."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "tree", "id": str(tree.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={farm_block.id}&tree={tree.id}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["farm_block"] = self.block
        context["is_create"] = False
        context["tree"] = self.tree
        context["tree_map_data"] = {
            "boundary": geometry_to_6dp_geojson(self.block.boundary) if self.block.boundary else None,
            "existing_point": geometry_to_6dp_geojson(self.tree.location),
            "other_trees": [
                {"name": t.label, "geometry": geometry_to_6dp_geojson(t.location)}
                for t in self.block.trees.filter(status=Tree.STATUS_ACTIVE).select_related("farm_block").exclude(id=self.tree.id)
            ],
        }
        return context


class TreeRemoveView(ManagerOrSuperAdminRequiredMixin, View):
    """Soft-delete only — see the module-level comment above this section
    for why. A GET confirmation isn't a separate template here (unlike
    FarmBlockDeleteView, which has real consequences to explain); this is
    reversible (status can be flipped back via TreeUpdateView), so a plain
    POST-only action with an inline confirm on the list page is enough.
    """

    def post(self, request, tree_id):
        tree = get_object_or_404(_viewset_queryset(TreeViewSet, request), id=tree_id)
        _require_manager_for_farm(request.user, tree.farm_block.farm)
        tree.status = Tree.STATUS_REMOVED
        tree.updated_by = request.user
        tree.save(update_fields=["status", "updated_by", "updated_at"])
        message = f"Tree “{tree.label}” marked removed."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(tree.farm_block_id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={tree.farm_block_id}")


class TreeHealthHistoryView(ManagerOrSuperAdminRequiredMixin, View):
    """On-demand per-tree health-by-date history. TreeHealthClassification
    rows (apps/imagery/models.py) were already being recorded by the
    detect+classify pipeline — one per TreeDetection, and a new
    TreeDetection row is created on every processing run rather than
    overwriting the last one, which is what gives a real history over time
    (see TreeDetection's own docstring). This was just never surfaced in
    the dashboard until now; read-only, no new model.

    Deliberately a separate on-demand endpoint rather than embedded into
    _build_farm_map_data() the way block yield history is
    (_recent_yield_by_block() above): that payload is refetched after
    every single write action anywhere on the Farm Map page, and a farm
    can have thousands of trees against only a handful of blocks — baking
    a (usually short or empty) history array onto every tree in it isn't
    worth the size for data that's only ever looked at one tree at a time.
    """

    def get(self, request, tree_id):
        tree = get_object_or_404(_viewset_queryset(TreeViewSet, request), id=tree_id)
        _require_manager_for_farm(request.user, tree.farm_block.farm)
        classifications = (
            TreeHealthClassification.objects.filter(tree_detection__tree_id=tree.id)
            .select_related("tree_detection")
            .order_by("-tree_detection__created_at")[:20]
        )
        health_choices = dict(TreeHealthClassification.HEALTH_CHOICES)
        history = [
            {
                "date": c.tree_detection.created_at.isoformat(),
                "label": c.effective_label,
                "label_display": health_choices.get(c.effective_label, c.effective_label),
                "confidence": c.confidence,
                "reviewed": c.reviewed,
            }
            for c in classifications
        ]
        return JsonResponse({"history": history})


class TreeCareHistoryView(ManagerOrSuperAdminRequiredMixin, View):
    """On-demand per-tree harvest/fertilizer marking history —
    TreeCareRecord rows (apps/trees/models.py), one per "mark
    harvested"/"mark fertilized" completion from either surface (the PWA's
    mark_harvested/mark_fertilized API actions, or this dashboard's own
    MarkTreesHarvestedView/MarkTreesFertilizedView), plus (since `action`
    was added) one per stage-1 "needs" assignment this tree was ever part
    of. Mirrors TreeHealthHistoryView immediately above — same
    on-demand-fetch shape, same reasoning for not baking this into
    _build_farm_map_data() (a farm block can hold over a thousand trees;
    see that view's own docstring).

    Not grouped by batch_id like BlockCareHistoryView below — a single
    bulk action can only ever include a given tree once (it's a checkbox
    selection), so scoped to one tree every row is already its own
    complete "this happened" entry; batching only matters once you're
    looking across many trees at once.

    Both kinds are returned together, newest first, with `kind` on each
    entry so the panel can label/group them — a tree's harvest and
    fertilizer history are usually looked at side by side, unlike health
    history which has no fertilizer/harvest equivalent to interleave with.
    """

    def get(self, request, tree_id):
        tree = get_object_or_404(_viewset_queryset(TreeViewSet, request), id=tree_id)
        _require_manager_for_farm(request.user, tree.farm_block.farm)
        records = (
            TreeCareRecord.objects.filter(tree_id=tree.id)
            .select_related("recorded_by")
            .order_by("-date", "-recorded_at")[:20]
        )
        history = [
            {
                "date": r.date.isoformat(),
                "kind": r.kind,
                "kind_display": r.get_kind_display(),
                "action": r.action,
                "action_display": r.get_action_display(),
                "quantity": r.quantity,
                "recorded_by_name": r.recorded_by.get_full_name() or r.recorded_by.username if r.recorded_by else None,
            }
            for r in records
        ]
        return JsonResponse({"history": history})


class BlockCareHistoryView(ManagerOrSuperAdminRequiredMixin, View):
    """Block-wide harvest/fertilizer marking history — grouped by the
    bulk mark/record ACTION (TreeCareRecord.batch_id), not one row per
    tree per completion. Backs the Harvest/Fertilizer section panels
    (farm_map.html's renderBlockSectionPanel()).

    Originally returned one flat row per TreeCareRecord, each labelled
    with its own tree — reported directly as wrong: "the harvest marking
    history is not the each tree history but the marking history itself,"
    e.g. marking 3 trees as needing harvest on one date should show as ONE
    history entry (how many trees, and for a completion, the total
    quantity), not 3 separate lines — and clicking that entry should
    highlight just those trees on the map, not jump into one of them.
    Every row from the same bulk submission already shares one batch_id
    (see TreeCareRecord's own docstring), so grouping by it here is enough
    to reconstruct "one action" without a separate batch table.

    Two passes, not one capped slice — a single bulk action can now cover
    however many trees a block actually holds (confirmed directly: a
    600-tree "Record harvest directly" selection, the exact scale the
    tree_ids/quantities JSON-payload fix above exists for). A single
    `[:N]` slice on the raw TreeCareRecord rows would risk cutting off
    partway through one large batch and silently under-reporting its own
    tree_count/total_quantity — reproduced exactly that during testing (a
    600-tree batch showed as "400 trees, 5000 bunches" under the old
    400-row cap). So: pass 1 walks the ordered rows only far enough to
    collect the 50 most recent DISTINCT batch_ids (cheap — most batches
    are a handful of trees), then pass 2 fetches every row belonging to
    just those batches, however many that turns out to be, so each
    batch's own totals are always complete.
    """

    HISTORY_BATCH_LIMIT = 500  # the PWA marks one tree per batch, so 50 covered only a day or two of field work

    def get(self, request, block_id):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=block_id)
        _require_manager_for_farm(request.user, block.farm)

        ordered_batch_ids = (
            TreeCareRecord.objects.filter(tree__farm_block_id=block.id)
            .order_by("-date", "-recorded_at")
            .values_list("batch_id", flat=True)
        )
        recent_batch_ids = []
        seen = set()
        for batch_id in ordered_batch_ids.iterator():
            if batch_id not in seen:
                seen.add(batch_id)
                recent_batch_ids.append(batch_id)
                if len(recent_batch_ids) >= self.HISTORY_BATCH_LIMIT:
                    break

        records = (
            TreeCareRecord.objects.filter(batch_id__in=recent_batch_ids)
            .select_related("recorded_by", "tree")
        )
        batches = {}
        for r in records:
            batch = batches.get(r.batch_id)
            if batch is None:
                batch = {
                    "batch_id": str(r.batch_id),
                    "date": r.date.isoformat(),
                    "kind": r.kind,
                    "kind_display": r.get_kind_display(),
                    "action": r.action,
                    "action_display": r.get_action_display(),
                    "recorded_by_name": r.recorded_by.get_full_name() or r.recorded_by.username if r.recorded_by else None,
                    "tree_ids": [],
                    "quantities": [],
                }
                batches[r.batch_id] = batch
            batch["tree_ids"].append(str(r.tree_id))
            if r.quantity is not None:
                batch["quantities"].append(r.quantity)

        history = []
        for batch_id in recent_batch_ids:
            batch = batches[batch_id]
            quantities = batch.pop("quantities")
            batch["tree_count"] = len(batch["tree_ids"])
            batch["total_quantity"] = sum(quantities) if quantities else None
            history.append(batch)
        return JsonResponse({"history": history})


class _MarkTreesActionMixin:
    """Shared plumbing for every "select trees on the map, then confirm one
    action" view below — stage-1 assignment (_MarkTreesNeedView) and
    completion (_MarkTreesView) alike, plus HarvestPlanCreateView's own
    tree checklist. POST-only, no GET/form-fragment branch: there's no
    separate form to fetch, the tree-selection UI it submits from is
    already live in the panel (renderTreeSelectionChecklist()/
    renderMarkTreesSection() in farm_map.html) — this is "confirm the
    selection I'm already looking at," not "open a form." (HarvestPlanCreateView
    is the one exception — it does fetch/submit a real form, for the plan's
    own assessment_date/assessment_quantity fields, but reuses this mixin's
    tree-selection helpers for the checklist embedded in that same screen.)
    """

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def _get_selected_trees(self, request, block):
        tree_ids = self._tree_ids_from_request(request)
        if not tree_ids:
            return None
        return Tree.objects.filter(id__in=tree_ids, farm_block=block, status=Tree.STATUS_ACTIVE)

    def _tree_ids_from_request(self, request):
        """Reads the selected tree ids from ONE JSON-encoded `tree_ids`
        field, not one `tree_ids` POST field per tree. Reported directly,
        with a real reproduction: lasso-selecting roughly half of a
        ~1000-tree block ("Record harvest directly," which also sends a
        `quantity_<id>` field per tree — see the old _quantity_for below)
        pushed the total POST field count past Django's
        DATA_UPLOAD_MAX_NUMBER_FIELDS (1000), which fails the WHOLE
        request with a 400 before this view ever runs — the frontend could
        only report that as a generic "couldn't save." A single JSON field
        scales to however many trees a block actually holds, independent
        of the selection size.
        """
        raw = request.POST.get("tree_ids") or "[]"
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(i) for i in parsed] if isinstance(parsed, list) else []

    def _no_selection_response(self, request, block):
        message = "Select at least one tree first."
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")

    def _quantities_from_request(self, request):
        """Reads every tree's optional quantity from ONE JSON-encoded
        `quantities` field (`{tree_id: value}`), not one `quantity_<id>`
        field per tree — see _tree_ids_from_request()'s docstring above for
        why one-field-per-tree doesn't scale to a real block's tree count.
        Different trees still yield different amounts (this is a dict, not
        a single shared value); a blank/missing/unparseable entry is a
        normal input for this optional field, not an error — same
        reasoning as the API path's _mark_done(). Moved up from
        _MarkTreesView (below) so RecordActualHarvestView/
        RecordActualFertilizerView can reuse the exact same decoding.
        """
        raw = request.POST.get("quantities") or "{}"
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _quantity_for(self, quantities, tree_id):
        raw = quantities.get(str(tree_id))
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _success_response(self, request, block, message):
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")

    def _mark_trees_done(
        self, request, trees, date, quantities, *, date_field, quantity_field, marked_at_field, marked_by_field, kind,
    ):
        """Completes each tree: sets its last-done date and quantity
        (`quantities` maps tree id → value or None), clears its "needs …"
        flag, and logs one TreeCareRecord per tree sharing a batch_id. Used
        by the "mark done" views and by Record harvest, so recording a
        harvest also updates the trees the same way. Saves per tree, not a
        bulk .update(), so simple_history records each change.
        """
        batch_id = uuid.uuid4()
        for tree in trees:
            quantity = quantities.get(tree.id)
            setattr(tree, date_field, date)
            setattr(tree, quantity_field, quantity)
            setattr(tree, marked_at_field, None)
            setattr(tree, marked_by_field, None)
            tree.updated_by = request.user
            tree.save(update_fields=[date_field, quantity_field, marked_at_field, marked_by_field, "updated_by", "updated_at"])
            TreeCareRecord.objects.create(
                tree=tree, kind=kind, action=TreeCareRecord.ACTION_COMPLETED,
                date=date, quantity=quantity, recorded_by=request.user, batch_id=batch_id,
            )
        return len(trees)

    def _mark_trees_needing(self, request, trees, marked_at_field, marked_by_field, kind):
        """The actual "flag these trees" write — sets Tree.harvest_marked_at/
        harvest_marked_by (or the fertilizer equivalents) and logs one
        TreeCareRecord per tree sharing a batch_id, same as
        _MarkTreesNeedView.post() below (which this was pulled out of).
        Shared so HarvestPlanCreateView can mark a plan's selected trees
        exactly the same way "Mark trees needing harvest" already does,
        rather than a second, drifting copy of the same loop — requested
        directly: "move this feature into plan harvest - select trees. so
        selected trees in plan-harvest is marked." Returns the count marked.
        """
        now = timezone.now()
        marked_date = timezone.localdate()
        batch_id = uuid.uuid4()
        count = 0
        for tree in trees:
            setattr(tree, marked_at_field, now)
            setattr(tree, marked_by_field, request.user)
            tree.updated_by = request.user
            tree.save(update_fields=[marked_at_field, marked_by_field, "updated_by", "updated_at"])
            TreeCareRecord.objects.create(
                tree=tree, kind=kind, action=TreeCareRecord.ACTION_NEEDED,
                date=marked_date, quantity=None, recorded_by=request.user, batch_id=batch_id,
            )
            count += 1
        return count


class HarvestPlanCreateView(_MarkTreesActionMixin, ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Creates a block-level Harvest plan from the Farm Map panel. The plan
    itself stays block-level — Harvest has no tree-level scoping field
    (docs/03-data-model.md), and that's an explicit, separate scope
    decision, not something this view changes.

    Update — the tree checklist on this screen ("Plan harvest — select
    trees") used to be a client-side visual affordance only: nothing about
    which trees were checked was ever sent here or persisted, which
    surprised a user who selected 9 trees and found none of them showing
    up as "needing harvest" anywhere. Asked directly to fix this by moving
    "Mark trees needing harvest"'s own behavior into this flow instead of
    building separate tree-level plan scoping — selecting trees here now
    marks them via the exact same `_mark_trees_needing()`
    (_MarkTreesActionMixin, above) that `MarkTreesNeedHarvestView` calls,
    so both entry points behave identically and share one implementation.
    Selecting zero trees still creates a plan with nothing marked, exactly
    as before — the tree checklist remains optional here, unlike the
    dedicated "Mark trees needing harvest" action where a selection is
    required.
    """

    template_name = "dashboard/harvest_plan_form.html"

    def get(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = HarvestPlanForm()
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/harvest_plan_form.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = HarvestPlanForm(request.POST)
        if not self.form.is_valid():
            if _is_ajax(request):
                return JsonResponse({"ok": False, "html": render_to_string(
                    "dashboard/_partials/harvest_plan_form.html", self.get_context_data(), request=request,
                )}, status=400)
            return self.render_to_response(self.get_context_data())
        plan = self.form.save(commit=False)
        plan.farm_block = self.block
        plan.save()

        # Selecting trees on this screen is optional — a Manager who just
        # wants a block-level plan with no specific trees flagged yet
        # still has that option, same as before this feature existed.
        trees = self._get_selected_trees(request, self.block)
        marked_count = (
            self._mark_trees_needing(request, trees, "harvest_marked_at", "harvest_marked_by", TreeCareRecord.KIND_HARVEST)
            if trees is not None else 0
        )

        message = f"Harvest plan created for “{self.block.name}”."
        if marked_count:
            message += f" {marked_count} tree{'s' if marked_count != 1 else ''} marked as needing harvest."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(self.block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={self.block.id}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["farm_block"] = self.block
        return context


class FertilizerPlanCreateView(_MarkTreesActionMixin, ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Creates a block-level Fertilizer plan from the Farm Map panel — the
    fertilizer counterpart to HarvestPlanCreateView above, added after
    direct feedback asking where the fertilizer process was on the map
    page. See FertilizerPlanForm's own docstring (apps/dashboard/forms.py)
    for why this is block-level only, same as Harvest.
    """

    template_name = "dashboard/fertilizer_plan_form.html"

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def get(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = FertilizerPlanForm()
        if _is_ajax(request):
            return JsonResponse({"html": render_to_string(
                "dashboard/_partials/fertilizer_plan_form.html", self.get_context_data(), request=request,
            )})
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.form = FertilizerPlanForm(request.POST)
        if not self.form.is_valid():
            if _is_ajax(request):
                return JsonResponse({"ok": False, "html": render_to_string(
                    "dashboard/_partials/fertilizer_plan_form.html", self.get_context_data(), request=request,
                )}, status=400)
            return self.render_to_response(self.get_context_data())
        plan = self.form.save(commit=False)
        plan.farm_block = self.block
        plan.save()
        # Same as HarvestPlanCreateView: trees checked while planning are
        # marked as needing fertilizer (optional — none checked is fine).
        trees = self._get_selected_trees(request, self.block)
        marked_count = (
            self._mark_trees_needing(
                request, trees, "fertilizer_marked_at", "fertilizer_marked_by", TreeCareRecord.KIND_FERTILIZER,
            ) if trees is not None else 0
        )
        message = f"Fertilizer plan created for “{self.block.name}”."
        if marked_count:
            message += f" {marked_count} tree{'s' if marked_count != 1 else ''} marked as needing fertilizer."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(self.block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={self.block.id}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["farm_block"] = self.block
        return context


class _MarkTreesNeedView(_MarkTreesActionMixin, ManagerOrSuperAdminRequiredMixin, View):
    """Stage 1 of the two-stage harvest/fertilizer workflow — a Manager
    flags which trees need harvesting/fertilizing, from the office, before
    any field work happens. Sets Tree.harvest_marked_at/harvest_marked_by
    (or the fertilizer equivalents) — see harvest_marked_at's help_text
    (apps/trees/models.py) for the full two-stage, two-actor shape this is
    part of. Stage 2 (the PIC Block user confirming the work is actually
    done) is deliberately NOT here — it's apps.trees.views.TreeViewSet's
    mark_harvested/mark_fertilized API actions, reached from the PWA, per
    this app's existing Manager-dashboard/PIC-Block-PWA split.

    Also logs one TreeCareRecord per tree (action=ACTION_NEEDED, no
    quantity — nothing's actually happened yet) sharing a single batch_id
    for the whole request. Reported directly: this assignment previously
    left no trace once the "needs" pending list moved on (the tree gets
    completed, or reassigned later) — only Tree.harvest_marked_at existed,
    which this action itself immediately clears again on completion. The
    block-wide history (BlockCareHistoryView) is what actually surfaces
    these, grouped by batch_id into one "N trees marked needing X" entry.
    """

    marked_at_field = None  # "harvest_marked_at" or "fertilizer_marked_at" — set by subclass
    marked_by_field = None  # "harvest_marked_by" or "fertilizer_marked_by" — set by subclass
    verb = None  # "harvest" or "fertilizer" — set by subclass
    kind = None  # TreeCareRecord.KIND_HARVEST or KIND_FERTILIZER — set by subclass

    def post(self, request, *args, **kwargs):
        block = self._get_block()
        trees = self._get_selected_trees(request, block)
        if trees is None:
            return self._no_selection_response(request, block)

        count = self._mark_trees_needing(request, trees, self.marked_at_field, self.marked_by_field, self.kind)

        message = f"Marked {count} tree{'s' if count != 1 else ''} as needing {self.verb} — the assigned PIC Block user will see this in the field app."
        return self._success_response(request, block, message)


class MarkTreesNeedHarvestView(_MarkTreesNeedView):
    marked_at_field = "harvest_marked_at"
    marked_by_field = "harvest_marked_by"
    verb = "harvest"
    kind = TreeCareRecord.KIND_HARVEST


class MarkTreesNeedFertilizeView(_MarkTreesNeedView):
    marked_at_field = "fertilizer_marked_at"
    marked_by_field = "fertilizer_marked_by"
    verb = "fertilizer"
    kind = TreeCareRecord.KIND_FERTILIZER


class _MarkTreesView(_MarkTreesActionMixin, ManagerOrSuperAdminRequiredMixin, View):
    """Completion, recorded directly by a Manager/Super Admin — the
    dashboard-side counterpart to apps.trees.views.TreeViewSet's
    mark_harvested/mark_fertilized API actions (which do the same thing for
    a PIC Block user via the PWA). Kept as its own path rather than removed
    now that stage 1 (_MarkTreesNeedView above) exists — a Manager who
    already knows the work is done (told by phone, or catching up on
    historical records) shouldn't have to wait on a PWA round-trip just to
    record it. Either path clears the pending marked_at/marked_by fields,
    so a tree completed this way never gets stuck showing as still "marked."

    Deliberately NOT the same code path as Harvest/ActualHarvest/HarvestTree
    (or the Fertilizer equivalents) in apps/operations/models.py — see
    Tree.last_harvested_at's help_text for the full reasoning. Loops and
    calls .save() per tree rather than a bulk .update() so simple_history
    still records each change (bulk .update() bypasses model signals,
    which would silently break the audit trail every other write in this
    app relies on).
    """

    date_field = None  # "last_harvested_at" or "last_fertilized_at" — set by subclass
    quantity_field = None  # "last_harvest_quantity" or "last_fertilizer_quantity" — set by subclass
    marked_at_field = None  # the stage-1 field this completion clears — set by subclass
    marked_by_field = None  # ditto — set by subclass
    verb = None  # "harvested" or "fertilized" — set by subclass
    kind = None  # TreeCareRecord.KIND_HARVEST or KIND_FERTILIZER — set by subclass

    def post(self, request, *args, **kwargs):
        block = self._get_block()
        trees = self._get_selected_trees(request, block)
        if trees is None:
            return self._no_selection_response(request, block)

        marked_date = parse_date(request.POST.get("date") or "") or timezone.localdate()
        quantities = self._quantities_from_request(request)
        bad = _bad_amounts_message(marked_date, quantities)
        if bad:
            return JsonResponse({"ok": False, "message": bad}, status=400)
        trees = list(trees)
        count = self._mark_trees_done(
            request, trees, marked_date, {tree.id: self._quantity_for(quantities, tree.id) for tree in trees},
            date_field=self.date_field, quantity_field=self.quantity_field,
            marked_at_field=self.marked_at_field, marked_by_field=self.marked_by_field, kind=self.kind,
        )

        message = f"Marked {count} tree{'s' if count != 1 else ''} as {self.verb} ({marked_date.isoformat()})."
        return self._success_response(request, block, message)


class MarkTreesHarvestedView(_MarkTreesView):
    date_field = "last_harvested_at"
    quantity_field = "last_harvest_quantity"
    marked_at_field = "harvest_marked_at"
    marked_by_field = "harvest_marked_by"
    verb = "harvested"
    kind = TreeCareRecord.KIND_HARVEST


class MarkTreesFertilizedView(_MarkTreesView):
    date_field = "last_fertilized_at"
    quantity_field = "last_fertilizer_quantity"
    marked_at_field = "fertilizer_marked_at"
    marked_by_field = "fertilizer_marked_by"
    verb = "fertilized"
    kind = TreeCareRecord.KIND_FERTILIZER


# --- The operations pipeline's own dashboard UI (Harvest/ActualHarvest/
# HarvestTree, Fertilizer/ActualFertilizer/FertilizerTree —
# apps/operations/models.py). Built API-only through the last several
# rounds (see BUILD_STATUS.md); requested directly, alongside making
# farm/block yield actually visible once this pipeline has somewhere to
# get its data from. Deliberately a separate model/path from
# TreeCareRecord's own mark_harvested/mark_fertilized flows above — see
# _MarkTreesView's own docstring for why those two don't share a table. ---


class _RecordActualMixin(_MarkTreesActionMixin):
    """Shared plumbing for RecordActualHarvestView/RecordActualFertilizerView
    below — both create one "actual" row plus a per-tree row for every
    selected tree, from the exact same tree_ids/quantities encoding
    _MarkTreesActionMixin already reads, plus an optional `total_quantity`
    field (the actual/action-level manual total — a real, distinct field on
    ActualHarvest/ActualFertilizer that TreeCareRecord never had one for).
    """

    plan_model = None  # Harvest or Fertilizer — set by subclass
    actual_model = None  # ActualHarvest or ActualFertilizer — set by subclass
    tree_model = None  # HarvestTree or FertilizerTree — set by subclass
    plan_fk_field = None  # "harvest" or "fertilizer" — the FK name on actual_model
    actual_fk_field = None  # "actual_harvest" or "actual_fertilizer" — the FK name on tree_model
    noun = None  # "harvest" or "fertilizer application" — set by subclass
    # "weight_kg" for Harvest (asked directly to move every harvest record
    # from bunch count to kg — weight_kg is now the authoritative field on
    # both ActualHarvest and HarvestTree, see apps/operations/models.py)
    # or "quantity" for Fertilizer (already kg-native, unchanged) — the
    # field this view actually writes the reported total/per-tree value
    # into. `quantity` stays a separate, optional bunch-count field on the
    # Harvest side; nothing here ever touches it.
    value_field = None
    # When set, recording also completes the selected trees the way "Record
    # harvest directly" used to (last-done date/quantity, clears "needs …",
    # marking history) — kwargs for _mark_trees_done(). None keeps the two
    # systems separate.
    tree_completion = None

    def _total_quantity_from_request(self, request):
        raw = request.POST.get("total_quantity")
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _advance_plan_progress(self, plan, finished):
        """Recording an actual is what moves a plan out of "just created"
        and toward "done" — the piece this pipeline was missing entirely.
        `advance_status()` (apps/operations/services.py) can only ever
        advance *out of* created/on_progress_wait/done_wait; nothing set a
        plan TO on_progress_wait or done_wait in the first place, so a plan
        could never actually reach Done through the UI no matter how much
        real field work got recorded against it.

        Mirrors the original Laravel app's own behavior (Palm_Vision_PDR.md
        § 3.6 — `ActualFertilizerController::store` flips the parent's
        status to DONE_WAIT or ON_PROGRESS_WAIT depending on whether the
        submission was marked "finish"): the Manager still has to Approve
        the final done_wait -> done step (and that step still refuses to
        fire with zero actual records, per advance_status()'s own guard —
        this is what makes that guard reachable at all), but recording an
        actual is what signals "this plan has real progress against it,"
        the same one-click way "Record harvest directly" already worked.

        Never downgrades an already-Done plan — recording a further actual
        against a completed plan is legitimate historical record-keeping,
        not a reason to reopen it. The rule itself lives in
        apps.operations.services.record_actual_progress(), shared with the
        API path the PWA uses.
        """
        record_actual_progress(plan, finished)

    def _get_open_or_new_plan(self, block, date, total_quantity, fallback_quantity):
        """Reuses the block's own most recent still-open plan (status not
        yet DONE) if one exists — an explicit "Plan harvest"/"Plan
        fertilizer application" a Manager already created isn't silently
        duplicated by then clicking "Record actual" — otherwise creates a
        minimal one automatically, so recording an actual is a single click
        the same way "Record harvest directly" already is (requested
        directly rather than forcing a separate "create a plan first"
        step).
        """
        plan = (
            self.plan_model.objects.filter(farm_block=block)
            .exclude(status=PlanStatus.DONE)
            .order_by("-assessment_date")
            .first()
        )
        if plan is not None:
            return plan
        create_kwargs = {"farm_block": block, "assessment_date": date}
        if hasattr(self.plan_model, "assessment_quantity"):
            create_kwargs["assessment_quantity"] = (
                total_quantity if total_quantity is not None else (fallback_quantity or 0.0)
            )
        return self.plan_model.objects.create(**create_kwargs)

    def post(self, request, *args, **kwargs):
        block = self._get_block()
        trees = self._get_selected_trees(request, block)
        if trees is None:
            return self._no_selection_response(request, block)
        trees = list(trees)

        date = parse_date(request.POST.get("date") or "") or timezone.localdate()
        quantities = self._quantities_from_request(request)
        total_quantity = self._total_quantity_from_request(request)
        bad = _bad_amounts_message(date, {**quantities, "_total": total_quantity})
        if bad:
            return JsonResponse({"ok": False, "message": bad}, status=400)
        finished = request.POST.get("finished") == "1"
        per_tree = {tree.id: self._quantity_for(quantities, tree.id) for tree in trees}
        fallback_quantity = sum(q for q in per_tree.values() if q is not None)

        with transaction.atomic():
            plan = self._get_open_or_new_plan(block, date, total_quantity, fallback_quantity)
            actual = self.actual_model.objects.create(
                **{self.plan_fk_field: plan}, date=date, **{self.value_field: total_quantity},
            )
            self.tree_model.objects.bulk_create([
                self.tree_model(
                    **{self.actual_fk_field: actual}, tree=tree, **{self.value_field: per_tree[tree.id]},
                    recorded_by=request.user,
                )
                for tree in trees
            ])
            self._advance_plan_progress(plan, finished)
            if self.tree_completion:
                self._mark_trees_done(request, trees, date, per_tree, **self.tree_completion)
        # Reads plan.status AFTER _advance_plan_progress() rather than
        # assuming from `finished` — an already-Done plan is deliberately
        # left untouched by that call, so the message has to reflect what
        # actually happened, not what was requested.
        status_notes = {
            PlanStatus.DONE: "plan already marked done",
            PlanStatus.DONE_WAIT: "plan awaiting approval as done",
            PlanStatus.ON_PROGRESS_WAIT: "plan marked in progress",
        }
        status_note = status_notes.get(plan.status, f"plan status: {plan.get_status_display()}")
        message = (
            f"Recorded actual {self.noun} for {len(trees)} tree{'s' if len(trees) != 1 else ''} "
            f"— {status_note}."
        )
        return self._success_response(request, block, message)


class RecordActualHarvestView(_RecordActualMixin, ManagerOrSuperAdminRequiredMixin, View):
    """Creates one ActualHarvest + a HarvestTree row per selected tree — see
    _RecordActualMixin above for the shared logic. Per-tree weights are
    saved exactly as typed (a blank stays blank —
    HarvestTree.weight_kg=None — not auto-filled with a split share;
    that's what HarvestTree.effective_quantity already computes at read
    time).
    """

    plan_model = Harvest
    actual_model = ActualHarvest
    tree_model = HarvestTree
    plan_fk_field = "harvest"
    actual_fk_field = "actual_harvest"
    noun = "harvest"
    value_field = "weight_kg"
    # Asked directly to consolidate the Harvest panel to two actions on one
    # system: recording a harvest now also does what "Record harvest
    # directly" did to the trees, so there's no separate button whose
    # records never reach approval or yield. Harvest only for now — the
    # Fertilizer panel waits on the user's go-ahead.
    tree_completion = {
        "date_field": "last_harvested_at",
        "quantity_field": "last_harvest_quantity",
        "marked_at_field": "harvest_marked_at",
        "marked_by_field": "harvest_marked_by",
        "kind": TreeCareRecord.KIND_HARVEST,
    }


class RecordActualFertilizerView(_RecordActualMixin, ManagerOrSuperAdminRequiredMixin, View):
    """Fertilizer's counterpart to RecordActualHarvestView above."""

    plan_model = Fertilizer
    actual_model = ActualFertilizer
    tree_model = FertilizerTree
    plan_fk_field = "fertilizer"
    actual_fk_field = "actual_fertilizer"
    noun = "fertilizer application"
    tree_completion = {
        "date_field": "last_fertilized_at",
        "quantity_field": "last_fertilizer_quantity",
        "marked_at_field": "fertilizer_marked_at",
        "marked_by_field": "fertilizer_marked_by",
        "kind": TreeCareRecord.KIND_FERTILIZER,
    }
    value_field = "quantity"


class _PlanUpdateView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Shared Update logic for Harvest/Fertilizer plans — same AJAX-first
    shape as FarmBlockUpdateView, reusing the exact same create forms
    (HarvestPlanForm/FertilizerPlanForm) with `instance=` rather than a
    separate Update form, since the fields being edited are identical.
    """

    plan_model = None  # Harvest or Fertilizer — set by subclass
    plan_form_class = None  # HarvestPlanForm or FertilizerPlanForm — set by subclass
    partial_template_name = None  # the shared _partials/*.html fragment — set by subclass
    url_kwarg = None  # "harvest_id" or "fertilizer_id" — set by subclass
    plan_noun = None  # "Harvest" or "Fertilizer" — set by subclass

    def _get_block(self):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=self.kwargs["block_id"])
        _require_manager_for_farm(self.request.user, block.farm)
        return block

    def _get_plan(self, block):
        return get_object_or_404(self.plan_model, id=self.kwargs[self.url_kwarg], farm_block=block)

    def get(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.plan = self._get_plan(self.block)
        self.form = self.plan_form_class(instance=self.plan)
        if _is_ajax(request):
            return JsonResponse(
                {"html": render_to_string(self.partial_template_name, self.get_context_data(), request=request)}
            )
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.block = self._get_block()
        self.plan = self._get_plan(self.block)
        self.form = self.plan_form_class(request.POST, instance=self.plan)
        if not self.form.is_valid():
            if _is_ajax(request):
                return JsonResponse(
                    {
                        "ok": False,
                        "html": render_to_string(self.partial_template_name, self.get_context_data(), request=request),
                    },
                    status=400,
                )
            return self.render_to_response(self.get_context_data())
        self.form.save()
        message = f"{self.plan_noun} plan updated for “{self.block.name}”."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True, "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(self.block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={self.block.id}")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["farm_block"] = self.block
        context["plan"] = self.plan
        context["is_update"] = True
        return context


class HarvestPlanUpdateView(_PlanUpdateView):
    # Full page for a direct/non-AJAX GET (includes the partial below via
    # {% include %}, same relationship as HarvestPlanCreateView's own
    # template); the AJAX branch renders partial_template_name directly.
    template_name = "dashboard/harvest_plan_form.html"
    partial_template_name = "dashboard/_partials/harvest_plan_form.html"
    plan_model = Harvest
    plan_form_class = HarvestPlanForm
    url_kwarg = "harvest_id"
    plan_noun = "Harvest"


class FertilizerPlanUpdateView(_PlanUpdateView):
    template_name = "dashboard/fertilizer_plan_form.html"
    partial_template_name = "dashboard/_partials/fertilizer_plan_form.html"
    plan_model = Fertilizer
    plan_form_class = FertilizerPlanForm
    url_kwarg = "fertilizer_id"
    plan_noun = "Fertilizer"


class _PlanDeleteView(ManagerOrSuperAdminRequiredMixin, View):
    """POST-only, client-side window.confirm() gate (same pattern
    farm_map.html's removeTree() already uses) rather than a separate
    confirm page — deleting a plan cascades to its ActualHarvest/
    ActualFertilizer rows (and their tree rows), which is a legitimate
    correction (creating the wrong plan, or one auto-created behind the
    scenes that shouldn't have been), not a destructive-enough action to
    need FarmBlockDeleteView's stronger "refuse if used" guard.
    """

    plan_model = None  # Harvest or Fertilizer — set by subclass
    actuals_related_name = None  # "actual_harvests" or "actual_fertilizers" — set by subclass
    url_kwarg = None  # "harvest_id" or "fertilizer_id" — set by subclass
    plan_noun = None  # "Harvest" or "Fertilizer" — set by subclass

    def post(self, request, *args, **kwargs):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=kwargs["block_id"])
        _require_manager_for_farm(request.user, block.farm)
        plan = get_object_or_404(self.plan_model, id=kwargs[self.url_kwarg], farm_block=block)
        actual_count = getattr(plan, self.actuals_related_name).count()
        plan.delete()
        message = f"{self.plan_noun} plan deleted."
        if actual_count:
            message += f" ({actual_count} actual record{'s' if actual_count != 1 else ''} removed with it.)"
        if _is_ajax(request):
            return JsonResponse({
                "ok": True, "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")


class HarvestPlanDeleteView(_PlanDeleteView):
    plan_model = Harvest
    actuals_related_name = "actual_harvests"
    url_kwarg = "harvest_id"
    plan_noun = "Harvest"


class FertilizerPlanDeleteView(_PlanDeleteView):
    plan_model = Fertilizer
    actuals_related_name = "actual_fertilizers"
    url_kwarg = "fertilizer_id"
    plan_noun = "Fertilizer"


class _PlanApproveView(ManagerOrSuperAdminRequiredMixin, View):
    """Advances a plan's status one step via apps.operations.services'
    own advance_status() — reused directly rather than reimplementing the
    _TRANSITIONS table, same as apps/operations/views.py's own `approve`
    API action. farm_map.html only ever shows the button when
    can_advance(plan.status) is true (see _plan_entries() above), but this
    still re-validates server-side — a stale panel showing a now-invalid
    button shouldn't be trusted blindly.
    """

    plan_model = None  # Harvest or Fertilizer — set by subclass
    url_kwarg = None  # "harvest_id" or "fertilizer_id" — set by subclass
    plan_noun = None  # "Harvest" or "Fertilizer" — set by subclass

    def post(self, request, *args, **kwargs):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=kwargs["block_id"])
        _require_manager_for_farm(request.user, block.farm)
        plan = get_object_or_404(self.plan_model, id=kwargs[self.url_kwarg], farm_block=block)
        try:
            advance_status(plan)
        except DjangoValidationError as exc:
            message = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": message}, status=400)
            messages.error(request, message)
            return redirect(f"{reverse('dashboard:farms')}?block={block.id}")
        message = f"{self.plan_noun} plan advanced to “{plan.get_status_display()}”."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True, "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")


class HarvestPlanApproveView(_PlanApproveView):
    plan_model = Harvest
    url_kwarg = "harvest_id"
    plan_noun = "Harvest"


class FertilizerPlanApproveView(_PlanApproveView):
    plan_model = Fertilizer
    url_kwarg = "fertilizer_id"
    plan_noun = "Fertilizer"


class _ActualUpdateView(ManagerOrSuperAdminRequiredMixin, View):
    """Edits an existing ActualHarvest/ActualFertilizer — GET returns raw
    JSON (not an html fragment like every Django-form-backed view above)
    because the tree checklist it prefills is entirely client-rendered
    (renderTreeSelectionChecklist() in farm_map.html), not a server
    template; there's no Django form to render server-side for the
    per-tree rows. POST deletes and recreates the tree rows from the
    submitted selection rather than diffing add/removed/changed as three
    separate cases — simplest correct approach for what's realistically a
    handful of rows per action.
    """

    actual_model = None  # ActualHarvest or ActualFertilizer — set by subclass
    tree_model = None  # HarvestTree or FertilizerTree — set by subclass
    plan_fk_field = None  # "harvest" or "fertilizer" — set by subclass
    actual_fk_field = None  # "actual_harvest" or "actual_fertilizer" — set by subclass
    related_name = None  # "harvest_trees" or "fertilizer_trees" — set by subclass
    url_kwarg = None  # "actual_id" — set by subclass
    noun = None  # "harvest" or "fertilizer application" — set by subclass
    # "weight_kg" for Harvest, "quantity" for Fertilizer — see
    # _RecordActualMixin.value_field's own comment above for why.
    value_field = None

    def _get_block(self, request):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=self.kwargs["block_id"])
        _require_manager_for_farm(request.user, block.farm)
        return block

    def _get_actual(self, block):
        farm_block_lookup = f"{self.plan_fk_field}__farm_block"
        return get_object_or_404(
            self.actual_model.objects.select_related(self.plan_fk_field).prefetch_related(self.related_name),
            id=self.kwargs[self.url_kwarg], **{farm_block_lookup: block},
        )

    def get(self, request, *args, **kwargs):
        block = self._get_block(request)
        actual = self._get_actual(block)
        return JsonResponse({
            "ok": True,
            "actual": {
                "id": str(actual.id),
                "date": actual.date.isoformat(),
                "quantity": getattr(actual, self.value_field),
                "trees": [
                    {"id": str(row.tree_id), "quantity": getattr(row, self.value_field)}
                    for row in getattr(actual, self.related_name).all()
                ],
            },
        })

    def post(self, request, *args, **kwargs):
        block = self._get_block(request)
        actual = self._get_actual(block)
        try:
            tree_ids = _json.loads(request.POST.get("tree_ids") or "[]")
        except (TypeError, ValueError):
            tree_ids = []
        tree_ids = [str(i) for i in tree_ids] if isinstance(tree_ids, list) else []
        try:
            raw_quantities = _json.loads(request.POST.get("quantities") or "{}")
        except (TypeError, ValueError):
            raw_quantities = {}
        quantities = raw_quantities if isinstance(raw_quantities, dict) else {}

        def quantity_for(tree_id):
            raw = quantities.get(str(tree_id))
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        total_raw = request.POST.get("total_quantity")
        try:
            total_quantity = float(total_raw) if total_raw not in (None, "") else None
        except (TypeError, ValueError):
            total_quantity = None
        date = parse_date(request.POST.get("date") or "") or actual.date

        actual.date = date
        setattr(actual, self.value_field, total_quantity)
        actual.save(update_fields=["date", self.value_field])
        getattr(actual, self.related_name).all().delete()
        trees = Tree.objects.filter(id__in=tree_ids, farm_block=block, status=Tree.STATUS_ACTIVE)
        self.tree_model.objects.bulk_create([
            self.tree_model(
                **{self.actual_fk_field: actual, self.value_field: quantity_for(tree.id)}, tree=tree,
                recorded_by=request.user,
            )
            for tree in trees
        ])
        message = f"Updated actual {self.noun} record."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True, "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")


class ActualHarvestUpdateView(_ActualUpdateView):
    actual_model = ActualHarvest
    tree_model = HarvestTree
    plan_fk_field = "harvest"
    actual_fk_field = "actual_harvest"
    related_name = "harvest_trees"
    url_kwarg = "actual_id"
    noun = "harvest"
    value_field = "weight_kg"


class ActualFertilizerUpdateView(_ActualUpdateView):
    actual_model = ActualFertilizer
    tree_model = FertilizerTree
    plan_fk_field = "fertilizer"
    actual_fk_field = "actual_fertilizer"
    related_name = "fertilizer_trees"
    url_kwarg = "actual_id"
    noun = "fertilizer application"
    value_field = "quantity"


class _ActualDeleteView(ManagerOrSuperAdminRequiredMixin, View):
    """POST-only, client-side confirm — deletes an ActualHarvest/
    ActualFertilizer (cascades to its own tree rows only; the optional
    HarvestBatch link on ActualHarvest is SET_NULL, so this never orphans
    compliance data)."""

    actual_model = None  # ActualHarvest or ActualFertilizer — set by subclass
    plan_fk_field = None  # "harvest" or "fertilizer" — set by subclass
    url_kwarg = None  # "actual_id" — set by subclass
    noun = None  # "harvest" or "fertilizer application" — set by subclass

    def post(self, request, *args, **kwargs):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=kwargs["block_id"])
        _require_manager_for_farm(request.user, block.farm)
        farm_block_lookup = f"{self.plan_fk_field}__farm_block"
        actual = get_object_or_404(self.actual_model, id=kwargs[self.url_kwarg], **{farm_block_lookup: block})
        actual.delete()
        message = f"Deleted actual {self.noun} record."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True, "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "block", "id": str(block.id)},
            })
        messages.success(request, message)
        return redirect(f"{reverse('dashboard:farms')}?block={block.id}")


class ActualHarvestDeleteView(_ActualDeleteView):
    actual_model = ActualHarvest
    plan_fk_field = "harvest"
    url_kwarg = "actual_id"
    noun = "harvest"


class ActualFertilizerDeleteView(_ActualDeleteView):
    actual_model = ActualFertilizer
    plan_fk_field = "fertilizer"
    url_kwarg = "actual_id"
    noun = "fertilizer application"


class NotificationListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """The Manager's own in-app notification feed — apps/notifications/
    services.py's notify() is what actually populates this; this view only
    reads it. Not paginated beyond a simple recent-50 cap — a real
    paginated/filterable view is future work, not claimed here.
    """

    template_name = "dashboard/notifications.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["notifications"] = Notification.objects.filter(user=self.request.user)[:50]
        context["unread_count"] = Notification.objects.filter(user=self.request.user, read=False).count()
        return context

    def post(self, request, *args, **kwargs):
        """Mark one notification (`notification_id`) or all of them read —
        only ever the requesting user's own rows."""
        qs = Notification.objects.filter(user=request.user, read=False)
        note_id = request.POST.get("notification_id")
        if note_id:
            qs = qs.filter(id=note_id)
        qs.update(read=True)
        return redirect("dashboard:notifications")


# --- Imagery: upload, block-level processing, and the review queue —
# docs/06-ml-pipeline.md's pipeline had zero frontend before this (API-only
# since Phase 5); this is the first UI for it. Every permission check below
# mirrors apps/imagery/views.py's own checks exactly (same functions,
# same rules — see apps/common/permissions.py) rather than inventing a
# parallel authorization scheme for the dashboard. One simplification that
# IS a deliberate departure from the API: the API's review-queue actions
# are also open to a block's assigned PIC Block user
# (IsAssignedPicBlockOrManagerOrSuperAdmin); this dashboard has no PIC
# Block journey at all (see ManagerOrSuperAdminRequiredMixin), so the
# checks below only ever need to ask "is this a Manager of this block's
# farm, or a Super Admin" — a PIC Block user can't reach this dashboard to
# begin with. ---

def _require_manager_for_farm(user, farm):
    if user.is_superuser:
        return
    if user_role_on_farm(user, farm) == UserFarmRole.ROLE_MANAGER:
        return
    raise PermissionDenied("Only a Manager or Super Admin can do this.")


def _attach_processed_blocks(images):
    """Sets `.processed_block_runs` on each Image — the real
    ImageProcessingRun history against each block, one row per block this
    image has actually been *processed* against (most recent run only, if
    reprocessed), NOT the "Overlapping blocks" column this replaces (see
    BUILD_STATUS.md's matching entries) — that showed every block whose
    boundary merely intersects the image's whole footprint, whether or not
    anything was ever done with it. Requested directly, alongside the fix
    for the farm-map overlap this same distinction was the root cause of:
    a block genuinely hasn't been processed just because an image's
    footprint happens to reach it.

    One query for every image passed in (select_related, ordered so the
    per-block dedup below can just keep the first row it sees), not one
    per row.
    """
    for image in images:
        image.processed_block_runs = []

    image_ids = [image.id for image in images]
    if not image_ids:
        return

    runs_by_image = defaultdict(list)
    seen_blocks_by_image = defaultdict(set)
    runs = (
        ImageProcessingRun.objects.filter(image_id__in=image_ids)
        .select_related("farm_block")
        .order_by("image_id", "farm_block__name", "-created_at")
    )
    for run in runs:
        seen = seen_blocks_by_image[run.image_id]
        if run.farm_block_id in seen:
            continue  # an older run against the same block — keep only the latest
        seen.add(run.farm_block_id)
        runs_by_image[run.image_id].append(run)

    for image in images:
        image.processed_block_runs = runs_by_image.get(image.id, [])


class ImageListUploadView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    template_name = "dashboard/imagery_list.html"

    def get(self, request, *args, **kwargs):
        self.form = ImageUploadForm(user=request.user)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.form = ImageUploadForm(request.POST, request.FILES, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data["farm"]
            _require_manager_for_farm(request.user, farm)  # belt-and-suspenders — see ImageUploadForm docstring
            image = self.form.save(commit=False)
            image.uploaded_by = request.user
            image.save()
            georeference_image_task.delay(str(image.id))
            messages.success(request, "Image uploaded — georeferencing has started.")
            # Straight into block selection, not back to the bare list — an
            # upload with nothing telling you which block(s) to go process
            # it against next was the actual reported gap (see
            # ImageConfirmBlocksView's docstring for the full reasoning).
            # Lands on the Farm Map's upload_confirm mode (FarmMapView),
            # the same map instance the Manager was just browsing, rather
            # than a separate confirm page — ImageConfirmBlocksView is kept
            # working as a direct-URL fallback (e.g. bookmarked/shared
            # links) but is no longer the primary path.
            redirect_url = f"{reverse('dashboard:farms')}?upload_image={image.id}"
            # Block(s) chosen in this form's own "Process against block(s)"
            # checklist — reported directly, this page had no such
            # checklist at all (only the Farm Map's own in-panel upload
            # flow did), so there was no way to pick blocks and have
            # processing start right away from here; you always landed on
            # a second page first. Passed through as repeated query params
            # so FarmMapView's own JS can auto-trigger processing the
            # moment georeferencing finishes — the exact same
            # startProcessingForPreselectedBlocks() path the Farm Map
            # panel's own upload flow already uses (see that function's
            # docstring), just fed from a URL param instead of an in-page
            # JS variable, since this is a real navigation between two
            # different pages, not one continuous JS session. Left
            # entirely optional (not required) — no blocks picked here
            # falls straight through to the existing, unchanged
            # overlap-suggested confirm screen, same as before this
            # checklist existed. Not re-validated here: ImageConfirmBlocksView's
            # POST (what that JS ultimately calls) already re-checks farm/
            # boundary scoping server-side regardless of where the block
            # IDs came from.
            for block_id in request.POST.getlist("block_id"):
                redirect_url += f"&preselect_block={block_id}"
            return redirect(redirect_url)
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        images = list(
            _viewset_queryset(ImageViewSet, self.request).select_related("farm", "uploaded_by")[:50]
        )
        _attach_processed_blocks(images)
        context["images"] = images
        context["chunk_size_bytes"] = getattr(settings, "IMAGE_UPLOAD_CHUNK_SIZE_BYTES", 8 * 1024 * 1024)

        # "Process against block(s)" checklist data, grouped by farm_id so
        # this page's own JS can re-render the checklist when the Farm
        # <select> changes — mirrors farm_map.html's renderUploadBlockChecklist()
        # exactly, including the same boundary requirement
        # ImageConfirmBlocksView's picker already enforces ("a boundary-less
        # block can never be processed against" — run_processing() itself
        # refuses).
        blocks_by_farm = defaultdict(list)
        for block in scoped_farm_blocks(self.request.user).filter(boundary__isnull=False).order_by("name").only(
            "id", "name", "farm_id"
        ):
            blocks_by_farm[str(block.farm_id)].append({"id": str(block.id), "name": block.name})
        context["blocks_by_farm"] = dict(blocks_by_farm)
        return context


class ImageDeleteView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Confirm-then-delete for a single Image — see
    apps/imagery/services.py's delete_image() for exactly what gets
    removed (the image and everything derived from it: processing runs,
    detections, health classifications and their patch images — never a
    Tree a confirmed detection created) and the one hard refusal
    (compliance-exempt images, checked server-side by delete_image()
    itself, not re-derived here — same "one source of truth" reasoning
    apply_storage_lifecycle() already established for the same check).
    """

    template_name = "dashboard/image_delete.html"

    def _get_image(self):
        image = get_object_or_404(Image, id=self.kwargs["image_id"])
        _require_manager_for_farm(self.request.user, image.farm)
        return image

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        image = self._get_image()
        context.update({
            "image": image,
            "run_count": ImageProcessingRun.objects.filter(image=image).count(),
            "detection_count": TreeDetection.objects.filter(processing_run__image=image).count(),
            "compliance_exempt": image.is_compliance_exempt(),
        })
        return context

    def post(self, request, *args, **kwargs):
        image = self._get_image()
        try:
            imagery_services.delete_image(image)
        except imagery_services.ImageDeletionRefused as exc:
            messages.error(request, str(exc))
            return redirect("dashboard:imagery-delete", image_id=image.id)
        messages.success(request, "Image deleted.")
        return redirect("dashboard:imagery")


class ImageConfirmBlocksView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """The step this upload flow was missing, reported live: upload only
    ever asked for a Farm (correctly — docs/06-ml-pipeline.md: "the
    operator uploads at the Farm level, not tied to one block, since one
    file can cover several blocks"), but nothing then connected that
    upload to "here's what to process it against." The only place that
    connection existed was BlockImageryView, reachable only if you already
    knew to navigate Farms & Blocks → a specific block → Imagery — nothing
    on the upload page itself pointed there. This view is that missing
    link: both upload paths (the plain form in ImageListUploadView.post()
    and the chunked-upload JS's completion handler in imagery_list.html)
    land here immediately after upload.

    Georeferencing is an async Celery task, so this page has two states:

    - Not yet georeferenced: a waiting state that auto-refreshes itself
      every few seconds (plain `setTimeout` + reload — georeferencing is
      normally seconds, not something that needs a real polling endpoint
      built for it) rather than the plain "reload it yourself in a moment"
      text the old imagery_list.html panel-note used — no reason to make a
      Manager babysit a page refresh for something the browser can just
      watch for them.
    - Georeferenced: renders the image's footprint on a Leaflet map (same
      library/pattern as MapView, no new dependency) against every
      boundary-having block on the same farm, so the Manager can see the
      actual spatial overlap rather than guess from a name. Blocks whose
      boundary intersects the footprint are pre-checked (same
      `boundary__intersects` logic BlockImageryView already uses to decide
      which images cover a block, just run in the other direction here) —
      docs/06-ml-pipeline.md step 5's "the file's footprint is checked
      against every block on the farm ... to suggest overlapping blocks."
      The Manager can still check/uncheck any block, including
      non-suggested ones (flight overshoot can mean a real, useful overlap
      that a strict intersection check misses at the edges) — confirming
      creates one ImageProcessingRun per checked block, exactly what
      TriggerProcessingView already does for a single block, just batched.

    A failed georeference has nothing to pick a block against — shown as a
    terminal error state instead, linking back to the upload page.

    Blocks with no boundary at all are left out of the picker entirely
    (not just unchecked) — run_processing() itself refuses to process
    against a boundary-less block (docs/06-ml-pipeline.md: "a block must
    have a drawn boundary polygon before any image belonging to it can be
    processed"), so offering to select one here would just be a checkbox
    that's guaranteed to fail the moment it's submitted.

    Update: POST now also branches on _is_ajax() — farm_map.html's own
    upload-confirm mode (whether reached via ?upload_image=<id> on load or
    triggered by an in-page upload, see FarmMapView/ImageListUploadView)
    submits this same endpoint via fetch() and expects JSON back, so that
    confirming never causes a page navigation either. This view's own GET
    template (imagery_confirm_blocks.html) still submits a real form for
    its direct-URL-fallback use, unchanged.
    """

    template_name = "dashboard/imagery_confirm_blocks.html"

    def _get_image(self):
        image = get_object_or_404(Image, id=self.kwargs["image_id"])
        _require_manager_for_farm(self.request.user, image.farm)
        return image

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        image = self._get_image()
        context["image"] = image
        context["confirm_data"] = _build_image_confirm_data(image)  # None while still georeferencing
        return context

    def post(self, request, *args, **kwargs):
        image = self._get_image()

        def fail(message):
            if _is_ajax(request):
                return JsonResponse({"ok": False, "message": message}, status=400)
            messages.error(request, message)
            return redirect("dashboard:imagery-confirm-blocks", image_id=image.id)

        if image.georeference_status != Image.GEOREFERENCE_DONE:
            return fail("This image hasn't finished georeferencing yet.")

        block_ids = request.POST.getlist("block_id")
        if not block_ids:
            return fail("Choose at least one block to process this image against.")

        blocks = list(FarmBlock.objects.filter(id__in=block_ids, farm=image.farm, boundary__isnull=False))
        if not blocks:
            return fail("None of the selected blocks were valid — nothing was started.")

        started, already = [], []
        for block in blocks:
            _, was_started = imagery_services.start_processing_run(image, block, request.user)
            (started if was_started else already).append(block.name)

        message = f"Processing started against: {', '.join(started)}." if started else "Nothing new started."
        if already:
            message += f" Already running for: {', '.join(already)}."
        if _is_ajax(request):
            return JsonResponse({
                "ok": True,
                "message": message,
                "farm_map_data": _build_farm_map_data(request),
                "focus": {"type": "farm", "id": str(image.farm_id)},
            })
        messages.success(request, message)
        # Update: lands back on the one Farm Map page, not the bare imagery
        # list — this whole step now normally happens inline on that map
        # (see FarmMapView), this standalone page is kept working as a
        # direct-URL fallback but its own success path should feel
        # consistent with the primary one.
        return redirect(f"{reverse('dashboard:farms')}?farm={image.farm_id}")


# --- Chunked/resumable upload — see apps/imagery/models.py's
# ImageUploadSession for the full "why chunked at all" reasoning. These
# four views are the dashboard-only JSON API imagery_list.html's upload
# script talks to via fetch(); ImageListUploadView.post() above is left
# fully intact as the plain-form fallback for browsers/situations where
# JS or fetch() isn't available, same progressive-enhancement convention
# the upload-progress feature already used before chunking existed.
# Deliberately not exposed on apps/imagery's DRF/JWT API — see that
# model's docstring for why this is dashboard-only.

class CreateUploadSessionView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request):
        try:
            payload = _json.loads(request.body)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid JSON body.")

        farm = get_object_or_404(_viewset_queryset(FarmViewSet, request), id=payload.get("farm"))
        _require_manager_for_farm(request.user, farm)  # belt-and-suspenders — see ImageUploadForm's docstring

        filename = payload.get("filename")
        total_chunks = payload.get("total_chunks")
        if not filename or not isinstance(total_chunks, int) or total_chunks < 1:
            return HttpResponseBadRequest("filename and a positive integer total_chunks are required.")

        session = imagery_services.create_upload_session(
            farm=farm,
            uploaded_by=request.user,
            filename=filename,
            total_chunks=total_chunks,
            captured_at=payload.get("captured_at") or None,
        )
        return JsonResponse({"session_id": str(session.id)})


class UploadSessionStatusView(ManagerOrSuperAdminRequiredMixin, View):
    """Lets a resumed page load ask "which chunks do you already have" for
    a session id it remembers from localStorage, rather than assuming
    nothing survived a browser reload/crash.
    """

    def get(self, request, session_id):
        session = get_object_or_404(ImageUploadSession, id=session_id)
        _require_manager_for_farm(request.user, session.farm)
        return JsonResponse(
            {
                "received_chunk_indexes": session.received_chunk_indexes,
                "total_chunks": session.total_chunks,
                "status": session.status,
            }
        )


class UploadChunkView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request, session_id, chunk_index):
        session = get_object_or_404(ImageUploadSession, id=session_id)
        _require_manager_for_farm(request.user, session.farm)
        if session.status == ImageUploadSession.STATUS_COMPLETE:
            return HttpResponseBadRequest("This upload has already been completed.")
        if not (0 <= chunk_index < session.total_chunks):
            return HttpResponseBadRequest("chunk_index is out of range for this session.")

        try:
            imagery_services.store_chunk(session, chunk_index, request.body)
        except imagery_services.EmptyChunkUpload as exc:
            # Surfaced as a 4xx so the client's retry logic can re-PUT this
            # specific chunk, rather than silently recording a truncated
            # one and finding out only much later, when the reassembled
            # file fails to open at all.
            return HttpResponseBadRequest(str(exc))
        return JsonResponse({"received_chunk_indexes": session.received_chunk_indexes})


class CompleteUploadSessionView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request, session_id):
        session = get_object_or_404(ImageUploadSession, id=session_id)
        _require_manager_for_farm(request.user, session.farm)
        try:
            image = imagery_services.finalize_upload_session(session)
        except imagery_services.UploadSessionIncomplete as exc:
            return HttpResponseBadRequest(str(exc))
        return JsonResponse({"image_id": str(image.id)})


class BlockImageryView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """A block's imagery: which uploaded images actually cover it (once
    georeferenced), and its processing-run history. Triggering a run is a
    separate POST-only view (TriggerProcessingView) rather than folded into
    this GET view, per normal Django form-handling convention.
    """

    template_name = "dashboard/imagery_block.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Context key is deliberately `farm_block`, not `block` — Django's
        # {% block %} template tag shadows a context variable literally
        # named `block` inside its own body (it uses that name internally
        # for {{ block.super }} support), which silently broke every
        # {{ block.* }} reference in this template until caught by actually
        # rendering it against realistic data rather than just parsing it.
        farm_block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, self.request), id=kwargs["block_id"])
        overlapping_images = (
            Image.objects.filter(
                farm=farm_block.farm,
                georeference_status=Image.GEOREFERENCE_DONE,
                footprint__intersects=farm_block.boundary,
            )
            if farm_block.boundary
            else Image.objects.none()
        )
        runs = ImageProcessingRun.objects.filter(farm_block=farm_block).select_related("image").order_by("-created_at")[:20]
        context.update({"farm_block": farm_block, "overlapping_images": overlapping_images, "runs": runs})
        return context


class TriggerProcessingView(ManagerOrSuperAdminRequiredMixin, View):
    """Mirrors apps/imagery/views.py's ProcessImageView exactly — same
    precondition (image must be georeferenced), same run-creation call,
    same task dispatch. Redirects into the review queue for the run it just
    created, since that's the natural next step for whoever just triggered it.
    """

    def post(self, request, block_id, image_id):
        block = get_object_or_404(_viewset_queryset(FarmBlockViewSet, request), id=block_id)
        _require_manager_for_farm(request.user, block.farm)
        image = get_object_or_404(Image, id=image_id, farm_id=block.farm_id)

        if image.georeference_status != Image.GEOREFERENCE_DONE:
            messages.error(request, "This image hasn't finished georeferencing yet — try again shortly.")
            return redirect("dashboard:imagery-block", block_id=block.id)

        run, started = imagery_services.start_processing_run(image, block, request.user)
        if started:
            messages.success(request, "Processing started.")
        else:
            messages.info(request, "This image is already being processed against this block — showing that run.")
        return redirect("dashboard:imagery-run", run_id=run.id)


class RunStatusView(ManagerOrSuperAdminRequiredMixin, View):
    """Polled by imagery_review.html while a run is still working, so the
    "detecting"/"classifying" wait has a real, measured progress bar
    instead of just a static status word — matching the chunked-upload
    flow's own progress bar (imagery_list.html), which this was asked to
    be "like." See ImageProcessingRun.progress_percent for why this is a
    real percentage (tiles processed / total during detection, detections
    classified / total during classification — see
    apps/imagery/services.py's run_processing()), not a guessed one.
    """

    def get(self, request, run_id):
        run = get_object_or_404(ImageProcessingRun.objects.select_related("farm_block__farm"), id=run_id)
        _require_manager_for_farm(request.user, run.farm_block.farm)
        return JsonResponse({
            "status": run.status,
            "status_display": run.get_status_display(),
            "progress_percent": run.progress_percent,
            "progress_current": run.progress_current,
            "progress_total": run.progress_total,
            "detected_tree_count": run.detected_tree_count,
            "new_candidate_count": run.new_candidate_count,
            "error_message": run.error_message,
            "done": run.status in (ImageProcessingRun.STATUS_READY, ImageProcessingRun.STATUS_FAILED),
        })


# How many nearest existing trees the review queue offers per detection in
# its "Link to existing tree" dropdown — see ReviewQueueView. Small on
# purpose: this mirrors the "nearest 2-3 candidates, manual confirmation"
# rule CLAUDE.md sets for the field PWA's own proximity suggestions,
# loosened slightly here since a Manager reviewing on a desktop has more
# context on screen than a field worker on a phone.
REVIEW_LINK_CANDIDATE_COUNT = 5


class ReviewQueueView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """docs/04-api-spec.md review-queue actions (confirm/reject/link), and
    docs/10-user-journeys.md Manager journey step 3: "Reviews the detection
    queue. Confirms genuine new plantings, rejects false positives."

    Filtered on *two* conditions, not just match_status: `tree__isnull=True`
    in addition to `match_status in (new_candidate, ambiguous)`. This
    matters because apps/imagery/services.py's confirm_new_candidate()
    deliberately leaves match_status as "new_candidate" after confirming —
    it only sets `.tree` — since match_status records the pipeline's own
    automatic-matching outcome, a historical fact, while `.tree` records
    what's linked *now* regardless of how (see that function's docstring).
    Without the tree__isnull=True check, a detection a Manager just
    confirmed would still show up in this same queue right after, forever —
    caught by a test that actually confirmed a detection and then reloaded
    the queue, not by reasoning about the filter in isolation.
    """

    template_name = "dashboard/imagery_review.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        run = get_object_or_404(
            ImageProcessingRun.objects.select_related("farm_block__farm", "image"), id=kwargs["run_id"]
        )
        _require_manager_for_farm(self.request.user, run.farm_block.farm)
        detections = run.detections.filter(
            match_status__in=[TreeDetection.MATCH_NEW_CANDIDATE, TreeDetection.MATCH_AMBIGUOUS],
            tree__isnull=True,
        ).select_related("classification").order_by("-detection_confidence")
        detections = list(detections)

        # The "Link to existing tree" dropdown used to render EVERY active
        # tree in the block, inside every detection card's <select> — for a
        # real block that's (trees x detections) <option> elements, e.g.
        # 500 x 47 = 23,500 nodes of pure page weight, and a dropdown a
        # human can't realistically pick the right entry out of anyway. It
        # was also called "nearby_trees" while being nothing of the sort.
        #
        # Each card now offers only the genuinely nearest few trees, which
        # is both the useful set and a bounded one — and matches this
        # project's own stated proximity rule (docs/05-offline-sync.md /
        # CLAUDE.md: show the nearest 2-3 candidates and require manual
        # confirmation, never auto-select). Computed in Python over the
        # already-fetched trees rather than one ORM distance query per
        # detection, the same in-memory-geometry trade-off
        # _build_farm_map_data() already makes.
        block_trees = list(
            Tree.objects.filter(farm_block=run.farm_block, status=Tree.STATUS_ACTIVE)
            .select_related("farm_block").only("id", "name", "number", "location", "farm_block__name")
        )
        for detection in detections:
            # GEOSGeometry.distance() on SRID 4326 returns DEGREES, not
            # meters — fine for ranking "which is nearest" within one
            # block, which is all this is used for. Deliberately not
            # surfaced as a distance number anywhere in the template:
            # showing degrees labelled as a distance would be wrong, and
            # converting properly needs a projected CRS this doesn't have.
            detection.link_candidates = sorted(
                block_trees, key=lambda tree: detection.detected_location.distance(tree.location)
            )[:REVIEW_LINK_CANDIDATE_COUNT]

        # How many of `detections` ConfirmAllDetectionsView would actually
        # act on — computed here (not just len(detections)) so the "Confirm
        # all" button's own label/empty-state can be honest about excluding
        # ambiguous ones, same restriction that view enforces server-side.
        confirmable_count = sum(1 for d in detections if d.match_status == TreeDetection.MATCH_NEW_CANDIDATE)
        health_qs = TreeHealthClassification.objects.filter(
            tree_detection__processing_run=run, reviewed=False,
            confidence__lt=TreeHealthClassification.REVIEW_THRESHOLD,
        )
        health_checks_total = health_qs.count()
        health_checks = list(
            health_qs.select_related("tree_detection__tree__farm_block").order_by("confidence")[:60]
        )
        context.update({
            "health_checks": health_checks,
            "health_checks_total": health_checks_total,
            "run": run,
            "detections": detections,
            "confirmable_count": confirmable_count,
            "has_block_trees": bool(block_trees),
        })
        return context


class ConfirmDetectionView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request, detection_id):
        detection = get_object_or_404(
            TreeDetection.objects.select_related("processing_run__farm_block__farm"), id=detection_id
        )
        _require_manager_for_farm(request.user, detection.processing_run.farm_block.farm)
        if detection.match_status != TreeDetection.MATCH_NEW_CANDIDATE:
            messages.error(request, f"Only new-candidate detections can be confirmed (this one is {detection.match_status}).")
        else:
            imagery_services.confirm_new_candidate(detection, request.user)
            messages.success(request, "Detection confirmed as a new tree.")
        return redirect("dashboard:imagery-run", run_id=detection.processing_run_id)


class ConfirmAllDetectionsView(ManagerOrSuperAdminRequiredMixin, View):
    """Bulk counterpart to ConfirmDetectionView — confirms every
    new_candidate detection still in this run's review queue as a new
    tree, in one action, instead of clicking "Confirm as new tree" once
    per card.

    Deliberately excludes 'ambiguous' detections — the exact same
    restriction ConfirmDetectionView already enforces one at a time (see
    its own match_status check above). Ambiguous means match_detection()
    found either two-or-more existing active trees within the match
    threshold, or a removed ("possible regrowth") tree nearby — silently
    creating a brand-new Tree for each of those risks duplicating the
    registry, which is exactly what having a Manager resolve them
    individually (Link to the right existing tree, or Reject) is there to
    prevent. Only genuine new_candidate detections (nothing nearby at all)
    are safe to accept in bulk.
    """

    def post(self, request, run_id):
        run = get_object_or_404(ImageProcessingRun.objects.select_related("farm_block__farm", "image"), id=run_id)
        _require_manager_for_farm(request.user, run.farm_block.farm)
        detections = list(
            run.detections.filter(match_status=TreeDetection.MATCH_NEW_CANDIDATE, tree__isnull=True)
            .select_related("processing_run__farm_block", "processing_run__image")
        )

        # Clip the source raster at most once for the whole batch, and only
        # if some detection actually still needs classifying — see
        # confirm_new_candidate()'s `clipped` parameter for why: letting
        # each detection clip for itself would repeat a full masked read of
        # a multi-GB orthomosaic once per detection, inside this one
        # synchronous request. Normally nothing needs it at all (every
        # detection was already classified during the run itself), so this
        # stays None and no clip ever happens.
        clipped = None
        if detections and TreeHealthClassification.objects.filter(
            tree_detection__in=detections
        ).count() < len(detections):
            try:
                clipped = imagery_services.clip_to_block(
                    run.image, run.farm_block, max_gsd_m=run.farm_block.effective_detection_target_gsd_m()
                )
            except Exception:  # noqa: BLE001 — same trade-off confirm_new_candidate() makes: a missing health label must never block confirmation
                clipped = None

        for detection in detections:
            imagery_services.confirm_new_candidate(detection, request.user, clipped=clipped)
        if detections:
            count = len(detections)
            messages.success(request, f"Confirmed {count} new candidate{'s' if count != 1 else ''} as trees.")
        else:
            messages.info(
                request,
                "No new-candidate detections to confirm — remaining detections are ambiguous "
                "(two or more nearby trees, or a possible regrowth) and need individual review: "
                "link to the correct existing tree, or reject.",
            )
        return redirect("dashboard:imagery-run", run_id=run.id)


class ReviewClassificationView(ManagerOrSuperAdminRequiredMixin, View):
    """Human check of a low-confidence health label (docs/06-ml-pipeline.md
    step 4). Posting a label records it as the correction (when it differs
    from the model's), marks the result reviewed, and — since a reviewed
    result is trustworthy — updates the tree's health if it has one."""

    def post(self, request, classification_id):
        classification = get_object_or_404(
            TreeHealthClassification.objects.select_related("tree_detection__processing_run__farm_block__farm", "tree_detection__tree"),
            id=classification_id,
        )
        run = classification.tree_detection.processing_run
        _require_manager_for_farm(request.user, run.farm_block.farm)
        label = request.POST.get("label")
        if label not in dict(TreeHealthClassification.HEALTH_CHOICES):
            messages.error(request, "Choose Healthy, Yellow or Unknown.")
            return redirect(reverse("dashboard:imagery-run", kwargs={"run_id": run.id}) + "#health-checks")
        classification.corrected_label = "" if label == classification.health_label else label
        classification.reviewed = True
        classification.reviewed_by = request.user
        classification.save(update_fields=["corrected_label", "reviewed", "reviewed_by"])
        tree = classification.tree_detection.tree
        if tree is not None:
            tree.current_health = classification.effective_label
            tree.save(update_fields=["current_health", "updated_at"])
        messages.success(request, f"Health set to {label}.")
        return redirect(reverse("dashboard:imagery-run", kwargs={"run_id": run.id}) + "#health-checks")


class RejectDetectionView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request, detection_id):
        detection = get_object_or_404(
            TreeDetection.objects.select_related("processing_run__farm_block__farm"), id=detection_id
        )
        _require_manager_for_farm(request.user, detection.processing_run.farm_block.farm)
        if detection.tree_id:
            messages.error(request, "This detection is already linked to a tree — it can't be rejected.")
        else:
            detection.match_status = TreeDetection.MATCH_REJECTED
            detection.save(update_fields=["match_status"])
            messages.success(request, "Detection rejected.")
        return redirect("dashboard:imagery-run", run_id=detection.processing_run_id)


class LinkDetectionView(ManagerOrSuperAdminRequiredMixin, View):
    def post(self, request, detection_id):
        detection = get_object_or_404(
            TreeDetection.objects.select_related("processing_run__farm_block__farm"), id=detection_id
        )
        _require_manager_for_farm(request.user, detection.processing_run.farm_block.farm)
        tree_id = request.POST.get("tree_id")
        if not tree_id:
            messages.error(request, "Choose a tree to link this detection to.")
            return redirect("dashboard:imagery-run", run_id=detection.processing_run_id)
        tree = get_object_or_404(
            Tree, id=tree_id, farm_block=detection.processing_run.farm_block, status=Tree.STATUS_ACTIVE
        )
        detection.tree = tree
        detection.match_status = TreeDetection.MATCH_MATCHED
        detection.save(update_fields=["tree", "match_status"])
        messages.success(request, f"Detection linked to {tree}.")
        return redirect("dashboard:imagery-run", run_id=detection.processing_run_id)


# --- Compliance: document filing and the due-diligence export —
# docs/10-user-journeys.md Manager journey steps 5 ("Renews a compliance
# document. Uploads a renewal before it lapses, not after an audit catches
# it.") and, loosely, the traceability half of step 6. Scoped to
# ComplianceDocument and HarvestBatch only for this first pass —
# ComplianceAudit/ChecklistItem/DeforestationBaseline/GrievanceRecord have
# no dashboard screens yet (see BUILD_STATUS.md), tracked as further work
# rather than stubbed here. ---

class ComplianceDocumentListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    template_name = "dashboard/compliance_documents.html"

    def get(self, request, *args, **kwargs):
        self.form = ComplianceDocumentForm(user=request.user)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.form = ComplianceDocumentForm(request.POST, request.FILES, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data.get("farm")
            farm_block = self.form.cleaned_data.get("farm_block")
            target_farm = farm or (farm_block.farm if farm_block else None)
            _require_manager_for_farm(request.user, target_farm)
            self.form.save()
            messages.success(request, "Compliance document saved.")
            return redirect("dashboard:compliance-documents")
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["documents"] = (
            _viewset_queryset(ComplianceDocumentViewSet, self.request)
            .select_related("farm", "farm_block__farm")[:100]
        )
        return context


class HarvestBatchListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """docs/07-compliance.md § EUDR-specific functions. The due-diligence
    export is triggered and shown on the same page (not a redirect to a
    separate result page) so a validation failure — or the result itself —
    is never lost to a follow-up GET, same reasoning as
    ImageListUploadView's invalid-form handling.
    """

    template_name = "dashboard/compliance_batches.html"

    def post(self, request, *args, **kwargs):
        batch = get_object_or_404(
            _viewset_queryset(HarvestBatchViewSet, request), id=request.POST.get("batch_id")
        )
        _require_manager_for_farm(request.user, batch.farm_block.farm)
        self.export_batch_id = batch.id
        self.export_result = None
        try:
            self.export_result = compliance_services.assemble_due_diligence_package(batch)
        except compliance_services.DueDiligenceExportRefused as exc:
            messages.error(request, str(exc))
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batches"] = (
            _viewset_queryset(HarvestBatchViewSet, self.request).select_related("farm_block__farm")[:100]
        )
        context["export_result"] = getattr(self, "export_result", None)
        context["export_batch_id"] = getattr(self, "export_batch_id", None)
        return context


class DeforestationBaselineListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """docs/07-compliance.md's per-block EUDR deforestation-free
    determination. Read + a single targeted action (post a new status,
    stamping verified_by/verified_at server-side) — mirrors
    DeforestationBaselineViewSet.verify() exactly rather than adding a
    general edit form, the same "no bare edit surface, only specific
    actions" shape this dashboard already uses elsewhere (e.g. the map
    panel's read-only design, this module's own docstring).
    """

    template_name = "dashboard/compliance_baselines.html"

    def post(self, request, *args, **kwargs):
        baseline = get_object_or_404(
            _viewset_queryset(DeforestationBaselineViewSet, request), id=request.POST.get("baseline_id")
        )
        _require_manager_for_farm(request.user, baseline.farm_block.farm)
        new_status = request.POST.get("status")
        if new_status not in dict(DeforestationBaseline.STATUS_CHOICES):
            messages.error(request, "Invalid status.")
        else:
            baseline.status = new_status
            baseline.verified_by = request.user
            baseline.verified_at = timezone.now()
            baseline.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
            messages.success(request, "Deforestation baseline updated.")
        return redirect("dashboard:compliance-baselines")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["baselines"] = (
            _viewset_queryset(DeforestationBaselineViewSet, self.request)
            .select_related("farm_block__farm", "verified_by")[:100]
        )
        context["status_choices"] = DeforestationBaseline.STATUS_CHOICES
        return context


class ComplianceAuditListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """Read-only, deliberately — docs/07-compliance.md's audits are scored
    against a ComplianceAuditTemplate's checklist (potentially dozens of
    ChecklistItem rows per audit, each with its own pass/fail/n-a result and
    optional evidence file), which is real structured-capture-form work.
    This dashboard's job for Phase 8/prototype scope is making audit
    results *visible* to a Manager (including the per-checklist-item
    breakdown, via ComplianceAuditViewSet's own results prefetch) — not
    re-implementing that capture form; audits are created via the API or
    /admin/ for now, same as ComplianceAuditTemplate/ChecklistItem
    themselves (neither has a dashboard authoring screen either, for the
    same reason).
    """

    template_name = "dashboard/compliance_audits.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["audits"] = (
            _viewset_queryset(ComplianceAuditViewSet, self.request)
            .select_related("farm_block__farm", "template")
            .prefetch_related("results__checklist_item")[:100]
        )
        return context


class GrievanceListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    """docs/07-compliance.md grievance logging. Log-a-new-one form (like
    ComplianceDocumentListView) plus a targeted status-progression action
    (GrievanceUpdateStatusView below) rather than a general edit form —
    same shape as DeforestationBaselineListView above.
    """

    template_name = "dashboard/compliance_grievances.html"

    def get(self, request, *args, **kwargs):
        self.form = GrievanceRecordForm(user=request.user)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.form = GrievanceRecordForm(request.POST, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data["farm"]
            _require_manager_for_farm(request.user, farm)
            self.form.save()
            messages.success(request, "Grievance logged.")
            return redirect("dashboard:compliance-grievances")
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["grievances"] = (
            _viewset_queryset(GrievanceRecordViewSet, self.request).select_related("farm")[:100]
        )
        context["status_choices"] = GrievanceRecord.STATUS_CHOICES
        return context


class GrievanceUpdateStatusView(ManagerOrSuperAdminRequiredMixin, View):
    """Targeted status-progression action (open -> investigating ->
    resolved, with resolution notes) — see GrievanceListView's docstring."""

    def post(self, request, grievance_id):
        grievance = get_object_or_404(
            _viewset_queryset(GrievanceRecordViewSet, request), id=grievance_id
        )
        _require_manager_for_farm(request.user, grievance.farm)
        new_status = request.POST.get("status")
        if new_status not in dict(GrievanceRecord.STATUS_CHOICES):
            messages.error(request, "Invalid status.")
        else:
            grievance.status = new_status
            grievance.resolution_notes = request.POST.get("resolution_notes", grievance.resolution_notes)
            grievance.save(update_fields=["status", "resolution_notes", "updated_at"])
            messages.success(request, "Grievance updated.")
        return redirect("dashboard:compliance-grievances")


# --- Reporting — docs/10-user-journeys.md Manager journey step 6: "Exports
# a report. Pulls a monthly production report to share with ownership or
# an auditor — a real file, not a dashboard screenshot." ---

class ReportDefinitionListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    template_name = "dashboard/reports.html"

    def get(self, request, *args, **kwargs):
        self.form = ReportDefinitionForm(user=request.user)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.form = ReportDefinitionForm(request.POST, user=request.user)
        if self.form.is_valid():
            farm = self.form.cleaned_data["farm"]
            _require_manager_for_farm(request.user, farm)
            definition = self.form.save(commit=False)
            definition.created_by = request.user
            definition.save()
            # commit=False skips ModelForm's own M2M save — subscribers
            # would otherwise silently end up empty no matter what was
            # checked on the form (caught by a live-Postgres test run,
            # not in the sandbox — see BUILD_STATUS.md).
            self.form.save_m2m()
            messages.success(request, "Report definition created.")
            return redirect("dashboard:reports")
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = self.form
        context["definitions"] = (
            _viewset_queryset(ReportDefinitionViewSet, self.request).select_related("farm")[:50]
        )
        return context


class TriggerReportRunView(ManagerOrSuperAdminRequiredMixin, View):
    """Mirrors apps/reporting/views.py's ReportDefinitionViewSet.run()
    action exactly — same ReportRun creation, same task dispatch.
    """

    def post(self, request, definition_id):
        definition = get_object_or_404(
            _viewset_queryset(ReportDefinitionViewSet, request), id=definition_id
        )
        run = ReportRun.objects.create(definition=definition, triggered_by=request.user)
        generate_report_task.delay(str(run.id))
        messages.success(request, "Report run started.")
        return redirect("dashboard:report-runs", definition_id=definition.id)


class ReportRunListView(ManagerOrSuperAdminRequiredMixin, TemplateView):
    template_name = "dashboard/report_runs.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        definition = get_object_or_404(
            _viewset_queryset(ReportDefinitionViewSet, self.request), id=kwargs["definition_id"]
        )
        runs = ReportRun.objects.filter(definition=definition).order_by("-created_at")[:50]
        context.update({"definition": definition, "runs": runs})
        return context


# --- Map panel — docs/09-design-system.md's "Office dashboard ... sidebar
# nav, metric cards, map panel." Read-only: shows block boundaries, active
# trees (colored by current_health), and georeferenced image footprints,
# all scoped exactly like every other dashboard view. Every popup links
# into an existing per-block dashboard page rather than adding any new
# bare "act on whatever's under the pin" shortcut — see this module's
# docstring for why that distinction matters here specifically. Every
# feature carries `farm_id` so the template's farm filter can select
# client-side without a round trip. ---

# MapView (old, separate map page) removed — superseded by FarmMapView,
# which merges browsing, farm/block/tree management, imagery-upload
# confirmation, and harvest-plan creation into one map + contextual panel.
# See docs/10-user-journeys.md and BUILD_STATUS.md "Farm Map unification"
# for why the old Farms&Blocks / Map / block-trees pages were retired.
