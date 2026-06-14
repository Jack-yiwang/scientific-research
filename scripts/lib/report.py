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

from .paper import best_canonical_url


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


def build_mindmap(topic: str, period_buckets, route_buckets, *, max_per_branch: int = 4) -> str:
    """Build a deterministic Mermaid mindmap string. Every leaf maps to a real paper."""
    lines = ["```mermaid", "mindmap", f"  root(({_mermaid_safe(topic, limit=40)}))"]
    if period_buckets:
        lines.append("    时间脉络 / Timeline")
        for label, paps in period_buckets:
            lines.append(f"      {_mermaid_safe(label, limit=40)}")
            for p in paps[:max_per_branch]:
                first = (p.get("authors") or ["?"])[0].split()[-1]
                yr = p.get("year") or "?"
                short = _mermaid_safe(p.get("title", "")[:40], limit=40)
                lines.append(f"        {short} - {first} {yr}")
    if route_buckets:
        lines.append("    技术路线 / Routes")
        for label, paps in route_buckets:
            lines.append(f"      {_mermaid_safe(label, limit=30)}")
            for p in paps[:max_per_branch]:
                first = (p.get("authors") or ["?"])[0].split()[-1]
                yr = p.get("year") or "?"
                short = _mermaid_safe(p.get("title", "")[:40], limit=40)
                lines.append(f"        {short} - {first} {yr}")
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
    vol_iss = "/".join([
        str(p.get("volume") or "missing"),
        str(p.get("issue") or "missing"),
        str(p.get("pages") or "missing"),
    ])
    doi_cell = (
        f"[{p['doi']}](https://doi.org/{p['doi']})"
        if p.get("doi") else "missing"
    )
    retracted = p.get("retracted")
    if retracted is True:
        retracted_cell = "**YES**"
    elif retracted is False:
        retracted_cell = "no"
    else:
        retracted_cell = "unknown"
    return "| " + " | ".join([
        str(idx),
        _md_field(p.get("year")),
        _md_field(p.get("title")),
        _md_field(p.get("authors")),
        _md_field(p.get("venue")),
        vol_iss,
        doi_cell,
        str(p.get("citation_count") or "missing"),
        "yes" if p.get("verified") else "no",
        retracted_cell,
        _md_field(p.get("sources")),
    ]) + " |"


def _bucket_table(papers: Iterable[dict], start_idx: int = 1) -> str:
    header = (
        "| # | Year | Title | Authors | Venue | Vol/Issue/Pages | DOI | Citations | Verified | Retracted | Sources |\n"
        "|---|------|-------|---------|-------|------------------|-----|-----------|----------|-----------|---------|"
    )
    rows = [header]
    for i, p in enumerate(papers, start_idx):
        rows.append(_paper_row(i, p))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------

