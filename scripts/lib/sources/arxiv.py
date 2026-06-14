"""arXiv search via the Atom-format query API. No key required."""

from __future__ import annotations

import re
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


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None) -> list[dict]:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    status, _, body = http_get(
        ARXIV_API, params=params, rate_limiter=_LIMITER, cache_ttl=86400,
    )
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
