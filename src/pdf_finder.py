"""
Shared PDF URL resolution helpers.

Used by scripts/04_download_pdfs.py and scripts/import_scan.py.
Extracted into src/ so both scripts can import without code duplication.
"""
import re
import requests
from pathlib import Path
from urllib.parse import urlparse, unquote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ── Constants ──────────────────────────────────────────────────────────────────

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

ARCHIVE_API  = 'https://archive.org/metadata/{identifier}'
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
    s = unquote(s)
    s = re.sub(r'[^\w\-.]', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:max_len]


def is_pdf_response(resp: requests.Response) -> bool:
    ct = resp.headers.get('Content-Type', '')
    return 'application/pdf' in ct or 'application/octet-stream' in ct


def save_pdf(content: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


def host(url: str) -> str:
    return urlparse(url).netloc.replace('www.', '')


def is_paywall_url(url: str) -> bool:
    h = host(url)
    return any(h == d or h.endswith('.' + d) for d in PAYWALL_DOMAINS)


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
    except Exception:
        return None

    metadata = meta.get('metadata', {})
    access = metadata.get('access-restricted-item', '')
    if str(access).lower() == 'true':
        return None  # borrow-only

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


# ── URL accessibility check (no download) ─────────────────────────────────────

def check_url_accessible(url: str, session: requests.Session) -> dict:
    """
    Check if a URL leads to an accessible PDF without downloading the content.

    Returns a dict with keys:
      status       — 'open_access' | 'paywall' | 'borrow_required' |
                     'not_pdf' | 'not_found' | 'error' | 'http_NNN'
      resolved_url — final URL after redirects
    """
    if is_paywall_url(url):
        return {'status': 'paywall', 'resolved_url': url}

    if 'archive.org/details/' in url:
        dl_url = resolve_archive_org(url, session)
        if not dl_url:
            return {'status': 'borrow_required', 'resolved_url': url}
        return {'status': 'open_access', 'resolved_url': dl_url}

    try:
        head = session.head(url, allow_redirects=True, timeout=20)
        final_url = head.url
    except Exception as e:
        return {'status': 'error', 'resolved_url': url, 'error': str(e)[:120]}

    if head.status_code == 403:
        return {'status': 'paywall', 'resolved_url': final_url}
    if head.status_code == 404:
        return {'status': 'not_found', 'resolved_url': final_url}
    if head.status_code not in (200, 206):
        return {'status': f'http_{head.status_code}', 'resolved_url': final_url}

    # Check Content-Type
    if is_pdf_response(head):
        return {'status': 'open_access', 'resolved_url': final_url}

    # Some servers don't set Content-Type on HEAD — if URL looks like a PDF, accept it
    if final_url.lower().endswith('.pdf') or '/pdf' in final_url.lower():
        return {'status': 'open_access', 'resolved_url': final_url}

    return {'status': 'not_pdf', 'resolved_url': final_url}
