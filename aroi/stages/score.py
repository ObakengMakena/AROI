"""Stage 6 — Investment Committee. A Grok Bot reviewer challenges the case (through the work queue) and
returns 1-5 criterion scores; the weighted total, evidence-grade cap and recommended action are computed
in code at ingest, from the evidence stored at that moment."""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import agent_queue, prompts, schemas
from ..scoring import CRITERIA, RUBRIC_VERSION, action_for, cap_grade, weighted_score

log = logging.getLogger(__name__)

STAGE = "score"


def build_case(conn, o: dict[str, Any]) -> tuple[str, int, int]:
    papers = conn.execute(
        """select p.id, p.title, p.publication_date, a.problem, a.methods, a.readiness_level,
                  a.evidence_confidence, a.used_full_text, a.missing_information, op.relationship
             from opportunity_papers op join papers p on p.id = op.paper_id
             left join lateral (select * from paper_analyses a where a.paper_id=p.id
                                order by created_at desc limit 1) a on true
            where op.opportunity_id=%s""",
        (o["id"],),
    ).fetchall()
    verified_quotes = 0
    paper_blocks = []
    for p in papers:
        fs = conn.execute(
            "select claim, quote_verified, measured_or_inferred, limitations from findings where paper_id=%s",
            (p["id"],),
        ).fetchall()
        verified_quotes += sum(1 for f in fs if f["quote_verified"])
        lines = "\n".join(
            f"  - [{f['measured_or_inferred']}; quote {'VERIFIED' if f['quote_verified'] else 'not verified'}] {f['claim']}"
            + (f" (limits: {'; '.join(f['limitations'])})" if f["limitations"] else "")
            for f in fs
        )
        paper_blocks.append(
            f"Paper ({p['relationship']}): {p['title']} ({p['publication_date']})\n"
            f"  Problem: {p['problem']}\n  Methods: {p['methods']}\n  Readiness: {p['readiness_level']} | "
            f"analyst confidence {p['evidence_confidence']} | full text used: {p['used_full_text']}\n"
            f"  Not established: {'; '.join(p['missing_information'] or [])}\n  Findings:\n{lines}"
        )
    mv = conn.execute(
        "select memo, structured from market_validations where opportunity_id=%s order by created_at desc limit 1",
        (o["id"],),
    ).fetchone()
    # only fetched pages tied to a specific claim they support count; the raw citation trail does not
    web_sources = conn.execute(
        """select count(distinct source_url) as n from evidence_items
            where opportunity_id=%s and evidence_type <> 'web_citation'""",
        (o["id"],),
    ).fetchone()["n"]
    case = (
        f"<opportunity>\nTitle: {o['title']}\nCustomer problem: {o['customer_problem']}\nTarget customer: "
        f"{o['target_customer']}\nSolution: {o['proposed_solution']}\nRevenue model: {o['revenue_model']}\n"
        f"Assumptions: {'; '.join(o['commercial_assumptions'])}\nRisks: {'; '.join(o['key_risks'])}\n"
        f"IP pathway: {o['ip_pathway']}\nCheapest disproving experiment: {o['next_experiment']}\n</opportunity>\n\n"
        f"<scientific_basis>\n" + "\n\n".join(paper_blocks) + "\n</scientific_basis>\n\n"
        f"<market_memo web_sources_retrieved=\"{web_sources}\">\n{mv['memo'] if mv else 'No market research available.'}\n</market_memo>\n\n"
        f"<structured_market_findings>\n{json.dumps(mv['structured'] if mv else {}, ensure_ascii=False)[:6000]}\n</structured_market_findings>"
    )
    return case, verified_quotes, web_sources


def enqueue(conn: psycopg.Connection, cfg, oa=None) -> dict[str, Any]:
    opps = conn.execute(
        """select * from opportunities o where pipeline_status='validated' and stage <> 'rejected'
              and not exists (select 1 from agent_tasks t where t.stage='score'
                              and t.subject_key = 'opportunity:' || o.id and t.status in ('pending','claimed','submitted'))
            order by created_at limit %s""",
        (cfg.score_per_run,),
    ).fetchall()
    stats = {"tasks": 0}
    for o in opps:
        case, _, _ = build_case(conn, o)
        payload = {
            "prompt_version": prompts.REVIEW_VERSION,
            "instructions": prompts.render(prompts.REVIEW_SYSTEM, cfg.country, o["sector"]),
            "input": case,
            "output_schema": schemas.REVIEW,
        }
        if agent_queue.enqueue(conn, cfg, STAGE, f"opportunity:{o['id']}", [o["id"]], payload, sector=o["sector"]):
            stats["tasks"] += 1
    conn.commit()
    return stats


def check(payload: dict[str, Any], result: dict[str, Any]) -> list[str]:
    return []


def ingest(conn, cfg, task) -> dict[str, Any]:
    o = conn.execute("select * from opportunities where id=%s", (task["subject_ids"][0],)).fetchone()
    if o is None:
        raise ValueError("opportunity no longer exists")
    if o["pipeline_status"] != "validated":
        raise ValueError(f"opportunity is {o['pipeline_status']!r}, expected 'validated'")
    r = score_opportunity(conn, cfg, o, task["result"], worker=task["claimed_by"] or "grok-bot")
    log.info("Scored %r: %s", o["title"][:60], r)
    return {"scored": 1}


def give_up(conn, task) -> None:
    log.warning("Scoring gave up for %s: %s", task["subject_ids"], task["last_error"])
    conn.execute(
        "update opportunities set pipeline_status='failed', updated_at=now() where id = any(%s) and pipeline_status='validated'",
        (task["subject_ids"],),
    )


def score_opportunity(conn, cfg, o: dict[str, Any], out: dict[str, Any], worker: str) -> dict[str, Any]:
    case, verified_quotes, web_sources = build_case(conn, o)
    scores = {k: int(out["criteria"][k]["score"]) for k in CRITERIA}
    total = weighted_score(scores)
    grade = cap_grade(out.get("evidence_grade", "low"), verified_quotes, web_sources)
    blockers = out.get("blocking_risks") or []
    has_blocker = any(b.get("severity") == "blocker" for b in blockers)
    action = action_for(total, grade, has_blocker)
    criteria_json = {
        k: {"label": CRITERIA[k][0], "weight": CRITERIA[k][1], **out["criteria"][k]} for k in CRITERIA
    }
    criteria_json["_meta"] = {
        "model_grade": out.get("evidence_grade"),
        "verified_quotes": verified_quotes,
        "web_sources": web_sources,
        "unsupported_claims": out.get("unsupported_claims") or [],
    }
    with conn.transaction():
        conn.execute(
            """insert into opportunity_scores (opportunity_id, rubric_version, criteria_json, total_score,
                 evidence_grade, blocking_risks, model) values (%s,%s,%s,%s,%s,%s,%s)""",
            (o["id"], RUBRIC_VERSION, Jsonb(criteria_json), total, grade, Jsonb(blockers), worker),
        )
        conn.execute(
            """update opportunities set pipeline_status='scored', total_score=%s, evidence_grade=%s,
                 blocking_risks=%s, recommended_action=%s, next_action=%s, updated_at=now() where id=%s""",
            (total, grade, Jsonb(blockers), action, out.get("next_action"), o["id"]),
        )
    return {"score": total, "grade": grade, "action": action}


