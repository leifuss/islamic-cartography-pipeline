#!/usr/bin/env python3
"""
Download PDFs for URL-only Zotero items where the source is openly accessible.

Strategy (in order):
  1. Direct PDF URL — Content-Type: application/pdf on HEAD/GET
  2. archive.org/details/{id} — fetch metadata API to find the PDF file
  3. DOI — resolve via doi.org, then apply rules 1–2 to the final URL
  4. All others — mark as inaccessible (paywalled / HTML page)

Skips known paywall domains to avoid wasting time.
Saves PDFs to data/pdfs/{item_key}/{sanitised_filename}.pdf
Results logged to data/download_results.json (resume-safe).

Usage:
    python scripts/04_download_pdfs.py
    python scripts/04_download_pdfs.py --dry-run       # print plan, no downloads
    python scripts/04_download_pdfs.py --limit 20      # stop after N downloads
    python scripts/04_download_pdfs.py --delay 2.0     # seconds between requests
"""
import sys
import re
import time
import json
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / 'src'))

from dotenv import load_dotenv
load_dotenv()

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

from pdf_finder import (
    HEADERS, PAYWALL_DOMAINS, HTML_DOMAINS, ARCHIVE_API, DOI_RESOLVER,
    make_session, safe_filename, is_pdf_response, save_pdf, host,
    resolve_archive_org, resolve_doi,
)


# ── Core download logic ────────────────────────────────────────────────────────

