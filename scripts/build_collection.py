#!/usr/bin/env python3
"""
Build one or more collections end-to-end.

For each selected collection (from data/collections.json), this script:

  Phase 1 — "Readerless" (fast, no PDFs needed):
    1. Sync metadata from Zotero → collections/<slug>/inventory.json
    2. Generate explore.html (embedded data, works standalone)
    3. Update collections.json status to 'built'
    4. The frontend (index.html) now shows a clickable card for this collection

  Phase 2 — "Readers" (incremental, needs PDFs + extraction):
    5. Stage PDFs → collections/<slug>/pdfs/{KEY}.pdf
    6. Extract text → collections/<slug>/texts/{KEY}/...
    7. Generate reader metadata → reader_meta.json per doc
    8. Update collections.json status to 'readers_ready'

The two phases are decoupled: Phase 1 runs in seconds and gives you a
browsable dashboard immediately.  Phase 2 can run overnight.

Usage:
    python scripts/build_collection.py --slug islamic-cartography    # build one
    python scripts/build_collection.py --all                          # build all pending
    python scripts/build_collection.py --all --phase 1               # metadata only
    python scripts/build_collection.py --slug my-coll --phase 2      # readers only

Environment:
    ZOTERO_API_KEY    — required for web API sync
    ZOTERO_USER_ID    — optional (auto-detected)
"""

import sys
import os
import json
import re
import argparse
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env', override=True)
except ImportError:
    pass

sys.path.insert(0, str(_ROOT / 'src'))


# ---------------------------------------------------------------------------
# HTML stripping (from zotero_sync.py)
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html or '')
    for ent, ch in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                    ('&nbsp;', ' '), ('&#160;', ' '), ('&quot;', '"'), ('&#39;', "'")]:
        text = text.replace(ent, ch)
    return re.sub(r'\s+', ' ', text).strip()


# ---------------------------------------------------------------------------
# Phase 1: Metadata sync + explorer generation
# ---------------------------------------------------------------------------

def _item_to_entry(item: dict) -> dict:
    """Convert a raw pyzotero item dict to an inventory entry."""
    data = item.get('data', {})
    creators = data.get('creators', [])
    authors = '; '.join(
        c.get('lastName', c.get('name', ''))
        for c in creators
        if c.get('creatorType') in ('author', 'editor')
    )[:80]

    date = data.get('date', '')
    year = date[:4] if date else ''

    pub_title = (
        data.get('bookTitle', '') or
        data.get('publicationTitle', '') or
        data.get('proceedingsTitle', '') or
        data.get('encyclopediaTitle', '') or
        data.get('university', '') or
        data.get('institution', '') or
        ''
    )

    tags = [t.get('tag', '') for t in data.get('tags', []) if t.get('tag')]

    return {
        'key':          item.get('key', ''),
        'title':        data.get('title', 'Untitled'),
        'year':         year,
        'authors':      authors,
        'item_type':    data.get('itemType', ''),
        'place':        data.get('place', ''),
        'publisher':    data.get('publisher', ''),
        'abstract':     data.get('abstractNote', '') or None,
        'pub_title':    pub_title or None,
        'pages':        data.get('pages', '') or None,
        'url':          data.get('url', ''),
        'tags':         tags,
        'notes':        [],
        'pdf_status':   'url_only' if data.get('url') else 'no_attachment',
        'pdf_path':     None,
        'doc_type':     'unknown',
        'page_count':   None,
        'avg_chars_pg': None,
        'language':     None,
        'extracted':    False,
        'quality_score': None,
        'recommendation': '',
        'has_reader':   False,
        'text_quality': None,
        'chars_per_page': None,
    }


