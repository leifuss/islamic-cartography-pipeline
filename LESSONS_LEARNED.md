# Lessons Learned — Phase 1 & 2 Development

*February 2026 — End of initial pipeline build*

This document captures what was learned building the Zotero → structured text → reader pipeline across 15 sample documents. Written as preparation for Phase 3 (OCR).

---

## What works well

### The data model is sound

The choice to store extracted data as per-document JSON files (`layout_elements.json`, `page_texts.json`, `bibliography.json`, `translation.json`) has proven robust:

- Schema is flat and predictable — easy to read, write, and debug
- Docling's element structure (`label`, `text`, `bbox`, `page`) maps cleanly to reader needs
- Adding new data types (bibliography, translation) didn't require restructuring existing files
- `page_texts.json` as a simple `{page_number: string}` dict is the right granularity for most use cases

**Keep for Phase 3:** the schema is stable. OCR output should write into these same files.

### Docling is excellent for digitally-born PDFs

For PDFs that were born digital (machine-readable text layer), Docling produces:
- Accurate bounding boxes that align with visual layout
- Meaningful semantic labels (section_header, text, list_item, table, picture, page_header, footnote)
- Clean page_texts that read coherently
- Consistent 1-based page numbering

This part of the pipeline is ready for production.

### The reader is a solid interface

`data/reader.html` is a well-structured, self-contained reader that demonstrates the value of the pipeline. The architecture (single HTML file, data fetched from relative JSON paths) means it works with any static file server and is easy to extend. The element overlay approach (rendering coloured boxes over PDF/image pane and text in reading pane simultaneously) gives genuine research value.

### Zotero integration works

`pyzotero` connecting to the local Zotero SQLite database is reliable. The 313-item collection is enumerable; attachment paths resolve correctly. This is not a problem to solve.

---

## What's effective but needs work

### Bibliography extraction

`06_extract_bibliography.py` produces good structured output — the bibliography.json format (refs with structured fields, in-text citations with context, abstract + contents) is genuinely useful. The extraction quality from Claude Haiku is good: it correctly identifies bibliography sections, distinguishes footnote numbers from author-year citations, and produces coherent abstracts.

**The problem is operational, not conceptual:** the script can't run without an API key. Once that's resolved, this step should run cleanly at scale.

**At scale consideration:** For 313 documents, 3 API calls per document = ~940 calls. At Haiku pricing this is negligible. Batching by document type (English-only first, then non-English with translation hint) would be sensible.

### Translation — page_texts vs elements

The translation pipeline revealed an important distinction in how the reader uses translated data:

- **Page text view** uses `translation.page_texts[page]` — a whole-page string. Easy to produce, works well.
- **Reading/elements view** uses `translation.elements[page][i].text` — per-element translated strings. Hard to produce at scale, but this is what users see by default.

The three translated documents proved the concept at the page_texts level but failed at element level for the two longer documents (QVUQC6HN, W277BB43) because element-level translations were not provided. MJEJY7UC (9 pages, German) showed the full translation working properly because element translations were hand-written for all pages.

**Lesson:** translation is only truly complete when elements are translated. `08_translate.py` does this correctly — it calls Claude on each element individually and writes back into the element structure. This is the right approach but requires the API key.

**For future translation runs:** run `08_translate.py` after the API key issue is resolved. The page_texts translations already written are usable as context/fallback.

### Background agents across sessions

Using `Task(run_in_background=True)` for long-running extraction jobs works within a session. Agents are lost across session gaps (context window exhaustion, new session start). The agent IDs don't survive.

**Lesson:** For long background jobs, write progress to disk as you go (which the scripts already do — they write one file per document). Don't rely on in-memory state across sessions.

---

## What is broken or unstarted

### OCR — the core unresolved problem

All 15 sample documents are ILL (interlibrary loan) scans. This means:

1. **The PDF files are rasterised images**, not text-bearing PDFs. Docling extracted whatever embedded text layer existed (often poor quality or scan metadata artefacts), not actual visual OCR.

2. **Many pages have garbled text** — scan encoding artefacts, mixed Arabic/Latin character confusion, ILL cover sheet boilerplate mixed into content pages.

