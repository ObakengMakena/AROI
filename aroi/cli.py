"""Command line entry point: `python -m aroi <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import agent_queue, budget, db
from .budget import BudgetExceeded
from .config import Config
from psycopg.types.json import Jsonb

log = logging.getLogger("aroi")

STAGES = ["discover", "triage", "analyze", "generate", "validate", "score"]
AI_STAGES = agent_queue.AI_STAGES


def _openalex(cfg: Config):
    from .openalex import OpenAlexClient

    cfg.require("openalex_api_key")
    return OpenAlexClient(cfg.openalex_api_key, cfg.contact_email)


def run_stage(name: str, conn, cfg: Config, oa=None) -> dict:
    """discover runs directly; for an AI stage this *enqueues* agent tasks (see agent_queue)."""
    if name == "discover":
        from .stages import discover

        return discover.run(conn, cfg, oa)
    return agent_queue.stage_module(name).enqueue(conn, cfg, oa)


def rotate_sectors(keys: list[str], day: int) -> list[str]:
    """Start with a different sector each day, so quota-limited stages don't always favour the same one."""
    if not keys:
        return keys
    k = day % len(keys)
    return keys[k:] + keys[:k]


def _start_run(conn, command: str):
    row = conn.execute("insert into pipeline_runs (command) values (%s) returning id", (command,)).fetchone()
    conn.commit()
    return row["id"]


def _finish_run(conn, run_id, ok: bool, results: dict) -> None:
    conn.rollback()
    conn.execute(
        "update pipeline_runs set ok=%s, results=%s, finished_at=now() where id=%s",
        (ok, Jsonb(json.loads(json.dumps(results, default=str))), run_id),
    )
    conn.commit()


def run_pipeline(conn, cfg: Config, command: str, steps: list[str], oa=None) -> tuple[bool, dict]:
    """steps: 'discover', 'ingest', or an AI stage name (= enqueue). Logs a pipeline_runs heartbeat and the
    OpenAlex spend; a failing step is recorded and later steps still run."""
    from datetime import date

    from . import sectors

    for key in cfg.sectors:
        sectors.get(key)  # fail fast on a typo in SECTORS
    cfg.sectors = rotate_sectors(cfg.sectors, date.today().toordinal())
    run_id = _start_run(conn, command)
    results: dict = {"sector_order": cfg.sectors}
    failed = False
    cost_before = oa.cost_usd if oa else 0.0
    for step in steps:
        try:
            if step in ("discover", "analyze") and oa is not None:
                budget.check(conn, cfg)  # OpenAlex is the only metered API left
            if step == "ingest":
                results["ingest"] = agent_queue.ingest(conn, cfg)
            else:
                results[step] = run_stage(step, conn, cfg, oa)
                if isinstance(results[step], dict) and results[step].get("fatal"):
                    log.error("Step %s did not do its job: %s", step, results[step]["fatal"])
                    failed = True
        except BudgetExceeded as e:
            conn.rollback()
            log.warning("Skipping %s: %s", step, e)
            results[step] = {"stopped": str(e)}
        except Exception as e:  # keep going: later stages can still use earlier data
            conn.rollback()
            log.exception("Step %s failed", step)
            results[step] = {"error": str(e)[:300]}
            failed = True
    if oa:
        spent = oa.cost_usd - cost_before
        results["openalex_cost_this_run_usd"] = round(spent, 4)
        budget.log_openalex(conn, command, spent)
    results["queue"] = agent_queue.stats(conn)
    _finish_run(conn, run_id, not failed, results)
    return not failed, results


def cmd_run(cfg: Config, command: str, steps: list[str]) -> int:
    conn = db.connect(cfg.database_url)
    oa = _openalex(cfg) if any(s in ("discover", "analyze") for s in steps) else None
    ok, results = run_pipeline(conn, cfg, command, steps, oa)
    print(json.dumps(results, indent=2, default=str))
    return 0 if ok else 1


def cmd_review(cfg: Config, item: str, decision: str, notes: str | None) -> int:
    conn = db.connect(cfg.database_url)
    opps = conn.execute("select id, title from opportunities where id::text like %s", (item + "%",)).fetchall()
    hyps = conn.execute(
        "select id, hypothesis as title from finance_hypotheses where id::text like %s", (item + "%",)
    ).fetchall()
    if len(opps) + len(hyps) != 1:
        print(f"Expected one opportunity or finance hypothesis matching {item!r}, found {len(opps) + len(hyps)}.")
        return 1
    if opps:
        o = opps[0]
        conn.execute("insert into reviews (opportunity_id, decision, notes) values (%s,%s,%s)", (o["id"], decision, notes))
        conn.execute("update opportunities set stage=%s, updated_at=now() where id=%s", (decision, o["id"]))
    else:
        o = hyps[0]
        conn.execute(
            "insert into reviews (finance_hypothesis_id, decision, notes) values (%s,%s,%s)", (o["id"], decision, notes)
        )
        conn.execute("update finance_hypotheses set stage=%s where id=%s", (decision, o["id"]))
    conn.commit()
    print(f"{o['title'][:100]} → {decision}")
    return 0


