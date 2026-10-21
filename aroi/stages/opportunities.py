"""Stage 4 — Opportunity Generator. Turns verified findings into at most three commercial hypotheses
per paper, and merges near-duplicate hypotheses from different papers into one opportunity record."""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import prompts, schemas, sectors
from ..llm import BudgetExceeded
from ..scoring import clamp01

log = logging.getLogger(__name__)

PUBLIC_TYPES = {"education", "government", "facility", "nonprofit", "healthcare", "archive"}


def ip_pathway(institutions: list[dict[str, Any]], country: str, sector_key: str | None = None) -> str:
    base = _ip_base(institutions, country)
    note = sectors.get(sector_key).ip_note.get(country.upper()) if sector_key else None
    return f"{base} {note}" if note else base


def _ip_base(institutions: list[dict[str, Any]], country: str) -> str:
    local = [i for i in institutions if (i["country_code"] or "").upper() == country.upper()]
    public_local = [i for i in local if (i["type"] or "") in PUBLIC_TYPES]
    if country.upper() == "ZA" and public_local:
        names = ", ".join(sorted({i["name"] for i in public_local}))
        return (
            f"Publicly funded SA research: IP is by default owned by {names} under the IPR-PFRD Act. "
            "Route: contact the technology transfer office to ask whether it is protected and licensable "
            "(exclusive licence or spin-out)."
        )
    if local:
        return f"Originates at {', '.join(sorted({i['name'] for i in local}))}; check its IP policy and any patents."
    names = ", ".join(sorted({i["name"] for i in institutions})) or "unknown institutions"
    return (
        f"Research from outside {country} ({names}). Check patents in {country}; unpatented methods may be usable "
        "locally, but confirm with an IP attorney before relying on it."
    )


