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


def rotate_sectors(keys: list[str], day: int) -> list[str]:
    """Start with a different sector each day, so if the run budget runs out, the same sector
    isn't always the one left waiting."""
    if not keys:
        return keys
    k = day % len(keys)
    return keys[k:] + keys[:k]


def cmd_daily(cfg: Config, only: list[str] | None = None) -> int:
    from datetime import date

    from . import sectors

    for key in cfg.sectors:
        sectors.get(key)  # fail fast on a typo in SECTORS
    cfg.sectors = rotate_sectors(cfg.sectors, date.today().toordinal())
    log.info("Sector order today: %s", ", ".join(cfg.sectors))
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
    spend = db.month_to_date_spend(conn)
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
    if a.cmd == "daily":
        return cmd_daily(cfg, a.only)
    if a.cmd in STAGES:
        return cmd_daily(cfg, [a.cmd])
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
