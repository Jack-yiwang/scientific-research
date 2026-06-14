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
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script: ``python search-literature.py``
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.export import EXPORTERS  # noqa: E402
from lib.paper import dedup_papers  # noqa: E402
from lib.report import write_report, desktop_dir, safe_filename  # noqa: E402
from lib.sources import arxiv as arxiv_src  # noqa: E402
from lib.sources import crossref as crossref_src  # noqa: E402
from lib.sources import pubmed as pubmed_src  # noqa: E402
from lib.sources import semantic_scholar as s2_src  # noqa: E402
from lib.verify import verify_all  # noqa: E402


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
    return p


def log(msg: str, *, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def run_search(query: str, sources: list[str], *, max_results: int,
               year_from, year_to, type_filter, quiet: bool) -> list[dict]:
    all_papers: list[dict] = []
    for src in sources:
        fn = SOURCE_FNS.get(src)
        if fn is None:
            log(f"[skip] unknown source: {src}", quiet=quiet)
            continue
        log(f"[{src}] querying \"{query}\" (max={max_results}) …", quiet=quiet)
        try:
            results = fn(
                query,
                max_results=max_results,
                year_from=year_from,
                year_to=year_to,
                type_filter=None if type_filter == "all" else type_filter,
            )
        except TypeError:
            # arXiv ignores type_filter; retry without it
            results = fn(query, max_results=max_results,
                         year_from=year_from, year_to=year_to)
        log(f"[{src}] got {len(results)} records", quiet=quiet)
        all_papers.extend(results)
    return all_papers


def main() -> int:
    args = build_parser().parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    raw = run_search(
        args.query, sources,
        max_results=args.max_results,
        year_from=args.year_from, year_to=args.year_to,
        type_filter=args.type,
        quiet=args.quiet,
    )
    log(f"[merge] total raw records: {len(raw)}", quiet=args.quiet)

    if not args.no_verify:
        log("[verify] DOI-anchoring against CrossRef …", quiet=args.quiet)
        raw = verify_all(raw, drop_unverified=False)
        verified = sum(1 for p in raw if p.get("verified"))
        retracted = sum(1 for p in raw if p.get("retracted") is True)
        log(f"[verify] verified={verified}, unverified={len(raw) - verified}, "
            f"retracted={retracted}",
            quiet=args.quiet)

    deduped = dedup_papers(raw)
    log(f"[dedup] unique records: {len(deduped)} "
        f"(removed {len(raw) - len(deduped)})", quiet=args.quiet)

    if args.drop_unverified:
        before = len(deduped)
        deduped = [p for p in deduped if p.get("verified")]
        log(f"[filter] dropped {before - len(deduped)} unverified", quiet=args.quiet)

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
        )
        # Always announce the path on stdout so the user can see / click it,
        # even when --quiet suppresses stderr progress logs.
        print(f"[report] {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
