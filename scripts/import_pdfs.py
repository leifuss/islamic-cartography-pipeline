#!/usr/bin/env python3
"""
Download PDFs for items tracked in data/import_status.json.

Modes:
  available   — download all open_access items under the large-file threshold (default)
  single      — download one specific item by key
  fetch_url   — fetch a PDF from a provided direct URL for a specific item

Progress is written to data/import_status.json every COMMIT_EVERY items
and committed to git, so import_dashboard.html can track progress in real-time.

PDFs saved to:  data/pdfs/{key}/{filename}.pdf

Usage:
    python scripts/import_pdfs.py
    python scripts/import_pdfs.py --mode single --key ABC123DE
    python scripts/import_pdfs.py --mode fetch_url --key ABC123DE --url https://...
"""

import sys
import json
import time
import datetime
import argparse
import importlib.util
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / 'src'))

from dotenv import load_dotenv
load_dotenv()

try:
    import requests
except ImportError:
    print("ERROR: requests not installed.  Run: pip install requests")
    sys.exit(1)

try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False
    print("WARNING: PyPDF2 not installed — page-count check disabled")

# ── Load helpers from 04_download_pdfs.py ────────────────────────────────────

_spec = importlib.util.spec_from_file_location(
    "download_pdfs", Path(__file__).parent / "04_download_pdfs.py"
)
_dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dl)

make_session  = _dl.make_session
try_download  = _dl.try_download
safe_filename = _dl.safe_filename

# ── Config ────────────────────────────────────────────────────────────────────

INV_PATH    = _ROOT / 'data' / 'inventory.json'
STATUS_PATH = _ROOT / 'data' / 'import_status.json'
PDFS_DIR    = _ROOT / 'data' / 'pdfs'

LARGE_PAGE_THRESHOLD = 100   # pages
COMMIT_EVERY         = 5     # items between progress commits


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'


