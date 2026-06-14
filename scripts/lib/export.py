"""Exporters: JSON, BibTeX, RIS, NLM (.nbib), Markdown table.

Fields the source did not provide are emitted as the literal string ``"missing"``
in human-readable formats (Markdown), and **omitted** in machine formats
(BibTeX/RIS/nbib) — emitting "missing" inside a real .bib file would corrupt
downstream tools. This is the contract we expose to callers.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from .paper import best_canonical_url


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "", s or "")
    return s[:24] or "anon"


def _bib_key(p: dict) -> str:
    first = (p.get("authors") or ["anon"])[0]
    last = first.split()[-1] if first else "anon"
    yr = p.get("year") or "n.d."
    title = (p.get("title") or "").split()
    word = title[0] if title else "untitled"
    return f"{_slug(last)}{yr}{_slug(word)}"


def to_json(papers: list[dict]) -> str:
    return json.dumps(papers, indent=2, ensure_ascii=False)


def to_bibtex(papers: Iterable[dict]) -> str:
    out: list[str] = []
    for p in papers:
        ptype = (p.get("type") or "").lower()
        if "review" in ptype or "journal" in ptype:
            entry = "article"
        elif p.get("arxiv_id") and not p.get("doi"):
            entry = "misc"
        elif "book" in ptype:
            entry = "book"
        elif "proceedings" in ptype or "conference" in ptype:
            entry = "inproceedings"
        else:
            entry = "article"
        fields: list[tuple[str, str]] = []

        def add(key: str, value):
            if value in ("", None, []):
                return
            v = value
            if isinstance(v, list):
                v = " and ".join(v)
            v = str(v).replace("{", "").replace("}", "")
            fields.append((key, v))

        add("title", p.get("title"))
        add("author", p.get("authors"))
        add("year", p.get("year"))
        add("journal", p.get("venue"))
        add("volume", p.get("volume"))
        add("number", p.get("issue"))
        add("pages", p.get("pages"))
        add("publisher", p.get("publisher"))
        add("doi", p.get("doi"))
        if p.get("arxiv_id"):
            add("eprint", p.get("arxiv_id"))
            add("archivePrefix", "arXiv")
        add("url", best_canonical_url(p))
        add("note", p.get("verification_notes"))

        body = ",\n  ".join(f"{k} = {{{v}}}" for k, v in fields)
        out.append(f"@{entry}{{{_bib_key(p)},\n  {body}\n}}")
    return "\n\n".join(out) + ("\n" if out else "")


def to_ris(papers: Iterable[dict]) -> str:
    lines: list[str] = []
    for p in papers:
        ptype = (p.get("type") or "").lower()
        if "review" in ptype or "journal" in ptype:
            ty = "JOUR"
        elif "book" in ptype:
            ty = "BOOK"
        elif "proceedings" in ptype or "conference" in ptype:
            ty = "CONF"
        elif p.get("arxiv_id"):
            ty = "UNPD"
        else:
            ty = "JOUR"
        lines.append(f"TY  - {ty}")
        if p.get("title"):
            lines.append(f"TI  - {p['title']}")
        for a in p.get("authors") or []:
            lines.append(f"AU  - {a}")
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        if p.get("venue"):
            lines.append(f"JO  - {p['venue']}")
        if p.get("volume"):
            lines.append(f"VL  - {p['volume']}")
        if p.get("issue"):
            lines.append(f"IS  - {p['issue']}")
        if p.get("pages"):
            lines.append(f"SP  - {p['pages']}")
        if p.get("doi"):
            lines.append(f"DO  - {p['doi']}")
        if p.get("publisher"):
            lines.append(f"PB  - {p['publisher']}")
        url = best_canonical_url(p)
        if url:
            lines.append(f"UR  - {url}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract']}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def to_nbib(papers: Iterable[dict]) -> str:
    """NLM/PubMed .nbib format (subset). Only emit records that have a PMID."""
    blocks: list[str] = []
    for p in papers:
        if not p.get("pmid"):
            continue
        lines = [f"PMID- {p['pmid']}"]
        if p.get("title"):
            lines.append(f"TI  - {p['title']}")
        for a in p.get("authors") or []:
            lines.append(f"AU  - {a}")
        if p.get("venue"):
            lines.append(f"JT  - {p['venue']}")
        if p.get("venue_short"):
            lines.append(f"TA  - {p['venue_short']}")
        if p.get("volume"):
            lines.append(f"VI  - {p['volume']}")
        if p.get("issue"):
            lines.append(f"IP  - {p['issue']}")
        if p.get("pages"):
            lines.append(f"PG  - {p['pages']}")
        if p.get("year"):
            lines.append(f"DP  - {p['year']}")
        if p.get("doi"):
            lines.append(f"AID - {p['doi']} [doi]")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _md_cell(v) -> str:
    if v in ("", None, [], {}):
        return "missing"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "missing"
    return str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(papers: list[dict]) -> str:
    lines = [
        "| # | Year | Title | Authors | Venue | Vol/Issue/Pages | DOI | Verified | Retracted | Sources |",
        "|---|------|-------|---------|-------|------------------|-----|----------|-----------|---------|",
    ]
    for i, p in enumerate(papers, 1):
        vol_iss = "/".join([
            str(p.get("volume") or "missing"),
            str(p.get("issue") or "missing"),
            str(p.get("pages") or "missing"),
        ])
        doi_cell = f"[{p['doi']}](https://doi.org/{p['doi']})" if p.get("doi") else "missing"
        retracted = p.get("retracted")
        if retracted is True:
            retracted_cell = "**YES**"
        elif retracted is False:
            retracted_cell = "no"
        else:
            retracted_cell = "unknown"
        lines.append("| " + " | ".join([
            str(i),
            _md_cell(p.get("year")),
            _md_cell(p.get("title")),
            _md_cell(p.get("authors")),
            _md_cell(p.get("venue")),
            vol_iss,
            doi_cell,
            "yes" if p.get("verified") else "no",
            retracted_cell,
            _md_cell(p.get("sources")),
        ]) + " |")
    return "\n".join(lines) + "\n"


EXPORTERS = {
    "json": to_json,
    "bibtex": to_bibtex,
    "ris": to_ris,
    "nbib": to_nbib,
    "markdown": to_markdown,
    "md": to_markdown,
}
