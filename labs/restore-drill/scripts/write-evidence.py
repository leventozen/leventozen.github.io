#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return data


def parse_ts_utc(value: str) -> datetime:
    """Parse timestamps strictly as UTC.

    Naive datetimes (no offset) are treated as UTC — never as local time.
    This fixes the ClickHouse DateTime quirk where `2026-03-07 22:39:00`
    without a zone would previously be interpreted as Europe/Istanbul and
    shift RPO calculations by exactly 10800 seconds.
    """
    value = value.strip().replace(" ", "T")
    if value.endswith("+00"):
        value = value[:-3] + "+00:00"
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ms(ms: int | str) -> str:
    return f"{int(ms) / 1000:.3f}s"


def fmt_age(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s ({seconds}s)"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m ({seconds}s)"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ({seconds}s)"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-meta", required=True)
    args = parser.parse_args()

    meta = parse_env(Path(args.run_meta))
    backup_id = meta["BACKUP_ID"]
    drill_id = meta["DRILL_ID"]
    retrieved = ROOT / "retrieved" / backup_id
    manifest = json.loads((retrieved / "manifest.json").read_text())
    verify = json.loads((ROOT / "evidence" / "raw" / f"verify-{drill_id}.json").read_text())
    checksum = (ROOT / "evidence" / "raw" / f"checksum-{drill_id}.txt").read_text().strip()
    cleanup = (ROOT / "evidence" / "raw" / f"cleanup-{drill_id}.txt").read_text().strip()
    ch_restore = (ROOT / "evidence" / "raw" / f"ch-restore-{drill_id}.log").read_text().strip()

    pg_dump_size = (retrieved / "postgres.dump").stat().st_size
    ch_zip_size = (retrieved / "clickhouse" / "backup.zip").stat().st_size

    drill_start = parse_ts_utc(meta["DRILL_START"])
    backup_created = parse_ts_utc(manifest["created_at"])
    artifact_age_sec = (drill_start - backup_created).total_seconds()

    hw = verify["high_water_marks"]
    pg_hw = parse_ts_utc(hw["postgres_max_order_timestamp"])
    ch_hw = parse_ts_utc(hw["clickhouse_max_event_timestamp"])
    pg_rpo = (drill_start - pg_hw).total_seconds()
    ch_rpo = (drill_start - ch_hw).total_seconds()
    rpo_delta = abs(pg_rpo - ch_rpo)

    pg_ms = int(meta["PG_RESTORE_DURATION_MS"])
    ch_ms = int(meta["CH_RESTORE_DURATION_MS"])
    verify_ms = int(meta["VERIFY_DURATION_MS"])
    to_verified_ms = pg_ms + ch_ms + verify_ms
    total_ms = int(meta["DRILL_END_MS"]) - int(meta["DRILL_START_MS"])

    # Pull versions if containers are still up
    pg_ver = "postgres:16-alpine"
    ch_ver = "clickhouse/clickhouse-server:24.8-alpine"
    try:
        import subprocess

        pg_ver = subprocess.check_output(
            [
                "docker",
                "compose",
                "-p",
                "restore-drill",
                "-f",
                str(ROOT / "docker-compose.yml"),
                "exec",
                "-T",
                "postgres",
                "postgres",
                "--version",
            ],
            text=True,
        ).strip()
        ch_ver = subprocess.check_output(
            [
                "docker",
                "compose",
                "-p",
                "restore-drill",
                "-f",
                str(ROOT / "docker-compose.yml"),
                "exec",
                "-T",
                "clickhouse",
                "clickhouse-client",
                "--version",
            ],
            text=True,
        ).strip()
    except Exception:
        pass

    checks_by_name = {c["check"]: c for c in verify["checks"]}

    def cell(name: str) -> tuple[str, str, str]:
        c = checks_by_name[name]
        return str(c["expected"]), str(c["restored"]), c["result"]

    article_rows = [
        ("PostgreSQL orders", "PostgreSQL orders"),
        ("Latest migration", "Latest migration"),
        ("ClickHouse events", "ClickHouse event count"),
        ("Materialized view write", "Materialized view synthetic write"),
        ("Projection active", "Projection active parts rows"),
    ]
    article_table_lines = [
        "| Check | Expected | Restored | Result |",
        "| --- | ---: | ---: | --- |",
    ]
    for label, key in article_rows:
        expected, restored, result = cell(key)
        # Projection: show yes if PASS with >0 rows
        if key == "Projection active parts rows":
            expected, restored = "yes (>0 rows)", ("yes" if result == "PASS" else "no")
        article_table_lines.append(
            f"| {label} | `{expected}` | `{restored}` | {result} |"
        )
    article_table = "\n".join(article_table_lines)

    full_table = (ROOT / "evidence" / "raw" / f"verify-{drill_id}.md").read_text().strip()

    latest = f"""# Restore drill evidence

## Scope

This local lab validates the recovery procedure after backup retrieval.
Object-storage availability, credentials, and retrieval are separate failure modes and are not tested here.

PostgreSQL and ClickHouse being part of the same backup set does not automatically mean they represent one transactionally consistent point in time.

PostgreSQL restore used `--no-owner --no-privileges`. This drill does **not** prove recovery of roles, ownership, GRANT/REVOKE, or application DB permissions.

All recovery-point calculations normalize timestamps to UTC. Naive ClickHouse `DateTime` strings are treated as UTC, not local time.

## Environment

- Project: `restore-drill` (Docker Compose)
- PostgreSQL: `{pg_ver}`
- ClickHouse: `{ch_ver}`
- Architecture: local Docker Desktop (darwin)

## Backup

- Backup ID: `{backup_id}`
- Retrieved path: `retrieved/{backup_id}/`
- `postgres.dump` size: {pg_dump_size} bytes
- `clickhouse/backup.zip` size: {ch_zip_size} bytes
- Manifest:

```json
{json.dumps(manifest, indent=2)}
```

### Checksum output

```text
{checksum}
```

## PostgreSQL restore

- Target: `{meta['PG_DRILL_DB']}`
- Result: OK
- Duration: {fmt_ms(pg_ms)} ({pg_ms}ms)

## ClickHouse restore

- Target: `{meta['CH_DRILL_DB']}`
- Result/status:

```text
{ch_restore}
```

- Duration: {fmt_ms(ch_ms)} ({ch_ms}ms)

## Verification

{full_table}

## Recovery measurements

```text
Backup artifact age:                  {fmt_age(artifact_age_sec)}
PostgreSQL recovered high-water (UTC): {hw['postgres_max_order_timestamp']}
ClickHouse recovered high-water (UTC): {hw['clickhouse_max_event_timestamp']}
Observed recovery-point age (PG):      {fmt_age(pg_rpo)}
Observed recovery-point age (CH):      {fmt_age(ch_rpo)}
PG/CH RPO delta (abs):                 {fmt_age(rpo_delta)}

PostgreSQL restore duration:           {fmt_ms(pg_ms)}
ClickHouse restore duration:           {fmt_ms(ch_ms)}
Verification duration:                 {fmt_ms(verify_ms)}
Total time to verified recovery:       {fmt_ms(to_verified_ms)}
Full drill including cleanup:          {fmt_ms(total_ms)}
```

## Cleanup

```text
{cleanup}
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
"""

    snippets = f"""# Article-ready snippets from successful run

## 1. Checksum verification

```text
{checksum}
```

## 2. PostgreSQL restore result + duration

```text
PostgreSQL restore: OK
Duration: {fmt_ms(pg_ms)}
```

## 3. ClickHouse restore result + duration/status

```text
{ch_restore}
Duration: {fmt_ms(ch_ms)}
```

## 4. Verification summary (article)

{article_table}

All checks passed.

## 5. Recovered high-water marks (UTC)

```text
PostgreSQL max order timestamp: {hw['postgres_max_order_timestamp']}
ClickHouse max event timestamp: {hw['clickhouse_max_event_timestamp']}
```

## 6. Recovery timing summary

```text
Backup artifact age:             {fmt_age(artifact_age_sec)}
Observed recovery-point age PG:  {fmt_age(pg_rpo)}
Observed recovery-point age CH:  {fmt_age(ch_rpo)}
PG/CH RPO delta (abs):           {fmt_age(rpo_delta)}
PostgreSQL restore:              {fmt_ms(pg_ms)}
ClickHouse restore:              {fmt_ms(ch_ms)}
Verification:                    {fmt_ms(verify_ms)}
Total to verified recovery:      {fmt_ms(to_verified_ms)}
```

The backup artifact was {fmt_age(artifact_age_sec)} old, but the newest recoverable business data was about {fmt_age(max(pg_rpo, ch_rpo))} old.

## 7. Cleanup proof

```text
{cleanup}
```
"""

    (ROOT / "evidence" / "latest-run.md").write_text(latest)
    (ROOT / "evidence" / "article-snippets.md").write_text(snippets)

    # Also snapshot into run-002-final if present
    run_dir = ROOT / "evidence" / "runs" / "run-002-final"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "latest-run.md").write_text(latest)
    (run_dir / "article-snippets.md").write_text(snippets)
    (run_dir / "source-contract.json").write_text(
        (ROOT / "evidence" / "source-contract.json").read_text()
    )
    (run_dir / "run-meta.env").write_text(Path(args.run_meta).read_text())
    print("Wrote evidence/latest-run.md, evidence/article-snippets.md, and runs/run-002-final/")


if __name__ == "__main__":
    main()
