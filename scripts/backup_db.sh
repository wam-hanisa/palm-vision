#!/usr/bin/env bash
# Local backup helper — docs/14-backup-restore.md.
#
# Uses `pg_dump -Fc` (custom format: compressed, and the only format
# `pg_restore` in restore_db.sh's --no-owner mode accepts cleanly) against
# the running `db` compose service. This is a manual/local-dev stand-in for
# the "automated daily full backups plus continuous WAL archiving" policy
# docs/12-security-ops.md § Backup & disaster recovery describes — that
# policy is for a real deployed environment (a managed Postgres backup
# service, or a cron'd version of this same pg_dump approach against a real
# server), not something this script itself implements end-to-end.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/../backups"
mkdir -p "$BACKUP_DIR"

DB_USER="${DB_USER:-palmvision}"
DB_NAME="${DB_NAME:-palmvision}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="$BACKUP_DIR/palmvision_${TIMESTAMP}.dump"

echo "Backing up database '$DB_NAME' (via docker compose 'db' service) to:"
echo "  $OUT_FILE"

docker compose exec -T db pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$OUT_FILE"

echo "Done — $(du -h "$OUT_FILE" | cut -f1) written."
echo "To test this backup is actually restorable, run: scripts/restore_db.sh $OUT_FILE"
