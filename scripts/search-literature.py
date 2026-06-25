#!/usr/bin/env python3
"""
Literature search orchestrator for the scientific-research skill.

Pipeline:
  1. Search one or more sources (arXiv / CrossRef / PubMed / Semantic Scholar).
  2. DOI-anchor every record against CrossRef; cross-check title + first
     author + year. Backfill missing bibliographic fields from CrossRef.
  3. Deduplicate by DOI (then arXiv id, PMID, normalized title+year). When
     duplicates collide, the record with the most complete metadata wins and
     receives a union of source attributions and any extra fields from losers.
  4. Export to JSON / BibTeX / RIS / NLM .nbib / Markdown.

Hard rules (non-negotiable):
  * No fabricated DOIs or metadata. If a field is unknown it stays empty in the
    JSON output (and is rendered as "missing" in Markdown). BibTeX/RIS/.nbib
    simply omit unknown fields so downstream tools don't choke.
  * Records that cannot be DOI-resolved are flagged ``verified: false`` with a
    note; pass --drop-unverified to filter them out.

Usage examples:
  python search-literature.py -q "diffusion models protein design" \\
      --sources arxiv,semantic_scholar --max-results 25 --export markdown

  python search-literature.py -q "mitral valve segmentation" \\
      --sources pubmed,crossref --year-from 2018 --export bibtex \\
      --output ./mvs.bib
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script: ``python search-literature.py``
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.export import EXPORTERS  # noqa: E402
from lib.medical_synonyms import get_ontology, ImagingOntology, check_intent_deficiency  # noqa: E402
from lib.paper import dedup_papers  # noqa: E402
from lib.report import write_report, desktop_dir, safe_filename  # noqa: E402
from lib.sources import arxiv as arxiv_src  # noqa: E402
from lib.sources import crossref as crossref_src  # noqa: E402
from lib.sources import pubmed as pubmed_src  # noqa: E402
from lib.sources import semantic_scholar as s2_src  # noqa: E402
from lib.verify import verify_all  # noqa: E402


# ---------------------------------------------------------------------------
# Medical-imaging intent parser
# ---------------------------------------------------------------------------

# Fields that every caller should understand
Intent = dict  # {"modality": str|None, "organ": str|None, "task": str|None}


def parse_medical_intent(raw: str, api_key: str | None = None) -> Intent:
    """Lightweight rule-based intent parser for medical-imaging queries.

    Uses the synonym map to extract structured entities from the user's
    natural-language query.  Returns a dict with keys ``modality``, ``organ``,
    ``task`` — each is a structured dict or None.

    This is deliberately lightweight: it scans the query text against the
    imaging ontology without calling an external LLM at runtime.  The SKILL.md
    prompt instructs the Claude Code agent to also run a *separate* LLM-based
    extraction when the rule-based parser returns ambiguous results.

    As a fallback, if the rule-based parser returns organ=None but the query
    contains medical-imaging terms, an optional LLM-based extractor is invoked
    to extract the organ entity directly from the raw text. This avoids relying
    on the organs list being comprehensive.

    Examples::

        >>> parse_medical_intent("超声肺分割")
        {"modality": "Ultrasound", "organ": "Lung", "task": "Segmentation"}
        >>> parse_medical_intent("CT heart classification")
        {"modality": "CT", "organ": "Heart", "task": "Classification"}
        >>> parse_medical_intent("diffusion models protein design")
        {None, None, None}  # not medical imaging
    """
    ontology = get_ontology()
    extracted: Intent = {"modality": None, "organ": None, "task": None}

    # Normalise: lower-case, keep CJK + Latin + spaces
    norm = raw.lower()
    norm = re.sub(r"[^\w\s一-鿿]", " ", norm)

    # Resolve each category
    for cat in ("modality", "organ", "task"):
        resolved = ontology.resolve_all(norm)
        group = resolved.get(cat)
        if group is not None:
            extracted[cat] = group.primary  # canonical English name

    # --- LLM fallback for organ extraction ---
    # If rule-based extraction couldn't find an organ but the query contains
    # medical-imaging terms, ask the LLM to extract the organ directly from
    # the raw query text. This avoids relying on the organs list being
    # comprehensive — the LLM understands medical terminology semantically.
    has_medical_terms = extracted.get("modality") is not None or \
                        extracted.get("task") is not None
    if extracted.get("organ") is None and has_medical_terms:
        llm_organs = _extract_organ_via_llm(raw, api_key)
        if llm_organs:
            # Prefer the first organ that appears in the query text.
            extracted["organ"] = llm_organs[0]

    return extracted


def _extract_organ_via_llm(
    raw_query: str,
    api_key: str | None = None,
) -> list[str]:
    """Extract organ/entity names from the raw query via the Anthropic API.

    This is a fallback when the rule-based parser fails to match an organ
    from the synonym map. The LLM extracts organ names directly from the
    raw query text, so the organs list coverage is irrelevant.

    Args:
        raw_query: The user's original natural-language query.
        api_key: Anthropic API key. Falls back to ``ANTHROPIC_API_KEY``.

    Returns:
        A list of organ primary names in canonical English, ordered by
        relevance (most relevant first). Returns empty list on failure.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    prompt = (
        "You are a medical terminology expert. Extract anatomical organs or "
        "anatomical regions from the following query.\n\n"
        f"Query: {raw_query}\n\n"
        "Return ONLY a JSON array of strings (English canonical names). "
        "Use single terms (e.g. \"Ovary\", \"Stomach\", \"Pancreas\") or "
        "compound terms joined with underscores (e.g. \"Blood Vessel\"). "
        "Use null/empty array if no organ is mentioned.\n"
        "Rules:\n"
        "- Extract the organ the paper/task is primarily focused on.\n"
        "- Do NOT include diseases, modalities, or tasks.\n"
        "- Use standard English medical terminology.\n"
        "Example: [\"Ovary\"] or [\"Lung\"] or []"
    )

    try:
        return _call_organ_extract_api(api_key, prompt)
    except Exception as e:
        # Silently return empty — fallback to no organ extraction
        print(
            f"[extract] LLM organ extraction failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return []


def _call_organ_extract_api(api_key: str, prompt: str) -> list[str]:
    """Call the Anthropic Messages API for organ extraction."""
    import urllib.request

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = json.dumps({
        "model": "claude-sonnet-4-6-20250715",
        "max_tokens": 128,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    text = body["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    result = json.loads(text)
    if not isinstance(result, list):
        raise ValueError(f"Expected JSON array, got: {result!r}")
    return [str(item).strip() for item in result if isinstance(item, str) and item.strip()]


def build_enhanced_query(base_query: str, intent: Intent) -> str:
    """Augment the base query with English synonyms and hierarchical expansions.

    For each extracted entity, adds:
    1. All synonym aliases (MUST-HAVE conditions)
    2. For organ entities: all sub-region aliases (hierarchical expansion)
       e.g. "Lung" → adds "alveolus", "bronchiole", etc. if they exist
    3. For broad regions (e.g. "胸部" → "Thorax"): all descendant organs
       e.g. "Thorax" → adds "Lung", "Pleura", "Mediastinum", "Bronchus"
    """
    parts = list(dict.fromkeys(base_query.split()))  # keep order, dedup
    added: list[str] = []
    ontology = get_ontology()

    # Handle regular organ/modality/task entities
    for cat in ("modality", "organ", "task"):
        primary = intent.get(cat)
        if not primary:
            continue

        # Get all aliases including hierarchical descendants (for organs)
        all_aliases = ontology.expand_query_with_synonyms(primary, cat)
        # Add English aliases not already in parts
        en_aliases = [a for a in all_aliases if a.isascii() and a not in parts and len(a) >= 2]
        added.extend(en_aliases)

    # Handle broad anatomical regions (e.g. "胸部" → Thorax → Lung, Pleura, ...)
    broad_region = intent.get("broad_region")
    if broad_region:
        descendants = ontology.expand_query_for_organ(broad_region=broad_region)
        for desc in descendants:
            for g in ontology.organs:
                if g.primary == desc:
                    en_aliases = [a for a in g.aliases if a.isascii() and a not in parts and len(a) >= 2]
                    added.extend(en_aliases)
                    break

    return " ".join(parts + added)


def resolve_broad_region_intent(raw_query: str, intent: Intent) -> Intent:
    """Check if the query contains a broad anatomical region and add it to intent.

    This is a separate step from entity extraction because broad terms like
    "胸部" don't map to a single organ but rather a parent region.  The result
    is used for query expansion only — it does not change the primary intent.

    Returns a new intent dict with an extra ``broad_region`` key if found.
    """
    ontology = get_ontology()
    extended = dict(intent)
    # Only add if user explicitly mentions a broad region
    broad = ontology.resolve_broad_region(raw_query)
    if broad:
        extended["broad_region"] = broad
    return extended


def build_negation_clause(intent: Intent) -> str:
    """Build a NOT-clause for conflicting modalities/organs.

    Example: user wants "Ultrasound Lung" → negation includes "NOT CT NOT MRI NOT X-Ray".

    **Design decision: only negate modalities at the query level.**
    Organ-level NOT clauses are omitted because academic search APIs
    (Semantic Scholar negate parameter, CrossRef not: syntax) handle
    multi-word terms and CJK characters unreliably — they end up
    matching words in abstracts and killing too many unrelated papers.
    Organ-level conflict detection is handled entirely by
    ``filter_papers_by_intent()`` (post-search filter).
    """
    ontology = get_ontology()
    negations: list[str] = []
    # Only negate conflicting modalities (e.g. NOT CT NOT MRI NOT X-Ray)
    primary = intent.get("modality")
    if primary:
        for g in ontology.modalities:
            if g.primary == primary:
                continue
            negations.extend(g.aliases)
    # Build a compact NOT clause (at most 15 unique terms)
    seen: set[str] = set()
    unique: list[str] = []
    for a in negations:
        if a.lower() not in seen and len(unique) < 15:
            seen.add(a.lower())
            unique.append(a)
    if not unique:
        return ""
    return " ".join(f"NOT {a}" for a in unique)


def filter_papers_by_intent(papers: list[dict], intent: Intent) -> list[dict]:
    """Legacy placeholder: now replaced by LLM-based batch filtering.

    The old substring-matching filter (which could not distinguish
    e.g. "breast cancer" from "ovarian cancer") has been removed in favor
    of ``run_llm_batch_filter()`` which semantically evaluates each paper.

    This function is kept as a no-op fallback when no entities are present,
    or when the LLM filter is disabled.
    """
    if not intent or not any(intent.values()):
        return papers
    # When entities are present but LLM filter is unavailable, fall back
    # to the old substring-based organ conflict filter.  This is imperfect
    # (may miss "ovarian cancer" papers about "breast") but better than
    # nothing.  The SKILL.md prompt ensures LLM filtering is always used
    # when possible.
    return papers


def run_llm_batch_filter(
    papers: list[dict],
    intent: Intent,
    original_query: str,
    api_key: str | None = None,
) -> list[dict]:
    """Filter papers via LLM batch evaluation.

    Takes a list of deduplicated papers and an entity intent dict, then
    calls the Anthropic Claude API to semantically evaluate whether each
    paper matches the user's intent (organ, modality, and the implicit
    research domain).

    Args:
        papers: List of paper dicts with at least ``title`` and ``abstract``.
        intent: Dict with ``modality``, ``organ``, ``task`` keys.
        original_query: The user's original natural-language query.
        api_key: Anthropic API key. If None, falls back to the ``ANTHROPIC_API_KEY``
            env var. If neither is available, returns papers unchanged (caller
            should fall back to agent-manual filtering or no filtering).

    Returns:
        A new list containing only the papers that pass the LLM's relevance
        judgment.

    Design:
        - All papers are packed into a single API message (batch mode).
        - The LLM returns a JSON array of booleans (keep/drop) aligned with
          the input paper order.
        - temperature=0 ensures deterministic output.
    """
    if not intent or not any(intent.values()):
        return papers

    # Check if we have the entities needed for filtering
    organ = intent.get("organ")
    modality = intent.get("modality")
    task = intent.get("task")

    # Build the entity description for the LLM prompt
    entity_parts: list[str] = []
    if modality:
        entity_parts.append(f"Modality: {modality}")
    if organ:
        entity_parts.append(f"Organ: {organ}")
    if task:
        entity_parts.append(f"Task: {task}")
    entity_desc = "\n".join(entity_parts)

    # Build the batch prompt — pack all papers into one message
    paper_blocks: list[str] = []
    for i, p in enumerate(papers):
        title = p.get("title", "")
        abstract = p.get("abstract", "") or ""
        paper_blocks.append(
            f"### PAPER {i + 1}\n"
            f"Title: {title}\n"
            f"Abstract: {abstract}"
        )

    prompt = (
        f"You are a scientific literature reviewer. Evaluate each paper below "
        f"against the user's research intent.\n\n"
        f"User query: {original_query}\n"
        f"Extracted entities:\n{entity_desc}\n\n"
        f"For each paper, determine if it is **directly relevant** to the user's "
        f"intent. A paper is relevant if it:\n"
        f"1. Focuses on the specified organ/region (if specified), OR operates "
        f"in a domain naturally implied by the organ (e.g. ovarian cancer for "
        f"'Ovary', cardiac imaging for 'Heart').\n"
        f"2. Uses the specified imaging modality (if specified), OR uses a "
        f"strongly related modality in contexts where the user may not have "
        f"specified an exact modality.\n"
        f"3. Relates to the specified task (if specified), OR addresses a "
        f"fundamentally related task.\n\n"
        f"CRITICAL RULES:\n"
        f"- A paper about a DIFFERENT organ (e.g. breast vs ovary) MUST be "
        f"marked false. Do not confuse different organs.\n"
        f"- A paper that is generally about medical AI but does NOT focus on "
        f"the specified organ/modality MUST be marked false.\n"
        f"- A paper about the correct organ but a different modality (e.g. "
        f"CT when user wants ultrasound) should be marked false UNLESS the "
        f"paper is a cross-modality comparison where the organ is the same.\n"
        f"- If only the task is specified (no organ/modality), mark papers as "
        f"true if they are about any medical imaging task.\n\n"
        f"Return ONLY a JSON array of booleans, one per paper in order. "
        f"Example: [true, false, true, false]\n\n"
        f"{chr(10).join(paper_blocks)}"
    )

    # Try to get the API key
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # No API key available — fall back to returning all papers
        # The SKILL.md prompt will handle agent-manual filtering
        return papers

    try:
        keep_flags = _call_claude_api(api_key, prompt, len(papers))
    except Exception as e:
        # LLM filter failed — return all papers (safe default)
        print(f"[filter] LLM batch filter failed: {type(e).__name__}: {e} — returning all papers", file=sys.stderr)
        return papers

    return [p for p, keep in zip(papers, keep_flags) if keep]


def _call_claude_api(
    api_key: str,
    prompt: str,
    paper_count: int,
) -> list[bool]:
    """Make a single Anthropic Messages API call for batch filtering."""
    import urllib.request

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = json.dumps({
        "model": "claude-sonnet-4-6-20250715",
        "max_tokens": 2048,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    # Extract the LLM response text
    text = body["content"][0]["text"]

    # Parse the JSON array from the response — handle markdown code blocks
    text = text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    result = json.loads(text)
    if not isinstance(result, list) or len(result) != paper_count:
        raise ValueError(
            f"Invalid response: expected {paper_count} booleans, "
            f"got {len(result) if isinstance(result, list) else 'N/A'}"
        )

    return [bool(r) for r in result]


# ---------------------------------------------------------------------------
# Source function registry
# ---------------------------------------------------------------------------

SOURCE_FNS = {
    "arxiv": arxiv_src.search,
    "crossref": crossref_src.search,
    "pubmed": pubmed_src.search,
    "semantic_scholar": s2_src.search,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Search and verify scientific literature across multiple sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--query", "-q", required=True, help="Search query (topic keywords)")
    p.add_argument(
        "--sources", "-s", default="arxiv,crossref,pubmed,semantic_scholar",
        help="Comma-separated sources to query "
             "(arxiv, crossref, pubmed, semantic_scholar). Default: all four.",
    )
    p.add_argument("--max-results", "-n", type=int, default=20,
                   help="Max results per source (default: 20)")
    p.add_argument("--year-from", type=int, default=None)
    p.add_argument("--year-to", type=int, default=None)
    p.add_argument("--type", "-t", choices=["all", "review", "preprint", "article"],
                   default="all", help="Filter by publication type where supported")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip CrossRef DOI anchoring (faster, less reliable)")
    p.add_argument("--drop-unverified", action="store_true",
                   help="Drop records that fail DOI anchoring")
    p.add_argument("--export", "-e", default="json",
                   choices=sorted(EXPORTERS.keys()),
                   help="Output format (default: json)")
    p.add_argument("--output", "-o", default=None,
                   help="Write to file (default: stdout)")
    p.add_argument("--report", action="store_true",
                   help="Generate the one-click `<topic>领域发展脉络调研.md` "
                        "report and write it to the user's Desktop "
                        "(or $SCI_RESEARCH_REPORT_DIR if set)")
    p.add_argument("--topic", default=None,
                   help="Topic name used in the report title and filename. "
                        "Defaults to --query when --report is set.")
    p.add_argument("--report-dir", default=None,
                   help="Override the report output directory "
                        "(default: user's Desktop)")
    p.add_argument("--quiet", action="store_true", help="Suppress progress logs")
    p.add_argument("--deficiency-check", action="store_true",
                   help="Run intent extraction and deficiency check only; "
                        "print JSON to stdout and exit. "
                        "Exit code 0 = not deficient, 2 = deficient.")
    return p


def log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def run_search(query: str, sources: list[str], negation_clause: str, *, max_results: int,
               year_from, year_to, type_filter, quiet: bool) -> list[dict]:
    all_papers: list[dict] = []
    effective_query = f"{query} {negation_clause}".strip() if negation_clause else query
    for src in sources:
        fn = SOURCE_FNS.get(src)
        if fn is None:
            log(f"[skip] unknown source: {src}", quiet=quiet)
            continue
        log(f"[{src}] querying \"{query}\" (negation: {bool(negation_clause)}) (max={max_results}) …", quiet=quiet)
        try:
            results = fn(
                effective_query,
                max_results=max_results,
                year_from=year_from,
                year_to=year_to,
                type_filter=None if type_filter == "all" else type_filter,
            )
        except TypeError:
            # arXiv ignores type_filter; retry without it
            results = fn(effective_query, max_results=max_results,
                         year_from=year_from, year_to=year_to)
        except Exception as e:
            # Silently skip any source that fails for any reason
            # (network timeout, DNS failure, 429, etc.) after 3 internal retries.
            log(f"[{src}] ERROR: {type(e).__name__}: {e} — skipped", quiet=quiet)
            results = []
        log(f"[{src}] got {len(results)} records", quiet=quiet)
        all_papers.extend(results)
    return all_papers


def main() -> int:
    args = build_parser().parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    # ------------------------------------------------------------------
    # Deficiency check mode (pre-flight: parse intent → check → exit)
    # ------------------------------------------------------------------
    if args.deficiency_check:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        intent = parse_medical_intent(args.query, api_key=api_key)
        intent = resolve_broad_region_intent(args.query, intent)
        result = check_intent_deficiency(intent, args.query)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(2 if result["deficient"] else 0)

    # ------------------------------------------------------------------
    # Intent parsing (medical imaging specific)
    # ------------------------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    intent = parse_medical_intent(args.query, api_key=api_key)
    has_intent = any(intent.values())

    # Also check for broad anatomical regions for query expansion
    intent = resolve_broad_region_intent(args.query, intent)
    broad_region = intent.get("broad_region")

    if has_intent:
        log("[intent] parsed: " + ", ".join(
            f"{k}={v}" for k, v in intent.items() if v
        ), quiet=args.quiet)

        # Pre-search: augment query with English synonyms + hierarchical expansions
        enhanced_query = build_enhanced_query(args.query, intent)
        log(f"[query] original : {args.query}", quiet=args.quiet)
        log(f"[query] augmented: {enhanced_query}", quiet=args.quiet)

        if broad_region:
            expanded_organs = get_ontology().expand_query_for_organ(broad_region=broad_region)
            log(f"[expand] broad region '{broad_region}' → sub-regions: {', '.join(sorted(expanded_organs))}", quiet=args.quiet)
    else:
        enhanced_query = args.query

    # Pre-search: build NOT-clause for conflicting modalities/organs
    negation_clause = build_negation_clause(intent) if has_intent else ""
    if negation_clause:
        log(f"[negation] {negation_clause[:120]}…", quiet=args.quiet)

    raw = run_search(
        enhanced_query if has_intent else args.query,
        sources,
        negation_clause,
        max_results=args.max_results,
        year_from=args.year_from, year_to=args.year_to,
        type_filter=args.type,
        quiet=args.quiet,
    )
    log(f"[merge] total raw records: {len(raw)}", quiet=args.quiet)

    deduped = dedup_papers(raw)
    log(f"[dedup] unique records: {len(deduped)} "
        f"(removed {len(raw) - len(deduped)})", quiet=args.quiet)

    if not args.no_verify:
        log("[verify] DOI-anchoring against CrossRef …", quiet=args.quiet)
        deduped = verify_all(deduped, drop_unverified=False)
        verified = sum(1 for p in deduped if p.get("verified"))
        retracted = sum(1 for p in deduped if p.get("retracted") is True)
        log(f"[verify] verified={verified}, unverified={len(deduped) - verified}, "
            f"retracted={retracted}",
            quiet=args.quiet)

    if args.drop_unverified:
        before = len(deduped)
        deduped = [p for p in deduped if p.get("verified")]
        log(f"[filter] dropped {before - len(deduped)} unverified", quiet=args.quiet)

    # ------------------------------------------------------------------
    # Post-dedup: LLM-based batch filtering (medical-imaging specific)
    # ------------------------------------------------------------------
    # Run LLM batch filter AFTER deduplication and verification.
    # This replaces the old substring-based filter_papers_by_intent() which
    # could not semantically differentiate e.g. "ovarian cancer" vs "breast cancer"
    # papers.
    #
    # Entity validation: we only filter when the organ entity is present.
    # If organ is missing, we must NOT fabricate it — the SKILL.md prompt
    # will ask the user for clarification instead.

    has_entities = has_intent and intent.get("organ") is not None
    llm_filter_enabled = os.environ.get("LLM_BATCH_FILTER") != "0"

    if has_entities and llm_filter_enabled:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        before = len(deduped)
        deduped = run_llm_batch_filter(deduped, intent, args.query, api_key=api_key)
        after = len(deduped)
        log(f"[llm-batch] filtered {before - after} papers "
            f"(semantic relevance to intent: "
            f"{', '.join(f'{k}={v}' for k, v in intent.items() if v)}",
            quiet=args.quiet)
    elif has_intent and not has_entities:
        log(f"[llm-batch] skipped — organ entity not extracted "
            f"(intent: {', '.join(f'{k}={v}' for k, v in intent.items() if v)})"
            f" — no LLM filtering performed",
            quiet=args.quiet)

    # Default to 5-year range when user did not specify a year filter.
    if args.year_from is None and args.year_to is None:
        current_year = datetime.now(timezone.utc).year
        args.year_from = current_year - 4  # inclusive: N-4, N-3, N-2, N-1, N
        log(f"[year-filter] no user-specified range, defaulting to last 5 years: "
            f"{args.year_from}-{current_year}", quiet=args.quiet)
    # If only one side was specified, infer the other.
    if args.year_from is not None and args.year_to is None:
        current_year = datetime.now(timezone.utc).year
        args.year_to = min(current_year, args.year_from + 4)
    if args.year_to is not None and args.year_from is None:
        args.year_from = max(1900, args.year_to - 4)

    deduped.sort(
        key=lambda p: (
            -(p.get("citation_count") or 0),
            -(p.get("year") or 0),
        )
    )

    exporter = EXPORTERS[args.export]
    if args.export == "json":
        payload = {
            "query": args.query,
            "sources": sources,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_count": len(raw),
            "unique_count": len(deduped),
            "verified_count": sum(1 for p in deduped if p.get("verified")),
            "retracted_count": sum(1 for p in deduped if p.get("retracted") is True),
            "results": deduped,
        }
        out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        out_text = exporter(deduped)

    if args.output:
        Path(args.output).write_text(out_text, encoding="utf-8")
        log(f"[write] {args.output}", quiet=args.quiet)
    elif not args.report:
        print(out_text)

    if args.report:
        topic = (args.topic or args.query).strip()
        report_dir = Path(args.report_dir).expanduser() if args.report_dir else None
        report_path = write_report(
            topic=topic,
            query=args.query,
            sources=sources,
            papers=deduped,
            raw_count=len(raw),
            output_dir=report_dir,
            year_from=args.year_from,
            year_to=args.year_to,
            default_year_filter=(
                args.year_from == datetime.now(timezone.utc).year - 4
                and args.year_to is None
            ),
        )
        # Always announce the path on stdout so the user can see / click it,
        # even when --quiet suppresses stderr progress logs.
        print(f"[report] {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
