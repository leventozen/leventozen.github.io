---
external: false
featured: true
title: "A Backup Is Not a Recovery Plan: Running a Restore Drill"
description: "Before real customer data arrives, walk a PostgreSQL and ClickHouse restore path: verify artifacts, measure recovery-point age, and clean up without touching the originals."
date: 2026-09-04
categories:
  - Reliability
tags:
  - postgres
  - clickhouse
  - backup
  - disaster-recovery
references:
  - title: "PostgreSQL: pg_restore"
    url: "https://www.postgresql.org/docs/current/app-pgrestore.html"
  - title: "ClickHouse: Backup and restore"
    url: "https://clickhouse.com/docs/concepts/features/backup-restore/overview"
  - title: "ClickHouse: Backup and restore using local or S3 disks"
    url: "https://clickhouse.com/docs/concepts/features/backup-restore/local-disk"
  - title: "ClickHouse: system.projection_parts"
    url: "https://clickhouse.com/docs/operations/system-tables/projection_parts"
---

I have been getting a project ready for production, and one of the things I kept coming back to was customer data.

Once real users arrive, losing that data is one of the worst things we can get wrong.

So I spent some time on retention, backups, where the copies live, how long we keep them, and how we know the latest one is healthy.

On paper, things looked good.

Grafana showed a fresh backup. The PostgreSQL dump existed. The ClickHouse backup was there too. Checksums were generated, and the scheduled job had completed without an error.

It would have been easy to stop there.

But before the system started holding real customer data, I wanted to answer a more uncomfortable question:

> What if I actually need to use one of these backups?

An incident is a bad time to discover that the restore command is wrong, the archive is unreadable, some important data is missing, or nobody actually knows how to verify the recovered state.

And right now I had something I would not have later: room to make mistakes.

The system was not serving real customers yet. The databases contained controlled synthetic data, and I could break the recovery procedure, understand why, and run it again without turning the exercise into an incident.

So I ran a restore drill before launch.

I restored PostgreSQL and ClickHouse under separate database names, verified what came back, measured the recovery point, and cleaned everything up afterwards.

I was not trying to simulate losing the whole machine.

I wanted to answer one question first:

> If this system went live tomorrow, could I actually get usable databases back from the backups we already produce?

## The restore path

The system has two databases:

* PostgreSQL for transactional data
* ClickHouse for analytics and event data

A scheduled job creates backup artifacts and sends them to backup storage.

The path I wanted to exercise was:

```text
                       ┌───────────────────┐
                       │     Databases     │
                       │                   │
                       │ PostgreSQL        │
                       │ ClickHouse        │
                       └─────────┬─────────┘
                                 │
                                 │ backup
                                 ▼
                       ┌───────────────────┐
                       │  Backup storage   │
                       │                   │
                       │ postgres.dump     │
                       │ clickhouse backup │
                       │ manifest          │
                       │ checksums         │
                       └─────────┬─────────┘
                                 │
                                 │ retrieve + verify
                                 ▼
             ┌──────────────────────────────────────┐
             │      Separate drill databases       │
             │                                      │
             │ app_restore_drill_<timestamp>        │
             │ analytics_restore_drill_<timestamp>  │
             └──────────────────┬───────────────────┘
                                │
                                │ verify
                                ▼
                       ┌───────────────────┐
                       │ schema            │
                       │ row counts        │
                       │ timestamps        │
                       │ read paths        │
                       └─────────┬─────────┘
                                 │
                                 │ guarded cleanup
                                 ▼
                              removed
```

One detail matters here.

Putting PostgreSQL and ClickHouse into the same backup set does not make them one transactionally consistent snapshot.

They can have different high-water marks.

For this drill, I restore and verify them independently.

## Why do this before real users?

Because this was probably the easiest time I would ever have to test it properly.

There was no customer data to put at risk, the databases were still small, and nobody was waiting for the system to come back.

If the procedure was wrong, I could fix it.

If a command behaved differently than I expected, I could investigate it.

If cleanup failed, I had time to understand why.

Waiting until the system is important enough to need recovery is exactly when experimenting with recovery becomes expensive.

I would rather find the boring mistakes now.

## Before touching anything

The drill databases would live beside the originals, so I still did not want the restore itself to make the environment unhealthy.

I checked disk and memory headroom:

```bash
df -h
free -h
```

and ran the drill while the host was quiet.

The rules were simple:

1. Never restore over the original databases.
2. Give every drill database an obvious name.
3. Use only backup artifacts whose origin is known.
4. Stop when a restore fails.
5. Keep failed state long enough to inspect it.
6. Verify before cleanup.
7. Validate the exact cleanup target.
8. Keep credentials out of logs.

There is also a PostgreSQL detail worth remembering.

