# Restore drill evidence

## Scope

This local lab validates the recovery procedure after backup retrieval.
Object-storage availability, credentials, and retrieval are separate failure modes and are not tested here.

PostgreSQL and ClickHouse being part of the same backup set does not automatically mean they represent one transactionally consistent point in time.

PostgreSQL restore used `--no-owner --no-privileges`. This drill does **not** prove recovery of roles, ownership, GRANT/REVOKE, or application DB permissions.

All recovery-point calculations normalize timestamps to UTC. Naive ClickHouse `DateTime` strings are treated as UTC, not local time.

## Environment

- Project: `restore-drill` (Docker Compose)
- PostgreSQL: `postgres (PostgreSQL) 16.15`
- ClickHouse: `ClickHouse client version 24.8.14.39 (official build).`
- Architecture: local Docker Desktop (darwin)

## Backup

- Backup ID: `20260904T162200Z`
- Retrieved path: `retrieved/20260904T162200Z/`
- `postgres.dump` size: 2427547 bytes
- `clickhouse/backup.zip` size: 4315615 bytes
- Manifest:

```json
{
  "created_at": "2026-09-04T16:22:00Z",
  "backup_id": "20260904T162200Z",
  "postgres_database": "app",
  "clickhouse_database": "analytics",
  "backup_format_version": 1,
  "note": "PostgreSQL and ClickHouse in the same backup set are restored independently and are not assumed to be one transactionally consistent snapshot."
}
```

### Checksum output

```text
postgres.dump: OK
clickhouse/backup.zip: OK
```

## PostgreSQL restore

- Target: `app_restore_drill_20260904T162200Z`
- Result: OK
- Duration: 0.556s (556ms)

## ClickHouse restore

- Target: `analytics_restore_drill_20260904T162200Z`
- Result/status:

```text
1c6c1273-a6b8-4033-8c1f-184be3e41c5d	RESTORED
```

- Duration: 0.186s (186ms)

## Verification

| Check | Expected | Restored | Result |
| --- | --- | --- | --- |
| PostgreSQL tables | `['ingestion_checkpoints', 'orders', 'schema_migrations', 'shops', 'users']` | `['ingestion_checkpoints', 'orders', 'schema_migrations', 'shops', 'users']` | PASS |
| PostgreSQL shops | `100` | `100` | PASS |
| PostgreSQL users | `5000` | `5000` | PASS |
| PostgreSQL orders | `250000` | `250000` | PASS |
| Latest migration | `20260401_create_ingestion_checkpoints` | `20260401_create_ingestion_checkpoints` | PASS |
| Known shop | `{'id': 1, 'name': 'Shop 1', 'plan': 'enterprise'}` | `{'id': 1, 'name': 'Shop 1', 'plan': 'enterprise'}` | PASS |
| Min order timestamp | `2026-08-28 16:18:22+00` | `2026-08-28 16:18:22+00` | PASS |
| Max order timestamp | `2026-09-04 16:18:22+00` | `2026-09-04 16:18:22+00` | PASS |
| Ingestion checkpoint | `offset:250000` | `offset:250000` | PASS |
| ClickHouse tables | `['events', 'events_daily', 'events_daily_mv']` | `['events', 'events_daily', 'events_daily_mv']` | PASS |
| ClickHouse event count | `2000000` | `2000000` | PASS |
| ClickHouse min timestamp | `2026-08-28T16:18:25Z` | `2026-08-28T16:18:25Z` | PASS |
| ClickHouse max timestamp | `2026-09-04T16:18:28Z` | `2026-09-04T16:18:28Z` | PASS |
| Daily aggregate top | `{"day":"2026-09-04","shop_id":1,"sum(event_count)":"1941","sum(amount)":1941}` | `{"day":"2026-09-04","shop_id":1,"sum(event_count)":"1941","sum(amount)":1941}` | PASS |
| Projection exists | `PROJECTION events_by_type` | `present` | PASS |
| Projection active parts rows | `>0` | `300` | PASS |
| Materialized view synthetic write | `1` | `1` | PASS |
| Application-shaped PG tenant lookup | `Shop 1` | `Shop 1` | PASS |
| Application-shaped PG tenant paid orders | `>0` | `2500` | PASS |

## Recovery measurements

```text
Backup artifact age:                  0s
PostgreSQL recovered high-water (UTC): 2026-09-04 16:18:22+00
ClickHouse recovered high-water (UTC): 2026-09-04T16:18:28Z
Observed recovery-point age (PG):      3m 38s (218s)
Observed recovery-point age (CH):      3m 32s (212s)
PG/CH RPO delta (abs):                 6s

PostgreSQL restore duration:           0.556s
ClickHouse restore duration:           0.186s
Verification duration:                 2.687s
Total time to verified recovery:       3.429s
Full drill including cleanup:          4.103s
```

## Cleanup

```text
PostgreSQL drill database:  NOT FOUND
ClickHouse drill database:  NOT FOUND
PostgreSQL app:             PRESENT
ClickHouse analytics:       PRESENT

Cleanup verification: PASS
```

## What this local lab does NOT prove

- object-storage retrieval during an incident
- object-storage provider/account availability
- host-loss recovery / machine isolation
- PostgreSQL roles/permissions recovery
- cross-database transactional consistency
- application cutover
- point-in-time recovery / WAL replay
- external dependency recovery
- production load readiness
