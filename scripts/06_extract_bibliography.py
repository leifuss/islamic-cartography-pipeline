#!/usr/bin/env python3
"""
Extract bibliographic metadata from each document using Claude Haiku.

For every document in data/texts/{KEY}/ that has a docling.md and page_texts.json,
this script runs three Haiku calls:

  Pass 1 — REFERENCES:  Find the bibliography / references section at the end of the
            document and extract structured reference entries.

  Pass 2 — CITATIONS:   Scan the full text for in-text citations
            (footnotes, endnotes, author-year, numbered superscripts) and extract them.

  Pass 3 — SUMMARY:     Produce a concise academic abstract + a chapter-level
            table of contents for the document.

Output per document:
  data/texts/{KEY}/bibliography.json
    {
      "key":       "QIGTV3FC",
      "title":     "Ptolemy",
      "refs":      [ { "id": "1", "raw": "...", "authors": [...], "year": "...",
                       "title": "...", "venue": "...", "pages": "..." }, … ],
      "citations": [ { "page": 3, "marker": "1", "context": "..." }, … ],
      "summary": {
        "abstract":  "Two-to-five sentence academic abstract …",
        "contents":  [ { "heading": "Chapter Six: Ptolemy", "page": 1 }, … ]
      }
    }

Usage:
  # Set ANTHROPIC_API_KEY in environment or data/.env
  python scripts/06_extract_bibliography.py
  python scripts/06_extract_bibliography.py --keys QIGTV3FC DUZKRZFQ
  python scripts/06_extract_bibliography.py --force          # overwrite existing
  python scripts/06_extract_bibliography.py --model claude-haiku-4-5   # override model
"""

import sys
import os
import json
import time
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

# ── Load .env so ANTHROPIC_API_KEY can live there ─────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env', override=True)
    load_dotenv(_ROOT / 'data' / '.env', override=True)
except ImportError:
    pass


# ── Anthropic client ──────────────────────────────────────────────────────────
def _get_client():
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic not installed — run: pip install anthropic")

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        sys.exit(
            "ANTHROPIC_API_KEY not set.\n"
            "Add it to your environment or to data/.env:\n"
            "  ANTHROPIC_API_KEY=sk-ant-..."
        )
    return anthropic.Anthropic(api_key=api_key)


# ── Text helpers ───────────────────────────────────────────────────────────────

def _tail_text(full_text: str, max_chars: int = 8000) -> str:
    """Return the last `max_chars` characters — where bibliographies live."""
    return full_text[-max_chars:] if len(full_text) > max_chars else full_text


def _head_text(full_text: str, max_chars: int = 4000) -> str:
    """Return the first `max_chars` characters — where TOC/intro lives."""
    return full_text[:max_chars]


def _chunk_pages(page_texts: dict, max_chars_per_chunk: int = 12000) -> list:
    """
    Return a list of (page_range_str, combined_text) chunks suitable for
    citation scanning.  Pages are sorted numerically.
    """
    sorted_pages = sorted(page_texts.items(), key=lambda kv: int(kv[0]))
    chunks = []
    current_pages = []
    current_text = ''
    for pg, txt in sorted_pages:
        if current_text and len(current_text) + len(txt) > max_chars_per_chunk:
            chunks.append((f"{current_pages[0]}-{current_pages[-1]}", current_text))
            current_pages = []
            current_text = ''
        current_pages.append(pg)
        current_text += f'\n\n[Page {pg}]\n{txt}'
    if current_pages:
        chunks.append((f"{current_pages[0]}-{current_pages[-1]}", current_text))
    return chunks


# ── Haiku call wrapper ─────────────────────────────────────────────────────────

def _haiku(client, model: str, system: str, user: str, max_tokens: int = 2048) -> str:
    """Single Haiku/Sonnet call; retries once on overload or rate-limit (429)."""
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        except Exception as exc:
            exc_str = str(exc).lower()
            is_retriable = 'overload' in exc_str or '529' in exc_str or '429' in exc_str
            if attempt == 0 and is_retriable:
                log.warning(f"API rate-limited/overloaded — waiting 15 s … ({exc})")
                time.sleep(15)
            else:
                raise
    return ''


# ── Pass 1: References ─────────────────────────────────────────────────────────