A dump is not just a passive bag of rows. Restoring it can execute SQL contained in the archive.

So I want to know where the artifact came from and verify its integrity before restoring it.

## Step 1: Pick one backup set

I prefer one directory to represent one backup attempt:

```text
backups/
└── 2026-09-04T16:22:00Z/
    ├── postgres.dump
    ├── clickhouse/
    │   └── backup.zip
    ├── manifest.json
    └── SHA256SUMS
```

The manifest stays small:

```json
{
  "created_at": "2026-09-04T16:22:00Z",
  "postgres_database": "app",
  "clickhouse_database": "analytics",
  "backup_format_version": 1
}
```

I keep a format version because the recovery tooling will change over time.

A six-month-old backup is only useful if I still know how to interpret and restore it six months later.

Then I verify the artifacts:

```bash
sha256sum --check SHA256SUMS
```

The drill produced:

```text
postgres.dump: OK
clickhouse/backup.zip: OK
```

Good.

I have the files I expected.

That still does not tell me whether they can give me working databases back.

## Step 2: Make drill databases obvious

Every run gets a UTC ID:

```bash
DRILL_ID="$(date -u +%Y%m%dT%H%M%SZ)"

PG_DRILL_DB="app_restore_drill_${DRILL_ID}"
CH_DRILL_DB="analytics_restore_drill_${DRILL_ID}"
```

That gives me names like:

```text
app_restore_drill_20260904T162200Z
analytics_restore_drill_20260904T162200Z
```

I avoid names such as:

```text
app_test
analytics_old
temporary
```

Those names are obvious today and suspicious six months later.

The timestamped names are readable to a human and strict enough for cleanup code to validate.

## Step 3: Restore PostgreSQL

First I create a clean database from `template0`:

```bash
createdb \
  --username=postgres \
  --template=template0 \
  "$PG_DRILL_DB"
```

Then restore the dump:

```bash
pg_restore \
  --username=postgres \
  --dbname="$PG_DRILL_DB" \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  postgres.dump
```

### Why not `--create`?

The target already exists, and I want the destination to be completely obvious.

With `pg_restore --create`, the database name stored inside the archive becomes part of the restore flow.

That can be useful when I actually want to recreate the original database.

I do not want that here.

### Stop when something breaks

I also use `--exit-on-error`.

If the restore fails halfway through, I would rather have an obvious failed drill than a mostly restored database that quietly skipped a few objects.

```bash
if ! pg_restore \
  --username=postgres \
  --dbname="$PG_DRILL_DB" \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  postgres.dump
then
  echo "PostgreSQL restore failed; keeping drill database for inspection" >&2
  exit 1
fi
```

If it fails, I keep the target around until I understand why.

Deleting the failed restore immediately would also delete some of the best evidence for figuring out what happened.

### Ownership and privileges

The flags:

```text
--no-owner
--no-privileges
```

are intentional.

This drill is about rebuilding the database contents.

It does not prove that PostgreSQL roles, ownership, grants, and application permissions can also be reconstructed.

That is something I would test separately.

## Step 4: Restore ClickHouse

For ClickHouse, I use native `BACKUP` and `RESTORE`.

The database can be restored under another name:

```sql
RESTORE DATABASE analytics
AS analytics_restore_drill_20260904T162200Z
FROM Disk('backups', 'restore-drills/20260904T162200Z.zip');
```

`backups` is the name of a configured ClickHouse disk.

The exact path is environment-specific.

The thing I care about is that the archive passed to `RESTORE` is the exact artifact I selected and verified earlier.

If I verify:

```text
clickhouse/backup.zip
```

then the restore procedure needs to expose that exact file through the configured backup disk.

I do not want checksum verification and restore silently operating on two different files.

## Step 5: Verify what came back

For me, the restore is not finished when the database accepts the archive.

It is finished when I have enough evidence that the recovered data is actually usable.

### PostgreSQL

I start with the schema:

```bash
psql \
  --username=postgres \
  --dbname="$PG_DRILL_DB" \
  --tuples-only \
  --command="
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name;
  "
```

The expected list comes from outside the restored database, such as migration state or a version-controlled schema contract.

Otherwise, if a table disappears from the backup, I risk asking the broken restore what the correct schema is supposed to be.

Then I check things such as:

* critical table counts
* latest migration
* a known tenant record
* ingestion checkpoint
* minimum and maximum business timestamps

For example:

```bash
psql \
  --username=postgres \
  --dbname="$PG_DRILL_DB" \
  --tuples-only \
  --command="SELECT count(*) FROM shops;"
```

### ClickHouse

For ClickHouse, I inspect the restored objects:

```sql
SELECT
    name,
    engine,
    total_rows,
    total_bytes
FROM system.tables
WHERE database = 'analytics_restore_drill_20260904T162200Z'
ORDER BY name;
```

