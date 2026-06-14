"""CrossRef Works API search. Public, no key; polite-pool via mailto improves QoS."""

from __future__ import annotations

import os
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


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None,
           type_filter: Optional[str] = None) -> list[dict]:
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}")
    if year_to:
        filters.append(f"until-pub-date:{year_to}")
    if type_filter == "review":
        filters.append("type:journal-article")
    params: dict = {
        "query": query,
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
    try:
        data = http_get_json(
            CROSSREF_API, params=params, headers=_build_headers(),
            rate_limiter=_LIMITER, cache_ttl=86400,
        )
    except Exception:
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
