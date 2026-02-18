#!/usr/bin/env python3
"""
10_ocr_vision.py — Google Vision OCR for scanned documents.

Runs Google Cloud Vision document_text_detection on page images and writes
results into the existing schema:
  - page_texts.json     {page_num: full_page_text}
  - layout_elements.json  [{label, text, bbox, page}]

The reader.html consumes these files unchanged — no reader changes needed.

Usage:
    # Process one doc (all pages)
    python scripts/10_ocr_vision.py --key CR7CQJJ8

    # Process multiple docs
    python scripts/10_ocr_vision.py --key CR7CQJJ8 HMPTGZID

    # Specific page range
    python scripts/10_ocr_vision.py --key CR7CQJJ8 --pages 1-10

    # Overwrite existing Vision output
    python scripts/10_ocr_vision.py --key CR7CQJJ8 --force

    # Dry run: show what would be processed
    python scripts/10_ocr_vision.py --key CR7CQJJ8 --dry-run

Notes:
  - Only processes docs that have a pages/ directory (page images exist).
  - Skips pages already processed unless --force.
  - Backs up original page_texts.json → page_texts.docling.json before first write.
  - Google Vision free tier: 1000 pages/month.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_TEXTS = _ROOT / "data" / "texts"


# ── Credentials ────────────────────────────────────────────────────────────

def load_credentials() -> bool:
    """Load GOOGLE_APPLICATION_CREDENTIALS from .env if not already set."""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and \
            Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).exists():
        return True
    for env_path in [_ROOT / ".env", _ROOT / "data" / ".env"]:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GOOGLE_APPLICATION_CREDENTIALS="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if Path(val).exists():
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = val
                    return True
    return False


# ── Google Vision ──────────────────────────────────────────────────────────

def vision_client():
    from google.cloud import vision
    return vision.ImageAnnotatorClient()


def ocr_page(client, img_path: Path) -> dict:
    """
    Run Vision document_text_detection on one page image.
    Returns {
        'text':     str,                     # full page text
        'blocks':   [{text, bbox, label}]    # paragraph-level blocks
    }
    """
    from google.cloud import vision

    content = img_path.read_bytes()
    image = vision.Image(content=content)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision error on {img_path.name}: {response.error.message}")

    full_text = response.full_text_annotation.text.strip()

    # Extract paragraph-level blocks with bboxes
    blocks = []
    img_w, img_h = _image_size(img_path)

    for page in response.full_text_annotation.pages:
        for block in page.blocks:
            for para in block.paragraphs:
                # Collect paragraph text
                para_text = " ".join(
                    "".join(s.text for s in word.symbols)
                    for word in para.words
                )
                if not para_text.strip():
                    continue

                # Convert normalised vertices to pixel coords matching Docling schema
                verts = para.bounding_box.vertices
                xs = [v.x for v in verts]
                ys = [v.y for v in verts]
                # Docling bbox: l=left, t=top, r=right, b=bottom (in points from bottom-left)
                # Vision gives pixel coords from top-left; convert to Docling-style
                l = min(xs)
                r = max(xs)
                t_px = min(ys)   # top in pixels (from top)
                b_px = max(ys)   # bottom in pixels (from top)
                # Convert to Docling convention: t > b (measured from page bottom)
                t = img_h - t_px
                b = img_h - b_px

                blocks.append({
                    "label": "text",
                    "text": para_text.strip(),
                    "bbox": {"l": round(l, 2), "t": round(t, 2),
                             "r": round(r, 2), "b": round(b, 2)},
                })

    return {"text": full_text, "blocks": blocks}


def _image_size(img_path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(img_path) as img:
        return img.size  # (width, height)


# ── Schema writers ─────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def backup_if_needed(path: Path, suffix: str = ".docling.json") -> None:
    """Back up original Docling output before first Vision write."""
    if not path.exists():
        return
    backup = path.with_suffix(suffix)
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
        print(f"    backed up → {backup.name}")


# ── Processing ─────────────────────────────────────────────────────────────

def list_page_images(doc_dir: Path) -> list[Path]:
    pages_dir = doc_dir / "pages"
    if not pages_dir.exists():
        return []
    return sorted(pages_dir.glob("*.jpg")) + sorted(pages_dir.glob("*.png"))


def page_num_from_path(img_path: Path) -> int:
    return int(img_path.stem)


def already_processed(doc_dir: Path, page_num: int) -> bool:
    """Check if Vision OCR already ran on this page (page_texts.json has non-empty entry)."""
    pt_path = doc_dir / "page_texts.json"
    if not pt_path.exists():
        return False
    data = load_json(pt_path)
    text = data.get(str(page_num), "")
    # Consider it processed if there's substantial text
    return len(text.strip()) > 20


def process_doc(client, doc_dir: Path, page_nums: list[int] | None,
                force: bool, dry_run: bool) -> dict:
    """
    Process one document. Returns stats dict.
    """
    key = doc_dir.name
    images = list_page_images(doc_dir)
    if not images:
        return {"key": key, "status": "no_images", "processed": 0, "skipped": 0}

    # Filter to requested pages
    if page_nums is not None:
        images = [img for img in images if page_num_from_path(img) in page_nums]

    if not images:
        return {"key": key, "status": "no_matching_pages", "processed": 0, "skipped": 0}

    pt_path = doc_dir / "page_texts.json"
    le_path = doc_dir / "layout_elements.json"

    # Load existing data
    page_texts = load_json(pt_path)
    layout_elements = load_json(le_path)  # {page_str: [elements]}

    processed = 0
    skipped = 0
    errors = 0

    for img_path in images:
        pnum = page_num_from_path(img_path)
        pstr = str(pnum)

        if not force and already_processed(doc_dir, pnum):
            skipped += 1
            continue

        if dry_run:
            print(f"  [dry-run] would process page {pnum}")
            processed += 1
            continue

        try:
            result = ocr_page(client, img_path)
        except Exception as e:
            print(f"  ERROR page {pnum}: {e}")
            errors += 1
            continue

        # First write: back up originals
        if processed == 0:
            backup_if_needed(pt_path, ".docling.json")
            backup_if_needed(le_path, ".docling.json")

        # Update page_texts
        page_texts[pstr] = result["text"]

        # Update layout_elements — replace this page's elements with Vision blocks
        # Each element gets a 'page' field to match Docling convention
        vision_elements = []
        for block in result["blocks"]:
            vision_elements.append({
                "label": block["label"],
                "text": block["text"],
                "bbox": block["bbox"],
                "page": pnum,
            })
        layout_elements[pstr] = vision_elements

        word_count = len(result["text"].split())
        print(f"  page {pnum:4d}: {word_count} words, {len(result['blocks'])} blocks")
        processed += 1

        # Write after every page (safe progress)
        save_json(pt_path, page_texts)
        save_json(le_path, layout_elements)

        # Polite rate limiting (Vision free tier: ~1800 req/min)
        time.sleep(0.05)

    return {
        "key": key,
        "status": "ok",
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
    }


# ── Main ───────────────────────────────────────────────────────────────────

def parse_page_range(spec: str) -> list[int]:
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def main():
    parser = argparse.ArgumentParser(description="Google Vision OCR for scanned documents")
    parser.add_argument("--key", nargs="+",
                        help="Document key(s) to process. Omit to process all docs with page images.")
    parser.add_argument("--pages", default=None,
                        help="Page range e.g. 1-10 or 1,3,5 (default: all pages)")
    parser.add_argument("--force", action="store_true",
                        help="Re-process pages that already have Vision output")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without calling Vision API")
    args = parser.parse_args()

    # Credentials
    if not load_credentials():
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS not set or file not found.")
        print("Set it in .env: GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json")
        sys.exit(1)

    page_nums = parse_page_range(args.pages) if args.pages else None

    # Resolve doc list
    if args.key:
        doc_dirs = [_TEXTS / k for k in args.key]
        for d in doc_dirs:
            if not d.exists():
                print(f"ERROR: {d} does not exist")
                sys.exit(1)
    else:
        # All docs with a pages/ directory
        doc_dirs = sorted(
            d for d in _TEXTS.iterdir()
            if d.is_dir() and (d / "pages").exists()
        )
        if not doc_dirs:
            print("No documents with page images found in data/texts/")
            sys.exit(1)

    print(f"Documents : {len(doc_dirs)}")
    print(f"Pages     : {args.pages or 'all'}")
    print(f"Force     : {args.force}")
    print(f"Dry run   : {args.dry_run}")
    print()

    if args.dry_run:
        print("[DRY RUN — no API calls will be made]\n")
        client = None
    else:
        print("Connecting to Google Vision...", end=" ", flush=True)
        client = vision_client()
        print("OK\n")

    totals = {"processed": 0, "skipped": 0, "errors": 0}

    for doc_dir in doc_dirs:
        key = doc_dir.name
        images = list_page_images(doc_dir)
        total_pages = len(images)
        print(f"── {key} ({total_pages} images) ──")

        stats = process_doc(client, doc_dir, page_nums, args.force, args.dry_run)

        print(f"   processed={stats['processed']}  skipped={stats['skipped']}"
              f"  errors={stats.get('errors', 0)}\n")

        totals["processed"] += stats["processed"]
        totals["skipped"] += stats["skipped"]
        totals["errors"] += stats.get("errors", 0)

    print("=" * 50)
    print(f"Total processed : {totals['processed']}")
    print(f"Total skipped   : {totals['skipped']}")
    print(f"Total errors    : {totals['errors']}")
    if not args.dry_run and totals["processed"] > 0:
        print("\nNext step: open data/reader.html to verify output.")
        est_cost = totals["processed"] * 0.0015  # ~$1.50/1000 pages after free tier
        print(f"Approx Vision cost (if past free tier): ${est_cost:.2f}")


if __name__ == "__main__":
    main()
