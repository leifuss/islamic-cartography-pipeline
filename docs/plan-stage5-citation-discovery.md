# Stage 5: Citation Discovery — "The tool tells me what I'm missing"

> **Goal:** Using extracted bibliographies and the user's research
> questions, Scholion identifies cited works the researcher doesn't
> yet have, ranks them by relevance, and offers to import them
> automatically into Zotero.

## 1. User Stories

- *As a researcher, I want to know which works are most frequently cited
  across my sources, so I can prioritise my reading.*
- *As a researcher, I want to discover works that my sources cite but
  that I don't yet have in my bibliography.*
- *As a researcher writing about a specific topic, I want to know which
  of my sources discuss it and what they cite when they do.*
- *As a researcher, I want to import missing cited works into my Zotero
  library with one click.*

## 2. The Citation Discovery Pipeline

```
Stage 3 outputs:
  data/texts/{key}/page_texts.json    (full text)
  data/texts/{key}/bibliography.json  (extracted references)
      │
      ├──→ Step 1: AGGREGATE
      │    Collect all extracted bibliographic references across
      │    the corpus.  Normalise author names, years, titles.
      │    Output: data/citation_graph/all_references.json
      │
      ├──→ Step 2: RESOLVE
      │    Match each reference to a real-world scholarly record
      │    using OpenAlex / CrossRef / DOI lookup.
      │    Output: data/citation_graph/resolved_references.json
      │
      ├──→ Step 3: CROSS-CHECK
      │    Compare resolved references against the user's inventory.
      │    Flag: "you have this" / "you don't have this"
      │    Output: data/citation_graph/gap_analysis.json
      │
      ├──→ Step 4: RANK
      │    Rank missing works by:
      │      - Citation frequency (how many of your sources cite it)
      │      - Topical relevance (to user's research questions)
      │      - Recency (prefer newer works)
      │      - Availability (prefer works with open access PDFs)
      │    Output: data/citation_graph/recommendations.json
      │
      └──→ Step 5: PRESENT + IMPORT
           Show ranked recommendations in the explorer.
           One-click import to Zotero via the web API.
```

## 3. Detailed Design

### 3.1 Step 1: Aggregate References

**Input:** `data/texts/{key}/bibliography.json` for each document.

The bibliography extraction script (`06_extract_bibliography.py`)
already produces structured bibliography entries.  We need to collect
and normalise them.

```python
# scripts/build_citation_graph.py (new)

def aggregate_references(texts_dir: Path) -> list:
    """Collect all bibliography entries across the corpus."""
    all_refs = []
    for bib_path in texts_dir.glob("*/bibliography.json"):
        doc_key = bib_path.parent.name
        bib = json.loads(bib_path.read_text())
        for ref in bib.get("references", []):
            all_refs.append({
                "citing_doc": doc_key,
                "raw_text": ref.get("raw", ""),
                "authors": ref.get("authors", ""),
                "year": ref.get("year", ""),
                "title": ref.get("title", ""),
                "normalised_key": _normalise(ref),
            })
    return all_refs
```

**Normalisation:** Create a canonical key for each reference so that
"Smith, J. (2020). On Maps." and "Smith 2020" and "Smith, On Maps, 2020"
all resolve to the same work.

```python
def _normalise(ref: dict) -> str:
    """Create a canonical key: first_author_surname + year + title_words."""
    surname = ref.get("authors", "").split(",")[0].split(";")[0].strip().lower()
    year = ref.get("year", "").strip()
    # First 4 significant words of title (skip articles)
    title = ref.get("title", "").lower()
    words = [w for w in title.split() if w not in ("the", "a", "an", "of", "in", "on")]
    title_key = "-".join(words[:4])
    return f"{surname}:{year}:{title_key}"
```

### 3.2 Step 2: Resolve References

Match normalised references to real-world scholarly records using
free APIs.

**Primary: OpenAlex** (free, no API key needed)

```python
import requests

def resolve_via_openalex(ref: dict) -> dict | None:
    """Look up a reference in OpenAlex."""
    query = f"{ref['authors']} {ref['year']} {ref['title']}"
    resp = requests.get(
        "https://api.openalex.org/works",
        params={"search": query, "per_page": 3},
        headers={"User-Agent": "mailto:user@example.com"},  # polite pool
    )
    if resp.ok and resp.json().get("results"):
        best = resp.json()["results"][0]
        return {
            "openalex_id": best["id"],
            "doi": best.get("doi"),
            "title": best.get("title"),
            "year": best.get("publication_year"),
            "authors": [a["author"]["display_name"]
                        for a in best.get("authorships", [])],
            "open_access": best.get("open_access", {}).get("is_oa", False),
            "oa_url": best.get("open_access", {}).get("oa_url"),
            "cited_by_count": best.get("cited_by_count", 0),
            "type": best.get("type"),
        }
    return None
```

**Fallback: CrossRef** (free, no API key needed)

