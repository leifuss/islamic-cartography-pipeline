#!/usr/bin/env python3
"""
Inventory all Zotero items and write data/inventory.json.

For each item:
  - PDF status: has_attachment / url_only / no_attachment
  - Doc type:   embedded / scanned / unknown  (if PDF already fetched)
  - Language detection from text sample
  - Cross-referenced with extraction results if available

All Zotero access is via the web API (no local desktop required).

Outputs:
  data/inventory.json   – raw inventory data
  (dashboard.html and explore.html are now static files that fetch
   inventory.json at runtime — no regeneration needed.)

Usage:
    python scripts/03_inventory.py
    python scripts/03_inventory.py --output data/inventory.json
    python scripts/03_inventory.py --no-classify   # skip PDF classification (fast mode)
"""
import sys
import json
import re
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
    from pypdfium2.raw import FPDF_PAGEOBJ_IMAGE
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


def _estimate_page_dpi(page) -> float | None:
    """Estimate effective DPI of images on a single PDF page.

    Compares each embedded image's pixel size to its bounding box on the page
    (in points, 72pt = 1 inch) and returns the median DPI across all images.
    Returns None if no images found.
    """
    if not _PDFIUM:
        return None
    dpis = []
    try:
        for obj in page.get_objects(filter=[FPDF_PAGEOBJ_IMAGE]):
            try:
                px_w, px_h = obj.get_size()
                left, bottom, right, top = obj.get_bounds()
                box_w_pts = abs(right - left)
                box_h_pts = abs(top - bottom)
                if box_w_pts > 0 and px_w > 0:
                    dpis.append(px_w / (box_w_pts / 72.0))
                if box_h_pts > 0 and px_h > 0:
                    dpis.append(px_h / (box_h_pts / 72.0))
            except Exception:
                continue
    except Exception:
        return None
    if not dpis:
        return None
    dpis.sort()
    return dpis[len(dpis) // 2]


def classify_pdf(path: Path) -> dict:
    """
    Classify a PDF as embedded-font or scanned, and detect language.
    Samples up to the first 5 pages only — fast enough for 300+ items.
    Also estimates the effective DPI of embedded images.
    """
    result = {
        'page_count':    None,
        'doc_type':      'unknown',   # embedded | scanned | unknown
        'avg_chars_page': None,
        'language':      None,
        'lang_sample':   None,
        'pdf_dpi':       None,
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

        # Estimate DPI from images on the first few sampled pages
        all_dpis = []
        for i in sample_indices[:3]:
            d = _estimate_page_dpi(doc[i])
            if d is not None:
                all_dpis.append(d)
        if all_dpis:
            all_dpis.sort()
            result['pdf_dpi'] = round(all_dpis[len(all_dpis) // 2])

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


# ── Attachment status via API ─────────────────────────────────────────────────

def get_attachment_status(library: ZoteroLibrary, item: dict,
                          children: list | None = None) -> dict:
    """
    Check attachment availability via the Zotero web API.

    Returns:
      { 'status': 'has_attachment'|'url_only'|'no_attachment',
        'attachment_key': str|None,
        'filename': str|None,
        'url': str|None,
        'notes': [str, ...]
      }
    """
    item_url = item.get('data', {}).get('url', '')

    # Collect note children
    notes = []
    for child in (children or []):
        if child['data'].get('itemType') == 'note':
            note_text = _strip_html(child['data'].get('note', ''))
            if note_text:
                notes.append(note_text)

    # Look for a PDF/image attachment in the pre-fetched children
    att_info = library.get_attachment_info(item.get('key', ''), children=children)
    if att_info:
        # Check if already fetched to data/pdfs/
        staged_path = _ROOT / 'data' / 'pdfs' / f"{item.get('key', '')}.pdf"
        status = 'stored' if staged_path.exists() else 'has_attachment'
        return {
            'status': status,
            'pdf_path': str(staged_path.relative_to(_ROOT)) if staged_path.exists() else None,
            'attachment_key': att_info['key'],
            'filename': att_info['filename'],
            'url': item_url or None,
            'notes': notes,
        }

    status = 'url_only' if item_url else 'no_attachment'
    return {
        'status': status,
        'pdf_path': None,
        'attachment_key': None,
        'filename': None,
        'url': item_url or None,
        'notes': notes,
    }


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
    print(f"Fetching items from Zotero web API ({library.library_type} library, "
          f"ID {library.library_id})...")

    # Fetch all items + children in one paginated call (no N+1)
    items, children_by_parent = library.get_all_items_with_children()
    print(f"  {len(items)} items, "
          f"{sum(len(v) for v in children_by_parent.values())} child objects\n")

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

        # Skip child items that leaked through
        if itype in ('attachment', 'note'):
            continue

        creators  = data.get('creators', [])
        authors   = '; '.join(
            c.get('lastName', c.get('name', ''))
            for c in creators
            if c.get('creatorType') in ('author', 'editor')
        )[:80]
        place     = data.get('place', '')
        publisher = data.get('publisher', '')
        abstract  = data.get('abstractNote', '')

        pub_title = (
            data.get('bookTitle', '')          or
            data.get('publicationTitle', '')   or
            data.get('proceedingsTitle', '')   or
            data.get('encyclopediaTitle', '')  or
            data.get('university', '')         or
            data.get('institution', '')        or
            ''
        )

        pages = data.get('pages', '')
        tags = [t.get('tag', '') for t in data.get('tags', []) if t.get('tag')]

        print(f"\r  [{idx:3d}/{n}] {title[:55]:<55}", end="", flush=True)

        children = children_by_parent.get(key, [])
        att = get_attachment_status(library, item, children=children)
        pdf_path = att['pdf_path']

        # Fall back to locally downloaded file if it exists
        if not pdf_path and key in downloads:
            dl_path = downloads[key].get('pdf_path')
            if dl_path and Path(dl_path).exists():
                pdf_path = dl_path
                att['status'] = 'downloaded'

        pdf_info = {}
        if classify and pdf_path and att['status'] in ('stored', 'downloaded'):
            pdf_info = classify_pdf(Path(pdf_path) if Path(pdf_path).is_absolute()
                                    else _ROOT / pdf_path)

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
            'pdf_dpi':     pdf_info.get('pdf_dpi'),
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




# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',      default='data/inventory.json')
    parser.add_argument('--no-classify', action='store_true',
                        help='Skip PDF classification (fast mode — no doc_type/language)')
    args = parser.parse_args()

    inv_path  = _ROOT / args.output

    classify = not args.no_classify
    if not _PDFIUM and classify:
        print("pypdfium2 not available — running in fast mode (no PDF classification)")
        classify = False

    inventory = build_inventory(classify=classify)

    # Save JSON
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(inv_path, 'w', encoding='utf-8') as f:
        json.dump(inventory, f, indent=2, ensure_ascii=False)
    print(f"Inventory saved -> {inv_path}")

    # Quick summary
    total    = len(inventory)
    stored   = sum(1 for r in inventory if r['pdf_status'] == 'stored')
    has_att  = sum(1 for r in inventory if r['pdf_status'] == 'has_attachment')
    url_only = sum(1 for r in inventory if r['pdf_status'] == 'url_only')
    embedded = sum(1 for r in inventory if r['doc_type'] == 'embedded')
    scanned  = sum(1 for r in inventory if r['doc_type'] == 'scanned')

    print(f"\nSummary:")
    print(f"  Total items:        {total}")
    print(f"  PDF fetched:        {stored}")
    print(f"  PDF in Zotero:      {has_att}  (run 'make fetch-pdfs' to download)")
    print(f"  URL only:           {url_only}")
    print(f"  No attachment:      {total - stored - has_att - url_only}")
    print(f"  Embedded fonts:     {embedded}")
    print(f"  Scanned:            {scanned}")
    print(f"  Unknown:            {total - embedded - scanned}")


if __name__ == '__main__':
    main()
