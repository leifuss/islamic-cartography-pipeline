# Stage 4: Search — "I can ask questions across my whole corpus"

> **Goal:** The researcher can search across all extracted text in their
> corpus.  The free tier uses client-side search (no server needed).
> An optional hosted RAG tier provides conversational, citation-grounded
> answers.

## 1. User Stories

- *As a researcher, I want to search for a phrase across all my sources
  and see which documents mention it, with context.*
- *As a researcher, I want to ask a question in natural language and get
  an answer grounded in my own bibliography.*
- *As a researcher, I want search results to link directly to the
  relevant page in the reader.*
- *As a researcher, I want search to work without me paying for any
  service or setting up any server.*

## 2. Architecture: Two Tiers

### Tier 1: Client-Side Search (free, zero-config)

A pre-built search index served as a static JSON file on GitHub Pages.
The browser loads the index and runs searches locally.  No server, no
API key, no cost.

```
Build time (GitHub Actions):
  page_texts.json (all docs) ──→ build_search_index.py ──→ search_index.json

Runtime (browser):
  search.html loads search_index.json
  User types query → lunr.js / MiniSearch runs BM25 in-browser
  Results shown with snippets + links to reader.html#page
```

**Capabilities:**
- Keyword search (exact and fuzzy)
- Boolean operators (AND, OR, NOT)
- Field-scoped search (title:, author:, year:)
- Highlighted snippets showing match context
- Results ranked by BM25 relevance
- Link to exact page in reader

**Limitations:**
- No natural language Q&A
- No semantic similarity (only keyword matching)
- Index size grows with corpus (but compresses well — a 500-doc corpus
  is typically ~5–15 MB of index, which loads in <2 seconds)
- No cross-document synthesis

