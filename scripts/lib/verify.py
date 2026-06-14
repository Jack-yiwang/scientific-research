"""DOI anchoring & metadata validation.

For every paper that has a DOI, we resolve it against CrossRef's authoritative
record (`https://api.crossref.org/works/{doi}`) and:

  * Confirm the DOI exists (HTTP 200) — otherwise mark unverified.
  * Cross-check title + first-author surname + year against the candidate
    record. A mismatch flips ``verified`` to False and adds a note.
  * Backfill missing bibliographic fields (volume/issue/pages/publisher/venue)
    from the CrossRef record — never overwrite an existing non-empty value.

Papers without a DOI are not auto-removed: they keep ``verified = False`` and a
note explaining that no DOI was available to anchor against. Caller decides
whether to drop them.
"""

from __future__ import annotations

import re
from typing import Optional

from .http import RateLimiter, http_get_json
from .paper import normalize_title

CROSSREF_WORK = "https://api.crossref.org/works/{doi}"
_LIMITER = RateLimiter(min_interval=0.2)


def _last_name(full: str) -> str:
    parts = full.strip().split()
    return parts[-1].lower() if parts else ""


def _fuzzy_title_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Loose match: first 60 chars equal OR one is prefix of the other
    if na[:60] == nb[:60]:
        return True
    if na in nb or nb in na:
        return True
    # Token overlap ≥ 70 %
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(len(ta), len(tb))
    return overlap >= 0.7


def verify_paper(paper: dict) -> dict:
    """Resolve DOI against CrossRef and update the paper in-place. Returns the same dict."""
    doi = paper.get("doi") or ""
    if not doi:
        paper["verified"] = False
        paper["verification_notes"] = "no DOI available; metadata cannot be anchored"
        return paper

    url = CROSSREF_WORK.format(doi=doi)
    try:
        data = http_get_json(url, rate_limiter=_LIMITER, cache_ttl=86400 * 7)
    except Exception as e:
        paper["verified"] = False
        paper["verification_notes"] = f"DOI did not resolve: {type(e).__name__}"
        return paper

    msg = data.get("message") or {}
    cr_title_list = msg.get("title") or []
    cr_title = cr_title_list[0] if cr_title_list else ""
    cr_authors = msg.get("author") or []
    cr_first_last = _last_name(cr_authors[0].get("family", "")) if cr_authors else ""
    cr_year = None
    issued = (msg.get("issued") or {}).get("date-parts") or []
    if issued and issued[0]:
        try:
            cr_year = int(issued[0][0])
        except (TypeError, ValueError):
            cr_year = None

    notes: list[str] = []
    title_ok = _fuzzy_title_match(paper.get("title", ""), cr_title)
    if not title_ok and cr_title:
        notes.append(f"title mismatch with CrossRef ({cr_title!r})")

    author_ok = True
    if paper.get("authors") and cr_first_last:
        ours_first_last = _last_name(paper["authors"][0])
        if ours_first_last and ours_first_last != cr_first_last:
            author_ok = False
            notes.append(f"first-author surname mismatch (ours={ours_first_last!r}, crossref={cr_first_last!r})")

    year_ok = True
    if paper.get("year") and cr_year and abs(paper["year"] - cr_year) > 1:
        year_ok = False
        notes.append(f"year mismatch (ours={paper['year']}, crossref={cr_year})")

    paper["verified"] = bool(title_ok and author_ok and year_ok)

    # Retraction / withdrawal detection. CrossRef exposes this through:
    #   - message.type == "retraction" / "withdrawal" / etc.
    #   - message.update-to: list of DOIs this work updates (the retracted paper)
    #   - message.subtype indicating "retraction"
    cr_type = (msg.get("type") or "").lower()
    cr_subtype = (msg.get("subtype") or "").lower()
    update_to = msg.get("update-to") or []
    update_labels = {(u.get("label") or "").lower() for u in update_to}
    retraction_signals = {"retraction", "withdrawal", "removal", "expression of concern"}
    is_retraction_notice = (
        cr_type in retraction_signals
        or cr_subtype in retraction_signals
        or any(lbl in retraction_signals for lbl in update_labels)
    )
    if is_retraction_notice:
        # This DOI is itself a retraction notice for someone else's paper
        retracted_dois = [u.get("DOI", "") for u in update_to if u.get("DOI")]
        paper["retracted"] = True
        paper["retraction_notes"] = (
            f"This record is a retraction notice for: {', '.join(retracted_dois)}"
            if retracted_dois else "This record is a retraction/withdrawal notice"
        )
    else:
        # Check if this paper has been retracted by another notice (updated-by)
        updated_by = msg.get("updated-by") or []
        retraction_by = [
            u.get("DOI", "") for u in updated_by
            if (u.get("label") or "").lower() in retraction_signals
        ]
        if retraction_by:
            paper["retracted"] = True
            paper["retraction_notes"] = f"Retracted by: {', '.join(retraction_by)}"
        else:
            paper["retracted"] = False

    # Backfill — never overwrite. Only fill when our slot is empty.
    def _fill(key: str, value):
        if value and not paper.get(key):
            paper[key] = value

    _fill("volume", msg.get("volume", ""))
    _fill("issue", msg.get("issue", ""))
    _fill("pages", msg.get("page", ""))
    _fill("publisher", msg.get("publisher", ""))
    _fill("type", msg.get("type", ""))
    cr_venue_list = msg.get("container-title") or []
    if cr_venue_list:
        _fill("venue", cr_venue_list[0])
    cr_short_list = msg.get("short-container-title") or []
    if cr_short_list:
        _fill("venue_short", cr_short_list[0])
    if not paper.get("year") and cr_year:
        paper["year"] = cr_year
    cnt = msg.get("is-referenced-by-count")
    if isinstance(cnt, int) and paper.get("citation_count") is None:
        paper["citation_count"] = cnt
    if not paper.get("url"):
        paper["url"] = f"https://doi.org/{doi}"

    # Recompute missing_fields after backfill — only standard bib slots
    paper["missing_fields"] = [
        f for f in ("doi", "volume", "issue", "pages") if not paper.get(f)
    ]
    paper["verification_notes"] = "; ".join(notes) if notes else "verified against CrossRef"
    return paper


def verify_all(papers: list[dict], *, drop_unverified: bool = False) -> list[dict]:
    out = []
    for p in papers:
        verify_paper(p)
        if drop_unverified and not p.get("verified"):
            continue
        out.append(p)
    return out
