# Cross-Cutting: User Annotations, Notes & Zotero Write-Back

> **Goal:** The researcher can create notes, bookmarks, highlights,
> and cross-references directly in the browser.  These annotations
> are stored in the repository and optionally synced back to Zotero
> as note items.

## 1. User Stories

- *As a researcher reading a text in the reader, I want to highlight
  a passage and add a note, like I would in a physical book.*
- *As a researcher browsing the explorer, I want to bookmark items
  I plan to read next.*
- *As a researcher, I want to create links between two documents that
  discuss the same topic ("see also").*
- *As a researcher, I want my Scholion notes to appear in my Zotero
  library so I don't lose them if I stop using the tool.*
- *As a researcher, I want notes I write in Zotero to show up in
  Scholion automatically.*

## 2. Annotation Types

| Type | Where | Data stored |
|------|-------|-------------|
| **Bookmark** | Explorer | `{doc_key, created_at, label?}` |
| **Document note** | Explorer / Reader | `{doc_key, text, created_at}` |
| **Highlight** | Reader (text selection) | `{doc_key, page, start_offset, end_offset, text, color?}` |
| **Margin note** | Reader (per-page) | `{doc_key, page, text, anchor_offset?, created_at}` |
| **Cross-reference** | Reader | `{source_key, source_page?, target_key, target_page?, label}` |
| **Tag** | Explorer | `{doc_key, tag_name, created_at}` |

## 3. Storage: `data/annotations.json`

All annotations live in a single JSON file, versioned in git:

```json
{
  "version": 1,
  "updated_at": "2026-02-20T16:00:00Z",
  "annotations": [
    {
      "id": "ann_001",
      "type": "highlight",
      "doc_key": "5FBQAZ7C",
      "page": 7,
      "start_offset": 234,
      "end_offset": 312,
      "text": "the Ottoman cartographic tradition drew heavily on...",
      "note": "Key claim — compare with Emiralioğlu",
      "color": "yellow",
      "created_at": "2026-02-20T14:30:00Z",
      "synced_to_zotero": true,
      "zotero_note_key": "ABC12345"
    },
    {
      "id": "ann_002",
      "type": "bookmark",
      "doc_key": "HMPTGZFQ",
      "label": "Read next",
      "created_at": "2026-02-20T15:00:00Z"
    },
    {
      "id": "ann_003",
      "type": "cross_reference",
      "source_key": "5FBQAZ7C",
      "source_page": 7,
      "target_key": "HMPTGZFQ",
      "target_page": 45,
      "label": "Challenges the naval cartography thesis",
      "created_at": "2026-02-20T15:30:00Z"
    }
  ]
}
```

**Why one file, not per-document?**
- Simpler to load (one fetch)
- Cross-references span documents
- For a typical corpus (<500 docs), annotations will be <1 MB
- Git diffs are readable
- Can be split later if needed

## 4. How Annotations Are Created (Client-Side)

Annotations are created entirely in the browser:

### In the Explorer:
- **Bookmark:** Click a bookmark icon next to any item
- **Document note:** Click "Add note" on an item card, type in a modal
- **Tag:** Click "Add tag" on an item card

### In the Reader:
- **Highlight:** Select text → toolbar appears → choose colour / add note
- **Margin note:** Click in the margin next to a page → type
- **Cross-reference:** While reading doc A, click "Link to..." → search
  for doc B → select page → add label

### Saving back to the repo:

The browser cannot write directly to the git repo. Three approaches:

**Option A: GitHub API commit (recommended)**

The explorer/reader uses the GitHub Contents API to read and write
`data/annotations.json`:

```javascript
// Read annotations
const resp = await fetch(
  `https://api.github.com/repos/${owner}/${repo}/contents/data/annotations.json`,
  { headers: { Authorization: `token ${github_token}` } }
);
const data = JSON.parse(atob(resp.json().content));

// Write updated annotations
await fetch(
  `https://api.github.com/repos/${owner}/${repo}/contents/data/annotations.json`,
  {
    method: 'PUT',
    headers: { Authorization: `token ${github_token}` },
    body: JSON.stringify({
      message: 'annotation: add highlight on 5FBQAZ7C p.7',
      content: btoa(JSON.stringify(updatedAnnotations, null, 2)),
      sha: currentSha,
    })
  }
);
```

**Authentication:** The user provides a GitHub personal access token
(classic or fine-grained) with `contents: write` scope.  This token
is stored in the browser's localStorage (not ideal for shared
computers, but acceptable for a personal research tool).

**Pro:** Works with GitHub Pages, no server needed.
**Con:** Requires a GitHub token; creates a git commit for each save.

**Option B: localStorage + periodic export**

Annotations are stored in the browser's localStorage and periodically
exported (downloaded as a JSON file that the user commits manually,
or auto-committed via a GitHub Action).

**Pro:** No authentication needed.
**Con:** Annotations are lost if the user clears browser data; not
synced across devices.

**Option C: Hybrid**

Default to localStorage for instant saves; periodically sync to
GitHub via the Contents API (batch commit every 5 minutes or on
page unload).

**Recommendation:** Start with **Option C** (hybrid). localStorage
for immediate responsiveness, GitHub API for persistence.  The setup
wizard can include a "Generate a GitHub token for annotations" step.

## 5. Zotero Write-Back

### Scholion → Zotero

When the user creates a document note or highlight, optionally sync
it to Zotero as a child note on the parent item.

```python
# scripts/sync_annotations_to_zotero.py

