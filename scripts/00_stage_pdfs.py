#!/usr/bin/env python3
"""
Stage PDFs into data/pdfs/{key}.pdf with provenance metadata.

Copies PDFs from wherever they currently live (Zotero storage, downloads
folder, etc.) into a single flat directory: data/pdfs/{key}.pdf

Each item in inventory.json gains three provenance fields:
  pdf_staged_path     — relative path used by all pipeline scripts
                        e.g. "data/pdfs/QIGTV3FC.pdf"
  pdf_original_name   — original filename before staging
                        e.g. "Sinisgalli - 2012 - Ptolemy.pdf"
  pdf_zotero_key      — Zotero attachment storage folder name (if applicable)
                        e.g. "6LLPK8WV"  (the 8-char folder in Zotero/storage/)
  pdf_staged_at       — ISO timestamp of when staging happened

Designed to be re-run safely:
  - Already-staged docs are skipped (unless --force)
  - inventory.json is updated atomically after each copy
  - Works for both Zotero-stored PDFs and manually downloaded files

Usage:
    python scripts/00_stage_pdfs.py              # stage all available PDFs
    python scripts/00_stage_pdfs.py --dry-run    # preview without copying
    python scripts/00_stage_pdfs.py --keys KEY1  # stage specific docs
    python scripts/00_stage_pdfs.py --force      # re-stage even if already done

After running:
    - Use pdf_staged_path in all pipeline scripts (not pdf_path)
    - Commit data/pdfs/ via Git LFS: git lfs track "data/pdfs/*.pdf"
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
INV_PATH  = _ROOT / "data" / "inventory.json"
PDFS_DIR  = _ROOT / "data" / "pdfs"

# Zotero storage root — detect automatically
_ZOTERO_CANDIDATES = [
    Path.home() / "Zotero" / "storage",
    Path.home() / "Library" / "Application Support" / "Zotero" / "storage",
]
ZOTERO_STORAGE = next((p for p in _ZOTERO_CANDIDATES if p.exists()), None)


def _is_zotero_path(path: Path) -> bool:
    """Return True if this path is inside Zotero's storage directory."""
    if ZOTERO_STORAGE is None:
        return False
    try:
        path.relative_to(ZOTERO_STORAGE)
        return True
    except ValueError:
        return False


def _zotero_attachment_key(path: Path) -> str | None:
    """
    Extract the Zotero attachment key from a Zotero storage path.
    Zotero stores attachments as: .../Zotero/storage/{ATTACH_KEY}/filename.pdf
    The 8-char parent folder name IS the attachment key.
    """
    if not _is_zotero_path(path):
        return None
    # The folder immediately under storage/ is the attachment key
    try:
        rel = path.relative_to(ZOTERO_STORAGE)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0]  # e.g. "6LLPK8WV"
    except Exception:
        pass
    return None


def stage_pdf(item: dict, pdfs_dir: Path, dry_run: bool = False, force: bool = False) -> dict:
    """
    Copy one PDF to data/pdfs/{key}.pdf and return updated provenance fields.
    Returns a result dict with status and updated fields.
    """
    key      = item["key"]
    src_path = item.get("pdf_path", "")

    if not src_path:
        return {"key": key, "status": "no_path"}

    src = Path(src_path)
    if not src.exists():
        return {"key": key, "status": "missing", "path": str(src)}

    dest = pdfs_dir / f"{key}.pdf"

    # Skip if already staged and dest exists (unless --force)
    if not force and item.get("pdf_staged_path") and dest.exists():
        return {"key": key, "status": "already_staged"}

    if dry_run:
        zk = _zotero_attachment_key(src)
        return {
            "key":    key,
            "status": "dry_run",
            "src":    str(src),
            "dest":   str(dest.relative_to(_ROOT)),
            "original_name": src.name,
            "zotero_key":    zk,
        }

    # Copy
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    zk = _zotero_attachment_key(src)
    provenance = {
        "pdf_staged_path":   str(dest.relative_to(_ROOT)),  # relative, portable
        "pdf_original_name": src.name,
        "pdf_staged_at":     time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if zk:
        provenance["pdf_zotero_key"] = zk

    return {"key": key, "status": "staged", "size_mb": dest.stat().st_size / (1024*1024),
            **provenance}


def _save_inventory(inventory: list):
    """Atomic write."""
    tmp = INV_PATH.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INV_PATH)


def main():
    parser = argparse.ArgumentParser(description="Stage PDFs into data/pdfs/{key}.pdf")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force",   action="store_true",
                        help="Re-stage even if already done")
    parser.add_argument("--keys",    nargs="+", default=[],
                        help="Only stage these doc keys")
    parser.add_argument("--inventory", default="data/inventory.json")
    args = parser.parse_args()

    inv_path  = _ROOT / args.inventory
    inventory = json.loads(inv_path.read_text("utf-8"))
    key_to_idx = {item["key"]: i for i, item in enumerate(inventory)}

    candidates = [x for x in inventory if x.get("pdf_path")]
    if args.keys:
        key_set    = set(args.keys)
        candidates = [x for x in candidates if x["key"] in key_set]

    if not candidates:
        print("No PDFs to stage.")
        return

    staged = skipped = missing = 0
    total  = len(candidates)

    for item in candidates:
        result = stage_pdf(item, PDFS_DIR, dry_run=args.dry_run, force=args.force)
        status = result["status"]
        key    = result["key"]

        if status == "staged":
            staged += 1
            mb = result.get("size_mb", 0)
            zk = result.get("pdf_zotero_key", "")
            print(f"  ✓ {key}  {mb:.1f}MB  ← {result.get('pdf_original_name','')}"
                  + (f"  [zotero:{zk}]" if zk else ""))
            # Update inventory item in place
            idx = key_to_idx.get(key)
            if idx is not None:
                for field in ("pdf_staged_path", "pdf_original_name",
                              "pdf_staged_at", "pdf_zotero_key"):
                    if field in result:
                        inventory[idx][field] = result[field]
            # Save after each doc (resume-safe)
            if not args.dry_run:
                _save_inventory(inventory)

        elif status == "already_staged":
            skipped += 1

        elif status == "dry_run":
            staged += 1
            print(f"  [dry] {key}  → {result.get('dest','')}  "
                  f"({result.get('original_name','')})"
                  + (f"  [zotero:{result.get('zotero_key','')}]"
                     if result.get("zotero_key") else ""))

        elif status in ("missing", "no_path"):
            missing += 1
            if status == "missing":
                print(f"  ✗ {key}  PDF not found: {result.get('path','')}")

    print()
    print(f"{'[dry] ' if args.dry_run else ''}Staged: {staged}  "
          f"Skipped (done): {skipped}  Not found: {missing}  "
          f"Total: {total}")

    if not args.dry_run and staged:
        print(f"\nPDFs → {PDFS_DIR}/")
        print(f"Provenance stored in {INV_PATH.name} (pdf_staged_path, "
              f"pdf_original_name, pdf_zotero_key, pdf_staged_at)")
        print("\nNext: set up Git LFS and commit:")
        print("  git lfs install")
        print("  git lfs track 'data/pdfs/*.pdf'")
        print("  git add .gitattributes data/pdfs/")
        print("  git commit -m 'Add staged PDFs'")
        print("  git push")


if __name__ == "__main__":
    main()