def cmd_list(cfg: Config, min_score: float, limit: int, sector: str | None) -> int:
    conn = db.connect(cfg.database_url)
    rows = conn.execute(
        """select id, sector, title, total_score, evidence_grade, stage, pipeline_status from opportunities
            where coalesce(total_score,0) >= %s and (%s::text is null or sector = %s)
            order by total_score desc nulls last, created_at desc limit %s""",
        (min_score, sector, sector, limit),
    ).fetchall()
    for r in rows:
        score = f"{r['total_score']:>5}" if r["total_score"] is not None else "    -"
        print(f"{str(r['id'])[:8]}  {r['sector'][:12]:12} {score}  {(r['evidence_grade'] or '-'):6}  {r['stage']:11} {r['title'][:60]}")
    if sector in (None, "quant_finance"):
        hyps = conn.execute(
            """select id, testability_score, recommended_action, stage, hypothesis from finance_hypotheses
                order by testability_score desc limit %s""",
            (limit,),
        ).fetchall()
        for h in hyps:
            print(f"{str(h['id'])[:8]}  {'quant_finance':12} {h['testability_score']:>5}  {h['recommended_action'][:6]:6}  {h['stage']:11} {h['hypothesis'][:60]}")
    return 0


def cmd_status(cfg: Config) -> int:
    conn = db.connect(cfg.database_url)
    rows = conn.execute(
        """select coalesce(sector,'?') sector, status, count(*) n from papers group by 1, 2 order by 1, 2"""
    ).fetchall()
    by: dict[str, list[str]] = {}
    for r in rows:
        by.setdefault(r["sector"], []).append(f"{r['status']}={r['n']}")
    print("Papers")
    for k, v in by.items():
        print(f"  {k:14} " + ", ".join(v))
    opps = conn.execute(
        "select sector, pipeline_status, count(*) n from opportunities group by 1, 2 order by 1, 2"
    ).fetchall()
    print("Opportunities")
    for r in opps:
        print(f"  {r['sector']:14} {r['pipeline_status']}={r['n']}")
    n = conn.execute("select count(*) n from finance_hypotheses").fetchone()["n"]
    print(f"Finance hypotheses: {n}")
    print("Agent queue")
    for stage, st in agent_queue.stats(conn).items():
        print(f"  {stage:14} " + ", ".join(f"{k}={v}" for k, v in st.items()))
    spend = db.month_to_date_spend(conn)
    print(f"OpenAlex spend this month: ${spend:.2f} of ${cfg.monthly_budget_usd:.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="aroi", description="African Research Opportunity Intelligence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create tables and starter search queries")
    sub.add_parser("daily", help="discover new papers, then tick")
    sub.add_parser("tick", help="ingest submitted agent results, then enqueue the next agent tasks")
    sub.add_parser("ingest", help="ingest submitted agent results only")
    sub.add_parser("discover", help="run only discovery (OpenAlex)")
    for s in AI_STAGES:
        sub.add_parser(s, help=f"enqueue {s} tasks for the agents")
    g = sub.add_parser("digest", help="build (and email) the weekly digest")
    g.add_argument("--no-email", action="store_true")
    r = sub.add_parser("review", help="record your decision on an opportunity")
    r.add_argument("item", help="opportunity or finance hypothesis id (first 8 chars are enough)")
    r.add_argument("decision", choices=["hypothesis", "researching", "validation", "promising", "rejected", "licensed", "launched"])
    r.add_argument("notes", nargs="?")
    li = sub.add_parser("list", help="list opportunities")
    li.add_argument("--min-score", type=float, default=0)
    li.add_argument("--limit", type=int, default=30)
    li.add_argument("--sector", help="only this sector")
    sub.add_parser("status", help="pipeline counts and spend")
    a = ap.parse_args(argv)

    cfg = Config()
    cfg.require("database_url")
    if a.cmd == "init-db":
        conn = db.connect(cfg.database_url)
        db.init_db(conn)
        print("Database ready.")
        return 0
    tick = ["ingest", *AI_STAGES]
    if a.cmd == "daily":
        return cmd_run(cfg, "daily", ["discover", *tick])
    if a.cmd == "tick":
        return cmd_run(cfg, "tick", tick)
    if a.cmd == "ingest":
        return cmd_run(cfg, "ingest", ["ingest"])
    if a.cmd in STAGES:
        return cmd_run(cfg, a.cmd, [a.cmd])
    if a.cmd == "digest":
        from . import digest

        print(json.dumps(digest.run(db.connect(cfg.database_url), cfg, send=not a.no_email), indent=2))
        return 0
    if a.cmd == "review":
        return cmd_review(cfg, a.item, a.decision, a.notes)
    if a.cmd == "list":
        return cmd_list(cfg, a.min_score, a.limit, a.sector)
    if a.cmd == "status":
        return cmd_status(cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