def load_status() -> dict:
    if STATUS_PATH.exists():
        with open(STATUS_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {'items': {}}


def save_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    status['last_updated'] = now_iso()
    with open(STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, indent=2, ensure_ascii=False)


def count_pages(pdf_path: Path) -> int | None:
    if not HAS_PYPDF2:
        return None
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return len(reader.pages)
    except Exception:
        return None


def git_commit_progress(label: str) -> None:
    """Commit import_status.json + new PDFs for dashboard visibility."""
    try:
        subprocess.run(
            ['git', 'config', 'user.name', 'github-actions[bot]'],
            cwd=_ROOT, check=False, capture_output=True,
        )
        subprocess.run(
            ['git', 'config', 'user.email',
             'github-actions[bot]@users.noreply.github.com'],
            cwd=_ROOT, check=False, capture_output=True,
        )
        subprocess.run(
            ['git', 'add', 'data/import_status.json', 'data/pdfs/'],
            cwd=_ROOT, check=False, capture_output=True,
        )
        staged = subprocess.run(
            ['git', 'diff', '--staged', '--quiet'],
            cwd=_ROOT, capture_output=True,
        )
        if staged.returncode != 0:
            subprocess.run(
                ['git', 'commit', '-m',
                 f'import: progress ({label}) [skip ci]'],
                cwd=_ROOT, check=False, capture_output=True,
            )
            subprocess.run(
                ['git', 'pull', '--rebase', 'origin', 'main'],
                cwd=_ROOT, check=False, capture_output=True,
            )
            subprocess.run(
                ['git', 'push'],
                cwd=_ROOT, check=False, capture_output=True,
            )
            print(f"  ✓ Progress committed ({label})")
    except Exception as e:
        print(f"  ⚠  Git commit failed: {e}")


def download_item(key: str, url: str, title: str,
                  session: requests.Session) -> dict:
    """Download a PDF. Returns result dict."""
    result = try_download(url, session)
    status = result['status']
    if status != 'ok' or not result.get('content'):
        reason = f"{status}"
        if result.get('error'):
            reason += f": {result['error']}"
        return {'success': False, 'failure_reason': reason[:120]}

    raw_name = Path(urlparse(result['download_url']).path).name
    fname    = safe_filename(raw_name)
    if not fname.lower().endswith('.pdf'):
        fname = safe_filename(title) + '.pdf'
    if not fname.endswith('.pdf'):
        fname += '.pdf'

    dest = PDFS_DIR / key / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(result['content'])

    pages = count_pages(dest)
    return {
        'success':  True,
        'pdf_path': str(dest.relative_to(_ROOT)),
        'pages':    pages,
        'bytes':    result['bytes'],
    }


# ── Import modes ──────────────────────────────────────────────────────────────

def run_import(mode: str, key_arg: str | None, url_arg: str | None) -> None:
    with open(INV_PATH, encoding='utf-8') as f:
        inventory = {item['key']: item for item in json.load(f)}

    status = load_status()
    status['import_running'] = True
    save_status(status)

    session = make_session()

    # ── Build work list ───────────────────────────────────────────────────────
    items_to_import: list[tuple[str, str, str]] = []   # (key, url, title)

    if mode == 'fetch_url':
        if not key_arg or not url_arg:
            print("ERROR: --key and --url are both required for fetch_url mode")
            sys.exit(1)
        title = (inventory.get(key_arg) or {}).get('title', key_arg)
        items_to_import = [(key_arg, url_arg, title)]

    elif mode == 'single':
        if not key_arg:
            print("ERROR: --key is required for single mode")
            sys.exit(1)
        item_status = status['items'].get(key_arg, {})
        url = (item_status.get('pdf_url')
               or (inventory.get(key_arg) or {}).get('url', ''))
        if not url:
            print(f"ERROR: no URL found for key {key_arg}")
            sys.exit(1)
        title = (inventory.get(key_arg) or {}).get('title', key_arg)
        items_to_import = [(key_arg, url, title)]

    else:  # available (default)
        for key, entry in status['items'].items():
            if (entry.get('availability') == 'open_access'
                    and entry.get('import_status', 'pending') == 'pending'
                    and not entry.get('large_flag')):
                url = (entry.get('pdf_url')
                       or (inventory.get(key) or {}).get('url', ''))
                if url:
                    title = (inventory.get(key) or {}).get('title', key)
                    items_to_import.append((key, url, title))

    total = len(items_to_import)
    print(f"Mode: {mode}  |  Items to import: {total}\n")

    if total == 0:
        print("Nothing to import.  "
              "Run import-scan.yml first, or check availability statuses.")
        status['import_running'] = False
        save_status(status)
        return

    status['progress_total'] = total
    save_status(status)

    done = failed = 0

    for idx, (key, url, title) in enumerate(items_to_import, 1):
        print(f"[{idx:4d}/{total}] {title[:60]}")
        print(f"          {url[:80]}")

        status['current_item']  = key
        status['progress_n']    = idx
        if key not in status['items']:
            status['items'][key] = {}
        status['items'][key]['import_status'] = 'importing'
        status['items'][key]['last_updated']  = now_iso()
        save_status(status)

        res = download_item(key, url, title, session)

        if res['success']:
            pages  = res.get('pages')
            large  = pages is not None and pages > LARGE_PAGE_THRESHOLD
            kb     = (res.get('bytes') or 0) // 1024
            if large:
                status['items'][key].update({
                    'import_status':  'triage',
                    'large_flag':     True,
                    'page_count':     pages,
                    'failure_reason': (
                        f"Large file: {pages} pages "
                        f"(threshold {LARGE_PAGE_THRESHOLD}pp) — review before extraction"
                    ),
                })
                print(f"          ⚠  {pages}pp — moved to triage")
            else:
                status['items'][key].update({
                    'import_status': 'done',
                    'large_flag':    False,
                    'page_count':    pages,
                    'failure_reason': None,
                })
                pp = f"  {pages}pp" if pages else ""
                print(f"          ✓  {kb}KB{pp} → {res['pdf_path']}")
            done += 1
        else:
            status['items'][key].update({
                'import_status':  'triage',
                'failure_reason': res['failure_reason'],
            })
            print(f"          ✗  {res['failure_reason']}")
            failed += 1

        status['items'][key]['last_updated'] = now_iso()
        save_status(status)

        if idx % COMMIT_EVERY == 0:
            git_commit_progress(f"item {idx}/{total}")

        print()
        time.sleep(1.0)

    # Final state
    status['import_running'] = False
    status['current_item']   = None
    save_status(status)

    git_commit_progress('final')

    print('=' * 55)
    print('IMPORT COMPLETE')
    print(f'  Downloaded:  {done}')
    print(f'  Triage:      {failed}')
    print('=' * 55)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Download PDFs for Zotero inventory items'
    )
    parser.add_argument(
        '--mode', choices=['available', 'single', 'fetch_url'],
        default='available',
        help='available=all open-access; single=one item; fetch_url=server fetches URL',
    )
    parser.add_argument('--key', help='Item key (required for single / fetch_url)')
    parser.add_argument('--url', help='Direct PDF URL (required for fetch_url mode)')
    args = parser.parse_args()
    run_import(args.mode, args.key, args.url)
