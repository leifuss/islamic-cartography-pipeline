#!/usr/bin/env python3
"""
Discover all Zotero libraries and collections accessible with the given credentials.

Connects to the Zotero web API (requires ZOTERO_API_KEY) and enumerates:
  1. The user's personal library and its collections
  2. All group libraries the user is a member of, and their collections

Writes the results to data/discovered_collections.json so the user can
select which collections to process with select_collections.py.

Usage:
    python scripts/discover_collections.py                 # discover everything
    python scripts/discover_collections.py --dry-run       # print without saving
    python scripts/discover_collections.py --output out.json

Environment:
    ZOTERO_API_KEY    — required (create at https://www.zotero.org/settings/keys)
    ZOTERO_USER_ID    — optional; auto-detected via /users/me if omitted
"""

import sys
import os
import json
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env', override=True)
except ImportError:
    pass

try:
    from pyzotero import zotero as pyzotero_module
except ImportError:
    print("ERROR: pyzotero not installed.  Run: pip install pyzotero")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_id(api_key: str) -> str:
    """Resolve the numeric user ID from an API key via GET /keys/<key>."""
    import urllib.request
    import urllib.error

    url = f"https://api.zotero.org/keys/{api_key}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return str(data['userID'])
    except (urllib.error.HTTPError, KeyError) as e:
        raise RuntimeError(
            f"Could not resolve user ID from API key: {e}\n"
            "Set ZOTERO_USER_ID in .env or pass --user-id."
        )


def _build_tree(collections: list) -> list:
    """
    Build a nested tree from a flat list of Zotero collection dicts.

    Each node gets a 'children' list.  Top-level collections have
    parentCollection == False (pyzotero convention).
    """
    by_key = {}
    for c in collections:
        d = c.get('data', c)
        by_key[d['key']] = {
            'key':         d['key'],
            'name':        d.get('name', ''),
            'parentKey':   d.get('parentCollection') or None,
            'numItems':    c.get('meta', {}).get('numItems', d.get('numItems', 0)),
            'children':    [],
        }

    roots = []
    for node in by_key.values():
        parent_key = node['parentKey']
        if parent_key and parent_key in by_key:
            by_key[parent_key]['children'].append(node)
        else:
            roots.append(node)

    # Sort children alphabetically at every level
    def _sort(nodes):
        nodes.sort(key=lambda n: n['name'].lower())
        for n in nodes:
            _sort(n['children'])
    _sort(roots)
    return roots


