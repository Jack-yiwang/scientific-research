"""PubMed E-utilities (esearch + esummary + efetch). Public, no key required.

Retries up to 3 times on network errors (timeout, DNS failure, connection refused).
Returns [] after all retries exhausted — no exception propagates to the caller.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

from ..http import RateLimiter, http_get, http_get_json
from ..paper import make_paper

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Without API key NCBI allows ~3 req/s; we stay under that.
_LIMITER = RateLimiter(min_interval=0.4)


def _common_params() -> dict:
    p = {"tool": "scientific-research-skill"}
    mailto = os.environ.get("SCI_RESEARCH_MAILTO")
    if mailto:
        p["email"] = mailto
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        p["api_key"] = api_key
    return p


def _esearch_ids(query: str, max_results: int,
                 year_from: Optional[int], year_to: Optional[int],
                 type_filter: Optional[str]) -> list[str]:
    """Search PubMed with optional NOT-clause.

    PubMed uses ``AND NOT term[Field]`` syntax.  We auto-detect field names
    from NOT-clause terms and build a compound query.
    """
    import re as _re
    not_terms: list[str] = []
    q = query
    # PubMed's NOT syntax (`AND NOT term[Field]`) is fragile and can cause
    # zero results when generic negation keywords (echo, mri, ultrasound)
    # that happen to appear in *unrelated* papers' abstracts are added.
    # We deliberately ignore NOT-clauses at the search level and rely
    # entirely on ``filter_papers_by_intent()`` (post-search filter) instead.
    # NOT-clause detection kept for compatibility but all terms are discarded.
    # for m in _re.finditer(r'\bNOT\s+(\S+)', q):
    #     not_terms.append(m.group(1))
    # q = _re.sub(r'\s*NOT\s+\S+', '', q).strip()
    q = _re.sub(r'\s+NOT\s+\S+', '', q).strip()  # strip NOT clause to avoid query corruption
    clean_query = q

    term = clean_query
    # PubMed does not understand free-form synonym expansions appended by the
    # orchestrator (e.g. "CT ovarian cancer segmentation computed tomography
    # ct instance segmentation …").  Keep only the first 4 tokens which
    # capture the user's core intent (e.g. "CT ovarian cancer segmentation").
    tokens = clean_query.split()[:4]
    term = " ".join(tokens)
    if year_from or year_to:
        lo = year_from or 1900
        hi = year_to or 3000
        term = f"({term}) AND ({lo}:{hi}[dp])"
    if type_filter == "review":
        term = f"({term}) AND review[Publication Type]"
    for nt in not_terms:
        term = f"({term}) AND NOT ({nt}[Title/Abstract])"
    params = {
        **_common_params(),
        "db": "pubmed",
        "term": term,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
    }
    last_err = None
    for attempt in range(3):
        try:
            data = http_get_json(ESEARCH, params=params, rate_limiter=_LIMITER, cache_ttl=86400)
            return ((data.get("esearchresult") or {}).get("idlist")) or []
        except (Exception,) as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
    return []


def _efetch_details(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    params = {
        **_common_params(),
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    status, _, body = http_get(EFETCH, params=params, rate_limiter=_LIMITER, cache_ttl=86400)
    if status != 200:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    out: list[dict] = []
    for art in root.findall(".//PubmedArticle"):
        pmid = (art.findtext(".//PMID") or "").strip()
        title = (art.findtext(".//ArticleTitle") or "").strip()
        abstract_parts = [t.text or "" for t in art.findall(".//Abstract/AbstractText")]
        abstract = " ".join(s.strip() for s in abstract_parts if s).strip()
        venue = (art.findtext(".//Journal/Title") or "").strip()
        venue_short = (art.findtext(".//Journal/ISOAbbreviation") or "").strip()
        volume = (art.findtext(".//JournalIssue/Volume") or "").strip()
        issue = (art.findtext(".//JournalIssue/Issue") or "").strip()
        pages = (art.findtext(".//Pagination/MedlinePgn") or "").strip()
        year_text = (art.findtext(".//JournalIssue/PubDate/Year")
                     or art.findtext(".//JournalIssue/PubDate/MedlineDate") or "")
        year = None
        if year_text[:4].isdigit():
            year = int(year_text[:4])
        authors = []
        for a in art.findall(".//Author"):
            last_name = a.findtext("LastName") or ""
            fore = a.findtext("ForeName") or a.findtext("Initials") or ""
            name = (f"{fore} {last_name}").strip()
            if name:
                authors.append(name)
        doi = ""
        pmcid = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            id_type = aid.attrib.get("IdType", "").lower()
            val = (aid.text or "").strip()
            if id_type == "doi":
                doi = val
            elif id_type == "pmc":
                pmcid = val
        types = [t.text for t in art.findall(".//PublicationTypeList/PublicationType") if t.text]
        ptype = "review" if any("review" in (t or "").lower() for t in types) else "journal-article"

        paper = make_paper(
            "pubmed",
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            venue_short=venue_short,
            volume=volume,
            issue=issue,
            pages=pages,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            abstract=abstract,
            type=ptype,
        )
        out.append(paper)
    return out


def search(query: str, *, max_results: int = 20,
           year_from: Optional[int] = None,
           year_to: Optional[int] = None,
           type_filter: Optional[str] = None) -> list[dict]:
    pmids = _esearch_ids(query, max_results, year_from, year_to, type_filter)
    return _efetch_details(pmids)
