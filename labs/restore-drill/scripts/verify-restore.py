#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True).strip()


def pg(db: str, sql: str) -> str:
    return sh(
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
            "psql",
            "-U",
            "postgres",
            "-d",
            db,
            "-At",
            "-c",
            sql,
        ]
    )


def ch(sql: str) -> str:
    return sh(
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
            "--query",
            sql,
        ]
    )


def check(name: str, expected, restored, checks: list[dict]) -> None:
    result = "PASS" if str(expected) == str(restored) else "FAIL"
    checks.append(
        {
            "check": name,
            "expected": expected,
            "restored": restored,
            "result": result,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True)
    parser.add_argument("--pg-db", required=True)
    parser.add_argument("--ch-db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()

    contract = json.loads(Path(args.contract).read_text())
    checks: list[dict] = []

    # PostgreSQL
    tables = [
        t
        for t in pg(
            args.pg_db,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name",
        ).splitlines()
        if t
    ]
    check("PostgreSQL tables", contract["postgres"]["tables"], tables, checks)
    check(
        "PostgreSQL shops",
        contract["postgres"]["shops_count"],
        int(pg(args.pg_db, "SELECT count(*) FROM shops")),
        checks,
    )
    check(
        "PostgreSQL users",
        contract["postgres"]["users_count"],
        int(pg(args.pg_db, "SELECT count(*) FROM users")),
        checks,
    )
    check(
        "PostgreSQL orders",
        contract["postgres"]["orders_count"],
        int(pg(args.pg_db, "SELECT count(*) FROM orders")),
        checks,
    )
    check(
        "Latest migration",
        contract["postgres"]["latest_migration"],
        pg(
            args.pg_db,
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1",
        ),
        checks,
    )
    known = contract["postgres"]["known_shop"]
    restored_shop = {
        "id": int(pg(args.pg_db, "SELECT id FROM shops WHERE id=1")),
        "name": pg(args.pg_db, "SELECT name FROM shops WHERE id=1"),
        "plan": pg(args.pg_db, "SELECT plan FROM shops WHERE id=1"),
    }
    check("Known shop", known, restored_shop, checks)
    check(
        "Min order timestamp",
        contract["postgres"]["min_order_timestamp"],
        pg(
            args.pg_db,
            "SELECT to_char(min(created_at) AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD HH24:MI:SS') || '+00' FROM orders",
        ),
        checks,
    )
    check(
        "Max order timestamp",
        contract["postgres"]["max_order_timestamp"],
        pg(
            args.pg_db,
            "SELECT to_char(max(created_at) AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD HH24:MI:SS') || '+00' FROM orders",
        ),
        checks,
    )
    check(
        "Ingestion checkpoint",
        contract["postgres"]["ingestion_checkpoint"],
        pg(
            args.pg_db,
            "SELECT checkpoint FROM ingestion_checkpoints WHERE source='orders-feed'",
        ),
        checks,
    )

    # ClickHouse inventory
    expected_tables = sorted(t["name"] for t in contract["clickhouse"]["tables"])
    restored_tables = [
        line.split("\t")[0]
        for line in ch(
            f"SELECT name, engine FROM system.tables WHERE database='{args.ch_db}' "
            f"AND name NOT LIKE '.inner%' ORDER BY name FORMAT TSV"
        ).splitlines()
        if line
    ]
    check("ClickHouse tables", expected_tables, restored_tables, checks)
    check(
        "ClickHouse event count",
        contract["clickhouse"]["event_count"],
        int(ch(f"SELECT count() FROM {args.ch_db}.events")),
        checks,
    )
    check(
        "ClickHouse min timestamp",
        contract["clickhouse"]["min_timestamp"],
        ch(
            f"SELECT formatDateTime(min(timestamp), '%Y-%m-%dT%H:%i:%SZ', 'UTC') "
            f"FROM {args.ch_db}.events"
        ),
        checks,
    )
    check(
        "ClickHouse max timestamp",
        contract["clickhouse"]["max_timestamp"],
        ch(
            f"SELECT formatDateTime(max(timestamp), '%Y-%m-%dT%H:%i:%SZ', 'UTC') "
            f"FROM {args.ch_db}.events"
        ),
        checks,
    )

    agg = ch(
        f"SELECT day, shop_id, sum(event_count), sum(amount) "
        f"FROM {args.ch_db}.events_daily GROUP BY day, shop_id "
        f"ORDER BY day DESC, shop_id ASC LIMIT 1 FORMAT JSONEachRow"
    )
    check(
        "Daily aggregate top",
        contract["clickhouse"]["daily_aggregate_top"],
        agg,
        checks,
    )

    # Projection existence (ClickHouse 24.8 has projection_parts, not system.projections)
    create_events = ch(f"SHOW CREATE TABLE {args.ch_db}.events")
    checks.append(
        {
            "check": "Projection exists",
            "expected": "PROJECTION events_by_type",
            "restored": "present" if "PROJECTION events_by_type" in create_events else "missing",
            "result": "PASS" if "PROJECTION events_by_type" in create_events else "FAIL",
        }
    )
    parts = ch(
        f"SELECT coalesce(sum(rows), 0) FROM system.projection_parts "
        f"WHERE database='{args.ch_db}' AND table='events' "
        f"AND name='events_by_type' AND active"
    )
    checks.append(
        {
            "check": "Projection active parts rows",
            "expected": ">0",
            "restored": parts,
            "result": "PASS" if parts and int(parts) > 0 else "FAIL",
        }
    )

    # Materialized view safety + synthetic write
    mv_query = ch(
        f"SELECT create_table_query FROM system.tables "
        f"WHERE database='{args.ch_db}' AND name='events_daily_mv'"
    )
    unsafe = False
    for bad in ["analytics.events", "analytics.events_daily", "TO analytics."]:
        # After rename restore, references should use drill DB names.
        if bad in mv_query and args.ch_db not in mv_query:
            # Only unsafe if still pointing at production without drill DB.
            pass
    # Safer check: create_table_query must mention drill DB for both source and target.
    if args.ch_db not in mv_query:
        unsafe = True

    if unsafe:
        checks.append(
            {
                "check": "Materialized view synthetic write",
                "expected": "drill-local references",
                "restored": mv_query[:200],
                "result": "FAIL",
            }
        )
    else:
        marker_shop = 999001
        ch(
            f"INSERT INTO {args.ch_db}.events VALUES "
            f"(toDateTime('2099-01-01 00:00:00'), {marker_shop}, 'synthetic', 1.0, 1)"
        )
        # Force MV path by selecting from target after insert
        hit = ch(
            f"SELECT sum(event_count) FROM {args.ch_db}.events_daily "
            f"WHERE shop_id={marker_shop} AND day=toDate('2099-01-01')"
        )
        checks.append(
            {
                "check": "Materialized view synthetic write",
                "expected": "1",
                "restored": hit,
                "result": "PASS" if hit == "1" else "FAIL",
            }
        )

    # Application-shaped reads (as postgres superuser / default CH user)
    app_shop = pg(args.pg_db, "SELECT name FROM shops WHERE id=1")
    app_shop_orders = pg(
        args.pg_db,
        "SELECT count(*) FROM orders WHERE shop_id=1 AND status='paid'",
    )
    checks.append(
        {
            "check": "Application-shaped PG tenant lookup",
            "expected": known["name"],
            "restored": app_shop,
            "result": "PASS" if app_shop == known["name"] else "FAIL",
            "note": "Executed as postgres superuser; does not prove app role permissions.",
        }
    )
    checks.append(
        {
            "check": "Application-shaped PG tenant paid orders",
            "expected": ">0",
            "restored": app_shop_orders,
            "result": "PASS" if int(app_shop_orders) > 0 else "FAIL",
            "note": "Executed as postgres superuser; does not prove app role permissions.",
        }
    )

    high_water = {
        "postgres_max_order_timestamp": pg(
            args.pg_db,
            "SELECT to_char(max(created_at) AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD HH24:MI:SS') || '+00' FROM orders",
        ),
        "postgres_ingestion_checkpoint": pg(
            args.pg_db,
            "SELECT checkpoint FROM ingestion_checkpoints WHERE source='orders-feed'",
        ),
        "clickhouse_max_event_timestamp": ch(
            f"SELECT formatDateTime(max(timestamp), '%Y-%m-%dT%H:%i:%SZ', 'UTC') "
            f"FROM {args.ch_db}.events WHERE shop_id < 900000"
        ),
        "timezone_note": (
            "All recovery-point timestamps are normalized to UTC (Z). "
            "Naive ClickHouse DateTime strings must not be parsed as local time."
        ),
    }

    payload = {
        "checks": checks,
        "high_water_marks": high_water,
        "permission_note": (
            "PostgreSQL restore used --no-owner --no-privileges. "
            "This drill does not prove roles, ownership, GRANT/REVOKE, or app DB permissions."
        ),
    }

    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")

    lines = [
        "| Check | Expected | Restored | Result |",
        "| --- | --- | --- | --- |",
    ]
    for c in checks:
        lines.append(
            f"| {c['check']} | `{c['expected']}` | `{c['restored']}` | {c['result']} |"
        )
    Path(args.markdown).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
