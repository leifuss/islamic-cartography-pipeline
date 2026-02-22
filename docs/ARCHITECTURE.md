# Scholion — Developer's Manual

## Overview

Scholion is a multi-collection scholarly PDF pipeline that ingests bibliographic records from Zotero, fetches or receives PDFs, extracts text using multiple competing methods, assesses quality, and presents the results through a browser-based dashboard and reader interface.

The name derives from *scholion* (pl. *scholia*) — marginal annotations in classical manuscripts. The system applies a similar layered-commentary approach to modern PDF extraction: multiple "witnesses" (extraction methods) produce independent readings, which are compared and adjudicated.

---

## Architecture at a Glance

```
┌──────────────────────────────────────────────────────────────────┐
│                          Zotero Web API                          │
│                      (bibliographic source)                       │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                   ┌─────────▼──────────┐
                   │   03_inventory.py   │  Sync metadata → inventory.json
                   └─────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
     ┌────────────┐  ┌────────────┐  ┌──────────────┐
     │ import_scan │  │ import_pdfs│  │ pdf_server.py│
     │  (URL scan) │  │ (download) │  │  (local UI)  │
     └──────┬─────┘  └──────┬─────┘  └──────┬───────┘
            └───────────┬────┘               │
                        ▼                    ▼
              data/collections/{slug}/pdfs/{key}.pdf
                        │
         ┌──────────────┼──────────────────┐
         ▼              ▼                  ▼
  ┌─────────────┐ ┌───────────┐  ┌──────────────────┐
  │   Docling    │ │ Tesseract │  │  Google Vision    │
  │ (ML layout)  │ │   (OCR)   │  │ (cloud OCR gate) │
  └──────┬──────┘ └─────┬─────┘  └────────┬─────────┘
         └──────────┬────┘                 │
                    ▼                      │
         quality assessment  ◄─────────────┘
         (similarity + corruption)
                    │
                    ▼
  data/collections/{slug}/texts/{key}/
      ├── meta.json
      ├── page_texts.json
      ├── layout_elements.json
      └── pages/001.jpg, 002.jpg, ...
                    │
         ┌──────────┼──────────────┐
         ▼          ▼              ▼
   dashboard.html  reader.html   chat.html
   (inventory)     (per-doc)     (RAG)
```

---

## Directory Structure

