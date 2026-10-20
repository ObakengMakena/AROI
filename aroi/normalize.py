"""Pure functions that turn raw OpenAlex records into clean paper rows. No I/O here."""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the original text."""
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions))


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d or None


def short_openalex_id(oa_id: str | None) -> str | None:
    if not oa_id:
        return None
    return oa_id.rstrip("/").rsplit("/", 1)[-1]


def title_fingerprint(title: str) -> str:
    """Lower-case, accent-free, alphanumeric-only title used as a fallback dedup key."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"<[^>]+>", " ", t)  # stray HTML/MathML tags in titles
    t = re.sub(r"[^a-z0-9]+", "", t.lower())
    return t[:200]


def _short(oa_url: str | None) -> str | None:
    return oa_url.rsplit("/", 1)[-1] if oa_url else None


def parse_work(work: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenAlex work to the columns of the `papers` table plus an institutions list."""
    title = (work.get("title") or work.get("display_name") or "").strip()
    primary_topic = work.get("primary_topic") or {}
    best_oa = work.get("best_oa_location") or {}
    open_access = work.get("open_access") or {}
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    has_content = work.get("has_content") or {}

    authors: list[dict[str, Any]] = []
    institutions: dict[str, dict[str, Any]] = {}
    countries: set[str] = set()
    for a in work.get("authorships") or []:
        inst_names = []
        for inst in a.get("institutions") or []:
            if not inst.get("id"):
                continue
            cc = (inst.get("country_code") or "").upper() or None
            if cc:
                countries.add(cc)
            institutions[inst["id"]] = {
                "openalex_id": _short(inst["id"]),
                "ror": inst.get("ror"),
                "name": inst.get("display_name") or "Unknown institution",
                "country_code": cc,
                "type": inst.get("type"),
            }
            inst_names.append(inst.get("display_name"))
        authors.append(
            {
                "name": (a.get("author") or {}).get("display_name"),
                "orcid": (a.get("author") or {}).get("orcid"),
                "position": a.get("author_position"),
                "corresponding": bool(a.get("is_corresponding")),
                "institutions": inst_names,
            }
        )

    return {
        "openalex_id": short_openalex_id(work.get("id")),
        "doi": normalize_doi(work.get("doi")),
        "title": title,
        "title_fingerprint": title_fingerprint(title),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "publication_date": work.get("publication_date"),
        "work_type": work.get("type"),
        "language": work.get("language"),
        "source_name": source.get("display_name"),
        "landing_url": primary_location.get("landing_page_url") or work.get("doi"),
        "oa_status": open_access.get("oa_status"),
        "oa_url": best_oa.get("pdf_url") or best_oa.get("landing_page_url") or open_access.get("oa_url"),
        "has_grobid": bool(has_content.get("grobid_xml")),
        "primary_field": (primary_topic.get("field") or {}).get("display_name"),
        "primary_subfield": (primary_topic.get("subfield") or {}).get("display_name"),
        "primary_topic": primary_topic.get("display_name"),
        "countries": sorted(countries),
        "authors": authors,
        "cited_by_count": work.get("cited_by_count"),
        "keywords": [k.get("display_name") for k in (work.get("keywords") or []) if k.get("display_name")],
        "funders": [f.get("display_name") for f in (work.get("funders") or []) if f.get("display_name")],
        "institutions": list(institutions.values()),
    }


# ---------------------------------------------------------------------------
# Full text (GROBID TEI XML from the OpenAlex content API)
# ---------------------------------------------------------------------------
_TEI = "{http://www.tei-c.org/ns/1.0}"


def tei_to_text(xml: str, max_chars: int = 45000) -> str | None:
    """Extract section headings and paragraphs from a GROBID TEI document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    body = root.find(f".//{_TEI}body")
    if body is None:
        return None
    parts: list[str] = []
    for div in body.iter(f"{_TEI}div"):
        head = div.find(f"{_TEI}head")
        if head is not None and (head.text or "").strip():
            parts.append(f"\n## {''.join(head.itertext()).strip()}")
        for p in div.findall(f"{_TEI}p"):
            txt = " ".join("".join(p.itertext()).split())
            if txt:
                parts.append(txt)
    # tables/figure captions often carry the actual numbers
    for fig in body.iter(f"{_TEI}figure"):
        desc = fig.find(f"{_TEI}figDesc")
        if desc is not None:
            txt = " ".join("".join(desc.itertext()).split())
            if txt:
                parts.append(f"[Figure/Table] {txt}")
    text = "\n".join(parts).strip()
    if not text:
        return None
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Hallucination guard: does a quoted passage really appear in the source?
# ---------------------------------------------------------------------------
def _squash(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    s = re.sub(r"[\"'‘’“”]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_in_source(quote: str | None, source: str | None) -> bool:
    if not quote or not source:
        return False
    q = _squash(quote).strip(" .…")
    if len(q) < 12:
        return False
    src = _squash(source)
    if q in src:
        return True
    # tolerate an ellipsis joining two exact fragments
    frags = [f.strip(" .") for f in re.split(r"\.\.\.|…", q) if len(f.strip(" .")) >= 12]
    return bool(frags) and all(f in src for f in frags)
