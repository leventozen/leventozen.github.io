#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose -p restore-drill -f docker-compose.yml)
EVIDENCE_DIR=evidence
RAW_DIR=evidence/raw
mkdir -p "$RAW_DIR" "$EVIDENCE_DIR"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# macOS date has no %N; use Python for millisecond precision.
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

pg() {
  "${COMPOSE[@]}" exec -T postgres "$@"
}

ch() {
  "${COMPOSE[@]}" exec -T clickhouse clickhouse-client "$@"
}

BACKUP_ID="${1:-$(cat evidence/raw/latest-retrieved-id.txt)}"
RETRIEVED="retrieved/${BACKUP_ID}"
[[ -d "${RETRIEVED}" ]] || { echo "Missing retrieved backup set: ${RETRIEVED}" >&2; exit 1; }

CONTRACT=evidence/source-contract.json
[[ -f "${CONTRACT}" ]] || { echo "Missing source contract" >&2; exit 1; }

DRILL_ID="$(date -u +%Y%m%dT%H%M%SZ)"
PG_DRILL_DB="app_restore_drill_${DRILL_ID}"
CH_DRILL_DB="analytics_restore_drill_${DRILL_ID}"

DRILL_START_MS=$(now_ms)
RUN_META="${RAW_DIR}/run-${DRILL_ID}.env"
{
  echo "BACKUP_ID=${BACKUP_ID}"
  echo "DRILL_ID=${DRILL_ID}"
  echo "PG_DRILL_DB=${PG_DRILL_DB}"
  echo "CH_DRILL_DB=${CH_DRILL_DB}"
  echo "DRILL_START=$(ts)"
  echo "DRILL_START_MS=${DRILL_START_MS}"
} > "${RUN_META}"

echo "Scope: This local lab validates the recovery procedure after backup retrieval."
echo "Object-storage availability, credentials, and retrieval are separate failure modes and are not tested here."
echo "Drill IDs: PG=${PG_DRILL_DB} CH=${CH_DRILL_DB}"

########################################
# Checksums
########################################
echo "$(ts) checksum verification started" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"
(
  cd "${RETRIEVED}"
  sha256sum --check SHA256SUMS
) | tee "${RAW_DIR}/checksum-${DRILL_ID}.txt"
echo "$(ts) checksum verification completed" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

########################################
# PostgreSQL restore
########################################
PG_START_MS=$(now_ms)
echo "$(ts) PostgreSQL restore started" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

pg createdb --username=postgres --template=template0 "${PG_DRILL_DB}"

set +e
pg pg_restore \
  --username=postgres \
  --dbname="${PG_DRILL_DB}" \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  "/retrieved/${BACKUP_ID}/postgres.dump" >"${RAW_DIR}/pg-restore-${DRILL_ID}.log" 2>&1
PG_RC=$?
set -e

PG_END_MS=$(now_ms)
PG_DURATION_MS=$((PG_END_MS - PG_START_MS))
echo "$(ts) PostgreSQL restore completed rc=${PG_RC} duration_ms=${PG_DURATION_MS}" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

if [[ ${PG_RC} -ne 0 ]]; then
  echo "PostgreSQL restore failed; keeping drill database for inspection: ${PG_DRILL_DB}" >&2
  cat "${RAW_DIR}/pg-restore-${DRILL_ID}.log" >&2 || true
  exit 1
fi

echo "PostgreSQL restore: OK"
echo "Duration: ${PG_DURATION_MS}ms"
{
  echo "PG_RESTORE_OK=1"
  echo "PG_RESTORE_DURATION_MS=${PG_DURATION_MS}"
} >> "${RUN_META}"

########################################
# ClickHouse restore from exact verified artifact
########################################
CH_START_MS=$(now_ms)
echo "$(ts) ClickHouse restore started" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

RESTORE_REL="restore-drills/${DRILL_ID}.zip"
mkdir -p "clickhouse/backup-disk/restore-drills"
cp -f "${RETRIEVED}/clickhouse/backup.zip" "clickhouse/backup-disk/${RESTORE_REL}"

set +e
ch --query "
RESTORE DATABASE analytics
AS ${CH_DRILL_DB}
FROM Disk('backups', '${RESTORE_REL}')
" >"${RAW_DIR}/ch-restore-${DRILL_ID}.log" 2>&1
CH_RC=$?
set -e

CH_END_MS=$(now_ms)
CH_DURATION_MS=$((CH_END_MS - CH_START_MS))
echo "$(ts) ClickHouse restore completed rc=${CH_RC} duration_ms=${CH_DURATION_MS}" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"
cat "${RAW_DIR}/ch-restore-${DRILL_ID}.log"

if [[ ${CH_RC} -ne 0 ]]; then
  echo "ClickHouse restore failed; keeping drill DBs for inspection" >&2
  exit 1
fi

echo "ClickHouse restore: OK"
echo "Duration: ${CH_DURATION_MS}ms"
{
  echo "CH_RESTORE_OK=1"
  echo "CH_RESTORE_DURATION_MS=${CH_DURATION_MS}"
} >> "${RUN_META}"

