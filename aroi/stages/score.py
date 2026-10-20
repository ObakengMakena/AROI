"""Stage 6 — Investment Committee. A reviewer model challenges the case and returns 1-5 criterion
scores; the weighted total, evidence-grade cap and recommended action are computed in code."""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import prompts, schemas
from ..llm import BudgetExceeded
from ..scoring import CRITERIA, RUBRIC_VERSION, action_for, cap_grade, weighted_score

log = logging.getLogger(__name__)


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
    web_sources = conn.execute(
        "select count(distinct source_url) as n from evidence_items where opportunity_id=%s and evidence_type='web_citation'",
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


def score_opportunity(conn, cfg, llm, o: dict[str, Any]) -> dict[str, Any]:
    case, verified_quotes, web_sources = build_case(conn, o)
    out = llm.structured(
        stage="review",
        model=cfg.review_model,
        system=prompts.render(prompts.REVIEW_SYSTEM, cfg.country),
        user=case,
        schema=schemas.REVIEW,
        prompt_version=prompts.REVIEW_VERSION,
        max_tokens=3500,
        opportunity_id=o["id"],
    )
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
            (o["id"], RUBRIC_VERSION, Jsonb(criteria_json), total, grade, Jsonb(blockers), cfg.review_model),
        )
        conn.execute(
            """update opportunities set pipeline_status='scored', total_score=%s, evidence_grade=%s,
                 blocking_risks=%s, recommended_action=%s, next_action=%s, updated_at=now() where id=%s""",
            (total, grade, Jsonb(blockers), action, out.get("next_action"), o["id"]),
        )
    return {"score": total, "grade": grade, "action": action}


def run(conn: psycopg.Connection, cfg, llm) -> dict[str, Any]:
    opps = conn.execute(
        """select * from opportunities where pipeline_status='validated' and stage <> 'rejected'
            order by created_at limit %s""",
        (cfg.score_per_run,),
    ).fetchall()
    stats = {"scored": 0, "failed": 0}
    for o in opps:
        try:
            r = score_opportunity(conn, cfg, llm, o)
            log.info("Scored %r: %s", o["title"][:60], r)
        except BudgetExceeded:
            raise
        except Exception as e:
            conn.rollback()
            stats["failed"] += 1
            log.warning("Scoring failed for %s: %s", o["id"], e)
            continue
        conn.commit()
        stats["scored"] += 1
    log.info("Scoring: %s", stats)
    return stats
