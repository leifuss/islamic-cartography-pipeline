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
import re
import shutil
import sys
import time
from pathlib import Path
from PIL import Image

# ── Paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_TEXTS = _ROOT / "data" / "texts"

COLLECTIONS_PATH = _ROOT / "data" / "collections.json"


def _resolve_collection_texts(slug: str) -> Path:
    """Resolve the texts directory for a given collection slug."""
    if not COLLECTIONS_PATH.exists():
        print(f"ERROR: {COLLECTIONS_PATH} not found"); sys.exit(1)
    colls = json.loads(COLLECTIONS_PATH.read_text("utf-8"))
    for c in colls.get("collections", []):
        if c["slug"] == slug:
            path = c.get("path", slug)
            base = _ROOT / "data" if path == "." else _ROOT / "data" / path
            return base / "texts"
    print(f"ERROR: collection slug {slug!r} not found"); sys.exit(1)


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


def upscale_image(img_path: Path, factor: int, tmp_dir: Path) -> Path:
    """Return path to a temporary upscaled copy of the image."""
    img = Image.open(img_path)
    w, h = img.size
    img = img.resize((w * factor, h * factor), Image.LANCZOS)
    tmp = tmp_dir / f"_up{factor}_{img_path.name}"
    img.save(tmp)
    return tmp


def _classify_paragraph(para, block_type, para_text: str,
                        para_height: float, img_w: int, img_h: int,
                        t_px: float, b_px: float,
                        median_body_height: float) -> str:
    """
    Assign a Docling-compatible label to a Vision paragraph using
    block_type and position/size heuristics.
    """
    from google.cloud import vision

    # Non-text block types
    if block_type == vision.Block.BlockType.TABLE:
        return "table"
    if block_type == vision.Block.BlockType.PICTURE:
        return "picture"

    text = para_text.strip()

    # Page header: in top 8% of page, short text
    if t_px < img_h * 0.08 and len(text) < 80:
        return "page_header"

    # Page footer: in bottom 8% of page, short text
    if b_px > img_h * 0.92 and len(text) < 80:
        return "page_footer"

    # Footnote: small text in bottom 30% of page, starts with a number
    if (median_body_height > 0 and para_height < median_body_height * 0.75
            and b_px > img_h * 0.70
            and len(text) < 200):
        if re.match(r'^\d{1,3}[\s.)‐–-]', text):
            return "footnote"

    # Section header: tall text (> 1.3× median body height) and short
    if (median_body_height > 0 and para_height > median_body_height * 1.3
            and len(text) < 120):
        return "section_header"

    # Caption: short italic-style text near pictures (heuristic: short + parenthetical)
    if len(text) < 100 and (text.startswith("Fig") or text.startswith("Table")
                            or text.startswith("Map") or text.startswith("Plate")):
        return "caption"

    return "text"


