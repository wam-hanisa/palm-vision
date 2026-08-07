# Build plan

Follow these phases in order. **Each phase ends with a verification gate — a concrete, checkable condition.** Do not start the next phase until the current gate passes. If a gate can't be made to pass, stop and flag it rather than moving on and hoping it resolves itself later; these gates exist specifically to catch that.

Deployment (production infrastructure, managed database, real object storage, domain, TLS) is deliberately not a phase in this plan. It comes after every phase below is verified locally — see `CLAUDE.md` for why, and don't add deployment-specific code or config before then.

---

## Phase 0 — Project scaffold & local environment

- [ ] `django-admin startproject palmvision` with the `settings/` split described in `01-architecture.md` (`base.py` + `local.py`; no `production.py` yet)
- [ ] Write the `Dockerfile` `docker-compose.yml` references — base image needs Python 3.12, plus system packages for GDAL/GEOS/PROJ (GeoDjango's dependencies), plus `libpq-dev` for Postgres
- [ ] `requirements.txt` (or `pyproject.toml`): Django 5.2, `djangorestframework`, `djangorestframework-simplejwt`, `django-cors-headers` (needed once the PWA is a separate origin in dev), `psycopg` (or `psycopg2-binary`), `celery`, `redis`, `django-storages`, `pytest-django`
- [ ] Configure `GDAL_LIBRARY_PATH`/`GEOS_LIBRARY_PATH` in settings if the base image needs it (varies by base image — verify, don't assume)
- [ ] `docker compose build && docker compose up` brings up all services without error
- [ ] `python manage.py migrate` runs cleanly against the `db` service
- [ ] `python manage.py createsuperuser` works, Django admin loads at `localhost:8000/admin`

**Verification gate:** a fresh clone, `cp .env.example .env`, `docker compose up`, `docker compose exec web python manage.py migrate`, `docker compose exec web python manage.py createsuperuser` results in a working, empty Django admin panel. No models exist yet — that's fine, this phase is proving the environment works at all.

---

## Phase 1 — Core domain models & auth

Read `03-data-model.md` and `12-security-ops.md` (§ Multi-Farm Role Scoping, § Geometry & Data Integrity Validation) first.

- [ ] Custom `User` model (or Django's default + a profile extension — pick one, don't do both) with Groups for Super Admin / Manager / PIC Block
- [ ] `UserFarmRole` join table (user, farm, role) — build this now even if only ever exercised with one farm; see `12-security-ops.md` for why
- [ ] `Farm`, `FarmBlock` (`boundary` as `PolygonField`), `Tree` (`location` as `PointField`) models, registered in Django admin
- [ ] `FarmBlock.tree_match_threshold_m` (nullable, falls back to `TREE_MATCH_DEFAULT_THRESHOLD_M` env default)
- [ ] Geometry validation: boundary `is_valid` check on save, orphaned-tree flagging, block-overlap warning (`12-security-ops.md` § Geometry & Data Integrity Validation)
- [ ] Session auth wired up for Django views; `djangorestframework-simplejwt` configured for the API
- [ ] Seed fixtures/management command: one demo farm, 2–3 blocks with real (even if simplified) boundary polygons, a handful of trees, one user per role
- [ ] `django-simple-history` (or `django-auditlog`) attached to `Tree` and `FarmBlock` now — don't defer audit logging to Phase 8 for models introduced this early (`12-security-ops.md` § Audit Logging)

**Verification gate:** logging in as each of the three seeded roles shows correctly scoped data; a spatial query (e.g. "which block contains this point") returns a correct result against the seed data; saving a deliberately self-intersecting polygon is rejected with a clear error.

---

## Phase 2 — Harvest & Fertilizer workflows

Read `03-data-model.md` and `05-offline-sync.md` (§ Yield can be logged at block level or tree level).

- [ ] `Harvest`, `Fertilizer` models with `TextChoices` status enums matching the plan → approve → execute workflow
- [ ] `ActualHarvest`, `ActualFertilizer` models
- [ ] `HarvestTree` extended with `quantity`, `weight_kg`, `recorded_location` (point), `recorded_by`, `recorded_at`; `FertilizerTree` as the equivalent link for fertilizer application
- [ ] `ActualHarvest.quantity` derives from summed `HarvestTree` rows when tree-level entries exist, otherwise stays a manually-entered block-level total — implement both paths, don't force one
- [ ] DRF permission classes on every state-changing endpoint (create plan, log actual, approve) — approval specifically must check the actor's role/permission, not just "is authenticated." This is the single most important rule carried over from auditing the original app; see `CLAUDE.md`.
- [ ] Audit history wired to `Harvest`/`Fertilizer` status transitions
- [ ] Tests: a non-Manager attempting to approve a plan is rejected; a plan can't skip a status transition

**Verification gate:** end-to-end via Django admin or a quick script — create a harvest plan, log a tree-level actual harvest, confirm the block total is derived correctly, approve it as a Manager-role user, confirm a PIC Block-role user cannot approve it, and confirm the approval shows up in that record's history.

---

## Phase 3 — REST API + offline sync foundation

Read `04-api-spec.md` and `05-offline-sync.md` in full.

- [ ] DRF viewsets/serializers for every model that needs API access (see `04-api-spec.md` for the representative list — extend consistently to models not explicitly listed)
- [ ] JWT token obtain/refresh endpoints
- [ ] Client-generated UUID support on offline-writable models (`Tree`, `HarvestTree`/actual-harvest records, `FertilizerTree`/actual-fertilizer records) — the API must accept a client-supplied ID rather than only server-generating one
- [ ] Sync semantics: offline-writable creates are append-only from the API's perspective; edits to existing records are a separate, online-only code path (don't build merge/conflict resolution beyond this — see `CLAUDE.md`)
- [ ] `GET /api/sync/pull/?since={timestamp}` — returns server-side changes since a given time, scoped to what the requesting user's role/farm assignment should see
- [ ] Tests: submitting the same client UUID twice is idempotent, not a duplicate-record error

**Verification gate:** using `curl` or an API client (not the PWA yet — that's Phase 4), you can authenticate, create a tree with a client-supplied UUID, create an actual-harvest record referencing it, and pull it back via the sync endpoint.

---

## Phase 4 — PWA field client

Read `05-offline-sync.md` in full, and `09-design-system.md` for the field-UI density/contrast requirements.

- [ ] Service worker: caches the app shell, intercepts network requests
- [ ] IndexedDB schema mirroring the offline-writable models from Phase 3
- [ ] Sync queue: local writes go to IndexedDB immediately, queue for background sync, replay against the API on reconnect
- [ ] Farm → Block → Tree navigation (`05-offline-sync.md` § geo-navigation) using the browser Geolocation API
- [ ] Proximity prompt for yield entry — nearest 2–3 trees shown, never auto-selected (this is a hard rule, not a suggestion — see `CLAUDE.md`)
- [ ] Offline screens: log harvest/fertilizer actuals, add/edit/soft-delete a tree
- [ ] Visible offline/sync status indicator, always on screen, not buried in a menu

**Verification gate:** with the browser's network throttling set to "offline," you can navigate farm → block → tree, log a harvest entry, and add a new tree — all of it visibly queued. Re-enabling the network triggers automatic sync without user action, and the queue clears.

---

## Phase 5 — Imagery pipeline

Read `06-ml-pipeline.md` in full before starting — this phase has the most sequencing-sensitive logic in the whole build.

- [ ] `Image` model at the farm level (not block-scoped), chunked/resumable upload to storage
- [ ] Georeferencing via `rasterio` on upload (Celery task); manual bounding-box fields kept as fallback
- [ ] `GET /api/images/{id}/overlapping-blocks/` — suggests which blocks a file's footprint intersects
- [ ] `ImageProcessingRun` model + the trigger endpoint (`POST /api/farm-blocks/{block_id}/images/{image_id}/process/`)
- [ ] Processing refuses to start if `FarmBlock.boundary` is null — surface a clear error, never fall back to processing unclipped
- [ ] Celery chain: clip raster to block boundary (`rasterio.mask.mask()`) → run detection model → match against existing `Tree` records via `dwithin` → run classification model on matched/confirmed crops → write `TreeDetection`/`TreeHealthClassification` rows
- [ ] Post-clip safety check: every detection's GPS point validated against the block polygon with a `within` lookup before being persisted
- [ ] Detection/classification models: for local dev, it's acceptable to stub these with a trivial placeholder (e.g. a function returning random/fixed predictions) if no trained model exists yet — the pipeline's correctness (clipping, matching, status transitions) needs to be verifiable independent of having a production-quality model. Wire the real model in via `DETECTION_MODEL_PATH`/`CLASSIFICATION_MODEL_PATH` when available; don't block this phase on having trained weights.
- [ ] Review queue endpoints/UI: confirm/reject/link for `new_candidate` and `ambiguous` detections
- [ ] `ModelVersion` registry + staging/shadow-mode comparison (`06-ml-pipeline.md` § Model Governance)

**Verification gate:** upload a small test image (doesn't need to be a real drone orthomosaic — a small georeferenced test raster is fine) against a seeded block with a boundary polygon, run it through the full chain with stub models, and confirm: the run's status progresses through each stage, detections outside the block boundary don't appear, and the review queue shows unmatched detections correctly.

---

## Phase 6 — Compliance module

Read `07-compliance.md` in full.

- [ ] `ComplianceDocument`, `DeforestationBaseline`, `ComplianceAuditTemplate`, `ChecklistItem`, `ComplianceAudit`, `AuditResult`, `HarvestBatch`, `GrievanceRecord` models
- [ ] Document expiry status computed from `expiry_date` (valid / expiring_soon / expired)
- [ ] Due diligence export: given a `HarvestBatch`, assemble block polygon (6-decimal precision check), deforestation-baseline status, and placeholder risk-assessment/supplier fields into a GeoJSON + structured document export
- [ ] Audit history wired to `ComplianceDocument`, `ComplianceAudit`, `DeforestationBaseline` (compliance-critical, per `CLAUDE.md`)
- [ ] Compliance-linked imagery flagged as exempt from the general storage lifecycle rule (`12-security-ops.md` § Image Storage Lifecycle)

**Verification gate:** upload a sample compliance document with a near-future expiry date, confirm its status computes correctly; generate a due-diligence export for a seeded `HarvestBatch` and confirm it includes the block's polygon coordinates and a deforestation-baseline status field (even if that status is "under_review" due to no real baseline source configured locally).

---

## Phase 7 — Reporting & notifications

Read `08-reporting.md` and `12-security-ops.md` (§ Alerting & Notifications).

- [ ] `ReportDefinition`/`ReportRun` models, Celery task for generation
- [ ] PDF export (WeasyPrint) and Excel export (`openpyxl`) for at least the production report and tree-health report
- [ ] Celery Beat scheduled report generation + email delivery (console backend is fine for local dev — see `.env.example`)
- [ ] `Notification` model, in-app + email delivery channels
- [ ] Celery Beat checks for: compliance expiry windows, tree-health anomalies, stalled sync (device with queued items past N days), detection review backlog, processing-run failures, approvals pending too long
- [ ] Per-user notification preferences (category + channel)

**Verification gate:** trigger a report generation on-demand and confirm a downloadable file is produced; manually set a `ComplianceDocument.expiry_date` into the near-future window and confirm the next Beat check produces a notification (visible in-app and in the console email backend's output).

---

## Phase 8 — Polish & pre-deployment hardening

- [ ] pytest-django coverage for: approval authorization (the highest-risk area), offline sync idempotency, detection clipping/matching correctness, compliance export correctness
- [ ] GitHub Actions workflow: run tests + linting on push
- [ ] Confirm every state-changing endpoint has an explicit permission class (grep for DRF views without one — don't rely on memory)
- [ ] Confirm no debug leftovers (no `print()`-as-debugging, no hardcoded test credentials, no equivalent of the original app's `dd()` calls)
- [ ] Document the backup/restore procedure from `12-security-ops.md` § Backup & Disaster Recovery Policy, and actually run a local restore drill once against the `db` compose service to confirm it works
- [ ] `README.md` walkthrough (this doc set's root `README.md`, or the generated project's own) works verbatim for someone with zero prior context

**Verification gate:** a new developer (or a fresh Claude Code session with no memory of this build) can clone the repo, follow `11-local-development.md` alone, and end up with a fully working local instance — auth, core models, offline PWA, imagery pipeline with stub models, compliance, and reporting all functional — without needing to ask a clarifying question about setup.

---

## Explicitly out of scope for this build

Don't build these unless separately asked — they're deferred or open, not forgotten:

- Mill delivery, weighbridge integration, OER reconciliation, or any downstream/post-harvest system connection (`HarvestBatch` is the seam for this later, per `07-compliance.md`)
- Labor/harvester-level identity tracking beyond `recorded_by` — see `13-open-questions.md`
- SMS notification delivery (mentioned as optional in `12-security-ops.md`, not committed to)
- A native mobile app (Flutter/React Native) — the PWA is the field client; native is a future option only if the PWA proves genuinely insufficient
- Any production deployment configuration
