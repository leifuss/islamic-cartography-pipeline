# Stage 6: AI-Assisted Academic Commentary — "I get scholarly context for my key texts"

> **Goal:** For the researcher's most important texts, Scholion
> generates structured academic commentary: reception history,
> contested claims, superseded arguments, and connections to the
> wider literature — all grounded in the researcher's own corpus.

## 1. User Stories

- *As a researcher, I want to know which of my sources have been
  superseded by later work, so I don't rely on outdated arguments.*
- *As a researcher reading a key text, I want to see what other
  scholars in my bibliography say about its claims.*
- *As a researcher, I want a "state of the debate" summary for
  controversial topics that appear across my sources.*
- *As a new PhD student, I want orientation commentary that helps
  me understand the significance of foundational texts in my field.*

## 2. What This Is (and Isn't)

**This IS:**
- Commentary grounded in the researcher's own corpus
- Synthesis across multiple sources (not just summarising one text)
- Explicit about uncertainty ("Sources X and Y disagree on this point")
- Citable (every claim links back to a specific passage)

**This is NOT:**
- A substitute for reading the texts
- Hallucinated information presented as fact
- A Wikipedia-style summary from general knowledge
- Literary criticism or aesthetic judgment

The commentary is generated once per document (or per topic) and
served as static HTML/JSON — not real-time chat.

## 3. Commentary Types

### 3.1 Document-Level Commentary

For each important document in the corpus, generate a structured
commentary panel displayed alongside the reader:

```
┌─────────────────────────────────────────────────┐
│  📖 Reader: Karamustafa (1992)                  │
│  "Introduction to Ottoman Cartography"          │
│                                                 │
│  [Text]                    [Commentary]          │
│  ┌──────────────────┐     ┌──────────────────┐  │
│  │ The Ottoman       │     │ RECEPTION         │  │
│  │ mapping tradition │     │                   │  │
│  │ drew heavily on   │     │ This chapter is   │  │
│  │ earlier Islamic   │     │ cited by 28 of    │  │
│  │ cartographic      │     │ your other sources │  │
│  │ practice...       │     │ and is considered  │  │
│  │                   │     │ foundational.      │  │
│  │                   │     │                   │  │
│  │                   │     │ CONTESTED CLAIMS   │  │
│  │                   │     │                   │  │
│  │                   │     │ K's claim that     │  │
│  │                   │     │ Ottoman maps were  │  │
│  │                   │     │ "primarily naval"  │  │
│  │                   │     │ is challenged by   │  │
│  │                   │     │ Emiralioğlu (2014) │  │
│  │                   │     │ who argues for a   │  │
│  │                   │     │ broader land-based │  │
│  │                   │     │ tradition. [→p.45] │  │
│  └──────────────────┘     └──────────────────┘  │
└─────────────────────────────────────────────────┘
```

**Commentary sections:**

| Section | Content | Data source |
|---------|---------|-------------|
| **Reception** | How widely cited is this work in the corpus? Who engages with it? | Citation graph (Stage 5) |
| **Key Claims** | What are the main arguments? | LLM extraction from page_texts |
| **Contested Claims** | Where do other sources disagree? | Cross-corpus RAG search |
| **Superseded Arguments** | Has later work overturned any claims? | Temporal analysis + LLM |
| **Connections** | What other works in the corpus discuss similar topics? | Search similarity |
| **Further Reading** | Related works not in the corpus | Citation discovery (Stage 5) |

### 3.2 Topic-Level Commentary ("State of the Debate")

For a given research question or topic, synthesise what the corpus
says:

```
┌─────────────────────────────────────────────────┐
│  💡 State of the Debate:                        │
│  "Ottoman reception of Ptolemaic projection"    │
│                                                 │
│  Summary:                                       │
│  The debate centres on whether Ottoman           │
│  cartographers directly accessed Greek texts     │
│  or relied on Arabic intermediaries...          │
│                                                 │
│  Key positions:                                  │
│                                                 │
│  1. Direct Greek influence                      │
│     Argued by: Karamustafa (1992), ...          │
│     Evidence: [passage from p.34] [→Reader]     │
│                                                 │
│  2. Arabic intermediary model                   │
│     Argued by: Emiralioğlu (2014), ...          │
│     Evidence: [passage from p.112] [→Reader]    │
│                                                 │
│  3. Independent development                     │
│     Argued by: Pinto (2016), ...                │
│     Evidence: [passage from p.78] [→Reader]     │
│                                                 │
│  Chronology of the debate:                      │
│  1987 → 1992 → 2006 → 2014 → 2016 → 2020      │
│  [Timeline visualisation]                        │
│                                                 │
│  You may be missing:                            │
│  - Brotton (1997) "Trading Territories"         │
│    (cited by 4 of your sources) [Import]        │
└─────────────────────────────────────────────────┘
```

