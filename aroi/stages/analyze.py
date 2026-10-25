"""Stage 3 — Scientific Analyst, done by a Grok Bot agent through the work queue. The pipeline picks the
highest-signal queued papers in each sector, fetches GROBID full text from OpenAlex (so agents never need
the OpenAlex key) and enqueues an analysis task. On ingest every quote is checked verbatim against the
abstract and full text. Quant-finance papers get the finance analyst, which extracts a testable
hypothesis instead."""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import agent_queue, prompts, schemas, sectors
from ..normalize import tei_to_text, verify_quote
from ..scoring import FINANCE_RUBRIC_VERSION, clamp01, finance_action, testability_score

log = logging.getLogger(__name__)

STAGE = "analyze"


def _institutions(conn, paper_id) -> list[str]:
    rows = conn.execute(
        """select i.name, i.country_code from paper_institutions pi
             join institutions i on i.id = pi.institution_id where pi.paper_id = %s""",
        (paper_id,),
    ).fetchall()
    return [f"{r['name']} ({r['country_code'] or '?'})" for r in rows]


def build_user_prompt(p: dict[str, Any], institutions: list[str], full_text: str | None) -> str:
    meta = {
        "title": p["title"],
        "published": str(p["publication_date"]),
        "type": p["work_type"],
        "venue": p["source_name"],
        "topic": p["primary_topic"],
        "institutions": institutions,
        "doi": p["doi"],
    }
    parts = [
        "<paper_metadata>\n" + json.dumps(meta, ensure_ascii=False, indent=1) + "\n</paper_metadata>",
        "<abstract>\n" + (p["abstract"] or "") + "\n</abstract>",
    ]
    if full_text:
        parts.append("<full_text>\n" + full_text + "\n</full_text>")
    else:
        parts.append("<full_text>NOT AVAILABLE — only the abstract was supplied.</full_text>")
    return "\n\n".join(parts)


def enqueue(conn: psycopg.Connection, cfg, oa=None) -> dict[str, Any]:
    stats: dict[str, Any] = {"tasks": 0, "full_text": 0, "by_sector": {}}
    for key in cfg.sectors:
        papers = conn.execute(
            """select * from papers where status = 'queued' and sector = %s
                order by triage_score desc, publication_date desc nulls last limit %s for update skip locked""",
            (key, cfg.analyze_per_sector),
        ).fetchall()
        for p in papers:
            if agent_queue.has_active(conn, STAGE, f"paper:{p['id']}"):
                continue
            full_text = None
            if cfg.use_full_text and p["has_grobid"] and p["openalex_id"] and oa is not None:
                try:
                    xml = oa.get_grobid_text(p["openalex_id"])
                    full_text = tei_to_text(xml, cfg.full_text_max_chars) if xml else None
                except Exception as e:
                    log.info("Full text unavailable for %s: %s", p["openalex_id"], e)
            sector = sectors.get(key)
            quant = sector.kind == "quant"
            payload = {
                "prompt_version": prompts.FINANCE_ANALYSIS_VERSION if quant else prompts.ANALYSIS_VERSION,
                "instructions": prompts.render(
                    prompts.FINANCE_ANALYSIS_SYSTEM if quant else prompts.ANALYSIS_SYSTEM, cfg.country, key)
                + QUOTE_RULES.format(n=cfg.min_quote_chars),
                "input": build_user_prompt(p, _institutions(conn, p["id"]), full_text),
                "output_schema": schemas.FINANCE_ANALYSIS if quant else schemas.ANALYSIS,
                "quant": quant,
                "abstract": p["abstract"] or "",
                "full_text": full_text,
            }
            if agent_queue.enqueue(conn, cfg, STAGE, f"paper:{p['id']}", [p["id"]], payload, sector=key,
                                   priority=float(p["triage_score"] or 0)):
                conn.execute("update papers set status='analyzing', updated_at=now() where id=%s", (p["id"],))
                stats["tasks"] += 1
                stats["full_text"] += bool(full_text)
                stats["by_sector"][key] = stats["by_sector"].get(key, 0) + 1
        conn.commit()
    return stats


QUOTE_RULES = (
    "\n\nQUOTES (checked by code): every evidence_quote must be copied character-for-character from the "
    "<abstract> or <full_text> above, at least {n} characters, one contiguous passage (no paraphrase, no "
    "joining sentences). A quote that cannot be found, is too short, or drops a negation is marked "
    "unverified and lowers the paper's evidence confidence. If no passage supports a claim, use an empty "
    "evidence_quote and say so in limitations."
)


