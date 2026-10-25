"""Minimal OpenAlex client: cursor paging, retries, budget awareness, GROBID full text.

Pricing (Feb 2026 model): every key gets $1/day free. List+filter calls cost $0.10/1,000,
search calls $1/1,000, content (PDF/GROBID) downloads $10/1,000, single-record lookups free.
The daily sweep therefore uses filters, and keyword search is kept to a few calls per day.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.openalex.org"
CONTENT_BASE = "https://content.openalex.org"

SELECT_FIELDS = ",".join(
    [
        "id", "doi", "title", "display_name", "publication_date", "created_date", "updated_date", "type", "language",
        "primary_location", "open_access", "best_oa_location", "authorships", "primary_topic",
        "keywords", "funders", "cited_by_count", "abstract_inverted_index", "has_content",
    ]
)


class OpenAlexError(RuntimeError):
    pass


class OpenAlexClient:
    def __init__(
        self,
        api_key: str,
        contact_email: str = "",
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
    ) -> None:
        if not api_key:
            log.warning("No OPENALEX_API_KEY set: you only get $0.10/day and cursor paging is disabled.")
        self.api_key = api_key
        self.contact_email = contact_email
        self.max_retries = max_retries
        self.use_select = True
        self.cost_usd = 0.0
        # set by iter_works: meta.count of the last query and whether max_results cut it short
        self.last_count: int | None = None
        self.last_truncated = False
        headers = {"User-Agent": f"AROI/1.0 ({contact_email or 'no-contact-set'})"}
        self.http = httpx.Client(timeout=60, headers=headers, transport=transport, follow_redirects=True)

    # ------------------------------------------------------------------
    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        p = dict(extra)
        if self.api_key:
            p["api_key"] = self.api_key
        if self.contact_email:
            p["mailto"] = self.contact_email
        return p

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(url, params=self._params(params))
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise OpenAlexError(f"Network error calling OpenAlex: {e}") from e
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429:
                body = r.text[:300]
                # Out of daily budget: retrying won't help until midnight UTC.
                if any(w in body.lower() for w in ("budget", "daily", "exhausted")):
                    raise OpenAlexError(f"OpenAlex daily budget exhausted or rate limited: {body}")
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            return r
        raise OpenAlexError(f"OpenAlex request failed after retries: {url}")

    def _track_cost(self, data: dict[str, Any]) -> None:
        try:
            self.cost_usd += float((data.get("meta") or {}).get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------
    def iter_works(
        self,
        filter: str,
        search: str | None = None,
        per_page: int = 100,
        max_results: int = 2000,
        sort: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield works matching `filter` (and optional boolean `search`), cursor-paged."""
        params: dict[str, Any] = {"filter": filter, "per_page": min(per_page, 100), "cursor": "*"}
        if search:
            params["search"] = search
        if sort:
            params["sort"] = sort
        yielded = 0
        self.last_count = None
        self.last_truncated = False
        while True:
            if self.use_select:
                params["select"] = SELECT_FIELDS
            r = self._get(f"{BASE}/works", params)
            if r.status_code == 400 and self.use_select:
                # maybe a field in `select` was renamed; retry once without it
                retry = dict(params)
                retry.pop("select", None)
                r2 = self._get(f"{BASE}/works", retry)
                if r2.status_code == 200:
                    log.warning("OpenAlex rejected the select list; using full records from now on.")
                    self.use_select = False
                    params.pop("select", None)
                r = r2
            if r.status_code != 200:
                raise OpenAlexError(f"OpenAlex {r.status_code}: {r.text[:400]}")
            data = r.json()
            self._track_cost(data)
            if self.last_count is None:
                try:
                    self.last_count = int((data.get("meta") or {}).get("count"))
                except (TypeError, ValueError):
                    self.last_count = None
            results = data.get("results") or []
            for i, w in enumerate(results):
                yield w
                yielded += 1
                if yielded >= max_results:
                    more_here = i < len(results) - 1
                    more_pages = bool((data.get("meta") or {}).get("next_cursor"))
                    self.last_truncated = more_here or (self.last_count or 0) > yielded or (
                        more_pages and self.last_count is None)
                    return
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor or not results:
                return
            params["cursor"] = cursor

    def get_grobid_text(self, openalex_id: str) -> str | None:
        """Download the GROBID TEI XML for a work ($0.01 each). Returns raw XML or None."""
        r = self._get(f"{CONTENT_BASE}/works/{openalex_id}.grobid-xml", {})
        if r.status_code != 200:
            log.info("No GROBID full text for %s (HTTP %s)", openalex_id, r.status_code)
            return None
        self.cost_usd += 0.01
        return r.text

    def close(self) -> None:
        self.http.close()
