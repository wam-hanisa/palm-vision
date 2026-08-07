# Security, performance & operations

## Baseline

- Every state-changing DRF endpoint has an explicit permission class — no repeat of the original app's silently-bypassable approvals.
- Current, supported versions from day one — Django 5.2 LTS, current Python — avoiding the exact EOL trap the original app is in.
- Real spatial indexing — GeoDjango's geometry fields get PostGIS spatial indexes automatically.
- Test coverage and CI from the start (see `02-build-plan.md` Phase 8) — the original app has zero meaningful tests.
- Celery-based thumbnailing/tiling on imagery upload, so large files don't bloat page loads.

## Audit logging

Two risk categories both need real history, not just a "who's currently assigned" field: **safety-critical** (harvest/fertilizer approvals) and **compliance-critical** (land boundaries, certification documents, audit results).

- `django-simple-history` (or `django-auditlog`) attached to: `Tree`, `FarmBlock`, `Harvest`/`Fertilizer` status transitions, `ComplianceDocument`, `ComplianceAudit`, `DeforestationBaseline`, and role/permission assignments. Every save gets a versioned snapshot — who changed it, when, before/after values.
- A "current user" middleware so history entries capture who made the change (Django's ORM doesn't know about the request/user outside a view by default).
- A read-only history view per record in the admin/manager UI.
- Append-only, never purged by the retention policy below.

## Backup & disaster recovery

- **Database**: automated daily full backups plus continuous WAL archiving for point-in-time recovery. Retention: daily backups for 30 days, weekly for a year, as a starting default — tune to actual risk tolerance.
- **Compliance documents need a stricter policy** than routine backups: object storage versioning enabled, ideally an object-lock/write-once setting on compliance evidence specifically, honoring the 5-year EUDR retention requirement (`07-compliance.md`). Routine imagery doesn't need this; compliance evidence does.
- **Backup restore drills, on a schedule** — an untested backup is a belief, not a plan. Quarterly restore-to-staging exercises catch silent backup failures before they matter. Phase 8 in `02-build-plan.md` requires running this once locally against the `db` compose service, at minimum.
- **RTO/RPO targets**: RPO of a few hours (via WAL archiving), RTO of same-day restore, as reasonable defaults — genuinely a business call to confirm, not something to finalize unilaterally.

## Alerting & notifications

Everything else in this system is pull-based by default — someone has to open the dashboard to notice a problem. A `Notification` model with pluggable delivery channels (in-app, email; SMS optional and not committed to) closes this, triggered by:

- Compliance document/certification expiring within a configurable window (90/60/30 days)
- Tree health anomalies — a meaningful week-over-week increase in yellow/unhealthy classifications for a block
- Stalled offline sync — a device with unsynced queued items for more than N days
- Detection review queue backlog
- Processing run failures
- Approvals pending too long

Delivered via Celery Beat periodic checks (most are daily-digest material) plus event-driven triggers for anything that shouldn't wait (a failed processing run). Each user chooses which categories/channels they receive — sending everyone everything is how alerts stop getting read.

## Image storage lifecycle & cost policy

- **Tiered storage**: recently uploaded/actively-processed imagery on hot storage; after processing completes and a retention window passes (90 days as a starting point), the raw file moves to cold storage while derived data (tiles, thumbnails, all `TreeDetection`/`TreeHealthClassification` rows) stays hot indefinitely.
- **Exception**: any image referenced by a `DeforestationBaseline` or other compliance record is exempt from this lifecycle and follows the stricter 5-year-minimum retention policy above instead.
- **Periodic cost review** — not a "set once" policy; revisit as flight volume grows.

## Multi-farm role scoping

`UserFarmRole` (user, farm, role) scopes Manager/PIC Block assignments to specific farms; Super Admin remains global by definition. Every DRF permission class checks "does this user hold a role granting this permission on farm X," not just "does this user hold role Y" globally. Build this in Phase 1 even for a single-farm deployment — retrofitting farm-scoped permissions after roles and habits are established is a much bigger migration than building the join table from the start.

## Geometry & data integrity validation

- **Boundary validity** — `FarmBlock.boundary` checked for geometric validity (no self-intersection) via GEOS `is_valid` at save time; rejected with a clear error otherwise.
- **Orphaned trees** — a `Tree.location` falling outside every block boundary on the farm is flagged for review, not silently accepted or hard-rejected (GPS error in the field is real and shouldn't block data entry outright).
- **Block overlap warnings** — two `FarmBlock` polygons on the same farm overlapping by more than a small tolerance triggers a warning at save time (not a hard block — adjacent blocks can legitimately share an edge).
