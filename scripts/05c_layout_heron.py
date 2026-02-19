#!/usr/bin/env python3
"""
Layout enrichment using Docling's Heron model (RT-DETRv2).

Heron is a 42.9M-parameter object detection model trained on document images.
It detects 17 semantic region types: section_header, text, footnote, table,
picture, caption, formula, list_item, page_header, page_footer, title, etc.

── Cloud-first design ───────────────────────────────────────────────────────────

This script does NOT need the source PDFs. It works entirely from:
  • data/texts/{KEY}/pages/NNN.jpg   (page images saved by 05b)
  • data/texts/{KEY}/page_texts.json (text saved by 05b)

Both of those are tracked in git and available in GitHub Actions / cloud runners.

── What it does ────────────────────────────────────────────────────────────────

  1. Loads saved page images from data/texts/{KEY}/pages/
  2. Runs Heron on each image to detect semantic layout regions
  3. Assigns text to each region using Tesseract image_to_data() word positions
     (works equally well for embedded and scanned docs; no PDF required)
  4. Writes an enriched layout_elements.json with proper semantic labels
     (replaces the crude single-blob-per-page version from 05b)

── Performance ─────────────────────────────────────────────────────────────────

  • With MPS (Apple Silicon) or CUDA: ~2-4 s/page
  • CPU only (GitHub Actions free tier): ~3-5 s/page
    → 95 docs × 40 pages = 3,800 pages ≈ 3-4 hours on CPU (fits in Actions 6h limit)
  • For speed, prefer Modal.com with a T4 GPU (~$0.10 for the full corpus)

── Usage ───────────────────────────────────────────────────────────────────────

    python scripts/05c_layout_heron.py              # all extracted, not yet enriched
    python scripts/05c_layout_heron.py --dry-run    # show plan, no writes
    python scripts/05c_layout_heron.py --keys KEY1  # specific docs
    python scripts/05c_layout_heron.py --force      # re-enrich already-enriched docs
    python scripts/05c_layout_heron.py --threshold 0.5   # detection confidence (default 0.6)
    python scripts/05c_layout_heron.py --batch 8         # pages per GPU batch (default 4)

Run in background:
    nohup python scripts/05c_layout_heron.py > /tmp/layout.log 2>&1 &
    tail -f /tmp/layout.log

── Prerequisite ────────────────────────────────────────────────────────────────

Run 05b_extract_robust.py first. It creates page_texts.json AND (since pages are
rendered for Tesseract/Vision OCR) should save page images. If page images are
missing, this script will log a warning and skip those docs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────────

TEXTS_ROOT  = _ROOT / "data" / "texts"
INV_PATH    = _ROOT / "data" / "inventory.json"
HERON_MODEL = "docling-project/docling-layout-heron"

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 0.6    # Heron confidence threshold
DEFAULT_BATCH     = 4      # Pages per inference batch

# Heron label map: class index → label name compatible with reader.html
HERON_LABELS: dict[int, str] = {
    0:  "caption",
    1:  "footnote",
    2:  "formula",
    3:  "list_item",
    4:  "page_footer",
    5:  "page_header",
    6:  "picture",
    7:  "section_header",
    8:  "table",
    9:  "text",
    10: "title",
    11: "document_index",
    12: "code",
    13: "form",       # Checkbox-Selected
    14: "form",       # Checkbox-Unselected
    15: "form",
    16: "key_value",
}

# ── Lazy model singleton ───────────────────────────────────────────────────────

_model     = None
_processor = None
_device    = None


def _get_model():
    """Load Heron once, return (processor, model, device)."""
    global _model, _processor, _device
    if _model is not None:
        return _processor, _model, _device

    import torch
    from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection

    if torch.backends.mps.is_available():
        _device = torch.device("mps")
    elif torch.cuda.is_available():
        _device = torch.device("cuda")
    else:
        _device = torch.device("cpu")
        print(f"  [heron] No GPU found — running on CPU (will be slow)", flush=True)

    print(f"  [heron] Loading {HERON_MODEL} on {_device} …", flush=True)
    t0 = time.time()
    _processor = RTDetrImageProcessor.from_pretrained(HERON_MODEL)
    _model     = RTDetrV2ForObjectDetection.from_pretrained(HERON_MODEL).to(_device)
    _model.eval()
    print(f"  [heron] Ready in {time.time()-t0:.1f}s", flush=True)

    return _processor, _model, _device


# ── Heron inference ────────────────────────────────────────────────────────────

def _detect_layout(pil_images: list, threshold: float = DEFAULT_THRESHOLD) -> list[list[dict]]:
    """
    Run Heron on a batch of PIL images.
    Returns: list (one entry per image) of lists of detected regions:
        [{"label": str, "score": float, "bbox_px": [x1, y1, x2, y2]}, …]
    bbox_px is image pixel coords: origin top-left, y increases downward.
    """
    import torch
    processor, model, device = _get_model()

    rgb_images   = [img.convert("RGB") for img in pil_images]
    target_sizes = torch.tensor([[img.size[1], img.size[0]] for img in rgb_images])  # (H, W)

    inputs = processor(images=rgb_images, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        outputs = model(**inputs)

    raw_results = processor.post_process_object_detection(
        outputs,
        target_sizes=target_sizes.to(device),
        threshold=threshold,
    )

    all_regions = []
    for raw in raw_results:
        regions = []
        for score, label_id, box in zip(
            raw["scores"].cpu().tolist(),
            raw["labels"].cpu().tolist(),
            raw["boxes"].cpu().tolist(),
        ):
            x1, y1, x2, y2 = box
            regions.append({
                "label":   HERON_LABELS.get(int(label_id), "text"),
                "score":   round(float(score), 3),
                "bbox_px": [round(x1), round(y1), round(x2), round(y2)],
            })
        # Sort top-to-bottom, left-to-right (natural reading order)
        regions.sort(key=lambda r: (r["bbox_px"][1], r["bbox_px"][0]))
        all_regions.append(regions)

    return all_regions


# ── Text assignment via Tesseract word positions ───────────────────────────────

def _assign_text_from_image(pil_image, regions: list[dict],
                             lang: str = "eng") -> list[dict]:
    """
    Use Tesseract image_to_data() to get word bounding boxes, then assign
    each word to the Heron region whose bbox contains the word's centre.

    Works for both embedded and scanned pages — only needs the rendered image.
    For clean (embedded) pages at 144 DPI, Tesseract word detection is accurate.

    Modifies regions in-place, adding a "text" key to each.
    Returns the modified list.
    """
    import pytesseract

    # image_to_data returns word-level boxes + confidence
    try:
        data = pytesseract.image_to_data(
            pil_image.convert("RGB"),
            lang=lang,
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        for r in regions:
            r.setdefault("text", "")
        return regions

    # Collect valid words with their centre points
    words: list[tuple[float, float, str]] = []  # (cx, cy, word)
    n = len(data["text"])
    for i in range(n):
        word = str(data["text"][i]).strip()
        conf = int(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1
        if not word or conf < 0:
            continue
        left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        cx = left + w / 2
        cy = top  + h / 2
        words.append((cx, cy, word))

    # Assign words to regions
    word_buckets: list[list[str]] = [[] for _ in regions]
    for cx, cy, word in words:
        for i, reg in enumerate(regions):
            x1, y1, x2, y2 = reg["bbox_px"]
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                word_buckets[i].append(word)
                break   # assign to first (topmost) containing region

    for reg, bucket in zip(regions, word_buckets):
        reg["text"] = " ".join(bucket)

    return regions


# ── Coordinate conversion (image pixels → PDF points) ─────────────────────────

def _px_to_pdf_bbox(x1: float, y1: float, x2: float, y2: float,
                    page_w_pts: float, page_h_pts: float,
                    img_w_px: int, img_h_px: int) -> dict:
    """
    Convert image pixel bbox to PDF point space.

    Image space: origin top-left, y increases downward.
    PDF space:   origin bottom-left, y increases upward.

    We infer the render scale from the image dimensions vs the stored PDF page size,
    so this works regardless of what scale was used when the page was rendered.

    Returns {"l", "b", "r", "t"} in PDF points, where t > b.
    """
    scale_x = page_w_pts / img_w_px   # pts per pixel, x axis
    scale_y = page_h_pts / img_h_px   # pts per pixel, y axis

    l = x1 * scale_x
    r = x2 * scale_x
    # Flip Y: image y_min (top of box) → PDF high y (top)
    t = page_h_pts - y1 * scale_y
    b = page_h_pts - y2 * scale_y

    return {"l": round(l, 1), "b": round(b, 1), "r": round(r, 1), "t": round(t, 1)}


# ── Load saved page images ─────────────────────────────────────────────────────

def _load_page_images(pages_dir: Path) -> dict[int, object]:
    """
    Load all pages/NNN.jpg images from a doc directory.
    Returns {0-based page_idx: PIL Image}.
    """
    from PIL import Image
    images: dict[int, object] = {}
    if not pages_dir.is_dir():
        return images
    for img_path in sorted(pages_dir.glob("*.jpg")):
        try:
            page_num = int(img_path.stem) - 1   # NNN.jpg → 0-based index
            images[page_num] = Image.open(img_path).copy()  # copy to close file handle
        except Exception:
            continue
    return images


# ── Infer page sizes from images + layout_elements._page_sizes ────────────────

def _get_page_sizes(doc_dir: Path, n_pages: int,
                    img_sizes: dict[int, tuple[int, int]]) -> dict[str, dict]:
    """
    Get page sizes in PDF points. Prefers existing _page_sizes from layout_elements.json,
    falls back to estimating from image dimensions at 144 DPI (scale=2.0).
    """
    # Try to read from existing layout_elements.json
    le_path = doc_dir / "layout_elements.json"
    if le_path.exists():
        try:
            le  = json.loads(le_path.read_text("utf-8"))
            pts = le.get("_page_sizes", {})
            if pts:
                return pts
        except Exception:
            pass

    # Estimate from image dimensions at 144 DPI (scale=2.0 → 72 pts/inch base × 2)
    # PDF points = pixels / scale where scale=2.0
    sizes: dict[str, dict] = {}
    for i in range(n_pages):
        if i in img_sizes:
            w_px, h_px = img_sizes[i]
            sizes[str(i + 1)] = {
                "w": round(w_px / 2.0, 1),
                "h": round(h_px / 2.0, 1),
            }
        else:
            sizes[str(i + 1)] = {"w": 612.0, "h": 792.0}   # US Letter fallback

    return sizes


# ── Core: enrich one document ──────────────────────────────────────────────────

def enrich_document(item: dict, force: bool = False,
                    threshold: float = DEFAULT_THRESHOLD,
                    batch_size: int = DEFAULT_BATCH) -> dict:
    """
    Enrich layout_elements.json for one document using Heron.
    Works entirely from saved page images — no PDF required.

    Returns a result dict: key, status ('ok'|'error'|'skip'), pages, regions, elapsed_s.
    """
    key    = item["key"]
    title  = item.get("title", key)
    doc_dir = TEXTS_ROOT / key
    le_path = doc_dir / "layout_elements.json"
    pt_path = doc_dir / "page_texts.json"

    # ── Prerequisite: need page_texts.json ────────────────────────────────────
    if not pt_path.exists():
        return {"key": key, "title": title, "status": "skip",
                "reason": "no page_texts.json — run 05b_extract_robust.py first"}

    # ── Resume check ──────────────────────────────────────────────────────────
    if not force and le_path.exists():
        try:
            existing = json.loads(le_path.read_text("utf-8"))
            if existing.get("_heron_version"):
                return {"key": key, "title": title, "status": "skip",
                        "reason": "already enriched"}
        except Exception:
            pass  # corrupt file → re-run

    # ── Load saved page images ─────────────────────────────────────────────────
    pages_dir   = doc_dir / "pages"
    pil_images  = _load_page_images(pages_dir)

    if not pil_images:
        return {"key": key, "title": title, "status": "error",
                "error": (f"No page images found in {pages_dir}. "
                          "Run 05b first, or ensure page images are committed to git.")}

    # ── Tesseract language ────────────────────────────────────────────────────
    tess_lang = "eng"
    meta_path = doc_dir / "meta.json"
    if meta_path.exists():
        try:
            meta      = json.loads(meta_path.read_text("utf-8"))
            lang      = meta.get("language", "en")
            tess_lang = {"ar": "ara", "fa": "fas", "tr": "tur",
                         "de": "deu", "fr": "fra"}.get(lang, "eng")
        except Exception:
            pass

    # ── Page metadata ─────────────────────────────────────────────────────────
    n_pages   = max(pil_images.keys()) + 1 if pil_images else 0
    img_sizes = {idx: (img.size[0], img.size[1]) for idx, img in pil_images.items()}
    page_sizes = _get_page_sizes(doc_dir, n_pages, img_sizes)
    page_texts = json.loads(pt_path.read_text("utf-8"))

    # ── Run Heron in batches ──────────────────────────────────────────────────
    heron_per_page: dict[int, list[dict]] = {}
    page_indices   = sorted(pil_images.keys())

    for batch_start in range(0, len(page_indices), batch_size):
        batch_idxs = page_indices[batch_start : batch_start + batch_size]
        batch_imgs = [pil_images[i] for i in batch_idxs]
        try:
            detected = _detect_layout(batch_imgs, threshold=threshold)
        except Exception as e:
            # Inference failure — leave these pages with fallback single element
            detected = [[] for _ in batch_imgs]
        for page_idx, regions in zip(batch_idxs, detected):
            heron_per_page[page_idx] = regions

    # ── Assign text and build output ──────────────────────────────────────────
    result_elements: dict[str, object] = {}

    for i in range(n_pages):
        page_num   = str(i + 1)
        regions    = heron_per_page.get(i, [])
        sz         = page_sizes.get(page_num, {"w": 612.0, "h": 792.0})
        pw, ph     = sz["w"], sz["h"]
        page_text  = page_texts.get(page_num, "")
        pil_img    = pil_images.get(i)

        if not regions or pil_img is None:
            # No detections or no image — fall back to single text element
            result_elements[page_num] = [{
                "label": "text",
                "text":  page_text.strip(),
                "bbox":  {"l": 0.05*pw, "b": 0.05*ph, "r": 0.95*pw, "t": 0.95*ph},
            }] if page_text.strip() else []
            continue

        # Add bbox_px to regions (needed for text assignment)
        for r in regions:
            r.setdefault("bbox_px", r.get("bbox_px", [0, 0, 1, 1]))

        # Text assignment via Tesseract word positions on the page image
        _assign_text_from_image(pil_img, regions, lang=tess_lang)

        # Convert bboxes to PDF point space
        w_px, h_px = pil_img.size
        page_regions_out = []
        for reg in regions:
            x1, y1, x2, y2 = reg["bbox_px"]
            page_regions_out.append({
                "label": reg["label"],
                "text":  reg.get("text", ""),
                "bbox":  _px_to_pdf_bbox(x1, y1, x2, y2, pw, ph, w_px, h_px),
            })

        result_elements[page_num] = page_regions_out

    # ── Write enriched layout_elements.json ───────────────────────────────────
    result_elements["_page_sizes"]      = page_sizes
    result_elements["_heron_version"]   = "1.0"
    result_elements["_heron_threshold"] = threshold

    le_path.write_text(
        json.dumps(result_elements, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    n_regions = sum(
        len(v) for k, v in result_elements.items()
        if isinstance(v, list)
    )

    return {
        "key":     key,
        "title":   title,
        "status":  "ok",
        "pages":   n_pages,
        "regions": n_regions,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Enrich layout_elements.json using Heron (RT-DETRv2). No PDF required."
    )
    parser.add_argument("--dry-run",   action="store_true",
                        help="Show plan without writing")
    parser.add_argument("--force",     action="store_true",
                        help="Re-enrich even if already done")
    parser.add_argument("--keys",      nargs="+", default=[],
                        help="Only process these doc keys")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Detection confidence threshold (default {DEFAULT_THRESHOLD})")
    parser.add_argument("--batch",     type=int, default=DEFAULT_BATCH,
                        help=f"Pages per inference batch (default {DEFAULT_BATCH})")
    parser.add_argument("--limit",     type=int, default=0,
                        help="Stop after N docs (0 = all)")
    parser.add_argument("--inventory", default="data/inventory.json")
    args = parser.parse_args()

    inv_path  = _ROOT / args.inventory
    inventory = json.loads(inv_path.read_text("utf-8"))

    # Candidates: docs with page_texts.json AND at least one page image
    candidates = []
    for r in inventory:
        doc_dir = TEXTS_ROOT / r["key"]
        if not (doc_dir / "page_texts.json").exists():
            continue
        if not (doc_dir / "pages").is_dir() or not any((doc_dir / "pages").glob("*.jpg")):
            continue
        candidates.append(r)

    if args.keys:
        key_set    = set(args.keys)
        candidates = [r for r in candidates if r["key"] in key_set]

    if not args.force:
        def _already_enriched(r):
            le = TEXTS_ROOT / r["key"] / "layout_elements.json"
            if not le.exists():
                return False
            try:
                return bool(json.loads(le.read_text("utf-8")).get("_heron_version"))
            except Exception:
                return False
        candidates = [r for r in candidates if not _already_enriched(r)]

    if args.limit:
        candidates = candidates[:args.limit]

    total = len(candidates)
    if not total:
        print("Nothing to do. All eligible docs already have Heron layout enrichment.")
        print("(Use --force to re-run, or check that page images exist in pages/*.jpg)")
        return

    print(f"Docs to enrich: {total}")
    print(f"Threshold: {args.threshold}  Batch: {args.batch}")

    if args.dry_run:
        for r in candidates:
            pages_dir = TEXTS_ROOT / r["key"] / "pages"
            n_imgs    = len(list(pages_dir.glob("*.jpg"))) if pages_dir.is_dir() else 0
            print(f"  {r['key']}  {n_imgs} pages  {r.get('title','')[:55]}")
        return

    ok_count   = 0
    err_count  = 0
    skip_count = 0

    for n, item in enumerate(candidates, 1):
        key   = item["key"]
        title = item.get("title", key)
        print(f"\n  ⟳  [{n}/{total}] {key}  {title[:55]}", flush=True)

        t0     = time.time()
        result = enrich_document(
            item,
            force=args.force,
            threshold=args.threshold,
            batch_size=args.batch,
        )
        elapsed = int(time.time() - t0)
        result["elapsed_s"] = elapsed

        status = result["status"]
        if status == "ok":
            ok_count += 1
            print(
                f"  ✓  {key}  {result.get('pages',0)} pp · "
                f"{result.get('regions',0)} regions · {elapsed}s"
            )
        elif status == "skip":
            skip_count += 1
            print(f"  –  {key}  skipped: {result.get('reason','')}")
        else:
            err_count += 1
            print(f"  ✗  {key}  ERROR: {result.get('error','')[:100]}")

    print("\n" + "=" * 60)
    print(f"✓ Enriched: {ok_count}")
    print(f"✗ Errors:   {err_count}")
    print(f"– Skipped:  {skip_count}")
    if ok_count:
        print(f"\nTexts → {TEXTS_ROOT}/")
        print("Commit with: git add data/texts/ && git commit -m 'layout: Heron enrichment'")


if __name__ == "__main__":
    main()