########################################
# Verification
########################################
VERIFY_START_MS=$(now_ms)
echo "$(ts) verification started" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

python3 scripts/verify-restore.py \
  --contract "${CONTRACT}" \
  --pg-db "${PG_DRILL_DB}" \
  --ch-db "${CH_DRILL_DB}" \
  --out "${RAW_DIR}/verify-${DRILL_ID}.json" \
  --markdown "${RAW_DIR}/verify-${DRILL_ID}.md"

VERIFY_END_MS=$(now_ms)
VERIFY_DURATION_MS=$((VERIFY_END_MS - VERIFY_START_MS))
echo "$(ts) verification completed duration_ms=${VERIFY_DURATION_MS}" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"
echo "VERIFY_DURATION_MS=${VERIFY_DURATION_MS}" >> "${RUN_META}"

# Fail if any verification failed
python3 - <<PY
import json, sys
data=json.load(open("${RAW_DIR}/verify-${DRILL_ID}.json"))
failed=[r for r in data["checks"] if r["result"] != "PASS"]
if failed:
    print("Verification failures:", failed)
    sys.exit(1)
print("Verification: all PASS")
PY

########################################
# Cleanup (guarded)
########################################
echo "$(ts) cleanup started" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"

if [[ ! "${PG_DRILL_DB}" =~ ^app_restore_drill_[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "Refusing unsafe PostgreSQL cleanup target" >&2
  exit 1
fi
if [[ ! "${CH_DRILL_DB}" =~ ^analytics_restore_drill_[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "Refusing unsafe ClickHouse cleanup target" >&2
  exit 1
fi
if [[ "${PG_DRILL_DB}" != "app_restore_drill_${DRILL_ID}" ]]; then
  echo "PostgreSQL drill DB does not match current DRILL_ID" >&2
  exit 1
fi
if [[ "${CH_DRILL_DB}" != "analytics_restore_drill_${DRILL_ID}" ]]; then
  echo "ClickHouse drill DB does not match current DRILL_ID" >&2
  exit 1
fi
if [[ "${PG_DRILL_DB}" == "app" || "${CH_DRILL_DB}" == "analytics" ]]; then
  echo "Refusing production database cleanup" >&2
  exit 1
fi
[[ -f "${RAW_DIR}/verify-${DRILL_ID}.json" ]] || { echo "Missing verification evidence" >&2; exit 1; }

pg dropdb --username=postgres "${PG_DRILL_DB}"
ch --query "DROP DATABASE \`${CH_DRILL_DB}\`"

PG_APP_PRESENT=$(pg psql -U postgres -At -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_database WHERE datname='app') THEN 'PRESENT' ELSE 'NOT FOUND' END;")
PG_DRILL_PRESENT=$(pg psql -U postgres -At -c "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_database WHERE datname='${PG_DRILL_DB}') THEN 'PRESENT' ELSE 'NOT FOUND' END;")
CH_APP_PRESENT=$(ch --query "SELECT if(count() > 0, 'PRESENT', 'NOT FOUND') FROM system.databases WHERE name='analytics'")
CH_DRILL_PRESENT=$(ch --query "SELECT if(count() > 0, 'PRESENT', 'NOT FOUND') FROM system.databases WHERE name='${CH_DRILL_DB}'")

CLEANUP_PASS=1
[[ "${PG_DRILL_PRESENT}" == "NOT FOUND" ]] || CLEANUP_PASS=0
[[ "${CH_DRILL_PRESENT}" == "NOT FOUND" ]] || CLEANUP_PASS=0
[[ "${PG_APP_PRESENT}" == "PRESENT" ]] || CLEANUP_PASS=0
[[ "${CH_APP_PRESENT}" == "PRESENT" ]] || CLEANUP_PASS=0

{
  echo "PostgreSQL drill database:  ${PG_DRILL_PRESENT}"
  echo "ClickHouse drill database:  ${CH_DRILL_PRESENT}"
  echo "PostgreSQL app:             ${PG_APP_PRESENT}"
  echo "ClickHouse analytics:       ${CH_APP_PRESENT}"
  echo ""
  if [[ ${CLEANUP_PASS} -eq 1 ]]; then
    echo "Cleanup verification: PASS"
  else
    echo "Cleanup verification: FAIL"
  fi
} | tee "${RAW_DIR}/cleanup-${DRILL_ID}.txt"

if [[ ${CLEANUP_PASS} -ne 1 ]]; then
  echo "Cleanup verification failed" >&2
  exit 1
fi

DRILL_END_MS=$(now_ms)
echo "$(ts) cleanup completed" | tee -a "${RAW_DIR}/timeline-${DRILL_ID}.txt"
{
  echo "DRILL_END=$(ts)"
  echo "DRILL_END_MS=${DRILL_END_MS}"
} >> "${RUN_META}"

python3 scripts/write-evidence.py --run-meta "${RUN_META}"

echo "Drill completed successfully."
