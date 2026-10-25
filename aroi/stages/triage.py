"""Stage 2 — cheap relevance filter, done by a Grok Bot agent through the work queue. The pipeline
batches new papers per sector into triage tasks; the agent scores the commercial signal 0-10 using
the sector's guide; ingest moves papers at or above TRIAGE_THRESHOLD to 'queued'.

A paper found by several sectors' searches lists all of them as candidates; the agent picks the best
fit (`sector`), which becomes the paper's primary sector for the rest of the pipeline."""
from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import agent_queue, prompts, schemas, sectors

log = logging.getLogger(__name__)

STAGE = "triage"


def _paper_block(ref: str, p: dict[str, Any]) -> str:
    abstract = (p["abstract"] or "")[:2500]
    cands = p.get("sectors") or [p["sector"]]
    return (
        f'<paper ref="{ref}">\nTitle: {p["title"]}\nType: {p["work_type"]} | Topic: {p["primary_topic"]} '
        f'| Published: {p["publication_date"]}\nCandidate sectors: {", ".join(cands)}\nAbstract: {abstract}\n</paper>'
    )


def _sector_note(keys: list[str]) -> str:
    lines = [
        "\n\nMULTI-SECTOR PAPERS: each paper lists its candidate sectors. Set `sector` to the candidate it fits "
        "best and pick `category` from THAT sector's list (or not_relevant). Categories per sector:"
    ]
    for k in keys:
        lines.append(f"- {k}: {', '.join(sectors.get(k).triage_categories)}")
    return "\n".join(lines)


def enqueue(conn: psycopg.Connection, cfg, oa=None) -> dict[str, Any]:
    stats: dict[str, Any] = {"tasks": 0, "papers": 0}
    for key in cfg.sectors:
        sector = sectors.get(key)
        papers = conn.execute(
            """select id, title, abstract, work_type, primary_topic, publication_date, sector, sectors
                 from papers where status = 'new' and (sector = %s or %s = any(sectors))
                order by discovered_at limit %s for update skip locked""",
            (key, key, cfg.triage_max_per_sector),
        ).fetchall()
        for start in range(0, len(papers), cfg.triage_batch_size):
            batch = papers[start : start + cfg.triage_batch_size]
            refs = {f"P{i + 1}": p for i, p in enumerate(batch)}
            cand_keys = list(dict.fromkeys(
                [key] + [s for p in batch for s in (p["sectors"] or []) if s in sectors.SECTORS]))
            categories = tuple(c for k in cand_keys for c in sectors.get(k).triage_categories)
            instructions = prompts.render(prompts.TRIAGE_SYSTEM, cfg.country, key) + _sector_note(cand_keys)
            payload = {
                "prompt_version": prompts.TRIAGE_VERSION,
                "instructions": instructions,
                "input": "Screen these papers. Return one result per ref.\n\n"
                + "\n\n".join(_paper_block(r, p) for r, p in refs.items()),
                "output_schema": schemas.triage_schema(categories, tuple(cand_keys)),
                "refs": {r: str(p["id"]) for r, p in refs.items()},
                "candidates": {r: list(p["sectors"] or [p["sector"]]) for r, p in refs.items()},
            }
            ids = [p["id"] for p in batch]
            tid = agent_queue.enqueue(conn, cfg, STAGE, "papers:" + ",".join(sorted(map(str, ids))), ids,
                                      payload, sector=key)
            if tid:
                conn.execute("update papers set status='triaging', updated_at=now() where id = any(%s)", (ids,))
                stats["tasks"] += 1
                stats["papers"] += len(batch)
        conn.commit()
    return stats


def check(payload: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors = []
    refs = payload["refs"]
    seen = set()
    for i, r in enumerate(result["results"]):
        ref = r["ref"]
        if ref not in refs:
            errors.append(f"results/{i}/ref: {ref!r} is not one of {sorted(refs)}")
            continue
        if ref in seen:
            errors.append(f"results/{i}/ref: {ref} appears twice")
        seen.add(ref)
        cands = payload["candidates"][ref]
        sec = r.get("sector", cands[0])
        if sec not in cands:
            errors.append(f"results/{i}/sector: {sec!r} is not a candidate sector for {ref} ({cands})")
        elif r["category"] != "not_relevant" and r["category"] not in sectors.get(sec).triage_categories:
            errors.append(f"results/{i}/category: {r['category']!r} is not a {sec} category")
    missing = set(refs) - seen
    if missing:
        errors.append(f"results: missing verdicts for {sorted(missing)} (every paper needs one)")
    return errors


def ingest(conn, cfg, task) -> dict[str, Any]:
    payload, result = task["payload"], task["result"]
    stats: dict[str, Any] = {"triaged": 0, "queued": 0, "dropped": 0, "queued_by_sector": {}}
    seen = set()
    for r in result["results"]:
        pid = payload["refs"].get(r["ref"])
        if not pid or r["ref"] in seen:
            continue
        seen.add(r["ref"])
        sec = r.get("sector") or payload["candidates"][r["ref"]][0]
        score = max(0, min(10, int(r["commercial_signal"])))
        category = r["category"].lower()
        status = "queued" if category != "not_relevant" and score >= cfg.triage_threshold else "triaged_out"
        n = conn.execute(
            """update papers set status=%s, triage_score=%s, triage_sector=%s, triage_reason=%s, sector=%s,
                 updated_at=now() where id=%s and status='triaging'""",
            (status, score, category, r["reason"][:500], sec, pid),
        ).rowcount
        if not n:
            continue
        stats["triaged"] += 1
        stats["queued" if status == "queued" else "dropped"] += 1
        if status == "queued":
            stats["queued_by_sector"][sec] = stats["queued_by_sector"].get(sec, 0) + 1
    # anything without a verdict goes back to the pool
    missing = [pid for ref, pid in payload["refs"].items() if ref not in seen]
    if missing:
        conn.execute("update papers set status='new' where id = any(%s::uuid[]) and status='triaging'", (missing,))
    return stats


def give_up(conn, task) -> None:
    conn.execute(
        """update papers set status='failed', metadata = metadata || %s
            where id = any(%s) and status='triaging'""",
        (Jsonb({"triage_error": task["last_error"]}), task["subject_ids"]),
    )
