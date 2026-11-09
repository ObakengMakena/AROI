"""Stage 5 — Market Validator, done by a Grok Bot agent with web access through the work queue. One task
per opportunity: the agent researches, writes a memo, and submits structured fields where every
competitor/price/regulatory/IP item points at a source id (S1, S2...) in its `sources` list.

Only sources the agent reports as fetched (fetched=true, public http(s) URL) are stored. Items that point
at a missing or unfetched source are moved to 'unknowns', not trusted."""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

import ipaddress
from urllib.parse import urlparse

from .. import agent_queue, prompts, schemas

log = logging.getLogger(__name__)

STAGE = "validate"

SUBMISSION_RULES = """

HOW TO SUBMIT (checked by code):
- Do the research yourself with web search and by opening pages. List EVERY page you rely on in `sources`
  with a unique id (S1, S2, ...), its exact URL and title, fetched=true only if you actually opened it this
  session, and quoted_text = a short passage copied verbatim from that page. Never list a URL you did not
  open; search-result snippets alone count as fetched=false.
- Put the memo (sections above) in `memo`, citing ids inline like [S1]. searches_used = number of web searches.
- Every competitor, price, regulatory and IP item needs source_ref = the id of the page that supports it,
  or "none". Items pointing at a missing or unfetched source are moved to unknowns automatically.
- Put unsourced claims in unknowns_needing_field_research or unit_economics_assumptions instead.
"""


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


def _source_errors(sources: list[dict[str, Any]]) -> list[str]:
    errors, ids = [], set()
    for i, src in enumerate(sources):
        if src["id"] in ids:
            errors.append(f"sources/{i}/id: duplicate id {src['id']}")
        ids.add(src["id"])
        host = urlparse(src["url"]).hostname or ""
        if not host or "." not in host or host == "localhost" or host.endswith((".local", ".internal")):
            errors.append(f"sources/{i}/url: {src['url']!r} is not a public web address")
            continue
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                errors.append(f"sources/{i}/url: {src['url']!r} is a private address")
        except ValueError:
            pass
    return errors


def check(payload: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors = _source_errors(result["sources"])
    if result["recommendation"] == "investigate" and not any(s["fetched"] for s in result["sources"]):
        errors.append("sources: 'investigate' needs at least one fetched source; otherwise recommend 'park'")
    return errors


def enqueue(conn: psycopg.Connection, cfg, oa=None) -> dict[str, Any]:
    stats: dict[str, Any] = {"tasks": 0, "by_sector": {}}
    for key in cfg.sectors:
        opps = conn.execute(
            """select * from opportunities o
                where pipeline_status = 'generated' and stage <> 'rejected' and sector = %s
                order by generator_confidence desc nulls last, created_at limit %s for update skip locked""",
            (key, cfg.validate_per_sector),
        ).fetchall()
        for o in opps:
            payload = {
                "prompt_version": prompts.VALIDATION_VERSION,
                "instructions": prompts.render(prompts.VALIDATION_SYSTEM, cfg.country, o["sector"]) + SUBMISSION_RULES,
                "input": "Research this product idea.\n\n" + _opportunity_brief(conn, o),
                "output_schema": schemas.VALIDATION_SUBMISSION,
            }
            if agent_queue.enqueue(conn, cfg, STAGE, f"opportunity:{o['id']}", [o["id"]], payload, sector=key,
                                   priority=float(o["generator_confidence"] or 0)):
                conn.execute("update opportunities set pipeline_status='validating' where id=%s", (o["id"],))
                stats["tasks"] += 1
                stats["by_sector"][key] = stats["by_sector"].get(key, 0) + 1
        conn.commit()
    return stats


def ingest(conn, cfg, task) -> dict[str, Any]:
    o = conn.execute("select * from opportunities where id=%s", (task["subject_ids"][0],)).fetchone()
    if o is None:
        raise ValueError("opportunity no longer exists")
    r = store_validation(conn, o, task["result"], worker=task["claimed_by"] or "grok-bot",
                         prompt_version=task["payload"].get("prompt_version"))
    return {"validated": 1, "by_sector": {o["sector"]: 1}, **r}


def give_up(conn, task) -> None:
    conn.execute(
        "update opportunities set pipeline_status='failed', updated_at=now() where id = any(%s)",
        (task["subject_ids"],),
    )


def store_validation(conn, o: dict[str, Any], out: dict[str, Any], worker: str,
                     prompt_version: str | None) -> dict[str, Any]:
    # only pages the agent actually opened count as evidence
    fetched = {s["id"]: s for s in out["sources"] if s["fetched"] and s["url"].lower().startswith(("http://", "https://"))}
    structured = {k: v for k, v in out.items() if k in schemas.VALIDATION["properties"]}

    # Re-home unsourced or wrongly sourced items into unknowns
    unknowns = list(structured.get("unknowns_needing_field_research") or [])
    evidence: list[tuple[str, str, dict]] = []
    for key, etype, text_of in (
        ("competitors", "competitor", lambda x: f"{x['name']}: {x['what_they_offer']}"),
        ("price_evidence", "price", lambda x: f"{x['item']}: {x['price']}"),
        ("regulatory_findings", "regulation", lambda x: x["finding"]),
        ("ip_findings", "patent", lambda x: x["finding"]),
    ):
        kept = []
        for item in structured.get(key) or []:
            src = fetched.get((item.get("source_ref") or "").upper())
            if src is None:
                unknowns.append(f"Unsourced ({etype}): {text_of(item)}")
                item["source_ref"] = "none"
            else:
                evidence.append((etype, text_of(item), src))
                item["source_url"] = src["url"]
            kept.append(item)
        structured[key] = kept
    structured["unknowns_needing_field_research"] = unknowns
    structured["sources"] = list(fetched.values())
    structured["unfetched_sources_ignored"] = len(out["sources"]) - len(fetched)

    with conn.transaction():
        conn.execute(
            """insert into market_validations (opportunity_id, model, prompt_version, memo, structured, searches_used)
               values (%s,%s,%s,%s,%s,%s)""",
            (o["id"], worker, prompt_version, out["memo"], Jsonb(structured), out["searches_used"]),
        )
        for etype, claim, src in evidence:
            conn.execute(
                """insert into evidence_items (opportunity_id, source_url, source_title, evidence_type, claim_supported, cited_text)
                   values (%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                (o["id"], src["url"], src["title"], etype, claim[:1000], src["quoted_text"][:2000]),
            )
        for src in fetched.values():  # keep the raw citation trail too (not counted as evidence by scoring)
            conn.execute(
                """insert into evidence_items (opportunity_id, source_url, source_title, evidence_type, claim_supported, cited_text)
                   values (%s,%s,%s,'web_citation',%s,%s) on conflict do nothing""",
                (o["id"], src["url"], src["title"], "Cited in market memo", src["quoted_text"][:2000]),
            )
        conn.execute(
            """update opportunities set pipeline_status='validated', recommended_action=%s, updated_at=now()
                where id=%s""",
            (structured.get("recommendation"), o["id"]),
        )
    return {"sources": len(fetched), "searches": out["searches_used"], "linked_evidence": len(evidence)}
