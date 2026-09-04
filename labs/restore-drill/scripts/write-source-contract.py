#!/usr/bin/env python3
"""Capture independent source expectations before backup."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "source-contract.json"


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True).strip()


def pg(sql: str) -> str:
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
            "app",
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


def main() -> None:
    tables = [t for t in pg(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    ).splitlines() if t]

    contract = {
        "scope_note": (
            "This local lab validates the recovery procedure after backup retrieval. "
            "Object-storage availability, credentials, and retrieval are separate "
            "failure modes and are not tested here."
        ),
        "postgres": {
            "database": "app",
            "tables": tables,
            "shops_count": int(pg("SELECT count(*) FROM shops")),
            "users_count": int(pg("SELECT count(*) FROM users")),
            "orders_count": int(pg("SELECT count(*) FROM orders")),
            "latest_migration": pg(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ),
            "known_shop": {
                "id": int(pg("SELECT id FROM shops WHERE id=1")),
                "name": pg("SELECT name FROM shops WHERE id=1"),
                "plan": pg("SELECT plan FROM shops WHERE id=1"),
            },
            "min_order_timestamp": pg(
                "SELECT to_char(min(created_at) AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD HH24:MI:SS') || '+00' FROM orders"
            ),
            "max_order_timestamp": pg(
                "SELECT to_char(max(created_at) AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD HH24:MI:SS') || '+00' FROM orders"
            ),
            "ingestion_checkpoint": pg(
                "SELECT checkpoint FROM ingestion_checkpoints WHERE source='orders-feed'"
            ),
        },
        "clickhouse": {
            "database": "analytics",
            "tables": [],
            "event_count": int(ch("SELECT count() FROM analytics.events")),
            "min_timestamp": ch(
                "SELECT formatDateTime(min(timestamp), '%Y-%m-%dT%H:%i:%SZ', 'UTC') "
                "FROM analytics.events"
            ),
            "max_timestamp": ch(
                "SELECT formatDateTime(max(timestamp), '%Y-%m-%dT%H:%i:%SZ', 'UTC') "
                "FROM analytics.events"
            ),
            "daily_aggregate_top": ch(
                "SELECT day, shop_id, sum(event_count), sum(amount) "
                "FROM analytics.events_daily GROUP BY day, shop_id "
                "ORDER BY day DESC, shop_id ASC LIMIT 1 FORMAT JSONEachRow"
            ),
            "materialized_view": "events_daily_mv",
            "projection": "events_by_type",
        },
    }

    rows = ch(
        "SELECT name, engine FROM system.tables "
        "WHERE database='analytics' AND name NOT LIKE '.inner%' "
        "ORDER BY name FORMAT TSV"
    ).splitlines()
    for row in rows:
        name, engine = row.split("\t")
        contract["clickhouse"]["tables"].append({"name": name, "engine": engine})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(contract, indent=2) + "\n")
    print(json.dumps(contract, indent=2))


if __name__ == "__main__":
    main()
