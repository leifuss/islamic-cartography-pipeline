#!/usr/bin/env python3
"""
Scan Zotero inventory for PDF availability.

For each item in data/inventory.json:
  stored       — already in data/pdfs/{key}/ or pdf_status='stored_embedded'
  open_access  — DOI resolves to OA URL (Unpaywall) or direct PDF URL confirmed
  restricted   — URL on known paywall domain
  unavailable  — no URL/DOI or all checks failed

Results written to data/import_status.json (polled by import_dashboard.html).

Usage:
    python scripts/import_scan.py
    python scripts/import_scan.py --force   # re-scan already-scanned items
"""

import sys
import re
import json
import time
import argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / 'src'))

from dotenv import load_dotenv
load_dotenv()

try:
    import requests
except ImportError:
    print("ERROR: requests not installed.  Run: pip install requests")
    sys.exit(1)

from pdf_finder import (
    PAYWALL_DOMAINS, HTML_DOMAINS,
    make_session, host, resolve_archive_org,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

INV_PATH    = _ROOT / 'data' / 'inventory.json'
STATUS_PATH = _ROOT / 'data' / 'import_status.json'
PDFS_DIR    = _ROOT / 'data' / 'pdfs'

UNPAYWALL   = 'https://api.unpaywall.org/v2/{doi}?email=scholion-bot@noreply.github.com'


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_status() -> dict:
    if STATUS_PATH.exists():
        with open(STATUS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {
        'last_scan':       None,
        'scan_complete':   False,
        'import_running':  False,
        'current_item':    None,
        'progress_n':      0,
        'progress_total':  0,
        'last_updated':    None,
        'items':           {},
    }


def save_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    status['last_updated'] = now_iso()
    with open(STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, indent=2, ensure_ascii=False)


def extract_doi(item: dict) -> str | None:
    """Extract a DOI from the item's URL or note fields."""
    for field in ('url', 'notes', 'extra'):
        val = item.get(field, '') or ''
        m = re.search(r'10\.\d{4,9}/[^\s"<>]+', val)
        if m:
            return m.group(0).rstrip('.')
    return None


def is_stored(item: dict) -> bool:
    key = item['key']
    pdf_dir = PDFS_DIR / key
    if pdf_dir.exists() and any(pdf_dir.glob('*.pdf')):
        return True
    if item.get('pdf_status') == 'stored_embedded':
        return True
    return False


def check_unpaywall(doi: str, session: requests.Session) -> str | None:
    """Return an OA PDF URL from Unpaywall, or None."""
    try:
        r = session.get(UNPAYWALL.format(doi=doi.strip()), timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        # Best OA location first
        best = data.get('best_oa_location') or {}
        pdf_url = best.get('url_for_pdf') or best.get('url')
        if pdf_url:
            return pdf_url
        for loc in data.get('oa_locations', []):
            u = loc.get('url_for_pdf') or loc.get('url')
            if u:
                return u
        return None
    except Exception:
        return None


def check_url_accessible(url: str, session: requests.Session) -> tuple[bool, str | None]:
    """
    Lightweight HEAD-only check: is this URL accessible and likely a PDF?
    Returns (is_accessible, resolved_url_or_None).
    Does NOT download the full file.
    """
    h = host(url)

    if any(h == d or h.endswith('.' + d) for d in PAYWALL_DOMAINS):
        return False, None
    if any(h == d or h.endswith('.' + d) for d in HTML_DOMAINS):
        return False, None

    # archive.org — resolve via metadata API (no download)
    if 'archive.org/details/' in url:
        dl_url = resolve_archive_org(url, session)
        return (True, dl_url) if dl_url else (False, None)

    try:
        resp = session.head(url, allow_redirects=True, timeout=20)
        final_url = resp.url
        if resp.status_code in (401, 403):
            return False, None
        if resp.status_code not in (200, 206):
            return False, None
        ct = resp.headers.get('Content-Type', '')
        if 'application/pdf' in ct:
            return True, final_url
        if final_url.lower().endswith('.pdf') or '/pdf' in final_url.lower():
            return True, final_url
        return False, None
    except Exception:
        return False, None


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(force: bool = False) -> None:
    with open(INV_PATH, encoding='utf-8') as f:
        inventory = json.load(f)

    status = load_status()
    status['scan_complete'] = False
    status['last_scan'] = now_iso()
    total = len(inventory)
    print(f"Scanning {total} items for PDF availability...\n")

    session = make_session()

    for idx, item in enumerate(inventory, 1):
        key   = item['key']
        title = (item.get('title') or key)[:70]

        existing = status['items'].get(key, {})
        if not force and existing.get('availability'):
            print(f"[{idx:4d}/{total}] skip (already scanned): {title[:45]}")
            continue

        print(f"[{idx:4d}/{total}] {title[:55]}", end='  ', flush=True)

        entry = {
            'availability':  'unavailable',
            'import_status': existing.get('import_status', 'pending'),
            'pdf_url':       None,
            'page_count':    None,
            'large_flag':    False,
            'failure_reason': None,
            'last_updated':  now_iso(),
        }

        # ── 1. Already stored locally ─────────────────────────────────────────
        if is_stored(item):
            entry['availability']  = 'stored'
            entry['import_status'] = 'done'
            print('stored')

        # ── 2. Try Unpaywall for DOI items ────────────────────────────────────
        else:
            doi = extract_doi(item)
            if doi:
                oa_url = check_unpaywall(doi, session)
                if oa_url:
                    entry['availability'] = 'open_access'
                    entry['pdf_url']      = oa_url
                    print('open_access (unpaywall)')
                else:
                    # Fall through to URL check
                    doi = None   # signal: no Unpaywall result

            # ── 3. Check URL directly ─────────────────────────────────────────
            if entry['availability'] == 'unavailable':
                url = item.get('url', '')
                if url:
                    h = host(url)
                    if any(h == d or h.endswith('.' + d) for d in PAYWALL_DOMAINS):
                        entry['availability'] = 'restricted'
                        print('restricted')
                    elif any(h == d or h.endswith('.' + d) for d in HTML_DOMAINS):
                        entry['availability'] = 'unavailable'
                        print('unavailable (reference site)')
                    else:
                        accessible, resolved = check_url_accessible(url, session)
                        if accessible:
                            entry['availability'] = 'open_access'
                            entry['pdf_url']      = resolved or url
                            print('open_access (url)')
                        else:
                            entry['availability'] = 'unavailable'
                            print('unavailable')
                else:
                    print('unavailable (no url)')

        status['items'][key]  = entry
        status['progress_n']  = sum(1 for v in status['items'].values() if v.get('availability'))
        status['progress_total'] = total
        save_status(status)
        time.sleep(0.3)

    status['scan_complete'] = True
    save_status(status)

    # Summary
    counts: dict[str, int] = {}
    for v in status['items'].values():
        av = v.get('availability', 'unknown')
        counts[av] = counts.get(av, 0) + 1

    print('\n' + '=' * 55)
    print('SCAN COMPLETE')
    print('=' * 55)
    for av, n in sorted(counts.items()):
        print(f'  {av:<20} {n}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Scan Zotero inventory for PDF availability'
    )
    parser.add_argument('--force', action='store_true',
                        help='Re-scan items that already have an availability status')
    args = parser.parse_args()
    scan(force=args.force)
