"""
Citation Tree — trace research lineage around an Anchor Paper.

Given a root (anchor) paper, builds a structured "citation tree" by:
  1. Backwards: extracting its references, then scoring/filtering the
     most influential prior works (【技术源头/奠基理论】).
  2. Forwards: querying Semantic Scholar for "Cited By" records, then
     ranking by recency + citation count (【最新后续演进/SOTA应用】).

Design constraints:
  * Semantic Scholar anonymous: ~1 req/s.  All API calls are rate-limited.
  * Cascading queries are capped: max_refs per anchor (default 30),
    max_cited_by per anchor (default 50).  No unbounded fan-out.
  * Cross-source fallback: if S2 is unreachable, use CrossRef `references`
    and `is-referenced-by-count` as best-effort data.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

# Relative imports (loaded as part of the lib package).
# When run as a standalone script, see the CLI entry point at the bottom.
from .http import RateLimiter  # noqa: E402
from .paper import make_paper  # noqa: E402
from .sources import semantic_scholar as s2_src  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Max references to fetch per paper (S2 default page size = 100, cap lower
# to avoid unbounded fan-out on papers with thousands of refs).
MAX_REFS = 30

# Max "Cited By" records to fetch per anchor paper.
MAX_CITED_BY = 50

# Max backward papers to return after scoring.
MAX_BACKWARD = 5

# Max forward papers to return after scoring.
MAX_FORWARD = 5

# Rate limiter: S2 anonymous = 1 req / 1.1 s safe.
_S2_LIMITER = RateLimiter(min_interval=1.1)

# ---------------------------------------------------------------------------
# Backward: references extraction + filtering
# ---------------------------------------------------------------------------

def _fetch_references_doi(
    doi: str,
    api_key: str | None = None,
) -> list[dict]:
    """Fetch references for a paper via Semantic Scholar Graph API.

    Uses /paper/{DOI}?fields=externalIds,references.
    """
    api_key = api_key or _get_s2_key()
    headers = {"x-api-key": api_key} if api_key else {}

    try:
        resp = s2_src._request_with_retry(  # noqa: SLF001
            f"refs via DOI={doi}",
            params={"fields": "references"},
            headers=headers,
            retries=2,
        )
    except Exception:
        return []

    if not resp or not isinstance(resp, dict):
        return []

    # Try reference IDs first, then externalIds fallback.
    ref_ids = resp.get("references")
    if ref_ids:
        return _fetch_papers_by_s2_ids(ref_ids[:MAX_REFS], api_key=api_key)

    # Fallback: try DOI lookup via S2 paper API.
    try:
        paper = s2_src._get_paper_doi(doi, fields="references", api_key=api_key)  # noqa: SLF001
    except Exception:
        return []
    return []


def _fetch_papers_by_s2_ids(
    s2_ids: list[str],
    api_key: str | None = None,
) -> list[dict]:
    """Given S2 paper IDs (or external ID strings), return paper dicts.

    Semantic Scholar's /paper/{ID} returns full paper metadata.
    We batch by calling /paper/search with a list of IDs.
    """
    api_key = api_key or _get_s2_key()
    if not s2_ids:
        return []

    results: list[dict] = []
    # S2 allows fetching papers by ID via /paper/{s2_id} individually.
    # For efficiency, we try the bulk endpoint first.
    for sid in s2_ids[:MAX_REFS]:
        try:
            _S2_LIMITER.wait()
            bulk_params = {
                "fields": "title,year,venue,citationCount,externalIds,abstract,authors",
                "ids": [sid],
            }
            data = s2_src._request_with_retry(  # noqa: SLF001
                f"bulk ref {sid}",
                params=bulk_params,
                headers={"x-api-key": api_key} if api_key else {},
            )
            if data and isinstance(data, dict):
                items = data.get("data", [])
                if items:
                    results.extend(_parse_s2_item(items[0]))
        except Exception:
            continue
    return results


def _get_s2_key() -> str | None:
    return None  # S2 API key from env if set


def _parse_s2_item(it: dict) -> list[dict]:
    """Parse a single S2 API response item into paper dicts.

    Returns a list because one S2 item may map to multiple records
    if multiple external IDs resolve.
    """
    ext = it.get("externalIds") or {}
    authors = [a.get("name", "") for a in (it.get("authors") or []) if a.get("name")]
    journal = it.get("journal") or {}

    paper = make_paper(
        "semantic_scholar",
        title=it.get("title", ""),
        authors=authors,
        year=it.get("year"),
        venue=it.get("venue", ""),
        volume=journal.get("volume", ""),
        pages=journal.get("pages", ""),
        doi=ext.get("DOI", ""),
        arxiv_id=ext.get("ARXIV", ""),
        pmid=str(ext.get("PubMed", "") or ""),
        abstract=it.get("abstract", "") or "",
        citation_count=it.get("citationCount"),
    )
    return [paper]


def filter_references_by_influence(
    references: list[dict],
    *,
    max_results: int = MAX_BACKWARD,
) -> list[dict]:
    """Score and filter references to find the most influential prior works.

    Scoring: citation_count * time_decay(year) — similar to anchor scorer but
    simpler (no venue tier needed for backward refs).

    Args:
        references: List of paper dicts (from references API call).
        max_results: Max papers to return.

    Returns:
        Top-K references sorted by influence score.
    """
    if not references:
        return []

    # Filter out papers with no year or no citation count.
    valid = [
        p for p in references
        if p.get("year") and isinstance(p.get("year"), int) and p.get("citation_count") is not None
    ]

    # Score by citation count weighted by time decay.
    def influence_score(p: dict) -> float:
        C = p.get("citation_count") or 0
        Y = p.get("year") or 2000
        # Normalize: log(C+1) * (2027 - Y)^(-1)
        delta = 2027 - Y
        if delta <= 0:
            delta = 1
        return (C + 1) / delta ** 1.0

    valid.sort(key=influence_score, reverse=True)
    return valid[:max_results]


# ---------------------------------------------------------------------------
# Forwards: "Cited By" lookup
# ---------------------------------------------------------------------------

def fetch_cited_by(
    anchor_paper: dict,
    *,
    max_results: int = MAX_CITED_BY,
    year_from: int | None = None,
    api_key: str | None = None,
) -> list[dict]:
    """Find papers that cite the anchor paper.

    Uses Semantic Scholar's /paper/{ID}/citations endpoint.
    Falls back to CrossRef cross-reference lookup if S2 is unavailable.

    Args:
        anchor_paper: The anchor paper dict (must have doi or s2_id).
        max_results: Max cited-by records to return.
        year_from: Optional year filter (e.g. 2022 for recent papers).
        api_key: Optional S2 API key.

    Returns:
        List of paper dicts that cite the anchor paper.
    """
    doi = anchor_paper.get("doi", "")
    s2_id = anchor_paper.get("s2_id", "")
    api_key = api_key or _get_s2_key()

    # --- Primary: Semantic Scholar /paper/{ID}/citations ---
    if s2_id:
        try:
            _S2_LIMITER.wait()
            params = {
                "fields": "title,year,venue,citationCount,externalIds,abstract,authors",
                "limit": min(max_results, 100),
            }
            if year_from:
                params["year"] = f"{year_from}-"

            data = s2_src._request_with_retry(  # noqa: SLF001
                f"citations via s2_id={s2_id}",
                params=params,
                headers={"x-api-key": api_key} if api_key else {},
            )
            if data and isinstance(data, dict):
                items = data.get("data", []) or data.get("papers", [])
                results = []
                for item in items[:max_results]:
                    results.extend(_parse_s2_item(item))
                return results
        except Exception as e:
            print(f"[citation_tree] S2 citations failed for {doi}: {e}", file=sys.stderr)

    # --- Fallback: CrossRef via /works/{DOI}/cited-by ---
    if doi:
        try:
            _S2_LIMITER.wait()
            results = _crossref_cited_by(doi, max_results=max_results, year_from=year_from)
            if results:
                return results
        except Exception as e:
            print(f"[citation_tree] CrossRef cited-by failed for {doi}: {e}", file=sys.stderr)

    return []


def _crossref_cited_by(
    doi: str,
    *,
    max_results: int = 20,
    year_from: int | None = None,
) -> list[dict]:
    """CrossRef fallback: query /works/{DOI}/cited-by for referencing papers.

    CrossRef's /cited-by endpoint returns a paginated list of referencing DOIs.
    We then fetch the first page of metadata for those DOIs.

    Note: This is a best-effort fallback; CrossRef cited-by has limitations.
    """
    import urllib.request

    base_url = f"https://api.crossref.org/works/{doi}/cited-by"
    params = f"?select=title,author,issued,container-title,DOI,citation&rows={min(max_results, 20)}"
    if year_from:
        params += f"&filter=from-pub-date:{year_from}"
    url = base_url + params

    try:
        from lib.http import http_get_json
        data = http_get_json(url, rate_limiter=_S2_LIMITER, cache_ttl=86400)
    except Exception:
        return []

    message = (data or {}).get("message", {})
    items = message.get("items", [])
    results = []

    for item in items[:max_results]:
        title_list = item.get("title") or []
        title = title_list[0] if title_list else ""

        authors = []
        for a in item.get("author") or []:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
            if name:
                authors.append(name)

        issued = (item.get("issued") or {}).get("date-parts") or []
        year = None
        if issued and issued[0]:
            try:
                year = int(issued[0][0])
            except (TypeError, ValueError):
                pass

        venue_list = item.get("container-title") or []
        venue = venue_list[0] if venue_list else ""

        # Strip JATS XML from abstract
        abstract = (item.get("abstract") or "").strip()
        if abstract:
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        paper = make_paper(
            "crossref",
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=item.get("DOI", ""),
            abstract=abstract,
        )
        results.append(paper)

    return results


def score_forward_papers(
    cited_by: list[dict],
    *,
    max_results: int = MAX_FORWARD,
) -> list[dict]:
    """Score and rank forward citations by recency + citation strength.

    Formula: score = log(C+1) * (1 + recent_bonus)
    where recent_bonus = 2.0 if year >= 2024, 1.0 if year >= 2022.

    Args:
        cited_by: List of paper dicts from fetch_cited_by.
        max_results: Max papers to return.

    Returns:
        Top-K forward papers sorted by score.
    """
    if not cited_by:
        return []

    def forward_score(p: dict) -> tuple:
        C = p.get("citation_count") or 0
        Y = p.get("year") or 2000
        # Recency bonus: newer papers get a boost.
        if Y >= 2025:
            recent = 2.5
        elif Y >= 2024:
            recent = 2.0
        elif Y >= 2022:
            recent = 1.0
        else:
            recent = 0.0
        # Combined: log citations weighted by recency.
        return (math.log(C + 1) * (1 + recent), -(p.get("year") or 0))

    cited_by.sort(key=forward_score, reverse=True)
    return cited_by[:max_results]


# ---------------------------------------------------------------------------
# Citation Tree Data Structure
# ---------------------------------------------------------------------------

def build_citation_tree(
    anchor_paper: dict,
    *,
    max_backward: int = MAX_BACKWARD,
    max_forward: int = MAX_FORWARD,
    api_key: str | None = None,
) -> dict:
    """Build a structured citation tree rooted at ``anchor_paper``.

    Args:
        anchor_paper: The anchor (root) paper dict.
        max_backward: Max backward refs to include.
        max_forward: Max forward citations to include.
        api_key: S2 API key.

    Returns:
        Dict with schema:
        {
            "root": {"title": ..., "doi": ..., "year": ..., "venue": ...},
            "backward": [{"title": ..., "doi": ..., ...}, ...],
            "forward": [{"title": ..., "doi": ..., ...}, ...],
            "metadata": {"total_backward": N, "total_forward": N, "timestamp": ...}
        }
    """
    root_info = {
        "title": anchor_paper.get("title", ""),
        "doi": anchor_paper.get("doi", ""),
        "arxiv_id": anchor_paper.get("arxiv_id", ""),
        "year": anchor_paper.get("year"),
        "venue": anchor_paper.get("venue", ""),
        "authors": anchor_paper.get("authors", []),
        "citation_count": anchor_paper.get("citation_count"),
    }

    # --- Backward: references ---
    ref_papers = _fetch_references_from_anchor(anchor_paper, api_key=api_key)
    backward = filter_references_by_influence(ref_papers, max_results=max_backward)
    backward_nodes = [_paper_to_node(p) for p in backward]

    # --- Forward: cited-by ---
    forward = fetch_cited_by(anchor_paper, max_results=max_forward, year_from=2021, api_key=api_key)
    forward = score_forward_papers(forward, max_results=max_forward)
    forward_nodes = [_paper_to_node(p) for p in forward]

    return {
        "root": root_info,
        "backward": backward_nodes,
        "forward": forward_nodes,
        "metadata": {
            "total_backward": len(backward_nodes),
            "total_forward": len(forward_nodes),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "anchor_doi": anchor_paper.get("doi", ""),
        },
    }


def _fetch_references_from_anchor(
    anchor_paper: dict,
    api_key: str | None = None,
) -> list[dict]:
    """Fetch references for the anchor paper via S2 Graph API.

    Uses /paper/{s2_id_or_doi}?fields=references.
    Falls back to existing references field if already present.
    """
    api_key = api_key or _get_s2_key()

    # Check if references are already in the paper dict (from previous S2 query).
    existing_refs = anchor_paper.get("references")
    if existing_refs:
        return existing_refs

    s2_id = anchor_paper.get("s2_id", "")
    doi = anchor_paper.get("doi", "")

    if s2_id:
        try:
            _S2_LIMITER.wait()
            params = {"fields": "references"}
            data = s2_src._request_with_retry(
                f"refs s2_id={s2_id}",
                params=params,
                headers={"x-api-key": api_key} if api_key else {},
            )
            if data and isinstance(data, dict):
                ref_ids = data.get("references", [])
                if ref_ids:
                    return _fetch_papers_by_s2_ids(ref_ids[:MAX_REFS], api_key=api_key)
        except Exception as e:
            print(f"[citation_tree] S2 refs failed: {e}", file=sys.stderr)

    # Last resort: try CrossRef to get reference metadata.
    if doi:
        try:
            return _crossref_references(doi)
        except Exception as e:
            print(f"[citation_tree] CrossRef refs failed: {e}", file=sys.stderr)

    return []


def _crossref_references(doi: str) -> list[dict]:
    """Fetch references via CrossRef /works/{DOI}.

    CrossRef includes a 'reference' field with DOIs of cited works.
    We parse those DOIs and fetch metadata for each.
    """
    try:
        from lib.http import http_get_json
        data = http_get_json(
            f"https://api.crossref.org/works/{doi}",
            params={"select": "reference"},
            rate_limiter=_S2_LIMITER,
            cache_ttl=86400,
        )
    except Exception:
        return []

    message = (data or {}).get("message", {})
    refs = message.get("reference") or []
    if not refs:
        return []

    # Parse reference DOIs from the reference array.
    ref_dois: list[str] = []
    for ref in refs[:MAX_REFS]:
        doi_field = ref.get("DOI")
        if doi_field:
            ref_dois.append(doi_field.lower().strip())

    # Fetch metadata for each DOI.
    results = []
    for ref_doi in ref_dois[:10]:  # cap metadata fetch
        try:
            from lib.http import http_get_json
            data = http_get_json(
                f"https://api.crossref.org/works/{ref_doi}",
                params={"select": "title,author,issued,container-title,citationCount,DOI"},
                rate_limiter=_S2_LIMITER,
                cache_ttl=86400,
            )
            msg = (data or {}).get("message", {})
            title_list = msg.get("title") or []
            authors = []
            for a in msg.get("author") or []:
                name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
                if name:
                    authors.append(name)
            issued = (msg.get("issued") or {}).get("date-parts") or []
            year = None
            if issued and issued[0]:
                try:
                    year = int(issued[0][0])
                except (TypeError, ValueError):
                    pass
            venue_list = msg.get("container-title") or []
            venue = venue_list[0] if venue_list else ""
            citation_count = msg.get("is-referenced-by-count")

            p = make_paper(
                "crossref",
                title=title_list[0] if title_list else "",
                authors=authors,
                year=year,
                venue=venue,
                doi=msg.get("DOI", ""),
                citation_count=citation_count,
            )
            results.append(p)
        except Exception:
            continue

    return results


def _paper_to_node(p: dict) -> dict:
    """Convert a paper dict to a citation tree node."""
    return {
        "title": p.get("title", ""),
        "doi": p.get("doi", ""),
        "arxiv_id": p.get("arxiv_id", ""),
        "year": p.get("year"),
        "venue": p.get("venue", ""),
        "authors": p.get("authors", []),
        "citation_count": p.get("citation_count"),
        "abstract": p.get("abstract", ""),
    }


# ---------------------------------------------------------------------------
# Rendering: Markdown text tree + Mermaid diagram
# ---------------------------------------------------------------------------

def render_tree_md(tree: dict) -> str:
    """Render a citation tree as a Markdown text tree diagram.

    Example output:

    # Citation Tree: [Anchor Paper Title]

    ## Backward: 【技术源头/奠基理论】
    • 2016 — "Deep Residual Learning" (CVPR, 70K citations)
      └── influence_score: 12500.0
    ...

    ## Anchor (Root)
    • 2017 — "Attention Is All You Need" (NeurIPS, 95K citations)

    ## Forward: 【最新后续演进/SOTA应用】
    • 2024 — "Transformer in Medical Imaging" (MedIA, 500 citations)
      └── forward_score: 8.5
    ...
    """
    root = tree["root"]
    lines: list[str] = []

    lines.append(f"# Citation Tree: {root['title']}")
    lines.append("")
    lines.append(f"> DOI: `{root.get('doi', 'missing')}` | "
                 f"Year: {root.get('year', 'missing')} | "
                 f"Venue: {root.get('venue', 'missing')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Backward section
    backward = tree.get("backward", [])
    lines.append("## 向后追溯：【技术源头 / Foundational Works】")
    lines.append("")
    if backward:
        for i, node in enumerate(backward, 1):
            c = node.get("citation_count") or "?"
            lines.append(
                f"- **{i}. {node['title']}** "
                f"({node.get('venue', 'unknown')}, {node.get('year', '?')}, {c} citations)"
            )
            if node.get("doi"):
                lines.append(f"  - DOI: [{node['doi']}](https://doi.org/{node['doi']})")
    else:
        lines.append("_No backward references available._")
    lines.append("")

    # Root (anchor)
    lines.append("## 核心经典文献：【Anchor Paper】")
    lines.append("")
    lines.append(
        f"**{root['title']}** ({root.get('venue', 'unknown')}, "
        f"{root.get('year', '?')}, "
        f"{root.get('citation_count', '?')} citations)"
    )
    if root.get("authors"):
        lines.append(f"- Authors: {', '.join(root['authors'][:5])}" +
                     (f" (+{len(root['authors'])-5} more)" if len(root['authors']) > 5 else ""))
    if root.get("doi"):
        lines.append(f"- DOI: [{root['doi']}](https://doi.org/{root['doi']})")
    lines.append("")

    # Forward section
    forward = tree.get("forward", [])
    lines.append("## 向前追踪：【最新后续演进 / Recent Developments】")
    lines.append("")
    if forward:
        for i, node in enumerate(forward, 1):
            c = node.get("citation_count") or "?"
            lines.append(
                f"- **{i}. {node['title']}** "
                f"({node.get('venue', 'unknown')}, {node.get('year', '?')}, {c} citations)"
            )
            if node.get("doi"):
                lines.append(f"  - DOI: [{node['doi']}](https://doi.org/{node['doi']})")
    else:
        lines.append("_No forward citations available._")
    lines.append("")

    # Mermaid diagram
    mermaid = render_tree_mermaid(tree)
    lines.append("## 引用树可视化 / Citation Tree Diagram")
    lines.append("")
    lines.append(mermaid)
    lines.append("")

    return "\n".join(lines)


def render_tree_mermaid(tree: dict) -> str:
    """Render the citation tree as a Mermaid flowchart.

    Layout:
      [Backward Papers] --> [Anchor Paper] --> [Forward Papers]
    """
    root = tree["root"]
    backward = tree.get("backward", [])
    forward = tree.get("forward", [])

    lines: list[str] = ["```mermaid", "flowchart LR"]

    # Escape node labels for Mermaid (no special chars).
    def safe_label(text: str) -> str:
        if not text:
            return "untitled"
        text = text.replace('"', "'").replace("\n", " ").strip()
        if len(text) > 50:
            text = text[:47] + "..."
        return text

    # Root node
    root_id = "anchor"
    root_label = safe_label(root.get("title", "Anchor"))
    lines.append(f'    {root_id}["{root_label}\\n({root.get("year", "?")})"]')

    # Backward nodes
    for i, node in enumerate(backward):
        nid = f"b{i}"
        label = safe_label(node.get("title", ""))
        lines.append(f'    {nid}["{label}\\n({node.get("year", "?")})"]')

    # Forward nodes
    for i, node in enumerate(forward):
        nid = f"f{i}"
        label = safe_label(node.get("title", ""))
        lines.append(f'    {nid}["{label}\\n({node.get("year", "?")})"]')

    # Edges: backward --> anchor --> forward
    for i in range(len(backward)):
        lines.append(f"    b{i} --> {root_id}")
    for i in range(len(forward)):
        lines.append(f"    {root_id} --> f{i}")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report integration
# ---------------------------------------------------------------------------

def append_to_report(
    report_text: str,
    tree: dict,
) -> str:
    """Append citation tree section to an existing report text."""
    tree_md = render_tree_md(tree)
    return report_text.rstrip() + "\n\n---\n\n" + tree_md


# ---------------------------------------------------------------------------
# CLI entry point (standalone debug / testing)
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI: read anchor paper JSON from stdin, print citation tree."""
    raw = sys.stdin.read()
    anchor = json.loads(raw)

    tree = build_citation_tree(anchor, max_backward=5, max_forward=5)
    print(render_tree_md(tree))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
