#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose -p restore-drill -f docker-compose.yml)

pg() {
  "${COMPOSE[@]}" exec -T postgres "$@"
}

ch() {
  "${COMPOSE[@]}" exec -T clickhouse clickhouse-client --multiquery "$@"
}

ensure_dirs() {
  mkdir -p backups retrieved evidence/raw clickhouse/backup-disk
}

cmd_up() {
  ensure_dirs
  "${COMPOSE[@]}" up -d
  echo "Waiting for PostgreSQL..."
  for _ in $(seq 1 40); do
    if pg pg_isready -U postgres >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  echo "Waiting for ClickHouse..."
  for _ in $(seq 1 40); do
    if "${COMPOSE[@]}" exec -T clickhouse wget -qO- http://localhost:8123/ping >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  # ClickHouse initdb.d is not always reliable across image variants; apply explicitly.
  "${COMPOSE[@]}" exec -T clickhouse clickhouse-client --multiquery < clickhouse/init.sql
  echo "Lab is up."
}

cmd_down() {
  "${COMPOSE[@]}" down --remove-orphans
}

cmd_destroy() {
  "${COMPOSE[@]}" down --volumes --remove-orphans
  # Project-scoped host dirs only. Keep evidence/runs archives and published evidence markdown.
  rm -rf backups/* retrieved/* evidence/raw/* clickhouse/backup-disk/* 2>/dev/null || true
  echo "Destroyed restore-drill project resources only (evidence/runs preserved)."
}

cmd_seed() {
  # Laptop-friendly but non-toy final dataset. Timestamps relative to now (UTC).
  # oldest ~= now-7d, newest ~= now-2m
  echo "Seeding PostgreSQL (100 shops / 5k users / 250k orders)..."
  pg psql -U postgres -d app <<'SQL'
TRUNCATE orders, users, shops, ingestion_checkpoints RESTART IDENTITY CASCADE;

INSERT INTO shops (id, name, plan, created_at)
SELECT
  g,
  'Shop ' || g,
  CASE WHEN g = 1 THEN 'enterprise' WHEN g % 5 = 0 THEN 'pro' ELSE 'starter' END,
  now() - interval '7 days' + ((g - 1) || ' hours')::interval
FROM generate_series(1, 100) AS g;

INSERT INTO users (id, shop_id, email, created_at)
SELECT
  g,
  ((g - 1) % 100) + 1,
  'user' || g || '@example.test',
  now() - interval '7 days' + ((g - 1) || ' minutes')::interval
FROM generate_series(1, 5000) AS g;

-- Spread 250k orders across ~7 days, newest about 2 minutes ago.
INSERT INTO orders (id, shop_id, user_id, amount_cents, status, created_at)
SELECT
  g,
  ((g - 1) % 100) + 1,
  ((g - 1) % 5000) + 1,
  1000 + (g % 9000),
  CASE WHEN g % 10 = 0 THEN 'refunded' ELSE 'paid' END,
  (now() - interval '2 minutes')
    - make_interval(secs => (((250000 - g)::bigint * 7 * 86400) / 249999))
FROM generate_series(1, 250000) AS g;

INSERT INTO ingestion_checkpoints (source, checkpoint, updated_at)
SELECT
  'orders-feed',
  'offset:250000',
  max(created_at) + interval '1 second'
FROM orders;
SQL

  echo "Seeding ClickHouse (2M events, UTC timestamps)..."
  ch --query "TRUNCATE TABLE IF EXISTS analytics.events"
  ch --query "TRUNCATE TABLE IF EXISTS analytics.events_daily"

  # 2M events in 20 x 100k batches. Timestamps in UTC: newest ~2 minutes ago, span ~7 days.
  for batch in $(seq 0 19); do
    offset=$((batch * 100000))
    echo "  ClickHouse batch $((batch + 1))/20 (offset ${offset})..."
    ch --query "
INSERT INTO analytics.events
SELECT
  now('UTC') - INTERVAL 2 MINUTE
    - toIntervalSecond(
        intDiv(
          (toUInt64(1999999) - (toUInt64(number) + toUInt64(${offset}))) * toUInt64(7 * 86400),
          toUInt64(1999999)
        )
      ),
  (toUInt32(number) + toUInt32(${offset})) % 100 + 1,
  if((number + ${offset}) % 7 = 0, 'refund', if((number + ${offset}) % 3 = 0, 'purchase', 'view')),
  toFloat64(((number + ${offset}) % 100) + 1),
  toUInt32(1)
FROM numbers(100000)
SETTINGS max_block_size = 100000
"
  done

  echo "Materializing projection..."
  ch --query "ALTER TABLE analytics.events MATERIALIZE PROJECTION events_by_type"
  echo "Waiting for projection mutations to finish..."
  for _ in $(seq 1 60); do
    pending="$(ch --query "SELECT count() FROM system.mutations WHERE database='analytics' AND table='events' AND is_done=0" || echo 1)"
    if [[ "${pending}" == "0" ]]; then
      break
    fi
    sleep 2
  done
  echo "Seed complete."
}

cmd_verify_source() {
  python3 scripts/write-source-contract.py
  echo "Wrote evidence/source-contract.json"
}

cmd_backup() {
  BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)"
  DEST="backups/${BACKUP_ID}"
  mkdir -p "${DEST}/clickhouse" clickhouse/backup-disk

  echo "Creating PostgreSQL dump..."
  pg pg_dump -U postgres -Fc -d app -f "/backups/${BACKUP_ID}/postgres.dump"

  echo "Creating ClickHouse backup as zip on configured disk..."
  ch --query "BACKUP DATABASE analytics TO Disk('backups', '${BACKUP_ID}.zip')"

  if [[ ! -f "clickhouse/backup-disk/${BACKUP_ID}.zip" ]]; then
    echo "ClickHouse backup zip not found at clickhouse/backup-disk/${BACKUP_ID}.zip" >&2
    ls -la clickhouse/backup-disk || true
    exit 1
  fi

  cp -a "clickhouse/backup-disk/${BACKUP_ID}.zip" "${DEST}/clickhouse/backup.zip"

  cat > "${DEST}/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "backup_id": "${BACKUP_ID}",
  "postgres_database": "app",
  "clickhouse_database": "analytics",
  "backup_format_version": 1,
  "note": "PostgreSQL and ClickHouse in the same backup set are restored independently and are not assumed to be one transactionally consistent snapshot."
}
EOF

  (
    cd "${DEST}"
    sha256sum postgres.dump clickhouse/backup.zip > SHA256SUMS
  )

  echo "${BACKUP_ID}" > evidence/raw/latest-backup-id.txt
  ls -lh "${DEST}/postgres.dump" "${DEST}/clickhouse/backup.zip"
  echo "Backup created: ${DEST}"
}

cmd_retrieve() {
  BACKUP_ID="${1:-$(cat evidence/raw/latest-backup-id.txt)}"
  SRC="backups/${BACKUP_ID}"
  DST="retrieved/${BACKUP_ID}"
  [[ -d "${SRC}" ]] || { echo "Missing backup set ${SRC}" >&2; exit 1; }
  rm -rf "${DST}"
  mkdir -p retrieved
  cp -a "${SRC}" "${DST}"
  echo "${BACKUP_ID}" > evidence/raw/latest-retrieved-id.txt
  echo "Retrieved backup set into ${DST}"
  echo "NOTE: This local lab validates recovery after backup retrieval. Object-storage availability, credentials, and retrieval are separate failure modes and are not tested here."
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  destroy) cmd_destroy ;;
  seed) cmd_seed ;;
  verify-source) cmd_verify_source ;;
  backup) cmd_backup ;;
  retrieve) cmd_retrieve "${2:-}" ;;
  *)
    echo "Usage: $0 {up|down|destroy|seed|verify-source|backup|retrieve}" >&2
    exit 1
    ;;
esac
