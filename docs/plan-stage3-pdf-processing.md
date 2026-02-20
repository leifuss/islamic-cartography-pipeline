# Stage 3: PDF Processing — "My PDFs are searchable and readable online"

> **Goal:** The researcher's PDFs are fetched from Zotero's cloud (or
> uploaded directly), processed through OCR, and presented in a web
> reader with full-text content.  Translation and bibliography
> extraction become available for processed documents.

## 1. User Stories

- *As a researcher, I want my Zotero PDFs to be automatically fetched
  and processed without me having to download or upload them.*
- *As a researcher with scanned documents, I want OCR to extract
  readable text from my scans.*
- *As a researcher working with Arabic/Persian texts, I want OCR that
  handles right-to-left scripts correctly.*
- *As a researcher, I want to read my PDFs in a clean web reader
  without needing a PDF viewer.*
- *As a researcher, I want to see a machine translation alongside
  non-English texts.*
- *As a researcher, I want bibliographic references extracted
  automatically from my sources.*

## 2. Current State

This stage is **largely built**.  The recent cloud-native refactor
(removing `ZOTERO_LOCAL`) means the entire pipeline runs in GitHub
Actions with no local machine involvement.

### Already implemented:
- `00_stage_pdfs.py` — downloads PDFs from Zotero web API
- `05b_extract_robust.py` — hybrid extraction (pypdfium2 + Tesseract +
  optional Google Vision)
- `05c_layout_heron.py` — semantic layout enrichment via Modal GPU
- `06_extract_bibliography.py` — bibliography extraction
- `08_translate.py` — translation of non-English docs
- `extract.yml` — GitHub Actions workflow for cloud processing
- `reader.html` — web reader for extracted documents

### Remaining work for Stage 3:
- [ ] Trigger PDF fetch automatically after sync detects new items with
      attachments
- [ ] Quality triage UI (let user review/approve re-processing of
      garbled docs)
- [ ] Upload path for documents not in Zotero (scans, unpublished)
- [ ] Cost estimation before Vision OCR processing
- [ ] Progress reporting visible in the explorer

## 3. Processing Pipeline

```
Zotero sync detects new items with attachments
    ↓
00_stage_pdfs.py downloads PDFs from Zotero cloud
    ↓
05b_extract_robust.py runs extraction:
    ├── Embedded text → pypdfium2 (fast, free)
    ├── Blank/scanned pages → Tesseract OCR (slow, free)
    └── Garbled text → Google Vision API (user's key, ~$0.002/page)
    ↓
Output: data/texts/{key}/
    ├── page_texts.json      (full text per page)
    ├── meta.json             (quality metrics)
    └── pages/{001..N}.jpg   (page images for reader)
    ↓
Optional post-processing:
    ├── 05c_layout_heron.py  (semantic layout via Modal GPU)
    ├── 06_extract_bibliography.py (reference extraction)
    └── 08_translate.py       (translation)
    ↓
00_update_inventory_flags.py updates inventory.json
    ↓
Explorer + Reader show processed documents
```

## 4. Key Implementation Items

### 4.1 Automatic Extraction Trigger

Currently, extraction is triggered manually or by the zotero-sync
workflow when new items are detected.  We should make this more
seamless:

```yaml
# In zotero-sync.yml, after committing new inventory:
- name: Trigger PDF fetch + extraction for new items
  if: steps.sync.outputs.has_new == 'true'
  run: |
    gh workflow run extract.yml \
      -f keys="${{ steps.sync.outputs.new_keys }}"
```

This is **already implemented** in the current workflow.  The remaining
issue is ensuring `extract.yml` handles the PDF fetch step reliably
(which was added in the cloud-native refactor).

### 4.2 Quality Triage UI

After extraction, some documents will have poor quality text (garbled
fonts, bad OCR).  The user needs a way to review these and decide
whether to:
- Accept the text as-is
- Re-process with Google Vision API (costs money)
- Flag for manual review later

**Implementation:**

Add a "Quality Review" view to the explorer:

```
┌─────────────────────────────────────────────────┐
│  Quality Review                                 │
│                                                 │
│  12 documents need attention:                   │
│                                                 │
│  ⚠ [QIGTV3FC] Al-Idrisi's World Map           │
│    Quality: garbled (avg 12 chars/page)         │
│    [Preview] [Re-process with Vision ($0.08)]   │
│                                                 │
│  ⚠ [HMPTGZFQ] Ottoman Maritime Charts          │
│    Quality: suspect (PUA ratio 0.03)            │
│    [Preview] [Re-process with Vision ($0.15)]   │
│                                                 │
│  [Process all selected ($0.23)]  [Skip for now] │
└─────────────────────────────────────────────────┘
```