Then the data boundary:

```sql
SELECT
    min(timestamp),
    max(timestamp),
    count()
FROM analytics_restore_drill_20260904T162200Z.events;
```

The maximum timestamp is particularly useful.

A database can contain millions of perfectly valid rows and still be much further behind than expected.

I also run a few read-only, application-shaped queries:

* tenant lookup
* recent event query
* daily aggregate
* revenue or issue summary

These do not prove that the application itself can connect yet.

They tell me whether the recovered data can answer the questions the application depends on.

## Materialized views and projections

This system also uses a materialized view and a projection.

I wanted to know that those survived too.

### Materialized view

First I inspect the restored definition:

```sql
SELECT
    name,
    create_table_query
FROM system.tables
WHERE database = 'analytics_restore_drill_20260904T162200Z'
  AND engine = 'MaterializedView'
ORDER BY name;
```

Before writing anything, I check that the source and target both stay inside the drill environment.

Then I insert one synthetic event and verify that the transformed result appears exactly once.

That checks whether the view still processes new writes, not just whether its definition exists.

### Projection

This environment uses ClickHouse 24.8.

I inspect the restored table definition directly:

```sql
SHOW CREATE TABLE analytics_restore_drill_20260904T162200Z.events;
```

The projection should still be there.

Then I check that it has active parts:

```sql
SELECT
    table,
    name,
    sum(rows) AS rows
FROM system.projection_parts
WHERE database = 'analytics_restore_drill_20260904T162200Z'
  AND active
GROUP BY table, name
ORDER BY table, name;
```

A missing projection may not change the result of a query.

It can still make the recovered system much slower than expected.

## What the drill actually restored

The environment was using controlled synthetic data at this point.

That was intentional. I wanted enough data and database objects to exercise the recovery path without pretending I was doing a production-scale benchmark.

The backup contained:

```text
PostgreSQL
100 shops
5,000 users
250,000 orders

ClickHouse
2,000,000 events
1 materialized view
1 projection
```

The PostgreSQL dump was:

```text
2,427,547 bytes
```

and the ClickHouse backup:

```text
4,315,615 bytes
```

The restore results were:

```text
PostgreSQL restore: OK
Duration: 0.556s
```

and:

```text
1c6c1273-a6b8-4033-8c1f-184be3e41c5d    RESTORED
Duration: 0.186s
```

Those timings obviously do not tell me what production RTO will be.

The database is small and there are no real customers yet.

The useful part was that the procedure worked and the restored state matched the source.

| Check                   |                                Expected |                                Restored | Result |
| ----------------------- | --------------------------------------: | --------------------------------------: | ------ |
| PostgreSQL orders       |                                 250,000 |                                 250,000 | PASS   |
| Latest migration        | `20260401_create_ingestion_checkpoints` | `20260401_create_ingestion_checkpoints` | PASS   |
| ClickHouse events       |                               2,000,000 |                               2,000,000 | PASS   |
| Materialized view write |                                       1 |                                       1 | PASS   |
| Projection active       |                                     yes |                                     yes | PASS   |

All checks passed.

## Backup freshness is not the recovery point

This was one of the more useful things to make measurable.

The manifest tells me when the backup artifact was created.

During recovery, I care about something else too:

**How recent is the newest usable state inside it?**

So I look at high-water marks such as:

* latest business timestamp
* ingestion checkpoint
* event timestamp
* transaction-log position

Conceptually:

```text
recovery-point age
=
recovery time
-
latest usable state in restored data
```

In this drill, backup creation and recovery started within the same second:

```text
Backup artifact age:             0s
```

That number by itself is not very interesting.

The recovered high-water marks were:

```text
PostgreSQL max order timestamp:  2026-09-04 16:18:22+00
ClickHouse max event timestamp:  2026-09-04T16:18:28Z
```

which gave:

```text
Observed recovery-point age PG:  3m 38s
Observed recovery-point age CH:  3m 32s
PG/CH high-water delta:          6s
```

So the backup file was effectively brand new, while the newest recoverable data was already about three and a half minutes behind.

That is exactly why I do not want to use backup creation time as a proxy for the actual recovery point.

This also caught a bug in my first calculation.

PostgreSQL and ClickHouse timestamps were being interpreted in different timezones, which created a fake three-hour difference.

Normalizing both to UTC fixed it.

That is exactly the kind of mistake I wanted to find before real customer data existed.

## Measuring the whole path

The database restore commands themselves were fast:

```text
PostgreSQL restore:              0.556s
ClickHouse restore:              0.186s
```

Verification took longer:

```text
Verification:                    2.687s
Total to verified recovery:      3.429s
```

Again, this is **not a production RTO**.

It is a measurement from this pre-production drill with a small controlled dataset.