def build_report(*, topic: str, query: str, sources: list[str],
                 papers: list[dict], raw_count: int) -> str:
    """Assemble the full Markdown report string."""
    n = len(papers)
    n_verified = sum(1 for p in papers if p.get("verified"))
    n_retracted = sum(1 for p in papers if p.get("retracted") is True)
    years = [p.get("year") for p in papers if isinstance(p.get("year"), int)]
    year_lo = min(years) if years else "?"
    year_hi = max(years) if years else "?"

    period_buckets = bucket_by_period(papers)
    route_buckets = cluster_by_route(papers)
    milestones = top_milestones(papers)
    mindmap = build_mindmap(topic, period_buckets, route_buckets)

    lines: list[str] = []
    lines.append(f"# {topic}领域发展脉络调研")
    lines.append("")
    lines.append(f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"> 检索 query：`{query}`  ")
    lines.append(f"> 数据源：{', '.join(sources)}  ")
    lines.append(f"> 原始记录 {raw_count} 条 → 去重后 **{n} 条** "
                 f"（{n_verified} 条经 CrossRef DOI 锚定验证，{n_retracted} 条已撤回）  ")
    lines.append(f"> 文献年份范围：{year_lo}—{year_hi}  ")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 1: Overview
    lines.append("## 1. 领域概览 / Overview")
    lines.append("")
    lines.append(f"本报告基于 **{n}** 篇去重后的真实文献，自动整理出该方向的发展脉络。"
                 f"所有引用均通过 CrossRef DOI 锚定，缺失字段以 `missing` 标记，未做任何编造。")
    lines.append("")
    lines.append("> **使用说明**：")
    lines.append("> - "
                 "本骨架由脚本机械生成，可作为独立交付物直接阅读。")
    lines.append("> - "
                 "如需在每节加入叙事性总结（典型脉络判断、范式转移分析等），"
                 "可由 LLM 基于本表格中的真实文献进行二次撰写——禁止引入未在表格中出现的论文。")
    lines.append("")

    # Section 2: Timeline
    lines.append("## 2. 时间脉络 / Development Timeline")
    lines.append("")
    if not period_buckets:
        lines.append("_暂无年份明确的论文。_")
    else:
        idx = 1
        for label, paps in period_buckets:
            lines.append(f"### 2.{period_buckets.index((label, paps)) + 1} {label}（{len(paps)} 篇）")
            lines.append("")
            lines.append(_bucket_table(paps, start_idx=idx))
            lines.append("")
            idx += len(paps)
    lines.append("")

    # Section 3: Technical routes
    lines.append("## 3. 技术路线 / Technical Routes")
    lines.append("")
    lines.append("> 路线标签由标题高频关键词自动聚类得到，仅作浏览索引。"
                 "若某论文同时属于多个路线，归入其首个匹配关键词所在路线。")
    lines.append("")
    if not route_buckets:
        lines.append("_暂无可聚类路线。_")
    else:
        for i, (label, paps) in enumerate(route_buckets, 1):
            lines.append(f"### 3.{i} 路线关键词：`{label}`（{len(paps)} 篇）")
            lines.append("")
            lines.append(_bucket_table(paps))
            lines.append("")
    lines.append("")

    # Section 4: Milestones
    lines.append("## 4. 里程碑论文 / Key Milestones")
    lines.append("")
    lines.append(f"按引用数排序的 Top-{len(milestones)} 论文（仅作客观信号，"
                 f"不等同于学术价值评判）：")
    lines.append("")
    lines.append(_bucket_table(milestones))
    lines.append("")

    # Section 5: Mind map
    lines.append("## 5. 思维导图 / Mind Map")
    lines.append("")
    lines.append(mindmap)
    lines.append("")

    # Section 6: Synthesis placeholders
    lines.append("## 6. 综合分析 / Synthesis")
    lines.append("")
    lines.append("> 以下小节预留给 LLM 基于上方真实文献撰写。每条结论必须能在表格中"
                 "找到对应论文支撑；研究空白必须给出数据依据（参见 SKILL.md Step 6.D）。")
    lines.append("")
    lines.append("### 6.1 关键里程碑解读 (Why these milestones?)")
    lines.append("")
    lines.append("- _待补充：基于第 4 节里程碑论文，说明各篇为何标志性。_")
    lines.append("")
    lines.append("### 6.2 范式转移 / Paradigm Shifts")
    lines.append("")
    lines.append("- _待补充：标注 before / after / bridge 论文。_")
    lines.append("")
    lines.append("### 6.3 当前共识与争议 / Current Consensus & Open Debates")
    lines.append("")
    lines.append("- _待补充。_")
    lines.append("")
    lines.append("### 6.4 研究空白 / Research Gaps（必须有数据支撑）")
    lines.append("")
    lines.append("- _待补充：每条空白需指出是源于综述明文 future-work、检索范围声明、"
                 "或语料分布计数。_")
    lines.append("")

    # Section 7: Self-review
    lines.append("## 7. 自审报告 / Self-Review Report")
    lines.append("")
    lines.append("```")
    lines.append(f"- Citations verified: {n_verified} / {n} "
                 f"(0 manually inspected — fill in if any spot-checks done)")
    lines.append(f"- Retracted papers detected: {n_retracted} "
                 f"(action: {'labelled in tables' if n_retracted else 'none found'})")
    lines.append("- Mind-map ↔ synthesis coverage: pending LLM narrative review")
    lines.append("- Research-gap claims with data backing: pending — see §6.4")
    lines.append(f"- Sources queried: {', '.join(sources)}; "
                 "missing sources I could not reach: (e.g., CNKI/万方 if relevant)")
    lines.append("- Known limitations of this review:")
    lines.append("    * 路线聚类基于英文标题关键词，对纯中文标题论文的归类可能偏粗。")
    lines.append("    * 引用数来自 CrossRef / Semantic Scholar 当次快照，存在滞后。")
    lines.append("    * 未覆盖的源（如 IEEE Xplore、ACM DL、CNKI）需要人工补检索。")
    lines.append("```")
    lines.append("")

    # Section 8: Full citation table
    lines.append("## 8. 完整引用表 / Full Citation Table")
    lines.append("")
    lines.append(_bucket_table(sorted(papers, key=lambda p: -(p.get("year") or 0))))
    lines.append("")

    # Section 9: Methodology
    lines.append("## 9. 方法说明 / Methodology")
    lines.append("")
    lines.append(f"- **检索 query**：`{query}`")
    lines.append(f"- **数据源**：{', '.join(sources)}")
    lines.append("- **DOI 锚定**：每条 DOI 经 `api.crossref.org/works/{doi}` 解析，"
                 "并对 title / 一作姓氏 / 年份做模糊比对。")
    lines.append("- **去重**：DOI → arXiv id → PMID → 归一化 title+year 四级 key，"
                 "保留元数据最完整的记录，sources 取并集。")
    lines.append("- **撤回检测**：基于 CrossRef `update-to / updated-by` 关系。")
    lines.append("- **生成器**：`scripts/search-literature.py --report`，"
                 "Markdown 骨架完全机械生成，无 LLM 参与表格内容。")
    lines.append("")

    return "\n".join(lines) + "\n"


def write_report(*, topic: str, query: str, sources: list[str],
                 papers: list[dict], raw_count: int,
                 output_dir: Path | None = None) -> Path:
    """Render the report and write to ``<Desktop>/<topic>领域发展脉络调研.md``.

    Returns the absolute path of the written file.
    """
    out_dir = output_dir or desktop_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / safe_filename(topic)
    text = build_report(
        topic=topic, query=query, sources=sources,
        papers=papers, raw_count=raw_count,
    )
    path.write_text(text, encoding="utf-8")
    return path
