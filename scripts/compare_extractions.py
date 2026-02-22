#!/usr/bin/env python3
"""
Compare extraction methods side-by-side on selected documents.

For each document:
  1. Load existing extraction (page_texts.json) as the Docling baseline
  2. Run Tesseract on the PDF (per-page OCR at 300 DPI)
  3. Optionally run Google Vision on sampled pages
  4. Compute page-by-page and overall similarity between all method pairs
  5. Flag pages where methods disagree significantly

Default: runs on the 4 test subjects in the islamic-cartography collection.

Output:
  data/collections/{slug}/comparison_report.json — machine-readable
  data/collections/{slug}/comparison_report.html — visual side-by-side

Usage:
    python scripts/compare_extractions.py
    python scripts/compare_extractions.py --keys 23K87F66 QVUQC6HN
    python scripts/compare_extractions.py --collection-slug islamic-cartography
    python scripts/compare_extractions.py --pages 1-5
    python scripts/compare_extractions.py --no-vision
    python scripts/compare_extractions.py --no-tesseract   # only compare existing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / "src")
sys.path.insert(0, _SRC)

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from quality.similarity import compute_similarity, strip_markdown

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_COLLECTION = "islamic-cartography"
TEST_SUBJECTS      = ["23K87F66", "QVUQC6HN", "W277BB43", "CR7CQJJ8"]
COLLECTIONS_PATH   = _ROOT / "data" / "collections.json"

# Tesseract language codes (mirror of 05b)
LANG_TESSERACT = {
    "ar": "ara", "fa": "fas", "tr": "tur",
    "de": "deu", "fr": "fra", "en": "eng",
}

# Similarity thresholds for flagging
FLAG_THRESHOLD  = 0.70   # per-page similarity below this is "disagreement"
WARN_THRESHOLD  = 0.85   # below this is "minor disagreement"


# ── Path helpers ──────────────────────────────────────────────────────────────

def get_collection_base(slug: str) -> Path:
    if not COLLECTIONS_PATH.exists():
        raise SystemExit("ERROR: data/collections.json not found")
    with open(COLLECTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for c in data.get("collections", []):
        if c["slug"] == slug:
            path = c.get("path", slug)
            return _ROOT / "data" if path == "." else _ROOT / "data" / path
    raise SystemExit(f"ERROR: collection slug {slug!r} not found")


# ── Lazy extractor imports ────────────────────────────────────────────────────

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


# ── Tesseract extraction (per-page) ──────────────────────────────────────────

def run_tesseract(pdf_path: Path, lang: str = "eng",
                  page_range: Optional[tuple[int, int]] = None) -> dict[str, str]:
    """
    Run Tesseract OCR on each page of a PDF.

    Returns: {page_str (1-based): text}
    """
    pdfium = _import_pypdfium2()
    tess   = _import_pytesseract()
    Image  = _import_pil()
    if not pdfium or not tess or not Image:
        return {}

    doc     = pdfium.PdfDocument(str(pdf_path))
    n_pages = len(doc)
    start   = (page_range[0] - 1) if page_range else 0
    end     = min(page_range[1], n_pages) if page_range else n_pages

    results = {}
    for i in range(start, end):
        page_num = str(i + 1)
        try:
            bitmap = doc[i].render(scale=300 / 72)  # 300 DPI
            pil_img = bitmap.to_pil()
            text = tess.image_to_string(pil_img, lang=lang, config="--psm 3")
            results[page_num] = text or ""
        except Exception as e:
            results[page_num] = f"[ERROR: {e}]"
    doc.close()
    return results


# ── Vision extraction (sampled pages) ────────────────────────────────────────

def run_vision(pdf_path: Path, n_samples: int = 3,
               page_range: Optional[tuple[int, int]] = None) -> dict[str, str]:
    """
    Run Google Vision on a spread of pages.

    Returns: {page_str (1-based): text}
    """
    pdfium = _import_pypdfium2()
    Image  = _import_pil()
    try:
        from google.cloud import vision as gv
    except ImportError:
        return {}

    if not pdfium or not Image:
        return {}

    doc     = pdfium.PdfDocument(str(pdf_path))
    n_pages = len(doc)
    start   = (page_range[0] - 1) if page_range else 0
    end     = min(page_range[1], n_pages) if page_range else n_pages
    total   = end - start

    # Spread sampling across the active range
    if total <= n_samples:
        indices = list(range(start, end))
    else:
        step = (total - 1) / (n_samples - 1)
        indices = sorted(set(start + round(i * step) for i in range(n_samples)))

    try:
        client = gv.ImageAnnotatorClient()
    except Exception:
        doc.close()
        return {}

    import io
    results = {}
    for idx in indices:
        page_num = str(idx + 1)
        try:
            bitmap  = doc[idx].render(scale=300 / 72)
            pil_img = bitmap.to_pil()
            buf     = io.BytesIO()
            pil_img.save(buf, format="PNG")
            image    = gv.Image(content=buf.getvalue())
            response = client.text_detection(image=image)
            if response.text_annotations:
                results[page_num] = response.text_annotations[0].description
            else:
                results[page_num] = ""
        except Exception as e:
            results[page_num] = f"[ERROR: {e}]"
    doc.close()
    return results


# ── Comparison logic ──────────────────────────────────────────────────────────

def compare_pages(method_a: dict[str, str], method_b: dict[str, str],
                  name_a: str, name_b: str) -> dict:
    """
    Compare two page-text dicts page by page.

    Returns comparison dict with per-page scores, overall score, and flagged pages.
    """
    # Only compare pages present in both
    common_pages = sorted(set(method_a) & set(method_b), key=lambda x: int(x))

    if not common_pages:
        return {
            "pair": f"{name_a}_vs_{name_b}",
            "pages_compared": 0,
            "overall_similarity": 0.0,
            "per_page": {},
            "flagged_pages": [],
        }

    per_page = {}
    flagged  = []
    scores   = []

    for pg in common_pages:
        text_a = method_a[pg]
        text_b = method_b[pg]

        # Skip error markers
        if text_a.startswith("[ERROR") or text_b.startswith("[ERROR"):
            per_page[pg] = {"similarity": 0.0, "error": True}
            continue

        # Skip pages where both are empty/near-empty
        if len(text_a.strip()) < 20 and len(text_b.strip()) < 20:
            per_page[pg] = {"similarity": 1.0, "both_empty": True}
            scores.append(1.0)
            continue

        sim = compute_similarity(text_a, text_b)
        scores.append(sim)

        entry = {
            "similarity": round(sim, 4),
            "chars_a": len(text_a),
            "chars_b": len(text_b),
        }
        per_page[pg] = entry

        if sim < FLAG_THRESHOLD:
            flagged.append({
                "page": pg,
                "similarity": round(sim, 4),
                "level": "disagreement",
                "chars_a": len(text_a),
                "chars_b": len(text_b),
            })
        elif sim < WARN_THRESHOLD:
            flagged.append({
                "page": pg,
                "similarity": round(sim, 4),
                "level": "minor",
                "chars_a": len(text_a),
                "chars_b": len(text_b),
            })

    overall = sum(scores) / len(scores) if scores else 0.0

    return {
        "pair": f"{name_a}_vs_{name_b}",
        "pages_compared": len(common_pages),
        "overall_similarity": round(overall, 4),
        "per_page": per_page,
        "flagged_pages": flagged,
    }


def text_stats(page_texts: dict[str, str]) -> dict:
    """Summary statistics for a page-text dict."""
    chars = [len(t) for t in page_texts.values() if not t.startswith("[ERROR")]
    return {
        "total_chars": sum(chars),
        "pages_with_text": sum(1 for c in chars if c > 50),
        "total_pages": len(page_texts),
        "avg_chars_per_page": round(sum(chars) / len(chars), 1) if chars else 0,
        "min_chars_page": min(chars) if chars else 0,
        "max_chars_page": max(chars) if chars else 0,
    }


# ── HTML report generator ────────────────────────────────────────────────────

def generate_html_report(report: dict, output_path: Path) -> None:
    """Generate a visual HTML comparison report."""
    docs = report["documents"]

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Extraction Comparison Report</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; font-size: 13px;
         background: #f5f5f7; color: #1d1d1f; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 8px; }
  .meta { font-size: 12px; color: #6e6e73; margin-bottom: 24px; }
  .doc { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 20px;
         box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .doc h2 { font-size: 16px; margin-bottom: 4px; }
  .doc .subtitle { font-size: 12px; color: #6e6e73; margin-bottom: 16px; }
  .methods { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
  .method-card { flex: 1; min-width: 180px; padding: 12px; border-radius: 8px;
                 background: #f5f5f7; border: 1px solid #e0e0e5; }
  .method-card h3 { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .method-card .stat { font-size: 12px; color: #444; margin: 2px 0; }
  .comparisons { margin-top: 12px; }
  .pair { margin-bottom: 12px; }
  .pair-header { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
  .pair-score { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  .pair-score.high { color: #1a7f37; }
  .pair-score.medium { color: #b45309; }
  .pair-score.low { color: #c0392b; }
  .page-grid { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
  .page-cell { width: 28px; height: 28px; border-radius: 4px; display: flex;
               align-items: center; justify-content: center; font-size: 10px;
               font-weight: 600; color: #fff; cursor: default; }
  .page-cell.good { background: #34c759; }
  .page-cell.warn { background: #ff9f0a; }
  .page-cell.bad  { background: #ff3b30; }
  .page-cell.na   { background: #d1d1d6; color: #666; }
  .flags { margin-top: 12px; }
  .flag { font-size: 12px; padding: 6px 10px; background: #fff3e0; border-radius: 6px;
          border-left: 3px solid #ff9f0a; margin-bottom: 4px; }
  .flag.severe { background: #fde8e8; border-left-color: #ff3b30; }
  .summary { background: #e8f0fe; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
  .summary h2 { font-size: 16px; margin-bottom: 12px; }
  .summary .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
  .summary .stat-box { background: #fff; padding: 12px; border-radius: 8px; }
  .summary .stat-label { font-size: 11px; color: #6e6e73; text-transform: uppercase;
                          letter-spacing: .04em; margin-bottom: 4px; }
  .summary .stat-value { font-size: 24px; font-weight: 700; }
</style>
</head>
<body>
"""

    summary = report["summary"]
    html += f'<h1>Extraction Comparison Report</h1>\n'
    html += f'<div class="meta">Generated {report["generated_at"]} &middot; '
    html += f'{summary["documents_compared"]} documents compared</div>\n'

    # Summary
    html += '<div class="summary"><h2>Summary</h2><div class="grid">\n'
    html += f'<div class="stat-box"><div class="stat-label">Documents</div>'
    html += f'<div class="stat-value">{summary["documents_compared"]}</div></div>\n'
    html += f'<div class="stat-box"><div class="stat-label">Avg Similarity</div>'
    avg = summary.get("avg_similarity", 0)
    cls = "high" if avg >= 0.85 else ("medium" if avg >= 0.70 else "low")
    html += f'<div class="stat-value pair-score {cls}">{avg:.1%}</div></div>\n'
    html += f'<div class="stat-box"><div class="stat-label">Flagged Pages</div>'
    html += f'<div class="stat-value">{summary.get("total_flagged_pages", 0)}</div></div>\n'

    if summary.get("worst_page"):
        wp = summary["worst_page"]
        html += f'<div class="stat-box"><div class="stat-label">Worst Page</div>'
        html += f'<div class="stat-value pair-score low">{wp["key"]} p.{wp["page"]}: {wp["score"]:.0%}</div></div>\n'

    html += '</div></div>\n'

    # Per document
    for doc in docs:
        html += f'<div class="doc">\n'
        html += f'<h2>{_esc(doc["title"])}</h2>\n'
        html += f'<div class="subtitle">{_esc(doc["key"])} &middot; '
        html += f'{doc.get("page_count", "?")} pages &middot; '
        html += f'lang: {_esc(str(doc.get("language", "?")))}</div>\n'

        # Method cards
        html += '<div class="methods">\n'
        for name, stats in doc.get("methods", {}).items():
            html += f'<div class="method-card"><h3>{_esc(name)}</h3>\n'
            html += f'<div class="stat">{stats.get("total_chars", 0):,} chars</div>\n'
            html += f'<div class="stat">{stats.get("pages_with_text", 0)}/{stats.get("total_pages", 0)} pages with text</div>\n'
            html += f'<div class="stat">{stats.get("avg_chars_per_page", 0):.0f} avg chars/page</div>\n'
            html += '</div>\n'
        html += '</div>\n'

        # Comparisons
        html += '<div class="comparisons">\n'
        for comp in doc.get("comparisons", []):
            sim = comp["overall_similarity"]
            cls = "high" if sim >= 0.85 else ("medium" if sim >= 0.70 else "low")
            html += f'<div class="pair">\n'
            html += f'<div class="pair-header">{_esc(comp["pair"])}</div>\n'
            html += f'<div class="pair-score {cls}">{sim:.1%}</div>\n'
            html += f'<div style="font-size:11px;color:#6e6e73">{comp["pages_compared"]} pages compared</div>\n'

            # Page grid
            html += '<div class="page-grid">\n'
            for pg, info in sorted(comp.get("per_page", {}).items(), key=lambda x: int(x[0])):
                s = info.get("similarity", 0)
                if info.get("both_empty"):
                    html += f'<div class="page-cell na" title="p.{pg}: both empty">{pg}</div>\n'
                elif info.get("error"):
                    html += f'<div class="page-cell bad" title="p.{pg}: error">{pg}</div>\n'
                elif s >= 0.85:
                    html += f'<div class="page-cell good" title="p.{pg}: {s:.0%}">{pg}</div>\n'
                elif s >= 0.70:
                    html += f'<div class="page-cell warn" title="p.{pg}: {s:.0%}">{pg}</div>\n'
                else:
                    html += f'<div class="page-cell bad" title="p.{pg}: {s:.0%}">{pg}</div>\n'
            html += '</div>\n'

            # Flags
            if comp["flagged_pages"]:
                html += '<div class="flags">\n'
                for flag in comp["flagged_pages"]:
                    sev = "severe" if flag["level"] == "disagreement" else ""
                    html += f'<div class="flag {sev}">p.{flag["page"]}: '
                    html += f'{flag["similarity"]:.0%} similarity '
                    html += f'({flag["chars_a"]:,} vs {flag["chars_b"]:,} chars)</div>\n'
                html += '</div>\n'

            html += '</div>\n'
        html += '</div>\n'
        html += '</div>\n'

    html += '</body>\n</html>\n'
    output_path.write_text(html, encoding="utf-8")