def sync_collection_metadata(coll_entry: dict, api_key: str) -> list:
    """
    Sync metadata from Zotero for a single collection.

    Returns the inventory list.
    """
    from pyzotero import zotero as pyzotero_module

    source = coll_entry.get('source', {})
    lib_type = source.get('library_type', 'user')
    lib_id = source.get('library_id')
    coll_key = source.get('collection_key')
    coll_name = source.get('collection_name')

    log.info(f"  Connecting to Zotero ({lib_type} library {lib_id})...")
    zot = pyzotero_module.Zotero(lib_id, lib_type, api_key)

    # Fetch items
    if coll_key:
        log.info(f"  Fetching collection '{coll_name}' ({coll_key})...")
        all_items = zot.everything(zot.collection_items(coll_key))
    else:
        log.info(f"  Fetching all items from library root...")
        all_items = zot.everything(zot.items())

    # Separate parents and children
    top_items = []
    children_by_parent = {}
    for item in all_items:
        data = item.get('data', {})
        if data.get('itemType') in ('attachment', 'note', 'linkMode'):
            parent = data.get('parentItem')
            if parent:
                children_by_parent.setdefault(parent, []).append(item)
            continue
        parent = data.get('parentItem')
        if parent:
            children_by_parent.setdefault(parent, []).append(item)
        else:
            top_items.append(item)

    log.info(f"  {len(top_items)} top-level items, {sum(len(v) for v in children_by_parent.values())} children")

    # Extract notes
    notes_by_key = {}
    for parent_key, children in children_by_parent.items():
        item_notes = []
        for child in children:
            if child['data'].get('itemType') == 'note':
                note_text = _strip_html(child['data'].get('note', ''))
                if note_text:
                    item_notes.append(note_text)
        if item_notes:
            notes_by_key[parent_key] = item_notes

    # Build inventory
    slug = coll_entry['slug']
    data_dir = _ROOT / 'data' / 'collections' / slug
    data_dir.mkdir(parents=True, exist_ok=True)

    inv_path = data_dir / 'inventory.json'
    existing = []
    if inv_path.exists():
        existing = json.loads(inv_path.read_text(encoding='utf-8'))
    existing_by_key = {e['key']: e for e in existing}

    # Fields that Zotero owns vs pipeline owns
    ZOTERO_FIELDS = {
        'title', 'year', 'authors', 'item_type', 'place', 'publisher',
        'abstract', 'pub_title', 'pages', 'url', 'tags', 'notes',
    }
    PIPELINE_FIELDS = {
        'pdf_status', 'pdf_path', 'doc_type', 'page_count', 'avg_chars_pg',
        'language', 'extracted', 'quality_score', 'recommendation',
        'has_reader', 'text_quality', 'chars_per_page',
    }

    added = 0
    updated = 0
    for item in top_items:
        entry = _item_to_entry(item)
        key = entry['key']
        if not key:
            continue
        entry['notes'] = notes_by_key.get(key, [])

        if key in existing_by_key:
            old = existing_by_key[key]
            merged = dict(old)
            changed = False
            for field in ZOTERO_FIELDS:
                new_val = entry.get(field)
                old_val = old.get(field)
                if new_val != old_val:
                    merged[field] = new_val
                    changed = True
            existing_by_key[key] = merged
            if changed:
                updated += 1
        else:
            existing_by_key[key] = entry
            added += 1

    # Rebuild list
    seen = set()
    result = []
    for e in existing:
        if e['key'] in existing_by_key:
            result.append(existing_by_key[e['key']])
            seen.add(e['key'])
    for item in top_items:
        key = item.get('key', '')
        if key and key not in seen and key in existing_by_key:
            result.append(existing_by_key[key])
            seen.add(key)

    inv_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    log.info(f"  inventory.json: {len(result)} items (+{added} new, ~{updated} updated)")

    return result


def generate_collection_explorer(coll_entry: dict, inventory: list) -> None:
    """
    Generate a standalone explore.html for a collection.

    Delegates to the existing generate_explore.py logic if importable,
    otherwise writes a minimal version that loads inventory.json at runtime.
    """
    slug = coll_entry['slug']
    data_dir = _ROOT / 'data' / 'collections' / slug

    # Write a corpus_config.json for this collection
    config = {
        'name': coll_entry['name'],
        'description': coll_entry.get('description', ''),
        'texts_dir': 'texts/',
        'nav': {
            'dashboard': {'label': 'Dashboard', 'href': f'../../explore.html?collection={slug}'},
            'explorer':  {'label': 'Explorer',  'href': f'../../explore.html?collection={slug}'},
        },
        'source': coll_entry.get('source', {}),
    }
    config_path = data_dir / 'corpus_config.json'
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')
    log.info(f"  Wrote {config_path}")

    # Try to use the existing explore generator
    try:
        sys.path.insert(0, str(_ROOT / 'scripts'))
        from generate_explore import build_html
        html = build_html(inventory, data_dir=data_dir)
        explore_path = data_dir / 'explore.html'
        explore_path.write_text(html, encoding='utf-8')
        log.info(f"  Wrote {explore_path} ({explore_path.stat().st_size // 1024} KB)")
    except Exception as e:
        log.warning(f"  Could not generate explore.html via generate_explore.py: {e}")
        log.info("  The collection is still browsable via the shared explore.html?collection= URL")


def build_phase1(coll_entry: dict, api_key: str) -> None:
    """
    Phase 1: Sync metadata + generate readerless explorer view.
    """
    slug = coll_entry['slug']
    name = coll_entry['name']
    log.info(f"\n{'='*60}")
    log.info(f"Phase 1: {name} ({slug})")
    log.info(f"{'='*60}")

    # 1. Sync metadata
    inventory = sync_collection_metadata(coll_entry, api_key)

    # 2. Generate explorer
    generate_collection_explorer(coll_entry, inventory)

    # 3. Update status
    coll_entry['status'] = 'built'
    coll_entry['num_items'] = len(inventory)
    coll_entry['built_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')

    log.info(f"  Phase 1 complete: {len(inventory)} items, browsable at explore.html?collection={slug}")