def try_download(url: str, session: requests.Session) -> dict:
    """
    Attempt to download a PDF from `url`.
    Returns {'status': ..., 'download_url': ..., 'content': bytes|None, 'bytes': int}
    """
    h = host(url)

    # Paywall domains — skip immediately
    if any(h == d or h.endswith('.' + d) for d in PAYWALL_DOMAINS):
        return {'status': 'paywall', 'download_url': url, 'content': None, 'bytes': 0}

    # archive.org/details — resolve via API
    if 'archive.org/details/' in url:
        dl_url = resolve_archive_org(url, session)
        if not dl_url:
            return {'status': 'borrow_required', 'download_url': url,
                    'content': None, 'bytes': 0}
        url = dl_url

    # Try HEAD first to check Content-Type without downloading body.
    # Some servers return 405 (Method Not Allowed) for HEAD — fall through to GET.
    final_url = url
    head_ok = False
    try:
        head = session.head(url, allow_redirects=True, timeout=20)
        final_url = head.url
        if head.status_code == 403:
            return {'status': 'paywall', 'download_url': final_url, 'content': None, 'bytes': 0}
        if head.status_code == 404:
            return {'status': 'not_found', 'download_url': final_url, 'content': None, 'bytes': 0}
        if head.status_code in (200, 206):
            head_ok = True
            if is_pdf_response(head):
                pass  # confirmed PDF — fall through to download
            else:
                # Non-PDF content-type on HEAD; check URL pattern before attempting GET
                url_looks_like_pdf = (
                    final_url.lower().endswith('.pdf') or '/pdf' in final_url.lower() or
                    url.lower().endswith('.pdf') or '/pdf' in url.lower()
                )
                if not url_looks_like_pdf:
                    return {'status': 'not_pdf', 'download_url': final_url,
                            'content': None, 'bytes': 0}
        # For 405 or other non-fatal codes, fall through and try GET directly
    except Exception as e:
        return {'status': 'error', 'download_url': url,
                'content': None, 'bytes': 0, 'error': str(e)[:120]}

    # Actually download the PDF
    try:
        get = session.get(final_url, stream=True, timeout=60)
        get.raise_for_status()
        if not is_pdf_response(get):
            # One more check — peek at magic bytes
            chunk = next(get.iter_content(8), b'')
            if not chunk.startswith(b'%PDF'):
                return {'status': 'not_pdf', 'download_url': final_url,
                        'content': None, 'bytes': 0}
            content = chunk + get.content
        else:
            content = get.content
        return {'status': 'ok', 'download_url': final_url,
                'content': content, 'bytes': len(content)}
    except Exception as e:
        return {'status': 'error', 'download_url': final_url,
                'content': None, 'bytes': 0, 'error': str(e)[:120]}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run',  action='store_true',
                        help='Print plan without downloading anything')
    parser.add_argument('--limit',   type=int, default=0,
                        help='Stop after N successful downloads (0 = unlimited)')
    parser.add_argument('--delay',   type=float, default=1.5,
                        help='Seconds to wait between requests (default 1.5)')
    parser.add_argument('--inventory', default='data/inventory.json')
    parser.add_argument('--results',   default='data/download_results.json')
    parser.add_argument('--out-dir',   default='data/pdfs')
    args = parser.parse_args()

    inv_path    = _ROOT / args.inventory
    res_path    = _ROOT / args.results
    out_dir     = _ROOT / args.out_dir

    # Load inventory
    with open(inv_path, encoding='utf-8') as f:
        inventory = json.load(f)

    # Load existing results (resume)
    if res_path.exists():
        with open(res_path, encoding='utf-8') as f:
            results = json.load(f)
        done_keys = {r['key'] for r in results}
        print(f"Resuming: {len(done_keys)} item(s) already processed\n")
    else:
        results, done_keys = [], set()

    # Candidates: URL-only items with a URL
    candidates = [
        r for r in inventory
        if r['pdf_status'] in ('url_only', 'no_attachment')
        and r.get('url')
        and r['key'] not in done_keys
    ]
    # Also include DOI-resolvable items not yet processed
    print(f"Candidates: {len(candidates)} items to attempt")
    print(f"Dry run: {'YES' if args.dry_run else 'no'}\n")

    session     = make_session()
    downloaded  = 0
    counts      = {}

    for idx, item in enumerate(candidates, 1):
        key   = item['key']
        title = item['title'][:60]
        url   = item['url']

        print(f"[{idx:3d}/{len(candidates)}] {title}")
        print(f"          {url[:80]}")

        if args.dry_run:
            h = host(url)
            is_paywall = any(h == d or h.endswith('.' + d) for d in PAYWALL_DOMAINS)
            is_archive = 'archive.org/details/' in url
            is_direct  = url.lower().endswith('.pdf')
            tag = 'PAYWALL' if is_paywall else ('ARCHIVE' if is_archive else ('DIRECT-PDF' if is_direct else 'UNKNOWN'))
            print(f"          → [{tag}]")
            print()
            continue

        result = try_download(url, session)
        status = result['status']
        counts[status] = counts.get(status, 0) + 1

        entry = {
            'key':          key,
            'title':        title,
            'source_url':   url,
            'download_url': result.get('download_url', url),
            'status':       status,
            'bytes':        result.get('bytes', 0),
            'pdf_path':     None,
            'error':        result.get('error'),
        }

        if status == 'ok' and result.get('content'):
            # Derive filename from URL, always sanitise + truncate
            raw_name = Path(urlparse(result['download_url']).path).name
            fname    = safe_filename(raw_name)
            if not fname.lower().endswith('.pdf'):
                fname = safe_filename(title) + '.pdf'
            if not fname.endswith('.pdf'):
                fname += '.pdf'
            dest = out_dir / key / fname
            save_pdf(result['content'], dest)
            entry['pdf_path'] = str(dest)
            downloaded += 1
            print(f"          ✓ Downloaded → {dest.name} ({result['bytes']//1024}KB)")
        else:
            icon = {'paywall': '🔒', 'not_pdf': '📄', 'borrow_required': '📚',
                    'not_found': '❌', 'error': '⚠'}.get(status, '–')
            print(f"          {icon} {status}" + (f": {result.get('error','')}" if result.get('error') else ''))

        results.append(entry)
        done_keys.add(key)

        # Save incrementally
        res_path.parent.mkdir(parents=True, exist_ok=True)
        with open(res_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        if args.limit and downloaded >= args.limit:
            print(f"\nReached download limit ({args.limit}), stopping.")
            break

        print()
        time.sleep(args.delay)

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if args.dry_run:
        print("  (dry run — nothing downloaded)")
    else:
        print(f"  Downloaded:       {downloaded}")
        for status, count in sorted(counts.items()):
            print(f"  {status:<20} {count}")
        print(f"\n  PDFs saved to:    {out_dir}/")
        print(f"  Results log:      {res_path}")


if __name__ == '__main__':
    main()
