# Pipeline Status — Islamic Cartography Research Tool

*Last updated: February 2026*

## Sample corpus: 15 documents

These 15 documents were processed as a development sample from the 313-item Zotero collection. All are ILL (interlibrary loan) scans of academic articles — not digitally-born PDFs.

### Per-document file inventory

| Key | Title (short) | Lang | layout | page_texts | bib | translation | docling.md |
|---|---|---|---|---|---|---|---|
| 228AHDGB | Geography and Mapmaking | EN | ✓ | ✓ | ✓ | — | ✓ |
| 23K87F66 | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| 5FBQAZ7C | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| 6ELZA7R7 | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| 6Y43BXJ9 | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| ANLFTXVL | Journey of Maps, Silk Road | EN | ✓ | ✓ | pending | — | ✓ |
| C9YPFKU3 | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| CR7CQJJ8 | (article) | EN | ✗ | ✓ | pending | — | ✓ |
| DUZKRZFQ | (article) | EN | ✓ | ✓ | ✓ | — | ✓ |
| HMPTGZID | Against Ptolemy / Cosmography | EN | ✓ | ✓ | pending | — | ✓ |
| MJEJY7UC | Ps.-Aristotle De Mundo (Arabic MS) | DE | ✓ | ✓ | ✓ | ✓ | ✓ |
| PRZTK6C7 | Cosmology and Cosmic Order | EN | ✓ | ✓ | pending | — | ✓ |
| QIGTV3FC | Chapter Six (Ptolemy) | EN | ✓ | ✓ | pending | — | ✓ |
| QVUQC6HN | World as Apple / Orb and Cartography | ES | ✓ | ✓ | ✓ | ✓ (partial) | ✓ |
| W277BB43 | Roman Épistolaire / Salim Abu-l-Ala | FR | ✓ | ✓ | pending | ✓ (partial) | ✓ |

**legend:** ✓ = complete · ✗ = missing · pending = extraction queued · partial = page_texts translated, elements not

### Translation detail

Three non-English documents were translated (FR→EN, DE→EN, ES→EN):

| Key | page_texts | elements | Notes |
|---|---|---|---|
| MJEJY7UC (DE) | ✓ all 9 pages | ~36% | DE_el dict was hand-written per page |
| QVUQC6HN (ES) | ✓ all 13 pages | ~6% | Only heading elements translated |
| W277BB43 (FR) | ✓ all 55 pages | ~0% | No element-level entries written |

*The reader's "Reading view" renders elements; translation is only visible in "Page text" view for QVUQC6HN and W277BB43. Element-level translation requires running `08_translate.py` with a valid API key.*

---

## Pipeline script inventory

| Script | Purpose | Status |
|---|---|---|
| `01_setup.py` | Verify Zotero connection, environment | Working |
| `02_test_extraction.py` | Test Docling on a single file | Working |
| `03_inventory.py` | List all items in collection | Working |
| `04_download_pdfs.py` | Download attachments from Zotero | Working (PDFs must be locally stored) |
| `05_extract_embedded.py` | Run Docling → layout_elements.json, page_texts.json | Working for digital PDFs; OCR quality varies for scans |
| `06_extract_bibliography.py` | Extract refs, citations, summary via Claude | **Requires ANTHROPIC_API_KEY** |
| `07_extract_spans.py` | Extract text spans for annotation | Implemented, not yet run at scale |
| `08_translate.py` | Translate non-English docs element-by-element | **Requires ANTHROPIC_API_KEY** |
| `generate_reader.py` | Generate reader.html from data | Working |
| `generate_explore.py` | Generate explore/index view | Working |

### Blocker: ANTHROPIC_API_KEY

`data/.env` has `ANTHROPIC_API_KEY=` (empty). Scripts 06 and 08 cannot run directly. Workarounds used:
- Bibliography extraction: run via Claude Code's PAL tool (proxied through Gemini) — slow, timeout-prone for large docs
- Translation: hand-translated and embedded in `/tmp/do_translations.py` for 3 docs — not scalable

**Resolution needed before scaling:** Add a valid API key to `data/.env` or `~/.env`.

---

## Reader (data/reader.html)

The reader is the primary interface for the processed corpus. Features:
- Side-by-side PDF (if available) and reading pane
- Semantic element rendering (bboxes from Docling, colour-coded by label type)
- Page text / reading view / full-text view modes
- Bibliography panel (refs + citations from bibliography.json)
- Translation toggle (when translation.json present)
- Notes panel

**Known issues:**
- Translation toggle only improves reading experience when element-level translations exist
- PDF pane requires PDF to be in the data directory; currently all PDFs absent (ILL scans only)
- Some pages have no layout elements (CR7CQJJ8 missing layout_elements.json entirely)
