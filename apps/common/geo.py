"""
Generic GEOS-geometry formatting helpers shared across apps.

`geometry_to_6dp_geojson` originally lived only in `apps/compliance/services.py`
(written for the due-diligence export's GeoJSON output). The office
dashboard's map panel (`apps/dashboard/views.py::MapView`) needs the exact
same coordinate-rounding formatting for block boundaries, tree points, and
image footprints — moved here (not duplicated) so both callers share one
function, same reasoning as `apps/common/scoping.py`'s extraction.
"""
import json as _json

from django.contrib.gis.geos import GEOSGeometry


def geometry_to_6dp_geojson(geometry: GEOSGeometry):
    """Formats a GEOS geometry's coordinates to 6 decimal places (~11cm
    precision at the equator) — docs/07-compliance.md's "6-decimal
    coordinate precision" requirement, reused here for any GeoJSON this
    project sends to a client (compliance exports, the dashboard map).
    This controls output formatting; it does not (and cannot reliably)
    validate that the original survey capture had that much real-world
    precision — a stored float can always be *formatted* to 6dp regardless
    of its actual source accuracy, so this is a formatting guarantee, not
    a data-quality guarantee.
    """
    raw = _json.loads(geometry.geojson)

    def _round_coords(coords):
        if isinstance(coords[0], (list, tuple)):
            return [_round_coords(c) for c in coords]
        return [round(c, 6) for c in coords]

    raw["coordinates"] = _round_coords(raw["coordinates"])
    return raw
