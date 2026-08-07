# Local development

This is the authority on "runs locally." Nothing in `02-build-plan.md` should be considered done if it only works against a hypothetical deployed environment — every phase gate is checkable against what's described here.

## Prerequisites

- Docker Desktop (or equivalent Docker Engine + Compose v2)
- Nothing else strictly required on the host — Python, GDAL, and all other dependencies live inside the containers. Installing Python 3.12 locally is convenient for editor tooling but not required to run the app.

## Services (`docker-compose.yml`)

| Service | Purpose |
|---|---|
| `db` | PostgreSQL + PostGIS (`postgis/postgis` image) |
| `redis` | Celery broker/result backend |
| `web` | Django dev server |
| `worker` | Celery worker (imagery processing, ML pipeline, reports, notifications) |
| `beat` | Celery Beat (scheduled reports, alert checks) |

**Update:** the `Dockerfile` now exists (written in Phase 0, extended in Phase 7) — the paragraph below is what it needed to contain, kept as a reference for what each package in it is for. It has: a Python 3.12 base image (currently resolves to Debian trixie — see the Dockerfile's own comments for a real trixie-vs-bookworm package-naming gotcha this hit during Phase 7), system packages for GDAL/GEOS/PROJ (GeoDjango's spatial library dependencies), `libpq-dev` (Postgres client library), WeasyPrint's native Pango/cairo/gdk-pixbuf rendering libraries (Phase 7 reporting), and a `pip install -r requirements.txt` step.

## First-time setup

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Then visit `http://localhost:8000/admin` — a working, empty Django admin panel confirms Phase 0 is done. Nothing beyond auth should exist yet at that point.

## Seed data

Once Phase 1 models exist, a management command (e.g. `python manage.py seed_demo_data`) should create: one demo farm, 2–3 blocks with real (simplified is fine) boundary polygons, a handful of trees, and one user per role (Super Admin, Manager, PIC Block). Every phase's verification gate in `02-build-plan.md` assumes this seed data exists — build the command in Phase 1 and keep it current as new required fields get added in later phases, don't let it go stale.

## Verifying PostGIS specifically

A quick sanity check that GeoDjango is actually talking to PostGIS correctly (not just that Postgres is up):

```bash
docker compose exec web python manage.py shell -c "
from django.contrib.gis.geos import Point
from apps.farms.models import FarmBlock
print(FarmBlock.objects.filter(boundary__contains=Point(0, 0)).count())
"
```

**Two corrections to this snippet from how it originally read in this doc,
both caught by actually running it, not assumed:** the import is
`apps.farms.models` (this project's actual module path — every app lives
under `apps/`), not `farms.models`; and the query needs `.count()` (or
`list(...)`, or any other queryset-evaluation), not `.query` — `.query`
only builds the SQL string without executing it, so it would never
actually surface a GDAL/GEOS wiring problem the way this check is meant to.

If the corrected command raises an error about missing GDAL/GEOS libraries
rather than returning a number (`0` is fine — the point is that it
returns, not what it returns), the Dockerfile's system dependencies are
incomplete — fix that before proceeding, don't work around it with a
non-spatial workaround.

## Testing offline behavior locally

The PWA's offline behavior can be tested without a real phone: open Chrome/Edge DevTools → Network tab → set throttling to "Offline." Data entry should continue working against IndexedDB; re-enabling the network should trigger sync automatically. This is the mechanism the Phase 4 verification gate in `02-build-plan.md` relies on.

## Running tests

```bash
docker compose exec web pytest
```

## Linting

```bash
ruff check .
```

Config lives in `pyproject.toml` — only pyflakes-equivalent rules (unused
imports, undefined names, etc.) are enabled, not style/line-length nits,
which would just fight this codebase's deliberately explanatory-comment
convention (see `CLAUDE.md`). This same command runs in CI —
`.github/workflows/ci.yml`'s `lint` job.

## Backup & restore

See `docs/14-backup-restore.md` for the full policy and drill procedure.
Quick version:

```bash
scripts/backup_db.sh                                   # take a backup
scripts/restore_db.sh backups/palmvision_<timestamp>.dump   # drill: restore it into a separate test DB
```

## Common issues

- **GDAL/GEOS import errors on `web` or `worker` startup** — almost always a Dockerfile system-package gap, not a Python package issue. Check the base image actually has `gdal-bin`/`libgdal-dev` (or equivalent for the chosen base image) installed, not just `pip install gdal`.
- **`worker` can't reach `redis` or `db`** — check `depends_on` conditions in `docker-compose.yml` are using `service_healthy`, not just service-started, since Django/Celery will fail fast if Postgres/Redis aren't actually ready to accept connections yet.
- **Migrations fail with a PostGIS extension error** — the `postgis/postgis` image should have the extension available by default; if migrations reference `django.contrib.postgres` features Postgres doesn't have the extension for, confirm `CREATE EXTENSION postgis;` succeeded (check `db` service logs on first boot).

## What's explicitly not covered here

Production deployment — managed database services, real object storage credentials, a real domain, TLS, secrets management, horizontal scaling. None of that should be attempted until every phase in `02-build-plan.md` is verified against this local setup.
