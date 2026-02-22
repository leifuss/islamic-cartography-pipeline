#!/usr/bin/env python3
"""
Sync inventory.json with what's actually on disk.

For each doc in inventory, checks data/texts/{key}/ and updates:
  extracted          — True if page_texts.json exists
  has_reader         — True if pages/001.jpg exists (rendered pages available)
  text_quality       — from meta.json if available
  page_count         — from meta.json if available (overrides inventory value)
  extraction_method  — 'google_vision', 'docling', or 'tesseract'; fallback to
                       'tesseract' for extracted items where meta.json has no method

Also generates reader_meta.json for any doc that has page images but is
missing the file (needed by the reader SPA for title/author display).

Safe to run repeatedly — only updates fields where disk state differs.
Fast: just file-existence checks, no network, no ML.

Usage:
    python scripts/00_update_inventory_flags.py
    python scripts/00_update_inventory_flags.py --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT            = Path(__file__).parent.parent
TEXTS_ROOT       = _ROOT / "data" / "texts"
INV_PATH         = _ROOT / "data" / "inventory.json"
COLLECTIONS_PATH = _ROOT / "data" / "collections.json"


def _get_collection_base(slug: str) -> Path:
    if not COLLECTIONS_PATH.exists():
        raise SystemExit("ERROR: data/collections.json not found")
    with open(COLLECTIONS_PATH, encoding='utf-8') as f:
        coll_data = json.load(f)
    for c in coll_data.get('collections', []):
        if c['slug'] == slug:
            path = c.get('path', slug)
            if path == '.':
                return _ROOT / "data"
            return _ROOT / "data" / path
    raise SystemExit(f"ERROR: collection slug {slug!r} not found in data/collections.json")


def check_doc(key: str, texts_root: Path = None) -> dict:
    """Return dict of flags to update in inventory for this key."""
    if texts_root is None:
        texts_root = TEXTS_ROOT
    doc_dir         = texts_root / key
    pt_path         = doc_dir / "page_texts.json"
    meta_path       = doc_dir / "meta.json"
    page1_path      = doc_dir / "pages" / "001.jpg"
    vision_bak_path = doc_dir / "page_texts.docling.json"

    flags: dict = {}

    extracted  = pt_path.exists()
    has_reader = page1_path.exists()
    flags["extracted"]  = extracted
    flags["has_reader"] = has_reader

    method = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            if meta.get("text_quality"):
                flags["text_quality"] = meta["text_quality"]
            if meta.get("chars_per_page") is not None:
                flags["chars_per_page"] = meta["chars_per_page"]
            if meta.get("page_count"):
                flags["page_count"] = meta["page_count"]
            method = meta.get("method")  # 'docling' or 'tesseract'
        except Exception:
            pass

    # Determine extraction method; Vision API leaves a backup of the original output
    if vision_bak_path.exists():
        flags["extraction_method"] = "google_vision"
    elif method:
        flags["extraction_method"] = method
    elif extracted:
        flags["extraction_method"] = "tesseract"  # default for pre-method items

    return flags


def ensure_reader_meta(key: str, inv_entry: dict, texts_root: Path = None) -> bool:
    """Generate reader_meta.json from inventory + meta.json if missing.

    Returns True if a new file was written.
    """
    if texts_root is None:
        texts_root = TEXTS_ROOT
    doc_dir = texts_root / key
    rm_path = doc_dir / "reader_meta.json"

    if rm_path.exists():
        return False

    # Only generate if we have page images (i.e. reader can display it)
    if not (doc_dir / "pages" / "001.jpg").exists():
        return False

    # Read extraction meta.json for any additional info
    meta_path = doc_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            pass

    # Count pages from images on disk
    pages_dir = doc_dir / "pages"
    page_count = len(sorted(pages_dir.glob("*.jpg"))) if pages_dir.is_dir() else 0
    if not page_count:
        page_count = meta.get("page_count", 0)

    reader_meta = {
        "key":        key,
        "title":      inv_entry.get("title") or meta.get("title", key),
        "authors":    inv_entry.get("authors") or meta.get("authors", ""),
        "year":       str(inv_entry.get("year") or meta.get("year", "") or ""),
        "publisher":  inv_entry.get("publisher", ""),
        "place":      inv_entry.get("place", ""),
        "item_type":  inv_entry.get("item_type", ""),
        "language":   inv_entry.get("language") or meta.get("language", ""),
        "page_count": page_count,
    }

    rm_path.write_text(
        json.dumps(reader_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inventory", default="data/inventory.json",
                        help="Inventory path (overridden by --collection-slug)")
    parser.add_argument("--collection-slug", default=None,
                        help="Collection slug from data/collections.json; "
                             "sets inventory and texts paths automatically")
    args = parser.parse_args()

    if args.collection_slug:
        base       = _get_collection_base(args.collection_slug)
        inv_path   = base / "inventory.json"
        texts_root = base / "texts"
        print(f"Collection: {args.collection_slug}  ({base})")
    else:
        inv_path   = _ROOT / args.inventory
        texts_root = TEXTS_ROOT

    inventory = json.loads(inv_path.read_text("utf-8"))

    changed   = 0
    meta_gen  = 0
    for item in inventory:
        key   = item["key"]
        flags = check_doc(key, texts_root)
        for k, v in flags.items():
            if item.get(k) != v:
                item[k] = v
                changed += 1
        if not args.dry_run:
            if ensure_reader_meta(key, item, texts_root):
                meta_gen += 1

    if args.dry_run:
        extracted  = sum(1 for x in inventory if x.get("extracted"))
        has_reader = sum(1 for x in inventory if x.get("has_reader"))
        print(f"Would update {changed} fields across {len(inventory)} items")
        print(f"  extracted:  {extracted}")
        print(f"  has_reader: {has_reader}")
        return

    if changed:
        tmp = inv_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(inv_path)
        print(f"Updated {changed} fields in {inv_path.name}")
    else:
        print("No changes needed — inventory already up to date")

    extracted  = sum(1 for x in inventory if x.get("extracted"))
    has_reader = sum(1 for x in inventory if x.get("has_reader"))
    print(f"  extracted:  {extracted} / {len(inventory)}")
    print(f"  has_reader: {has_reader} / {len(inventory)}")
    if meta_gen:
        print(f"  reader_meta.json generated: {meta_gen}")


if __name__ == "__main__":
    main()
