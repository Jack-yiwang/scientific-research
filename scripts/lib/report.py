"""
Deterministic Markdown review report generator.

Given a list of verified papers, produces a complete "<topic>领域发展脉络调研.md"
that the user can read directly. The skeleton is fully mechanical — every node
in the mindmap, every bullet in the timeline, and every milestone entry maps
back to a specific paper in the citation table. LLM narrative can be appended
on top, but the file is a usable deliverable on its own.

Output format choice: **Markdown**. Reasons:
  * LLMs produce the fewest format errors in Markdown vs. LaTeX/HTML/DOCX.
  * Renders natively on GitHub, VS Code, Obsidian, Typora, Bear, and most
    chat tools. Falls back to plain text gracefully.
  * Mermaid mindmap is embedded as a fenced block; viewers without Mermaid
    support still see a readable text outline.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .anchor_scorer import score_anchor_papers  # noqa: E402

from .citation_tree import build_citation_tree, render_tree_md  # noqa: E402

from .paper import best_canonical_url  # noqa: E402


# ---------------------------------------------------------------------------
# Filename / path helpers
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF      # CJK Unified
        or 0x3400 <= o <= 0x4DBF   # CJK Ext A
        or 0x20000 <= o <= 0x2A6DF # CJK Ext B
        or 0x3040 <= o <= 0x30FF   # Kana
        or 0xAC00 <= o <= 0xD7AF   # Hangul
    )


def safe_filename(topic: str) -> str:
    """Build a cross-platform-safe filename: '<topic>领域发展脉络调研.md'.

    Strips characters illegal on Windows (<>:"/\\|?*) and trims length so the
    final path stays under typical filesystem limits.
    """
    t = unicodedata.normalize("NFKC", topic).strip()
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = "未命名方向"
    # Soft length cap (filesystem limits are bytes, not chars; be generous)
    if len(t) > 80:
        t = t[:80].rstrip()
    return f"{t}领域发展脉络调研.md"


def desktop_dir() -> Path:
    """Locate the user's Desktop in a cross-platform way.

    Honours $SCI_RESEARCH_REPORT_DIR override (useful for headless / CI runs).
    On systems without a Desktop folder, falls back to the home directory.
    """
    override = os.environ.get("SCI_RESEARCH_REPORT_DIR")
    if override:
        p = Path(override).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    home = Path.home()
    candidates = [
        home / "Desktop",
        home / "桌面",       # zh-CN Windows / Linux locale
        home / "デスクトップ",  # ja
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    # Last resort: create ~/Desktop
    fallback = home / "Desktop"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    except OSError:
        return home


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------

# Stop-word list for keyword clustering. Kept minimal and English-only because
# arXiv / CrossRef titles are overwhelmingly English; CJK title keywords are
# preserved verbatim (we do not tokenize them — too unreliable without jieba).
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with",
    "via", "using", "based", "from", "by", "is", "are", "as", "at", "be",
    "this", "that", "these", "those", "we", "our", "their", "its", "it",
    "study", "studies", "paper", "approach", "approaches", "method", "methods",
    "model", "models", "novel", "new", "deep", "neural", "learning", "network",
    "networks", "analysis", "review", "survey", "towards", "toward", "over",
    "under", "between", "among", "into", "out", "up", "down", "without",
    "applications", "application", "based", "use", "used", "results", "result",
    "improved", "improving", "improvement", "system", "systems", "framework",
    "frameworks", "data", "high", "low", "large", "small",
}


def _normalize_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9\-]+", "", tok.lower())


def _title_keywords(title: str) -> list[str]:
    """Extract candidate keyword tokens from a title (English only, lowercased)."""
    if not title:
        return []
    out: list[str] = []
    for raw in re.split(r"\s+", title):
        tok = _normalize_token(raw)
        if not tok or tok in _STOP_WORDS or len(tok) < 3:
            continue
        if tok.isdigit():
            continue
        out.append(tok)
    return out


def _decade_label(year: int) -> str:
    if year < 2000:
        return f"{(year // 10) * 10}s 及以前"
    return f"{(year // 5) * 5}—{(year // 5) * 5 + 4}"


def bucket_by_period(papers: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group papers into 5-year buckets, sorted ascending by start year."""
    buckets: dict[tuple[int, str], list[dict]] = defaultdict(list)
    no_year: list[dict] = []
    for p in papers:
        y = p.get("year")
        if not isinstance(y, int):
            no_year.append(p)
            continue
        key = (y // 5) * 5 if y >= 2000 else (y // 10) * 10
        buckets[(key, _decade_label(y))].append(p)
    out = sorted(buckets.items(), key=lambda kv: kv[0][0])
    if no_year:
        out.append(((9999, "年份缺失"), no_year))
    return [(label, sorted(paps, key=lambda p: -(p.get("citation_count") or 0)))
            for (_, label), paps in out]


def cluster_by_route(papers: list[dict], max_routes: int = 6) -> list[tuple[str, list[dict]]]:
    """Coarse keyword clustering: pick the top-N most frequent non-stop-word
    tokens across titles, then assign each paper to its highest-frequency
    matching keyword. Papers with no matching keyword fall into "其他 / Misc".
    """
    counter: Counter[str] = Counter()
    paper_tokens: list[set[str]] = []
    for p in papers:
        toks = set(_title_keywords(p.get("title", "")))
        paper_tokens.append(toks)
        counter.update(toks)
    if not counter:
        return [("其他 / Misc", papers)]
    top = [t for t, _ in counter.most_common(max_routes)]
    rank = {t: i for i, t in enumerate(top)}
    routes: dict[str, list[dict]] = defaultdict(list)
    misc: list[dict] = []
    for p, toks in zip(papers, paper_tokens):
        match = sorted((t for t in toks if t in rank), key=lambda t: rank[t])
        if match:
            routes[match[0]].append(p)
        else:
            misc.append(p)
    out = [(t, sorted(routes[t], key=lambda x: -(x.get("citation_count") or 0)))
           for t in top if routes[t]]
    if misc:
        out.append(("其他 / Misc", misc))
    return out


def top_milestones(papers: list[dict], k: int = 8) -> list[dict]:
    """Pick top-K papers by citation_count (verified preferred). Stable order."""
    def score(p: dict) -> tuple:
        return (
            1 if p.get("verified") else 0,
            p.get("citation_count") or 0,
            -(p.get("year") or 0),
        )
    return sorted(papers, key=score, reverse=True)[:k]


# ---------------------------------------------------------------------------
# Mermaid generation
# ---------------------------------------------------------------------------

def _mermaid_safe(s: str, *, limit: int = 60) -> str:
    """Strip characters that break Mermaid mindmap labels."""
    if not s:
        return "untitled"
    s = re.sub(r"[\(\)\[\]\{\}\"`<>]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s or "untitled"


def build_mindmap(topic: str, papers: list[dict], *, max_per_branch: int = 6) -> str:
    """Build a deterministic Mermaid mindmap string. Every leaf maps to a real paper.

    Papers are bucketed by year (5-year windows) for a single timeline view.
    """
    # Bucket papers into 5-year windows
    buckets: dict[int, list[dict]] = defaultdict(list)
    no_year: list[dict] = []
    for p in papers:
        y = p.get("year")
        if not isinstance(y, int):
            no_year.append(p)
            continue
        key = (y // 5) * 5 if y >= 2000 else (y // 10) * 10
        buckets[key].append(p)

    lines = ["```mermaid", "mindmap", f"  root(({_mermaid_safe(topic, limit=40)}))"]
    lines.append("    时间脉络 / Timeline")

    for key in sorted(buckets.keys(), reverse=True):
        label = f"{key}—{key + 4}" if key >= 2000 else f"{(key // 10) * 10}s 及以前"
        lines.append(f"      {_mermaid_safe(label, limit=30)}")
        for p in buckets[key][:max_per_branch]:
            first = (p.get("authors") or ["?"])[0].split()[-1]
            yr = p.get("year") or "?"
            short = _mermaid_safe(p.get("title", "")[:40], limit=40)
            lines.append(f"        {short} - {first} {yr}")

    if no_year:
        lines.append("      年份缺失")
        for p in no_year[:max_per_branch]:
            first = (p.get("authors") or ["?"])[0].split()[-1]
            short = _mermaid_safe(p.get("title", "")[:40], limit=40)
            lines.append(f"        {short} - {first}")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Citation rendering
# ---------------------------------------------------------------------------

def _md_field(v) -> str:
    if v in ("", None, [], {}):
        return "missing"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) or "missing"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _paper_row(idx: int, p: dict) -> str:
    doi_cell = (
        f"[{p['doi']}](https://doi.org/{p['doi']})"
        if p.get("doi") else "missing"
    )
    return "| " + " | ".join([
        str(idx),
        _md_field(p.get("year")),
        _md_field(p.get("title")),
        _md_field(p.get("venue")),
        doi_cell,
        _md_field(p.get("sources")),
    ]) + " |"


def _bucket_table(papers: Iterable[dict], start_idx: int = 1) -> str:
    header = (
        "| # | Year | Title | Venue | DOI | Sources |\n"
        "|---|------|-------|-------|-----|---------|"
    )
    rows = [header]
    for i, p in enumerate(papers, start_idx):
        rows.append(_paper_row(i, p))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------

def build_report(*, topic: str, query: str, sources: list[str],
                 papers: list[dict], raw_count: int,
                 year_from: int | None = None,
                 year_to: int | None = None,
                 default_year_filter: bool = False) -> str:
    """Assemble the full Markdown report string."""
    n = len(papers)
    years = [p.get("year") for p in papers if isinstance(p.get("year"), int)]
    year_lo = min(years) if years else "?"
    year_hi = max(years) if years else "?"

    # Build mindmap from all papers (sorted by year descending)
    sorted_by_year = sorted(papers, key=lambda p: -(p.get("year") or 0))
    mindmap = build_mindmap(topic, sorted_by_year)

    # Score anchor papers from the result set
    anchors = score_anchor_papers(papers, max_anchor_count=3)

    lines: list[str] = []
    lines.append(f"# {topic}领域发展脉络调研")
    lines.append("")
    lines.append(f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"> 检索 query：`{query}`  ")
    lines.append(f"> 数据源：{', '.join(sources)}  ")
    lines.append(f"> 原始记录 {raw_count} 条 → 去重后 **{n} 条**  ")
    if year_from and year_to:
        lines.append(f"> 文献年份范围：{year_from}—{year_to}  ")
    else:
        lines.append(f"> 文献年份范围：{year_lo}—{year_hi}  ")
    if default_year_filter:
        lines.append("> 默认检索近五年文献（用户未指定年份范围）。  ")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 0: Anchor Papers
    if anchors:
        lines.append("## 0. 经典文献推荐 / Anchor Papers")
        lines.append("")
        lines.append("以下文献通过**多维度综合打分**（被引频次、时间衰减、期刊权重、局部共引网络）")
        lines.append("从检索结果中筛选出 1~3 篇奠基性或里程碑式论文。")
        lines.append("")
        for i, a in enumerate(anchors, 1):
            bd = a.get("_anchor_score_breakdown", {})
            score = a.get("_anchor_score", 0)
            lines.append(
                f"### {i}. [{a.get('title', 'missing')}]({a.get('url', 'missing')})"
            )
            lines.append("")
            lines.append(f"- **Authors**: {_md_field(a.get('authors'))}")
            lines.append(f"- **Year**: {_md_field(a.get('year'))}")
            lines.append(f"- **Venue**: {_md_field(a.get('venue'))}")
            lines.append(f"- **Citations**: {a.get('citation_count') or 'missing'}")
            lines.append(f"- **综合得分**: `{score:.4f}`")
            if bd:
                lines.append(
                    f"- 被引分量: `{bd.get('citation_component', 'N/A')}` | "
                    f"时间衰减: `{bd.get('time_decay', 'N/A')}` | "
                    f"期刊权重: `{bd.get('venue_weight_normalised', 'N/A')}` | "
                    f"局部共引: `{bd.get('network_boost', 'N/A')}`"
                )
            lines.append("")

    # Section 0.5: Citation Tree (around the top anchor paper)
    if anchors:
        top_anchor = anchors[0]
        try:
            tree = build_citation_tree(top_anchor, max_backward=3, max_forward=3)
            tree_md = render_tree_md(tree)
            if tree_md:
                lines.append("---")
                lines.append("")
                lines.append(tree_md)
                lines.append("")
        except Exception as e:
            # Citation tree is optional; fail gracefully.
            lines.append("")
            lines.append(f"> 注：引用树构建失败（{e}），不影响其他报告内容。")
            lines.append("")

    # Section 1: Overview
    lines.append("## 1. 领域概览 / Overview")
    lines.append("")
    lines.append(f"本报告基于 **{n}** 篇去重后的真实文献，自动整理出该方向的发展脉络。"
                 f"所有引用均经 DOI 去重验证，缺失字段以 `missing` 标记，未做任何编造。")
    lines.append("")

    # Section 2: All papers
    lines.append("## 2. 文献列表 / Literature List")
    lines.append("")
    lines.append(f"共 {n} 篇，按年份降序排列。")
    lines.append("")
    lines.append(_bucket_table(sorted_by_year))
    lines.append("")

    # Section 3: Mind map
    lines.append("## 3. 思维导图 / Mind Map")
    lines.append("")
    lines.append(mindmap)
    lines.append("")

    return "\n".join(lines) + "\n"


def write_report(*, topic: str, query: str, sources: list[str],
                 papers: list[dict], raw_count: int,
                 output_dir: Path | None = None,
                 year_from: int | None = None,
                 year_to: int | None = None,
                 default_year_filter: bool = False) -> Path:
    """Render the report and write to ``<Desktop>/<topic>领域发展脉络调研.md``.

    Returns the absolute path of the written file.
    """
    out_dir = output_dir or desktop_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / safe_filename(topic)
    text = build_report(
        topic=topic, query=query, sources=sources,
        papers=papers, raw_count=raw_count,
        year_from=year_from, year_to=year_to,
        default_year_filter=default_year_filter,
    )
    path.write_text(text, encoding="utf-8")
    return path
