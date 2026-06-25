"""Semantic Scholar Graph API. Public; key optional via SEMANTIC_SCHOLAR_API_KEY.

Retries up to 3 times on network errors (timeout, DNS failure, connection refused).
Returns [] after all retries exhausted — no exception propagates to the caller.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..http import RateLimiter, http_get_json
from ..paper import make_paper

S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
# Anonymous: ~1 req/s safe. With key: higher.
_LIMITER = RateLimiter(min_interval=1.1)

FIELDS = ",".join([
    "title", "abstract", "authors", "year", "venue", "publicationVenue",
    "externalIds", "openAccessPdf", "citationCount", "publicationTypes",
    "publicationDate", "journal", "url",
])


def _headers() -> dict:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    return {"x-api-key": key} if key else {}


def _request_with_retry(query: str, params: dict, retries: int = 3) -> Optional[dict]:
    """Query Semantic Scholar with up to `retries` attempts on transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            return http_get_json(
                S2_API, params=params, headers=_headers(),
                rate_limiter=_LIMITER, cache_ttl=86400,
            )
        except (Exception,) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
    # All retries exhausted — return None so caller returns []
    return None


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None,
           type_filter: Optional[str] = None) -> list[dict]:
    """Search Semantic Scholar.

    The query may contain NOT-clauses (e.g. ``NOT CT NOT MRI``).
    Semantic Scholar supports the ``negate`` parameter for negation.
    """
    import re as _re
    not_terms: list[str] = []
    q = query
    for m in _re.finditer(r'\bNOT\s+(\S+)', q):
        not_terms.append(m.group(1))
    clean_query = _re.sub(r'\s*NOT\s+\S+', '', q).strip()

    params: dict = {
        "query": clean_query,
        "limit": min(max_results, 100),
        "fields": FIELDS,
    }
    if year_from or year_to:
        lo = year_from or 1900
        hi = year_to or 3000
        params["year"] = f"{lo}-{hi}"
    if type_filter == "review":
        params["publicationTypes"] = "Review"
    if not_terms:
        params["negate"] = ",".join(not_terms)

    data = _request_with_retry(query, params)
    if data is None:
        return []
    if not isinstance(data, dict):
        return []

    out: list[dict] = []
    for it in data.get("data", []):
        ext = it.get("externalIds") or {}
        authors = [a.get("name", "") for a in (it.get("authors") or []) if a.get("name")]
        journal = it.get("journal") or {}
        oa = it.get("openAccessPdf") or {}
        ptypes = it.get("publicationTypes") or []
        ptype = "review" if any("review" in (p or "").lower() for p in ptypes) else (
            "preprint" if "ARXIV" in ext else "journal-article"
        )
        paper = make_paper(
            "semantic_scholar",
            title=it.get("title", ""),
            authors=authors,
            year=it.get("year"),
            venue=it.get("venue", "") or (it.get("publicationVenue") or {}).get("name", ""),
            volume=journal.get("volume", ""),
            pages=journal.get("pages", ""),
            doi=ext.get("DOI", ""),
            arxiv_id=ext.get("ARXIV", ""),
            pmid=str(ext.get("PubMed", "") or ""),
            pmcid=str(ext.get("PubMedCentral", "") or ""),
            abstract=it.get("abstract", "") or "",
            citation_count=it.get("citationCount"),
            open_access=bool(oa.get("url")),
            url=it.get("url", ""),
            pdf_url=oa.get("url", ""),
            type=ptype,
        )
        out.append(paper)
    return out
