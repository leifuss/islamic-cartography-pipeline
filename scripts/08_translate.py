#!/usr/bin/env python3
"""
Translate non-English extracted documents to English using Claude Opus.

For each extracted doc whose language is not English, translates:
  - page_texts.json   → translation.json["page_texts"]
  - layout_elements   → translation.json["elements"]

Uses a single Opus call per page (combining page text + elements) for
efficiency and consistency.  Foreign words from the *source* language
perspective (Arabic transliterations, Greek terms, Hebrew, proper names)
are NOT translated.

Output: data/texts/{KEY}/translation.json
  {
    "key": "...",
    "source_language": "es",
    "target_language": "en",
    "model": "...",
    "translated_at": "...",
    "page_texts": {"1": "...", "2": "...", ...},
    "elements":   {"1": [{"text":"...", "label":"..."}, ...], ...}
  }

Usage:
  python scripts/08_translate.py
  python scripts/08_translate.py --keys QVUQC6HN MJEJY7UC W277BB43
  python scripts/08_translate.py --force        # overwrite existing files
  python scripts/08_translate.py --model claude-sonnet-4-5   # cheaper model
"""

import sys
import os
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent

# ── Prompts ────────────────────────────────────────────────────────────────────

_DETECT_SYSTEM = (
    "You are a language detector. Given a short text sample, "
    "respond with ONLY a single ISO 639-1 language code "
    "(e.g. 'en', 'de', 'fr', 'es', 'ar', 'it', 'ru'). "
    "No explanation, no punctuation — just the code."
)

_TRANSLATE_SYSTEM = """\
You are an expert academic translator specialising in Islamic history, \
cartography, historical geography, and medieval studies.

Translate the CURRENT PAGE JSON from {source_lang} to English.

STRICT RULES — violations will break the reader application:
1. Return ONLY valid JSON in the exact schema shown — no prose, no code fences.
2. Keep Arabic transliterations EXACTLY as written: words containing ā ī ū ḥ ḍ ẓ ṣ ṭ \
ṯ ḏ ġ ḫ ʿ ʾ and similar diacritics (these are foreign terms in {source_lang} too).
3. Keep personal names, place names, and titles of works unchanged.
4. Keep any text already in English unchanged.
5. Keep Greek, Hebrew, Arabic-script, and other non-Latin text unchanged.
6. Keep footnote/endnote number markers (e.g. "1 .", "²") in position.
7. Preserve markdown line breaks, paragraph breaks, and emphasis markers.
8. Translate the "page_text" as a coherent flowing text.
9. Translate each element "text" independently but consistently with the page text.
10. The "elements" array in the output MUST have the same length as the input "elements" \
array — one translated entry per input entry, matched by "n".
11. The CONTEXT blocks are provided in the source language for reference only — \
do NOT translate them and do NOT include them in the output. Use them solely to \
understand sentence continuity at page boundaries (a sentence may begin at the \
end of the previous page or conclude at the start of the next).
"""

_TRANSLATE_USER = """\
Translate the CURRENT PAGE from {source_lang} to English.
{prev_block}{next_block}
CURRENT PAGE (translate this):
{payload}

Return ONLY this JSON structure (no other text):
{{
  "page_text": "<translated page text>",
  "elements": [
    {{"n": 0, "text": "<translated text>"}},
    {{"n": 1, "text": "<translated text>"}},
    ...
  ]
}}"""

# How many characters of adjacent pages to include as boundary context
_CONTEXT_CHARS = 600


# ── Helpers ────────────────────────────────────────────────────────────────────

def _api_client():
    """Return an Anthropic client, loading .env if needed."""
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic package not installed — run: pip install anthropic")

    # Try loading API key from project .env files (override empty values)
    for env_path in [_ROOT / '.env', _ROOT / 'data' / '.env']:
        if env_path.exists() and not os.environ.get('ANTHROPIC_API_KEY'):
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith('ANTHROPIC_API_KEY='):
                    os.environ['ANTHROPIC_API_KEY'] = line.split('=', 1)[1].strip().strip('"\'')
                    break

    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        sys.exit(
            "ANTHROPIC_API_KEY not set.\n"
            "Add it to data/.env:  ANTHROPIC_API_KEY=sk-ant-...\n"
            "Or set it as an environment variable."
        )
    return anthropic.Anthropic(api_key=key)


