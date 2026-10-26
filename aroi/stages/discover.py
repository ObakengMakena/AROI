"""Stage 1 — Research Scout. Pulls new works from OpenAlex and upserts deduplicated paper rows,
for every enabled sector.

Two nets per sector:
  * The sweep: every new work with an author at an institution in the target country whose primary
    topic falls in the sector's OpenAlex fields/subfields (see aroi/sectors.py). Cheap list+filter calls; catches papers that never
    use your keywords.
  * Keyword searches from `search_queries`: targeted boolean searches across neighbouring countries,
    for research that could be adapted to the target market.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import sectors
from ..db import get_state, set_state
from ..normalize import parse_work
from ..openalex import OpenAlexBudgetError, OpenAlexError, OpenAlexPlanError

log = logging.getLogger(__name__)


def _year(d: str | None) -> int | None:
    try:
        return int(d[:4]) if d else None
    except ValueError:
        return None


def upsert_paper(conn: psycopg.Connection, p: dict[str, Any], found_by: str, sector: str) -> tuple[str, bool]:
    """Insert or update one paper. Returns (paper_id, is_new). Dedup order: OpenAlex id, DOI,
    then title fingerprint + publication year."""
    if not p["title"] or len(p["title_fingerprint"]) < 8:
        raise ValueError("work has no usable title")
    row = conn.execute(
        """select id from papers
            where openalex_id = %(oa)s
               or (%(doi)s::text is not null and doi = %(doi)s)
               or (title_fingerprint = %(fp)s
                   and extract(year from publication_date) is not distinct from %(yr)s::int)
            limit 1""",
        {"oa": p["openalex_id"], "doi": p["doi"], "fp": p["title_fingerprint"], "yr": _year(p["publication_date"])},
    ).fetchone()

    metadata = {"keywords": p["keywords"], "funders": p["funders"]}
    if row:
        paper_id = row["id"]
        conn.execute(
            """update papers set
                 openalex_id = coalesce(openalex_id, %(oa)s),
                 doi = coalesce(doi, %(doi)s),
                 abstract = coalesce(abstract, %(abstract)s),
                 oa_status = %(oa_status)s,
                 oa_url = coalesce(%(oa_url)s, oa_url),
                 has_grobid = has_grobid or %(has_grobid)s,
                 cited_by_count = %(cited)s,
                 found_by = (select array(select distinct unnest(found_by || array[%(found_by)s]::text[]))),
                 sectors = (select array(select distinct unnest(sectors || array[%(sector)s]::text[]))),
                 updated_at = now()
               where id = %(id)s""",
            {
                "id": paper_id, "oa": p["openalex_id"], "doi": p["doi"], "abstract": p["abstract"],
                "oa_status": p["oa_status"], "oa_url": p["oa_url"], "has_grobid": p["has_grobid"],
                "cited": p["cited_by_count"], "found_by": found_by, "sector": sector,
            },
        )
        is_new = False
    else:
        paper_id = conn.execute(
            """insert into papers (openalex_id, doi, title, title_fingerprint, abstract, publication_date,
                 work_type, language, source_name, landing_url, oa_status, oa_url, has_grobid, primary_field,
                 primary_subfield, primary_topic, countries, authors, cited_by_count, found_by, metadata, status, sector, sectors)
               values (%(openalex_id)s, %(doi)s, %(title)s, %(title_fingerprint)s, %(abstract)s,
                 %(publication_date)s, %(work_type)s, %(language)s, %(source_name)s, %(landing_url)s,
                 %(oa_status)s, %(oa_url)s, %(has_grobid)s, %(primary_field)s, %(primary_subfield)s,
                 %(primary_topic)s, %(countries)s, %(authors)s, %(cited_by_count)s, array[%(found_by)s],
                 %(metadata)s, %(status)s, %(sector)s, array[%(sector)s])
               returning id""",
            {
                **p,
                "authors": Jsonb(p["authors"]),
                "metadata": Jsonb(metadata),
                "found_by": found_by,
                "sector": sector,
                # no abstract -> nothing to analyse cheaply; keep the record but skip triage
                "status": "new" if p["abstract"] and len(p["abstract"]) >= 200 else "triaged_out",
            },
        ).fetchone()["id"]
        if not p["abstract"] or len(p["abstract"]) < 200:
            conn.execute(
                "update papers set triage_reason = 'No usable abstract in OpenAlex' where id = %s", (paper_id,)
            )
        is_new = True

    for inst in p["institutions"]:
        inst_id = conn.execute(
            """insert into institutions (openalex_id, ror, name, country_code, type)
               values (%(openalex_id)s, %(ror)s, %(name)s, %(country_code)s, %(type)s)
               on conflict (openalex_id) do update set name = excluded.name
               returning id""",
            inst,
        ).fetchone()["id"]
        conn.execute(
            "insert into paper_institutions (paper_id, institution_id) values (%s, %s) on conflict do nothing",
            (paper_id, inst_id),
        )
    return paper_id, is_new


def _ingest(conn, works, found_by: str, sector: str, stats: dict[str, int], seen_dates: list | None = None,
            date_field: str = "publication_date") -> int:
    n = 0
    for w in works:
        n += 1
        if seen_dates is not None and (w.get(date_field) or w.get("publication_date")):
            seen_dates.append(str(w.get(date_field) or w.get("publication_date"))[:10])
        try:
            with conn.transaction():
                _, is_new = upsert_paper(conn, parse_work(w), found_by, sector)
            stats["new" if is_new else "updated"] += 1
        except Exception as e:  # one malformed record must not sink the run
            stats["skipped"] += 1
            log.warning("Skipped work %s: %s", w.get("id"), e)
    return n


def _date_filter(cfg) -> tuple[str, str]:
    """(filter prefix, record field). created_date catches works OpenAlex indexes long after their
    publication date (theses and back-filled journals), which a publication-date window misses."""
    if cfg.discovery_date_field == "publication_date":
        return "from_publication_date", "publication_date"
    return "from_created_date", "created_date"


def _sweep(conn, cfg, oa, sector, stats) -> None:
    today = date.today()
    key = f"sweep_last_date:{cfg.country}:{sector.key}"
    last = get_state(conn, key)
    since = (
        date.fromisoformat(last) - timedelta(days=cfg.sweep_overlap_days)
        if last
        else today - timedelta(days=cfg.first_run_lookback_days)
    )
    prefix, field = _date_filter(cfg)
    flt = ",".join(
        [
            f"institutions.country_code:{cfg.country.lower()}",
            sector.sweep_filter,
            f"{prefix}:{since.isoformat()}",
            "has_abstract:true",
            f"type:{cfg.sweep_work_types}",
        ]
    )
    log.info("[%s] sweep filter: %s", sector.key, flt)
    seen_dates: list[str] = []
    stats["sweep_seen"] += _ingest(
        conn,
        oa.iter_works(flt, max_results=cfg.max_sweep_pages * 100, sort=f"{field}:asc"),
        f"sweep:{sector.key}", sector.key, stats, seen_dates, field,
    )
    if getattr(oa, "last_truncated", False):
        # results are sorted oldest first, so everything up to the last date we saw is covered; resume
        # there next run instead of jumping to today and silently skipping the rest
        resume = max(seen_dates) if seen_dates else (last or since.isoformat())
        log.warning("[%s] sweep truncated at %s of %s results; resuming from %s next run",
                    sector.key, len(seen_dates), oa.last_count, resume)
        stats.setdefault("sweep_truncated", []).append(sector.key)
        # never move backwards, and never stall: if a single day overflows the cap, step a day forward
        if last and resume <= last:
            resume = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            log.error("[%s] one day holds more than %s results: raise MAX_SWEEP_PAGES or results were skipped",
                      sector.key, cfg.max_sweep_pages * 100)
        set_state(conn, key, min(resume, today.isoformat()))
    else:
        set_state(conn, key, today.isoformat())
    conn.commit()


def _keyword_queries(conn, cfg, oa, sector, stats) -> None:
    today = date.today()
    queries = conn.execute(
        "select * from search_queries where active and sector = %s order by theme", (sector.key,)
    ).fetchall()
    for q in queries:
        q_since = (
            (q["last_run_at"].date() - timedelta(days=cfg.sweep_overlap_days))
            if q["last_run_at"]
            else today - timedelta(days=cfg.first_run_lookback_days)
        )
        flt = f"{_date_filter(cfg)[0]}:{q_since.isoformat()},has_abstract:true"
        if q["extra_filter"]:
            flt += "," + q["extra_filter"]
        before = stats["new"]
        try:
            seen = _ingest(
                conn,
                oa.iter_works(flt, search=q["query_text"], per_page=50, max_results=cfg.keyword_results_per_query),
                f"query:{sector.key}:{q['theme'] or q['query_text'][:40]}",
                sector.key,
                stats,
            )
        except (OpenAlexBudgetError, OpenAlexPlanError):
            conn.rollback()
            raise
        except OpenAlexError as e:
            conn.rollback()
            stats["query_errors"] += 1
            log.warning("[%s] query %r failed, skipping: %s", sector.key, q["query_text"][:60], e)
            continue
        conn.execute(
            "update search_queries set last_run_at = %s, results_last_run = %s where id = %s",
            (datetime.now(timezone.utc), seen, q["id"]),
        )
        conn.commit()
        stats["queries"] += 1
        log.info("[%s] query %r: %s results, %s new", sector.key, q["query_text"][:60], seen, stats["new"] - before)


def _fallback_date_field(cfg, stats, e) -> bool:
    """created_date filters/sorts need a paid OpenAlex plan. Fall back to publication_date once."""
    if cfg.discovery_date_field == "publication_date":
        return False
    log.error("OpenAlex refused created_date (%s). Falling back to DISCOVERY_DATE_FIELD=publication_date; "
              "set that variable to silence this.", e)
    cfg.discovery_date_field = "publication_date"
    stats["date_field_fallback"] = "publication_date"
    stats["problems"].append("created_date filters need a paid OpenAlex plan; fell back to publication_date")
    return True


def _with_fallback(fn, conn, cfg, stats):
    try:
        fn()
    except OpenAlexPlanError as e:
        conn.rollback()
        if not _fallback_date_field(cfg, stats, e):
            raise
        fn()


def run(conn: psycopg.Connection, cfg, oa) -> dict[str, Any]:
    """Never a silent no-op: anything that makes discovery see nothing for a config/API reason goes into
    stats['problems'], and stats['fatal'] (which fails the pipeline run) when no sweep succeeded."""
    stats: dict[str, Any] = {"new": 0, "updated": 0, "skipped": 0, "queries": 0, "query_errors": 0, "sweep_seen": 0,
                             "sweeps_ok": 0, "problems": [], "fatal": []}
    per_sector: dict[str, int] = {}
    if not cfg.sectors:
        stats["fatal"].append("no sectors enabled (check the SECTORS variable)")
    if not getattr(oa, "api_key", "x"):
        stats["problems"].append("OPENALEX_API_KEY is not set: keyless requests share a tiny daily budget")
    n_queries = conn.execute(
        "select count(*) n from search_queries where active and sector = any(%s)", (list(cfg.sectors),)
    ).fetchone()["n"]
    if n_queries == 0:
        stats["problems"].append("no active search_queries for the enabled sectors (load sql/seed.sql)")
    for key in cfg.sectors:
        sector = sectors.get(key)
        before = stats["new"]
        try:
            _with_fallback(lambda: _sweep(conn, cfg, oa, sector, stats), conn, cfg, stats)
            stats["sweeps_ok"] += 1
        except OpenAlexBudgetError as e:
            conn.rollback()
            log.error("OpenAlex budget exhausted during %s sweep; stopping discovery: %s", key, e)
            stats["fatal"].append(f"OpenAlex daily budget exhausted during the {key} sweep: {str(e)[:200]}")
            break
        except OpenAlexError as e:
            conn.rollback()
            log.error("[%s] sweep failed: %s", key, e)
            stats.setdefault("sweep_errors", []).append(key)
            stats["problems"].append(f"{key} sweep failed: {str(e)[:200]}")
        try:
            _with_fallback(lambda: _keyword_queries(conn, cfg, oa, sector, stats), conn, cfg, stats)
        except OpenAlexBudgetError as e:
            conn.rollback()
            log.error("OpenAlex budget exhausted during %s queries; stopping discovery: %s", key, e)
            stats["fatal"].append(f"OpenAlex daily budget exhausted during {key} keyword queries")
            break
        except OpenAlexError as e:
            conn.rollback()
            log.error("[%s] keyword queries stopped: %s", key, e)
            stats["problems"].append(f"{key} keyword queries stopped: {str(e)[:200]}")
        per_sector[key] = stats["new"] - before
    if cfg.sectors and stats["sweeps_ok"] == 0 and not stats["fatal"]:
        stats["fatal"].append("every sector sweep failed; see problems")
    if stats["sweeps_ok"] and stats["sweep_seen"] == 0 and stats["queries"] == 0:
        stats["problems"].append("discovery ran but OpenAlex returned 0 works for every sweep and query")
    stats["new_by_sector"] = per_sector
    stats["openalex_cost_usd"] = round(oa.cost_usd, 4)
    for p in stats["problems"] + stats["fatal"]:
        log.warning("Discovery problem: %s", p)
    log.info("Discovery: %s", json.dumps(stats))
    return stats
