# Restore drill lab

Local Docker Compose lab that validates the **recovery procedure after backup retrieval**.

This does **not** test object-storage availability, credentials, provider failure, or retrieval.

```text
already-retrieved backup set
        |
        v
manifest + checksums
        |
        v
PostgreSQL restore
ClickHouse restore
        |
        v
schema/data/read-path verification
        |
        v
timing + recovery-point measurement
        |
        v
guarded cleanup
```

## Disk-conscious defaults

- Reuses local images: `postgres:16-alpine`, `clickhouse/clickhouse-server:24.8-alpine`
- Final dataset: 100 shops, 5k users, 250k orders, 2M events (still laptop-friendly)
- Seed timestamps are relative to now (newest ~2 minutes before backup)
- All RPO math normalizes to UTC
- Durations measured in milliseconds
- No MinIO / S3 / LocalStack
- Evidence archives live under `evidence/runs/` (`run-001-small`, `run-002-final`)

## Commands

```bash
cd labs/restore-drill
make up
make seed
make verify-source
make backup
make retrieve
make drill
make destroy   # project-scoped only
```

## Notes

- PostgreSQL restore uses `--no-owner --no-privileges` and therefore does not prove roles/permissions recovery.
- PostgreSQL + ClickHouse in one backup set are restored independently; not assumed transactionally consistent.
- Cleanup only deletes `app_restore_drill_*` / `analytics_restore_drill_*` names created by the current run.
