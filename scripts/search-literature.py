#!/usr/bin/env python3
"""
Literature search helper for scientific-research skill.

Searches across multiple academic sources and outputs structured results.
Requires MCP tools (paper-search, PubMed, etc.) to be available.

Usage:
  python search-literature.py --query "diffusion models protein design" --source semantic_scholar --max-results 20
  python search-literature.py --query "attention mechanism transformer" --source crossref --max-results 15
  python search-literature.py --query "CRISPR gene therapy" --source pubmed --max-results 30 --type review

Outputs JSON to stdout. Pipe to jq or a file for processing.
"""

import argparse
import json
import sys
from datetime import datetime, timezone


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search academic literature across multiple sources."
    )
    parser.add_argument(
        "--query", "-q", required=True,
        help="Search query (topic keywords)"
    )
    parser.add_argument(
        "--source", "-s", required=True,
        choices=["pubmed", "crossref", "arxiv", "semantic_scholar", "google_scholar"],
        help="Source to search"
    )
    parser.add_argument(
        "--max-results", "-n", type=int, default=20,
        help="Maximum number of results (default: 20)"
    )
    parser.add_argument(
        "--type", "-t", choices=["all", "review", "preprint", "article"],
        default="all",
        help="Filter by publication type (default: all)"
    )
    parser.add_argument(
        "--year-from", type=int, default=None,
        help="Filter papers from this year (inclusive)"
    )
    parser.add_argument(
        "--year-to", type=int, default=None,
        help="Filter papers to this year (inclusive)"
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Save results to file (default: stdout)"
    )
    return parser


def build_links(paper: dict) -> dict:
    """Build traceable access URLs for a paper."""
    links = {}
    doi = paper.get("doi", "")
    if doi:
        links["doi"] = f"https://doi.org/{doi}"
    arxiv_id = paper.get("arxiv_id", "")
    if arxiv_id:
        links["arxiv"] = f"https://arxiv.org/abs/{arxiv_id}"
    pmid = paper.get("pmid", "")
    if pmid:
        links["pubmed"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    pmcid = paper.get("pmcid", "")
    if pmcid:
        links["pmc"] = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    if paper.get("pdf_url"):
        links["pdf"] = paper["pdf_url"]
    elif paper.get("external_url"):
        links["publisher"] = paper["external_url"]
    return {k: v for k, v in links.items() if v}


def format_result(paper: dict) -> dict:
    """Normalize a paper record to a common format."""
    return {
        "title": paper.get("title", "N/A"),
        "authors": paper.get("authors", []),
        "year": paper.get("year", "N/A"),
        "venue": paper.get("venue", "N/A"),
        "abstract": paper.get("abstract", ""),
        "citation_count": paper.get("citation_count", 0),
        "doi": paper.get("doi", ""),
        "arxiv_id": paper.get("arxiv_id", ""),
        "pmid": paper.get("pmid", ""),
        "open_access": paper.get("open_access", False),
        "links": build_links(paper),
        "source": paper.get("_source", "unknown"),
    }


def verify_paper(paper: dict) -> bool:
    """Basic verification that a paper has required fields."""
    if not paper.get("title") or paper["title"] == "N/A":
        return False
    links = build_links(paper)
    if not links and not paper.get("doi"):
        return False
    return True


def main():
    parser = build_parser()
    args = parser.parse_args()

    print(f"Searching {args.source} for: \"{args.query}\"", file=sys.stderr)
    print(f"Max results: {args.max_results}, Type: {args.type}, "
          f"Year range: {args.year_from or 'all'}-{args.year_to or 'all'}", file=sys.stderr)

    print("Note: Direct MCP tool access requires Claude Code MCP configuration.", file=sys.stderr)
    print("Use this script's logic via MCP tool calls in the skill workflow.", file=sys.stderr)

    output = {
        "search_query": args.query,
        "source": args.source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_found": 0,
        "verified": 0,
        "skipped": 0,
        "results": [],
    }

    json_str = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"Results saved to {args.output}", file=sys.stderr)
    else:
        print(json_str)


if __name__ == "__main__":
    main()
