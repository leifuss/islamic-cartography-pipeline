#!/usr/bin/env python3
"""
Interactively select which Zotero collections to process.

Reads data/discovered_collections.json (produced by discover_collections.py)
and lets the user pick collections.  The selection is written into
data/collections.json so the frontend (index.html → explore.html) can show
each collection as a separate browsable corpus.

Usage:
    python scripts/select_collections.py                # interactive picker
    python scripts/select_collections.py --list         # just list available
    python scripts/select_collections.py --add G:5166884:ABCD1234   # non-interactive
    python scripts/select_collections.py --add U::KEY1 --add G:999:KEY2

Selector syntax for --add:
    U::<collection_key>          user library, specific collection
    U::                          user library, root (all items)
    G:<group_id>:<collection_key>  group library, specific collection
    G:<group_id>:                group library, root (all items)
"""

import sys
import os
import re
import json
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent


def _slugify(name: str) -> str:
    """Convert a name to a URL-safe slug."""
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')[:60]


def _flatten_tree(tree: list, depth: int = 0) -> list:
    result = []
    for node in tree:
        result.append({**node, '_depth': depth})
        if 'children' in node:
            result.extend(_flatten_tree(node['children'], depth + 1))
    return result


def load_discovered() -> dict:
    """Load discovered_collections.json."""
    path = _ROOT / 'data' / 'discovered_collections.json'
    if not path.exists():
        print("ERROR: data/discovered_collections.json not found.")
        print("  Run first: python scripts/discover_collections.py")
        sys.exit(1)
    return json.loads(path.read_text(encoding='utf-8'))


def load_current_selections() -> dict:
    """Load existing collections.json."""
    path = _ROOT / 'data' / 'collections.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'default': None, 'collections': []}


def save_selections(data: dict) -> None:
    """Write collections.json."""
    path = _ROOT / 'data' / 'collections.json'
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    log.info(f"Saved {path} ({len(data.get('collections', []))} collections)")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def list_all(discovered: dict) -> list:
    """
    Print all discovered libraries/collections as a numbered menu.

    Returns a flat list of selectable items, each with metadata for
    building a collection entry.
    """
    items = []
    idx = 1

    for lib in discovered.get('libraries', []):
        lib_type = lib['type']
        lib_id = lib['id']
        lib_name = lib['name']

        # Option: the library root (all items, no collection filter)
        label = f"[{lib_type.upper()} {lib_id}] {lib_name} (root — all items)"
        items.append({
            'idx': idx,
            'label': label,
            'lib_type': lib_type,
            'lib_id': lib_id,
            'lib_name': lib_name,
            'collection_key': None,
            'collection_name': None,
            'num_items': lib.get('num_items', 0),
        })
        print(f"  {idx:>3}. {label}")
        idx += 1

        # Each collection in this library
        flat = _flatten_tree(lib.get('collections', []))
        for node in flat:
            indent = '    ' * (node['_depth'] + 1)
            items_label = f" ({node['numItems']} items)" if node.get('numItems') else ''
            label = f"[{lib_type.upper()} {lib_id}] {indent}{node['name']}{items_label}"
            items.append({
                'idx': idx,
                'label': label,
                'lib_type': lib_type,
                'lib_id': lib_id,
                'lib_name': lib_name,
                'collection_key': node['key'],
                'collection_name': node['name'],
                'num_items': node.get('numItems', 0),
            })
            print(f"  {idx:>3}. {label}")
            idx += 1

    return items


def build_collection_entry(item: dict, existing_slugs: set) -> dict:
    """
    Build a collections.json entry from a selection menu item.
    """
    # Build a readable name
    if item['collection_name']:
        name = item['collection_name']
    else:
        name = item['lib_name']

    slug = _slugify(name)
    # Ensure uniqueness
    base_slug = slug
    counter = 2
    while slug in existing_slugs:
        slug = f"{base_slug}-{counter}"
        counter += 1

    # Build the data path: collections/<slug>/
    data_path = f"collections/{slug}"

    entry = {
        'slug': slug,
        'name': name,
        'description': '',
        'path': data_path,
        # Source metadata — needed by build_collection.py to know where to sync from
        'source': {
            'library_type': item['lib_type'],
            'library_id': item['lib_id'],
            'collection_key': item['collection_key'],
            'collection_name': item['collection_name'],
        },
        'num_items': item['num_items'],
        'status': 'pending',  # pending | synced | built | readers_ready
    }

    return entry


# ---------------------------------------------------------------------------
# Interactive selection
# ---------------------------------------------------------------------------

