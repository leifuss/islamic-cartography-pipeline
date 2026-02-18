#!/usr/bin/env python3
"""
Extract span-level italic/bold formatting from PDFs using pdfplumber.

Docling extracts text content but does not propagate font-style information
(italic, bold) from embedded PDFs.  This script fills that gap by reading each
PDF directly with pdfplumber, which gives per-character font names, and grouping
consecutive characters that share the same style into "spans".

The output is  data/texts/{KEY}/text_spans.json  with the structure:

  {
    "1": [                                     // 1-based page number
      {
        "text":   "Sermo Tertius De Opticis",  // span text
        "italic": true,
        "bold":   false,
        "super":  false,                       // superscript
        "sub":    false,                       // subscript
        "bbox":   {"l":90.1,"t":640.2,"r":260.3,"b":625.0}  // PDF pts, TOPLEFT origin
      },
      …
    ],
    …
  }

Only spans with at least one of italic/bold/super set to True are stored
(plain-roman text spans are omitted to keep the file small).

Foreign-term heuristic
─────────────────────
Arabic transliteration diacritics (ā ī ū ḥ ḍ ẓ ṣ ṭ ṯ ḏ ġ ḫ ʿ ʾ etc.)
are a strong signal that a word is a technical/foreign term.  Spans containing
these characters are tagged with  "foreign": true  regardless of font style.
This lets the reader italicise them even when the PDF did not use an italic font.

Usage:
  # From project root:
  python scripts/07_extract_spans.py
  python scripts/07_extract_spans.py --keys QIGTV3FC HMPTGZID
  python scripts/07_extract_spans.py --force        # overwrite existing files
"""

import sys
import json
import re
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

# ── Arabic-transliteration diacritic detector ─────────────────────────────────
# Catches ā ī ū ḥ ḍ ẓ ṣ ṭ ṯ ḏ ġ ḫ ẖ ḳ ñ etc.
_FOREIGN_CHARS_RE = re.compile(
    r'[āīūḥḍẓṣṭṯḏġḫḳʿʾʼʻˈˌ'
    r'\u0100-\u024F'   # Latin Extended-A and B (covers most transliteration chars)
    r'\u1E00-\u1EFF'   # Latin Extended Additional (ḥ ḍ etc.)
    r']'
)

def _has_foreign_chars(text: str) -> bool:
    """
    True only if the text is plausibly a foreign/transliterated term.

    Rules:
    - Must contain at least one transliteration/extended-Latin character.
    - If the span is long (> 60 chars or > 6 words) it must have a meaningful
      density of foreign characters (≥ 8 %) to avoid tagging stray English
      sentences that happen to contain a lone diacritic or non-ASCII punctuation.
    """
    matches = _FOREIGN_CHARS_RE.findall(text)
    if not matches:
        return False
    words = text.split()
    if len(text) > 60 or len(words) > 6:
        # Require at least 8 % of characters to be foreign/diacritic
        return len(matches) / max(len(text), 1) >= 0.08
    return True


# ── Font-name style detection ──────────────────────────────────────────────────

def _font_style(fontname: str) -> dict:
    """
    Infer italic/bold from font name.

    Naming conventions vary across foundries; we use substring matching:
      Italic | Ital | Oblique | Slanted  → italic
      Bold | Heavy | Black | ExtraBold   → bold
    """
    fn = fontname or ''
    italic = bool(re.search(r'Italic|Ital|Oblique|Slanted', fn, re.I))
    bold   = bool(re.search(r'Bold|Heavy|Black|ExtraBold|Semibold|Demi', fn, re.I))
    return {'italic': italic, 'bold': bold}


# ── Main extraction per PDF ────────────────────────────────────────────────────