def check(payload: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors = []
    if not result.get("findings"):
        errors.append("findings: at least one finding is required (record what the paper does report)")
    return errors


def confidence_cap(verified: int, total: int, used_full_text: bool) -> float:
    """Confidence can't exceed what the verified quotes support: 0.3 with none verified, rising linearly
    to 1.0 when all are; abstract-only analyses are capped at 0.6."""
    cap = 0.3 + 0.7 * (verified / total) if total else 0.3
    if not used_full_text:
        cap = min(cap, 0.6)
    return round(cap, 3)


def ingest(conn, cfg, task) -> dict[str, Any]:
    p = conn.execute("select * from papers where id=%s", (task["subject_ids"][0],)).fetchone()
    if p is None:
        raise ValueError("paper no longer exists")
    if p["status"] != "analyzing":
        raise ValueError(f"paper status is {p['status']!r}, expected 'analyzing'")
    r = store_analysis(conn, cfg, p, task["payload"], task["result"], worker=task["claimed_by"] or "grok-bot")
    return {"analyzed": 1, "by_sector": {p["sector"]: 1}, "full_text": int(r["full_text"]),
            "findings": r["findings"], "verified_quotes": r["verified"]}


def give_up(conn, task) -> None:
    conn.execute(
        """update papers set status='failed', metadata = metadata || %s where id = any(%s) and status='analyzing'""",
        (Jsonb({"analysis_error": task["last_error"]}), task["subject_ids"]),
    )


def store_analysis(conn, cfg, p: dict[str, Any], payload: dict[str, Any], out: dict[str, Any],
                   worker: str) -> dict[str, Any]:
    full_text = payload.get("full_text")
    quant = bool(payload.get("quant"))
    abstract = payload.get("abstract") or p["abstract"] or ""
    confidence = clamp01(out.get("evidence_confidence"))
    findings = out.get("findings") or []
    verified = 0
    with conn.transaction():
        analysis_id = conn.execute(
            """insert into paper_analyses (paper_id, model, prompt_version, used_full_text, problem, methods,
                 novelty_claim, readiness_level, readiness_evidence, applications_to_investigate,
                 missing_information, evidence_confidence, raw)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (
                p["id"], worker, payload.get("prompt_version"), bool(full_text),
                out.get("hypothesis") if quant else out.get("problem"),
                out.get("signal_definition") if quant else out.get("methods"),
                out.get("reported_performance") if quant else out.get("novelty_claim"),
                None if quant else out.get("readiness_level"),
                Jsonb(out.get("readiness_evidence") or []), Jsonb(out.get("applications_to_investigate") or []),
                Jsonb(out.get("missing_information") or []), confidence, Jsonb(out),
            ),
        ).fetchone()["id"]
        for f in findings:
            ok, where, why = verify_quote(f.get("evidence_quote"), abstract, full_text, cfg.min_quote_chars)
            verified += ok
            conn.execute(
                """insert into findings (paper_id, analysis_id, claim, evidence_quote, evidence_location,
                     quote_verified, quote_source, quote_check, measured_or_inferred, limitations)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    p["id"], analysis_id, f.get("claim") or "", f.get("evidence_quote") or None,
                    f.get("location"), ok, where, why, f.get("measured_or_inferred"),
                    Jsonb(f.get("limitations") or []),
                ),
            )
        # unverifiable quotes erode confidence: the analysis may be leaning on invented text
        cap = confidence_cap(verified, len(findings), bool(full_text))
        if confidence > cap:
            confidence = cap
            conn.execute("update paper_analyses set evidence_confidence=%s where id=%s", (confidence, analysis_id))
        if quant:
            red_flags = out.get("red_flags") or []
            score = testability_score(
                out_of_sample=bool(out.get("out_of_sample_tested")),
                costs_modelled=bool(out.get("transaction_costs_modelled")),
                liquidity_considered=bool(out.get("liquidity_considered")),
                replication=out.get("replication_difficulty") or "hard",
                verified_quotes=verified,
                evidence_confidence=confidence,
                red_flags=len(red_flags),
            )
            conn.execute(
                """insert into finance_hypotheses (paper_id, analysis_id, hypothesis, signal_definition,
                     instruments_and_market, data_period, data_needed, out_of_sample_tested,
                     transaction_costs_modelled, liquidity_considered, reported_performance, red_flags,
                     replication_difficulty, testability_score, rubric_version, recommended_action)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    p["id"], analysis_id, out.get("hypothesis") or p["title"], out.get("signal_definition"),
                    out.get("instruments_and_market"), out.get("data_period"), Jsonb(out.get("data_needed") or []),
                    bool(out.get("out_of_sample_tested")), bool(out.get("transaction_costs_modelled")),
                    bool(out.get("liquidity_considered")), out.get("reported_performance"), Jsonb(red_flags),
                    out.get("replication_difficulty"), score, FINANCE_RUBRIC_VERSION, finance_action(score),
                ),
            )
            # the quant track ends here: no product opportunities are generated from finance papers
            conn.execute("update papers set opportunities_generated_at=now() where id=%s", (p["id"],))
        conn.execute(
            "update papers set status='analyzed', analyzed_at=now(), updated_at=now() where id=%s", (p["id"],)
        )
    return {"findings": len(findings), "verified": verified, "full_text": bool(full_text), "confidence": confidence}


