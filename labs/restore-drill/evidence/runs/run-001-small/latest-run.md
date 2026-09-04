# Restore drill evidence

## Scope

This local lab validates the recovery procedure after backup retrieval.
Object-storage availability, credentials, and retrieval are separate failure modes and are not tested here.

PostgreSQL and ClickHouse being part of the same backup set does not automatically mean they represent one transactionally consistent point in time.

PostgreSQL restore used `--no-owner --no-privileges`. This drill does **not** prove recovery of roles, ownership, GRANT/REVOKE, or application DB permissions.

## Environment

- Project: `restore-drill` (Docker Compose)
- PostgreSQL: `postgres (PostgreSQL) 16.15`
- ClickHouse: `ClickHouse client version 24.8.14.39 (official build)`
- Architecture: local Docker Desktop (darwin)
- Disk: reused local images only (`--pull never`); lab volumes destroyed after success (~9.8Gi free)

## Backup

- Backup ID: `20260904T160913Z`
- Retrieved path: `retrieved/20260904T160913Z/`
- `postgres.dump` size: 98553 bytes
- `clickhouse/backup.zip` size: 78244 bytes
- Manifest:

```json
{
  "created_at": "2026-09-04T16:09:13Z",
  "backup_id": "20260904T160913Z",
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

- Target: `app_restore_drill_20260904T160958Z`
- Result: OK
- Duration: <1s (timer resolution: 0s)

## ClickHouse restore

- Target: `analytics_restore_drill_20260904T160958Z`
- Result/status:

```text
07ce65ff-0dec-444b-ba03-02b746e285d2	RESTORED
```

- Duration: <1s (timer resolution: 0s)

## Verification

| Check | Expected | Restored | Result |
| --- | --- | --- | --- |
| PostgreSQL tables | `['ingestion_checkpoints', 'orders', 'schema_migrations', 'shops', 'users']` | `['ingestion_checkpoints', 'orders', 'schema_migrations', 'shops', 'users']` | PASS |
| PostgreSQL shops | `50` | `50` | PASS |
| PostgreSQL users | `500` | `500` | PASS |
| PostgreSQL orders | `10000` | `10000` | PASS |
| Latest migration | `20260401_create_ingestion_checkpoints` | `20260401_create_ingestion_checkpoints` | PASS |
| Known shop | `{'id': 1, 'name': 'Shop 1', 'plan': 'enterprise'}` | `{'id': 1, 'name': 'Shop 1', 'plan': 'enterprise'}` | PASS |
| Min order timestamp | `2026-03-01 00:00:00+00` | `2026-03-01 00:00:00+00` | PASS |
| Max order timestamp | `2026-03-07 22:39:00+00` | `2026-03-07 22:39:00+00` | PASS |
| Ingestion checkpoint | `offset:10000` | `offset:10000` | PASS |
| ClickHouse tables | `['events', 'events_daily', 'events_daily_mv']` | `['events', 'events_daily', 'events_daily_mv']` | PASS |
| ClickHouse event count | `80000` | `80000` | PASS |
| ClickHouse min timestamp | `2026-03-01 00:00:00` | `2026-03-01 00:00:00` | PASS |
| ClickHouse max timestamp | `2026-03-07 22:39:00` | `2026-03-07 22:39:00` | PASS |
| Daily aggregate top | `{"day":"2026-03-07","shop_id":1,"sum(event_count)":"216","sum(amount)":5816}` | `{"day":"2026-03-07","shop_id":1,"sum(event_count)":"216","sum(amount)":5816}` | PASS |
| Projection exists | `PROJECTION events_by_type` | `present` | PASS |
| Projection active parts rows | `>0` | `150` | PASS |
| Materialized view synthetic write | `1` | `1` | PASS |
| Application-shaped PG tenant lookup | `Shop 1` | `Shop 1` | PASS |
| Application-shaped PG tenant paid orders | `>0` | `200` | PASS |

## Recovery measurements

```text
Backup artifact age:                 45s
PostgreSQL recovered high-water mark: 2026-03-07 22:39:00+00
ClickHouse recovered high-water mark: 2026-03-07 22:39:00
Observed recovery-point age (PG):     15615058s
Observed recovery-point age (CH):     15625858s

PostgreSQL restore duration:          <1s
ClickHouse restore duration:          <1s
Verification duration:                3s
Total time to verified recovery:      3s
Full drill including cleanup:         3s

Note: recovery-point ages are large because seeded data ends 2026-03-07 while the drill ran 2026-09-04. Artifact age (45s) is the age of the backup file relative to drill start; RPO age is the age of the recovered high-water mark.
```

## Cleanup

```text
=== PostgreSQL databases after cleanup ===
app
=== ClickHouse databases after cleanup ===
analytics
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
