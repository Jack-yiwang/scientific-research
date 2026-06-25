"""arXiv search via the Atom-format query API. No key required.

Retries up to 3 times on network errors (timeout, DNS failure, connection refused).
Returns [] after all retries exhausted — no exception propagates to the caller.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from ..http import RateLimiter, http_get
from ..paper import make_paper

ARXIV_API = "http://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# arXiv asks for ≥3 s between calls.
_LIMITER = RateLimiter(min_interval=3.0)


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None and el.text else ""


def _fetch_with_retry(params: dict, retries: int = 3) -> tuple[int, str]:
    """Fetch arXiv with up to `retries` attempts on transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            status, _, body = http_get(ARXIV_API, params=params,
                                        rate_limiter=_LIMITER, cache_ttl=86400)
            return status, body
        except (Exception,) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
    return 0, ""


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None) -> list[dict]:
    """Search arXiv with optional NOT-clause.

    arXiv's search_query supports ``NOT:`` prefix for negation.
    We parse NOT-clauses from the incoming query and convert them
    to arXiv's ``NOT:`` syntax in the abstract field.
    """
    not_terms: list[str] = []
    q = query
    for m in re.finditer(r'\bNOT\s+(\S+)', q):
        not_terms.append(m.group(1))
    clean_query = re.sub(r'\s*NOT\s+\S+', '', q).strip()

    # Build the base query
    base = f"all:{clean_query}"
    # Append NOT clauses (arXiv supports NOT: in search_query)
    for nt in not_terms:
        base += f" AND NOT:{nt}"

    params = {
        "search_query": base,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    status, body = _fetch_with_retry(params)
    if status != 200:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    out: list[dict] = []
    for entry in root.findall("atom:entry", NS):
        arxiv_url = _text(entry.find("atom:id", NS))
        m = re.search(r"abs/([^v]+?)(?:v\d+)?$", arxiv_url)
        arxiv_id = m.group(1) if m else ""
        title = _text(entry.find("atom:title", NS)).replace("\n", " ").strip()
        title = re.sub(r"\s+", " ", title)
        summary = _text(entry.find("atom:summary", NS)).replace("\n", " ").strip()
        published = _text(entry.find("atom:published", NS))
        year = None
        if published[:4].isdigit():
            year = int(published[:4])
        if year_from and year and year < year_from:
            continue
        if year_to and year and year > year_to:
            continue
        authors = [
            _text(a.find("atom:name", NS))
            for a in entry.findall("atom:author", NS)
        ]
        authors = [a for a in authors if a]
        doi_el = entry.find("arxiv:doi", NS)
        doi = _text(doi_el)
        journal_el = entry.find("arxiv:journal_ref", NS)
        venue = _text(journal_el)
        pdf_url = ""
        for link in entry.findall("atom:link", NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        paper = make_paper(
            "arxiv",
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            arxiv_id=arxiv_id,
            abstract=summary,
            url=arxiv_url,
            pdf_url=pdf_url,
            type="preprint",
        )
        out.append(paper)
    return out