def _findings_block(findings: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    refs: dict[str, Any] = {}
    lines = []
    for i, f in enumerate(findings, 1):
        ref = f"F{i}"
        refs[ref] = f["id"]
        flag = "verified quote" if f["quote_verified"] else "UNVERIFIED quote"
        lines.append(
            f"{ref} [{f['measured_or_inferred'] or '?'}; {flag}] {f['claim']}"
            + (f'\n    quote: "{f["evidence_quote"]}" ({f["evidence_location"]})' if f["evidence_quote"] else "")
            + (f"\n    limitations: {'; '.join(f['limitations'])}" if f["limitations"] else "")
        )
    return "\n".join(lines), refs


def generate_for_paper(conn, cfg, llm, p: dict[str, Any]) -> dict[str, int]:
    analysis = conn.execute(
        "select * from paper_analyses where paper_id=%s order by created_at desc limit 1", (p["id"],)
    ).fetchone()
    findings = conn.execute(
        "select * from findings where analysis_id=%s order by quote_verified desc, created_at", (analysis["id"],)
    ).fetchall()
    institutions = conn.execute(
        """select i.name, i.country_code, i.type from paper_institutions pi
             join institutions i on i.id = pi.institution_id where pi.paper_id=%s""",
        (p["id"],),
    ).fetchall()
    findings_text, refs = _findings_block(findings)

    user = (
        f"<paper>\nTitle: {p['title']}\nPublished: {p['publication_date']}\n"
        f"Institutions: {', '.join(i['name'] + ' (' + (i['country_code'] or '?') + ')' for i in institutions)}\n"
        f"Problem studied: {analysis['problem']}\nSetting: {analysis['raw'].get('setting', '')}\n"
        f"Readiness: {analysis['readiness_level']}\n"
        f"Analyst's evidence confidence: {analysis['evidence_confidence']}\n"
        f"What the paper does not establish: {'; '.join(analysis['missing_information'])}\n</paper>\n\n"
        f"<findings>\n{findings_text}\n</findings>\n\n"
        f"<analyst_application_ideas>\n{'; '.join(analysis['applications_to_investigate'])}\n</analyst_application_ideas>"
    )
    sector_key = p["sector"] or "agriculture"
    out = llm.structured(
        stage=f"opportunities:{sector_key}",
        model=cfg.analysis_model,
        system=prompts.render(prompts.OPPORTUNITY_SYSTEM, cfg.country, sector_key),
        user=user,
        schema=schemas.OPPORTUNITIES,
        prompt_version=prompts.OPPORTUNITY_VERSION,
        max_tokens=5000,
        paper_id=p["id"],
    )
    created = merged = 0
    pathway = ip_pathway(institutions, cfg.country, sector_key)
    with conn.transaction():
        for o in (out.get("opportunities") or [])[:3]:
            title = (o.get("title") or "").strip()
            if not title:
                continue
            finding_ids = [refs[r] for r in o.get("enabling_finding_refs") or [] if r in refs]
            match = conn.execute(
                """select id, similarity(title, %(t)s) as sim from opportunities
                    where sector=%(s)s and target_country=%(c)s and similarity(title, %(t)s) >= %(min)s
                    order by sim desc limit 1""",
                {"t": title, "s": sector_key, "c": cfg.country, "min": cfg.merge_similarity},
            ).fetchone()
            if match:
                conn.execute(
                    """insert into opportunity_papers (opportunity_id, paper_id, relationship, supporting_finding_ids)
                       values (%s,%s,'supporting',%s) on conflict do nothing""",
                    (match["id"], p["id"], finding_ids),
                )
                # several institutions may now hold relevant IP: keep every pathway
                conn.execute(
                    """update opportunities set updated_at=now(),
                         ip_pathway = case when position(%(p)s in coalesce(ip_pathway,'')) > 0 then ip_pathway
                                           else concat_ws(E'\n', ip_pathway, %(p)s) end
                        where id=%(id)s""",
                    {"p": pathway, "id": match["id"]},
                )
                merged += 1
                continue
            description = (o.get("proposed_solution") or "") + (
                f"\n\nWhy current options fall short: {o['why_existing_solutions_fall_short']}"
                if o.get("why_existing_solutions_fall_short")
                else ""
            )
            opp_id = conn.execute(
                """insert into opportunities (title, description, sector, target_country, customer_problem,
                     target_customer, proposed_solution, revenue_model, commercial_assumptions, key_risks,
                     ip_pathway, next_experiment, generator_confidence)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (
                    title[:300], description, sector_key, cfg.country, o.get("customer_problem"),
                    o.get("target_customer"), o.get("proposed_solution"), o.get("revenue_model"),
                    Jsonb((o.get("commercial_assumptions") or []) + [f"Delivery needs: {d}" for d in o.get("delivery_requirements") or []]),
                    Jsonb(o.get("key_risks") or []), pathway, o.get("cheapest_disproving_experiment"),
                    clamp01(o.get("confidence")),
                ),
            ).fetchone()["id"]
            conn.execute(
                """insert into opportunity_papers (opportunity_id, paper_id, relationship, supporting_finding_ids)
                   values (%s,%s,'origin',%s)""",
                (opp_id, p["id"], finding_ids),
            )
            created += 1
        conn.execute("update papers set opportunities_generated_at=now() where id=%s", (p["id"],))
    return {"created": created, "merged": merged}


def run(conn: psycopg.Connection, cfg, llm) -> dict[str, Any]:
    product_sectors = [k for k in cfg.sectors if sectors.get(k).kind == "product"]
    papers = conn.execute(
        """select p.* from papers p
             join lateral (select evidence_confidence from paper_analyses a where a.paper_id = p.id
                           order by created_at desc limit 1) a on true
            where p.status = 'analyzed' and p.opportunities_generated_at is null
              and a.evidence_confidence >= %s and p.sector = any(%s)
            order by (%s = any(p.countries)) desc, a.evidence_confidence desc, p.triage_score desc""",
        (cfg.min_evidence_for_opportunities, product_sectors, cfg.country.upper()),
    ).fetchall()
    # papers below the evidence bar are marked done so they aren't re-checked forever
    conn.execute(
        """update papers p set opportunities_generated_at = now()
            where p.status='analyzed' and p.opportunities_generated_at is null
              and not exists (select 1 from paper_analyses a where a.paper_id=p.id and a.evidence_confidence >= %s)""",
        (cfg.min_evidence_for_opportunities,),
    )
    conn.commit()
    stats = {"papers": 0, "created": 0, "merged": 0, "failed": 0}
    for p in papers:
        try:
            r = generate_for_paper(conn, cfg, llm, p)
        except BudgetExceeded:
            raise
        except Exception as e:
            conn.rollback()
            stats["failed"] += 1
            log.warning("Opportunity generation failed for %s: %s", p["id"], e)
            continue
        conn.commit()
        stats["papers"] += 1
        stats["created"] += r["created"]
        stats["merged"] += r["merged"]
    log.info("Opportunities: %s", stats)
    return stats
