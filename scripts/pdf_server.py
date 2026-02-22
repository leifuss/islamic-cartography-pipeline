#!/usr/bin/env python3
"""
Lightweight local server for PDF upload and URL fetch.

Runs on localhost — designed for use alongside the static dashboard.
Start this server, then use the dashboard's upload/URL buttons.

Endpoints:
  GET  /                     — health check + CORS preflight
  POST /api/upload-pdf       — multipart file upload
  POST /api/fetch-url        — JSON: {"url": "...", "key": "...", "collection": "..."}
  GET  /api/collections      — list available collections
  GET  /api/inventory        — inventory for a collection (?collection=slug)

Usage:
    python scripts/pdf_server.py                    # port 8787
    python scripts/pdf_server.py --port 9000
    python scripts/pdf_server.py --open             # open dashboard in browser
"""

from __future__ import annotations

import argparse
import cgi
import io
import json
import os
import sys
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_ROOT = Path(__file__).parent.parent
_SRC  = str(_ROOT / "src")
sys.path.insert(0, _SRC)

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

COLLECTIONS_PATH = _ROOT / "data" / "collections.json"
DEFAULT_PORT = 8787


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_collection_paths(slug: str) -> dict:
    """Resolve paths for a collection slug."""
    if not COLLECTIONS_PATH.exists():
        return None
    with open(COLLECTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for c in data.get("collections", []):
        if c["slug"] == slug:
            path = c.get("path", slug)
            base = _ROOT / "data" if path == "." else _ROOT / "data" / path
            return {
                "base":      base,
                "pdfs_dir":  base / "pdfs",
                "texts_dir": base / "texts",
                "inventory": base / "inventory.json",
            }
    return None


def load_collections() -> list:
    if not COLLECTIONS_PATH.exists():
        return []
    with open(COLLECTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("collections", [])


def classify_pdf(pdf_path: Path) -> dict:
    """Quick classification of a PDF — doc_type, page_count, DPI, language."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"doc_type": "unknown", "page_count": None, "pdf_dpi": None}

    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        n_pages = len(doc)

        # Check embedded text on first 3 pages
        sample_chars = 0
        for i in range(min(3, n_pages)):
            page = doc[i]
            textpage = page.get_textpage()
            sample_chars += len(textpage.get_text_range() or "")
        avg = sample_chars / min(3, n_pages) if n_pages else 0
        doc_type = "scanned" if avg < 50 else "embedded"

        # DPI estimation — born-digital docs get 0 (no raster resolution)
        if doc_type == "embedded":
            pdf_dpi = 0
        else:
            pdf_dpi = None
            try:
                from pypdfium2.raw import FPDF_PAGEOBJ_IMAGE
                dpis = []
                for i in range(min(3, n_pages)):
                    page = doc[i]
                    for obj in page.get_objects(filter=[FPDF_PAGEOBJ_IMAGE]):
                        try:
                            px_w, px_h = obj.get_size()
                            left, bottom, right, top = obj.get_bounds()
                            box_w = abs(right - left)
                            if box_w > 0 and px_w > 0:
                                dpis.append(px_w / (box_w / 72.0))
                        except Exception:
                            continue
                if dpis:
                    dpis.sort()
                    pdf_dpi = round(dpis[len(dpis) // 2])
            except Exception:
                pass

        doc.close()
        return {"doc_type": doc_type, "page_count": n_pages, "pdf_dpi": pdf_dpi}
    except Exception as e:
        return {"doc_type": "unknown", "page_count": None, "pdf_dpi": None,
                "error": str(e)[:100]}


def update_inventory(inv_path: Path, key: str, updates: dict) -> bool:
    """Update a single item in inventory.json. Returns True if item was found."""
    if not inv_path.exists():
        return False
    with open(inv_path, encoding="utf-8") as f:
        inventory = json.load(f)

    found = False
    for item in inventory:
        if item["key"] == key:
            item.update(updates)
            found = True
            break

    if found:
        tmp = inv_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(inv_path)
    return found


def download_pdf_from_url(url: str, timeout: int = 30) -> tuple[bytes | None, str | None]:
    """Download a PDF from a URL. Returns (content, error)."""
    try:
        import requests
    except ImportError:
        return None, "requests library not installed"

    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Scholion/1.0 (scholarly research tool)"
        })
        resp = session.get(url, timeout=timeout, stream=False)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower() and not resp.content[:4] == b"%PDF":
            return None, "Response is HTML, not a PDF"
        if resp.content[:4] != b"%PDF":
            return None, "Response does not look like a PDF"
        return resp.content, None
    except Exception as e:
        return None, str(e)[:200]


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class PDFServerHandler(BaseHTTPRequestHandler):

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self._json_response({
                "status": "ok",
                "service": "scholion-pdf-server",
                "endpoints": ["/api/upload-pdf", "/api/fetch-url",
                              "/api/collections", "/api/inventory"],
            })

        elif path == "/api/collections":
            colls = load_collections()
            self._json_response({"collections": colls})

        elif path == "/api/inventory":
            slug = (qs.get("collection") or [None])[0]
            if not slug:
                self._json_response({"error": "collection parameter required"}, 400)
                return
            paths = get_collection_paths(slug)
            if not paths or not paths["inventory"].exists():
                self._json_response({"error": f"collection {slug} not found"}, 404)
                return
            with open(paths["inventory"], encoding="utf-8") as f:
                inv = json.load(f)
            self._json_response({"collection": slug, "items": inv})

        else:
            self._json_response({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/api/upload-pdf":
            self._handle_upload()
        elif path == "/api/fetch-url":
            self._handle_fetch_url()
        else:
            self._json_response({"error": "not found"}, 404)

    def _handle_upload(self):
        """Handle multipart PDF upload."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json_response({"error": "Expected multipart/form-data"}, 400)
            return

        # Parse multipart form data
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Extract boundary from content type
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):]
                break

        if not boundary:
            self._json_response({"error": "No boundary in content type"}, 400)
            return

        # Parse the multipart data
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(content_length),
        }
        fs = cgi.FieldStorage(
            fp=io.BytesIO(body),
            environ=environ,
            keep_blank_values=True,
        )

        # Extract fields
        key = fs.getvalue("key", "").strip()
        collection = fs.getvalue("collection", "").strip()
        file_item = fs["file"] if "file" in fs else None

        if not key:
            self._json_response({"error": "key is required"}, 400)
            return
        if not collection:
            self._json_response({"error": "collection is required"}, 400)
            return
        if not file_item or not file_item.file:
            self._json_response({"error": "file is required"}, 400)
            return

        paths = get_collection_paths(collection)
        if not paths:
            self._json_response({"error": f"collection {collection} not found"}, 404)
            return

        # Read file content
        pdf_content = file_item.file.read()
        if pdf_content[:4] != b"%PDF":
            self._json_response({"error": "File does not appear to be a PDF"}, 400)
            return

        # Save PDF
        filename = file_item.filename or f"{key}.pdf"
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        dest = paths["pdfs_dir"] / key / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf_content)

        # Classify
        info = classify_pdf(dest)

        # Update inventory
        rel_path = str(dest.relative_to(_ROOT))
        update_inventory(paths["inventory"], key, {
            "pdf_path":   rel_path,
            "pdf_status": "downloaded",
            "doc_type":   info.get("doc_type", "unknown"),
            "page_count": info.get("page_count"),
            "pdf_dpi":    info.get("pdf_dpi"),
        })

        self._json_response({
            "status":     "ok",
            "key":        key,
            "pdf_path":   rel_path,
            "bytes":      len(pdf_content),
            "page_count": info.get("page_count"),
            "doc_type":   info.get("doc_type"),
            "pdf_dpi":    info.get("pdf_dpi"),
        })
        print(f"  Uploaded: {key} -> {rel_path} "
              f"({len(pdf_content) // 1024}KB, {info.get('page_count', '?')}pp)")

    def _handle_fetch_url(self):
        """Handle URL fetch request."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
            return

        url        = data.get("url", "").strip()
        key        = data.get("key", "").strip()
        collection = data.get("collection", "").strip()

        if not url:
            self._json_response({"error": "url is required"}, 400)
            return
        if not key:
            self._json_response({"error": "key is required"}, 400)
            return
        if not collection:
            self._json_response({"error": "collection is required"}, 400)
            return

        paths = get_collection_paths(collection)
        if not paths:
            self._json_response({"error": f"collection {collection} not found"}, 404)
            return

        print(f"  Fetching: {url[:80]}...")
        pdf_content, error = download_pdf_from_url(url)

        if error:
            self._json_response({"error": f"Download failed: {error}"}, 502)
            return

        # Derive filename from URL
        url_path = urlparse(url).path
        filename = Path(url_path).name
        if not filename.lower().endswith(".pdf"):
            filename = f"{key}.pdf"

        dest = paths["pdfs_dir"] / key / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf_content)

        # Classify
        info = classify_pdf(dest)

        # Update inventory
        rel_path = str(dest.relative_to(_ROOT))
        update_inventory(paths["inventory"], key, {
            "pdf_path":   rel_path,
            "pdf_status": "downloaded",
            "doc_type":   info.get("doc_type", "unknown"),
            "page_count": info.get("page_count"),
            "pdf_dpi":    info.get("pdf_dpi"),
        })

        self._json_response({
            "status":     "ok",
            "key":        key,
            "url":        url,
            "pdf_path":   rel_path,
            "bytes":      len(pdf_content),
            "page_count": info.get("page_count"),
            "doc_type":   info.get("doc_type"),
            "pdf_dpi":    info.get("pdf_dpi"),
        })
        print(f"  Fetched: {key} -> {rel_path} "
              f"({len(pdf_content) // 1024}KB, {info.get('page_count', '?')}pp)")

    def log_message(self, format, *args):
        """Quieter logging — only show errors."""
        if args and str(args[0]).startswith("4") or str(args[0]).startswith("5"):
            super().log_message(format, *args)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PDF upload/fetch server for Scholion")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true",
                        help="Open dashboard in browser on startup")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), PDFServerHandler)
    print(f"Scholion PDF server running on http://127.0.0.1:{args.port}")
    print(f"Endpoints:")
    print(f"  POST /api/upload-pdf   — upload a PDF file")
    print(f"  POST /api/fetch-url    — fetch a PDF from URL")
    print(f"  GET  /api/collections  — list collections")
    print(f"  GET  /api/inventory    — get inventory for a collection")
    print(f"\nPress Ctrl+C to stop.\n")

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{_ROOT / 'data' / 'dashboard.html'}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
