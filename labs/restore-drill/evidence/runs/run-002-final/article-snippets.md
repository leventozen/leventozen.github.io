# Article-ready snippets from successful run

## 1. Checksum verification

```text
postgres.dump: OK
clickhouse/backup.zip: OK
```

## 2. PostgreSQL restore result + duration

```text
PostgreSQL restore: OK
Duration: 0.556s
```

## 3. ClickHouse restore result + duration/status

```text
1c6c1273-a6b8-4033-8c1f-184be3e41c5d	RESTORED
Duration: 0.186s
```

## 4. Verification summary (article)

| Check | Expected | Restored | Result |
| --- | ---: | ---: | --- |
| PostgreSQL orders | `250000` | `250000` | PASS |
| Latest migration | `20260401_create_ingestion_checkpoints` | `20260401_create_ingestion_checkpoints` | PASS |
| ClickHouse events | `2000000` | `2000000` | PASS |
| Materialized view write | `1` | `1` | PASS |
| Projection active | `yes (>0 rows)` | `yes` | PASS |

All checks passed.

## 5. Recovered high-water marks (UTC)

```text
PostgreSQL max order timestamp: 2026-09-04 16:18:22+00
ClickHouse max event timestamp: 2026-09-04T16:18:28Z
```

## 6. Recovery timing summary

```text
Backup artifact age:             0s
Observed recovery-point age PG:  3m 38s (218s)
Observed recovery-point age CH:  3m 32s (212s)
PG/CH RPO delta (abs):           6s
PostgreSQL restore:              0.556s
ClickHouse restore:              0.186s
Verification:                    2.687s
Total to verified recovery:      3.429s
```

The backup artifact was 0s old, but the newest recoverable business data was about 3m 38s (218s) old.

## 7. Cleanup proof

```text
PostgreSQL drill database:  NOT FOUND
ClickHouse drill database:  NOT FOUND
PostgreSQL app:             PRESENT
ClickHouse analytics:       PRESENT

Cleanup verification: PASS
```
