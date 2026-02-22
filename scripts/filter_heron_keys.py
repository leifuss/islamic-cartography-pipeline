"""Filter candidate keys for Heron layout enrichment.

Reads TEXTS_DIR, RAW_KEYS, and FORCE from environment variables and
prints the subset of keys that actually need Heron to run:
  - must have page_texts.json  (05b extraction succeeded)
  - must have at least one page image (Heron works on rendered pages)
  - if FORCE != 'true', skip keys already enriched by Heron
"""
import json
import os
import sys
from pathlib import Path

texts_dir = Path(os.environ["TEXTS_DIR"])
force = os.environ.get("FORCE", "").lower() == "true"
raw_keys = os.environ.get("RAW_KEYS", "").split()

keep = []
for key in raw_keys:
    d = texts_dir / key
    # Must have page_texts.json (05b extraction succeeded)
    if not (d / "page_texts.json").exists():
        print(f"Skipping {key} — no page_texts.json", file=sys.stderr)
        continue
    # Must have page images (Heron runs on rendered page images)
    pages_dir = d / "pages"
    if not pages_dir.is_dir() or not any(pages_dir.glob("*.jpg")):
        print(f"Skipping {key} — no page images", file=sys.stderr)
        continue
    # Skip if already Heron-enriched (unless FORCE is set)
    if not force:
        le = d / "layout_elements.json"
        if le.exists():
            try:
                if json.loads(le.read_text()).get("_heron_version"):
                    print(f"Skipping {key} — already Heron-enriched", file=sys.stderr)
                    continue
            except Exception:
                pass  # corrupt file → re-enrich
    keep.append(key)

print(" ".join(keep))
