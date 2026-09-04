# Article-ready snippets from successful run

## 1. Checksum verification

```text
postgres.dump: OK
clickhouse/backup.zip: OK
```

## 2. PostgreSQL restore result + duration

```text
PostgreSQL restore: OK
Duration: <1s
```

## 3. ClickHouse restore result + duration/status

```text
07ce65ff-0dec-444b-ba03-02b746e285d2	RESTORED
Duration: <1s
```

## 4. PostgreSQL verification summary

See selected PASS rows in latest-run.md for shops/orders/migration/known shop.

## 5. ClickHouse verification summary

Event count, timestamps, aggregate, projection, and materialized-view synthetic write all PASS in this run.

## 6. Recovered high-water marks

```text
PostgreSQL max order timestamp: 2026-03-07 22:39:00+00
ClickHouse max event timestamp: 2026-03-07 22:39:00
```

## 7. Recovery timing summary

```text
Backup artifact age:             45s
Observed recovery-point age PG:  15615058s (~181 days)
Observed recovery-point age CH:  15625858s (~181 days)
PostgreSQL restore:              <1s
ClickHouse restore:              <1s
Verification:                    3s
Total to verified recovery:      3s
```

Artifact age ≠ recovery-point age: the backup file was 45s old; the recovered data high-water mark was from 2026-03-07.

## 8. Cleanup proof

```text
=== PostgreSQL databases after cleanup ===
app
=== ClickHouse databases after cleanup ===
analytics
```
