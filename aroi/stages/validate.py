"""Stage 5 — Market Validator. Two calls per opportunity:
  1. A research call with live web search that writes a memo. Its sources come from the API's own
     citation blocks, so every stored URL is a page the model actually retrieved.
  2. A cheap structuring call that maps the memo into fields, each tied to a source id (S1, S2...).
Fields that point at a source id we did not retrieve are moved to 'unknowns', not trusted."""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import prompts, schemas
from ..llm import BudgetExceeded

log = logging.getLogger(__name__)


def _opportunity_brief(conn, o: dict[str, Any]) -> str:
    papers = conn.execute(
        """select p.title, p.publication_date, p.doi, a.readiness_level, a.problem,
                  (select string_agg(distinct i.name, '; ') from paper_institutions pi
                     join institutions i on i.id = pi.institution_id where pi.paper_id = p.id) as institutions
             from opportunity_papers op join papers p on p.id = op.paper_id
             left join lateral (select * from paper_analyses a where a.paper_id=p.id
                                order by created_at desc limit 1) a on true
            where op.opportunity_id = %s""",
        (o["id"],),
    ).fetchall()
    paper_lines = "\n".join(
        f"- {p['title']} ({p['publication_date']}; {p['institutions'] or 'institution unknown'}; "
        f"readiness: {p['readiness_level']}; doi: {p['doi'] or 'n/a'})"
        for p in papers
    )
    return (
        f"<opportunity>\nTitle: {o['title']}\nCustomer problem: {o['customer_problem']}\n"
        f"Target customer: {o['target_customer']}\nProposed solution: {o['proposed_solution']}\n"
        f"Revenue model: {o['revenue_model']}\nAssumptions: {'; '.join(o['commercial_assumptions'])}\n"
        f"Known risks: {'; '.join(o['key_risks'])}\nIP note: {o['ip_pathway']}\n</opportunity>\n\n"
        f"<source_research>\n{paper_lines}\n</source_research>"
    )


def _resolve(ref: str | None, sources) -> Any:
    if not ref or not ref.upper().startswith("S"):
        return None
    try:
        n = int(ref[1:])
    except ValueError:
        return None
    return sources[n - 1] if 1 <= n <= len(sources) else None


def validate_opportunity(conn, cfg, llm, o: dict[str, Any]) -> dict[str, Any]:
    conn.execute("update opportunities set pipeline_status='validating' where id=%s", (o["id"],))
    conn.commit()
    brief = _opportunity_brief(conn, o)
    research = llm.research(
        stage="validation_research",
        model=cfg.research_model,
        system=prompts.render(prompts.VALIDATION_SYSTEM, cfg.country),
        user=f"Research this product idea.\n\n{brief}",
        prompt_version=prompts.VALIDATION_VERSION,
        max_searches=cfg.web_searches_per_validation,
        country=cfg.country,
        opportunity_id=o["id"],
    )
    structured = llm.structured(
        stage="validation_structure",
        model=cfg.structuring_model,
        system=prompts.STRUCTURE_SYSTEM,
        user=f"<memo>\n{research.memo}\n</memo>",
        schema=schemas.VALIDATION,
        prompt_version=prompts.STRUCTURE_VERSION,
        max_tokens=4000,
        opportunity_id=o["id"],
    )

    # Re-home unsourced or wrongly sourced items into unknowns
    unknowns = list(structured.get("unknowns_needing_field_research") or [])
    evidence: list[tuple[str, str, str, Any]] = []  # (type, claim, ref)
    for key, etype, text_of in (
        ("competitors", "competitor", lambda x: f"{x['name']}: {x['what_they_offer']}"),
        ("price_evidence", "price", lambda x: f"{x['item']}: {x['price']}"),
        ("regulatory_findings", "regulation", lambda x: x["finding"]),
        ("ip_findings", "patent", lambda x: x["finding"]),
    ):
        kept = []
        for item in structured.get(key) or []:
            src = _resolve(item.get("source_ref"), research.sources)
            if src is None:
                unknowns.append(f"Unsourced ({etype}): {text_of(item)}")
                item["source_ref"] = "none"
            else:
                evidence.append((etype, text_of(item), src))
                item["source_url"] = src.url
            kept.append(item)
        structured[key] = kept
    structured["unknowns_needing_field_research"] = unknowns

    with conn.transaction():
        conn.execute(
            """insert into market_validations (opportunity_id, model, prompt_version, memo, structured, searches_used)
               values (%s,%s,%s,%s,%s,%s)""",
            (o["id"], cfg.research_model, prompts.VALIDATION_VERSION, research.memo, Jsonb(structured), research.searches),
        )
        for etype, claim, src in evidence:
            conn.execute(
                """insert into evidence_items (opportunity_id, source_url, source_title, evidence_type, claim_supported, cited_text)
                   values (%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                (o["id"], src.url, src.title, etype, claim[:1000], src.cited_text),
            )
        for src in research.sources:  # keep the raw citation trail too
            conn.execute(
                """insert into evidence_items (opportunity_id, source_url, source_title, evidence_type, claim_supported, cited_text)
                   values (%s,%s,%s,'web_citation',%s,%s) on conflict do nothing""",
                (o["id"], src.url, src.title, "Cited in market memo", src.cited_text),
            )
        conn.execute(
            """update opportunities set pipeline_status='validated', recommended_action=%s, updated_at=now()
                where id=%s""",
            (structured.get("recommendation"), o["id"]),
        )
    return {"sources": len(research.sources), "searches": research.searches, "linked_evidence": len(evidence)}


def run(conn: psycopg.Connection, cfg, llm) -> dict[str, Any]:
    opps = conn.execute(
        """select * from opportunities where pipeline_status in ('generated','validating') and stage <> 'rejected'
            order by generator_confidence desc nulls last, created_at limit %s""",
        (cfg.validate_per_run,),
    ).fetchall()
    stats = {"validated": 0, "failed": 0, "sources": 0, "searches": 0}
    for o in opps:
        try:
            r = validate_opportunity(conn, cfg, llm, o)
        except BudgetExceeded:
            conn.rollback()
            conn.execute("update opportunities set pipeline_status='generated' where id=%s", (o["id"],))
            conn.commit()
            raise
        except Exception as e:
            # 'failed' stops it being retried (and re-billed) every night; reset by hand to retry
            conn.rollback()
            conn.execute("update opportunities set pipeline_status='failed' where id=%s", (o["id"],))
            conn.commit()
            stats["failed"] += 1
            log.warning("Validation failed for %s: %s", o["id"], e)
            continue
        conn.commit()
        stats["validated"] += 1
        stats["sources"] += r["sources"]
        stats["searches"] += r["searches"]
    log.info("Validation: %s", stats)
    return stats
