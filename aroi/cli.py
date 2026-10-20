"""Command line entry point: `python -m aroi <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db
from .config import Config
from .llm import LLM, BudgetExceeded

log = logging.getLogger("aroi")

STAGES = ["discover", "triage", "analyze", "generate", "validate", "score"]


def _llm(cfg: Config) -> LLM:
    cfg.require("anthropic_api_key")
    return LLM(cfg, db.connect(cfg.database_url))


def _openalex(cfg: Config):
    from .openalex import OpenAlexClient

    cfg.require("openalex_api_key")
    return OpenAlexClient(cfg.openalex_api_key, cfg.contact_email)


def run_stage(name: str, conn, cfg: Config, llm=None, oa=None) -> dict:
    from .stages import analyze, discover, opportunities, score, triage, validate

    if name == "discover":
        return discover.run(conn, cfg, oa)
    if name == "triage":
        return triage.run(conn, cfg, llm)
    if name == "analyze":
        return analyze.run(conn, cfg, llm, oa)
    if name == "generate":
        return opportunities.run(conn, cfg, llm)
    if name == "validate":
        return validate.run(conn, cfg, llm)
    if name == "score":
        return score.run(conn, cfg, llm)
    raise ValueError(name)


def cmd_daily(cfg: Config, only: list[str] | None = None) -> int:
    conn = db.connect(cfg.database_url)
    needs_llm = any(s != "discover" for s in (only or STAGES))
    needs_oa = any(s in ("discover", "analyze") for s in (only or STAGES))
    llm = _llm(cfg) if needs_llm else None
    oa = _openalex(cfg) if needs_oa else None
    results, failed = {}, False
    for stage in only or STAGES:
        try:
            results[stage] = run_stage(stage, conn, cfg, llm, oa)
        except BudgetExceeded as e:
            log.warning("Stopping: %s", e)
            results[stage] = {"stopped": str(e)}
            break
        except Exception as e:  # keep going: later stages can still use earlier data
            conn.rollback()
            log.exception("Stage %s failed", stage)
            results[stage] = {"error": str(e)[:300]}
            failed = True
    if llm:
        results["llm_cost_this_run_usd"] = round(llm.run_spend, 4)
    if oa:
        results["openalex_cost_this_run_usd"] = round(oa.cost_usd, 4)
    print(json.dumps(results, indent=2, default=str))
    return 1 if failed else 0


def cmd_review(cfg: Config, opp: str, decision: str, notes: str | None) -> int:
    conn = db.connect(cfg.database_url)
    rows = conn.execute("select id, title from opportunities where id::text like %s", (opp + "%",)).fetchall()
    if len(rows) != 1:
        print(f"Expected one opportunity matching {opp!r}, found {len(rows)}.")
        return 1
    o = rows[0]
    conn.execute("insert into reviews (opportunity_id, decision, notes) values (%s,%s,%s)", (o["id"], decision, notes))
    conn.execute("update opportunities set stage=%s, updated_at=now() where id=%s", (decision, o["id"]))
    conn.commit()
    print(f"{o['title']} → {decision}")
    return 0


def cmd_list(cfg: Config, min_score: float, limit: int) -> int:
    conn = db.connect(cfg.database_url)
    rows = conn.execute(
        """select id, title, total_score, evidence_grade, stage, pipeline_status from opportunities
            where coalesce(total_score,0) >= %s order by total_score desc nulls last, created_at desc limit %s""",
        (min_score, limit),
    ).fetchall()
    for r in rows:
        score = f"{r['total_score']:>5}" if r["total_score"] is not None else "    -"
        print(f"{str(r['id'])[:8]}  {score}  {(r['evidence_grade'] or '-'):6}  {r['stage']:11} {r['pipeline_status']:10} {r['title'][:70]}")
    return 0


def cmd_status(cfg: Config) -> int:
    conn = db.connect(cfg.database_url)
    papers = conn.execute("select status, count(*) n from papers group by status order by status").fetchall()
    opps = conn.execute("select pipeline_status, count(*) n from opportunities group by 1 order by 1").fetchall()
    spend = db.month_to_date_spend(conn)
    print("Papers:        " + ", ".join(f"{r['status']}={r['n']}" for r in papers))
    print("Opportunities: " + ", ".join(f"{r['pipeline_status']}={r['n']}" for r in opps))
    print(f"LLM spend this month: ${spend:.2f} of ${cfg.monthly_budget_usd:.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="aroi", description="African Research Opportunity Intelligence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create tables and starter search queries")
    d = sub.add_parser("daily", help="run the full pipeline (discover → score)")
    d.add_argument("--only", nargs="+", choices=STAGES, help="run just these stages, in order")
    for s in STAGES:
        sub.add_parser(s, help=f"run only the {s} stage")
    g = sub.add_parser("digest", help="build (and email) the weekly digest")
    g.add_argument("--no-email", action="store_true")
    r = sub.add_parser("review", help="record your decision on an opportunity")
    r.add_argument("opportunity", help="id or id prefix (first 8 chars are enough)")
    r.add_argument("decision", choices=["hypothesis", "researching", "validation", "promising", "rejected", "licensed", "launched"])
    r.add_argument("notes", nargs="?")
    li = sub.add_parser("list", help="list opportunities")
    li.add_argument("--min-score", type=float, default=0)
    li.add_argument("--limit", type=int, default=30)
    sub.add_parser("status", help="pipeline counts and spend")
    a = ap.parse_args(argv)

    cfg = Config()
    cfg.require("database_url")
    if a.cmd == "init-db":
        conn = db.connect(cfg.database_url)
        db.init_db(conn)
        print("Database ready.")
        return 0
    if a.cmd == "daily":
        return cmd_daily(cfg, a.only)
    if a.cmd in STAGES:
        return cmd_daily(cfg, [a.cmd])
    if a.cmd == "digest":
        from . import digest

        print(json.dumps(digest.run(db.connect(cfg.database_url), cfg, send=not a.no_email), indent=2))
        return 0
    if a.cmd == "review":
        return cmd_review(cfg, a.opportunity, a.decision, a.notes)
    if a.cmd == "list":
        return cmd_list(cfg, a.min_score, a.limit)
    if a.cmd == "status":
        return cmd_status(cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
