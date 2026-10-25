"""Database helpers (PostgreSQL / Supabase via psycopg 3)."""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def connect(database_url: str) -> psycopg.Connection:
    # prepare_threshold=None keeps us compatible with Supabase's transaction pooler (port 6543),
    # which does not support server-side prepared statements.
    return psycopg.connect(database_url, row_factory=dict_row, prepare_threshold=None, autocommit=False)


def init_db(conn: psycopg.Connection, seed: bool = True) -> None:
    conn.execute((SQL_DIR / "schema.sql").read_text())
    if seed:
        conn.execute((SQL_DIR / "seed.sql").read_text())
    conn.commit()


def get_state(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("select value from pipeline_state where key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        """insert into pipeline_state (key, value) values (%s, %s)
           on conflict (key) do update set value = excluded.value, updated_at = now()""",
        (key, value),
    )


def month_to_date_spend(conn: psycopg.Connection) -> float:
    row = conn.execute(
        "select coalesce(sum(cost_usd), 0) as s from agent_runs where started_at >= date_trunc('month', now())"
    ).fetchone()
    return float(row["s"])
