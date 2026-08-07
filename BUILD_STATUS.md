# Build Status

This tracks what's actually built and verified in `palm-vision-app/`, versus
what's still open, as of 2026-08-07. Read this before continuing the build —
it tells you exactly where to pick up. `CLAUDE.md` in this same folder has
the non-negotiable design decisions; `docs/02-build-plan.md` has the full
9-phase plan this status maps onto.

**Update:** Phases 0–3 have since been run for real against live Postgres/
PostGIS via `docker compose` (not just sandbox structural checks — see
below for what that distinction meant originally). That run caught two real
bugs, both fixed and covered by regression tests: (1) DRF auto-attaches a
uniqueness validator to a writable primary-key field, which broke the
idempotent-resubmit path on every serializer using it; (2)
`IdempotentUUIDCreateMixin` assumed every viewset sets a class-level
`queryset` attribute, which isn't true for viewsets that only define
`get_queryset()`. A third issue was found by inspection (not by a failing
test) and also fixed: the same mixin performed its update-or-create against
the unscoped model manager, so a user who knew another user's record UUID
could overwrite a record outside their farm/block scope. All 31 tests pass
as of this update.

**Phase 5 update:** while building imagery tests, found that `FarmBlock.boundary`
was a *required* field — meaning docs/06-ml-pipeline.md's stated precondition
("processing refuses to start if a block has no boundary") could never
actually be violated, since you couldn't create a block without one in the
first place. Fixed: `boundary` is now nullable (migration
`apps/farms/migrations/0003_alter_farmblock_boundary_and_more.py`), with
`FarmBlock.overlapping_blocks()` and `Tree.is_location_orphaned()` updated to
skip blocks with no boundary rather than crashing on `None`. Run
`docker compose exec web python manage.py migrate` to apply it.

## Done and verified

**Phase 0 — scaffold.** Django 5.2 project, `settings/base.py` +
`settings/local.py` + `settings/test.py`, Docker Compose (db/redis/web/worker/beat),
Celery wiring, `.env.example`.

**Phase 1 — users, farms, trees apps.** Full models (`Farm`, `FarmBlock`,
`UserFarmRole`, `Tree`), serializers, viewsets, permissions, URLs, admin,
audit history via `django-simple-history`, `seed_demo_data` management
command.

**Phase 2 — operations app (Harvest/Fertilizer).** Full models (`Harvest`,
`ActualHarvest`, `HarvestTree`, `Fertilizer`, `ActualFertilizer`,
`FertilizerTree`), the `advance_status()` approval-transition service,
serializers, viewsets (including the `approve` action gated on
`IsSuperAdminOrManager`), URLs, admin.

**Phase 3 — DRF API + JWT auth.** `djangorestframework-simplejwt` token
endpoints wired in `palmvision/urls.py`; every viewset scoped by role
(`UserFarmRole`) or block assignment (`FarmBlock.pic`); the idempotent
client-UUID create path (`IdempotentUUIDCreateMixin`) that makes offline
sync-replay safe.

**Automated test suite.** `conftest.py` + `apps/{users,farms,trees,operations}/tests.py` —
32 tests covering: JWT obtain/refresh/reject, farm/block role scoping
(manager, PIC Block, unaffiliated user, super admin), boundary geometry
validation (rejects self-intersecting polygons) and overlap detection, tree
CRUD scoping, the idempotent-UUID resubmit-doesn't-duplicate behavior (Tree
and HarvestTree), the out-of-scope resubmit-is-forbidden check, soft
delete, location-orphan detection, harvest effective-quantity derivation
(tree-level sum overrides manual total), and the approve-action permission
enforcement (Manager can, PIC Block can't, unrelated user can't, terminal
status returns a clean 400 not a crash). **These 32 are confirmed passing
against live Postgres/PostGIS** via `docker compose exec web pytest -v`.

