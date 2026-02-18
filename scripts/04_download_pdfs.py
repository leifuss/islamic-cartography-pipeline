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


# ── Config ─────────────────────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; academic-research-bot/1.0; '
        '+https://github.com/academic-research)'
    )
}

# Domains we know require login or subscription — skip without attempting
PAYWALL_DOMAINS = {
    'jstor.org', 'brill.com', 'cambridge.org', 'press.uchicago.edu',
    'onlinelibrary.wiley.com', 'degruyter.com', 'springer.com',
    'loebclassics.com', 'fulcrum.org', 'vlebooks.com', 'aspresolver.com',
    'librarysearch.exeter.ac.uk', 'iupress.istanbul.edu.tr',
    'referenceworks.brillonline.com',
}

# Domains that are HTML references / encyclopaedias — not PDFs
HTML_DOMAINS = {
    'en.wikipedia.org', 'iranicaonline.org', 'encyclopedia.com',
    'myoldmaps.com',       # handled separately — direct PDF links
}

ARCHIVE_API = 'https://archive.org/metadata/{identifier}'
DOI_RESOLVER = 'https://doi.org/{doi}'


# ── HTTP session ───────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    s.mount('http://',  HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


# ── Helpers ────────────────────────────────────────────────────────────────────

def safe_filename(s: str, max_len: int = 60) -> str:
    s = unquote(s)                          # decode %XX percent-encoding
    s = re.sub(r'[^\w\-.]', '_', s)        # replace unsafe chars
    s = re.sub(r'_+', '_', s).strip('_')   # collapse runs of underscores
    return s[:max_len]


def is_pdf_response(resp: requests.Response) -> bool:
    ct = resp.headers.get('Content-Type', '')
    return 'application/pdf' in ct or 'application/octet-stream' in ct


def save_pdf(content: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


def host(url: str) -> str:
    return urlparse(url).netloc.replace('www.', '')


# ── Archive.org handler ────────────────────────────────────────────────────────

def resolve_archive_org(url: str, session: requests.Session) -> str | None:
    """
    Given an archive.org/details/{id} URL, return a direct PDF download URL
    using the metadata API.  Returns None if no PDF is available openly.
    """
    m = re.search(r'archive\.org/details/([^/?#]+)', url)
    if not m:
        return None
    identifier = m.group(1)

    try:
        meta_url = ARCHIVE_API.format(identifier=identifier)
        r = session.get(meta_url, timeout=20)
        r.raise_for_status()
        meta = r.json()
    except Exception as e:
        return None

    # Check access — items that require borrowing have 'access-restricted-item'
    metadata = meta.get('metadata', {})
    access = metadata.get('access-restricted-item', '')
    if str(access).lower() == 'true':
        return None  # borrow-only, skip

    # Find the best file: prefer .pdf, fall back to .epub, .txt
    files = meta.get('files', [])
    for fmt in ('pdf', 'epub', 'txt'):
        for f in files:
            if f.get('name', '').lower().endswith(f'.{fmt}'):
                name = f['name']
                return f'https://archive.org/download/{identifier}/{name}'

    return None


# ── DOI handler ────────────────────────────────────────────────────────────────

def resolve_doi(doi_str: str, session: requests.Session) -> str | None:
    """Resolve a DOI to its final URL via doi.org."""
    url = DOI_RESOLVER.format(doi=doi_str.strip())
    try:
        r = session.head(url, allow_redirects=True, timeout=15)
        return r.url
    except Exception:
        return None


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

    # Try HEAD first to check Content-Type without downloading body
    try:
        head = session.head(url, allow_redirects=True, timeout=20)
        final_url = head.url
    except Exception as e:
        return {'status': 'error', 'download_url': url,
                'content': None, 'bytes': 0, 'error': str(e)[:120]}

    if head.status_code == 403:
        return {'status': 'paywall', 'download_url': final_url, 'content': None, 'bytes': 0}
    if head.status_code == 404:
        return {'status': 'not_found', 'download_url': final_url, 'content': None, 'bytes': 0}
    if head.status_code not in (200, 206):
        return {'status': f'http_{head.status_code}', 'download_url': final_url,
                'content': None, 'bytes': 0}

    if not is_pdf_response(head):
        # Some servers don't send Content-Type on HEAD — try GET with streaming
        # to peek at content (but only if URL looks like a PDF)
        if not (final_url.lower().endswith('.pdf') or '/pdf' in final_url.lower()):
            return {'status': 'not_pdf', 'download_url': final_url, 'content': None, 'bytes': 0}

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