```python
def resolve_via_crossref(ref: dict) -> dict | None:
    """Look up a reference in CrossRef."""
    query = f"{ref['authors']} {ref['title']}"
    resp = requests.get(
        "https://api.crossref.org/works",
        params={"query": query, "rows": 3},
    )
    if resp.ok:
        items = resp.json().get("message", {}).get("items", [])
        if items:
            best = items[0]
            return {
                "doi": best.get("DOI"),
                "title": best.get("title", [""])[0],
                "year": str(best.get("published-print", {}).get(
                    "date-parts", [[None]])[0][0] or ""),
                "authors": [f"{a.get('family', '')} {a.get('given', '')}"
                            for a in best.get("author", [])],
                "type": best.get("type"),
            }
    return None
```

**Rate limiting:** OpenAlex allows 10 req/sec (polite pool) or 100k
req/day.  CrossRef allows ~50 req/sec.  For a 500-doc corpus with
~5000 references, this takes ~10 minutes.

**Caching:** Resolved references are cached in
`data/citation_graph/resolved_references.json`.  Re-running only
resolves new/unresolved references.

### 3.3 Step 3: Cross-Check Against Inventory

```python
def cross_check(resolved: list, inventory: list) -> list:
    """Flag which resolved references the user already has."""
    inv_keys = set()
    for item in inventory:
        # Match by DOI
        if item.get("url") and "doi.org" in item["url"]:
            inv_keys.add(item["url"].split("doi.org/")[-1].lower())
        # Match by normalised key
        inv_keys.add(_normalise({
            "authors": item.get("authors", ""),
            "year": item.get("year", ""),
            "title": item.get("title", ""),
        }))

    for ref in resolved:
        doi = (ref.get("doi") or "").replace("https://doi.org/", "").lower()
        ref["in_library"] = (
            doi in inv_keys or
            ref.get("normalised_key", "") in inv_keys
        )
    return resolved
```

### 3.4 Step 4: Rank Recommendations

```python
def rank_recommendations(refs: list, research_questions: list = None) -> list:
    """Rank missing works by relevance."""
    missing = [r for r in refs if not r.get("in_library")]

    # Count how many of the user's sources cite each work
    citation_counts = Counter(r["normalised_key"] for r in missing)

    for ref in missing:
        nkey = ref["normalised_key"]
        ref["local_citation_count"] = citation_counts[nkey]
        ref["global_citation_count"] = ref.get("cited_by_count", 0)
        ref["has_open_access"] = ref.get("open_access", False)

        # Composite score
        ref["relevance_score"] = (
            ref["local_citation_count"] * 10 +   # heavily weight local citations
            min(ref["global_citation_count"] / 100, 5) +  # cap global impact
            (2 if ref["has_open_access"] else 0) +  # bonus for accessible
            (1 if int(ref.get("year", 0) or 0) >= 2010 else 0)  # recency
        )

    # Optional: LLM-based relevance scoring against research questions
    if research_questions:
        missing = _llm_relevance_boost(missing, research_questions)

    return sorted(missing, key=lambda r: r["relevance_score"], reverse=True)
```

### 3.5 Step 5: Present + Import

#### Explorer UI: "Discover" Tab

```
┌─────────────────────────────────────────────────────────┐
│  📚 Citation Discovery                                  │
│                                                         │
│  Your 247 sources cite 1,843 unique works.              │
│  You have 247 of them. Here are the top works           │
│  you're missing:                                        │
│                                                         │
│  1. Harley & Woodward (1987) "History of               │
│     Cartography, Vol. 2"                                │
│     Cited by 34 of your sources · 2,891 global cites   │
│     📖 Open Access available                            │
│     [Import to Zotero] [View on OpenAlex]               │
│                                                         │
│  2. Tibbetts (1992) "The Beginnings of a                │
│     Cartographic Tradition"                              │
│     Cited by 28 of your sources · 412 global cites     │
│     [Import to Zotero] [View on OpenAlex]               │
│                                                         │
│  Showing 50 of 312 missing works                        │
│  [Show more] [Export as BibTeX]                          │
│                                                         │
│  Research question filter:                               │
│  ┌─────────────────────────────────────────┐            │
│  │ Ottoman reception of Ptolemaic maps     │            │
│  └─────────────────────────────────────────┘            │
│  [Find relevant missing works →]                        │
└─────────────────────────────────────────────────────────┘
```

#### One-Click Zotero Import

When the user clicks "Import to Zotero," we use the Zotero web API
to create a new item:

```python
def import_to_zotero(library: ZoteroLibrary, ref: dict) -> str:
    """Create a new Zotero item from a resolved reference."""
    template = library.client.item_template("journalArticle")
    template["title"] = ref.get("title", "")
    template["date"] = ref.get("year", "")
    template["DOI"] = ref.get("doi", "")
    template["url"] = ref.get("oa_url") or f"https://doi.org/{ref.get('doi', '')}"
    template["creators"] = [
        {"creatorType": "author", "name": name}
        for name in ref.get("authors", [])
    ]
    result = library.client.create_items([template])
    return result
```

**Note:** This requires the Zotero API key to have *write* access.
The setup wizard should request this.

