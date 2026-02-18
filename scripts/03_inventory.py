#!/usr/bin/env python3
"""
Inventory all Zotero items and generate a sortable HTML dashboard.

For each item:
  - PDF status: stored / url_only / no_attachment
  - Doc type:   embedded / scanned / unknown
  - Language detection from text sample
  - Cross-referenced with extraction results if available

Outputs:
  data/inventory.json   – raw inventory data
  data/dashboard.html   – self-contained sortable HTML dashboard

Usage:
    python scripts/03_inventory.py
    python scripts/03_inventory.py --output data/inventory.json
    python scripts/03_inventory.py --no-classify   # skip PDF classification (fast mode)
"""
import sys
import json
import re
import time
import argparse
from pathlib import Path


# ── HTML helper (for Zotero note content) ─────────────────────────────────────

def _strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities from Zotero note HTML."""
    text = re.sub(r'<[^>]+>', ' ', html or '')
    for ent, ch in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                    ('&nbsp;', ' '), ('&#160;', ' '), ('&quot;', '"'), ('&#39;', "'")]:
        text = text.replace(ent, ch)
    return re.sub(r'\s+', ' ', text).strip()

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / 'src')
sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv()

from zotero_client import ZoteroLibrary

# ── PDF classification ─────────────────────────────────────────────────────────
try:
    import pypdfium2 as pdfium
    _PDFIUM = True
except ImportError:
    _PDFIUM = False

try:
    from langdetect import detect, LangDetectException
    _LANGDETECT = True
except ImportError:
    _LANGDETECT = False

# Avg chars/page threshold below which we call a PDF "scanned"
_SCANNED_THRESHOLD = 50


def classify_pdf(path: Path) -> dict:
    """
    Classify a PDF as embedded-font or scanned, and detect language.
    Samples up to the first 5 pages only — fast enough for 300+ items.
    """
    result = {
        'page_count':    None,
        'doc_type':      'unknown',   # embedded | scanned | unknown
        'avg_chars_page': None,
        'language':      None,
        'lang_sample':   None,
    }

    if not _PDFIUM:
        return result

    try:
        doc = pdfium.PdfDocument(str(path))
        n   = len(doc)
        result['page_count'] = n

        # Sample first 3 + last 3 pages (deduped). Bilingual critical editions
        # often have Arabic at the physical back of the book, Western commentary
        # at the front — so we need both ends to detect all languages present.
        head = list(range(min(3, n)))
        tail = list(range(max(0, n - 3), n))
        sample_indices = list(dict.fromkeys(head + tail))  # preserve order, no dups

        texts       = []
        total_chars = 0

        for i in sample_indices:
            page     = doc[i]
            textpage = page.get_textpage()
            text     = textpage.get_text_range()
            total_chars += len(text)
            texts.append(text)

        # Use first-3 pages only for embedded/scanned classification
        first_chars = sum(len(doc[i].get_textpage().get_text_range()) for i in head)
        avg = first_chars / len(head) if head else 0
        result['avg_chars_page'] = round(avg, 1)
        result['doc_type'] = 'scanned' if avg < _SCANNED_THRESHOLD else 'embedded'

        sample_text = ' '.join(texts)[:4000]
        result['lang_sample'] = sample_text[:200].strip()

        if _LANGDETECT and sample_text.strip():
            try:
                result['language'] = detect(sample_text)
            except LangDetectException:
                result['language'] = 'unknown'

    except Exception as e:
        result['doc_type'] = 'error'
        result['error']    = str(e)[:120]

    return result


# ── Attachment status ──────────────────────────────────────────────────────────

def get_attachment_status(library: ZoteroLibrary, item: dict) -> dict:
    """
    Returns:
      { 'status': 'stored'|'url_only'|'no_attachment',
        'pdf_path': str|None,
        'url': str|None,
        'attachment_key': str|None,
        'notes': [str, …]   # text content of Zotero note children
      }
    """
    num_children = item.get('meta', {}).get('numChildren', 0)
    item_url     = item.get('data', {}).get('url', '')

    if num_children == 0:
        status = 'url_only' if item_url else 'no_attachment'
        return {'status': status, 'pdf_path': None, 'url': item_url or None,
                'attachment_key': None, 'notes': []}

    try:
        children = library.client.children(item['key'])
    except Exception:
        return {'status': 'error', 'pdf_path': None, 'url': item_url or None,
                'attachment_key': None, 'notes': []}

    # Collect note children (Zotero annotations / research notes)
    notes = []
    for child in (children or []):
        if child['data'].get('itemType') == 'note':
            note_text = _strip_html(child['data'].get('note', ''))
            if note_text:
                notes.append(note_text)

    for child in (children or []):
        if child['data'].get('itemType') != 'attachment':
            continue

        content_type = child['data'].get('contentType', '')
        if content_type not in ('application/pdf', 'image/png', 'image/jpeg', 'image/tiff'):
            continue

        # Try explicit path
        path_str = child['data'].get('path', '')
        if path_str and path_str.startswith('/') and Path(path_str).exists():
            return {'status': 'stored', 'pdf_path': path_str,
                    'url': item_url or None, 'attachment_key': child['key'],
                    'notes': notes}

        # Try Zotero storage convention
        filename     = child['data'].get('filename', '')
        storage_path = Path.home() / 'Zotero' / 'storage' / child['key'] / filename
        if filename and storage_path.exists():
            return {'status': 'stored', 'pdf_path': str(storage_path),
                    'url': item_url or None, 'attachment_key': child['key'],
                    'notes': notes}

    # Children exist but no local file found
    status = 'url_only' if item_url else 'attachment_missing'
    return {'status': status, 'pdf_path': None, 'url': item_url or None,
            'attachment_key': None, 'notes': notes}


# ── Cross-reference with extraction results ────────────────────────────────────

def load_download_results(path: Path) -> dict:
    """Load download_results.json keyed by item key."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return {r['key']: r for r in data if r.get('status') == 'ok' and r.get('pdf_path')}
    except Exception:
        return {}