_REFS_SYSTEM = """
You are a bibliographic metadata extractor.  Given the end section of an
academic document, extract every reference / bibliography entry.

Return ONLY valid JSON — no prose, no markdown fences — matching this schema:
{
  "refs": [
    {
      "id":      "1",           // number or label as it appears in the text ("1", "Smi57", etc.)
      "raw":     "Smith 1957 …",// verbatim entry text
      "authors": ["Smith, J."],  // list of author strings
      "year":    "1957",
      "title":   "Article or book title",
      "venue":   "Journal / publisher / conference",
      "pages":   "123-145"      // omit key if not present
    }
  ]
}

If there is no bibliography section, return {"refs": []}.
Do not invent information. Use null for genuinely unknown fields.
""".strip()


def _salvage_json(raw: str) -> dict | None:
    """Extract the outermost JSON object from a response that may have prose wrapping it."""
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            pass
    return None


def extract_refs(client, model: str, full_text: str) -> list:
    tail = _tail_text(full_text, max_chars=10000)
    raw = _haiku(client, model, _REFS_SYSTEM,
                 f"DOCUMENT (last section):\n\n{tail}",
                 max_tokens=4096)
    try:
        return json.loads(raw).get('refs', [])
    except Exception:
        salvaged = _salvage_json(raw)
        if salvaged:
            return salvaged.get('refs', [])
        log.warning("  Refs parse failed — storing raw text")
        return [{"raw": raw, "parse_error": True}]


# ── Pass 2: In-text citations ──────────────────────────────────────────────────

_CITE_SYSTEM = """
You are a citation extractor for academic texts.  Given a numbered page range
from a document, identify every in-text citation (superscript numbers, footnote
markers, author-year references like "(Smith 1957)", or endnote markers).

Return ONLY valid JSON with this schema:
{
  "citations": [
    {
      "page":    3,            // integer page number
      "marker":  "1",          // the citation marker as it appears
      "context": "… words around the citation …"  // ≤30 words of surrounding text
    }
  ]
}

Only include citations that point to external sources (skip "ibid.", cross-refs
to other chapters, etc.).  If none found, return {"citations": []}.
""".strip()


def extract_citations(client, model: str, page_texts: dict) -> list:
    all_citations = []
    chunks = _chunk_pages(page_texts)
    for i, (page_range, chunk_text) in enumerate(chunks):
        if i > 0:
            time.sleep(1)   # brief pause between chunks to avoid rate-limit bursts
        raw = _haiku(client, model, _CITE_SYSTEM,
                     f"PAGES {page_range}:\n\n{chunk_text}",
                     max_tokens=2048)
        try:
            cites = json.loads(raw).get('citations', [])
            all_citations.extend(cites)
        except Exception:
            salvaged = _salvage_json(raw)
            if salvaged:
                all_citations.extend(salvaged.get('citations', []))
                continue
            log.warning(f"  Citation parse failed for pages {page_range}")
    return all_citations


# ── Pass 3: Summary & Table of Contents ───────────────────────────────────────

_SUMMARY_SYSTEM = """
You are an academic research assistant specialising in the history of cartography
and Islamic scholarship.

Given the text of a scholarly article or book chapter, produce:
1. A concise academic abstract (3–6 sentences) summarising the argument,
   sources, and conclusions.
2. A structured table of contents listing every major section heading and the
   approximate page on which it begins.

Return ONLY valid JSON — no prose, no markdown fences:
{
  "abstract": "…",
  "contents": [
    { "heading": "1. Introduction", "page": 1 },
    { "heading": "2. The Ptolemaic Tradition", "page": 5 }
  ]
}
""".strip()


def extract_summary(client, model: str, full_text: str,
                    page_texts: dict, layout_elements: dict) -> dict:
    """
    Build the best possible input for the summary call:
    • Prefer section_header elements from layout_elements (most reliable for TOC)
    • Fallback: first ~4 000 chars + last ~2 000 chars of full text
    """
    # Build a condensed version: section headers + first paragraph of each section
    condensed_parts = []
    section_headers = []

    sorted_pages = sorted(
        (k for k in layout_elements if k != '_page_sizes'),
        key=lambda x: int(x)
    )
    for pg_str in sorted_pages:
        items = layout_elements.get(pg_str, [])
        if not isinstance(items, list):
            continue
        for item in items:
            lbl = (item.get('label') or '').lower().replace('-', '_')
            txt = (item.get('text') or '').strip()
            if not txt:
                continue
            if lbl in ('section_header', 'title'):
                section_headers.append({'heading': txt, 'page': int(pg_str)})
                condensed_parts.append(f'\n## [{pg_str}] {txt}')
            elif lbl == 'text' and len(condensed_parts) > 0:
                # First text block after a header — append truncated
                condensed_parts.append(txt[:200])

    condensed = '\n'.join(condensed_parts)[:8000]

    # If condensed is thin, fall back to head+tail of full text
    if len(condensed) < 500:
        head = _head_text(full_text, 4000)
        tail = _tail_text(full_text, 2000)
        condensed = head + '\n\n[…]\n\n' + tail

    user_prompt = f"DOCUMENT OUTLINE:\n\n{condensed}"

    raw = _haiku(client, model, _SUMMARY_SYSTEM, user_prompt, max_tokens=2048)

    try:
        result = json.loads(raw)
        # If model didn't include contents but we have headers, merge them
        if not result.get('contents') and section_headers:
            result['contents'] = section_headers
        return result
    except Exception:
        salvaged = _salvage_json(raw)
        if salvaged:
            if not salvaged.get('contents') and section_headers:
                salvaged['contents'] = section_headers
            return salvaged
        log.warning("  Summary parse failed — using raw text")
        return {
            "abstract": raw[:2000],
            "contents": section_headers,
            "parse_error": True,
        }