def _esc(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare extraction methods side-by-side"
    )
    parser.add_argument("--collection-slug", default=DEFAULT_COLLECTION,
                        help=f"Collection slug (default: {DEFAULT_COLLECTION})")
    parser.add_argument("--keys", nargs="+", default=None,
                        help="Document keys to compare (default: 4 test subjects)")
    parser.add_argument("--pages", default=None,
                        help="Page range, e.g. '1-5' (default: all pages)")
    parser.add_argument("--no-tesseract", action="store_true",
                        help="Skip Tesseract re-extraction")
    parser.add_argument("--no-vision", action="store_true",
                        help="Skip Google Vision extraction")
    parser.add_argument("--vision-samples", type=int, default=3,
                        help="Number of pages to sample for Vision (default: 3)")
    parser.add_argument("--no-html", action="store_true",
                        help="Skip HTML report generation")
    args = parser.parse_args()

    keys = args.keys or TEST_SUBJECTS

    # Parse page range
    page_range = None
    if args.pages:
        parts = args.pages.split("-")
        page_range = (int(parts[0]), int(parts[1]) if len(parts) > 1 else int(parts[0]))

    base = get_collection_base(args.collection_slug)
    texts_dir = base / "texts"
    pdfs_dir  = base / "pdfs"
    inv_path  = base / "inventory.json"

    # Load inventory for metadata
    inventory = {}
    if inv_path.exists():
        with open(inv_path, encoding="utf-8") as f:
            for item in json.load(f):
                inventory[item["key"]] = item

    # Check available extractors
    tess_available   = not args.no_tesseract and _import_pytesseract() is not None
    vision_available = not args.no_vision

    print(f"Collection: {args.collection_slug}")
    print(f"Documents:  {', '.join(keys)}")
    print(f"Tesseract:  {'yes' if tess_available else 'no (skipped or unavailable)'}")
    print(f"Vision:     {'yes' if vision_available else 'no (skipped)'}")
    if page_range:
        print(f"Pages:      {page_range[0]}-{page_range[1]}")
    print("=" * 60)

    documents = []
    all_flagged = 0

    for key in keys:
        item_meta = inventory.get(key, {})
        title     = item_meta.get("title", key)
        language  = item_meta.get("language", "en")
        if isinstance(language, list):
            language = language[0] if language else "en"

        print(f"\n{key}: {title[:60]}")
        print("-" * 60)

        # ── Load existing extraction ─────────────────────────────────────────
        pt_path = texts_dir / key / "page_texts.json"
        if not pt_path.exists():
            print(f"  SKIP: no page_texts.json found")
            continue

        with open(pt_path, encoding="utf-8") as f:
            existing_pages = json.load(f)

        # Determine the extraction method from meta.json
        meta_path = texts_dir / key / "meta.json"
        existing_method = "docling"
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
                existing_method = meta.get("extraction_method", "docling")

        methods = {existing_method: existing_pages}
        print(f"  {existing_method}: {len(existing_pages)} pages, "
              f"{sum(len(t) for t in existing_pages.values()):,} chars")

        # ── Find PDF ─────────────────────────────────────────────────────────
        pdf_path = None
        if item_meta.get("pdf_path"):
            p = Path(item_meta["pdf_path"])
            if not p.is_absolute():
                p = _ROOT / p
            if p.exists():
                pdf_path = p

        if not pdf_path:
            # Try common locations
            for candidate in [
                pdfs_dir / f"{key}.pdf",
                pdfs_dir / key,
            ]:
                if candidate.is_file():
                    pdf_path = candidate
                    break
                elif candidate.is_dir():
                    pdfs = list(candidate.glob("*.pdf"))
                    if pdfs:
                        pdf_path = pdfs[0]
                        break

        if not pdf_path:
            print(f"  WARNING: no PDF found, skipping re-extraction")
            tess_available_for_doc   = False
            vision_available_for_doc = False
        else:
            tess_available_for_doc   = tess_available
            vision_available_for_doc = vision_available
            print(f"  PDF: {pdf_path.name}")

        # ── Run Tesseract ────────────────────────────────────────────────────
        if tess_available_for_doc:
            tess_lang = LANG_TESSERACT.get(language, "eng")
            print(f"  Running Tesseract (lang={tess_lang})...", end="", flush=True)
            t0 = time.time()
            tess_pages = run_tesseract(pdf_path, lang=tess_lang, page_range=page_range)
            elapsed = time.time() - t0
            if tess_pages:
                methods["tesseract"] = tess_pages
                chars = sum(len(t) for t in tess_pages.values())
                print(f" {len(tess_pages)} pages, {chars:,} chars [{elapsed:.0f}s]")
            else:
                print(f" failed [{elapsed:.0f}s]")

        # ── Run Vision ───────────────────────────────────────────────────────
        if vision_available_for_doc:
            print(f"  Running Vision ({args.vision_samples} samples)...", end="", flush=True)
            t0 = time.time()
            vision_pages = run_vision(pdf_path, n_samples=args.vision_samples,
                                      page_range=page_range)
            elapsed = time.time() - t0
            if vision_pages:
                methods["vision"] = vision_pages
                chars = sum(len(t) for t in vision_pages.values())
                print(f" {len(vision_pages)} pages, {chars:,} chars [{elapsed:.0f}s]")
            else:
                print(f" skipped (unavailable) [{elapsed:.0f}s]")

        # ── Compare all pairs ────────────────────────────────────────────────
        method_names = list(methods.keys())
        comparisons  = []

        if len(method_names) < 2:
            print(f"  Only one method available — no comparison possible")

        for i, name_a in enumerate(method_names):
            for name_b in method_names[i + 1:]:
                comp = compare_pages(methods[name_a], methods[name_b], name_a, name_b)
                comparisons.append(comp)
                sim = comp["overall_similarity"]
                n_flags = len(comp["flagged_pages"])
                all_flagged += n_flags
                icon = "+" if sim >= 0.85 else ("~" if sim >= 0.70 else "!")
                print(f"  {icon} {comp['pair']}: {sim:.1%} overall, "
                      f"{comp['pages_compared']} pages, {n_flags} flagged")

        doc_result = {
            "key":         key,
            "title":       title,
            "language":    language,
            "page_count":  len(existing_pages),
            "methods":     {name: text_stats(pages) for name, pages in methods.items()},
            "comparisons": comparisons,
        }
        documents.append(doc_result)

    # ── Build summary ─────────────────────────────────────────────────────────
    all_sims = []
    worst_page = None
    for doc in documents:
        for comp in doc["comparisons"]:
            all_sims.append(comp["overall_similarity"])
            for flag in comp["flagged_pages"]:
                if worst_page is None or flag["similarity"] < worst_page["score"]:
                    worst_page = {
                        "key":   doc["key"],
                        "page":  flag["page"],
                        "score": flag["similarity"],
                        "pair":  comp["pair"],
                    }

    summary = {
        "documents_compared": len(documents),
        "avg_similarity":     round(sum(all_sims) / len(all_sims), 4) if all_sims else 0,
        "total_flagged_pages": all_flagged,
        "worst_page":         worst_page,
    }

    report = {
        "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
        "collection":    args.collection_slug,
        "keys":          keys,
        "page_range":    args.pages,
        "summary":       summary,
        "documents":     documents,
    }

    # ── Save JSON report ──────────────────────────────────────────────────────
    json_path = base / "comparison_report.json"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n{'=' * 60}")
    print(f"JSON report: {json_path}")

    # ── Save HTML report ──────────────────────────────────────────────────────
    if not args.no_html:
        html_path = base / "comparison_report.html"
        generate_html_report(report, html_path)
        print(f"HTML report: {html_path}")

    # ── Print summary ─────────────────────────────────────────────────────────
    print(f"\nSummary:")
    print(f"  Documents:    {summary['documents_compared']}")
    print(f"  Avg similarity: {summary['avg_similarity']:.1%}")
    print(f"  Flagged pages:  {summary['total_flagged_pages']}")
    if worst_page:
        print(f"  Worst page:     {worst_page['key']} p.{worst_page['page']} "
              f"({worst_page['score']:.0%}, {worst_page['pair']})")


if __name__ == "__main__":
    main()
