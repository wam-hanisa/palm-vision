# Palm Vision (Python rebuild) — instructions for Claude Code

This file is the entry point. Read it first, then **`BUILD_STATUS.md`** (what's
actually built and verified vs. still open — do not skip this), then
`docs/02-build-plan.md` before writing any code.

## What this project is

A plantation management system for an Indonesian oil-palm operation, rebuilt from an existing Laravel/PHP app into Python/Django. Full background and rationale are in `docs/` — this file is a working reference, not the design rationale. If you want the "why" behind a decision, the relevant `docs/*.md` file has it; don't re-derive it from scratch.

Two source documents also exist at the repository's parent level and are worth reading once for context if anything here is ambiguous:
- `Palm_Vision_PDR.md` — reverse-engineered analysis of the original Laravel app (what exists today, including its bugs and gaps)
- `Palm_Vision_Python_Rebuild_PDR.md` — the full design proposal this doc set was extracted from

## Non-negotiable ground rule: local-first

**Nothing in this project should be built or reasoned about as a deployment concern until it runs correctly on `docker compose up` on a local machine.** Every phase in `docs/02-build-plan.md` has a "verification gate" — a concrete, checkable condition that must be true before moving to the next phase. Do not skip a gate to make faster progress on a later phase. Do not add production infrastructure (managed database, real object storage, real domain, TLS, CI deployment) until every phase gate in the build plan is green locally. `docs/11-local-development.md` is the authority on what "runs locally" means and how to verify it.

## Tech stack (see `docs/01-architecture.md` for the full table and diagram)

- Django 5.2 LTS, Python 3.12+
- GeoDjango + PostgreSQL/PostGIS (real `PointField`/`PolygonField` geometry — never store coordinates as text/strings)
- Django REST Framework + `djangorestframework-simplejwt` for the API the offline PWA talks to
- Celery + Redis for background jobs (imagery processing, detection/classification, reports, notifications)
- Progressive Web App (vanilla JS + a service worker, not a framework) for the offline-capable field client
- Django templates + Bootstrap 5 for the office/admin dashboard
- pytest-django for tests, GitHub Actions for CI (added in the last build phase, not skipped)

## Project structure convention

Use a Django "apps by domain" layout, not a single monolithic app:

```
palmvision/
  settings/            # base.py, local.py, (production.py written later, not yet)
  farms/                # Farm, FarmBlock, UserFarmRole, geometry validation
  trees/                # Tree, TreeDetection, TreeHealthClassification
  imagery/               # Image, ImageProcessingRun, detection/classification pipeline, ModelVersion
  operations/             # Harvest, Fertilizer, ActualHarvest, ActualFertilizer, HarvestTree, FertilizerTree
  compliance/             # ComplianceDocument, DeforestationBaseline, ComplianceAudit*, HarvestBatch, GrievanceRecord
  reporting/               # ReportDefinition, ReportRun, generation tasks
  notifications/            # Notification model, delivery, Celery Beat triggers
  sync/                    # offline sync pull/push endpoints, client-UUID handling
  users/                    # custom User model glue, role scoping
static/, templates/         # office dashboard
pwa/                        # field client: service worker, IndexedDB schema, sync queue JS
docs/                       # this doc set — read before building the corresponding app
```

Each Django app should own its own models, serializers, permissions, and Celery tasks — don't centralize all serializers or all tasks in one place.

## Read order for each build phase

Before starting a phase in `docs/02-build-plan.md`, read the doc(s) it references. Don't implement a feature from memory of this file alone — the referenced doc has the actual field lists, edge cases, and reasoning:

| Phase | Read first |
|---|---|
| 0 — scaffold | `docs/11-local-development.md` |
| 1 — core models & auth | `docs/03-data-model.md`, `docs/12-security-ops.md` (§ role scoping, § geometry validation) |
| 2 — harvest/fertilizer workflows | `docs/03-data-model.md`, `docs/12-security-ops.md` (§ audit logging) |
| 3 — API + offline sync foundation | `docs/04-api-spec.md`, `docs/05-offline-sync.md` |
| 4 — PWA field client | `docs/05-offline-sync.md` |
| 5 — imagery pipeline | `docs/06-ml-pipeline.md` |
| 6 — compliance module | `docs/07-compliance.md` |
| 7 — reporting & notifications | `docs/08-reporting.md`, `docs/12-security-ops.md` (§ alerting) |
| 8 — polish & pre-deployment hardening | `docs/12-security-ops.md` (all), `docs/13-open-questions.md` |

## Decisions already made — don't relitigate these

- Detection and classification are **two separate models/functions**, not one combined step (`docs/06-ml-pipeline.md`).
- Detection runs on an image **clipped to the target block's polygon boundary first** — never filter after the fact as the primary mechanism; clipping is primary, a post-filter `within` check is a secondary safety net (`docs/06-ml-pipeline.md`).
- A block must have a drawn boundary polygon before any image can be processed against it. Refuse and surface a clear message; never fall back to processing the unclipped image.
- One uploaded `Image` can be processed independently against multiple `FarmBlock`s via separate `ImageProcessingRun` rows — `Image` belongs to a `Farm`, not a single block.
- The tree-matching distance threshold is per-`FarmBlock`, configurable, not a global constant.
- Offline-writable records use **client-generated UUIDs**, and sync is treated as **append-only** — editing an existing record is an online-only action. Don't build merge/conflict-resolution logic beyond this; it's a deliberate scope boundary, not an oversight (`docs/05-offline-sync.md`).
- Proximity-based tree suggestions in the field UI **must show the nearest 2–3 candidates and require manual confirmation** — never auto-submit yield data against a GPS-guessed tree.
- Certification/traceability data (compliance module) is scoped at the **block (plot) level**, matching how certification schemes actually audit. Operational data (health, yield) stays tree-level. These are not in conflict — don't try to force compliance records down to tree granularity.
- System scope currently **ends at "harvest logged in the field."** Do not build mill delivery, weighbridge, OER reconciliation, or downstream integration — `HarvestBatch` exists as the seam for that later, not a callout to build it now.
- Model validation/retraining is owned by the project owner directly, not an ML-ops team — keep the tooling in `docs/06-ml-pipeline.md` (registry, shadow mode, rollback) lightweight; don't reach for a heavier MLOps platform unprompted.
- Labor/harvester-level productivity tracking is an **open question**, not decided either way — see `docs/13-open-questions.md`. Don't build it, and don't build something that assumes it won't be needed either (e.g. don't make `recorded_by` un-extendable).

## Coding conventions

- Follow standard Django/DRF idioms; don't introduce a second web framework or a second ORM.
- Every state-changing DRF endpoint needs an explicit permission class — no endpoint should rely on "authenticated" alone if the original design says a specific role is required. This is a hard rule: the original Laravel app's core flaw was silently-bypassable approvals (see `Palm_Vision_PDR.md`), and repeating that here would defeat the entire point of the rebuild.
- Models identified as safety-critical or compliance-critical in `docs/12-security-ops.md` § Audit Logging must have history tracking wired in from the commit that introduces them, not retrofitted later.
- Write tests alongside each phase's models/views, not only in the final polish phase — the polish-phase test task is for coverage gaps and integration tests, not the first tests written for a feature.
- Don't add new external services/dependencies (SMS gateways, third-party MLOps platforms, forest-monitoring APIs, etc.) beyond what's specified without flagging it — several docs explicitly call out "this is a plausible integration point, not a decision," meaning: implement the interface/seam, don't wire up a real paid third-party integration without confirming.