```
scholion/
├── src/                          # Shared Python library
│   ├── extractors/
│   │   ├── docling_extractor.py  # Primary: ML-based layout + text
│   │   ├── tesseract_extractor.py# Witness B: OCR fallback
│   │   └── vision_extractor.py   # Quality gate: cloud Vision API
│   ├── quality/
│   │   ├── similarity.py         # Levenshtein + Arabic normalisation
│   │   └── corruption_detector.py# Gibberish/artifact detection
│   ├── language_detector.py      # Script analysis + langdetect
│   ├── zotero_client.py          # Zotero web API wrapper (pyzotero)
│   ├── pdf_finder.py             # URL resolution, paywall detection
│   └── state.py                  # Pipeline checkpointing (legacy)
│
├── scripts/                      # Numbered pipeline scripts + utilities
│   ├── 00_stage_pdfs.py          # Download PDFs from Zotero cloud storage
│   ├── 00_update_inventory_flags.py  # Sync disk state → inventory.json
│   ├── 00_backfill_dpi.py        # Backfill pdf_dpi field for existing PDFs
│   ├── 01_setup.py               # Environment verification
│   ├── 02_test_extraction.py     # Multi-witness test harness (heavy)
│   ├── 03_inventory.py           # Zotero sync → inventory.json
│   ├── 04_download_pdfs.py       # Batch PDF downloader (helper functions)
│   ├── 05_extract_embedded.py    # Docling batch extraction (persistent workers)
│   ├── 05b_extract_robust.py     # Production extraction (pypdfium2 + Tesseract)
│   ├── 05c_layout_heron.py       # Layout enrichment via Heron VLM
│   ├── 06_extract_bibliography.py# Bibliography section extraction
│   ├── 07_extract_spans.py       # Text span annotation
│   ├── 08_translate.py           # Machine translation (non-English docs)
│   ├── 09_ocr_test.py            # OCR comparison experiments
│   ├── 10_ocr_vision.py          # Vision API OCR runner
│   ├── 10a_vision_upsample_test.py  # DPI upsampling experiments
│   ├── 11_patch_page_sizes.py    # Page-size backfill
│   ├── 12_merge_labels.py        # Layout label merging
│   ├── build_collection.py       # One-command collection builder
│   ├── discover_collections.py   # Enumerate Zotero libraries
│   ├── select_collections.py     # Interactive collection picker
│   ├── generate_explore.py       # Generate explore.html per collection
│   ├── generate_reader.py        # Generate reader data
│   ├── import_scan.py            # Scan URLs for PDF availability
│   ├── import_pdfs.py            # Download PDFs from scanned URLs
│   ├── pdf_server.py             # Local HTTP server for upload/URL fetch
│   ├── compare_extractions.py    # Side-by-side extraction comparison
│   ├── rag_server.py             # FastAPI RAG chat server
│   ├── modal_heron.py            # Modal.com Heron deployment
│   ├── modal_rag.py              # Modal.com RAG deployment
│   └── zotero_sync.py            # Lightweight Zotero sync
│
├── data/                         # All data lives here (gitignored selectively)
│   ├── collections.json          # Registry of all collections
│   ├── collections/
│   │   └── {slug}/
│   │       ├── inventory.json    # Per-collection item metadata
│   │       ├── pdfs/             # PDF files, keyed by Zotero key
│   │       ├── texts/            # Extracted text + metadata per doc
│   │       ├── extract_results.json
│   │       ├── pipeline_status.json
│   │       └── explore.html
│   ├── dashboard.html            # Main corpus dashboard
│   ├── reader.html               # Per-document reader with layout overlay
│   ├── chat.html                 # RAG chat interface
│   ├── index.html                # Collection picker / landing page
│   ├── import_dashboard.html     # PDF import progress tracker
│   └── status.html               # Pipeline status monitor
│
├── .github/workflows/            # GitHub Actions automation
│   ├── setup.yml                 # Environment + dependency setup
│   ├── zotero-sync.yml           # Scheduled Zotero sync
│   ├── discover-collections.yml  # Collection discovery
│   ├── select-collection.yml     # Collection selection
│   ├── import-scan.yml           # PDF availability scan
│   ├── import-pdfs.yml           # PDF download
│   ├── extract.yml               # Text extraction pipeline
│   └── deploy-pages.yml          # GitHub Pages deployment
│
├── config.yaml                   # Quality thresholds + extraction config
├── Makefile                      # Local task runner (make extract, etc.)
├── requirements.txt              # Python dependencies
└── requirements-rag.txt          # Additional RAG server dependencies
```

---

## Core Components

### 1. Zotero Client (`src/zotero_client.py`)

Wraps pyzotero to access the Zotero web API. All bibliographic metadata comes from Zotero — Scholion never stores its own copy of titles/authors/dates; the inventory mirrors what Zotero has.

**Key methods:**
- `get_all_items()` — top-level items only (no children)
- `get_all_items_with_children()` — items + notes/attachments in one API call (avoids N+1)
- `get_attachment_info()` — find the primary PDF attachment for an item
- `download_attachment()` — fetch file bytes from Zotero cloud

**Required env vars:** `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, optionally `ZOTERO_LIBRARY_TYPE` (default: `group`).

### 2. Extraction Pipeline

Three extractors compete to produce the best text:

#### Docling (`src/extractors/docling_extractor.py`)
The primary extractor. Uses IBM's Docling library for ML-based document understanding. Produces Markdown output with layout elements (section headers, footnotes, tables, figures) and bounding boxes.

- `do_ocr=True` (default): OCR enabled, auto-skips embedded-font pages
- `do_ocr=False`: Faster for known embedded-font PDFs
- Returns: `page_texts` (1-based), `layout_elements` with bbox coordinates, `_page_sizes`
- Handles ligature artifacts (`fi  rst` → `first`)

**Note:** Docling loads ~500MB of ML models per worker. The pipeline uses persistent workers to amortise this cost across documents.

#### Tesseract (`src/extractors/tesseract_extractor.py`)
Fallback OCR for scanned documents or pages where Docling fails. Renders pages at 300 DPI and runs `pytesseract.image_to_string`.

- Language support: `eng`, `ara`, `fas`, `tur`, `deu`, `fra` (auto-detected)
- Returns confidence scores from `image_to_data`

#### Google Vision (`src/extractors/vision_extractor.py`)
Cloud-based OCR used as a **quality gate**, not a primary extractor. Samples a spread of pages (first, middle, last) to compare against Docling output.

- `_spread_indices(total, n)` selects evenly-spaced pages
- Returns per-page text (0-based indices)
- Used in `02_test_extraction.py` for the Vision gate score

#### Decision Flow (script 02)
```
1. Docling + Vision run in parallel
2. Vision gate score = page-aligned similarity(Docling, Vision)
3. If gate ≥ 0.5: accept Docling, skip Tesseract
4. If gate < 0.5: run Tesseract, compare all three
```

#### Production Path (script 05b)
Script `05b_extract_robust.py` is the "fire and forget" production extractor. No Docling ML — uses pypdfium2 for embedded text, Tesseract for scanned pages, optional Vision fallback.

```
For each page:
  1. Try pypdfium2 embedded text extraction
  2. If < 50 chars: render page → Tesseract OCR
  3. If Tesseract suspect AND --vision: try Vision API
