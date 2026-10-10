"""
Login gating for the office dashboard — docs/10-user-journeys.md deliberately
describes only two journeys (PIC Block field work, Manager oversight) and
says explicitly "No Super Admin journey — user/role/farm administration is
comparatively straightforward CRUD" (i.e. handled by /admin/, not this app).
There's no PIC Block dashboard journey at all — field staff use the PWA at
/field/. A PIC-Block-only user who authenticates correctly but holds no
Manager/Super-Admin standing anywhere is refused here with a clear message,
not silently shown an empty (or worse, partially-scoped) dashboard.
"""
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.db import models

from apps.common.scoping import managed_farm_ids
from apps.compliance.models import ComplianceDocument, GrievanceRecord
from apps.farms.models import Farm, FarmBlock, UserFarmRole
from apps.imagery.models import Image
from apps.operations.models import Fertilizer, Harvest
from apps.reporting.models import ReportDefinition
from apps.reporting.services import IMPLEMENTED_REPORT_TYPES, eligible_subscribers
from apps.trees.models import Tree


class DashboardAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.is_superuser:
            return
        if UserFarmRole.objects.filter(user=user, role=UserFarmRole.ROLE_MANAGER).exists():
            return
        raise forms.ValidationError(
            "This dashboard is for Managers and Super Admins. "
            "PIC Block field staff should use the field app at /field/ instead.",
            code="not_office_staff",
        )


