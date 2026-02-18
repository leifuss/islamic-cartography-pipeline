# Islamic Cartography Research Pipeline

A text extraction and reading pipeline for 313 academic sources on Islamic cartography, stored in Zotero.

**Current status:** Phase 2 complete (structured extraction + reader). Phase 3 (OCR) in design.
See [`PIPELINE_STATUS.md`](PIPELINE_STATUS.md) for the per-document inventory and [`LESSONS_LEARNED.md`](LESSONS_LEARNED.md) for a full retrospective.

---

## What this does

Takes a Zotero library of academic PDFs → extracts structured text and layout → produces a browsable research reader with bibliography, translation, and annotation support.

```
Zotero (313 items)
    ↓
Docling (PDF → structured JSON)
    ↓
Claude API (bibliography extraction + translation)
    ↓
data/reader.html  ←  the research interface
```

## Setup

### Prerequisites

- Python 3.11+
- Zotero desktop app running locally (pipeline reads its SQLite database directly)
- `ANTHROPIC_API_KEY` — required for bibliography extraction and translation

### Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.template data/.env
# Edit data/.env:
#   ANTHROPIC_API_KEY=your-key-here   ← required for scripts 06 and 08
#   ZOTERO_LIBRARY_ID=your-library-id
#   COLLECTION_NAME=Islamic Cartography
```

### Verify

```bash
python scripts/01_setup.py
```

---

## Running the pipeline

Scripts are numbered and run in order. Each writes output to `data/texts/{KEY}/`.

```bash
# 1. Check environment and Zotero connection
python scripts/01_setup.py

# 2. Inventory the collection
python scripts/03_inventory.py

# 3. Extract text and layout from PDFs (via Docling)
python scripts/05_extract_embedded.py

# 4. Extract bibliography, citations, and summary
python scripts/06_extract_bibliography.py
# Options:
#   --keys QIGTV3FC DUZKRZFQ    (process specific docs)
#   --force                      (overwrite existing)

# 5. Translate non-English documents
python scripts/08_translate.py

# 6. Open the reader
open data/reader.html
```

---

## Data format

Each document gets a directory: `data/texts/{ZOTERO_KEY}/`

| File | Contents | Produced by |
|---|---|---|
| `layout_elements.json` | Semantic elements with bboxes: `{page: [{label, text, bbox}]}` | Docling (script 05) |
| `page_texts.json` | Full page text blobs: `{page: string}` | Docling (script 05) |
| `docling.md` | Markdown rendering of document | Docling (script 05) |
| `bibliography.json` | Refs, citations, abstract, contents | Claude Haiku (script 06) |
| `translation.json` | English translations of elements + page_texts | Claude (script 08) |

### Element labels (from Docling)

`section_header`, `text`, `list_item`, `table`, `table_of_contents`, `picture`, `page_header`, `page_footer`, `footnote`, `formula`, `code`

---

## Project structure

```
scripts/           Pipeline scripts (numbered 01–08)
src/               Core library code (Zotero client, state management)
data/
  texts/           Per-document output directories
  reader.html      The research reader (open directly in browser)
  explore.html     Collection browse view
```

---

## Known limitations

- **OCR quality:** The 15-document sample corpus consists of ILL scans. Docling extracted embedded text rather than performing visual OCR. Many pages have garbled output, especially for Arabic-script content. Phase 3 will address this.
- **API key required:** Scripts 06 and 08 call the Anthropic API. Without a key in `data/.env`, these steps must be run manually or proxied.
- **Translation coverage:** Translation works at page-text level for all three non-English documents. Element-level translation (used in reading view) is complete only for MJEJY7UC (German, 9 pages). Run `08_translate.py` to complete QVUQC6HN and W277BB43.
- **No PDFs in repo:** Source PDFs are not included (Zotero-managed, locally stored). The reader's PDF pane will be empty unless PDFs are present in the document directories.

---

## Phase 3 — OCR (next)

The next phase focuses on replacing Docling's embedded-text extraction with genuine visual OCR for scanned documents. Key design goals:

- Image preprocessing pipeline (deskew, denoise, binarise) as a first step
- Engine routing: Latin-script → Tesseract/EasyOCR, Arabic-script → Tesseract (ara) or Kraken
- Output writes into the same `layout_elements.json` / `page_texts.json` schema — reader is unchanged
- Quality validation via multi-engine comparison

See [`LESSONS_LEARNED.md`](LESSONS_LEARNED.md) for the full analysis and recommendations.