**Library choice:** [MiniSearch](https://lucaong.github.io/minisearch/)
is preferred over Lunr.js because:
- Smaller bundle size (~7 KB vs ~20 KB)
- Better performance for larger indices
- Active maintenance
- Supports field boosting and fuzzy matching out of the box

### Tier 2: Hosted RAG (optional, for power users)

A conversational search endpoint that:
- Uses BM25 + optional embedding similarity to retrieve passages
- Sends top-k passages to an LLM with the user's question
- Returns a grounded answer with inline citations
- Supports follow-up questions (conversation memory)

**Current implementation:** `scripts/rag_server.py` — a FastAPI server
with BM25 retrieval and LLM synthesis.  Currently requires self-hosting
on Railway/Render.

**Options for making this accessible:**

| Option | Setup complexity | Cost | Scalability |
|--------|-----------------|------|-------------|
| **A: User self-hosts on Render** | High (too hard for target users) | Free tier available | Good |
| **B: Serverless function (Vercel/Cloudflare)** | Medium (deploy from repo) | Free tier covers it | Good |
| **C: Multi-tenant hosted service** | Zero (we host it) | $5/month subscription | Excellent |
| **D: GitHub Actions on-demand** | Low (workflow dispatch) | Free (within limits) | Limited |

**Recommendation for MVP:** Start with **Option D** — a GitHub Actions
workflow that answers a single question.  This is unusual but workable:

```yaml
# .github/workflows/ask.yml
name: Ask a question
on:
  workflow_dispatch:
    inputs:
      question:
        description: "Your research question"
        required: true
```

The workflow:
1. Loads the search index and extracted texts
2. Runs BM25 retrieval
3. Sends top passages + question to an LLM (user's API key)
4. Writes the answer to `data/answers/{timestamp}.json`
5. The explorer shows recent answers

**Pros:** Zero infrastructure. Works within GitHub's free tier.
**Cons:** Slow (30–60 seconds). Not conversational. Limited to 2000
Actions minutes/month.

**For v1.5:** Add Option B (serverless) or Option C (hosted) for users
who want real-time conversational search.

## 3. Client-Side Search Implementation

### 3.1 Index Builder

```python
# scripts/build_search_index.py

"""
Build a client-side search index from extracted texts.

Reads data/texts/{key}/page_texts.json for all extracted documents
and produces data/search_index.json — a pre-built MiniSearch-compatible
index that the browser loads for instant search.

Usage:
    python scripts/build_search_index.py
    python scripts/build_search_index.py --max-tokens 500  # per page
"""
```

The index contains, for each document page:
- Document key + page number
- Title, authors, year (from inventory)
- Page text (truncated to ~500 tokens to control index size)
- Text snippet (first 200 chars, for result display)

**Index format:**
```json
{
  "version": 1,
  "built_at": "2026-02-20T14:30:00",
  "doc_count": 247,
  "page_count": 4832,
  "documents": [
    {
      "id": "5FBQAZ7C/3",
      "key": "5FBQAZ7C",
      "page": 3,
      "title": "Tradition and Innovation...",
      "authors": "Pambakian",
      "year": "2018",
      "text": "The cosmological ideas of Anania...",
      "snippet": "The cosmological ideas of Anania Širakac'i..."
    }
  ]
}
```

### 3.2 Search UI

A new `data/search.html` page (or a search panel in the explorer):

```
┌─────────────────────────────────────────────────┐
│  🔍 Search your library                        │
│  ┌─────────────────────────────────────────┐   │
│  │ ottoman cartographic tradition          │   │
│  └─────────────────────────────────────────┘   │
│                                                 │
│  247 documents indexed · 4,832 pages            │
│                                                 │
│  12 results for "ottoman cartographic           │
│  tradition":                                    │
│                                                 │
│  1. [Pambakian 2018] p.7  (score: 4.2)        │
│     "...the Ottoman cartographic tradition      │
│     drew heavily on earlier Islamic models..."  │
│     [Open in Reader →]                          │
│                                                 │
│  2. [Karamustafa 1992] p.12  (score: 3.8)     │
│     "...mapping practices within the Ottoman    │
│     cartographic tradition can be traced..."    │
│     [Open in Reader →]                          │
│                                                 │
│  Filter: [All years ▾] [All languages ▾]       │
└─────────────────────────────────────────────────┘
```

**Features:**
- Instant search as you type (debounced)
- Highlighted match terms in snippets
- Click to open in reader at the exact page
- Filter by year range, language, document type
- Result count and index stats

### 3.3 Build Workflow

```yaml
# .github/workflows/build-index.yml
name: Build search index
on:
  push:
    paths:
      - 'data/texts/*/page_texts.json'
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: python scripts/build_search_index.py
      - run: |
          git add data/search_index.json
          git diff --staged --quiet || \
            git commit -m "search: rebuild index [skip ci]" && \
            git push
```

**Trigger note:** The `extract.yml` workflow commits with `[skip ci]`
to avoid recursive CI.  This means the `push` trigger above will NOT
fire after extraction.  Instead, `extract.yml` should explicitly
trigger `build-index.yml` via `gh workflow run build-index.yml` after
committing extraction results (same pattern used for
`zotero-sync.yml` → `extract.yml`).

### 3.4 Index Size Management

For corpora under 500 documents:
- Full page text in index: ~5-15 MB (compresses to ~2-4 MB gzipped)
- GitHub Pages serves gzipped: loads in 1-3 seconds on broadband

For larger corpora:
- Truncate page text to first 500 tokens per page
- Or split into per-document indices, load on demand
- Or switch to a server-side search (Tier 2)

**Threshold:** If `search_index.json` exceeds 20 MB uncompressed,
warn the user and suggest Tier 2.

## 4. RAG Search Implementation (Tier 2)

### 4.1 GitHub Actions RAG (MVP)

A `ask.yml` workflow that processes one question at a time:

```
User: types question in explorer UI
  ↓
Explorer UI: commits question to data/questions/{timestamp}.json
  via GitHub API
  ↓
GitHub Actions: ask.yml triggers on push to data/questions/
  1. Load search index
  2. BM25 retrieve top-10 passages
  3. Send to LLM: "Based on these passages from the user's library,
     answer this question: {question}"
  4. Write answer to data/answers/{timestamp}.json
  5. Commit + push
  ↓
Explorer UI: polls for new answer file, displays it
```

**Latency:** 30-60 seconds (acceptable for research questions).

### 4.2 Serverless RAG (v1.5)

A Cloudflare Worker or Vercel Edge Function:

```
Browser → POST /api/ask {question, corpus_id}
  ↓
Worker:
  1. Load pre-built index from GitHub Pages (cached)
  2. BM25 retrieve top-10 passages
  3. Send to LLM API (user's key or shared key)
  4. Return streamed answer
  ↓
Browser: display answer with citations
```

**Deployment:** Automatic from the repo via `wrangler deploy` in a
GitHub Actions workflow.  User just needs to add a Cloudflare API
token as a repo secret.

### 4.3 Multi-Tenant Hosted RAG (v2)

A single deployed service that serves multiple users' corpora:

```
https://rag.marginalia.dev/api/ask
  POST {corpus_id: "username/repo", question: "...", api_key: "..."}
```

Each user's corpus is a namespace.  The index is fetched from their
GitHub Pages URL and cached.

**Business model:** Free for <100 queries/month, $5/month for unlimited.

## 5. Implementation Tasks

| # | Task | Effort | Priority |
|---|------|--------|----------|
| 4.1 | Search index builder script | 1 day | P0 |
| 4.2 | Search UI (search.html) | 1.5 days | P0 |
| 4.3 | Build-index workflow | 0.5 day | P0 |
| 4.4 | Index size management / warnings | 0.5 day | P1 |
| 4.5 | GitHub Actions RAG (ask.yml) | 1.5 days | P1 |
| 4.6 | Explorer integration (ask UI + answer display) | 1 day | P1 |
| 4.7 | Serverless RAG (Cloudflare Worker) | 2 days | P2 |
| 4.8 | Multi-tenant hosted RAG | 3 days | P3 |
| **Total (P0)** | | **~3 days** | |
| **Total (P0+P1)** | | **~6 days** | |

## 6. Testing Plan

| Test case | Method |
|-----------|--------|
| Index builds from 1 document | Minimal corpus |
| Index builds from 500 documents | Large corpus |
| Search finds exact phrase | Query known text |
| Search handles Arabic text | Query Arabic terms |
| Search UI renders on mobile | iPad/iPhone test |
| MiniSearch handles 15 MB index | Performance test |
| GitHub Actions RAG returns answer | End-to-end test |
| RAG cites correct passages | Verify citations against source |

## 7. Open Questions

1. **MiniSearch vs Lunr.js vs Fuse.js:** MiniSearch is recommended
   but we should benchmark all three with a real corpus for index
   size and query speed.

2. **Search across collections:** If a user has multiple collections,
   should search span all of them?  **Yes** — the index should
   include all extracted documents regardless of collection.

3. **Embedding-based search:** For Tier 2, should we add semantic
   similarity (via embeddings) alongside BM25?  **Later** — BM25 is
   sufficient for keyword-oriented humanities research.  Embeddings
   add latency and cost.

4. **Search history:** Should we store recent searches?  **Yes** —
   in localStorage, client-side only.
