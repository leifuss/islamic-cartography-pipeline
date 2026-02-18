#!/usr/bin/env python3
"""
Render PDF pages to JPEG files and write reader_meta.json for the SPA reader.

For each document the script produces:
    data/texts/{KEY}/pages/001.jpg  002.jpg  …
    data/texts/{KEY}/reader_meta.json

The static reader at data/reader.html then loads these on demand.

Usage:
    # Single document by Zotero key
    python scripts/generate_reader.py --key QIGTV3FC

    # Single document by direct PDF path
    python scripts/generate_reader.py --pdf path/to/file.pdf

    # All documents that have been extracted (data/texts/*/page_texts.json)
    python scripts/generate_reader.py --all

    # Re-render even if pages/ already exist
    python scripts/generate_reader.py --all --force

Options:
    --scale FLOAT   Render scale factor (default 1.5 ≈ 144 dpi)
    --quality INT   JPEG quality 1-95 (default 82)
"""
import sys, json, io, argparse
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / 'src'))

try:
    import pypdfium2 as pdfium
except ImportError:
    print("ERROR: pip install pypdfium2"); sys.exit(1)


# ── Page rendering ─────────────────────────────────────────────────────────────

def render_pages_to_dir(pdf_path: Path, out_dir: Path,
                         scale: float = 1.5, quality: int = 82,
                         force: bool = False) -> int:
    """
    Render every page of *pdf_path* to JPEG files inside *out_dir*.

    Files are named 001.jpg, 002.jpg, … (1-based, zero-padded to 3 digits).
    Returns the number of pages rendered (0 if skipped because already done).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(pdf_path))
    n   = len(doc)

    # Decide whether to skip
    if not force:
        existing = sorted(out_dir.glob('*.jpg'))
        if len(existing) == n:
            print(f"  pages/ already has {n} images — skipping (use --force to re-render)")
            return 0

    for i in range(n):
        page   = doc[i]
        bitmap = page.render(scale=scale, rotation=0)
        pil    = bitmap.to_pil()
        buf    = io.BytesIO()
        pil.save(buf, format='JPEG', quality=quality, optimize=True)
        out_file = out_dir / f"{i+1:03d}.jpg"
        out_file.write_bytes(buf.getvalue())
        print(f"\r  Rendering pages… {i+1}/{n}", end="", flush=True)

    print()
    return n


# ── reader_meta.json ───────────────────────────────────────────────────────────

def write_reader_meta(key: str, pdf_path: Path, n_pages: int,
                       inv_entry: dict | None, texts_dir: Path) -> None:
    """
    Write (or overwrite) reader_meta.json in *texts_dir* with enriched metadata.

    Falls back gracefully when inventory data is unavailable.
    """
    if inv_entry:
        meta = {
            "key":        key,
            "title":      inv_entry.get("title",   ""),
            "authors":    inv_entry.get("authors", ""),
            "year":       str(inv_entry.get("year", "") or ""),
            "publisher":  inv_entry.get("publisher", ""),
            "place":      inv_entry.get("place", ""),
            "item_type":  inv_entry.get("item_type", ""),
            "language":   inv_entry.get("language", ""),
            "page_count": n_pages,
            "pdf_path":   str(pdf_path),
        }
    else:
        # Minimal fallback — try to read whatever meta.json already has
        existing_meta_path = texts_dir / "meta.json"
        existing = {}
        if existing_meta_path.exists():
            try:
                existing = json.loads(existing_meta_path.read_text())
            except Exception:
                pass
        meta = {
            "key":        key,
            "title":      existing.get("title",   key),
            "authors":    existing.get("authors", ""),
            "year":       str(existing.get("year", "") or ""),
            "publisher":  existing.get("publisher", ""),
            "place":      existing.get("place", ""),
            "item_type":  existing.get("item_type", ""),
            "language":   existing.get("language", ""),
            "page_count": n_pages,
            "pdf_path":   str(pdf_path),
        }

    out = texts_dir / "reader_meta.json"
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  reader_meta.json written ({meta['title'][:60]})")


# ── Processing helpers ─────────────────────────────────────────────────────────

def _load_inventory() -> dict[str, dict]:
    """Return inventory as a dict keyed by Zotero key, or {} if not found."""
    inv_path = _ROOT / "data" / "inventory.json"
    if not inv_path.exists():
        return {}
    try:
        rows = json.loads(inv_path.read_text())
        return {r["key"]: r for r in rows if "key" in r}
    except Exception as e:
        print(f"  ⚠ Could not load inventory.json: {e}")
        return {}


def process_key(key: str, inv: dict[str, dict],
                scale: float, quality: int, force: bool) -> bool:
    """
    Render pages and write reader_meta.json for a single document key.
    Returns True on success.
    """
    texts_dir = _ROOT / "data" / "texts" / key
    pages_dir = texts_dir / "pages"

    # Resolve PDF path
    pdf_path: Path | None = None
    inv_entry = inv.get(key)

    if inv_entry and inv_entry.get("pdf_path"):
        candidate = Path(inv_entry["pdf_path"])
        if candidate.exists():
            pdf_path = candidate

    if pdf_path is None:
        print(f"  ⚠ PDF not found for {key} — skipping")
        return False

    print(f"\n── {key}  ({inv_entry.get('title','?')[:60] if inv_entry else '?'})")
    print(f"   PDF: {pdf_path}")
    print(f"   Out: {pages_dir}")

    rendered = render_pages_to_dir(pdf_path, pages_dir, scale, quality, force)

    # Determine page count (use rendered count, or count existing JPEGs)
    if rendered:
        n_pages = rendered
    else:
        n_pages = len(sorted(pages_dir.glob("*.jpg")))

    write_reader_meta(key, pdf_path, n_pages, inv_entry, texts_dir)
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Render PDF pages to JPEG + write reader_meta.json for the SPA reader."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--key",  help="Zotero item key")
    group.add_argument("--pdf",  help="Direct path to PDF (key derived from filename)")
    group.add_argument("--all",  action="store_true",
                       help="Process all docs that have data/texts/*/page_texts.json")

    parser.add_argument("--scale",   type=float, default=1.5,
                        help="Render scale factor (default 1.5 ≈ 144 dpi)")
    parser.add_argument("--quality", type=int,   default=82,
                        help="JPEG quality 1-95 (default 82)")
    parser.add_argument("--force",   action="store_true",
                        help="Re-render even if pages/ already exists")
    args = parser.parse_args()

    inv = _load_inventory()

    # ── Single key ──
    if args.key:
        ok = process_key(args.key, inv, args.scale, args.quality, args.force)
        sys.exit(0 if ok else 1)

    # ── Direct PDF ──
    if args.pdf:
        pdf_path = Path(args.pdf)
        if not pdf_path.exists():
            print(f"ERROR: {pdf_path} not found"); sys.exit(1)
        key = pdf_path.stem
        texts_dir = _ROOT / "data" / "texts" / key
        texts_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = texts_dir / "pages"

        print(f"── {key}")
        print(f"   PDF: {pdf_path}")
        rendered = render_pages_to_dir(pdf_path, pages_dir, args.scale, args.quality, args.force)
        n_pages  = rendered or len(sorted(pages_dir.glob("*.jpg")))
        write_reader_meta(key, pdf_path, n_pages, inv.get(key), texts_dir)
        sys.exit(0)

    # ── All extracted docs ──
    if args.all:
        texts_root = _ROOT / "data" / "texts"
        keys = sorted(d.name for d in texts_root.iterdir()
                      if d.is_dir() and (d / "page_texts.json").exists())
        if not keys:
            print("No extracted documents found in data/texts/")
            sys.exit(0)

        print(f"Found {len(keys)} extracted documents: {', '.join(keys)}")
        ok_count = 0
        for key in keys:
            if process_key(key, inv, args.scale, args.quality, args.force):
                ok_count += 1

        print(f"\n✓ Done — {ok_count}/{len(keys)} documents processed")
        sys.exit(0 if ok_count == len(keys) else 1)


if __name__ == "__main__":
    main()