def _salvage_json(raw: str) -> dict | None:
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            pass
    return None


def _call(client, model: str, system: str, user: str,
          max_tokens: int = 4096, retries: int = 3) -> str:
    """Single API call with retries on rate-limit / overload."""
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        except Exception as exc:
            s = str(exc).lower()
            if attempt < retries - 1 and ('overload' in s or '429' in s or '529' in s):
                wait = 20 * (attempt + 1)
                log.warning(f"  API throttled (attempt {attempt+1}) — waiting {wait}s…")
                time.sleep(wait)
            else:
                raise
    return ''


def detect_language(client, model_fast: str, text_sample: str) -> str:
    """Return ISO 639-1 code for the dominant language in text_sample."""
    sample = text_sample[:800].strip()
    try:
        raw = _call(client, model_fast, _DETECT_SYSTEM,
                    f"Detect the language of this text:\n\n{sample}",
                    max_tokens=10, retries=2)
        lang = raw.strip().lower().split()[0][:5].rstrip('.,;:')
        return lang if len(lang) == 2 else 'unknown'
    except Exception as e:
        log.warning(f"  Language detection failed: {e}")
        return 'unknown'


def translate_page(client, model: str, source_lang: str,
                   page_text: str, elements: list,
                   prev_context: str = '', next_context: str = '') -> tuple[str, list]:
    """
    Translate page_text and a list of element dicts {label, text} in one call.
    prev_context / next_context: raw source-language tails/heads of adjacent
    pages, passed as read-only context to handle cross-boundary sentences.
    Returns (translated_page_text, [{label, text, ...original fields}, ...]).
    """
    # Build compact input payload
    el_in = [{'n': i, 'label': e.get('label', ''), 'text': e.get('text', '')}
             for i, e in enumerate(elements)]
    payload = json.dumps({
        'page_text': page_text,
        'elements':  el_in,
    }, ensure_ascii=False)

    # Build optional context blocks
    prev_block = (
        f"\n[END OF PREVIOUS PAGE — context only, do not translate or include in output:]\n"
        f"…{prev_context.strip()}\n"
        if prev_context.strip() else ''
    )
    next_block = (
        f"\n[START OF NEXT PAGE — context only, do not translate or include in output:]\n"
        f"{next_context.strip()}…\n"
        if next_context.strip() else ''
    )

    system = _TRANSLATE_SYSTEM.format(source_lang=source_lang)
    user   = _TRANSLATE_USER.format(
        source_lang=source_lang,
        payload=payload,
        prev_block=prev_block,
        next_block=next_block,
    )

    try:
        raw = _call(client, model, system, user, max_tokens=8192)
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = _salvage_json(raw) or {}

    translated_pt = result.get('page_text', page_text)

    # Rebuild elements preserving all original fields, only replacing text
    translated_els = list(elements)  # copy
    for item in result.get('elements', []):
        n = item.get('n')
        if isinstance(n, int) and 0 <= n < len(translated_els):
            orig = dict(translated_els[n])
            orig['text'] = item.get('text', orig.get('text', ''))
            translated_els[n] = orig

    return translated_pt, translated_els


# ── Per-doc translation ────────────────────────────────────────────────────────

