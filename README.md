# Research Corpus Pipeline

A pipeline for turning a bibliography of academic PDFs into a browsable, searchable, RAG-powered reading environment.

**Live demo:** [Islamic Cartography Corpus](https://leifuss.github.io/islamic-cartography-pipeline/)

The pipeline takes a Zotero library (or, in future, a BibTeX export or CSV) and produces:

- `explore.html` — filterable browse view of the corpus with map and timeline
- `reader.html` — per-document reading view with page images, extracted text, bibliography, and translation
- `chat.html` — BM25 RAG chat interface (requires a running API server)
- `status.html` — live extraction progress monitor

---

## Architecture

```
Zotero / BibTeX
    ↓  scripts/03_inventory.py
data/inventory.json  (canonical metadata, tracked in git)
    ↓  make stage
data/pdfs/{KEY}.pdf  (staged copies, Git LFS)
    ↓  make extract
data/texts/{KEY}/
    ├── page_texts.json       full text per page
    ├── layout_elements.json  semantic elements with bboxes
    ├── bibliography.json     references / abstract / contents
    └── translation.json      English translation (non-English docs)
    ↓  GitHub Actions → GitHub Pages
explore.html / reader.html / chat.html  (static, public)
    ↓  make rag (or Render.com)
RAG API server (BM25 + LLM)
```

### Extraction triage

The extractor (`scripts/05b_extract_robust.py`) picks the right tool per document:

| Situation | Tool | Speed |
|---|---|---|
| Embedded text, clean | pypdfium2 | ~7 s/doc |
| Embedded text, garbled fonts | Google Vision | ~2 min/doc |
| Scanned, Latin script | Tesseract | ~3 min/doc |
| Scanned, Arabic/Persian | Google Vision | ~2 min/doc |

All extraction runs fire-and-forget (background process) and is resume-safe.

---

## Quick start

### Prerequisites

- Python 3.11+
- Tesseract OCR (`brew install tesseract tesseract-lang` on macOS)
- Zotero desktop (for the default local-SQLite source)
- Optional: `GOOGLE_APPLICATION_CREDENTIALS` for Vision OCR fallback
- Optional: Any of `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` for the RAG chat server

### Install

```bash
git clone https://github.com/leifuss/islamic-cartography-pipeline
cd islamic-cartography-pipeline
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
make check          # verify environment
```

### Configure

Edit `data/corpus_config.json` (see [Adapting for your own corpus](#adapting-for-your-own-corpus) below).

Copy and edit the environment template:

```bash
cp .env.template .env
# Fill in: ZOTERO_LIBRARY_ID, COLLECTION_NAME, and any API keys
```

### Run

```bash
make inventory      # scan Zotero → data/inventory.json + dashboard.html
make stage          # copy PDFs to data/pdfs/{KEY}.pdf with provenance
make extract        # extract text (runs in background, resume-safe)
make status         # check progress
make rag            # start RAG chat server on localhost:8001
open data/explore.html
```

---

## Makefile reference

| Target | What it does |
|---|---|
| `make inventory` | Re-scan Zotero and regenerate `inventory.json` + `dashboard.html` |
| `make flags` | Sync `extracted`/`has_reader` flags from disk into `inventory.json` |
| `make stage` | Copy PDFs from Zotero/downloads → `data/pdfs/{KEY}.pdf` |
| `make stage-dry` | Preview staging without copying |
| `make lfs-setup` | One-time: configure Git LFS for `data/pdfs/` |
| `make extract` | Run extraction in background (pypdfium2 + Tesseract) |
| `make extract-dry` | Preview extraction plan, no writes |
| `make extract-vision` | Extraction with Google Vision fallback |
| `make status` | Show log tail + per-doc status summary |
| `make status-web` | Open `status.html` in browser |
| `make rag` | Start RAG server on localhost:8001 |
| `make check` | Verify environment (pypdfium2, tesseract, vision, rank-bm25) |

---

## Data format

`data/inventory.json` — one object per document:

```json
{
  "key": "QIGTV3FC",          // Zotero item key
  "title": "...",
  "author": "...",
  "year": 1990,
  "language": "en",
  "pdf_path": "/abs/path/to/original.pdf",
  "pdf_staged_path": "data/pdfs/QIGTV3FC.pdf",
  "pdf_original_name": "Sinisgalli - 2012 - Ptolemy.pdf",
  "pdf_zotero_key": "6LLPK8WV",
  "extracted": true,
  "has_reader": true,
  "text_quality": "good",     // good | suspect | garbled | scanned
  "page_count": 211
}
```

`data/texts/{KEY}/` — one directory per extracted document:

| File | Contents | Produced by |
|---|---|---|
| `page_texts.json` | `{"1": "full page text…", …}` | 05b_extract_robust.py |
| `layout_elements.json` | `{"1": [{label, text, bbox}, …], …}` | 05b_extract_robust.py |
| `meta.json` | extraction method, quality, timing | 05b_extract_robust.py |
| `bibliography.json` | references, abstract, contents list | 06_extract_bibliography.py |
| `translation.json` | English translation (non-English docs) | 08_translate.py |
| `pages/001.jpg` … | Page images at 150 DPI | 05b_extract_robust.py |

Element labels: `section_header`, `text`, `list_item`, `table`, `picture`, `page_header`, `page_footer`, `footnote`, `formula`

---

## Adapting for your own corpus

The pipeline is designed to be corpus-agnostic. Three things need changing:

### 1. `data/corpus_config.json`

```json
{
  "name": "Your Corpus Name",
  "description": "A brief description of the collection",
  "rag_api": "https://your-rag-server.onrender.com/api",
  "texts_dir": "texts/",
  "nav": { ... }
}
```

The HTML files (`explore.html`, `reader.html`, `chat.html`, `status.html`) read all titles, descriptions, and API URLs from this file. No HTML edits needed.

### 2. `data/inventory.json`

Populate with your own documents. The required fields for the UI are:

| Field | Type | Used by |
|---|---|---|
| `key` | string | all (unique doc ID) |
| `title` | string | explore, reader |
| `author` | string | explore, reader |
| `year` | int | explore (timeline, filter) |
| `language` | string | explore (filter) |
| `extracted` | bool | explore (link to reader) |
| `has_reader` | bool | reader (enables page view) |

Additional fields (`tags`, `abstract`, `place`, `doi`, `url`) are displayed if present.

**From Zotero:** run `scripts/03_inventory.py` — reads from Zotero's local SQLite database.

**From BibTeX:** `scripts/00_import_bibtex.py` (planned) — converts `.bib` file to `inventory.json`.

**From CSV:** Any spreadsheet with the above column names exported as CSV can be converted with a small script.

### 3. `.env`

```bash
ZOTERO_LIBRARY_ID=12345       # from zotero.org/settings/keys
ZOTERO_COLLECTION=My Collection
ANTHROPIC_API_KEY=sk-ant-...  # for bibliography extraction + translation
GEMINI_API_KEY=...            # optional, for RAG chat (preferred over OpenAI/Anthropic)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json  # optional, for Vision OCR
```

### Fork checklist

1. Fork this repo on GitHub
2. Edit `data/corpus_config.json` — name, description, RAG API URL
3. Replace `data/inventory.json` with your own bibliography data
4. Set repository secrets: `ANTHROPIC_API_KEY`, etc. (Settings → Secrets → Actions)
5. Enable GitHub Pages (Settings → Pages → Source: GitHub Actions)
6. Push — the deploy workflow publishes `data/` as a static site automatically

---

## Hosting

### Static UI (explore, reader, chat, status)

GitHub Pages via the included workflow (`.github/workflows/deploy-pages.yml`). Push to `main` → auto-deploys `data/` as a static site.

### RAG chat server

The `rag_server.py` FastAPI app requires a persistent Python process. Options:

- **Local**: `make rag` (localhost:8001, update `corpus_config.json → rag_api`)
- **Render.com** free tier: push repo, create Web Service, set start command to `venv/bin/python scripts/rag_server.py --port $PORT`
- **GitHub Actions** (batch): run nightly, write results to static JSON, no server needed

---

## Project structure

```
scripts/           Pipeline scripts
  00_stage_pdfs.py         Copy PDFs to data/pdfs/ with provenance
  03_inventory.py          Scan Zotero → inventory.json
  05b_extract_robust.py    Main extraction (pypdfium2 / Tesseract / Vision)
  06_extract_bibliography.py  Claude-powered bibliography extraction
  08_translate.py          Claude-powered translation
  rag_server.py            BM25 RAG chat API (FastAPI)
src/               Core library (Zotero client, state management)
data/
  corpus_config.json    Corpus identity + nav (edit this to adapt)
  inventory.json        Canonical metadata for all documents
  texts/                Per-document extracted output
  pdfs/                 Staged PDF copies (Git LFS, not in repo)
  explore.html          Collection browse view
  reader.html           Per-document reading view
  chat.html             RAG chat interface
  status.html           Extraction progress monitor
  dashboard.html        Full inventory dashboard
Makefile              Task runner (make help for full list)
.github/workflows/
  deploy-pages.yml      Auto-deploy data/ to GitHub Pages on push
```

---

## Roadmap

- [x] `scripts/05c_layout_heron.py` — semantic layout enrichment via Heron (RT-DETRv2); cloud-friendly (works from page images, no PDF needed)
- [ ] `scripts/00_import_bibtex.py` — BibTeX → inventory.json bootstrapper
- [ ] `scripts/03_sync_zotero_api.py` — sync metadata from Zotero web API (no desktop app required)
- [ ] GitHub Actions `layout.yml` workflow — run 05c in the cloud after new pages are committed
- [ ] Render.com deployment guide for RAG server
- [ ] Image compression pass (target 60 KB/page, ~300 MB for full corpus)
- [ ] DPI upscaling test — evaluate bicubic / ESRGAN upscaling on 144 DPI scans before Heron + Tesseract
