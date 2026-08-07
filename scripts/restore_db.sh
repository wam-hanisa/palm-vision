#!/usr/bin/env bash
# Restore-drill helper — docs/14-backup-restore.md, docs/12-security-ops.md
# § Backup & disaster recovery: "Backup restore drills, on a schedule — an
# untested backup is a belief, not a plan."
#
# Restores into a FRESH, separate database (default name:
# "${DB_NAME}_restore_test"), never over the live dev database — the whole
# point of a restore drill is confirming the backup file itself works,
# without any risk of the drill overwriting real data if something about
# the dump or the restore process is broken.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <path-to-dump-file> [target-db-name]" >&2
  echo "Example: $0 backups/palmvision_20260807_120000.dump" >&2
  exit 1
fi

DUMP_FILE="$1"
if [ ! -f "$DUMP_FILE" ]; then
  echo "Error: dump file not found: $DUMP_FILE" >&2
  exit 1
fi

DB_USER="${DB_USER:-palmvision}"
BASE_DB_NAME="${DB_NAME:-palmvision}"
TARGET_DB="${2:-${BASE_DB_NAME}_restore_test}"

echo "Restoring $DUMP_FILE into fresh database '$TARGET_DB' (NOT the live dev DB)..."

docker compose exec -T db psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET_DB\";"
docker compose exec -T db psql -U "$DB_USER" -d postgres -c "CREATE DATABASE \"$TARGET_DB\";"
# PostGIS must be enabled in the target DB before restoring any
# geometry-column data — pg_restore doesn't create extensions for you.
docker compose exec -T db psql -U "$DB_USER" -d "$TARGET_DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;"

docker compose exec -T db pg_restore -U "$DB_USER" -d "$TARGET_DB" --no-owner < "$DUMP_FILE"

echo
echo "Restore complete. Row counts in the restored database (sanity check —"
echo "compare these against what you expect from the backed-up source):"
docker compose exec -T db psql -U "$DB_USER" -d "$TARGET_DB" -c "
SELECT 'farms_farm' AS table_name, count(*) FROM farms_farm
UNION ALL SELECT 'farms_farmblock', count(*) FROM farms_farmblock
UNION ALL SELECT 'trees_tree', count(*) FROM trees_tree
UNION ALL SELECT 'operations_harvest', count(*) FROM operations_harvest
UNION ALL SELECT 'compliance_compliancedocument', count(*) FROM compliance_compliancedocument
UNION ALL SELECT 'imagery_image', count(*) FROM imagery_image
ORDER BY table_name;
"

echo
echo "If those counts look right, the drill passed — this backup file is"
echo "genuinely restorable, not just present on disk."
echo
echo "Clean up the test database when done:"
echo "  docker compose exec db psql -U $DB_USER -d postgres -c 'DROP DATABASE \"$TARGET_DB\";'"
