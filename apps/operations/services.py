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

from .models import PlanStatus

_TRANSITIONS = {
    PlanStatus.CREATED: PlanStatus.CREATED_CONFIRMED,
    PlanStatus.ON_PROGRESS_WAIT: PlanStatus.ON_PROGRESS,
    PlanStatus.DONE_WAIT: PlanStatus.DONE,
}


def advance_status(plan):
    """Advance `plan` (a Harvest or Fertilizer instance) to its next status
    in the approval workflow. Raises ValidationError — not a crash — if
    `plan.status` isn't one of the states that has a defined next step.
    """
    next_status = _TRANSITIONS.get(plan.status)
    if next_status is None:
        raise ValidationError(
            f"'{plan.status}' has no defined approval transition. "
            f"Valid current states for approval are: {', '.join(_TRANSITIONS)}."
        )
    plan.status = next_status
    plan.save(update_fields=["status", "updated_at"])
    return plan
