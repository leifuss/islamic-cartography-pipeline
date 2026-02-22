#!/usr/bin/env python3
"""
Backfill pdf_dpi for existing PDFs in inventory.json.

Scans each inventory item that has a local PDF, estimates the effective DPI
of embedded images using pypdfium2, and updates inventory.json in place.

Usage:
    python scripts/00_backfill_dpi.py
    python scripts/00_backfill_dpi.py --collection-slug islamic-cartography
    python scripts/00_backfill_dpi.py --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent

try:
    import pypdfium2 as pdfium
    from pypdfium2.raw import FPDF_PAGEOBJ_IMAGE
except ImportError:
    print("ERROR: pypdfium2 not installed.  Run: pip install pypdfium2")
    sys.exit(1)


COLLECTIONS_PATH = _ROOT / "data" / "collections.json"


def _get_collection_base(slug: str) -> Path:
    if not COLLECTIONS_PATH.exists():
        raise SystemExit("ERROR: data/collections.json not found")
    with open(COLLECTIONS_PATH, encoding="utf-8") as f:
        coll_data = json.load(f)
    for c in coll_data.get("collections", []):
        if c["slug"] == slug:
            path = c.get("path", slug)
            if path == ".":
                return _ROOT / "data"
            return _ROOT / "data" / path
    raise SystemExit(f"ERROR: collection slug {slug!r} not found")


def _estimate_page_dpi(page) -> float | None:
    """Estimate effective DPI of images on a single PDF page."""
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


def estimate_pdf_dpi(pdf_path: Path) -> int | None:
    """Open a PDF and estimate DPI from first 3 pages."""
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        n = len(doc)
        sample = list(range(min(3, n)))
        all_dpis = []
        for i in sample:
            d = _estimate_page_dpi(doc[i])
            if d is not None:
                all_dpis.append(d)
        if all_dpis:
            all_dpis.sort()
            return round(all_dpis[len(all_dpis) // 2])
    except Exception as e:
        print(f"    error: {e}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Backfill pdf_dpi for existing PDFs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--collection-slug", default=None,
                        help="Collection slug from data/collections.json")
    args = parser.parse_args()

    if args.collection_slug:
        base = _get_collection_base(args.collection_slug)
        inv_path = base / "inventory.json"
        print(f"Collection: {args.collection_slug}  ({base})")
    else:
        inv_path = _ROOT / "data" / "inventory.json"

    inventory = json.loads(inv_path.read_text("utf-8"))
    print(f"Loaded {len(inventory)} items from {inv_path.name}")

    updated = 0
    scanned = 0
    skipped = 0
    no_images = 0

    for item in inventory:
        key = item["key"]
        pdf_path_rel = item.get("pdf_path")

        # Skip items without a local PDF
        if not pdf_path_rel or item.get("pdf_status") not in ("stored", "downloaded"):
            skipped += 1
            continue

        # Skip if DPI is already set
        if item.get("pdf_dpi") is not None:
            skipped += 1
            continue

        pdf_path = _ROOT / pdf_path_rel
        if not pdf_path.exists():
            skipped += 1
            continue

        scanned += 1

        # Born-digital docs get DPI 0 — no raster resolution to measure
        if item.get("doc_type") == "embedded":
            item["pdf_dpi"] = 0
            updated += 1
            print(f"  {key}: 0 DPI (born-digital)")
            continue

        dpi = estimate_pdf_dpi(pdf_path)

        if dpi is not None:
            item["pdf_dpi"] = dpi
            updated += 1
            print(f"  {key}: {dpi} DPI")
        else:
            no_images += 1
            if scanned % 20 == 0:
                print(f"  ... scanned {scanned} PDFs so far")

    print(f"\nResults: {scanned} PDFs scanned, {updated} DPI values set, "
          f"{no_images} had no embedded images, {skipped} skipped")

    if args.dry_run:
        print("(dry run — no changes written)")
        return

    if updated:
        tmp = inv_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(inv_path)
        print(f"Updated {inv_path.name}")
    else:
        print("No changes needed")


if __name__ == "__main__":
    main()