3. **Arabic-script content is especially problematic.** Pages containing Arabic text (either Arabic-language articles, or Arabic quotations within Latin-script articles) produce nonsense from Docling's internal OCR. Right-to-left script, connected ligatures, and the diversity of Arabic typefaces all demand a dedicated Arabic OCR engine.

4. **The bboxes may not align** with actual visual text on the page images, because Docling inferred layout from the embedded text rather than from the rendered page image.

**What OCR Phase 3 needs to address:**
- True visual OCR on the page images (not embedded text extraction)
- Preprocessing: deskew, denoise, binarise (especially for old photocopier-quality ILL scans)
- Engine routing by script: Latin-script → Tesseract (English/French/German/Spanish trained) or EasyOCR; Arabic script → Tesseract (ara), Kraken, or a dedicated Arabic OCR model
- Possibly Transkribus for any handwritten or early-print manuscript content
- Multi-engine comparison / voting for quality validation

### Document type taxonomy

The 313-item corpus has not been formally characterised by type. Before designing the OCR pipeline, it would help to know:
- How many are digitally-born PDFs vs. scanned?
- Among scans: ILL photocopies, book scans, or high-quality archive digitisations?
- What proportion contain significant Arabic text (vs. being entirely Latin-script)?
- Are there manuscript images that are illustrations only (not OCR targets)?

**A one-time Zotero inventory pass** (`03_inventory.py` + human review) to tag each document with type would save significant OCR design effort.

### The API key situation

Almost nothing in this pipeline can run autonomously without `ANTHROPIC_API_KEY` set. Scripts 06 and 08 both require it. The workaround (proxying through PAL/Gemini) is fragile and unscalable. This is a purely administrative blocker.

---

## Zotero → accessible text: chain assessment

```
Zotero record
    │
    ▼ [01_setup / 03_inventory] ──────── SOLID
Attachment path on disk (PDF or image)
    │
    ▼ [04_download_pdfs / 05_extract_embedded] ── SOLID for digital PDFs
    │                                              BROKEN for ILL scans (Phase 3 target)
Docling output:
  • layout_elements.json  ←─ SOLID (schema stable)
  • page_texts.json       ←─ SOLID for digital, UNRELIABLE for scans
  • docling.md            ←─ SOLID
    │
    ├──▶ [06_extract_bibliography] ─────── WORKS (needs API key)
    │       bibliography.json
    │
    ├──▶ [08_translate] ────────────────── WORKS at page level (needs API key)
    │       translation.json                BROKEN at element level without API key
    │
    └──▶ [generate_reader]  ────────────── SOLID
            reader.html
```

---

## Recommendations for Phase 3

1. **Resolve the API key first.** Set `ANTHROPIC_API_KEY` in `data/.env`. Run `06_extract_bibliography.py` across all 15 docs. Run `08_translate.py` on QVUQC6HN and W277BB43 to fix element-level translations. This clears the backlog before adding OCR complexity.

2. **Characterise the corpus before building OCR.** A quick manual scan of 20–30 Zotero records will reveal what proportion are ILL scans vs. digital PDFs, and what languages are represented. This shapes OCR engine choice significantly.

3. **Build OCR as a replaceable step, not an integrated one.** The existing `layout_elements.json` schema is the right output target. OCR should write into the same structure as Docling's text extraction — so the reader and downstream scripts don't need to know which engine produced the text.

4. **Prioritise Latin-script OCR first.** Most of the 313 items are likely Latin-script. Getting clean English/French/German OCR working reliably is achievable quickly and will demonstrate value across most of the corpus. Arabic OCR is harder and should be a separate workstream.

5. **Image preprocessing is not optional.** ILL scan quality varies enormously. A preprocessing pass (OpenCV or Pillow: deskew, CLAHE contrast enhancement, binarise) before OCR will substantially improve output quality across all engines.

6. **Keep page_texts as the primary readable artefact.** The element-level bboxes are valuable for layout, but page_texts is what most downstream uses (search, translation, display) actually need. Prioritise getting clean page_texts first; element-level text quality comes second.
