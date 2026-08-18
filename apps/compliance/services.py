"""
Compliance business logic kept out of apps/compliance/views.py so it's
reusable outside the DRF request/response cycle — specifically,
apps/dashboard/views.py's due-diligence export screen needs the exact same
package-assembly logic HarvestBatchViewSet.export_due_diligence() uses, not
a second, hand-copied version that could quietly drift from it. Extracted
here (moved, not duplicated) rather than left inline in the viewset action.
"""
from django.utils import timezone

from apps.common.geo import geometry_to_6dp_geojson

from .models import DeforestationBaseline

# Re-exported for backwards compatibility — this module's own tests and any
# other existing importer reference `services.geometry_to_6dp_geojson`
# directly; the implementation now lives in apps/common/geo.py (shared with
# apps/dashboard/views.py's map panel) rather than being duplicated here.
__all__ = ["DueDiligenceExportRefused", "geometry_to_6dp_geojson", "assemble_due_diligence_package"]


class DueDiligenceExportRefused(Exception):
    """Raised when a batch's block has no boundary polygon — mirrors
    apps/imagery/services.py's ProcessingRefused pattern: a plain,
    named exception this module can raise without depending on
    rest_framework, letting each caller (the API view, the dashboard view)
    turn it into whatever error response makes sense for that surface.
    """


def assemble_due_diligence_package(batch):
    """docs/07-compliance.md § EUDR-specific functions: assembles the
    due-diligence package for a HarvestBatch — the block's boundary as a
    GeoJSON Feature (6dp precision), a requires_geojson_polygon_per_eudr_4ha_rule
    flag (the actual EUDR trigger for needing plot-level polygon data —
    block area >= 4 hectares, not a vague "precision" check), the linked
    DeforestationBaseline's status (or null if none exists — never
    fabricated), the batch's recomputed totals, and explicit null
    placeholders for risk_assessment/supplier/hs_code, which this system
    doesn't collect data for yet. Marks the batch exported as a side
    effect, same as before this was extracted from the viewset action.

    Raises DueDiligenceExportRefused if the block has no boundary polygon
    — same precondition apps/imagery/services.py's run_processing() enforces.
    """
    block = batch.farm_block
    batch.recompute_totals()

    if not block.boundary:
        raise DueDiligenceExportRefused(
            "This block has no boundary polygon — a due-diligence export requires one "
            "(EUDR filing needs the plot geometry)."
        )

    boundary_geojson = geometry_to_6dp_geojson(block.boundary)

    try:
        baseline = block.deforestation_baseline
        baseline_payload = {
            "status": baseline.status,
            "cutoff_date": baseline.cutoff_date,
            "source": baseline.source,
            "verified_at": baseline.verified_at,
        }
    except DeforestationBaseline.DoesNotExist:
        baseline_payload = None

    requires_geojson_polygon = bool(block.area and block.area >= 4)

    package = {
        "harvest_batch_id": str(batch.id),
        "farm_block": {
            "id": str(block.id),
            "name": block.name,
            "area_hectares": block.area,
        },
        "period_start": batch.period_start,
        "period_end": batch.period_end,
        "total_quantity": batch.total_quantity,
        "total_weight_kg": batch.total_weight_kg,
        "plot_geometry": {
            "type": "Feature",
            "geometry": boundary_geojson,
            "properties": {"requires_geojson_polygon_per_eudr_4ha_rule": requires_geojson_polygon},
        },
        "deforestation_baseline": baseline_payload,
        # Placeholder fields — docs/07-compliance.md notes these need
        # supplier-chain data this system doesn't collect yet; left
        # explicit and null rather than fabricated, so a compliance
        # officer completing the filing knows exactly what's missing.
        "risk_assessment": None,
        "supplier": None,
        "hs_code": None,
        "generated_at": timezone.now(),
    }

    batch.export_status = batch.EXPORT_STATUS_EXPORTED
    batch.save(update_fields=["export_status", "updated_at"])
    return package