def extract_spans_from_pdf(pdf_path: Path) -> dict:
    """
    Return {page_no_str: [span_dict, …]} for all pages.
    Only spans with italic=True OR bold=True OR foreign=True are returned.
    """
    try:
        import pdfplumber
    except ImportError:
        sys.exit("pdfplumber not installed — run: pip install pdfplumber")

    result = {}

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pg_idx, pg in enumerate(pdf.pages):
                page_no = pg_idx + 1  # 1-based
                chars = pg.chars

                if not chars:
                    continue

                # Group consecutive chars with same style into spans
                spans = []
                cur_text  = ''
                cur_style = None
                cur_x0    = None
                cur_y_top = None
                cur_y_bot = None
                cur_x1    = None

                def _flush():
                    if not cur_text.strip() or cur_style is None:
                        return
                    style = cur_style.copy()
                    style['foreign'] = _has_foreign_chars(cur_text)
                    if style['italic'] or style['bold'] or style['foreign']:
                        style['text'] = cur_text.strip()
                        style['bbox'] = {
                            'l': round(cur_x0,    2),
                            't': round(cur_y_top, 2),
                            'r': round(cur_x1,    2),
                            'b': round(cur_y_bot, 2),
                        }
                        spans.append(style)

                for c in chars:
                    fn    = c.get('fontname', '') or ''
                    style = _font_style(fn)
                    text  = c.get('text', '') or ''

                    if style == cur_style:
                        cur_text  += text
                        cur_x1     = c.get('x1', cur_x1)
                        # pdfplumber 'top' = distance from page-top (smaller = higher)
                        # pdfplumber 'bottom' = distance from page-top (larger = lower)
                        # Span bbox should enclose all characters: min top, max bottom
                        cur_y_top  = min(cur_y_top if cur_y_top is not None else 1e9,
                                         c.get('top',    1e9))
                        cur_y_bot  = max(cur_y_bot or 0, c.get('bottom', 0))
                    else:
                        _flush()
                        cur_text  = text
                        cur_style = style
                        cur_x0    = c.get('x0',    0)
                        cur_x1    = c.get('x1',    0)
                        cur_y_top = c.get('top',   0)
                        cur_y_bot = c.get('bottom',0)

                _flush()

                if spans:
                    result[str(page_no)] = spans

    except Exception as e:
        log.warning(f"pdfplumber failed on {pdf_path}: {e}")

    return result


# ── Inventory lookup ───────────────────────────────────────────────────────────

def _build_key_to_pdf(inventory_path: Path) -> dict:
    if not inventory_path.exists():
        return {}
    inv = json.loads(inventory_path.read_text())
    return {item['key']: item.get('pdf_path', '') for item in inv}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Extract italic/bold/foreign spans from PDFs using pdfplumber.'
    )
    parser.add_argument('--texts-dir', default='data/texts')
    parser.add_argument('--inventory', default='data/inventory.json')
    parser.add_argument('--keys', nargs='+', default=[],
                        help='Only process these document keys')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing text_spans.json files')
    args = parser.parse_args()

    texts_dir     = _ROOT / args.texts_dir
    inventory_path = _ROOT / args.inventory

    key_to_pdf = _build_key_to_pdf(inventory_path)

    all_keys = sorted(d.name for d in texts_dir.iterdir()
                      if d.is_dir() and (d / 'docling.md').exists())
    keys = [k for k in args.keys if k in set(all_keys)] if args.keys else all_keys
    if args.keys:
        missing = [k for k in args.keys if k not in set(all_keys)]
        if missing:
            log.warning(f"Keys not in texts-dir: {missing}")

    log.info(f"Processing {len(keys)} document(s) …")

    ok = err = skipped = 0
    for key in keys:
        out_path = texts_dir / key / 'text_spans.json'
        if out_path.exists() and not args.force:
            skipped += 1
            log.info(f"  {key}: already done")
            continue

        pdf_path_str = key_to_pdf.get(key, '')
        if not pdf_path_str:
            log.warning(f"  {key}: no PDF path in inventory")
            err += 1
            continue

        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists():
            log.warning(f"  {key}: PDF not found at {pdf_path}")
            err += 1
            continue

        try:
            spans = extract_spans_from_pdf(pdf_path)
            total = sum(len(v) for v in spans.values())
            out_path.write_text(json.dumps(spans, ensure_ascii=False, indent=2))
            log.info(f"  {key}: {len(spans)} pages with formatting, {total} spans → {out_path.name}")
            ok += 1
        except Exception as exc:
            log.error(f"  {key}: FAILED — {exc}")
            err += 1

    print(f"\n{'='*60}")
    print(f"✓ Done: {ok}   ✗ Errors: {err}   – Skipped: {skipped}")
    print(f"Output: {texts_dir}/{{KEY}}/text_spans.json")


if __name__ == '__main__':
    main()
