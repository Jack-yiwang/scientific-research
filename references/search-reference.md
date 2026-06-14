# Literature Search Reference

Reference documentation for the scientific-research skill.

## Table of Contents

- [Source Reliability Tiers](#source-reliability-tiers)
- [Source Routing Table](#source-routing-table)
- [Search Strategies](#search-strategies)
- [Query Construction](#query-construction)
- [Citation Formats](#citation-formats)
- [Common Pitfalls](#common-pitfalls)
- [Mermaid Mindmap Tips](#mermaid-mindmap-tips)

## Source Reliability Tiers

| Tier | Source | Type | Reliability | Notes |
|------|--------|------|-------------|-------|
| T1 | PubMed | API | High | Gold standard for biomedicine |
| T1 | arXiv | API | High | Preprints, peer-reviewed later |
| T1 | CrossRef | API | High | DOI resolver, large coverage |
| T2 | Semantic Scholar | API | Good | Free-text + citation graph |
| T2 | Google Scholar | Web | Good | Broadest but scraped |
| T3 | CNKI / 万方 | Web | Good | Chinese-language literature |

## Source Routing Table

| User's field | Primary source | Secondary | Fallback |
|---|---|---|---|
| Biomedical / life sciences | PubMed | Semantic Scholar | CrossRef |
| CS / AI / ML | arXiv | Semantic Scholar | Google Scholar (web) |
| Physics / math | arXiv | Semantic Scholar | CrossRef |
| Chemistry | CrossRef | Semantic Scholar | PubMed |
| Cross-disciplinary | Semantic Scholar | CrossRef | Google Scholar (web) |
| Engineering / general | Semantic Scholar | CrossRef | Google Scholar (web) |
| Chinese literature | CNKI / 万方 | Semantic Scholar | Web search |

## Search Strategies

### PubMed
```
Basic:   [topic keywords] AND [species/group] AND [Publication Date]
Mesh:    MeSH terms for subject precision
Filter:  "Review"[Publication Type] for survey papers
```

### arXiv
```
Basic:   Subject: [category] AND Title/Abstract: [keywords]
Cats:    cs.AI, cs.LG, cs.CV, stat.ML, q-bio.BM, eess.IV (medical imaging), etc.
```

### Semantic Scholar
```
Basic:   Query with optional filters: year, citation_count, open_access
Fields:  computer_science, biology, physics, mathematics, medicine
```

### CrossRef
```
Basic:   Query via REST API with title/abstract keywords
Filter:  content_type, license, publication_date
```

### Google Scholar (via WebFetch)
```
Basic:   Search query with site:scholar.google.com
Tips:    Use quotes for exact title search, e.g., "Attention Is All You Need"
```

### CNKI / 万方
```
Basic:   Search via web (no public API)
Tips:    Use Chinese keywords for better results
```

## Query Construction

1. **Start specific**: Use domain-specific terminology, not lay terms.
2. **Use synonyms**: Search both "deep learning" and "neural networks" for broad topics.
3. **Use acronyms**: Search both full terms and acronyms (e.g., "convolutional neural network" and "CNN").
4. **Add method terms**: For technical routes, include method names (e.g., "attention mechanism", "transformer").
5. **Exclude noise**: Use NOT or exclude terms for irrelevant subfields.
6. **Language diversity**: For fields with Chinese contribution, search both English and Chinese terms.
7. **Time-aware queries**: When user wants recent trends, append year range filters.

## Citation Formats

Common bibliographic formats:

| Format | Use Case |
|--------|----------|
| BibTeX | LaTeX documents, reference managers |
| RIS | Citation manager import (EndNote, Zotero) |
| .nbib (NLM) | PubMed/NCBI format |
| APA / MLA | Human-readable |

## Common Pitfalls

1. **Fabricating citations** — The cardinal sin. Always verify with a real source.
2. **Including unrelated papers** — "Related" is not the same as "relevant to the research direction."
3. **Using outdated information** — A paper's journal may have changed names; verify current venue.
4. **Missing open-access alternatives** — If a DOI link is paywalled, check for arXiv/PMC versions.
5. **Ignoring negative results** — Important null findings should sometimes be noted (e.g., "Method X was shown ineffective for Y in [ref]").
6. **Over-relying on English databases** — Chinese literature may be missed; supplement with CNKI when relevant.
7. **Including conference vs journal confusion** — Some fields (CS) prioritize conferences; others (bio) prioritize journals. Know the domain conventions.

## Mermaid Mindmap Tips

When generating Mermaid mindmaps:

1. **Keep it readable**: Avoid more than 2 levels deep and ~8 children per node.
2. **Use concise labels**: e.g., "U-Net (Ronneberger, 2015)" not the full paper title.
3. **Group by meaningful categories**: Phases or technical routes, not individual papers.
4. **Fallback**: If the field is too complex for a single mindmap, provide a simplified overview and offer to expand specific branches.
5. **Syntax note**: Use the `mindmap` diagram type, not `graph` or `flowchart`.