def _flatten_tree(tree: list, depth: int = 0) -> list:
    """Flatten a nested tree into an indented list for display / selection."""
    result = []
    for node in tree:
        result.append({**node, '_depth': depth})
        result.extend(_flatten_tree(node['children'], depth + 1))
    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover(api_key: str, user_id: str) -> dict:
    """
    Discover all accessible libraries and collections.

    Returns a dict:
        {
            "user_id": "12345",
            "libraries": [
                {
                    "type": "user",
                    "id": "12345",
                    "name": "My Library",
                    "num_items": 1234,
                    "collections": [ ... tree ... ]
                },
                {
                    "type": "group",
                    "id": "5166884",
                    "name": "Islamic Cartography",
                    "num_items": 42,
                    "collections": [ ... ]
                },
                ...
            ]
        }
    """
    libraries = []

    # --- 1. User library ---------------------------------------------------
    log.info(f"Discovering user library (ID {user_id})...")
    try:
        uz = pyzotero_module.Zotero(user_id, 'user', api_key)
        # Get collection tree
        user_collections = uz.all_collections()
        tree = _build_tree(user_collections)
        # Item count
        try:
            # pyzotero's count_items or just len of top-level
            items = uz.top(limit=1)
            total_items = int(uz.request.headers.get('Total-Results', 0)) if hasattr(uz, 'request') else 0
        except Exception:
            total_items = 0

        libraries.append({
            'type': 'user',
            'id': user_id,
            'name': 'My Library',
            'num_items': total_items,
            'collections': tree,
        })
        flat = _flatten_tree(tree)
        log.info(f"  User library: {len(flat)} collections, ~{total_items} items")
    except Exception as e:
        log.warning(f"  Could not read user library: {e}")

    # --- 2. Group libraries ------------------------------------------------
    log.info("Discovering group memberships...")
    try:
        # Fetch groups via the API
        import urllib.request
        url = f"https://api.zotero.org/users/{user_id}/groups"
        req = urllib.request.Request(url, headers={
            'Zotero-API-Key': api_key,
            'Zotero-API-Version': '3',
        })
        with urllib.request.urlopen(req) as resp:
            groups = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"  Could not fetch groups: {e}")
        groups = []

    log.info(f"  Found {len(groups)} group(s)")

    for group in groups:
        gdata = group.get('data', group)
        gid = str(gdata.get('id', ''))
        gname = gdata.get('name', f'Group {gid}')
        log.info(f"  Scanning group: {gname} (ID {gid})...")

        try:
            gz = pyzotero_module.Zotero(gid, 'group', api_key)
            group_collections = gz.all_collections()
            gtree = _build_tree(group_collections)

            # Item count
            try:
                gz.top(limit=1)
                g_items = int(gz.request.headers.get('Total-Results', 0)) if hasattr(gz, 'request') else 0
            except Exception:
                g_items = 0

            libraries.append({
                'type': 'group',
                'id': gid,
                'name': gname,
                'num_items': g_items,
                'collections': gtree,
            })
            flat = _flatten_tree(gtree)
            log.info(f"    {len(flat)} collections, ~{g_items} items")
        except Exception as e:
            log.warning(f"    Could not read group {gid}: {e}")

    return {
        'user_id': user_id,
        'libraries': libraries,
    }


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def print_discovery(result: dict) -> None:
    """Print discovered libraries and collections in a human-readable tree."""
    libs = result.get('libraries', [])
    if not libs:
        print("No libraries found.")
        return

    total_colls = 0
    for lib in libs:
        label = lib['name']
        if lib['type'] == 'group':
            label += f"  [group {lib['id']}]"
        else:
            label += f"  [user {lib['id']}]"
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"  ~{lib['num_items']} items")
        print(f"{'='*60}")

        flat = _flatten_tree(lib['collections'])
        total_colls += len(flat)

        if not flat:
            print("  (no collections — all items are at the library root)")
        for node in flat:
            indent = '  ' + '    ' * node['_depth']
            items_label = f"  ({node['numItems']} items)" if node['numItems'] else ''
            print(f"{indent}{node['name']}{items_label}")

    print(f"\nTotal: {len(libs)} libraries, {total_colls} collections")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Discover all Zotero libraries and collections')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print results without saving to file')
    parser.add_argument('--output', default='data/discovered_collections.json',
                        help='Output path (relative to project root)')
    parser.add_argument('--user-id', default=None,
                        help='Zotero user ID (auto-detected if omitted)')
    args = parser.parse_args()

    api_key = os.getenv('ZOTERO_API_KEY')
    if not api_key:
        print("ERROR: ZOTERO_API_KEY is required.")
        print("  Create one at: https://www.zotero.org/settings/keys")
        print("  Add to .env:   ZOTERO_API_KEY=your_key_here")
        sys.exit(1)

    user_id = args.user_id or os.getenv('ZOTERO_USER_ID')
    if not user_id:
        log.info("No ZOTERO_USER_ID set — resolving from API key...")
        user_id = _get_user_id(api_key)
        log.info(f"  Resolved user ID: {user_id}")

    result = discover(api_key, user_id)
    print_discovery(result)

    if not args.dry_run:
        out_path = _ROOT / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        print(f"\nSaved to {out_path}")
        print(f"Next step: python scripts/select_collections.py")


if __name__ == '__main__':
    main()