## 4. Research Question Integration

The user can optionally provide research questions (stored in
`data/research_questions.json`):

```json
[
  "How did Ottoman cartographers adapt Ptolemaic projection methods?",
  "What role did maritime trade routes play in Islamic map-making?",
  "How were astronomical observations integrated into cartographic practice?"
]
```

These are used to:
1. **Filter passages:** Use search (Stage 4) to find passages in the
   corpus that discuss each question.
2. **Extract local citations:** Identify which works are cited in
   those specific passages (not just in the bibliography).
3. **Boost relevance:** Missing works cited in passages related to the
   user's questions get a higher relevance score.

This is the "killer feature" — personalised literature recommendations
based on the intersection of the user's existing bibliography and
their specific research questions.

## 5. Data Outputs

| File | Contents |
|------|----------|
| `data/citation_graph/all_references.json` | All extracted references, normalised |
| `data/citation_graph/resolved_references.json` | References matched to OpenAlex/CrossRef |
| `data/citation_graph/gap_analysis.json` | Cross-check results (have/don't have) |
| `data/citation_graph/recommendations.json` | Ranked missing works |
| `data/citation_graph/citation_network.json` | Who-cites-whom graph (for vis) |
| `data/research_questions.json` | User's research questions |

## 6. Visualisation: Citation Network

A graph visualisation showing:
- **Nodes:** Works in the user's library (blue) + frequently cited
  missing works (orange)
- **Edges:** Citation relationships
- **Clusters:** Topically related works cluster together
- **Size:** Node size proportional to citation count

**Library:** [D3-force](https://d3js.org/) or
[Cytoscape.js](https://js.cytoscape.org/) — both work client-side,
no server needed.

This gives the researcher a visual map of their bibliography's
intellectual landscape, with gaps highlighted.

## 7. Implementation Tasks

| # | Task | Effort | Priority |
|---|------|--------|----------|
| 5.1 | Reference aggregation + normalisation | 1 day | P0 |
| 5.2 | OpenAlex resolution | 1 day | P0 |
| 5.3 | CrossRef fallback resolution | 0.5 day | P0 |
| 5.4 | Cross-check against inventory | 0.5 day | P0 |
| 5.5 | Ranking algorithm | 1 day | P0 |
| 5.6 | Discovery UI in explorer | 2 days | P0 |
| 5.7 | One-click Zotero import | 1 day | P1 |
| 5.8 | Research question integration | 1.5 days | P1 |
| 5.9 | Citation network visualisation | 2 days | P2 |
| 5.10 | LLM relevance boosting | 1 day | P2 |
| 5.11 | BibTeX export of recommendations | 0.5 day | P2 |
| 5.12 | GitHub Actions workflow | 0.5 day | P0 |
| **Total (P0)** | | **~6 days** | |
| **Total (all)** | | **~12 days** | |

## 8. API Usage and Costs

| API | Rate limit | Cost | Usage for 500-doc corpus |
|-----|-----------|------|-------------------------|
| OpenAlex | 10 req/sec (polite pool) | Free | ~5000 requests (~8 min) |
| CrossRef | ~50 req/sec | Free | ~1000 requests (fallback only) |
| Zotero write API | 50 req/sec | Free | ~50 requests (imports) |
| LLM (relevance scoring) | Depends on provider | ~$0.01/query | ~$0.50 (optional) |

**Total cost:** Free for core features. ~$0.50 for LLM-enhanced ranking.

## 9. Testing Plan

| Test case | Method |
|-----------|--------|
| Aggregate references from 10 docs | Unit test |
| Normalisation handles "Smith 2020" variants | Unit test |
| OpenAlex resolves a known DOI | Integration test |
| CrossRef fallback when OpenAlex misses | Integration test |
| Cross-check correctly identifies items in library | Unit test |
| Ranking puts highly-cited items first | Unit test |
| Zotero import creates correct item | Integration test |
| Discovery UI renders with 0 / 50 / 500 recs | Visual test |
| Citation network renders for 100-node graph | Performance test |

## 10. Open Questions

1. **In-text citation extraction:** The current bibliography extraction
   gets the bibliography section at the end of each document.  For
   research-question-aware discovery, we also need to extract *inline*
   citations (e.g., "(Smith 2020, p.45)") and associate them with the
   surrounding text.  This is harder but much more powerful.  **Plan:**
   Add inline citation extraction as a sub-task of 5.8.

2. **Duplicate resolution across references:** Different sources may
   cite the same work with slightly different metadata.  The
   normalisation key helps but won't catch everything.  OpenAlex
   resolution is the most reliable deduplication method.

3. **Non-English references:** Arabic/Persian/Turkish references may
   not resolve well in OpenAlex (which has better coverage of English-
   language scholarship).  **Mitigation:** Show unresolved references
   separately with a "manually verify" option.

4. **Write access to Zotero:** The current setup wizard only requests
   read access.  For one-click import, we need write access.
   **Plan:** Add a note to the setup wizard: "For full features,
   grant read+write access to your API key."
