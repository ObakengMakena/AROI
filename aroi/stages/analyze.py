"""Stage 3 — Scientific Analyst. Extracts structured findings from the highest-signal queued papers,
using GROBID full text from OpenAlex when available, and checks every quote against the source."""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import prompts, schemas
from ..llm import BudgetExceeded
from ..normalize import quote_in_source, tei_to_text
from ..scoring import clamp01

log = logging.getLogger(__name__)


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


def analyze_paper(conn, cfg, llm, oa, p: dict[str, Any]) -> dict[str, Any]:
    full_text = None
    if cfg.use_full_text and p["has_grobid"] and p["openalex_id"] and oa is not None:
        try:
            xml = oa.get_grobid_text(p["openalex_id"])
            full_text = tei_to_text(xml, cfg.full_text_max_chars) if xml else None
        except Exception as e:
            log.info("Full text unavailable for %s: %s", p["openalex_id"], e)

    out = llm.structured(
        stage="analysis",
        model=cfg.analysis_model,
        system=prompts.render(prompts.ANALYSIS_SYSTEM, cfg.country),
        user=build_user_prompt(p, _institutions(conn, p["id"]), full_text),
        schema=schemas.ANALYSIS,
        prompt_version=prompts.ANALYSIS_VERSION,
        max_tokens=5000,
        paper_id=p["id"],
    )

    source_text = (p["abstract"] or "") + "\n" + (full_text or "")
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
                p["id"], cfg.analysis_model, prompts.ANALYSIS_VERSION, bool(full_text), out.get("problem"),
                out.get("methods"), out.get("novelty_claim"), out.get("readiness_level"),
                Jsonb(out.get("readiness_evidence") or []), Jsonb(out.get("applications_to_investigate") or []),
                Jsonb(out.get("missing_information") or []), confidence, Jsonb(out),
            ),
        ).fetchone()["id"]
        for f in findings:
            ok = quote_in_source(f.get("evidence_quote"), source_text)
            verified += ok
            conn.execute(
                """insert into findings (paper_id, analysis_id, claim, evidence_quote, evidence_location,
                     quote_verified, measured_or_inferred, limitations)
                   values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    p["id"], analysis_id, f.get("claim") or "", f.get("evidence_quote") or None,
                    f.get("location"), ok, f.get("measured_or_inferred"), Jsonb(f.get("limitations") or []),
                ),
            )
        # unverifiable quotes erode confidence: the analysis may be leaning on invented text
        if findings and verified == 0:
            confidence = min(confidence, 0.3)
            conn.execute("update paper_analyses set evidence_confidence=%s where id=%s", (confidence, analysis_id))
        conn.execute(
            "update papers set status='analyzed', analyzed_at=now(), updated_at=now() where id=%s", (p["id"],)
        )
    return {"findings": len(findings), "verified": verified, "full_text": bool(full_text), "confidence": confidence}


def run(conn: psycopg.Connection, cfg, llm, oa=None) -> dict[str, Any]:
    papers = conn.execute(
        """select * from papers where status = 'queued'
            order by triage_score desc, publication_date desc nulls last limit %s""",
        (cfg.analyze_per_run,),
    ).fetchall()
    stats = {"analyzed": 0, "failed": 0, "full_text": 0, "findings": 0, "verified_quotes": 0}
    for p in papers:
        try:
            r = analyze_paper(conn, cfg, llm, oa, p)
            stats["analyzed"] += 1
            stats["full_text"] += r["full_text"]
            stats["findings"] += r["findings"]
            stats["verified_quotes"] += r["verified"]
        except BudgetExceeded:
            raise
        except Exception as e:
            conn.rollback()
            conn.execute(
                "update papers set status='failed', metadata = metadata || %s where id=%s",
                (Jsonb({"analysis_error": str(e)[:300]}), p["id"]),
            )
            conn.commit()
            stats["failed"] += 1
            log.warning("Analysis failed for %s: %s", p["id"], e)
            continue
        conn.commit()
    log.info("Analysis: %s", stats)
    return stats
