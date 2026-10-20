"""Stage 2 — cheap relevance filter. A small model reads titles + abstracts in batches and scores the
commercial signal 0-10; only papers at or above TRIAGE_THRESHOLD move on to full analysis."""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from .. import prompts, schemas
from ..llm import BudgetExceeded

log = logging.getLogger(__name__)


def _paper_block(ref: str, p: dict[str, Any]) -> str:
    abstract = (p["abstract"] or "")[:2500]
    return (
        f'<paper ref="{ref}">\nTitle: {p["title"]}\nType: {p["work_type"]} | Topic: {p["primary_topic"]} '
        f'| Published: {p["publication_date"]}\nAbstract: {abstract}\n</paper>'
    )


def run(conn: psycopg.Connection, cfg, llm) -> dict[str, Any]:
    papers = conn.execute(
        """select id, title, abstract, work_type, primary_topic, publication_date
             from papers where status = 'new' order by discovered_at limit %s""",
        (cfg.triage_max_per_run,),
    ).fetchall()
    stats = {"triaged": 0, "queued": 0, "dropped": 0, "errors": 0}
    system = prompts.render(prompts.TRIAGE_SYSTEM, cfg.country)

    for start in range(0, len(papers), cfg.triage_batch_size):
        batch = papers[start : start + cfg.triage_batch_size]
        refs = {f"P{i + 1}": p for i, p in enumerate(batch)}
        user = "Screen these papers.\n\n" + "\n\n".join(_paper_block(r, p) for r, p in refs.items())
        try:
            out = llm.structured(
                stage="triage",
                model=cfg.triage_model,
                system=system,
                user=user,
                schema=schemas.TRIAGE,
                prompt_version=prompts.TRIAGE_VERSION,
                max_tokens=200 + 160 * len(batch),
            )
        except BudgetExceeded:
            raise
        except Exception as e:
            stats["errors"] += 1
            log.warning("Triage batch failed: %s", e)
            continue

        seen = set()
        for r in out.get("results", []):
            p = refs.get(r.get("ref"))
            if not p or r["ref"] in seen:
                continue
            seen.add(r["ref"])
            score = max(0, min(10, int(r.get("commercial_signal", 0))))
            relevant = r.get("sector") != "not_relevant"
            status = "queued" if relevant and score >= cfg.triage_threshold else "triaged_out"
            conn.execute(
                """update papers set status=%s, triage_score=%s, triage_sector=%s, triage_reason=%s, updated_at=now()
                    where id=%s and status='new'""",
                (status, score, r.get("sector"), (r.get("reason") or "")[:500], p["id"]),
            )
            stats["triaged"] += 1
            stats["queued" if status == "queued" else "dropped"] += 1
        conn.commit()
        missing = set(refs) - seen
        if missing:
            log.warning("Triage returned no verdict for %s; they stay 'new' for the next run", sorted(missing))

    log.info("Triage: %s", stats)
    return stats