def interactive_select(discovered: dict) -> None:
    """Run the interactive selection loop."""
    current = load_current_selections()
    existing_slugs = {c['slug'] for c in current.get('collections', [])}

    print("\n=== Discovered Zotero Libraries & Collections ===\n")
    items = list_all(discovered)

    if not items:
        print("\nNo libraries or collections found.")
        return

    print(f"\n  Currently selected: {len(current.get('collections', []))} collection(s)")
    for c in current.get('collections', []):
        src = c.get('source', {})
        print(f"    - {c['name']} ({c['slug']}) [{src.get('library_type', '?')} {src.get('library_id', '?')}]")

    print("\n  Enter numbers to add (comma-separated), or 'q' to quit.")
    print("  Example: 1,3,7\n")

    try:
        raw = input("  Select> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return

    if raw.lower() in ('q', 'quit', ''):
        print("No changes made.")
        return

    # Parse selection
    try:
        indices = [int(x.strip()) for x in raw.split(',') if x.strip()]
    except ValueError:
        print("ERROR: Enter numbers separated by commas.")
        return

    items_by_idx = {it['idx']: it for it in items}
    added = []

    for idx in indices:
        if idx not in items_by_idx:
            print(f"  WARNING: #{idx} not found, skipping.")
            continue
        item = items_by_idx[idx]
        entry = build_collection_entry(item, existing_slugs)
        existing_slugs.add(entry['slug'])
        current.setdefault('collections', []).append(entry)
        added.append(entry)
        print(f"  + Added: {entry['name']} → {entry['path']}")

    if not added:
        print("No collections added.")
        return

    # Set default if none
    if not current.get('default') and current['collections']:
        current['default'] = current['collections'][0]['slug']

    save_selections(current)
    print(f"\nNext step: python scripts/build_collection.py --slug {added[0]['slug']}")
    print(f"  Or build all: python scripts/build_collection.py --all")


# ---------------------------------------------------------------------------
# Non-interactive --add
# ---------------------------------------------------------------------------

def add_by_selector(discovered: dict, selectors: list) -> None:
    """
    Add collections via CLI selectors (non-interactive).

    Selector syntax:
        U::<collection_key>       user library, specific collection
        U::                        user library root
        G:<group_id>:<coll_key>   group, specific collection
        G:<group_id>:             group root
    """
    current = load_current_selections()
    existing_slugs = {c['slug'] for c in current.get('collections', [])}

    # Build a lookup from discovered
    lib_lookup = {}
    for lib in discovered.get('libraries', []):
        key = (lib['type'], lib['id'])
        lib_lookup[key] = lib

    for sel in selectors:
        parts = sel.split(':')
        if len(parts) < 3:
            log.warning(f"Invalid selector '{sel}' — expected TYPE:LIB_ID:COLL_KEY")
            continue

        stype = parts[0].upper()
        lib_id = parts[1]
        coll_key = parts[2] if len(parts) > 2 else ''

        lib_type = 'user' if stype == 'U' else 'group'
        if lib_type == 'user' and not lib_id:
            # Auto-detect user library ID
            for lib in discovered.get('libraries', []):
                if lib['type'] == 'user':
                    lib_id = lib['id']
                    break

        lib = lib_lookup.get((lib_type, lib_id))
        if not lib:
            log.warning(f"Library {lib_type}/{lib_id} not found in discovery")
            continue

        # Find the collection name
        coll_name = None
        if coll_key:
            flat = _flatten_tree(lib.get('collections', []))
            for node in flat:
                if node['key'] == coll_key:
                    coll_name = node['name']
                    break
            if not coll_name:
                log.warning(f"Collection key {coll_key} not found in {lib['name']}")
                continue

        item = {
            'lib_type': lib_type,
            'lib_id': lib_id,
            'lib_name': lib['name'],
            'collection_key': coll_key or None,
            'collection_name': coll_name,
            'num_items': 0,
        }

        entry = build_collection_entry(item, existing_slugs)
        existing_slugs.add(entry['slug'])
        current.setdefault('collections', []).append(entry)
        log.info(f"Added: {entry['name']} → {entry['path']}")

    if not current.get('default') and current['collections']:
        current['default'] = current['collections'][0]['slug']

    save_selections(current)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Select Zotero collections to process')
    parser.add_argument('--list', action='store_true',
                        help='List discovered collections without selecting')
    parser.add_argument('--add', action='append', default=[],
                        help='Add collection by selector (non-interactive)')
    args = parser.parse_args()

    discovered = load_discovered()

    if args.list:
        print("\n=== Discovered Zotero Libraries & Collections ===\n")
        list_all(discovered)
        return

    if args.add:
        add_by_selector(discovered, args.add)
        return

    interactive_select(discovered)


if __name__ == '__main__':
    main()