```

### 3. Quality Assessment (`src/quality/`)

#### Similarity (`similarity.py`)
- `strip_markdown()` — removes Markdown formatting before comparison
- `normalize_arabic_text()` — strips diacritics (tashkeel), normalises Unicode
- `compute_similarity()` — Levenshtein ratio on normalised text
- `pairwise_similarities()` — all-pairs comparison between witnesses

#### Corruption Detection (`corruption_detector.py`)
Checks for OCR artifacts:
- Arabic character ratio (expects >50% for Arabic text)
- Excessive symbols/punctuation
- Repeated character sequences (noise)
- Word fragmentation (avg word length < 2 chars)

#### Quality Metrics (`__init__.py`)
Combines agreement (70%) and cleanliness (30%) into an overall score:
- `≥ 0.85` → `auto_accept`
- `≥ 0.65` → `flag`
- `≥ 0.40` → `arbitrate`
- `< 0.40` → `review`

### 4. Language Detection (`src/language_detector.py`)

Two-pass detection:
1. **Unicode script analysis** — reliably identifies Arabic, Persian (via distinctive letters پچژگ), and Greek
2. **langdetect sampling** — distinguishes Latin-script languages (English, French, Latin) using sampled chunks

Returns Tesseract-compatible language strings (e.g., `eng+fra+ara`).

### 5. PDF Finder (`src/pdf_finder.py`)

Shared URL resolution logic:
- Paywall domain blacklist (jstor.org, brill.com, etc.)
- HTML reference domain blacklist (wikipedia, iranicaonline)
- Archive.org metadata API resolution
- PDF magic-byte verification via Range GET

---

## Data Model

### `inventory.json` — Per-Collection Item List

Each item in the inventory array has:

```json
{
  "key":          "23K87F66",       // Zotero item key (unique ID)
  "title":        "...",
  "year":         "2012",
  "authors":      "Cvijanovic",
  "item_type":    "journalArticle",
  "url":          "https://...",
  "pdf_status":   "stored",         // stored|downloaded|url_only|no_attachment
  "pdf_path":     "data/collections/islamic-cartography/pdfs/23K87F66.pdf",
  "doc_type":     "embedded",       // embedded|scanned|unknown
  "page_count":   5,
  "pdf_dpi":      300,              // estimated DPI of embedded images
  "language":     "en",
  "extracted":    true,
  "text_quality": "good",           // good|suspect|garbled
  "quality_score": 25.72,
  "tags":         ["cartography"],
  "notes":        ["..."]
}
```

### `texts/{key}/` — Extraction Outputs

| File | Description |
|------|-------------|
| `meta.json` | Title, authors, year, page_count, quality metrics, extraction method |
| `page_texts.json` | `{page_str: text}` — 1-based page numbers as string keys |
| `layout_elements.json` | `{page_str: [{label, text, bbox}, ...]}` + `_page_sizes` |
| `pages/001.jpg` | Rendered page images (JPEG, ~105 DPI for reader display) |
| `translation.json` | Machine translation (if non-English) |
| `bibliography.json` | Extracted bibliography entries |
| `text_spans.json` | Text span annotations |

### `collections.json` — Collection Registry

```json
{
  "default": "islamic-cartography",
  "collections": [
    {
      "slug":         "islamic-cartography",
      "name":         "Islamic Cartography",
      "path":         "collections/islamic-cartography",
      "library_type": "group",
      "library_id":   "5166884",
      "num_items":    20,
      "status":       "built"
    }
  ]
}
```

---

## Frontend

All UI is static HTML + vanilla JavaScript. No build step, no framework dependencies. Pages fetch JSON at runtime and render client-side.

### `dashboard.html`
The main corpus management interface. Shows:
- Donut charts (PDF coverage, doc type, access status)
- Sortable/filterable table of all items
- Side panel with full item details
- Workflow trigger buttons (GitHub Actions)
- PDF upload/URL fetch (via local `pdf_server.py`)

### `reader.html`
Per-document reader with:
- Page image display
- Text overlay (from layout_elements.json bounding boxes)
- Page navigation
- Text search within document

### `explore.html`
Collection-specific explorer generated by `generate_explore.py`.

### `chat.html`
RAG (Retrieval-Augmented Generation) interface connected to `rag_server.py`.

### `import_dashboard.html`
Real-time import progress tracker polling `import_status.json`.

---

## GitHub Actions Workflows

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `setup.yml` | Manual | Install environment, verify tools |
| `zotero-sync.yml` | Manual/scheduled | Sync from Zotero → inventory.json |
| `discover-collections.yml` | Manual | Enumerate Zotero libraries |
| `select-collection.yml` | Manual | Interactive collection selection |
| `import-scan.yml` | Manual | Scan URLs for PDF availability |
| `import-pdfs.yml` | Manual | Download available PDFs |
| `extract.yml` | Manual | Run extraction pipeline |
| `deploy-pages.yml` | Push to main | Deploy data/ to GitHub Pages |

The dashboard can trigger these workflows via the GitHub API (requires a PAT stored in localStorage).

---

## Key Design Decisions

### Multi-Witness Approach
Rather than trusting a single OCR method, Scholion runs multiple extractors and compares outputs. This is essential for the corpus because:
- Historical texts have degraded scans, archaic fonts
- Bilingual documents (Arabic/Latin) need different OCR strategies
- No single method handles all edge cases

### Collection-Aware Paths
Everything is scoped to a collection slug. Paths follow the pattern:
```
data/collections/{slug}/inventory.json
data/collections/{slug}/pdfs/{key}.pdf
data/collections/{slug}/texts/{key}/page_texts.json
```

### Resume-Safe Extraction
Both `05_extract_embedded.py` and `05b_extract_robust.py` skip documents that already have `page_texts.json` unless `--force` is passed. Progress is written to `pipeline_status.json` after each document.

### Static-First UI
All frontends are static HTML. This enables:
- GitHub Pages hosting (free, fast, global CDN)
- Zero server requirements for browsing
- Local development with just `open data/dashboard.html`

The only server-requiring features are:
- PDF upload (`pdf_server.py` on localhost)
- RAG chat (`rag_server.py` on localhost)
- GitHub Actions triggers (require PAT)

---

## Common Tasks

### Add a new collection
```bash
make discover                          # find Zotero libraries
make select                            # pick collections
make build-collection SLUG=my-coll     # sync + classify + generate explorer
```

### Run extraction locally
```bash
make extract                           # background, pypdfium2 + Tesseract
make status                            # monitor progress
# or for a specific collection:
python scripts/05b_extract_robust.py --collection-slug islamic-cartography
```

### Compare extraction methods
```bash
python scripts/compare_extractions.py  # default: 4 test subjects
python scripts/compare_extractions.py --keys ABC123 DEF456
```

### Upload PDFs via browser
```bash
python scripts/pdf_server.py           # start server on port 8787
# then use the "Upload / Fetch PDF" button on dashboard.html
```

### Backfill DPI metadata
```bash
python scripts/00_backfill_dpi.py --collection-slug islamic-cartography
```

---

## Environment

**Required:**
- Python 3.10+
- pypdfium2 (PDF text extraction + page rendering)
- pytesseract + Tesseract binary (OCR)
- pyzotero (Zotero API)
- python-Levenshtein (text comparison)
- langdetect (language detection)

**Optional:**
- docling (ML-based extraction — heavy, ~500MB models)
- google-cloud-vision (cloud OCR quality gate — costs ~$1.50/1000 pages)
- requests (PDF download)
- fastapi + uvicorn (RAG server)

**Env vars** (in `.env`):
- `ZOTERO_API_KEY` — Zotero web API key
- `ZOTERO_LIBRARY_ID` — library ID
- `ZOTERO_LIBRARY_TYPE` — `group` or `user`
- `GOOGLE_APPLICATION_CREDENTIALS` — path to Vision API service account JSON (optional)
- `NTFY_TOPIC` — ntfy.sh topic for push notifications (optional)