## 4. Generation Pipeline

### 4.1 Identify Key Documents

Not every document needs commentary.  Prioritise:
- Documents cited by many other sources in the corpus
- Documents the user has tagged as important
- Documents with the most extracted text (likely read more carefully)

```python
def identify_key_documents(inventory: list, citation_graph: dict,
                            top_n: int = 20) -> list:
    """Select documents that warrant commentary."""
    scores = {}
    for item in inventory:
        key = item["key"]
        if not item.get("extracted"):
            continue
        local_citations = citation_graph.get(key, {}).get("cited_by_count", 0)
        is_tagged_important = "important" in [t.lower() for t in item.get("tags", [])]
        page_count = item.get("page_count", 0) or 0

        scores[key] = (
            local_citations * 10 +
            (50 if is_tagged_important else 0) +
            min(page_count, 20)  # longer works are more likely to be substantial
        )

    return sorted(scores, key=scores.get, reverse=True)[:top_n]
```

### 4.2 Generate Commentary

For each key document, run an LLM pipeline:

```python
def generate_commentary(doc_key: str, corpus_context: list[dict]) -> dict:
    """Generate structured commentary for one document."""

    # 1. Extract key claims from the document
    doc_text = load_page_texts(doc_key)
    claims = llm_extract_claims(doc_text)

    # 2. For each claim, search the corpus for supporting/contradicting passages
    for claim in claims:
        related = search_corpus(claim["text"], exclude=doc_key)
        claim["supporting"] = [r for r in related if r["stance"] == "supporting"]
        claim["contradicting"] = [r for r in related if r["stance"] == "contradicting"]
        claim["superseding"] = [r for r in related
                                 if r["stance"] == "contradicting"
                                 and r["year"] > claim.get("year", 0)]

    # 3. Generate reception summary
    reception = llm_generate_reception(doc_key, corpus_context)

    # 4. Generate connections
    connections = find_similar_documents(doc_key, top_n=5)

    return {
        "key": doc_key,
        "generated_at": datetime.now().isoformat(),
        "reception": reception,
        "key_claims": claims,
        "connections": connections,
        "model": "gemini-2.5-flash",  # or whatever was used
    }
```

### 4.3 LLM Prompts

**Claim extraction prompt:**
```
You are a scholarly research assistant. Given the following academic
text, identify the 3-5 most important claims or arguments made by
the author. For each claim:
- State it in one sentence
- Note the page number(s) where it appears
- Classify it as: factual, interpretive, methodological, or theoretical

Text: {page_texts}
```

**Stance detection prompt:**
```
Given the following passage from {author} ({year}), and a claim from
{original_author} ({original_year}):

Claim: "{claim_text}"

Passage: "{passage_text}"

Does this passage:
A) Support the claim
B) Contradict or challenge the claim
C) Extend or modify the claim
D) Not directly relevant to the claim

Respond with the letter and a one-sentence explanation.
```

**Reception summary prompt:**
```
You are writing a brief reception history for an academic work.

The work: {title} by {author} ({year})

It is cited by the following works in this researcher's library:
{citing_works_list}

Passages where it is discussed:
{relevant_passages}

Write a 2-3 paragraph reception summary covering:
1. How widely cited is this work and by whom?
2. What aspects of it are most discussed?
3. Has it been challenged or superseded on any points?

Ground every claim in the passages provided. Do not add information
from outside the researcher's corpus.
```

### 4.4 Output Format