class _PlainEmptyLabelsMixin:
    """Replaces Django's default "---------" placeholder in dropdowns with
    words. Runs after the form's own __init__, so querysets narrowed there
    are kept."""

    EMPTY_LABELS = {"farm": "Choose a farm…", "farm_block": "Choose a block…", "pic": "No PIC Block assigned"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label in self.EMPTY_LABELS.items():
            field = self.fields.get(name)
            if field is not None and getattr(field, "empty_label", None) is not None:
                field.empty_label = label


class ImageUploadForm(_PlainEmptyLabelsMixin, forms.ModelForm):
    """docs/06-ml-pipeline.md: "upload happens once back on real bandwidth,
    from the office dashboard — not the field PWA" — this is that upload
    path, first built here. `farm` is restricted to farms the requesting
    user actually manages (not merely sees), same rule
    apps/imagery/views.py's ImageViewSet.perform_create() enforces
    server-side — restricting the field's queryset here is a UX nicety
    (no point offering a farm choice that would just get rejected), the
    view still re-checks server-side since a form field restriction alone
    is never a substitute for that (see ImageListUploadView.post()).
    """

    class Meta:
        model = Image
        fields = ["farm", "raw_file", "captured_at"]
        widgets = {"captured_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["captured_at"].required = False
        if user is not None and not user.is_superuser:
            self.fields["farm"].queryset = Farm.objects.filter(id__in=managed_farm_ids(user))


class ComplianceDocumentForm(_PlainEmptyLabelsMixin, forms.ModelForm):
    """docs/10-user-journeys.md Manager journey step 5: "Renews a compliance
    document. Uploads a renewal before it lapses." `farm`/`farm_block` are
    both optional at the form level — ComplianceDocument.clean()/save()
    (apps/compliance/models.py) already enforce "exactly one of the two",
    and ModelForm's own validation surfaces that as a normal form error via
    the model's clean(), so it isn't duplicated here. Both fields are
    restricted to farms/blocks the user manages, same reasoning as
    ImageUploadForm above.
    """

    class Meta:
        model = ComplianceDocument
        fields = [
            "farm",
            "farm_block",
            "document_type",
            "document_file",
            "issued_date",
            "expiry_date",
            "issuing_authority",
        ]
        widgets = {
            "issued_date": forms.DateInput(attrs={"type": "date"}),
            "expiry_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("farm", "farm_block", "issued_date", "expiry_date", "issuing_authority"):
            self.fields[field_name].required = False
        if user is not None and not user.is_superuser:
            managed_ids = list(managed_farm_ids(user))
            self.fields["farm"].queryset = Farm.objects.filter(id__in=managed_ids)
            self.fields["farm_block"].queryset = FarmBlock.objects.filter(farm_id__in=managed_ids)


class ReportDefinitionForm(_PlainEmptyLabelsMixin, forms.ModelForm):
    """docs/10-user-journeys.md Manager journey step 6: "Exports a report."
    `farm` restricted to managed farms, same pattern as the two forms
    above.

    `subscribers` is now exposed (Update: previously left off — see
    BUILD_STATUS.md). Choices are narrowed to office staff (users holding
    any UserFarmRole as Manager, plus Super Admins) rather than every User
    in the system — subscribing an arbitrary PIC Block or unrelated account
    to a recurring report email isn't a real use case docs/10-user-journeys.md
    describes, and an unfiltered `ModelMultipleChoiceField` would make that
    trivially easy to fat-finger.

    `filters` (JSONField: optional farm_block_id/date_from/date_to) is still
    left off this form — it has no natural fixed set of widgets (the block
    choices depend on which farm is picked, which this static ModelForm
    can't react to without added JS), and defaults already make date_from/
    date_to optional. On-demand runs (TriggerReportRunView) work fully
    without it; still tracked as future work in BUILD_STATUS.md.
    """

    class Meta:
        model = ReportDefinition
        fields = ["farm", "name", "report_type", "export_format", "recurrence", "subscribers"]
        widgets = {"subscribers": forms.CheckboxSelectMultiple}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subscribers"].required = False
        # Only people eligible for a farm this user can pick — a Manager no
        # longer sees (or can subscribe) other farms' Managers. clean() then
        # checks the choice against the specific farm selected.
        # Only types that actually generate a file — the form used to offer
        # all five and explain the difference with a note pointing at
        # BUILD_STATUS.md.
        self.fields["report_type"].choices = [
            (value, label) for value, label in self.fields["report_type"].choices
            if value in IMPLEMENTED_REPORT_TYPES
        ]
        manager_role = models.Q(farm_roles__role=UserFarmRole.ROLE_MANAGER)
        if user is not None and not user.is_superuser:
            self.fields["farm"].queryset = Farm.objects.filter(id__in=managed_farm_ids(user))
            manager_role &= models.Q(farm_roles__farm_id__in=managed_farm_ids(user))
        self.fields["subscribers"].queryset = (
            get_user_model().objects.filter(models.Q(is_superuser=True) | manager_role).distinct().order_by("username")
        )

    def clean(self):
        cleaned = super().clean()
        farm, subscribers = cleaned.get("farm"), cleaned.get("subscribers")
        if farm is not None and subscribers:
            allowed = set(eligible_subscribers(farm).values_list("id", flat=True))
            outside = [u.username for u in subscribers if u.id not in allowed]
            if outside:
                self.add_error(
                    "subscribers",
                    f"Not a Manager of {farm.name}: {', '.join(outside)}. Subscribers must manage this report's farm.",
                )
        return cleaned


class FarmBlockForm(_PlainEmptyLabelsMixin, forms.ModelForm):
    """Manager-facing block create/edit — see FarmBlockCreateView/
    FarmBlockUpdateView in apps/dashboard/views.py. `boundary` is
    deliberately excluded: it's a real PostGIS polygon, and this project's
    own "no text-string coordinates anywhere" rule (docs/01-architecture.md)
    means it has to be drawn on a map, not typed into this form — see the
    separate FarmBlockBoundaryEditView, reachable from the block list once
    a block exists. `farm` restricted to managed farms, same pattern as
    every other form in this file. `pic` restricted to users holding a PIC
    Block role on *some* farm the requesting Manager manages, not narrowed
    further to whichever farm happens to be selected — same "can't react
    to another field without added JS" limitation ReportDefinitionForm's
    own docstring already documents for `subscribers`.
    """

    class Meta:
        model = FarmBlock
        fields = [
            "farm",
            "name",
            "commodity",
            "area",
            "year",
            "pic",
            "tree_match_threshold_m",
            "harvest_interval_days",
            "fertilizer_interval_days",
            "crown_detection_iou_threshold",
            "detection_target_gsd_m",
        ]

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in (
            "commodity",
            "area",
            "year",
            "pic",
            "tree_match_threshold_m",
            "harvest_interval_days",
            "fertilizer_interval_days",
            "crown_detection_iou_threshold",
            "detection_target_gsd_m",
        ):
            self.fields[field_name].required = False
        pic_filter = {"farm_roles__role": UserFarmRole.ROLE_PIC_BLOCK}
        if user is not None and not user.is_superuser:
            managed_ids = list(managed_farm_ids(user))
            self.fields["farm"].queryset = Farm.objects.filter(id__in=managed_ids)
            pic_filter["farm_roles__farm_id__in"] = managed_ids
        self.fields["pic"].queryset = get_user_model().objects.filter(**pic_filter).distinct()


class TreeForm(forms.ModelForm):
    """Manager-facing tree create/edit — see TreeCreateView/TreeUpdateView.
    `location` is excluded for the same reason FarmBlockForm excludes
    `boundary`: a real PostGIS point, placed by clicking a map in the view
    rather than typed here. It's non-nullable on the model (unlike
    `boundary`, which can genuinely be absent), so the view sets it on the
    instance before saving and the create view refuses to save without a
    point actually having been clicked — see TreeCreateView's docstring.
    `farm_block` is always rendered as a hidden field, not a visible
    dropdown — TreeCreateView/TreeUpdateView are both entered from a
    specific block's own trees, selected in the Farm Map side panel (see
    FarmMapView), so the block is already fixed by the URL the Manager
    navigated to; offering a free
    dropdown here would be redundant at best and let a mis-click silently
    move a tree to a wrong block at worst. Still restricted to blocks on
    farms the user manages, same server-side check every other form in
    this file makes — a hidden field is not a substitute for that.

    `last_harvested_at`/`last_fertilized_at` are included here too, but
    editing a single tree's date by hand through this form is the
    exception, not the normal path — the normal path is the Farm Map's
    bulk "Mark trees harvested"/"Mark trees fertilized" actions (select
    several trees at once, by click or lasso, then confirm — see
    MarkTreesHarvestedView/MarkTreesFertilizedView below). These fields
    are kept editable here only so a mis-marked date can be corrected
    without re-running the bulk flow.
    """

    class Meta:
        model = Tree
        fields = [
            "farm_block",
            "name",
            "source",
            "status",
            "current_health",
            "last_harvested_at",
            "last_fertilized_at",
        ]
        widgets = {
            "farm_block": forms.HiddenInput(),
            "last_harvested_at": forms.DateInput(attrs={"type": "date"}),
            "last_fertilized_at": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False
        self.fields["last_harvested_at"].required = False
        self.fields["last_fertilized_at"].required = False
        if user is not None and not user.is_superuser:
            self.fields["farm_block"].queryset = FarmBlock.objects.filter(
                farm_id__in=list(managed_farm_ids(user))
            )


class HarvestPlanForm(forms.ModelForm):
    """Block-level harvest plan creation from the Farm Map panel — see
    HarvestPlanCreateView. `farm_block` is excluded entirely (not just
    hidden, unlike TreeForm's `farm_block`): the block is always fixed by
    the URL the Manager reached this form from and is set on the instance
    server-side in the view, so there's no form field for it to
    mismatch/tamper with at all.

    This is deliberately the "Phase 2 first" block-level-only version
    agreed with the project owner: selecting individual trees on the map
    before creating a plan is a client-side visual affordance only (see
    farm_map.html) — it is not persisted here and does not appear on this
    form. Real tree-level harvest scoping (a HarvestTree-style planning
    link, a dedicated approval screen, PWA plan selection) is explicitly
    deferred to a later round — see BUILD_STATUS.md "Farm Map unification".
    """

    class Meta:
        model = Harvest
        fields = ["assessment_date", "assessment_quantity"]
        widgets = {"assessment_date": forms.DateInput(attrs={"type": "date"})}


class FertilizerPlanForm(forms.ModelForm):
    """Block-level fertilizer-application plan creation from the Farm Map
    panel — the fertilizer counterpart to HarvestPlanForm above, added
    after direct feedback asking where the fertilizer process was on the
    map page (it simply hadn't been built yet — only Harvest had). Same
    "Phase 2 first," block-level-only reasoning applies here too: the
    `Fertilizer` model (apps/operations/models.py) has no tree-level
    scoping field either, so this stays block-level, no tree-selection
    step at all (Fertilizer, unlike Harvest, was never asked to have one —
    the project owner's original direction only mentioned "choosing trees
    for harvest/field logging").

    `farm_block` is excluded entirely, same reasoning as HarvestPlanForm:
    the block is always fixed by the URL and set on the instance
    server-side in the view.
    """

    class Meta:
        model = Fertilizer
        fields = ["assessment_date"]
        widgets = {"assessment_date": forms.DateInput(attrs={"type": "date"})}


class GrievanceRecordForm(_PlainEmptyLabelsMixin, forms.ModelForm):
    """docs/07-compliance.md grievance logging. `farm` restricted to managed
    farms, same pattern as the forms above. `status`/`resolution_notes` are
    deliberately left off this creation form — a new grievance always starts
    "open" (the model default), and status progression happens through
    GrievanceUpdateStatusView instead (apps/dashboard/views.py), a small
    targeted action rather than a general edit form — the same shape this
    dashboard already uses for DeforestationBaseline.verify().
    """

    class Meta:
        model = GrievanceRecord
        fields = ["farm", "date_reported", "description"]
        widgets = {"date_reported": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and not user.is_superuser:
            self.fields["farm"].queryset = Farm.objects.filter(id__in=managed_farm_ids(user))
