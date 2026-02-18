#!/usr/bin/env python3
"""
Verify environment and Zotero connection.
Run this first to check everything is working.
"""
import sys
from pathlib import Path
import subprocess

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from zotero_client import ZoteroLibrary
from dotenv import load_dotenv


def check_tesseract():
    """Verify Tesseract is installed with Arabic support."""
    try:
        result = subprocess.run(
            ['tesseract', '--list-langs'],
            capture_output=True,
            text=True
        )
        langs = result.stdout

        if 'ara' not in langs:
            print("❌ Tesseract found but missing Arabic language pack")
            print("   Install with: brew install tesseract-lang")
            return False

        print("✓ Tesseract installed with Arabic support")
        return True

    except FileNotFoundError:
        print("❌ Tesseract not found")
        print("   Install with: brew install tesseract")
        return False


def check_zotero():
    """Verify connection to Zotero library."""
    try:
        library = ZoteroLibrary()

        if not library.verify_connection():
            print("❌ Cannot connect to Zotero")
            print("   Make sure Zotero is running and API is enabled")
            print("   Preferences → Advanced → General → Allow other applications...")
            return False

        items = library.get_all_items()
        print(f"✓ Connected to Zotero: {len(items)} items found")

        if library.collection_name:
            print(f"  Collection: {library.collection_name}")

        # Show a few example items
        if items:
            print(f"\n  First 3 items:")
            for item in items[:3]:
                title = item.get('data', {}).get('title', 'Untitled')
                key = item.get('key', 'N/A')
                print(f"    - [{key}] {title[:60]}...")

        return True

    except Exception as e:
        print(f"❌ Zotero connection error: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_google_credentials():
    """Check Google Cloud credentials for Vision API."""
    import os

    creds_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')

    if not creds_path:
        print("⚠️  Google Cloud credentials not configured")
        print("   Vision API witness will be skipped in Phase 2")
        print("   To enable: set GOOGLE_APPLICATION_CREDENTIALS in .env")
        return True  # Not required for Phase 1

    if not Path(creds_path).exists():
        print(f"⚠️  Credentials file not found: {creds_path}")
        return True  # Not required for Phase 1

    print("✓ Google Cloud credentials configured")
    return True


def main():
    """Run all setup checks."""
    load_dotenv()

    print("Islamic Cartography Pipeline - Setup Check")
    print("=" * 50)
    print()

    checks = [
        ("Tesseract", check_tesseract),
        ("Zotero Connection", check_zotero),
        ("Google Cloud (optional)", check_google_credentials),
    ]

    results = []
    for name, check_func in checks:
        print(f"\nChecking {name}...")
        results.append(check_func())

    print("\n" + "="*50)
    if all(results[:2]):  # First 2 are required
        print("✓ Setup complete! Ready to proceed to Phase 2.")
        return 0
    else:
        print("❌ Setup incomplete. Fix errors above before proceeding.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