This view reads `text_quality` from inventory.json and provides
one-click re-processing.  The "Re-process" button triggers the
extract workflow via GitHub API (or via a form that commits a
trigger file).

**Effort:** 2 days (UI + workflow integration)

### 4.3 Direct Upload Path

For documents not in Zotero (local scans, unpublished manuscripts):

- User drags PDF into the GitHub repo's `data/pdfs/` folder via
  GitHub's web UI
- A workflow detects new PDFs and:
  1. Creates an inventory entry (minimal metadata: filename, upload date)
  2. Runs extraction
  3. Prompts user to add metadata (title, author, year) via an issue

**Effort:** 1 day

### 4.4 Cost Estimation

Before triggering Vision OCR, show the user an estimate:

```python
def estimate_vision_cost(inventory: list, keys: list) -> dict:
    """Estimate Google Vision API cost for given documents."""
    total_pages = sum(
        item.get('page_count', 0)
        for item in inventory
        if item['key'] in keys
    )
    cost_per_page = 0.0015  # Vision API pricing
    return {
        'pages': total_pages,
        'estimated_cost': round(total_pages * cost_per_page, 2),
        'currency': 'USD',
    }
```

This should be shown in the quality triage UI and in dry-run output.

### 4.5 Progress Reporting

The explorer should show extraction progress:

- "Processing: 5 of 47 documents extracted"
- Per-document status: queued → extracting → done / error
- Link to the GitHub Actions run for details

This can read from `data/pipeline_status.json` (already generated
by the extraction script) or from inventory.json flags.

## 5. API Key Management

### Free tier (no API key required):
- pypdfium2 text extraction (embedded fonts)
- Tesseract OCR (scanned pages in supported languages)
- Layout enrichment via Heron (if Modal is configured)

### User's own API key required:
- Google Vision OCR (for garbled/complex scripts)
- LLM-based translation (Gemini or Claude)
- Bibliography extraction (uses LLM)

### How users provide their API key:

Option A (current): Set as a GitHub repository secret.
- Pro: Secure. Works in Actions.
- Con: Requires navigating to Settings → Secrets.

Option B: Add to `.env` via a setup workflow input.
- Pro: Easier to discover.
- Con: Less secure (visible in workflow logs if not careful).

**Recommendation:** Keep Option A but provide clear screenshots in
the documentation.  The setup wizard (Stage 1) can include an
"Advanced: add API keys" section.

## 6. Language Support

### Currently supported OCR languages:
- English, Arabic, Persian, Turkish, German, French
- (Tesseract language packs installed in extract.yml)

### To add a new language:
- Add `tesseract-ocr-{lang}` to the apt-get install in extract.yml
- No code changes needed — Tesseract auto-detects or uses langdetect

### Languages requiring special handling:
- Chinese/Japanese/Korean — need CJK Tesseract models (large)
- Hebrew — similar to Arabic RTL handling
- Sanskrit/Tibetan — limited Tesseract support, Vision API recommended

## 7. Testing Plan

| Test case | Method |
|-----------|--------|
| PDF fetch from Zotero cloud | Run 00_stage_pdfs.py against real library |
| Extraction of embedded-font PDF | Process a modern academic PDF |
| OCR of scanned Arabic manuscript | Process a known scanned document |
| Vision fallback for garbled text | Process a doc with known bad fonts |
| Quality triage UI shows correct items | Check after extraction run |
| Direct upload path | Add PDF via GitHub web UI, verify workflow triggers |
| Cost estimation accuracy | Compare estimate vs actual Vision API bill |
| Reader renders extracted doc | Open reader.html for a processed doc |

## 8. Estimated Effort

| Task | Effort | Status |
|------|--------|--------|
| Cloud-native PDF fetch | Done | Completed (this session) |
| Extraction pipeline | Done | Working |
| Auto-trigger on new items | Done | In zotero-sync.yml |
| Quality triage UI | 2 days | To do |
| Direct upload path | 1 day | To do |
| Cost estimation | 0.5 day | To do |
| Progress reporting in explorer | 1 day | To do |
| Documentation | 0.5 day | To do |
| **Total remaining** | **~5 days** | |
