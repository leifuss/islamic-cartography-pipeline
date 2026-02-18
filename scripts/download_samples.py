#!/usr/bin/env python3
"""
Download sample PDFs from Zotero for testing.
"""
import sys
from pathlib import Path
import shutil

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from dotenv import load_dotenv
from zotero_client import ZoteroLibrary

def main():
    load_dotenv()

    library = ZoteroLibrary()
    items = library.get_all_items()

    print(f"Total items: {len(items)}")

    # Find items with attachments
    samples_dir = Path('data/test_samples')
    samples_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for item in items:
        if count >= 5:
            break

        attachment_path = library.get_attachment_path(item)
        if attachment_path and attachment_path.exists():
            title = item.get('data', {}).get('title', 'Untitled')
            key = item.get('key', 'unknown')

            # Copy to test_samples
            dest = samples_dir / f"{key}_{attachment_path.suffix.lower()}"
            shutil.copy(attachment_path, dest)

            print(f"✓ Downloaded: {title[:50]}")
            print(f"  → {dest.name}")
            count += 1

    print(f"\nDownloaded {count} sample files to {samples_dir}")
    return 0

if __name__ == '__main__':
    sys.exit(main())