# ---------------------------------------------------------------------------
# Phase 2: PDF staging + extraction + reader metadata (incremental)
# ---------------------------------------------------------------------------

def build_phase2(coll_entry: dict, api_key: str) -> None:
    """
    Phase 2: Stage PDFs, extract text, generate reader metadata.

    This is the slow, incremental phase.  It reuses the existing pipeline
    scripts by setting environment variables and calling them per-collection.
    """
    slug = coll_entry['slug']
    name = coll_entry['name']
    data_dir = _ROOT / 'data' / 'collections' / slug

    log.info(f"\n{'='*60}")
    log.info(f"Phase 2 (readers): {name} ({slug})")
    log.info(f"{'='*60}")

    inv_path = data_dir / 'inventory.json'
    if not inv_path.exists():
        log.error(f"  No inventory.json — run Phase 1 first.")
        return

    inventory = json.loads(inv_path.read_text(encoding='utf-8'))

    # Create directories
    (data_dir / 'pdfs').mkdir(exist_ok=True)
    (data_dir / 'texts').mkdir(exist_ok=True)

    # For now, Phase 2 prints guidance on how to run extraction.
    # Full automation would call 00_stage_pdfs.py and 05b_extract_robust.py
    # with overridden paths.  That requires those scripts to accept
    # --data-dir parameters, which is a follow-up enhancement.

    extractable = [r for r in inventory if r.get('pdf_status') in ('stored', 'downloaded')]
    already_done = [r for r in inventory if r.get('extracted')]

    log.info(f"  {len(inventory)} total items")
    log.info(f"  {len(extractable)} with PDFs available for extraction")
    log.info(f"  {len(already_done)} already extracted")

    remaining = len(extractable) - len(already_done)
    if remaining <= 0:
        log.info(f"  All extractable items are done (or no PDFs available).")
        coll_entry['status'] = 'readers_ready'
    else:
        log.info(f"  {remaining} items remaining to extract.")
        log.info(f"")
        log.info(f"  To extract, run:")
        log.info(f"    COLLECTION_DATA_DIR=data/collections/{slug} make extract")
        log.info(f"")
        log.info(f"  Or for individual keys:")
        keys = [r['key'] for r in extractable if not r.get('extracted')][:5]
        log.info(f"    python scripts/05b_extract_robust.py --data-dir data/collections/{slug} --keys {' '.join(keys)}")
        coll_entry['status'] = 'built'  # not yet readers_ready

    coll_entry['phase2_checked_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_collections() -> dict:
    path = _ROOT / 'data' / 'collections.json'
    if not path.exists():
        print("ERROR: data/collections.json not found.")
        print("  Run: python scripts/select_collections.py")
        sys.exit(1)
    return json.loads(path.read_text(encoding='utf-8'))


def save_collections(data: dict) -> None:
    path = _ROOT / 'data' / 'collections.json'
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )


def main():
    parser = argparse.ArgumentParser(
        description='Build collection(s) — sync metadata and generate explorer views')
    parser.add_argument('--slug', help='Build a specific collection by slug')
    parser.add_argument('--all', action='store_true',
                        help='Build all collections in collections.json')
    parser.add_argument('--phase', type=int, choices=[1, 2], default=None,
                        help='Run only Phase 1 (metadata) or Phase 2 (readers). Default: both.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be built without making changes')
    args = parser.parse_args()

    if not args.slug and not args.all:
        parser.error("Specify --slug <name> or --all")

    api_key = os.getenv('ZOTERO_API_KEY')
    if not api_key:
        print("ERROR: ZOTERO_API_KEY required. Add to .env.")
        sys.exit(1)

    data = load_collections()
    collections = data.get('collections', [])

    if args.slug:
        targets = [c for c in collections if c['slug'] == args.slug]
        if not targets:
            available = [c['slug'] for c in collections]
            print(f"ERROR: Collection '{args.slug}' not found in collections.json")
            print(f"  Available: {', '.join(available)}")
            sys.exit(1)
    else:
        targets = collections

    if args.dry_run:
        print(f"Would build {len(targets)} collection(s):")
        for c in targets:
            src = c.get('source', {})
            print(f"  - {c['name']} ({c['slug']}) [{src.get('library_type')} {src.get('library_id')}] status={c.get('status')}")
        return

    run_phase1 = args.phase is None or args.phase == 1
    run_phase2 = args.phase is None or args.phase == 2

    for coll in targets:
        if run_phase1:
            build_phase1(coll, api_key)
        if run_phase2:
            build_phase2(coll, api_key)

    # Save updated statuses back to collections.json
    save_collections(data)
    log.info(f"\nDone. Updated collections.json with {len(targets)} collection(s).")


if __name__ == '__main__':
    main()
