"""
Approval status-transition logic, shared by Harvest and Fertilizer.

This exists specifically to fix two documented bugs in the original Laravel
app (Palm_Vision_PDR.md § 3.6–3.7): the `approve()` action's default/fallback
branch called `dd()` (a debug dump-and-die) on an unexpected status instead
of failing cleanly, and the `HarvestController::approve()`/
`ActualHarvestController::destroy()` authorization checks were commented
out, meaning any authenticated user could approve or delete regardless of
permission. Neither happens here: permission is enforced by the view (see
apps/operations/views.py, IsSuperAdminOrManager), and an unexpected status
raises a clean, catchable exception rather than crashing the process.
"""
from django.core.exceptions import ValidationError

from .models import (
    ActualFertilizer, ActualHarvest, Fertilizer, FertilizerTree, Harvest, HarvestTree, PlanStatus,
)

_TRANSITIONS = {
    PlanStatus.CREATED: PlanStatus.CREATED_CONFIRMED,
    PlanStatus.ON_PROGRESS_WAIT: PlanStatus.ON_PROGRESS,
    PlanStatus.DONE_WAIT: PlanStatus.DONE,
}


def can_advance(status):
    """Whether `status` (a PlanStatus value) has a defined next step —
    what the dashboard uses to decide whether to show an "Approve" button
    at all, without duplicating _TRANSITIONS' own keys. This is a pure
    status check — it does NOT know about has_actual_record() below, since
    that requires the plan instance, not just its status string. Callers
    that already have the plan instance in hand (e.g. apps/dashboard/
    views.py's _plan_entries()) should additionally check
    has_actual_record() before deciding a "done_wait" plan is really
    approvable — see that function's docstring.
    """
    return status in _TRANSITIONS


def has_actual_record(plan):
    """Whether at least one actual-execution row (ActualHarvest/
    ActualFertilizer) has been recorded against `plan`. Gates *every*
    approval step in advance_status() below, not just done_wait -> done —
    added after a real gap was found: a bare plan with zero recorded
    actuals could still be clicked through created -> created_confirmed,
    offering "Approve" for a plan a PIC Block had never actually input
    anything against yet. Reported directly: "there are no need approve
    for harvest plan[,] only approve harvest plan that already inputted
    by the pic block." Requiring a recorded actual before ANY approval
    step is what closes that gap — approval is conceptually "approve the
    field data a PIC Block entered," not "approve the bare plan," so
    there's nothing to approve until that data exists. This is also what
    makes the done_wait -> done step's own version of this guard
    reachable in the first place: a plan can only ever reach
    on_progress_wait/done_wait once an actual has been recorded (see
    apps/dashboard/views.py's `_advance_plan_progress()`), so gating
    every step this way doesn't block the normal flow at all — it only
    ever blocks the case that shouldn't have had an Approve button to
    begin with.

    Distinguishes Harvest from Fertilizer via which reverse relation is
    present (`actual_harvests` vs `actual_fertilizers`) rather than an
    isinstance check, so this keeps working if either model gains a
    subclass later.
    """
    if hasattr(plan, "actual_harvests"):
        return plan.actual_harvests.exists()
    if hasattr(plan, "actual_fertilizers"):
        return plan.actual_fertilizers.exists()
    return True  # not a Harvest/Fertilizer — don't block on a check that doesn't apply


def awaiting_approval(plans):
    """The plans in `plans` (a Harvest or Fertilizer queryset) a Manager can
    approve right now — exactly the ones that show an Approve button
    (can_advance() + has_actual_record()). The Overview's "Pending
    approvals" count and the daily approval-pending notification both use
    this, so neither can drift from the button: the Overview used to count
    `created` plans (which can't be approved) and miss the ones that could.
    """
    actuals = "actual_harvests" if hasattr(plans.model, "actual_harvests") else "actual_fertilizers"
    return plans.filter(status__in=list(_TRANSITIONS), **{f"{actuals}__isnull": False}).distinct()


def record_actual_progress(plan, finished):
    """Recording an actual is what moves a plan toward Done: it goes to
    done_wait ("finished") or on_progress_wait, ready for a Manager to
    approve. Nothing else sets those states, so without this a plan can
    never reach Done — which is exactly what happened to every harvest
    logged from the PWA until this was shared with the API path (the
    first Approve moved it to created_confirmed, a dead end, and its yield
    never counted). Mirrors the original app's own "finish" flag
    (Palm_Vision_PDR.md § 3.6). Never downgrades an already-Done plan.
    """
    if plan.status == PlanStatus.DONE:
        return
    plan.status = PlanStatus.DONE_WAIT if finished else PlanStatus.ON_PROGRESS_WAIT
    plan.save(update_fields=["status", "updated_at"])


def advance_status(plan):
    """Advance `plan` (a Harvest or Fertilizer instance) to its next status
    in the approval workflow. Raises ValidationError — not a crash — if
    `plan.status` isn't one of the states that has a defined next step, or
    if no actual harvest/fertilizer application has been recorded against
    it yet (see has_actual_record() above — this applies to every
    transition, not just done_wait -> done).
    """
    next_status = _TRANSITIONS.get(plan.status)
    if next_status is None:
        raise ValidationError(
            f"'{plan.status}' has no defined approval transition. "
            f"Valid current states for approval are: {', '.join(_TRANSITIONS)}."
        )
    if not has_actual_record(plan):
        raise ValidationError(
            "This plan can't be approved until at least one actual "
            "harvest or fertilizer application has been recorded against "
            "it — use “Record actual harvest” (or “Record actual "
            "fertilizer”) first, then approve."
        )
    plan.status = next_status
    plan.save(update_fields=["status", "updated_at"])
    return plan


def record_field_completion(block, kind, quantities, date, user):
    """A field user marking trees harvested/fertilized is reporting finished
    work, so it goes through the same approval pipeline as every other
    actual: one ActualHarvest/ActualFertilizer (with per-tree rows) on the
    block's open plan — created if there is none — and the plan moves to
    done_wait for a Manager to approve. Before this, the PWA's "Mark
    harvested" buttons only stamped the trees, leaving the plan at "created"
    with nothing to approve and no yield ever counted.

    `quantities` maps Tree -> kg or None. Returns the plan.
    """
    if kind == "harvest":
        plan_model, actual_model, tree_model, fk, tree_fk, field = (
            Harvest, ActualHarvest, HarvestTree, "harvest", "actual_harvest", "weight_kg",
        )
    else:
        plan_model, actual_model, tree_model, fk, tree_fk, field = (
            Fertilizer, ActualFertilizer, FertilizerTree, "fertilizer", "actual_fertilizer", "quantity",
        )
    plan = plan_model.objects.filter(farm_block=block).exclude(status=PlanStatus.DONE).order_by("-assessment_date").first()
    if plan is None:
        extra = {"assessment_quantity": sum(q for q in quantities.values() if q) or 0.0} if kind == "harvest" else {}
        plan = plan_model.objects.create(farm_block=block, assessment_date=date, **extra)
    actual = actual_model.objects.create(**{fk: plan}, date=date)
    tree_model.objects.bulk_create([
        tree_model(**{tree_fk: actual}, tree=tree, recorded_by=user, **{field: qty})
        for tree, qty in quantities.items()
    ])
    record_actual_progress(plan, finished=True)
    return plan