As the database grows, those numbers will change.

What matters now is that I have a procedure that can produce the measurement.

The boundary I eventually care about is closer to:

```text
we decide to recover
        ↓
the recovered system is verified
```

not simply how long `pg_restore` runs.

Retrieval, loading, indexes, verification, external dependencies, and human decisions all become part of real recovery time.

## Cleanup

Cleanup is the one place where I am happy to be unnecessarily suspicious.

The scripts use Bash, and I validate the complete generated name before dropping anything:

```bash
if [[ ! "$PG_DRILL_DB" =~ ^app_restore_drill_[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "Refusing unsafe PostgreSQL cleanup target" >&2
  exit 1
fi

if [[ ! "$CH_DRILL_DB" =~ ^analytics_restore_drill_[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "Refusing unsafe ClickHouse cleanup target" >&2
  exit 1
fi
```

I also check that:

* the database was created by the current run
* its timestamp matches the current drill ID
* the target is not `app`
* the target is not `analytics`
* verification evidence has already been saved

Then:

```bash
dropdb \
  --username=postgres \
  "$PG_DRILL_DB"
```

and:

```bash
clickhouse-client \
  --query="DROP DATABASE \`$CH_DRILL_DB\`"
```

After cleanup:

```text
PostgreSQL drill database:  NOT FOUND
ClickHouse drill database:  NOT FOUND
PostgreSQL app:             PRESENT
ClickHouse analytics:       PRESENT

Cleanup verification: PASS
```

That is the output I wanted before letting real customer data anywhere near the system.

## What did this actually tell me?

Before doing the drill, I knew backups were being created.

Afterwards, I knew that:

* the artifacts passed integrity checks
* PostgreSQL could rebuild the database from its dump
* ClickHouse could restore its native backup
* the important schema and data matched expectations
* the materialized view still processed writes
* the projection survived
* representative queries worked
* I could measure the actual recovered high-water marks
* cleanup removed the drill databases and left the originals alone

That is a much better place to be before launch than simply having a green backup dashboard.

## What I still have not tested

There are much bigger recovery problems left:

* losing the database machine completely
* rebuilding PostgreSQL and ClickHouse configuration
* restoring PostgreSQL roles and permissions
* application cutover
* point-in-time recovery and WAL replay
* cross-database consistency
* secrets, queues, files, DNS, and other dependencies
* production load on the recovered system
* losing access to backup storage
* running the procedure under time pressure with somebody else driving

I am not trying to turn the first drill into all of those things.

The next useful step is restoring onto another machine.

After that, I want to start the application against the recovered databases and exercise the actual application path.

Eventually, somebody who did not write the procedure should be able to run it too.

## The newest backup is not the only one worth testing

The latest backup is the obvious one to test regularly.

But if silent corruption started ten days ago, the latest backups may all contain the same bad state.

So I would rather have two simple lanes:

```text
Frequent drill:
    restore the newest complete backup

Periodic drill:
    restore a rotating older backup
```

That also tests whether older backup formats and recovery tooling still work as the system evolves.

The `backup_format_version` in the manifest becomes useful here.

## Object storage is part of recovery too

This drill assumes the backup artifact is available.

That assumption can fail too.

The provider, account, region, credentials, or network path may be unavailable during the incident.

I do not think that automatically means every small system needs a complicated multi-cloud backup architecture.

But I do want to understand which failures can take both the primary data and its recovery path away at the same time.

If I eventually keep another copy somewhere else, that copy is only useful once I have tested retrieving it too.

## What I would automate

Now that I have walked through the path once, most of it is boring enough to automate.

A scheduled drill could:

1. select a backup
2. reject incomplete or unexpectedly old sets
3. retrieve it
4. verify checksums
5. check resource headroom
6. generate drill-only database names
7. restore PostgreSQL and ClickHouse
8. stop and preserve evidence on failure
9. run schema, data, and read-path checks
10. record high-water marks and timings
11. remove only databases created by the drill
12. publish the result

The dashboard I started with told me that backups existed.

Before real customers arrive, I would rather know both:

```text
Age of newest backup
Last successful restore drill
Recovered high-water mark
Observed recovery-point age
PostgreSQL restore result
ClickHouse restore result
Verification result
Cleanup result
```

A backup that exists and a recovery path that has actually been tested are different things.

## Final thoughts

Nothing dramatic happened during the drill.

That was almost disappointing.

But that was also the best possible time to do it.

There was no customer waiting for data to come back, no incident channel, and no pressure to type the right command on the first attempt.

I could break the procedure, fix it, run it again, and understand what each step was actually proving.

Before the drill, I knew the system was producing backups.

Afterwards, I knew I could take those backups and get usable databases back from them.

That is something I wanted to know before calling the system production-ready.