# ── Per-document orchestrator ─────────────────────────────────────────────────

def process_doc(key: str, texts_dir: Path, client, model: str, force: bool) -> bool:
    doc_dir = texts_dir / key
    out_path = doc_dir / 'bibliography.json'

    if out_path.exists() and not force:
        log.info(f"  {key}: already done (--force to redo)")
        return True

    # Load inputs
    md_path   = doc_dir / 'docling.md'
    pt_path   = doc_dir / 'page_texts.json'
    le_path   = doc_dir / 'layout_elements.json'
    meta_path = doc_dir / 'meta.json'

    if not md_path.exists():
        log.warning(f"  {key}: no docling.md — skipping")
        return False

    full_text     = md_path.read_text(encoding='utf-8')
    page_texts    = json.loads(pt_path.read_text())   if pt_path.exists()  else {}
    layout_elements = json.loads(le_path.read_text()) if le_path.exists()  else {}
    meta          = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    title = meta.get('title', key)
    log.info(f"  {key}: {title!r}")

    # Pass 1 — references
    log.info("    → pass 1: references …")
    refs = extract_refs(client, model, full_text)
    log.info(f"       {len(refs)} refs found")

    # Pass 2 — in-text citations
    log.info("    → pass 2: citations …")
    citations = extract_citations(client, model, page_texts)
    log.info(f"       {len(citations)} citation markers found")

    # Pass 3 — summary + contents
    log.info("    → pass 3: summary …")
    summary = extract_summary(client, model, full_text, page_texts, layout_elements)
    log.info(f"       abstract: {len(summary.get('abstract',''))} chars  "
             f"| contents: {len(summary.get('contents',[]))} entries")

    result = {
        'key':       key,
        'title':     title,
        'refs':      refs,
        'citations': citations,
        'summary':   summary,
    }

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    log.info(f"    ✓ saved {out_path}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Extract bibliography, citations, summary, and TOC using Haiku.'
    )
    parser.add_argument('--texts-dir', default='data/texts',
                        help='Directory containing per-document text folders')
    parser.add_argument('--keys', nargs='+', default=[],
                        help='Only process these document keys (space-separated)')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing bibliography.json files')
    parser.add_argument('--model', default='claude-haiku-4-5',
                        help='Anthropic model to use (default: claude-haiku-4-5)')
    args = parser.parse_args()

    texts_dir = _ROOT / args.texts_dir
    if not texts_dir.exists():
        sys.exit(f"texts-dir not found: {texts_dir}")

    client = _get_client()

    # Discover docs
    all_keys = sorted(d.name for d in texts_dir.iterdir()
                      if d.is_dir() and (d / 'docling.md').exists())

    if args.keys:
        keys = [k for k in args.keys if k in set(all_keys)]
        missing = [k for k in args.keys if k not in set(all_keys)]
        if missing:
            log.warning(f"Keys not found in texts-dir: {missing}")
    else:
        keys = all_keys

    log.info(f"Model: {args.model}")
    log.info(f"Processing {len(keys)} document(s) …\n")

    ok = err = skipped = 0
    for key in keys:
        out_path = texts_dir / key / 'bibliography.json'
        if out_path.exists() and not args.force:
            skipped += 1
            log.info(f"  {key}: already done (use --force to redo)")
            continue
        try:
            success = process_doc(key, texts_dir, client, args.model, args.force)
            if success:
                ok += 1
            else:
                err += 1
        except Exception as exc:
            log.error(f"  {key}: FAILED — {exc}")
            err += 1

    print(f"\n{'='*60}")
    print(f"✓ Done: {ok}   ✗ Errors: {err}   – Skipped: {skipped}")
    print(f"Output: {texts_dir}/{{KEY}}/bibliography.json")


if __name__ == '__main__':
    main()
