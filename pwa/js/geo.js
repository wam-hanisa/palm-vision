/**
 * Farm → Block → Tree geo-navigation and proximity-based yield-entry
 * prompt — docs/05-offline-sync.md § Farm → Block → Tree geo-navigation
 * and § Proximity prompt for yield entry.
 *
 * Hard rule from that doc, repeated here because it's easy to violate by
 * accident while wiring up UI later: NEVER auto-submit against a guessed
 * tree. This module only ever *suggests* candidates; only app.js's
 * explicit user-confirmation handler may create a HarvestTree/FertilizerTree
 * record.
 */

const EARTH_RADIUS_M = 6371000;
const PROXIMITY_THRESHOLD_M = 15; // wider than raw GPS accuracy on purpose — see module docstring

function haversineDistanceMeters(lat1, lon1, lat2, lon2) {
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** GeoJSON Point -> [lat, lon]. Every geometry this app receives from the
 * API is GeoJSON (rest_framework_gis) — [lon, lat] order, not [lat, lon].
 * Getting this backwards is a classic, silent bug, so it's isolated here.
 */
function pointToLatLon(geoJsonPoint) {
  const [lon, lat] = geoJsonPoint.coordinates;
  return { lat, lon };
}

/** Ray-casting point-in-polygon test against a single GeoJSON Polygon's
 * outer ring — good enough for "is the user's GPS dot inside this block,"
 * which only needs to be approximately right (it's a UI highlight, not a
 * server-side authority; the server's PostGIS `contains` check is the
 * real source of truth for anything that matters, e.g. tree orphan
 * detection in apps/trees/models.py).
 */
function pointInPolygon(lat, lon, geoJsonPolygon) {
  const ring = geoJsonPolygon.coordinates[0];
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    const intersects = yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

function findContainingBlock(lat, lon, blocks) {
  return blocks.find((block) => block.boundary && pointInPolygon(lat, lon, block.boundary)) || null;
}

/** Nearest 2–3 trees to a position — never returns a single "the" answer;
 * callers must present these as candidates requiring confirmation, per
 * this module's docstring.
 */
function nearestTrees(lat, lon, trees, limit = 3) {
  return trees
    .map((tree) => {
      const { lat: tLat, lon: tLon } = pointToLatLon(tree.location);
      return { tree, distanceM: haversineDistanceMeters(lat, lon, tLat, tLon) };
    })
    .sort((a, b) => a.distanceM - b.distanceM)
    .slice(0, limit);
}

/** Returns the nearest tree only if it's within PROXIMITY_THRESHOLD_M —
 * used to decide whether to surface the "Near Tree #X — log yield?"
 * prompt at all. Still just a suggestion; app.js must still get explicit
 * confirmation before writing anything.
 */
function proximityCandidate(lat, lon, trees) {
  const [nearest] = nearestTrees(lat, lon, trees, 1);
  if (nearest && nearest.distanceM <= PROXIMITY_THRESHOLD_M) return nearest;
  return null;
}

/** Thin wrapper around watchPosition that the caller is responsible for
 * clearing (clearWatch) when navigating away from a geo-aware screen —
 * see docs/05-offline-sync.md: "should only run while the relevant screen
 * is open, to avoid draining battery."
 */
function watchLocation(onUpdate, onError) {
  if (!("geolocation" in navigator)) {
    onError(new Error("This device does not support geolocation."));
    return null;
  }
  return navigator.geolocation.watchPosition(
    (position) => onUpdate({ lat: position.coords.latitude, lon: position.coords.longitude, accuracyM: position.coords.accuracy }),
    (err) => onError(err),
    { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 }
  );
}

function stopWatchingLocation(watchId) {
  if (watchId != null) navigator.geolocation.clearWatch(watchId);
}

window.PalmVisionGeo = {
  PROXIMITY_THRESHOLD_M,
  haversineDistanceMeters,
  pointToLatLon,
  pointInPolygon,
  findContainingBlock,
  nearestTrees,
  proximityCandidate,
  watchLocation,
  stopWatchingLocation,
};
