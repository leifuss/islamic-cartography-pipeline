# Stage 2: Bibliography Import & Exploration — "I can browse and visualise my library"

> **Goal:** The researcher's bibliography is imported from their chosen
> tool, and the explorer provides genuine utility beyond what Zotero/
> Mendeley/Endnote offer natively: timeline views, geographic
> visualisation, tag analysis, and a polished browsing experience.

## 1. User Stories

- *As a researcher using Zotero, I want my library to sync automatically
  so that new items appear in my dashboard without manual effort.*
- *As a researcher using Mendeley, I want to import my library so I can
  use the same exploration tools.*
- *As a researcher with a BibTeX file, I want to upload it and see my
  bibliography visualised immediately.*
- *As a researcher, I want to see my sources on a timeline so I can
  understand the chronological shape of my bibliography.*
- *As a researcher, I want to click on any entry and go directly to the
  source (URL, DOI, or publisher page).*

## 2. Import Sources

### 2.1 Zotero (primary, already implemented)

**Current state:** Fully working. `zotero_sync.py` connects via web API,
fetches metadata + children in one paginated call, merges into
`inventory.json`. Scheduled every 6 hours via GitHub Actions.

**Remaining work:**
- [ ] Ensure the sync workflow is automatically enabled after Stage 1 setup
- [ ] Add Zotero collection picker to setup wizard (let user choose which
      collection to sync, or sync entire library)

### 2.2 BibTeX Upload (universal fallback)

**Why this matters:** Every bibliography manager can export to BibTeX.
This is the universal interchange format for academic citations. By
supporting BibTeX, we immediately support Mendeley, Endnote, JabRef,
and any other tool.

**Implementation plan:**

```
scripts/import_bibtex.py
  ├─ Parse .bib file using `bibtexparser` library
  ├─ Map BibTeX fields → inventory.json fields:
  │   @article{smith2020,         {
  │     author = {Smith, J.},  →    "authors": "Smith",
  │     title = {On Maps},     →    "title": "On Maps",
  │     year = {2020},         →    "year": "2020",
  │     journal = {Cartographica}, → "pub_title": "Cartographica",
  │     doi = {10.1234/...},   →    "url": "https://doi.org/10.1234/...",
  │     pages = {1--20},       →    "pages": "1-20",
  │   }                           }
  ├─ Generate stable keys (hash of author+year+title, or use BibTeX key)
  ├─ Merge with existing inventory (don't overwrite pipeline fields)
  └─ Write data/inventory.json
```

**Upload mechanism:**
- A GitHub Action workflow (`import-bibtex.yml`) triggered via workflow
  dispatch with a file upload — but GitHub Actions doesn't support file
  upload inputs.
- **Alternative 1:** User commits a `.bib` file to `data/import/` and
  pushes. A workflow detects the new file and runs the import.
- **Alternative 2:** The explorer UI has an "Import BibTeX" button that
  uses the GitHub API to commit the file (requires GitHub token — tricky
  for non-technical users).
- **Alternative 3:** A simple web form on the setup page that takes a
  `.bib` file, reads it client-side, converts to `inventory.json`
  format, and commits via GitHub API.

**Recommendation:** Alternative 1 is simplest. The welcome issue can
say "Drag your .bib file into the `data/import/` folder in GitHub."

### 2.3 Mendeley API (future, lower priority)

Mendeley has a REST API but requires OAuth2 authentication, which is
more complex than Zotero's simple API key. Since Mendeley can export
to BibTeX, this is a convenience feature, not a blocker.

- [ ] `scripts/import_mendeley.py` — OAuth2 flow, fetch library, map to
      inventory.json format
- [ ] Mendeley OAuth app registration
- [ ] Setup wizard support for Mendeley credentials

### 2.4 Endnote (future, lowest priority)

Endnote has no public API. Import via BibTeX or RIS export.

- [ ] `scripts/import_ris.py` — Parse RIS format (common Endnote export)
- [ ] Map RIS fields → inventory.json

## 3. Explorer Enhancements

### 3.1 Current Features (already built)

- Filterable table with search
- Tag cloud
- Document type breakdown (pie chart)
- Language distribution
- Extraction status indicators
- Reader links for extracted docs

### 3.2 New Features for Stage 2

#### Timeline Visualisation

A horizontal timeline showing publications by year, with density
indication.  This gives researchers an immediate sense of the
chronological shape of their bibliography.

```
Implementation:
  - Add a <canvas> or SVG element to explore.html
  - Plot inventory items by year
  - Colour-code by doc_type or language
  - Click a year to filter the table to that year
  - Library: lightweight, no build step — Chart.js or pure SVG
```

**Effort:** 1 day

#### Geographic Visualisation

Show places of publication on a map.  Many humanities bibliographies
have a geographic dimension (Ottoman history → Istanbul, Cairo, Berlin).

