#!/usr/bin/env python3
"""
Robust, fire-and-forget extraction script.

Strategy (no Docling, no ML model loading):
  1. Embedded-font PDFs (doc_type='embedded'): extract text directly via pypdfium2.
     Per-page fallback: if a page yields < 50 chars, render it and run Tesseract.
  2. Unknown type: quick classify first (same heuristic as script 03),
     then treat as embedded or scanned accordingly.
  3. Scanned PDFs: render each page and run Tesseract.
  4. Vision API fallback: only used if --vision flag is given, or when Tesseract
     returns clearly suspect output AND a Vision key is available.

Design principles:
  - 100% non-interactive. All API keys from .env. All errors logged, never fatal.
  - Resume-safe: skips docs that already have page_texts.json (unless --force).
  - Writes data/pipeline_status.json after each doc — check progress in a browser.
  - Parallel via a simple ThreadPoolExecutor (I/O-bound, no ML models to fight over).

Usage:
    python scripts/05b_extract_robust.py                    # all unextracted docs
    python scripts/05b_extract_robust.py --dry-run          # classify only, no writes
    python scripts/05b_extract_robust.py --workers 4        # parallel (default 2)
    python scripts/05b_extract_robust.py --keys KEY1 KEY2   # specific docs
    python scripts/05b_extract_robust.py --force            # re-process already done
    python scripts/05b_extract_robust.py --vision           # enable Vision API fallback
    python scripts/05b_extract_robust.py --limit 5          # stop after 5 docs

Then run in background:
    nohup python scripts/05b_extract_robust.py > /tmp/extract.log 2>&1 &
    tail -f /tmp/extract.log
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / "src")
sys.path.insert(0, _SRC)

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # dotenv optional

# ── Lazy imports ──────────────────────────────────────────────────────────────
# (imported at use time so --dry-run works even in a minimal environment)

def _import_pypdfium2():
    try:
        import pypdfium2 as pdfium
        return pdfium
    except ImportError:
        return None

def _import_pytesseract():
    try:
        import pytesseract
        return pytesseract
    except ImportError:
        return None

def _import_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None

def _import_vision():
    try:
        from google.cloud import vision as gv
        return gv
    except ImportError:
        return None


# ── Paths ──────────────────────────────────────────────────────────────────────

TEXTS_ROOT   = _ROOT / "data" / "texts"
INV_PATH     = _ROOT / "data" / "inventory.json"
STATUS_PATH  = _ROOT / "data" / "pipeline_status.json"
RESULTS_PATH = _ROOT / "data" / "extract_results.json"

# ── Constants ──────────────────────────────────────────────────────────────────

EMBEDDED_THRESHOLD = 50     # chars/page below which a page is treated as scanned
SCANNED_THRESHOLD  = 50     # same threshold for classifying an unknown doc as scanned
RENDER_SCALE       = 2.0    # render scale for Tesseract (150 dpi at default 72dpi base)
JPEG_QUALITY       = 85     # for saved page images

# Tesseract language codes
LANG_TESSERACT = {
    "ar": "ara",
    "fa": "fas",
    "tr": "tur",   # Ottoman Turkish
    "de": "deu",
    "fr": "fra",
    "en": "eng",
}
DEFAULT_TESS_LANG = "eng"

# ── Status file ────────────────────────────────────────────────────────────────

_status_lock = threading.Lock()


def _load_status() -> dict:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "docs": {}}


def _save_status(status: dict):
    """Atomic write — safe to call from multiple threads."""
    with _status_lock:
        tmp = STATUS_PATH.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_PATH)


def _update_status(status: dict, key: str, state: str, **extra):
    status["docs"][key] = {
        "state": state,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **extra,
    }
    status["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_status(status)


# ── Results log ────────────────────────────────────────────────────────────────

_results_lock = threading.Lock()


def _load_results() -> list:
    if RESULTS_PATH.exists():
        try:
            return json.loads(RESULTS_PATH.read_text("utf-8"))
        except Exception:
            pass
    return []


def _append_result(results: list, entry: dict):
    with _results_lock:
        results.append(entry)
        tmp = RESULTS_PATH.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RESULTS_PATH)


# ── Text quality ───────────────────────────────────────────────────────────────

def _compute_quality(page_texts: dict) -> dict:
    """Return quality metrics dict (same schema as script 05)."""
    if not page_texts:
        return {"chars_per_page": 0.0, "pua_ratio": 1.0, "replacement_ratio": 1.0,
                "empty_pages": 0, "text_quality": "garbled"}

    all_text = " ".join(page_texts.values())
    n_pages  = len(page_texts)
    n_chars  = len(all_text) or 1

    pua_count  = sum(1 for c in all_text if "\ue000" <= c <= "\uf8ff")
    repl_count = all_text.count("\ufffd")
    pua_ratio  = pua_count  / n_chars
    repl_ratio = repl_count / n_chars
    cpp        = sum(len(t) for t in page_texts.values()) / n_pages
    empty      = sum(1 for t in page_texts.values() if len(t.strip()) < 50)

    if pua_ratio > 0.05 or repl_ratio > 0.05 or cpp < 20:
        quality = "garbled"
    elif pua_ratio > 0.01 or repl_ratio > 0.01 or cpp < 100:
        quality = "suspect"
    else:
        quality = "good"

    return {
        "chars_per_page":    round(cpp, 1),
        "pua_ratio":         round(pua_ratio, 4),
        "replacement_ratio": round(repl_ratio, 4),
        "empty_pages":       empty,
        "text_quality":      quality,
    }


# ── pypdfium2 extraction ───────────────────────────────────────────────────────

def _extract_page_embedded(doc, page_idx: int) -> str:
    """Extract embedded text from a single page using pypdfium2."""
    try:
        page     = doc[page_idx]
        textpage = page.get_textpage()
        return textpage.get_text_range() or ""
    except Exception:
        return ""


def _render_page_to_pil(doc, page_idx: int, scale: float = RENDER_SCALE):
    """Render a PDF page to a PIL image at the given scale."""
    Image = _import_pil()
    if not Image:
        return None
    try:
        page   = doc[page_idx]
        bitmap = page.render(scale=scale)
        return bitmap.to_pil()
    except Exception:
        return None


# ── Tesseract OCR ──────────────────────────────────────────────────────────────

def _tesseract_ocr(pil_image, lang: str = DEFAULT_TESS_LANG) -> str:
    """Run Tesseract on a PIL image. Returns empty string on failure."""
    tess = _import_pytesseract()
    if not tess or pil_image is None:
        return ""
    try:
        return tess.image_to_string(pil_image, lang=lang) or ""
    except Exception as e:
        # Language pack not installed — fall back to English
        if lang != "eng":
            try:
                return tess.image_to_string(pil_image, lang="eng") or ""
            except Exception:
                return ""
        return ""


# ── Google Vision OCR ──────────────────────────────────────────────────────────

_vision_client = None
_vision_lock   = threading.Lock()


def _get_vision_client():
    """Lazy singleton — only initialised if Vision is actually needed."""
    global _vision_client
    gv = _import_vision()
    if not gv:
        return None
    with _vision_lock:
        if _vision_client is None:
            try:
                _vision_client = gv.ImageAnnotatorClient()
            except Exception:
                _vision_client = None
    return _vision_client


def _vision_ocr_page(pil_image) -> str:
    """Run Google Vision document_text_detection on a PIL image."""
    gv     = _import_vision()
    client = _get_vision_client()
    if not gv or not client or pil_image is None:
        return ""
    try:
        # Encode image to JPEG bytes
        import io
        buf = io.BytesIO()
        pil_image.save(buf, format="JPEG", quality=85)
        content = buf.getvalue()
        image   = gv.Image(content=content)
        resp    = client.document_text_detection(image=image)
        if resp.error.message:
            return ""
        return resp.full_text_annotation.text or ""
    except Exception:
        return ""


# ── Simple layout_elements builder ────────────────────────────────────────────

def _build_layout_elements(page_texts: dict, page_sizes: Optional[dict] = None) -> dict:
    """
    Build a minimal layout_elements.json compatible with reader.html.
    Each page gets a single 'text' element spanning the whole page.
    No ML-based segmentation — good enough for RAG and basic reader display.
    """
    result = {}
    ps     = page_sizes or {}

    for page_str, text in page_texts.items():
        if not text.strip():
            result[page_str] = []
            continue
        sz = ps.get(page_str, {"w": 612, "h": 792})  # US Letter fallback
        w, h = sz.get("w", 612), sz.get("h", 792)
        # Single element covering full page with modest margins
        result[page_str] = [{
            "label": "text",
            "text":  text,
            "bbox":  {"l": 0.05 * w, "t": 0.95 * h, "r": 0.95 * w, "b": 0.05 * h},
        }]

    # Attach page sizes so reader.html bbox normalisation works
    if ps:
        result["_page_sizes"] = ps

    return result


# ── Per-page size extraction via pypdfium2 ────────────────────────────────────

def _get_page_sizes(doc) -> dict:
    """Return {str(page_1_based): {"w": w, "h": h}} in PDF points."""
    sizes = {}
    try:
        for i in range(len(doc)):
            page = doc[i]
            w    = page.get_width()
            h    = page.get_height()
            sizes[str(i + 1)] = {"w": round(w, 1), "h": round(h, 1)}
    except Exception:
        pass
    return sizes


# ── Core: extract one document ────────────────────────────────────────────────

def extract_document(item: dict, use_vision: bool = False,
                     dry_run: bool = False, force: bool = False) -> dict:
    """
    Extract a single document.  Returns a result dict with keys:
      key, title, status ('ok'|'error'|'skip'|'dry_run'), method, pages, chars,
      text_quality, error (if failed)
    """
    key      = item["key"]
    title    = item.get("title", key)
    # Prefer staged path (data/pdfs/{key}.pdf) over original Zotero path
    pdf_path = (item.get("pdf_staged_path") or item.get("pdf_path") or "")
    # Resolve relative staged paths against repo root
    if pdf_path and not Path(pdf_path).is_absolute():
        pdf_path = str(_ROOT / pdf_path)
    doc_type = item.get("doc_type", "unknown")
    language = item.get("language") or "en"

    doc_dir   = TEXTS_ROOT / key
    pt_path   = doc_dir / "page_texts.json"

    # ── Resume check ────────────────────────────────────────────────────────────
    if not force and pt_path.exists():
        return {"key": key, "title": title, "status": "skip",
                "reason": "already extracted"}

    # ── Validate PDF path ────────────────────────────────────────────────────────
    if not pdf_path or not Path(pdf_path).exists():
        return {"key": key, "title": title, "status": "error",
                "error": f"PDF not found: {pdf_path}"}

    if dry_run:
        return {"key": key, "title": title, "status": "dry_run",
                "doc_type": doc_type}

    # ── Open PDF ─────────────────────────────────────────────────────────────────
    pdfium = _import_pypdfium2()
    if not pdfium:
        return {"key": key, "title": title, "status": "error",
                "error": "pypdfium2 not installed"}

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:
        return {"key": key, "title": title, "status": "error",
                "error": f"Cannot open PDF: {e}"}

    n_pages = len(pdf)
    page_texts: dict[str, str] = {}
    page_methods: dict[str, str] = {}  # track method used per page
    tess_lang = LANG_TESSERACT.get(language, DEFAULT_TESS_LANG)

    # ── Classify unknown docs ─────────────────────────────────────────────────
    if doc_type == "unknown":
        # Quick embedded-vs-scanned check: sample first 3 pages
        sample_chars = 0
        for i in range(min(3, n_pages)):
            sample_chars += len(_extract_page_embedded(pdf, i).strip())
        avg = sample_chars / min(3, n_pages) if n_pages else 0
        doc_type = "embedded" if avg >= SCANNED_THRESHOLD else "scanned"

    # ── Page-by-page extraction ────────────────────────────────────────────────
    for i in range(n_pages):
        page_num = str(i + 1)
        text     = ""
        method   = "none"

        if doc_type == "embedded":
            text   = _extract_page_embedded(pdf, i)
            method = "pypdfium2"

        # Fallback for scanned docs OR embedded pages that came out blank
        if len(text.strip()) < EMBEDDED_THRESHOLD:
            pil_img = _render_page_to_pil(pdf, i)
            if pil_img is not None:
                tess_text = _tesseract_ocr(pil_img, lang=tess_lang)
                if len(tess_text.strip()) >= EMBEDDED_THRESHOLD:
                    text   = tess_text
                    method = f"tesseract:{tess_lang}"
                elif use_vision:
                    vision_text = _vision_ocr_page(pil_img)
                    if vision_text.strip():
                        text   = vision_text
                        method = "vision"
                    else:
                        text   = tess_text or ""   # keep whatever we have
                        method = f"tesseract:{tess_lang}(low)"
                else:
                    text   = tess_text or ""
                    method = f"tesseract:{tess_lang}(low)" if tess_text else "none"

        page_texts[page_num] = text
        page_methods[page_num] = method

    pdf.close()

    # ── Compute quality + page sizes ──────────────────────────────────────────
    # Re-open briefly just for page sizes (avoids keeping the doc open)
    try:
        pdf2 = pdfium.PdfDocument(str(pdf_path))
        page_sizes = _get_page_sizes(pdf2)
        pdf2.close()
    except Exception:
        page_sizes = {}

    quality = _compute_quality(page_texts)

    # ── Write outputs ──────────────────────────────────────────────────────────
    doc_dir.mkdir(parents=True, exist_ok=True)

    pt_path.write_text(
        json.dumps(page_texts, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    le = _build_layout_elements(page_texts, page_sizes)
    (doc_dir / "layout_elements.json").write_text(
        json.dumps(le, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    meta_out = {
        "key":          key,
        "title":        title,
        "authors":      item.get("authors", ""),
        "year":         item.get("year", ""),
        "language":     language,
        "doc_type":     doc_type,
        "page_count":   n_pages,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "extraction_method": "05b_extract_robust",
        **quality,
    }
    (doc_dir / "meta.json").write_text(
        json.dumps(meta_out, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    total_chars = sum(len(t) for t in page_texts.values())
    methods_used = sorted(set(page_methods.values()))

    return {
        "key":          key,
        "title":        title,
        "status":       "ok",
        "doc_type":     doc_type,
        "pages":        n_pages,
        "chars":        total_chars,
        "methods":      methods_used,
        "text_quality": quality["text_quality"],
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Robust PDF extraction — pypdfium2 + Tesseract + optional Vision"
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="Classify docs without writing anything")
    parser.add_argument("--force",    action="store_true",
                        help="Re-process docs even if already extracted")
    parser.add_argument("--vision",   action="store_true",
                        help="Enable Google Vision API fallback for hard pages")
    parser.add_argument("--workers",  type=int, default=2,
                        help="Parallel worker threads (default 2; safe — no shared ML models)")
    parser.add_argument("--limit",    type=int, default=0,
                        help="Stop after N docs (0 = all)")
    parser.add_argument("--keys",     nargs="+", default=[],
                        help="Only process these doc keys")
    parser.add_argument("--inventory", default="data/inventory.json")
    args = parser.parse_args()

    inv_path  = _ROOT / args.inventory
    if not inv_path.exists():
        print(f"Inventory not found: {inv_path}")
        sys.exit(1)

    inventory = json.loads(inv_path.read_text("utf-8"))

    # ── Filter candidates ──────────────────────────────────────────────────────
    candidates = [
        r for r in inventory
        if r.get("pdf_status") in ("stored", "downloaded")
        and r.get("pdf_path")
    ]

    if args.keys:
        key_set    = set(args.keys)
        candidates = [r for r in candidates if r["key"] in key_set]

    if not args.force:
        # Skip docs that already have page_texts.json (fast check)
        candidates = [
            r for r in candidates
            if not (TEXTS_ROOT / r["key"] / "page_texts.json").exists()
        ]

    if args.limit:
        candidates = candidates[:args.limit]

    if not candidates:
        print("Nothing to do — all docs already extracted. Use --force to re-run.")
        return

    total = len(candidates)
    print(f"Docs to process: {total}")
    print(f"Dry run: {args.dry_run}  |  Vision: {args.vision}  |  Workers: {args.workers}")
    if args.dry_run:
        # Just show what would happen
        for item in candidates:
            print(f"  {item['key']}  [{item.get('doc_type','?')}]  {item.get('title','')[:60]}")
        return

    TEXTS_ROOT.mkdir(parents=True, exist_ok=True)
    status  = _load_status()
    results = _load_results()

    done_count  = 0
    ok_count    = 0
    err_count   = 0
    skip_count  = 0
    lock_print  = threading.Lock()

    def _process(item: dict) -> dict:
        key   = item["key"]
        title = item.get("title", key)

        with lock_print:
            print(f"  ⟳  {key}  {title[:55]}", flush=True)

        _update_status(status, key, "in_progress", title=title)

        t0     = time.time()
        result = extract_document(item, use_vision=args.vision,
                                  dry_run=args.dry_run, force=args.force)
        elapsed = int(time.time() - t0)

        result["elapsed_s"] = elapsed
        return result

    def _on_done(result: dict):
        nonlocal done_count, ok_count, err_count, skip_count
        key    = result["key"]
        title  = result.get("title", key)
        state  = result["status"]
        elapsed = result.get("elapsed_s", 0)

        if state == "ok":
            ok_count   += 1
            done_count += 1
            q  = result.get("text_quality", "?")
            icon = {"good": "✓", "suspect": "⚠", "garbled": "✗"}.get(q, "?")
            methods = ", ".join(result.get("methods", []))
            with lock_print:
                print(
                    f"  {icon}  [{done_count}/{total}] {key}  "
                    f"{result.get('chars',0):,} chars · {result.get('pages',0)} pp · "
                    f"{q} · {methods} · {elapsed}s"
                )
            _update_status(status, key, "ok",
                           title=title, chars=result.get("chars"),
                           pages=result.get("pages"),
                           text_quality=q, elapsed_s=elapsed)
        elif state == "skip":
            skip_count += 1
            _update_status(status, key, "skip", title=title)
        else:
            err_count  += 1
            done_count += 1
            err = result.get("error", "")
            with lock_print:
                print(f"  ✗  [{done_count}/{total}] {key}  ERROR: {err[:100]}")
            _update_status(status, key, "error", title=title, error=err)

        _append_result(results, result)

    # ── Parallel execution ─────────────────────────────────────────────────────
    print("=" * 60)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process, item): item for item in candidates}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                item   = futures[future]
                result = {"key": item["key"], "title": item.get("title", ""),
                          "status": "error", "error": str(exc)}
            _on_done(result)

    # ── Summary ────────────────────────────────────────────────────────────────
    q_counts: dict[str, int] = {}
    for r in results:
        if r.get("status") == "ok":
            q = r.get("text_quality", "unknown")
            q_counts[q] = q_counts.get(q, 0) + 1

    print("=" * 60)
    print(f"✓ Extracted:  {ok_count}")
    for q, n in sorted(q_counts.items()):
        icon = {"good": "✓", "suspect": "⚠", "garbled": "✗"}.get(q, "?")
        print(f"    {icon} {q}: {n}")
    print(f"✗ Errors:     {err_count}")
    print(f"– Skipped:    {skip_count} (already done)")
    print(f"\nTexts  → {TEXTS_ROOT}/")
    print(f"Status → {STATUS_PATH}")
    print(f"Log    → {RESULTS_PATH}")


if __name__ == "__main__":
    main()