def load_extraction_results(path: Path) -> dict:
    """Load test_results.json keyed by item_key."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return {r['item_key']: r for r in data if isinstance(r, dict)}
    except Exception:
        return {}


# ── Main inventory loop ────────────────────────────────────────────────────────

def build_inventory(classify: bool = True) -> list:
    library = ZoteroLibrary()
    print("Fetching items from Zotero...", flush=True)
    items = library.get_all_items()
    print(f"  {len(items)} items found\n")

    extraction = load_extraction_results(_ROOT / 'data' / 'test_results.json')
    downloads  = load_download_results(_ROOT / 'data' / 'download_results.json')

    inventory = []
    n = len(items)

    for idx, item in enumerate(items, 1):
        data    = item.get('data', {})
        key     = item.get('key', '')
        title   = data.get('title', 'Untitled')
        date    = data.get('date', '')
        year    = date[:4] if date else ''
        itype   = data.get('itemType', '')
        url     = data.get('url', '')

        creators  = data.get('creators', [])
        authors   = '; '.join(
            c.get('lastName', c.get('name', ''))
            for c in creators
            if c.get('creatorType') in ('author', 'editor')
        )[:80]
        place     = data.get('place', '')          # place of publication (books)
        publisher = data.get('publisher', '')       # publisher name
        abstract  = data.get('abstractNote', '')    # Zotero abstract field

        # Publication/container title varies by item type
        pub_title = (
            data.get('bookTitle', '')          or  # bookSection
            data.get('publicationTitle', '')   or  # journalArticle, magazineArticle
            data.get('proceedingsTitle', '')   or  # conferencePaper
            data.get('encyclopediaTitle', '')  or  # encyclopediaArticle
            data.get('university', '')         or  # thesis
            data.get('institution', '')        or  # report
            ''
        )

        # Page range for articles / book chapters (e.g. "83-121")
        pages = data.get('pages', '')   # Zotero 'pages' field

        # Tags: list of tag name strings
        tags = [t.get('tag', '') for t in data.get('tags', []) if t.get('tag')]

        print(f"\r  [{idx:3d}/{n}] {title[:55]:<55}", end="", flush=True)

        att = get_attachment_status(library, item)
        pdf_path = att['pdf_path']

        # Fall back to locally downloaded file if Zotero has no attachment
        if not pdf_path and key in downloads:
            dl_path = downloads[key].get('pdf_path')
            if dl_path and Path(dl_path).exists():
                pdf_path = dl_path
                att['status'] = 'downloaded'

        pdf_info = {}
        if classify and pdf_path and att['status'] in ('stored', 'downloaded'):
            pdf_info = classify_pdf(Path(pdf_path))

        ext = extraction.get(key, {})
        quality  = ext.get('quality', {})
        rec      = quality.get('recommendation', '')
        score    = quality.get('score')

        entry = {
            'key':         key,
            'title':       title,
            'year':        year,
            'authors':     authors,
            'item_type':   itype,
            'place':       place,
            'publisher':   publisher,
            'abstract':    abstract or None,
            'pub_title':   pub_title or None,
            'pages':       pages or None,
            'url':         url or att.get('url', ''),
            # Attachment
            'pdf_status':  att['status'],
            'pdf_path':    pdf_path,
            # Classification
            'doc_type':    pdf_info.get('doc_type', 'unknown' if not pdf_path else 'unknown'),
            'page_count':  pdf_info.get('page_count'),
            'avg_chars_pg': pdf_info.get('avg_chars_page'),
            'language':    pdf_info.get('language'),
            # Extraction
            'extracted':   bool(ext),
            'quality_score': round(score, 2) if score is not None else None,
            'recommendation': rec,
            # Zotero metadata
            'tags':        tags,
            'notes':       att.get('notes', []),
        }
        inventory.append(entry)

    print(f"\r  Done — {n} items inventoried.{' '*30}")
    return inventory


# ── HTML Dashboard ─────────────────────────────────────────────────────────────

def build_dashboard(inventory: list) -> str:
    data_json = json.dumps(inventory, ensure_ascii=False, indent=None)

    # Summary stats
    total       = len(inventory)
    stored      = sum(1 for r in inventory if r['pdf_status'] == 'stored')
    downloaded  = sum(1 for r in inventory if r['pdf_status'] == 'downloaded')
    url_only    = sum(1 for r in inventory if r['pdf_status'] == 'url_only')
    no_att      = sum(1 for r in inventory if r['pdf_status'] in ('no_attachment', 'attachment_missing'))
    embedded    = sum(1 for r in inventory if r['doc_type'] == 'embedded')
    scanned     = sum(1 for r in inventory if r['doc_type'] == 'scanned')
    extracted   = sum(1 for r in inventory if r['extracted'])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Islamic Cartography — Corpus Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; font-size: 13px;
         background: #f5f5f7; color: #1d1d1f; }}
  header {{ background: #1c1c1e; color: #fff; padding: 14px 24px;
            display: flex; align-items: center; gap: 20px; }}
  header h1 {{ font-size: 16px; font-weight: 600; }}
  header p  {{ font-size: 12px; color: #98989d; margin-top: 2px; flex: 1; }}
  .site-nav {{ display: flex; gap: 12px; margin-left: auto; }}
  .site-nav a {{ font-size: 12px; color: #98989d; text-decoration: none;
    padding: 4px 10px; border-radius: 6px; border: 1px solid #444;
    transition: color .15s, border-color .15s; }}
  .site-nav a:hover {{ color: #fff; border-color: #888; }}
  .site-nav a.active {{ color: #fff; border-color: #0071e3; }}

  .stats {{ display: flex; gap: 12px; padding: 14px 24px; background: #fff;
            border-bottom: 1px solid #e0e0e5; flex-wrap: wrap; }}
  .stat {{ background: #f5f5f7; border-radius: 8px; padding: 8px 14px; min-width: 100px; }}
  .stat .val {{ font-size: 22px; font-weight: 700; color: #0071e3; }}
  .stat .lbl {{ font-size: 11px; color: #6e6e73; margin-top: 2px; }}

  .controls {{ display: flex; gap: 10px; align-items: center; padding: 10px 24px;
               background: #fff; border-bottom: 1px solid #e0e0e5; flex-wrap: wrap; }}
  .controls input  {{ padding: 6px 10px; border: 1px solid #d2d2d7; border-radius: 6px;
                      font-size: 13px; width: 260px; }}
  .controls select {{ padding: 6px 8px; border: 1px solid #d2d2d7; border-radius: 6px;
                      font-size: 13px; background: #fff; }}
  .controls label  {{ font-size: 12px; color: #6e6e73; }}

  .table-wrap {{ overflow-x: auto; padding: 0 24px 24px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px;
           background: #fff; border-radius: 10px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th {{ background: #f5f5f7; padding: 9px 10px; text-align: left;
        font-size: 12px; font-weight: 600; color: #6e6e73;
        border-bottom: 1px solid #e0e0e5; white-space: nowrap;
        cursor: pointer; user-select: none; position: sticky; top: 0; z-index: 1; }}
  th:hover {{ background: #ebebed; }}
  th .sort-arrow {{ margin-left: 4px; opacity: 0.4; font-size: 10px; }}
  th.sort-asc  .sort-arrow {{ opacity: 1; }}
  th.sort-desc .sort-arrow {{ opacity: 1; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #f0f0f2; vertical-align: top;
        max-width: 320px; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fafafa; }}

  /* Row colour coding */
  tr.row-stored-emb td:first-child {{ border-left: 3px solid #34c759; }}
  tr.row-stored-scan td:first-child {{ border-left: 3px solid #ff9f0a; }}
  tr.row-url td:first-child  {{ border-left: 3px solid #636366; }}
  tr.row-none td:first-child {{ border-left: 3px solid #ff3b30; }}

  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 4px;
            font-size: 11px; font-weight: 500; white-space: nowrap; }}
  .b-stored    {{ background: #e8f8ed; color: #1a7f37; }}
  .b-url       {{ background: #f0f0f0; color: #444; }}
  .b-missing   {{ background: #fde8e8; color: #c0392b; }}
  .b-embedded  {{ background: #e8f0fe; color: #1a56db; }}
  .b-scanned   {{ background: #fff3e0; color: #b45309; }}
  .b-unknown   {{ background: #f0f0f0; color: #666; }}
  .b-accept    {{ background: #e8f8ed; color: #1a7f37; }}
  .b-arbitrate {{ background: #fff3e0; color: #b45309; }}
  .b-manual    {{ background: #fde8e8; color: #c0392b; }}

  .title-cell {{ max-width: 300px; }}
  .title-cell a {{ color: #0071e3; text-decoration: none; }}
  .title-cell a:hover {{ text-decoration: underline; }}
  .title-text {{ font-weight: 500; }}
  .title-pub  {{ font-style: italic; font-size: 11px; color: #555; margin-top: 2px; }}
  .title-meta {{ font-size: 11px; color: #999; margin-top: 1px; }}
  .key-mono {{ font-family: monospace; font-size: 11px; color: #bbb; margin-top: 2px; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .hidden {{ display: none; }}

  #count-label {{ font-size: 12px; color: #6e6e73; }}
</style>
</head>
<body>

<header>
  <div>
    <h1>Islamic Cartography — Corpus Dashboard</h1>
    <p>Generated {time.strftime('%Y-%m-%d %H:%M')}</p>
  </div>
  <nav class="site-nav">
    <a href="dashboard.html" class="active">📋 Dashboard</a>
    <a href="explore.html">🔭 Explorer</a>
  </nav>
</header>

<div class="stats">
  <div class="stat"><div class="val">{total}</div><div class="lbl">Total items</div></div>
  <div class="stat"><div class="val">{stored}</div><div class="lbl">In Zotero</div></div>
  <div class="stat"><div class="val">{downloaded}</div><div class="lbl">Downloaded</div></div>
  <div class="stat"><div class="val">{url_only}</div><div class="lbl">URL only</div></div>
  <div class="stat"><div class="val">{no_att}</div><div class="lbl">No attachment</div></div>
  <div class="stat"><div class="val">{embedded}</div><div class="lbl">Embedded fonts</div></div>
  <div class="stat"><div class="val">{scanned}</div><div class="lbl">Scanned</div></div>
  <div class="stat"><div class="val">{extracted}</div><div class="lbl">Text extracted</div></div>
</div>

<div class="controls">
  <input type="text" id="search" placeholder="Search title, author, key…" oninput="filterTable()">
  <label>PDF:
    <select id="filter-pdf" onchange="filterTable()">
      <option value="">All</option>
      <option value="stored">Stored (Zotero)</option>
      <option value="downloaded">Downloaded</option>
      <option value="url_only">URL only</option>
      <option value="no_attachment">No attachment</option>
      <option value="attachment_missing">Missing</option>
    </select>
  </label>
  <label>Type:
    <select id="filter-type" onchange="filterTable()">
      <option value="">All</option>
      <option value="embedded">Embedded</option>
      <option value="scanned">Scanned</option>
      <option value="unknown">Unknown</option>
    </select>
  </label>
  <label>Lang:
    <select id="filter-lang" onchange="filterTable()">
      <option value="">All</option>
    </select>
  </label>
  <label>Extracted:
    <select id="filter-ext" onchange="filterTable()">
      <option value="">All</option>
      <option value="yes">Yes</option>
      <option value="no">No</option>
    </select>
  </label>
  <span id="count-label"></span>
</div>

<div class="table-wrap">
<table id="main-table">
<thead>
<tr>
  <th data-col="0" onclick="sortTable(0)">#<span class="sort-arrow">↕</span></th>
  <th data-col="1" onclick="sortTable(1)">Key<span class="sort-arrow">↕</span></th>
  <th data-col="2" onclick="sortTable(2)">Title<span class="sort-arrow">↕</span></th>
  <th data-col="3" onclick="sortTable(3)">Year<span class="sort-arrow">↕</span></th>
  <th data-col="4" onclick="sortTable(4)">Authors<span class="sort-arrow">↕</span></th>
  <th data-col="5" onclick="sortTable(5)">Publication<span class="sort-arrow">↕</span></th>
  <th data-col="6" onclick="sortTable(6)">Item Type<span class="sort-arrow">↕</span></th>
  <th data-col="7" onclick="sortTable(7)">PDF Status<span class="sort-arrow">↕</span></th>
  <th data-col="8" onclick="sortTable(8)">Doc Type<span class="sort-arrow">↕</span></th>
  <th data-col="9" onclick="sortTable(9)">Pages<span class="sort-arrow">↕</span></th>
  <th data-col="10" onclick="sortTable(10)">Lang<span class="sort-arrow">↕</span></th>
  <th data-col="11" onclick="sortTable(11)">Quality<span class="sort-arrow">↕</span></th>
  <th data-col="12" onclick="sortTable(12)">Result<span class="sort-arrow">↕</span></th>
</tr>
</thead>
<tbody id="tbody"></tbody>
</table>
</div>

<script>
const DATA = {data_json};

// ── Rendering ─────────────────────────────────────────────────────────────────

function pdfBadge(s) {{
  if (s === 'stored')             return '<span class="badge b-stored">✓ Stored</span>';
  if (s === 'downloaded')         return '<span class="badge b-stored">⬇ Downloaded</span>';
  if (s === 'url_only')           return '<span class="badge b-url">URL only</span>';
  if (s === 'no_attachment')      return '<span class="badge b-missing">None</span>';
  if (s === 'attachment_missing') return '<span class="badge b-missing">Missing</span>';
  return '<span class="badge b-unknown">' + s + '</span>';
}}

function typeBadge(s) {{
  if (s === 'embedded') return '<span class="badge b-embedded">Embedded</span>';
  if (s === 'scanned')  return '<span class="badge b-scanned">Scanned</span>';
  return '<span class="badge b-unknown">—</span>';
}}

function recBadge(s) {{
  if (!s) return '';
  if (s === 'auto_accept') return '<span class="badge b-accept">Auto ✓</span>';
  if (s === 'arbitrate')   return '<span class="badge b-arbitrate">Arbitrate</span>';
  return '<span class="badge b-manual">' + s + '</span>';
}}

function rowClass(r) {{
  if (['stored','downloaded'].includes(r.pdf_status) && r.doc_type === 'embedded') return 'row-stored-emb';
  if (['stored','downloaded'].includes(r.pdf_status) && r.doc_type === 'scanned')  return 'row-stored-scan';
  if (r.pdf_status === 'url_only') return 'row-url';
  return 'row-none';
}}

function titleCell(r) {{
  const key     = '<div class="key-mono">' + r.key + '</div>';
  const titleEl = '<div class="title-text">' + escHtml(r.title) + '</div>';
  const pubEl   = r.pub_title ? '<div class="title-pub">' + escHtml(r.pub_title) + '</div>' : '';
  const metaParts = [r.authors, r.year, r.place].filter(Boolean);
  const metaEl  = metaParts.length ? '<div class="title-meta">' + escHtml(metaParts.join(' · ')) + '</div>' : '';
  if (r.url) {{
    return '<div class="title-cell"><a href="' + escHtml(r.url) + '" target="_blank">' + titleEl + '</a>' + pubEl + metaEl + key + '</div>';
  }}
  return '<div class="title-cell">' + titleEl + pubEl + metaEl + key + '</div>';
}}

function escHtml(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function renderRow(r, idx) {{
  const q = r.quality_score !== null && r.quality_score !== undefined
            ? r.quality_score.toFixed(2) : '—';
  return `<tr class="${{rowClass(r)}}" data-idx="${{idx}}">
    <td class="num">${{idx+1}}</td>
    <td class="key-mono">${{escHtml(r.key)}}</td>
    <td>${{titleCell(r)}}</td>
    <td>${{escHtml(r.year)}}</td>
    <td>${{escHtml(r.authors)}}</td>
    <td style="max-width:200px;font-style:italic;color:#444">${{escHtml(r.pub_title||'—')}}</td>
    <td>${{escHtml(r.item_type)}}</td>
    <td>${{pdfBadge(r.pdf_status)}}</td>
    <td>${{typeBadge(r.doc_type)}}</td>
    <td class="num">${{r.page_count !== null && r.page_count !== undefined ? r.page_count : '—'}}</td>
    <td>${{escHtml(r.language||'—')}}</td>
    <td class="num">${{q}}</td>
    <td>${{recBadge(r.recommendation)}}</td>
  </tr>`;
}}

// ── Populate language filter ──────────────────────────────────────────────────
(function() {{
  const langs = [...new Set(DATA.map(r => r.language).filter(Boolean))].sort();
  const sel   = document.getElementById('filter-lang');
  langs.forEach(l => {{
    const o = document.createElement('option');
    o.value = l; o.textContent = l;
    sel.appendChild(o);
  }});
}})();

// ── Filter & render ───────────────────────────────────────────────────────────
let currentData = [...DATA];
let sortCol     = -1;
let sortAsc     = true;

function filterTable() {{
  const q    = document.getElementById('search').value.toLowerCase();
  const pdf  = document.getElementById('filter-pdf').value;
  const type = document.getElementById('filter-type').value;
  const lang = document.getElementById('filter-lang').value;
  const ext  = document.getElementById('filter-ext').value;

  currentData = DATA.filter(r => {{
    if (q && !(
      (r.title  ||'').toLowerCase().includes(q) ||
      (r.authors||'').toLowerCase().includes(q) ||
      (r.key    ||'').toLowerCase().includes(q)
    )) return false;
    if (pdf  && r.pdf_status !== pdf)  return false;
    if (type && r.doc_type   !== type) return false;
    if (lang && r.language   !== lang) return false;
    if (ext  === 'yes' && !r.extracted) return false;
    if (ext  === 'no'  &&  r.extracted) return false;
    return true;
  }});

  if (sortCol >= 0) applySort();
  else renderTable();
}}

function renderTable() {{
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = currentData.map((r, i) => renderRow(r, i)).join('');
  document.getElementById('count-label').textContent =
    currentData.length + ' of ' + DATA.length + ' items';
}}

// ── Sort ──────────────────────────────────────────────────────────────────────
const COL_KEYS = ['_idx','key','title','year','authors','pub_title','item_type',
                  'pdf_status','doc_type','page_count','language','quality_score','recommendation'];

function sortTable(col) {{
  if (sortCol === col) {{ sortAsc = !sortAsc; }}
  else {{ sortCol = col; sortAsc = true; }}

  document.querySelectorAll('th').forEach((th, i) => {{
    th.classList.remove('sort-asc','sort-desc');
    if (i === col) th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
    const arr = th.querySelector('.sort-arrow');
    if (arr) arr.textContent = i === col ? (sortAsc ? '↑' : '↓') : '↕';
  }});
  applySort();
}}

function applySort() {{
  const key = COL_KEYS[sortCol];
  currentData.sort((a, b) => {{
    let va = key === '_idx' ? DATA.indexOf(a) : (a[key] ?? '');
    let vb = key === '_idx' ? DATA.indexOf(b) : (b[key] ?? '');
    if (va === null || va === undefined || va === '') va = sortAsc ? Infinity : -Infinity;
    if (vb === null || vb === undefined || vb === '') vb = sortAsc ? Infinity : -Infinity;
    if (typeof va === 'number' && typeof vb === 'number') return sortAsc ? va - vb : vb - va;
    return sortAsc
      ? String(va).localeCompare(String(vb))
      : String(vb).localeCompare(String(va));
  }});
  renderTable();
}}

// Initial render
filterTable();
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',      default='data/inventory.json')
    parser.add_argument('--dashboard',   default='data/dashboard.html')
    parser.add_argument('--no-classify', action='store_true',
                        help='Skip PDF classification (fast mode — no doc_type/language)')
    args = parser.parse_args()

    inv_path  = _ROOT / args.output
    dash_path = _ROOT / args.dashboard

    classify = not args.no_classify
    if not _PDFIUM and classify:
        print("⚠ pypdfium2 not available — running in fast mode (no PDF classification)")
        classify = False

    inventory = build_inventory(classify=classify)

    # Save JSON
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(inv_path, 'w', encoding='utf-8') as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)
    print(f"✓ Inventory saved → {inv_path}")

    # Save HTML dashboard
    html = build_dashboard(inventory)
    dash_path.write_text(html, encoding='utf-8')
    print(f"✓ Dashboard saved → {dash_path}")

    # Quick summary
    total    = len(inventory)
    stored   = sum(1 for r in inventory if r['pdf_status'] == 'stored')
    url_only = sum(1 for r in inventory if r['pdf_status'] == 'url_only')
    embedded = sum(1 for r in inventory if r['doc_type'] == 'embedded')
    scanned  = sum(1 for r in inventory if r['doc_type'] == 'scanned')

    print(f"\nSummary:")
    print(f"  Total items:    {total}")
    print(f"  PDF stored:     {stored}")
    print(f"  URL only:       {url_only}")
    print(f"  No attachment:  {total - stored - url_only}")
    print(f"  Embedded fonts: {embedded}")
    print(f"  Scanned:        {scanned}")
    print(f"  Unknown:        {total - embedded - scanned}")


if __name__ == '__main__':
    main()
