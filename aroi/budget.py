"""Spend guard. The AI stages are done by Grok Bot agents through the work queue, so the only metered
API left is OpenAlex (list/search calls and $0.01 GROBID downloads). Its cost is logged to agent_runs
(stage 'openalex:<command>') and counted against MONTHLY_BUDGET_USD."""
from __future__ import annotations

import psycopg

from . import db


class BudgetExceeded(RuntimeError):
    pass


def check(conn: psycopg.Connection, cfg) -> float:
    spent = db.month_to_date_spend(conn)
    if spent >= cfg.monthly_budget_usd:
        raise BudgetExceeded(
            f"Monthly OpenAlex budget reached: ${spent:.2f} of ${cfg.monthly_budget_usd:.2f}. "
            "Raise MONTHLY_BUDGET_USD or wait for next month."
        )
    return spent


def log_openalex(conn: psycopg.Connection, command: str, cost_usd: float, error: str | None = None) -> None:
    if not cost_usd and not error:
        return
    conn.execute(
        """insert into agent_runs (stage, model, status, cost_usd, error, finished_at)
           values (%s, 'openalex', %s, %s, %s, now())""",
        (f"openalex:{command}", "error" if error else "ok", round(cost_usd, 6), error),
    )
    conn.commit()
