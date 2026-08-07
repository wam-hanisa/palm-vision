# Backup & restore

docs/02-build-plan.md Phase 8 requires documenting the backup/restore
procedure from `12-security-ops.md` § Backup & disaster recovery, and
actually running a local restore drill once against the `db` compose
service. This doc is that procedure; `scripts/backup_db.sh` and
`scripts/restore_db.sh` are the runnable pieces.

## Policy (docs/12-security-ops.md § Backup & disaster recovery)

- **Database**: automated daily full backups plus continuous WAL archiving
  for point-in-time recovery, in a real deployed environment. Retention:
  daily backups for 30 days, weekly for a year, as a starting default —
  tune to actual risk tolerance; this is a business decision, not something
  finalized here.
- **Compliance documents need a stricter policy** than routine backups:
  object storage versioning enabled, ideally an object-lock/write-once
  setting on compliance evidence specifically, honoring the 5-year EUDR
  retention requirement (`07-compliance.md`). Routine imagery doesn't need
  this; compliance evidence does. Not implemented here — this project has
  no production object storage configured yet (`AWS_STORAGE_BUCKET_NAME`
  etc. in `.env.example` are blank; local dev uses filesystem storage).
- **Backup restore drills, on a schedule.** An untested backup is a belief,
  not a plan. Quarterly restore-to-staging exercises catch silent backup
  failures before they matter.
- **RTO/RPO targets**: RPO of a few hours (via WAL archiving), RTO of
  same-day restore, as reasonable defaults — again, a business call to
  confirm, not something finalized unilaterally by this build.

None of the "real deployed environment" pieces above (managed WAL
archiving, object-lock compliance storage, a production backup schedule)
are built or configurable yet — this project has no production deployment
target defined (explicitly out of scope per `02-build-plan.md`). What
*is* built and runnable today is the local backup/restore mechanism below,
which is also the foundation any real production backup approach would
build on (`pg_dump`/`pg_restore` against the same Postgres/PostGIS the
`db` compose service runs).

## Taking a backup

With `docker compose up -d` running (at least the `db` service):

```bash
scripts/backup_db.sh
```

Writes a timestamped custom-format dump (`pg_dump -Fc`) to `backups/`.
`backups/` is gitignored — dumps can contain real data and shouldn't be
committed.

## Running a restore drill

```bash
scripts/restore_db.sh backups/palmvision_<timestamp>.dump
```

This restores into a **separate** database
(`${DB_NAME}_restore_test` by default — e.g. `palmvision_restore_test`),
never over the live dev database, then prints row counts from several key
tables spanning every phase of this app (farms, blocks, trees, harvests,
compliance documents, imagery) so you can eyeball that the restore
actually contains real data, not just that the command exited 0. Drop the
test database when you're done (the script prints the exact command).

## What this drill does and doesn't prove

**Proves:** a `pg_dump` of this project's actual schema — including every
GeoDjango geometry column and PostGIS extension dependency — restores
cleanly into a fresh database via `pg_restore`, and the restored data is
real (non-zero, plausible row counts), not silently corrupted or empty.

**Doesn't prove:** that a *real* production backup pipeline (WAL archiving,
automated scheduling, off-site storage, the 30-day/1-year retention policy)
is actually running anywhere — none of that exists yet, per the "not built"
note above. This drill validates the restore *mechanism*, which any real
backup pipeline would still depend on; it doesn't stand in for building
that pipeline.

## Running the drill yourself

This was written and the scripts were syntax-checked (`bash -n`) in a
sandbox with no Docker, so **the actual drill has not been run end-to-end
here** — there's no live `db` compose service to run it against. Please
run `scripts/backup_db.sh` then `scripts/restore_db.sh <the resulting
file>` yourself and report back what the row-count output shows. If
`pg_restore` errors, the most likely cause is a version mismatch between
the `pg_dump`/`pg_restore` binaries inside the `db` container (Postgres 16,
per `docker-compose.yml`) — both commands run *inside* that same container
via `docker compose exec`, so this shouldn't actually be possible, but it's
the first thing to check if it is.
