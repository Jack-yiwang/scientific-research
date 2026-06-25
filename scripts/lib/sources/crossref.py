"""CrossRef Works API search. Public, no key; polite-pool via mailto improves QoS.

Retries up to 3 times on network errors (timeout, DNS failure, connection refused).
Returns [] after all retries exhausted — no exception propagates to the caller.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..http import RateLimiter, http_get_json
from ..paper import make_paper

CROSSREF_API = "https://api.crossref.org/works"
_LIMITER = RateLimiter(min_interval=0.2)


def _build_headers() -> dict:
    mailto = os.environ.get("SCI_RESEARCH_MAILTO")
    if mailto:
        return {"User-Agent": f"scientific-research-skill (mailto:{mailto})"}
    return {}


def _request_with_retry(query: str, params: dict, retries: int = 3) -> Optional[dict]:
    """Query CrossRef with up to `retries` attempts on transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            return http_get_json(
                CROSSREF_API, params=params, headers=_build_headers(),
                rate_limiter=_LIMITER, cache_ttl=86400,
            )
        except (Exception,) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    return None


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None,
           type_filter: Optional[str] = None) -> list[dict]:
    """Search CrossRef.

    The query may contain NOT-clauses (e.g. ``NOT CT NOT MRI``) for negation
    filtering.  CrossRef's filter parameter supports ``not:`` prefix.
    """
    # Parse NOT clauses from the query for CrossRef's native filter syntax
    import re as _re
    not_terms: list[str] = []
    q = query
    for m in _re.finditer(r'\bNOT\s+(\S+)', q):
        not_terms.append(f"not:{m.group(1)}")
    clean_query = _re.sub(r'\s*NOT\s+\S+', '', q).strip()

    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}")
    if year_to:
        filters.append(f"until-pub-date:{year_to}")
    if type_filter == "review":
        filters.append("type:journal-article")
    if not_terms:
        filters.append(",".join(not_terms))
    params: dict = {
        "query": clean_query,
        "rows": min(max_results, 100),
        "select": ",".join([
            "DOI", "title", "author", "issued", "container-title",
            "short-container-title", "volume", "issue", "page",
            "publisher", "abstract", "type", "is-referenced-by-count",
            "URL",
        ]),
    }
    if filters:
        params["filter"] = ",".join(filters)

    data = _request_with_retry(query, params)
    if data is None:
        return []

    items = (data.get("message") or {}).get("items", [])
    out: list[dict] = []
    for it in items:
        title_list = it.get("title") or []
        title = title_list[0] if title_list else ""
        authors = []
        for a in it.get("author") or []:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
            if name:
                authors.append(name)
        year = None
        issued = (it.get("issued") or {}).get("date-parts") or []
        if issued and issued[0]:
            try:
                year = int(issued[0][0])
            except (TypeError, ValueError):
                year = None
        venue_list = it.get("container-title") or []
        venue = venue_list[0] if venue_list else ""
        venue_short_list = it.get("short-container-title") or []
        venue_short = venue_short_list[0] if venue_short_list else ""
        abstract = it.get("abstract") or ""
        # Strip JATS XML tags from abstract, if present
        if abstract:
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()
        paper = make_paper(
            "crossref",
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            venue_short=venue_short,
            volume=it.get("volume", ""),
            issue=it.get("issue", ""),
            pages=it.get("page", ""),
            publisher=it.get("publisher", ""),
            doi=it.get("DOI", ""),
            abstract=abstract,
            citation_count=it.get("is-referenced-by-count"),
            language=it.get("language", ""),
            url=it.get("URL", ""),
            type=it.get("type", ""),
        )
        out.append(paper)
    return out
