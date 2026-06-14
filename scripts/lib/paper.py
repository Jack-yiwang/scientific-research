"""
Normalized paper schema and helpers.

A "paper" is a plain dict with the keys defined in ``PAPER_FIELDS``. Fields that
cannot be retrieved must be left empty (``""`` for strings, ``[]`` for lists,
``None`` for numbers) — never invented. ``missing_fields`` records the keys for
which we explicitly failed to locate a value, so downstream consumers know the
absence is observed rather than untested.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Stable schema. Adding a new field means updating exporters and dedup logic too.
PAPER_FIELDS: tuple[str, ...] = (
    "title",
    "authors",          # list[str]
    "year",             # int | None
    "venue",            # str (journal / conference name)
    "venue_short",      # str (ISO abbrev when available)
    "volume",           # str
    "issue",            # str
    "pages",            # str ("123-145")
    "publisher",        # str
    "doi",              # str (lowercase, no URL prefix)
    "arxiv_id",         # str
    "pmid",             # str
    "pmcid",            # str
    "abstract",         # str
    "citation_count",   # int | None
    "open_access",      # bool | None
    "url",              # str (best canonical URL, usually doi.org/...)
    "pdf_url",          # str
    "type",             # str ("journal-article" | "preprint" | "review" | ...)
    "language",         # str
    "sources",          # list[str] — which APIs reported this record
    "missing_fields",   # list[str] — fields explicitly probed but unavailable
    "verified",         # bool — DOI resolved against an authoritative source
    "verification_notes",  # str
    "retracted",        # bool | None — True if CrossRef flags this work as retracted/withdrawn
    "retraction_notes", # str — DOI(s) of the retraction notice, if any
)

_STR_FIELDS = {
    "title", "venue", "venue_short", "volume", "issue", "pages",
    "publisher", "doi", "arxiv_id", "pmid", "pmcid", "abstract",
    "url", "pdf_url", "type", "language", "verification_notes",
    "retraction_notes",
}
_LIST_FIELDS = {"authors", "sources", "missing_fields"}
_BOOL_FIELDS = {"open_access", "verified", "retracted"}
_INT_FIELDS = {"year", "citation_count"}


def empty_paper() -> dict:
    p: dict[str, Any] = {}
    for f in PAPER_FIELDS:
        if f in _LIST_FIELDS:
            p[f] = []
        elif f in _BOOL_FIELDS:
            p[f] = None
        elif f in _INT_FIELDS:
            p[f] = None
        else:
            p[f] = ""
    p["verified"] = False
    return p


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    s = doi.strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = s.lower()
    return s


def normalize_arxiv(aid: str) -> str:
    if not aid:
        return ""
    s = aid.strip().lower()
    s = re.sub(r"^https?://arxiv\.org/abs/", "", s)
    s = re.sub(r"v\d+$", "", s)  # strip version
    return s


def normalize_title(title: str) -> str:
    if not title:
        return ""
    s = title.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return s.strip()


def make_paper(source: str, **fields: Any) -> dict:
    """Build a paper dict, recording which probed fields came back empty."""
    p = empty_paper()
    p["sources"] = [source]
    missing: list[str] = []
    for k, v in fields.items():
        if k not in p:
            continue
        if k in _STR_FIELDS:
            v = (v or "").strip() if isinstance(v, str) else (v or "")
            if k == "doi":
                v = normalize_doi(v)
            elif k == "arxiv_id":
                v = normalize_arxiv(v)
            p[k] = v
        elif k in _LIST_FIELDS:
            p[k] = list(v or [])
        elif k in _INT_FIELDS:
            try:
                p[k] = int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                p[k] = None
        elif k in _BOOL_FIELDS:
            p[k] = bool(v) if v is not None else None
    # Track standard bibliographic fields probed but unavailable
    for probe in ("doi", "volume", "issue", "pages"):
        if probe in fields and not p[probe]:
            missing.append(probe)
    p["missing_fields"] = missing
    return p


def best_canonical_url(p: dict) -> str:
    if p.get("doi"):
        return f"https://doi.org/{p['doi']}"
    if p.get("arxiv_id"):
        return f"https://arxiv.org/abs/{p['arxiv_id']}"
    if p.get("pmid"):
        return f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
    return p.get("url") or ""


def completeness_score(p: dict) -> int:
    """Higher score = more complete metadata. Used to pick the winner during dedup."""
    weights = {
        "doi": 5, "title": 3, "authors": 3, "year": 2, "venue": 2,
        "volume": 1, "issue": 1, "pages": 1, "abstract": 2,
        "pmid": 1, "arxiv_id": 1, "publisher": 1, "citation_count": 1,
    }
    score = 0
    for k, w in weights.items():
        v = p.get(k)
        if k == "authors":
            if v:
                score += w
        elif isinstance(v, str):
            if v.strip():
                score += w
        elif v is not None:
            score += w
    return score


def merge_papers(target: dict, other: dict) -> dict:
    """Merge ``other`` into ``target``: keep target's value when set, else take other's.

    Always unions ``sources`` and ``missing_fields`` (intersection — a field is only
    "missing" if neither record has it).
    """
    out = dict(target)
    for k in PAPER_FIELDS:
        tv, ov = out.get(k), other.get(k)
        if k == "sources":
            out[k] = sorted(set(list(tv or []) + list(ov or [])))
        elif k == "missing_fields":
            # Recompute after merge
            continue
        elif k in _LIST_FIELDS:
            if not tv and ov:
                out[k] = ov
        elif k in _BOOL_FIELDS:
            if tv is None and ov is not None:
                out[k] = ov
            elif k == "verified":
                out[k] = bool(tv) or bool(ov)
        elif k in _INT_FIELDS:
            if tv is None and ov is not None:
                out[k] = ov
        else:
            if (not tv) and ov:
                out[k] = ov
    # Recompute missing_fields for standard bib slots
    out["missing_fields"] = [
        f for f in ("doi", "volume", "issue", "pages") if not out.get(f)
    ]
    return out


def dedup_papers(papers: Iterable[dict]) -> list[dict]:
    """Deduplicate by DOI first, then arxiv_id, then normalized title+year.

    When duplicates are found, keep the record with the highest completeness
    score and merge in metadata from the loser.
    """
    buckets: dict[str, dict] = {}

    def key_for(p: dict) -> str:
        if p.get("doi"):
            return f"doi:{p['doi']}"
        if p.get("arxiv_id"):
            return f"arxiv:{p['arxiv_id']}"
        if p.get("pmid"):
            return f"pmid:{p['pmid']}"
        nt = normalize_title(p.get("title", ""))
        yr = p.get("year") or "?"
        return f"title:{nt}|{yr}"

    for p in papers:
        k = key_for(p)
        if k not in buckets:
            buckets[k] = p
            continue
        existing = buckets[k]
        if completeness_score(p) > completeness_score(existing):
            buckets[k] = merge_papers(p, existing)
        else:
            buckets[k] = merge_papers(existing, p)
    return list(buckets.values())