def ocr_page(client, img_path: Path, upsample: int = 1,
             lang_hints: list[str] | None = None,
             tmp_dir: Path | None = None) -> dict:
    """
    Run Vision document_text_detection on one page image.
    Returns {
        'text':     str,                     # full page text
        'blocks':   [{text, bbox, label}]    # paragraph-level blocks
    }
    """
    from google.cloud import vision

    src = img_path
    if upsample > 1 and tmp_dir is not None:
        src = upscale_image(img_path, upsample, tmp_dir)

    content = src.read_bytes()
    image = vision.Image(content=content)

    image_context = None
    if lang_hints:
        image_context = vision.ImageContext(language_hints=lang_hints)

    response = client.document_text_detection(
        image=image,
        image_context=image_context,
    )

    if response.error.message:
        raise RuntimeError(f"Vision error on {img_path.name}: {response.error.message}")

    full_text = response.full_text_annotation.text.strip()

    # Extract paragraph-level blocks with bboxes
    blocks = []
    img_w, img_h = _image_size(img_path)

    # First pass: collect paragraph heights to compute median body-text height
    para_heights = []
    for page in response.full_text_annotation.pages:
        for block in page.blocks:
            if block.block_type != vision.Block.BlockType.TEXT:
                continue
            for para in block.paragraphs:
                verts = para.bounding_box.vertices
                ys = [v.y for v in verts]
                if ys:
                    para_heights.append(max(ys) - min(ys))
    median_body_height = sorted(para_heights)[len(para_heights) // 2] if para_heights else 0

    # Second pass: extract blocks with labels
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
                para_height = b_px - t_px
                # Convert to Docling convention: t > b (measured from page bottom)
                t = img_h - t_px
                b = img_h - b_px

                label = _classify_paragraph(
                    para, block.block_type, para_text.strip(),
                    para_height, img_w, img_h, t_px, b_px,
                    median_body_height,
                )

                blocks.append({
                    "label": label,
                    "text": para_text.strip(),
                    "bbox": {"l": round(l, 2), "t": round(t, 2),
                             "r": round(r, 2), "b": round(b, 2)},
                })

    return {"text": full_text, "blocks": blocks, "src_w": img_w, "src_h": img_h}


def _image_size(img_path: Path) -> tuple[int, int]:
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
                force: bool, dry_run: bool,
                upsample: int = 1, lang_hints: list[str] | None = None) -> dict:
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
    ocr_dir = doc_dir / "ocr_test"
    ocr_dir.mkdir(exist_ok=True)

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
            result = ocr_page(client, img_path, upsample=upsample,
                              lang_hints=lang_hints, tmp_dir=ocr_dir)
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

        # Write pixel page size so reader.html can normalise bbox overlays
        if "_page_sizes" not in layout_elements:
            layout_elements["_page_sizes"] = {}
        layout_elements["_page_sizes"][pstr] = {
            "w": result["src_w"], "h": result["src_h"]
        }

        word_count = len(result["text"].split())
        print(f"  page {pnum:4d}: {word_count} words, {len(result['blocks'])} blocks")
        processed += 1

        # Write after every page (safe progress)
        save_json(pt_path, page_texts)
        save_json(le_path, layout_elements)

        # Polite rate limiting (Vision free tier: ~1800 req/min)
        time.sleep(0.05)

    # Clean up temporary upsampled images
    if ocr_dir.exists():
        shutil.rmtree(ocr_dir, ignore_errors=True)

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
    parser.add_argument("--upsample", type=int, default=1, metavar="N",
                        help="Upsample images N× before sending to Vision (e.g. 2 or 3). "
                             "Helps when source images are below ~150 DPI.")
    parser.add_argument("--lang-hints", nargs="+", default=None, metavar="LANG",
                        help="BCP-47 language hint(s) for Vision (e.g. fa ar en). "
                             "Useful for Arabic/Persian docs.")
    parser.add_argument("--collection-slug", default=None,
                        help="Collection slug from data/collections.json")
    args = parser.parse_args()

    # Credentials
    if not load_credentials():
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS not set or file not found.")
        print("Set it in .env: GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json")
        sys.exit(1)

    page_nums = parse_page_range(args.pages) if args.pages else None

    # Resolve texts directory (collection-aware)
    texts_dir = _resolve_collection_texts(args.collection_slug) if args.collection_slug else _TEXTS

    # Resolve doc list
    if args.key:
        doc_dirs = [texts_dir / k for k in args.key]
        for d in doc_dirs:
            if not d.exists():
                print(f"ERROR: {d} does not exist")
                sys.exit(1)
    else:
        # All docs with a pages/ directory
        doc_dirs = sorted(
            d for d in texts_dir.iterdir()
            if d.is_dir() and (d / "pages").exists()
        )
        if not doc_dirs:
            print(f"No documents with page images found in {texts_dir}")
            sys.exit(1)

    print(f"Documents : {len(doc_dirs)}")
    print(f"Pages     : {args.pages or 'all'}")
    print(f"Upsample  : {args.upsample}×" if args.upsample > 1 else f"Upsample  : none")
    print(f"Lang hints: {args.lang_hints or 'auto-detect'}")
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

        stats = process_doc(client, doc_dir, page_nums, args.force, args.dry_run,
                            upsample=args.upsample, lang_hints=args.lang_hints)

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