def sync_annotation_to_zotero(library, annotation):
    """Create or update a Zotero note from a Scholion annotation."""
    note_html = _format_as_zotero_note(annotation)

    if annotation.get("zotero_note_key"):
        # Update existing note
        library.client.update_item({
            "key": annotation["zotero_note_key"],
            "data": {"note": note_html}
        })
    else:
        # Create new child note
        template = library.client.item_template("note")
        template["parentItem"] = annotation["doc_key"]
        template["note"] = note_html
        template["tags"] = [{"tag": "scholion"}]
        result = library.client.create_items([template])
        return result  # includes the new note's key
```

**Format in Zotero:** Notes are HTML. We format them with a
Scholion header so they're identifiable:

```html
<p><strong>[Scholion]</strong> Highlight on p.7:</p>
<blockquote>"the Ottoman cartographic tradition drew heavily on..."</blockquote>
<p>Key claim — compare with Emiralioğlu</p>
<p><small>Created 2026-02-20 14:30</small></p>
```

### Zotero → Scholion

Already implemented: `zotero_sync.py` fetches note children and stores
them in `inventory.json` under the `notes` field.  These appear in the
explorer and reader.

**Conflict resolution:** If the same note is edited in both Zotero
and Scholion, the Zotero version wins (it is the "system of record"
for existing Zotero notes).  Scholion-originated notes (tagged
`scholion`) are updated from Scholion and not overwritten by sync.

## 6. UI Mockups

### Explorer: Bookmarked Items

```
┌─────────────────────────────────────────────────┐
│  Bookmarks (3)                                  │
│                                                 │
│  ★ [HMPTGZFQ] Ottoman Maritime Charts          │
│    "Read next" · bookmarked 2 days ago          │
│                                                 │
│  ★ [5FBQAZ7C] Tradition and Innovation...       │
│    bookmarked 1 week ago                        │
│                                                 │
│  ★ [QIGTV3FC] Al-Idrisi's World Map            │
│    "Compare with Karamustafa" · bookmarked 2w   │
└─────────────────────────────────────────────────┘
```

### Reader: Highlights and Margin Notes

```
┌──────────────────────────────────────────────────────────┐
│  Reader: Karamustafa (1992)                    p. 7 of 24│
│                                                          │
│  [highlighted]The Ottoman mapping tradition drew heavily  │
│  on earlier Islamic cartographic practice.[/highlighted]  │
│  ┌────────────────────────────────┐                      │
│  │ 📝 Key claim — compare with   │                      │
│  │    Emiralioğlu (2014, p.45)   │                      │
│  │    🔗 → HMPTGZFQ p.45        │                      │
│  └────────────────────────────────┘                      │
│                                                          │
│  However, the precise mechanisms of transmission remain  │
│  debated.  Some scholars have argued...                  │
│                                                          │
│                         │ [margin] This section is        │
│                         │ superseded by Pinto 2016. See   │
│                         │ also my Zotero note on this.    │
│                         │ — 20 Feb 2026                   │
└──────────────────────────────────────────────────────────┘
```

## 7. Implementation Tasks

| # | Task | Effort | Stage | Priority |
|---|------|--------|-------|----------|
| A.1 | `data/annotations.json` schema + read/write utility | 0.5 day | Alpha | P0 |
| A.2 | Bookmark UI in explorer | 0.5 day | Alpha | P0 |
| A.3 | Document note UI in explorer | 1 day | Alpha | P0 |
| A.4 | GitHub API commit integration (save annotations) | 1 day | Alpha | P0 |
| A.5 | localStorage fallback + sync | 0.5 day | Alpha | P1 |
| A.6 | Highlight UI in reader (text selection) | 1.5 days | Beta | P0 |
| A.7 | Margin note UI in reader | 1 day | Beta | P1 |
| A.8 | Cross-reference creation UI | 1 day | Beta | P1 |
| A.9 | Zotero write-back script | 1 day | Beta | P1 |
| A.10 | Zotero write-back workflow (periodic sync) | 0.5 day | Beta | P1 |
| A.11 | Annotation display in search results (Stage 4) | 0.5 day | v1.0 | P2 |
| **Total (P0)** | | **~4.5 days** | | |
| **Total (all)** | | **~9 days** | | |

## 8. Testing Plan

| Test case | Method |
|-----------|--------|
| Create bookmark in explorer | Manual UI test |
| Create document note, verify in annotations.json | Manual + check file |
| Highlight text in reader, verify persistence | Refresh page, check highlight preserved |
| Cross-reference links to correct target | Click link, verify navigation |
| GitHub API save works (create commit) | End-to-end with test repo |
| Zotero write-back creates note | Check Zotero library after sync |
| Zotero → Scholion sync preserves existing notes | Run zotero_sync, verify no data loss |
| Annotations survive git pull/merge | Two devices, verify merge |
| localStorage fallback works offline | Disconnect network, create annotation |

## 9. Open Questions

1. **Annotation format standard:** Should we use the W3C Web Annotation
   Data Model?  Pro: interop with Hypothes.is.  Con: more complex
   schema than needed for MVP.  **Recommendation:** Start with our
   simple schema; add W3C export later.

2. **Collaborative annotations:** If multiple people share a repo,
   should their annotations be in the same file?  **Recommendation:**
   Separate files per user (`annotations-{username}.json`) for
   collaboration scenarios; single file for solo use.

3. **Annotation search:** Should annotations be included in the
   Stage 4 search index?  **Yes** — this lets you search your own
   notes across the corpus.

4. **Rate limiting:** GitHub Contents API has rate limits (5000
   req/hour for authenticated users).  Batching saves (every 5 min
   or on page unload) should stay well within this.
