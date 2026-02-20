#!/usr/bin/env python3
"""
Fetch PDFs from Zotero's cloud storage into data/pdfs/{key}.pdf.

Downloads attachment files via the Zotero web API — no local Zotero
desktop or filesystem access required.  Works identically in GitHub
Actions and on a developer laptop.

Each item in inventory.json gains provenance fields:
  pdf_staged_path     — relative path, e.g. "data/pdfs/QIGTV3FC.pdf"
  pdf_original_name   — original filename in Zotero
  pdf_zotero_key      — Zotero attachment item key
  pdf_staged_at       — ISO timestamp

Designed to be re-run safely:
  - Already-fetched docs are skipped (unless --force)
  - inventory.json is updated atomically after each download

Usage:
    python scripts/00_stage_pdfs.py              # fetch all available PDFs
    python scripts/00_stage_pdfs.py --dry-run    # preview without downloading
    python scripts/00_stage_pdfs.py --keys KEY1  # fetch specific docs
    python scripts/00_stage_pdfs.py --force      # re-fetch even if already done
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
INV_PATH = _ROOT / "data" / "inventory.json"
PDFS_DIR = _ROOT / "data" / "pdfs"

sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=True)
except ImportError:
    pass


def _save_inventory(inventory: list):
    """Atomic write."""
    tmp = INV_PATH.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INV_PATH)


def fetch_pdf(library, item: dict, children_by_parent: dict,
              pdfs_dir: Path, dry_run: bool = False,
              force: bool = False) -> dict:
    """
    Download one PDF from Zotero's cloud to data/pdfs/{key}.pdf.
    Returns a result dict with status and provenance fields.
    """
    key = item["key"]
    dest = pdfs_dir / f"{key}.pdf"

    # Skip if already fetched and dest exists (unless --force)
    if not force and item.get("pdf_staged_path") and dest.exists():
        return {"key": key, "status": "already_staged"}

    # Find the attachment for this item
    children = children_by_parent.get(key, [])
    att_info = library.get_attachment_info(key, children=children)

    if not att_info:
        return {"key": key, "status": "no_attachment"}

    # linked_file attachments aren't stored in Zotero's cloud
    if att_info.get("link_mode") == "linked_file":
        return {"key": key, "status": "linked_file",
                "detail": "File is linked (not uploaded to Zotero cloud)"}

    if dry_run:
        return {
            "key": key,
            "status": "dry_run",
            "filename": att_info["filename"],
            "attachment_key": att_info["key"],
            "dest": str(dest.relative_to(_ROOT)),
        }

    # Download from Zotero cloud
    file_bytes = library.download_attachment(att_info["key"])
    if file_bytes is None:
        return {"key": key, "status": "download_failed",
                "attachment_key": att_info["key"]}

    pdfs_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_bytes)

    provenance = {
        "pdf_staged_path": str(dest.relative_to(_ROOT)),
        "pdf_original_name": att_info["filename"],
        "pdf_zotero_key": att_info["key"],
        "pdf_staged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    return {
        "key": key,
        "status": "staged",
        "size_mb": dest.stat().st_size / (1024 * 1024),
        **provenance,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fetch PDFs from Zotero cloud into data/pdfs/{key}.pdf")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if already done")
    parser.add_argument("--keys", nargs="+", default=[],
                        help="Only fetch these doc keys")
    parser.add_argument("--inventory", default="data/inventory.json")
    args = parser.parse_args()

    from zotero_client import ZoteroLibrary

    inv_path = _ROOT / args.inventory
    inventory = json.loads(inv_path.read_text("utf-8"))
    key_to_idx = {item["key"]: i for i, item in enumerate(inventory)}

    # Connect to Zotero and fetch all items + children in one call
    library = ZoteroLibrary()
    print(f"Connecting to Zotero web API ({library.library_type} library, "
          f"ID {library.library_id})...")
    _items, children_by_parent = library.get_all_items_with_children()
    print(f"  {len(_items)} items, "
          f"{sum(len(v) for v in children_by_parent.values())} child objects")

    candidates = list(inventory)
    if args.keys:
        key_set = set(args.keys)
        candidates = [x for x in candidates if x["key"] in key_set]

    if not candidates:
        print("No items to process.")
        return

    staged = skipped = missing = failed = 0
    total = len(candidates)

    for item in candidates:
        result = fetch_pdf(library, item, children_by_parent,
                           PDFS_DIR, dry_run=args.dry_run, force=args.force)
        status = result["status"]
        key = result["key"]

        if status == "staged":
            staged += 1
            mb = result.get("size_mb", 0)
            att_key = result.get("pdf_zotero_key", "")
            print(f"  + {key}  {mb:.1f} MB  <- {result.get('pdf_original_name', '')}"
                  f"  [att:{att_key}]")
            # Update inventory item in place
            idx = key_to_idx.get(key)
            if idx is not None:
                for field in ("pdf_staged_path", "pdf_original_name",
                              "pdf_staged_at", "pdf_zotero_key"):
                    if field in result:
                        inventory[idx][field] = result[field]
                inventory[idx]["pdf_status"] = "stored"
                inventory[idx]["pdf_path"] = result.get("pdf_staged_path")
            # Save after each doc (resume-safe)
            if not args.dry_run:
                _save_inventory(inventory)

        elif status == "already_staged":
            skipped += 1

        elif status == "dry_run":
            staged += 1
            print(f"  [dry] {key}  -> {result.get('dest', '')}  "
                  f"({result.get('filename', '')})")

        elif status == "no_attachment":
            missing += 1

        elif status in ("download_failed", "linked_file"):
            failed += 1
            detail = result.get("detail", result.get("attachment_key", ""))
            print(f"  x {key}  {status}: {detail}")

    print()
    print(f"{'[dry] ' if args.dry_run else ''}Fetched: {staged}  "
          f"Skipped (done): {skipped}  No attachment: {missing}  "
          f"Failed: {failed}  Total: {total}")

    if not args.dry_run and staged:
        print(f"\nPDFs saved to {PDFS_DIR}/")
        print(f"Provenance stored in {INV_PATH.name}")


if __name__ == "__main__":
    main()