```json
// data/texts/{key}/commentary.json
{
  "key": "QIGTV3FC",
  "generated_at": "2026-02-20T15:30:00",
  "model": "gemini-2.5-flash",
  "reception": {
    "summary": "Karamustafa's 1992 chapter is cited by 28 of the 247...",
    "cited_by": ["5FBQAZ7C", "HMPTGZFQ", ...],
    "most_discussed_aspects": ["naval cartography thesis", "periodisation"]
  },
  "key_claims": [
    {
      "text": "Ottoman mapping was primarily a naval enterprise",
      "pages": [12, 13, 14],
      "type": "interpretive",
      "supporting": [
        {"key": "5FBQAZ7C", "page": 7, "snippet": "...confirms the naval..."}
      ],
      "contradicting": [
        {"key": "HMPTGZFQ", "page": 45, "snippet": "...however, land-based..."}
      ],
      "superseded": true,
      "superseded_by": [
        {"key": "HMPTGZFQ", "year": 2014, "explanation": "Emiralioğlu argues..."}
      ]
    }
  ],
  "connections": [
    {"key": "5FBQAZ7C", "similarity": 0.85, "shared_topics": ["Ottoman", "naval"]}
  ]
}
```

## 5. Cost Estimation

| Operation | LLM calls per doc | Cost per doc | 20 key docs |
|-----------|-------------------|-------------|-------------|
| Claim extraction | 1 | ~$0.02 | $0.40 |
| Stance detection | 5–15 per claim × 3-5 claims | ~$0.10 | $2.00 |
| Reception summary | 1 | ~$0.03 | $0.60 |
| **Total** | | ~$0.15/doc | **~$3.00** |

Using Gemini 2.5 Flash (cheapest capable model).  Claude or GPT-4
would cost ~3-5x more.

## 6. Implementation Tasks

| # | Task | Effort | Priority |
|---|------|--------|----------|
| 6.1 | Key document identification | 0.5 day | P0 |
| 6.2 | Claim extraction pipeline | 1.5 days | P0 |
| 6.3 | Cross-corpus stance detection | 2 days | P0 |
| 6.4 | Reception summary generation | 1 day | P0 |
| 6.5 | Commentary JSON format + writer | 0.5 day | P0 |
| 6.6 | Reader UI: commentary sidebar | 2 days | P0 |
| 6.7 | Topic-level "state of debate" generation | 2 days | P1 |
| 6.8 | Topic debate UI in explorer | 1.5 days | P1 |
| 6.9 | GitHub Actions workflow for commentary | 0.5 day | P0 |
| 6.10 | Cost estimation + dry-run mode | 0.5 day | P0 |
| **Total (P0)** | | **~8.5 days** | |
| **Total (all)** | | **~12 days** | |

## 7. Quality Safeguards

Commentary is LLM-generated, so we need safeguards:

1. **Grounding requirement:** Every claim in the commentary must cite
   a specific passage from the corpus.  Claims without citations are
   flagged or removed.

2. **Uncertainty markers:** The commentary should explicitly mark
   uncertain interpretations: "This passage *may* be challenging X's
   position, though it could also be read as a refinement."

3. **Regeneration:** Users can regenerate commentary for any document
   (e.g., after adding new sources that change the citation landscape).

4. **Human override:** Users can edit or annotate the generated
   commentary (stored as a separate `commentary_annotations.json`).

5. **Model attribution:** Every commentary panel shows which model
   generated it and when, so the user knows it's AI-assisted.

## 8. Open Questions

1. **Which LLM?** Gemini 2.5 Flash is cheapest and handles long
   contexts well.  Claude is better at nuanced academic reasoning.
   **Recommendation:** Default to Gemini Flash for cost, allow
   Claude/GPT-4 as an option for users who want higher quality.

2. **Commentary for non-English texts:** Should commentary be in
   English even if the source text is in Arabic?  **Yes** — the
   commentary is for the researcher, in their working language.
   But we should note the original language.

3. **Incremental updates:** When a new source is added to the corpus,
   should existing commentary be regenerated?  **Flag for regeneration**
   — don't auto-regenerate (costs money), but show "Commentary may be
   outdated — 3 new sources added since generation."

4. **Collaboration potential:** In a research group setting, one
   person's commentary could be shared with the group.  This ties
   into the institutional tier in the strategy doc.

## 9. Relationship to Other Stages

| Stage | How it feeds into Stage 6 |
|-------|--------------------------|
| Stage 3 (PDF processing) | Provides page_texts.json — the raw text for claim extraction |
| Stage 4 (Search) | Provides the corpus-wide search capability for stance detection |
| Stage 5 (Citation discovery) | Provides the citation graph for reception analysis; recommendations feed into "Further Reading" |

Stage 6 is the capstone — it synthesises everything built in Stages
3-5 into a genuinely new form of scholarly support.