def translate_doc(client, model: str, model_fast: str,
                  key: str, texts_dir: Path, force: bool = False) -> bool:
    doc_dir  = texts_dir / key
    out_path = doc_dir / 'translation.json'

    if out_path.exists() and not force:
        log.info(f"  {key}: already translated — skipping (use --force to redo)")
        return True

    pt_path = doc_dir / 'page_texts.json'
    le_path = doc_dir / 'layout_elements.json'

    if not pt_path.exists():
        log.warning(f"  {key}: page_texts.json not found — skipping")
        return False

    page_texts      = json.loads(pt_path.read_text())
    layout_elements = json.loads(le_path.read_text()) if le_path.exists() else {}

    # ── Language detection ────────────────────────────────────────────────────
    sample = next(
        (v for v in page_texts.values() if v and len(v) > 100),
        list(page_texts.values())[0] if page_texts else ''
    )
    source_lang = detect_language(client, model_fast, sample)
    log.info(f"  {key}: detected language = {source_lang!r}")

    if source_lang in ('en', 'unknown'):
        log.info(f"  {key}: English or undetected — skipping")
        return False

    # ── Page-by-page translation ──────────────────────────────────────────────
    pages = sorted(
        (k for k in page_texts if k != '_page_sizes'),
        key=lambda x: int(x) if str(x).isdigit() else 0
    )

    translated_page_texts = {}
    translated_elements   = {}

    for i, pg in enumerate(pages):
        if i > 0:
            time.sleep(1.5)   # rate-limit buffer between pages

        raw_text = page_texts.get(pg, '') or ''
        raw_els  = layout_elements.get(pg, [])
        if isinstance(raw_els, str):
            raw_els = []  # skip _page_sizes etc.

        # Skip empty pages
        if not raw_text.strip() and not raw_els:
            translated_page_texts[pg] = raw_text
            translated_elements[pg]   = raw_els
            continue

        log.info(f"    [{i+1}/{len(pages)}] page {pg} — {len(raw_text)} chars, {len(raw_els)} elements")

        # Build boundary context from adjacent pages (original source language)
        prev_pg   = pages[i - 1] if i > 0 else None
        next_pg   = pages[i + 1] if i < len(pages) - 1 else None
        prev_ctx  = (page_texts.get(prev_pg, '') or '')[-_CONTEXT_CHARS:] if prev_pg else ''
        next_ctx  = (page_texts.get(next_pg, '') or '')[:_CONTEXT_CHARS]  if next_pg else ''

        try:
            t_text, t_els = translate_page(
                client, model, source_lang, raw_text, raw_els,
                prev_context=prev_ctx,
                next_context=next_ctx,
            )
            translated_page_texts[pg] = t_text
            translated_elements[pg]   = t_els
        except Exception as exc:
            log.error(f"    page {pg} FAILED: {exc} — keeping original")
            translated_page_texts[pg] = raw_text
            translated_elements[pg]   = raw_els

    # ── Save ──────────────────────────────────────────────────────────────────
    result = {
        'key':             key,
        'source_language': source_lang,
        'target_language': 'en',
        'model':           model,
        'translated_at':   datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'page_texts':      translated_page_texts,
        'elements':        translated_elements,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    log.info(f"  {key}: ✓ saved {len(pages)} pages → translation.json")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Translate non-English extracted docs to English using Claude Opus.'
    )
    parser.add_argument('--texts-dir',  default='data/texts')
    parser.add_argument('--inventory',  default='data/inventory.json')
    parser.add_argument('--keys',       nargs='+', default=[],
                        help='Process only these document keys')
    parser.add_argument('--force',      action='store_true',
                        help='Overwrite existing translation.json files')
    parser.add_argument('--model',      default='claude-opus-4-5',
                        help='Model for translation (default: claude-opus-4-5)')
    parser.add_argument('--model-fast', default='claude-haiku-4-5',
                        help='Fast/cheap model for language detection')
    args = parser.parse_args()

    texts_dir = _ROOT / args.texts_dir

    client = _api_client()

    # Determine which keys to process
    if args.keys:
        keys = args.keys
    else:
        # All dirs that have page_texts.json
        keys = sorted(
            d.name for d in texts_dir.iterdir()
            if d.is_dir() and (d / 'page_texts.json').exists()
        )

    log.info(f"Checking {len(keys)} document(s) for non-English content…\n")

    ok = err = skipped = 0
    for key in keys:
        log.info(f"── {key}")
        try:
            result = translate_doc(
                client, args.model, args.model_fast,
                key, texts_dir, force=args.force
            )
            if result is True:
                ok += 1
            else:
                skipped += 1
        except Exception as exc:
            log.error(f"  {key}: FAILED — {exc}")
            err += 1
        print()

    print('=' * 60)
    print(f"✓ Translated: {ok}   ✗ Errors: {err}   – Skipped (English): {skipped}")
    print(f"Output: {texts_dir}/{{KEY}}/translation.json")


if __name__ == '__main__':
    main()