```
Implementation:
  - Use the existing city_coords.json geocoding data
  - Add a Leaflet.js map to explore.html (free, no API key needed)
  - Geocode publication places from inventory.place field
  - Cluster markers for cities with many publications
  - Click a marker to filter the table
```

**Effort:** 1–2 days

#### Co-authorship / Citation Preview

Show which authors appear most frequently, and which items share
authors. This is a simple frequency analysis, no AI needed.

```
Implementation:
  - Parse authors field, split by ";"
  - Count author frequencies
  - Show top-N authors as a bar chart or word cloud
  - Click an author to filter the table
```

**Effort:** 0.5 day

#### Enhanced Entry Cards

Each item in the explorer should show:
- Title, authors, year (already shown)
- Abstract (if available from Zotero)
- Tags (clickable, filter the table)
- Direct link to URL/DOI (already partially implemented)
- "Read" link (if PDF has been processed — Stage 3)
- Zotero link (open the item in Zotero web library)

**Effort:** 0.5 day

### 3.3 Value Proposition vs. Native Tools

| Feature | Zotero | Mendeley | Scholion (Stage 2) |
|---------|--------|----------|---------------------|
| Search by title/author | yes | yes | yes |
| Filter by tag | yes (clunky) | yes | yes (one-click) |
| Timeline view | no | no | **yes** |
| Geographic view | no | no | **yes** |
| Author frequency | no | no | **yes** |
| Shareable web URL | no | limited | **yes** (GitHub Pages) |
| Works offline | yes | yes | no (web-based) |
| Custom styling | no | no | yes (HTML/CSS) |

The key differentiator at Stage 2 is **visualisation + shareability**.
A researcher can send their supervisor or collaborator a URL and say
"here's my bibliography, filterable and visualised."

## 4. Data Flow

```
Zotero cloud ──────┐
                    ├──→ zotero_sync.py ──→ data/inventory.json
BibTeX upload ─────┘                             │
                                                  ↓
                                          explore.html (reads
                                          inventory.json at runtime)
                                                  │
                                                  ↓
                                          GitHub Pages
                                          (public URL)
```

## 5. Implementation Tasks

| # | Task | Effort | Depends on |
|---|------|--------|------------|
| 2.1 | BibTeX import script (`import_bibtex.py`) | 1 day | — |
| 2.2 | BibTeX import workflow (`import-bibtex.yml`) | 0.5 day | 2.1 |
| 2.3 | Timeline visualisation in explorer | 1 day | — |
| 2.4 | Geographic visualisation (Leaflet map) | 1.5 days | — |
| 2.5 | Author frequency chart | 0.5 day | — |
| 2.6 | Enhanced entry cards (abstract, tags, links) | 0.5 day | — |
| 2.7 | BibTeX import documentation | 0.5 day | 2.1 |
| 2.8 | Integration testing (Zotero + BibTeX paths) | 0.5 day | 2.1, 2.2 |
| **Total** | | **~6 days** | |

## 6. Testing Plan

| Test case | Method |
|-----------|--------|
| BibTeX import with standard .bib file | Import a real dissertation .bib |
| BibTeX with Unicode (Arabic, Chinese) | Import multilingual .bib |
| BibTeX with missing fields | Ensure graceful handling |
| BibTeX merge with existing Zotero inventory | Import both, verify no duplicates |
| Timeline renders for 1, 50, 500 items | Visual check |
| Map renders with no geocoded places | Graceful empty state |
| Map clusters for >10 items in one city | Visual check |
| Explorer works on mobile (iPad) | Safari/Chrome on tablet |

## 7. Open Questions

1. **Duplicate detection across sources:** If a user has the same item
   in Zotero and in a BibTeX file, how do we detect and merge them?
   (DOI matching is the most reliable; title+year+author fuzzy match
   as fallback.)

2. **BibTeX key as inventory key:** Should we use the BibTeX citation
   key (e.g., `smith2020`) as the inventory key, or generate our own?
   Using the BibTeX key is more intuitive but may collide with Zotero
   keys.  **Recommendation:** Prefix BibTeX keys with `bib-` to
   avoid collisions: `bib-smith2020`.

3. **Ongoing BibTeX sync:** Unlike Zotero (which syncs automatically),
   BibTeX is a one-time import.  Should we support re-importing an
   updated .bib file?  **Yes** — the merge logic handles this
   (update Zotero fields, preserve pipeline fields).

## 8. Transition to Stage 3

At the end of Stage 2, the explorer should prompt the user toward
PDF processing:

> "Your library has **47 items with PDF attachments** in Zotero.
> Process them to enable full-text search and the document reader.
> [Get started with Stage 3 ->](docs/guide-stage3.md)"

This call-to-action appears in the explorer when the inventory has
items with `pdf_status: has_attachment` but no extracted texts yet.
It bridges the gap between browsing metadata and reading full text.
