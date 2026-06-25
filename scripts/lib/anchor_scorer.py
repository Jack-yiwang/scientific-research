"""
Anchor Paper — multi-dimensional classic-literature scorer.

Given a list of retrieved papers, identifies 1~3 truly foundational or
milestone papers ("Anchor Papers") through a weighted scoring function.

Scoring formula::

    Score = w1 * ln(C + 1) * t_decay(Y) * W_venue  +  w2 * L_local  +  w3 * network_boost

Where:
  - C = citation_count
  - Y = publication year
  - t_decay(Y) = 1 / (2027 - Y)^alpha  (time-decay, alpha=1 by default)
  - W_venue = venue impact weight (tier-based)
  - L_local = local relevance from retrieval (0~1)
  - network_boost = co-citation density bonus from the local reference network

Design goals:
  * Prevent old, low-relevance papers from dominating purely on citation count
  * Allow recent "dark-horse" papers (high C in short time) to surface
  * Bonus papers that are frequently co-cited within the local result set
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Venue impact tier map
# ---------------------------------------------------------------------------
# Key = normalised venue name substring (case-insensitive).
# Value = weight W_venue.  Higher = more prestigious.

_VENUE_TIERS: dict[str, tuple[str, float]] = {
    # --- Tier 5: Nature / Science / PNAS (flagship multi-disciplinary) ---
    ("Nature", "science", "pnas"): ("tier_5", 3.0),

    # --- Tier 4: Top medical / AI journals ---
    ("IEEE T-PAMI", "IEEE Transactions on Pattern Analysis and Machine Intelligence",
     "IEEE Transactions on Medical Imaging", "MedIA",
     "Medical Image Analysis", "Nature Methods", "Nature Machine Intelligence",
     "Nature Medicine", "The Lancet", "NEJM", "New England Journal of Medicine",
     "BMJ", "JAMA", "Nature Communications", "Science Advances"): ("tier_4", 2.5),

    # --- Tier 3: Strong venues ---
    ("IEEE Transactions on Image Processing", "TIP",
     "International Journal of Computer Vision", "IJCV",
     "IEEE TVT", "IEEE Transactions on Vehicular Technology",
     "Radiology", "European Radiology", "Medical Physics",
     "Artificial Intelligence", "AI",
     "Proceedings of the AAAI Conference on Artificial Intelligence", "AAAI"): (
         "tier_3", 2.0),

    # --- Tier 2: Reputable conferences & journals ---
    ("MICCAI", "Medical Image Analysis and Applications",
     "IPMI", "Information Processing in Medical Imaging",
     "LNCS", "Lecture Notes in Computer Science",
     "PAMI", "Pattern Recognition", "Computerized Medical Imaging"): ("tier_2", 1.5),

    # --- Tier 1: Major top-tier conferences ---
    ("CVPR", "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
     "ICCV", "IEEE/CVF International Conference on Computer Vision",
     "ECCV", "European Conference on Computer Vision",
     "NeurIPS", "Neural Information Processing Systems",
     "ICML", "International Conference on Machine Learning",
     "ACL", "Annual Meeting of the Association for Computational Linguistics",
     "EMNLP", "NAACL", "KDD", "SIGIR", "SIGGRAPH",
     "TMI", ): ("tier_1", 1.3),

    # --- Tier 0: Standard / unknown venues (base weight) ---
}

# Build a lookup: normalised substring -> (tier_name, weight)
_VENUE_LOOKUP: dict[str, float] = {}
for substrs, (tier, weight) in _VENUE_TIERS.items():
    for s in substrs:
        _VENUE_LOOKUP[s.lower()] = weight


def _normalise_venue(name: str) -> str:
    """Normalise venue string for matching."""
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r"[\(\)\[\]\{\}]", "", s)
    s = re.sub(r"\s+/+\s*", "/", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_venue_weight(venue: str) -> float:
    """Return the venue impact weight ``W_venue`` for a given venue name.

    Strategy: try exact matches first, then substring matches.
    The highest weight among all matching substrings is returned.
    """
    if not venue:
        return 1.0  # base weight for unknown / preprint venues

    norm = _normalise_venue(venue)
    norm_lower = norm.lower()

    best = 1.0  # default base

    # 1) Exact match (case-insensitive)
    if norm_lower in _VENUE_LOOKUP:
        return _VENUE_LOOKUP[norm_lower]

    # 2) Key substring found inside venue name (e.g. "IEEE Trans..." contains "ieee")
    # 3) Venue name contained in key (e.g. key="TMI" matches venue="IEEE TMI")
    for key, w in _VENUE_LOOKUP.items():
        if (len(key) >= 3 and key in norm_lower) or (len(norm_lower) >= 3 and norm_lower in key):
            best = max(best, w)

    return best


# ---------------------------------------------------------------------------
# Time decay helper
# ---------------------------------------------------------------------------

def time_decay(year: int, base_year: int = 2027, alpha: float = 1.0) -> float:
    """Time-decay factor: older papers get a higher multiplier.

    ``t_decay = 1 / (base_year - Y)^alpha``

    For Y=2020, base_year=2027, alpha=1.0:  decay = 1/7 = 0.143
    For Y=2000, base_year=2027, alpha=1.0:  decay = 1/27 = 0.037
    For Y=1990, base_year=2027, alpha=1.0:  decay = 1/37 = 0.027

    Note: the formula is inverted — a 2020 paper gets MORE weight than a 2000 paper,
    because (2027-2020) < (2027-2000).  This is intentional: we want to reward
    papers that gained citations *recently* without the decade-old penalty.
    """
    delta = base_year - year
    if delta <= 0:
        delta = 1  # paper from base_year or later: avoid division-by-zero
    return 1.0 / (delta ** alpha)


# ---------------------------------------------------------------------------
# Local network density (co-citation within the result set)
# ---------------------------------------------------------------------------

def compute_local_network_boost(
    papers: list[dict],
    s2_api_key: str | None = None,
) -> dict[str, float]:
    """Compute a co-citation network density score within the result set.

    For each paper in ``papers``, checks how many other papers in the set
    cite it (via their ``references`` field, available from Semantic Scholar).
    The "in-degree" is normalised to [0, 1].

    This captures "latent anchors" — papers that are frequently cited by
    the retrieved set but may not be in the results themselves.

    Args:
        papers: List of paper dicts. Each should have ``doi`` or ``s2_id``.
        s2_api_key: Optional Semantic Scholar API key for fetching references.

    Returns:
        Dict mapping paper DOI (or s2_id fallback) -> normalised in-degree [0, 1].
    """
    # --- Phase 1: Build a set of known DOIs in the result set ---
    doi_set: set[str] = set()
    doi_map: dict[str, int] = {}  # doi -> index
    for i, p in enumerate(papers):
        doi = p.get("doi", "")
        if doi:
            doi_set.add(doi)
            doi_map[doi] = i

    # --- Phase 2: Count how many papers in the set cite each other ---
    # We inspect the ``references`` field of each paper (available from S2).
    # If a reference DOI matches a DOI in doi_set, that's a co-citation edge.
    in_degree: dict[int, int] = {i: 0 for i in range(len(papers))}

    for i, p in enumerate(papers):
        refs = p.get("references", [])
        if not refs:
            continue
        for ref_doi in refs:
            ref_doi_norm = ref_doi.lower().strip()
            if ref_doi_norm in doi_map:
                target_idx = doi_map[ref_doi_norm]
                if target_idx != i:
                    in_degree[target_idx] += 1

    # --- Phase 3: Normalise to [0, 1] ---
    max_degree = max(in_degree.values()) if in_degree else 0
    if max_degree == 0:
        return {p.get("doi", ""): 0.0 for p in papers}

    result: dict[str, float] = {}
    for i, p in enumerate(papers):
        key = p.get("doi", "") or p.get("s2_id", "") or str(i)
        result[key] = in_degree[i] / max_degree

    return result


# ---------------------------------------------------------------------------
# Scoring function
# ---------------------------------------------------------------------------

def score_anchor_papers(
    papers: list[dict],
    *,
    w_citation: float = 1.0,
    w_venue: float = 0.4,
    w_local: float = 0.3,
    w_network: float = 0.3,
    time_decay_alpha: float = 1.0,
    time_decay_base_year: int = 2027,
    max_anchor_count: int = 3,
) -> list[dict]:
    """Score papers and return the top-K anchor papers.

    Args:
        papers: List of paper dicts (post-dedup, post-verify). Each should have
            ``citation_count``, ``year``, ``venue``, and optionally ``local_relevance``
            (0~1 float from retrieval similarity).
        w_citation: Weight for the citation + time-decay component.
        w_venue: Weight for the venue impact component.
        w_local: Weight for the local relevance component (semantic similarity).
        w_network: Weight for the local co-citation network bonus.
        time_decay_alpha: Exponent for the time-decay function.
        time_decay_base_year: Reference year for time decay.
        max_anchor_count: Number of anchor papers to return (default 3).

    Returns:
        List of up to ``max_anchor_count`` paper dicts, sorted by score descending.
        Each returned dict gets a ``_anchor_score`` field added.
    """
    if not papers:
        return []

    n = len(papers)
    scores: list[tuple[float, dict]] = []

    # --- Normalise citation counts for sublinear scaling ---
    citation_values = [max(p.get("citation_count") or 0, 0) for p in papers]
    max_c = max(citation_values) if citation_values else 0

    # --- Normalise venue weights ---
    venue_weights = [get_venue_weight(p.get("venue", "")) for p in papers]
    max_venue_w = max(venue_weights) if venue_weights else 1.0

    # --- Normalise local relevance ---
    local_vals = [p.get("local_relevance", 0.0) for p in papers]
    max_local = max(local_vals) if local_vals else 1.0
    if max_local == 0:
        max_local = 1.0  # avoid division by zero

    # --- Get network boost (optional) ---
    network_boosts = compute_local_network_boost(papers)

    for i, p in enumerate(papers):
        C = max(citation_values[i], 0)
        Y = p.get("year")
        W_v = venue_weights[i]
        L = local_vals[i]
        doi = p.get("doi", "")
        nb = network_boosts.get(doi, 0.0)

        # Citation component: sublinear (log) scaling
        citation_component = math.log(C + 1)

        # Time decay
        if Y and isinstance(Y, int) and Y > 1900:
            td = time_decay(Y, time_decay_base_year, time_decay_alpha)
        else:
            td = 1.0  # unknown year: neutral

        # Combined score
        score = (
            w_citation * citation_component * td * W_v
            + w_venue * (W_v / max_venue_w if max_venue_w > 0 else 1.0)
            + w_local * (L / max_local)
            + w_network * nb
        )

        scores.append((score, p))

    # Sort by score descending; ties broken by citation count (desc), then year (desc)
    scores.sort(
        key=lambda x: (x[0], math.log((x[1].get("citation_count") or 0) + 1),
                       -(x[1].get("year") or 0)),
        reverse=True,
    )

    # Annotate and return top-K
    result = []
    for score, p in scores[:max_anchor_count]:
        annotated = dict(p)
        annotated["_anchor_score"] = round(score, 4)
        # Derive human-readable components for reporting
        annotated["_anchor_score_breakdown"] = {
            "citation_component": round(math.log(max(p.get("citation_count") or 0, 0) + 1), 4),
            "time_decay": round(
                time_decay(p["year"], time_decay_base_year, time_decay_alpha)
                if p.get("year") and isinstance(p["year"], int) and p["year"] > 1900
                else 1.0, 4
            ),
            "venue_weight": get_venue_weight(p.get("venue", "")),
            "venue_weight_normalised": round(
                get_venue_weight(p.get("venue", "")) / max_venue_w, 4
            ) if max_venue_w > 0 else 1.0,
            "local_relevance": round(p.get("local_relevance", 0.0), 4),
            "network_boost": round(
                network_boosts.get(p.get("doi", ""), 0.0), 4
            ),
        }
        result.append(annotated)

    return result


# ---------------------------------------------------------------------------
# CLI entry point (for debugging / standalone use)
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI: read papers from stdin (JSON) and print anchor papers."""
    raw = sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, dict) and "results" in data:
        papers = data["results"]
    else:
        papers = data

    anchors = score_anchor_papers(papers, max_anchor_count=3)

    out = {
        "query": "standalone",
        "total_papers": len(papers),
        "anchor_count": len(anchors),
        "anchors": anchors,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