Since then, 18 more tests were added but only checked structurally in this
sandbox (`manage.py check`, `py_compile`, and — for the PWA shell routes —
Django's in-process test client), **not yet run against live Postgres**:
`apps/sync/tests.py` (8 tests: scoped/incremental pull behavior, the
pre-cache action) and `palmvision/pwa_tests.py` (10 tests: every PWA shell
URL serves the right file). Run `docker compose exec web pytest -v` again
to confirm these 18 too — expect them to pass (the pattern matches the
already-confirmed 32), but say so, don't assume.

### What "verified" means here, precisely

This was built in a sandbox with no Docker, no sudo, and no Postgres/PostGIS
server available — so the test suite above has **not been run against a
real database**. What I did verify in-sandbox, using `shapely`'s and
`pyogrio`'s bundled GEOS/GDAL libraries as a substitute for system packages:

- `python manage.py check` passes clean (structural correctness: every
  model, serializer, viewset, URL, and settings reference resolves).
- `python manage.py makemigrations` generates a consistent migration set
  with no errors, for all four apps.
- `python manage.py makemigrations --check --dry-run` reports no pending
  changes — models and migrations are in sync.
- Every test file passes `py_compile` (syntax-valid).
- Manually confirmed in a Django shell that the GIS serializer fields
  (`Tree.location`, `FarmBlock.boundary`, `HarvestTree.recorded_location`)
  resolve to `rest_framework_gis.serializers.GeometryField` correctly on
  plain `ModelSerializer` subclasses — this was worth checking explicitly
  since it's a common source of silent breakage with GeoDjango + DRF.

**What I could not verify here:** that the test suite actually passes end
to end against live PostGIS (spatial queries, JWT round-trip through
real HTTP, DB constraints, `django-simple-history` record creation). Do
this first, before writing any more code:

```bash
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data
docker compose exec web pytest -v
```

If everything above is green, Phases 0–3 are solid ground to build on. If
something fails, it's most likely a small mismatch between a test's
assumptions and actual runtime behavior (e.g. an exact status code) rather
than a structural problem — the checks above already caught anything
structural.

**Phase 3 addition — `apps/sync/`.** The build plan's Phase 3 checklist
required `GET /api/sync/pull/?since={timestamp}` (`docs/04-api-spec.md`);
this had been missed when Phase 3 was first marked done. Added since:
`SyncPullView`, which reuses each resource's own `ViewSet.get_queryset()`
(rather than reimplementing scoping) to return every offline-relevant
record — farms, blocks, trees, harvests/fertilizers and their actuals —
scoped to the requesting user, incrementally filterable by `?since=`. Also
added the `GET /api/farm-blocks/{id}/trees/` pre-cache action the API spec
lists but that hadn't been built. 8 new tests in `apps/sync/tests.py`,
passing against live Postgres.

**Phase 4 — PWA field client, first pass.** Built and structurally verified
(syntax-checked with `node --check`, and every route confirmed to serve the
right file with the right content-type via Django's test client): service
worker (`pwa/service-worker.js`) that caches the app shell and explicitly
never intercepts `/api/`; an IndexedDB layer (`pwa/js/db.js`) with one
object store per offline-relevant resource plus an `outbox` for queued
writes; an API wrapper (`pwa/js/api.js`) that queues a write to the outbox
on network failure and sends it immediately otherwise; a sync engine
(`pwa/js/sync.js`) that flushes the outbox then pulls changes, triggered on
login, reconnect, tab foreground, a 60s interval, and (where supported) the
Background Sync API; geo-navigation and a proximity-based yield-entry
suggestion (`pwa/js/geo.js` — deliberately never auto-selects a tree, only
suggests, per `docs/05-offline-sync.md`'s hard rule); and a small
hash-router UI (`pwa/js/app.js`) covering login, block/tree navigation,
tree creation, and harvest/fertilizer actual logging at both block and
tree level. Served locally at `/field/` via `django.views.static.serve`
(explicitly the local-dev path, not a production static pipeline).

**What I could NOT verify for Phase 4, and why:** everything above was
checked for JavaScript syntax validity and correct server-side wiring, but
none of it has been exercised in an actual browser — no service worker
registration, no IndexedDB read/write, no simulated offline network
throttling, no real GPS. This sandbox has no browser to test in. The build
plan's Phase 4 verification gate ("with the browser's network throttling
set to 'offline,' you can navigate farm → block → tree, log a harvest
entry, and add a new tree — all of it visibly queued. Re-enabling the
network triggers automatic sync without user action, and the queue
clears.") is **not yet confirmed** — this needs a real run in Chrome/Edge
DevTools (Application tab → Service Workers / IndexedDB, Network tab →
offline throttling) against `http://localhost:8000/field/`, logged in as
the seeded `pic_block` user. Please try it and report back what breaks —
I'd expect the first real bugs to show up in `pwa/js/app.js`'s DOM
rendering/event-wiring code, since that's the part with the least
mechanical way to check correctness ahead of time (unlike the Python side,
where `manage.py check` and migrations catch a lot).

Not yet built for Phase 4: a Leaflet map view (blocks/trees currently
render as a plain list, not a map — `docs/05-offline-sync.md`'s
farm/block-level map is still text-only for now), PNG icons are
placeholder art (not a real logo), and there's no UI for editing an
existing tree (only create) or for confirming a synced-vs-local record
conflict view (not expected to occur given append-only sync semantics, but
there's currently no UI path if it somehow did).

**Phase 5 — imagery/ML pipeline, first pass.** `apps/imagery/`: full models
(`Image`, `ImageProcessingRun`, `TreeDetection`, `TreeHealthClassification`,
`ModelVersion`), georeferencing (`services.georeference_image()` — reads a
raster's embedded CRS/bounds via `rasterio`, reprojects to EPSG:4326,
computes `Image.footprint`), clipping (`services.clip_to_block()` —
`rasterio.mask.mask()` against the block boundary reprojected into the
raster's native CRS first), the detect → match → classify chain
(`services.run_processing()`), review-queue actions (`confirm`/`reject`/
`link`), and `ModelVersion` staging/production/retired governance with a
`promote_to_production()` helper. Detection and classification models are
stubbed by design (`apps/imagery/ml/detection.py`, `classification.py`) —
real model loading (YOLOv8 via `ultralytics`, ResNet-18 via
`torch`/`torchvision`) is written but lazily imported, only reached when
`DETECTION_MODEL_PATH`/`CLASSIFICATION_MODEL_PATH` point at real trained
weights, which don't exist yet (docs/06-ml-pipeline.md is explicit this is
expected — "the pipeline's correctness ... needs to be verifiable
independent of having a production-quality model").

**What's actually verified here, and how, since real drone imagery and
Postgres/PostGIS still aren't available in this sandbox:** every raster
operation (`georeference_image`'s CRS reprojection including a genuine
EPSG:32648 UTM → EPSG:4326 case, `clip_to_block`'s masking, and the patch-
cropping used by classification) was run standalone against real,
`rasterio`-written synthetic GeoTIFFs outside Django entirely, confirming
the actual raster I/O and coordinate math work — not just that the code
parses. The matching logic (matched/new_candidate/ambiguous, including the
"nearby soft-deleted tree" edge case), the full `run_processing()` chain,
and every API endpoint are covered in `apps/imagery/tests.py` (24 tests)
using an *injected fake detector* returning deterministic points — this is
what makes the pipeline's routing/status-transition logic testable without
a trained model, per the build plan's own framing. None of these 24 have
run against live Postgres yet (needs real `ST_DWithin`/`ST_Contains`
spatial queries this sandbox can't execute) — run them next:

```bash
docker compose exec web python manage.py migrate
docker compose exec web pytest apps/imagery -v
```

**Also fixed as part of this phase:** `FarmBlock.boundary` nullability (see
the note near the top of this file) — a real Phase 1 gap that Phase 5's own
precondition check exposed.

**Update — confirmed against live Postgres/PostGIS, then fixed:** running
`apps/imagery/tests.py` for real surfaced three more issues the sandbox
structural checks couldn't have caught:

1. `match_detection()`'s use of the `dwithin` lookup (copied straight from
   docs/06-ml-pipeline.md's own code sample) raised `ValueError: Only
   numeric values of degree units are allowed on geographic DWithin
   queries` — PostGIS refuses a `Distance` object against a geodetic
   (SRID 4326) column unless it's declared `geography=True`, which
   `Tree.location` isn't. Fixed by switching to
   `.annotate(distance=Distance(...)).filter(distance__lte=D(m=...))`,
   which the function already used for ordering anyway.
2. Celery tasks weren't running eagerly in tests — traced to
   `DJANGO_SETTINGS_MODULE` being set as a container **environment
   variable** (from `.env`, needed for `runserver`), which pytest-django
   gives priority over `pytest.ini`'s setting of the same name. Fixed with
   `pytest-env`, forcing the override — see `pytest.ini`'s comment for the
   full explanation, including a mistake I made and self-corrected
   (initially used the `D:` prefix believing it meant "force," when it
   actually means the opposite — verified by reading the installed
   `pytest-env` source directly, then confirmed the corrected fix
   empirically in an isolated test run).
3. A UUID-vs-string comparison bug in `test_confirm_reject_link_api_actions`
   (DRF's `Response.data` in test-client responses holds a raw `uuid.UUID`
   object, not a string).

All three fixed and synced. **Confirmed: all tests passing** as of the
user's last live `docker compose exec web pytest -v` run.

Not yet built for Phase 5: tiling/preview generation for the map view
(docs/06-ml-pipeline.md step 4 — "rio-tiler/titiler, or a simpler
downsampled preview"), chunked/resumable upload (imagery uploads currently
go through a single synchronous multipart POST, fine for the small test
rasters used here but not for real hundreds-of-MB/GB orthomosaics), and the
office-dashboard UI for any of this (upload form, review-queue screen,
model version management) — everything in this phase so far is API +
pipeline only, no frontend.

**Phase 6 — compliance module, first pass.** `apps/compliance/`: full models
per `docs/07-compliance.md` — `ComplianceDocument` (attached to exactly one
of `farm`/`farm_block`, enforced in `clean()`/`save()`; `status` is a
computed property — valid/expiring_soon/expired — driven by `expiry_date`
and `settings.COMPLIANCE_EXPIRY_WARNING_DAYS`, default 90 days),
`DeforestationBaseline` (one per block, `verify` action sets
`verified_by`/`verified_at` server-side, never from client input),
`ComplianceAuditTemplate` + `ChecklistItem` (reusable checklist per
certification scheme), `ComplianceAudit` + `AuditResult` (per-item
pass/fail/n-a outcomes), `HarvestBatch` (aggregates `ActualHarvest` records
via a new nullable `batch` FK added to `ActualHarvest` — see
`apps/operations/migrations/0003_actualharvest_batch.py`), and
`GrievanceRecord`. `django-simple-history` wired to `ComplianceDocument`,
`ComplianceAudit`, and `DeforestationBaseline` specifically, per
`docs/12-security-ops.md`'s explicit list — the other five models aren't on
that list and were left without history rather than added speculatively.

Read access follows the same farm/block scoping used everywhere else in
this project; writes require Manager or Super Admin, checked server-side
(`_require_manager_or_superadmin_for_farm()` in `apps/compliance/views.py`)
— compliance filing is treated as an office function, not something PIC
Block field users can create, mirroring the approval-permission pattern from
Phase 2.

**`HarvestBatch.export_due_diligence` action** (`POST
/api/harvest-batches/{id}/export-due-diligence/`) assembles the due-diligence
package docs/07-compliance.md's EUDR section describes: the block's boundary
as a GeoJSON `Feature` with coordinates rounded to 6 decimal places, a
`requires_geojson_polygon_per_eudr_4ha_rule` flag (block area ≥ 4 hectares —
the actual EUDR trigger for needing plot-level polygon data at all, not a
vague "precision" check), the linked `DeforestationBaseline`'s status (or
`null` if none exists yet — never fabricated), the batch's recomputed
totals, and explicit `null` placeholders for `risk_assessment`/`supplier`/
`hs_code`, which this system doesn't collect data for yet — left visibly
`null` rather than guessed, so whoever completes the filing knows exactly
what's missing. Refuses (400) if the block has no boundary polygon, same
precondition Phase 5's imagery pipeline enforces.

**Two departures from `docs/07-compliance.md`'s literal field list, both
flagged in `apps/compliance/models.py`'s module docstring:**

1. `ComplianceDocument.status` and the expiry-status logic are computed
   properties, not stored fields — required for the build plan's own
   "computed from expiry_date" framing to stay true on every read, not just
   at write time.
2. `DeforestationBaseline.evidence_image` (nullable FK to `imagery.Image`) is
   an addition beyond the ERD. `docs/12-security-ops.md`'s storage-lifecycle
   section refers to "any image referenced by a DeforestationBaseline" as
   exempt from the standard imagery retention policy, but `07-compliance.md`'s
   own field list has no image FK to hang that off of. This is my inference
   to reconcile the two docs, not something either states outright — added
   `Image.is_compliance_exempt()` as the concrete hook a future storage-
   lifecycle job (not built — out of Phase 6's scope) would need to check.

**What's actually verified here, and how:** `manage.py check` passes clean;
`manage.py makemigrations compliance operations` generated a consistent,
single migration set with no errors (`apps/compliance/migrations/0001_initial.py`,
`apps/operations/migrations/0003_actualharvest_batch.py`);
`makemigrations --check --dry-run` confirms no drift. Two pieces of pure
logic were run standalone outside Django's DB layer, since GEOS geometry
math and a plain Python property don't need a live database: the 6-decimal-
place GeoJSON rounding helper (`_geometry_to_6dp_geojson`) against a real
GEOS polygon, and `ComplianceDocument.status`'s four branches (no expiry,
far-future, within-90-days, past) — both confirmed correct. 17 tests written
in `apps/compliance/tests.py` covering the build plan's stated Phase 6
verification gate (document expiry status, due-diligence export including
block polygon + baseline status) plus permission scoping for every model —
**none of these 17 have run against live Postgres/PostGIS yet** (spatial
queries and real multi-table joins this sandbox can't execute). Run next:

```bash
docker compose exec web python manage.py migrate
docker compose exec web pytest apps/compliance -v
```

Not yet built for Phase 6: any office-dashboard UI (this phase, like Phase
5, is API-only so far), the actual storage-lifecycle job that would consult
`Image.is_compliance_exempt()` (the hook exists; the job that calls it
doesn't), and the 60/30-day escalating-reminder notifications
`docs/12-security-ops.md` mentions alongside the 90-day warning threshold
(explicitly deferred to Phase 7 — notifications).

**Update — confirmed against live Postgres/PostGIS: 4 of 17 failed, both
test bugs, not production code bugs (`apps/compliance/models.py` and
`views.py` needed no changes):**

1. Three `ComplianceDocument.status` tests (`TypeError: '<' not supported
   between instances of 'str' and 'datetime.date'`) — the test helper
   `_today_plus()` returns an ISO date *string*, fine for API calls (DRF's
   `DateField` parses form/JSON strings into real `date` objects during
   `is_valid()`), but three tests passed that string straight into
   `ComplianceDocument.objects.create()` and read `.status` off the same
   in-memory instance with no save+refetch and no `full_clean()` — so the
   raw string never got coerced to a `datetime.date`, and `status`'s
   `self.expiry_date < today` comparison broke. This is ordinary Django
   model-field behavior (assignment doesn't validate/coerce types; only
   fetching from DB or running a form/serializer does), not a defect in the
   `status` property — every real construction path in this app (the API
   serializer, the admin) already hands the model a proper `date`. Fixed by
   adding `_today_plus_date()` (returns an actual `datetime.date`) for the
   three tests that construct `ComplianceDocument` directly, and reserving
   the string-returning `_today_plus()` for the actual API test.
2. `test_pic_block_can_read_but_not_create_baseline` expected 403, got 400.
   The test created a `DeforestationBaseline` for `block_a`, then tried to
   POST a second one for the *same* `block_a` as the PIC Block user —
   `DeforestationBaseline.farm_block` is a `OneToOneField` (by design: at
   most one baseline per block), so DRF's auto-uniqueness validation
   rejected the duplicate with 400 before the permission check in
   `perform_create()` ever ran, which is actually correct behavior for that
   input — the test just picked a bad target block for the negative case.
   Fixed by pointing the forbidden-create attempt at `block_b` (no existing
   baseline) instead, isolating the assertion to the permission check it was
   meant to test.

Both fixes were only in `apps/compliance/tests.py`. **Confirmed: 17/17
passing against live Postgres/PostGIS**, per the user's follow-up test run.

## Phase 7 — reporting & notifications, first pass

`apps/reporting/` and `apps/notifications/`, plus a small addition to
`apps/sync/` — see docs/08-reporting.md, docs/12-security-ops.md § Alerting
& notifications, and docs/02-build-plan.md's Phase 7 checklist.

**Reporting.** `ReportDefinition` (report type + filters + export format +
optional recurrence + subscriber list) and `ReportRun` (one generated
instance — status/file/error_message), the same async-job pattern as
`ImageProcessingRun`. `POST /api/reports/{id}/run/` creates a `ReportRun`
and hands it to a Celery task (`generate_report_task`), matching
`docs/04-api-spec.md`'s "Async — returns a ReportRun to poll/download once
ready." A scheduled Beat task (`run_scheduled_reports`, daily) picks up any
`ReportDefinition` whose `recurrence` is due (daily every day, weekly on
Mondays, monthly on the 1st) and emails the result to `subscribers`.

`docs/02-build-plan.md` explicitly scopes Phase 7's report *generation*
logic down to two of the five types docs/08-reporting.md describes:
production and tree_health. Both are fully implemented — PDF (WeasyPrint)
and Excel (openpyxl) — with real data assembly from `Harvest`/
`ActualHarvest` (production; reuses `get_effective_quantity()`, same as
Phase 2) and `TreeHealthClassification` (tree_health; uses
`effective_label`, so a human-corrected classification counts as itself,
not the model's original guess). The other three report types
(input/compliance/model_performance) are named, real choices on
`ReportDefinition.report_type` — a `ReportRun` requesting one fails
cleanly with a clear `error_message`, not silently, and not with a crash.

**What's genuinely verified here, not just structurally:** WeasyPrint and
openpyxl both installed and imported successfully in this sandbox — unlike
most of this project's non-Python dependencies, this wasn't just a
structural check. `_render_pdf`/`_render_xlsx` were run standalone against
synthetic report data for both report types; the resulting PDF bytes start
with a real `%PDF` header and the XLSX bytes round-tripped through
`openpyxl.load_workbook()` with the exact row content expected. **However**:
the sandbox's Python environment apparently already had WeasyPrint's native
Pango/cairo/gdk-pixbuf libraries present — the project's actual Docker image
(`python:3.12-slim`) did not, since nothing installed them before now.

**Update — the first `docker compose build` attempt against this failed for
real**, confirming the concern above was warranted: `apt-get install`
exited with code 100 trying to install `libgdk-pixbuf2.0-0`. Root cause,
confirmed via web search rather than guessed: `python:3.12-slim` currently
resolves to Debian **trixie** (13), not bookworm (12) — and trixie dropped
the `libgdk-pixbuf2.0-0` transitional package name that most install guides
(including my first attempt) still reference. The real package in trixie is
`libgdk-pixbuf-2.0-0` (hyphen before "2.0"). Fixed in the `Dockerfile`; the
other four packages (`libpango-1.0-0`, `libpangocairo-1.0-0`, `libcairo2`,
`fonts-dejavu-core`) were individually confirmed to exist under their
current names in trixie before assuming they're fine. **Still not
confirmed**: that the corrected `docker compose build web worker beat`
actually succeeds — this fix is based on confirming the correct package
name exists, not on a successful rebuild I've personally observed (no
Docker in this sandbox). **Update: confirmed working** — the rebuild succeeded and, after fixing a separate container-lifecycle issue (below), all report-generation tests pass too.

**Update — the apt-get fix worked** (confirmed: `pytest` ran 22 tests
inside the container, 18 passed — Django/GDAL/the rest of the image are
fine). The 4 that failed were exactly the 4 report-generation tests, all
with `No module named 'weasyprint'` / `'openpyxl'` — meaning those two
packages, despite being in `requirements.txt`, aren't actually installed in
the container `pytest` ran in. This is **not** a code or package-name bug;
`weasyprint>=62.0`/`openpyxl>=3.1` are correctly present in
`requirements.txt`. The most likely cause: `docker compose build web worker
beat` rebuilds the *images*, but any already-running containers keep using
the *old* image until they're recreated — `docker compose exec web pytest`
against a container that was never restarted after the rebuild would see
exactly this (new code present via the bind-mounted volume, but the old
image's installed packages). The fix is `docker compose up -d --build`
(rebuild and recreate in one step) rather than `build` followed by `exec`
against a container that's still the old one — or explicitly
`docker compose down && docker compose up -d --build` if `up -d --build`
alone doesn't force it. All 18 non-report tests passing is strong evidence
the actual application code for this phase is correct; this looks like a
container-lifecycle issue, not something to go fix in the codebase.
**Confirmed: 22/22 passing** after `docker compose up -d --build`.

**Notifications.** `Notification` (in-app record) + `NotificationPreference`
(per-user, per-category in-app/email toggle — no stored row for a category
means both channels enabled by default, so nobody misses a real problem
just because a settings screen that doesn't exist yet was never visited).
`apps/notifications/services.py`'s `notify()` is the single write path both
channels go through. `GET /api/notifications/` and
`PATCH /api/notification-preferences/` match `docs/04-api-spec.md` exactly
— the preferences endpoint deliberately isn't a per-object REST resource
(no `{id}` in the spec'd path); it's a bulk upsert-by-category endpoint.

All six trigger types from `docs/12-security-ops.md`'s list are implemented:

- Five run as daily Celery Beat checks (`apps/notifications/tasks.py`):
  compliance expiry, tree health anomaly, stalled sync, review backlog,
  approvals pending too long.
- The sixth — a failed processing run — is event-driven, called directly
  from `apps/imagery/services.py`'s `run_processing()` the moment a run
  fails, per the doc's own distinction ("event-driven triggers for
  anything that shouldn't wait").

Compliance-expiry notifications fire on **exact-day matches** against
`settings.COMPLIANCE_NOTIFICATION_THRESHOLDS_DAYS` (default `[90, 60, 30]`),
not "less than N days" — the latter would re-notify daily for the entire
window, which is precisely what docs/12-security-ops.md warns against
("sending everyone everything is how alerts stop getting read"). The other
checks don't have that same escalation shape and currently have no
dedup/snooze logic — a still-stalled device or still-open backlog gets
notified again every time the daily check runs. Not wrong, but worth
knowing before turning this on for a real team.

**The "stalled sync" check needed a real gap filled first.** Nothing in the
existing offline-sync design (`docs/05-offline-sync.md`) gave the server
any visibility into a client's local IndexedDB outbox — there was no
signal a Beat task could check at all. Added `apps/sync/models.py`'s
`SyncCheckIn` (user, device_id, pending_count, last_checkin_at) plus
`POST /api/sync/checkin/`, and wired `pwa/js/sync.js`'s `runSync()` to
report its outbox size after every sync attempt (success or failure alike).
This is apps/sync's first model — it had none before, just the pull
endpoint. Flagging this as a real design decision, not a trivial add: it
means "stalled" is only as fresh as the last time a device was online long
enough to check in, which is an inherent limit of a pull-based offline
architecture, not a bug in this implementation.

**What's verified vs. not for notifications:** `manage.py check` and
`makemigrations --check --dry-run` are clean; `node --check` passes on the
updated `pwa/js/sync.js`; all task names were confirmed to register
correctly with Celery (`apps.notifications.tasks.check_*`,
`apps.reporting.tasks.*`) matching `CELERY_BEAT_SCHEDULE` in
`palmvision/settings/base.py` exactly. The actual DB-dependent logic in
each Beat check (the queries, the threshold math, the email/in-app
delivery split) is covered by 16 new tests in `apps/notifications/tests.py`
plus 6 in `apps/reporting/tests.py` — **none of these 22 have run against
live Postgres yet**. Run next:

```bash
docker compose exec web python manage.py migrate
docker compose exec web pytest apps/reporting apps/notifications -v
```

Not yet built for Phase 7: any office-dashboard UI (a notification bell,
a report-download page — still API-only, same as Phases 5–6); per-device
snooze/dedup for the non-compliance notification checks; the PWA's own
"you have unsynced items" in-app banner (the server-side half — SyncCheckIn
+ the stalled-sync Beat check — is built; nothing surfaces this back to the
field user inside the PWA itself yet, which is arguably where it matters
most). `apps/reporting/tasks.py`'s scheduled-report day-of-week/day-of-month
rules are deliberately simple fixed-day logic, not a full per-definition
cron scheduler.

## Phase 8 — polish & pre-deployment hardening

Per docs/02-build-plan.md's checklist:

**Test coverage for the four highest-risk areas.** All four already had
real coverage from earlier phases; this pass found and closed the specific
gaps rather than adding coverage from scratch:

- Approval authorization — Harvest's approve action was well covered
  (Manager can, PIC Block can't, unrelated Manager can't, terminal-status
  400, history recorded), but Fertilizer's — the exact same view/permission
  code — had zero tests. Added 3 (manager-can, pic-block-cannot,
  unrelated-manager-cannot) plus one for the unauthenticated case (401, not
  a silent pass) in `apps/operations/tests.py`.
- Offline sync idempotency — HarvestTree's resubmit-in-place path was
  tested; FertilizerTree's (same `IdempotentUUIDCreateMixin`) wasn't. Added
  the parallel test.
- Detection clipping/matching — already thoroughly covered in
  `apps/imagery/tests.py` from Phase 5 (real rasterio I/O, all four match
  outcomes, the full pipeline chain, the clip-failure path). No gap found.
- Compliance export correctness — the due-diligence export's *output*
  (polygon, precision, baseline status) was covered, but every test used
  `as_manager`; nothing asserted a non-Manager is actually refused. Added
  `test_pic_block_cannot_trigger_due_diligence_export`.

**GitHub Actions CI** (`.github/workflows/ci.yml`) — two jobs: `lint` (ruff,
fast, no DB) and `test` (pytest against real Postgres+PostGIS and Redis
service containers — not `docker compose`, since GitHub Actions' `services:`
give the same real-database guarantee with far less orchestration overhead;
the Dockerfile/docker-compose.yml remain what actually deploys). The `test`
job installs the same GDAL/GEOS/PROJ/WeasyPrint system packages the
Dockerfile does, including the corrected `libgdk-pixbuf-2.0-0` name from
the Phase 7 trixie mixup — also verified that name is fine on Ubuntu
(`ubuntu-latest`'s runner OS), not just Debian, before using it here too.

**Linting.** Added `ruff` (`pyproject.toml` for config) — only pyflakes-
equivalent rules (unused imports, undefined names) are enabled, not
line-length/style nits, since this codebase's docstrings are deliberately
long and explanatory by design (CLAUDE.md's own convention) and style-nit
noise wouldn't serve anyone. Running it for the first time surfaced 4 real,
small issues accumulated across earlier phases — all fixed: an unused
`GrievanceRecord` import in `apps/compliance/tests.py`, an unused
`block_b` variable assignment in `seed_demo_data.py`, and two unused
imports (`Polygon`, `classify_detection`) in `apps/imagery/tests.py`. None
were functional bugs, just accumulated cruft — exactly what a linter step
is for catching before it compounds further.

**Permission-class audit.** Grepped every `views.py` across every app for
ViewSet/APIView class definitions and checked each declares
`permission_classes` explicitly (not inherited implicitly, not assumed).
**Clean result: every single one already does** — no gap found. The only
views without `permission_classes` are `TokenObtainPairView`/
`TokenRefreshView` (simplejwt's own, deliberately `AllowAny` — you can't
require login to log in), which is correct, not an oversight.

**Debug-leftovers / hardcoded-credentials audit.** Grepped for `print()`-
as-debugging, `pdb`/`breakpoint()`, `dd()`-equivalents, and hardcoded
passwords/secrets across all source (excluding tests/fixtures, where
throwaway demo passwords are expected and already flagged as local-dev-only
everywhere they appear — see README.md/docs/11-local-development.md).
**Clean result.** The only hardcoded-looking value is
`palmvision/settings/local.py`'s fallback `SECRET_KEY` — already correctly
gated behind `if not SECRET_KEY:`, scoped to the local-dev-only settings
module, and explicitly commented "never do this in a production settings
module." Not a finding; this is the same pattern Django's own
`startproject` scaffold uses.

**Backup & restore.** New `docs/14-backup-restore.md` documents the policy
from `docs/12-security-ops.md` and what is/isn't built (real WAL archiving,
object-lock compliance storage, and a production backup schedule are
explicitly **not** built — no production deployment target exists yet, per
the build plan's own scope). What is built: `scripts/backup_db.sh`
(`pg_dump -Fc` against the running `db` compose service) and
`scripts/restore_db.sh` (restores into a **separate** test database, never
the live dev DB, then prints row counts across tables from every phase as
a sanity check). **Both scripts were syntax-checked (`bash -n`) but the
actual drill has not been run end-to-end** — no Docker in this sandbox, so
there's no live `db` service to run `pg_dump`/`pg_restore` against. Please
run `scripts/backup_db.sh` then `scripts/restore_db.sh <the output file>`
yourself and confirm the row counts look right — this is the one Phase 8
checklist item that genuinely cannot be verified here, structurally or
otherwise.

**README/docs accuracy pass.** Cross-checked `README.md` and
`docs/11-local-development.md` (the doc the Phase 8 verification gate
specifically names) against the actual project, rather than assuming they
were still accurate. Found two real bugs in
`docs/11-local-development.md`'s PostGIS-sanity-check snippet: it imported
`from farms.models import FarmBlock` (wrong — every app lives under
`apps/`, so this would raise `ModuleNotFoundError` for a new developer
following it verbatim) and called `.query` on the queryset instead of
evaluating it (`.query` only builds the SQL string, never executes it, so
it could never actually surface the GDAL/GEOS-missing error the
surrounding text says to watch for). Both fixed. Also updated: the
now-stale "There is no Dockerfile checked in yet" line (Phase 0 built it;
Phase 7 extended it), added Linting and Backup & restore subsections, and
refreshed `.env.example`'s Phase 5/6 comments that still said "not built
yet" for phases that have been done for a while, plus added the Phase 7
notification-threshold env vars that were missing from it entirely.

**What's verified vs. not for Phase 8:** `ruff check .`, `manage.py check`,
and `makemigrations --check --dry-run` are all clean (no model changes this
phase, so no new migrations). The CI workflow's YAML was validated with a
parser but has never actually run on GitHub — it can't, from this sandbox,
without a real push. The backup/restore scripts are syntax-valid but
unexecuted, as above. Everything else (the new tests, the permission/
debug audits, the doc corrections) is either pure static analysis (grep,
ruff) that doesn't need a live database, or new tests that follow the
exact same "not yet run against live Postgres" caveat every other test in
this project has had until the user runs them for real.

## Known gaps in what's built (not blockers, but don't forget these)

- **FK write-scope validation is incomplete.** No viewset validates that a
  foreign key in a create/update payload (e.g. `Tree.farm_block`,
  `HarvestTree.tree`) actually belongs to the requesting user's scope —
  DRF's default `PrimaryKeyRelatedField` only checks that the referenced
  row *exists*, not that the user is allowed to reference it. In practice
  this means a PIC Block user could currently POST a `Tree` naming a
  `farm_block` they have no access to, and it would succeed. This is
  broader than `IdempotentUUIDCreateMixin` (which now has its own
  cross-scope check on the *update* path, see above) — it affects the
  plain `perform_create()` path on every scoped viewset. Worth a dedicated
  pass (likely a shared serializer `validate()` mixin that scopes
  `PrimaryKeyRelatedField` querysets to the requesting user) before this
  goes anywhere near real field data.

## Not built yet

- **Phase 4 — remaining items.** Browser-verified offline behavior (see
  above — this is the priority before writing more Phase 4 code), a real
  map view, tree editing, real icon art.
- **Phase 5 — remaining items.** Live-Postgres test confirmation (see
  above), tiling/preview generation, chunked/resumable upload, and any
  office-dashboard UI — the backend pipeline is built, nothing user-facing
  for it is yet.
- **Phase 6 — remaining items.** The storage-lifecycle job that consults
  `Image.is_compliance_exempt()` (still just a hook, no job calls it), and
  any office-dashboard UI.
- **Phase 7 — remaining items.** Any office-dashboard UI, per-check
  notification dedup/snooze, and the PWA-side "you have unsynced items"
  banner.
- **Phase 8 — remaining items.** The actual backup/restore drill run
  end-to-end (scripts are written and syntax-checked; the drill itself
  needs to happen on a machine with Docker — see above), and confirming
  the CI workflow actually goes green on a real push to GitHub (validated
  as syntactically correct YAML here, never run for real).

## Recommended next step

Run `docker compose up -d --build && docker compose exec web python
manage.py migrate && docker compose exec web pytest -v && docker compose
exec web ruff check .` to confirm everything (Phases 0–8) is solid against
a real database and passes linting. Then run the backup/restore drill
(`docs/14-backup-restore.md`) — the one piece of Phase 8 that couldn't be
verified in this sandbox at all. Push to GitHub to confirm the new CI
workflow (`.github/workflows/ci.yml`) actually goes green.

At that point every phase in `docs/02-build-plan.md` is built and
structurally verified, and every phase through 6 has live-Postgres
confirmation from a real run; Phase 7 and 8 are pending that same final
confirmation. This is genuinely close to "done, pending your own
end-to-end run" — the remaining work is verification, not new features,
unless something surfaces in that run that needs fixing. If it does, this
project has a well-established pattern for that by now: report exactly
what failed, and it'll get fixed the same way the last several rounds
were.
