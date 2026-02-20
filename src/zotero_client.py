"""
Wrapper around pyzotero for Zotero web-API access.

All access goes through the Zotero web API (api.zotero.org).
Required env vars: ZOTERO_API_KEY, ZOTERO_LIBRARY_ID.
Optional: ZOTERO_LIBRARY_TYPE (default 'group').
"""
from typing import List, Dict, Optional
import os
import logging
from pathlib import Path

try:
    from pyzotero import zotero as pyzotero_module
except ImportError:
    print("ERROR: pyzotero not installed. Run: pip install pyzotero")
    raise

log = logging.getLogger(__name__)


class ZoteroLibrary:
    """Interface to Zotero via the web API."""

    def __init__(self, collection_name: Optional[str] = None):
        """
        Initialize connection to Zotero web API.

        Requires ZOTERO_API_KEY and ZOTERO_LIBRARY_ID environment variables.

        Args:
            collection_name: Filter to specific collection (e.g., "Islamic Cartography")
        """
        self.collection_name = collection_name or os.getenv('COLLECTION_NAME')

        api_key = os.getenv('ZOTERO_API_KEY')
        library_id = os.getenv('ZOTERO_LIBRARY_ID')
        self.library_type = os.getenv('ZOTERO_LIBRARY_TYPE', 'group')

        if not api_key or not library_id:
            raise ValueError(
                "ZOTERO_API_KEY and ZOTERO_LIBRARY_ID are required.\n"
                "  1. Create an API key at https://www.zotero.org/settings/keys\n"
                "  2. Set both in your .env file (see .env.template)"
            )

        self.library_id = library_id
        self.client = pyzotero_module.Zotero(
            library_id=library_id,
            library_type=self.library_type,
            api_key=api_key,
        )

        # Cache for collections (lazy-loaded)
        self._collections = None
        self._collection_key = None

    def _get_collections(self) -> Dict[str, Dict]:
        """Get all collections, cached."""
        if self._collections is None:
            collections = self.client.all_collections()
            self._collections = {c['data']['name']: c for c in collections}
        return self._collections

    def _find_collection_key(self, name: str) -> Optional[str]:
        """Find collection key by name."""
        if self._collection_key is not None:
            return self._collection_key

        collections = self._get_collections()
        if name in collections:
            self._collection_key = collections[name]['key']
            return self._collection_key
        return None

    def _fetch_all(self, include_children: bool = False) -> List[Dict]:
        """Fetch all items from the library or collection, with collection fallback."""
        fetch = self.client.items if include_children else self.client.top

        if self.collection_name:
            coll_key = self._find_collection_key(self.collection_name)
            if coll_key:
                items = self.client.everything(
                    self.client.collection_items(coll_key)
                )
                if not items:
                    # Collection exists but is empty — items are probably
                    # top-level in the group library, not assigned to the
                    # collection.  Fall back to fetching everything.
                    log.warning(
                        f"Collection '{self.collection_name}' ({coll_key}) "
                        f"returned 0 items — falling back to all items"
                    )
                    items = self.client.everything(fetch())
            else:
                available = list(self._get_collections().keys())
                raise ValueError(
                    f"Collection '{self.collection_name}' not found. "
                    f"Available collections: {available}"
                )
        else:
            items = self.client.everything(fetch())

        return items

    def get_all_items(self) -> List[Dict]:
        """
        Retrieve all top-level items, optionally filtered by collection.

        Returns:
            List of item dicts (no child notes/attachments)
        """
        return self._fetch_all(include_children=False)

    def get_all_items_with_children(self) -> tuple:
        """
        Fetch all items (parents + children) in one paginated call.

        Returns a tuple of (top_level_items, children_by_parent_key) so
        callers can access notes/attachments without N+1 HTTP requests.
        """
        all_items = self._fetch_all(include_children=True)

        top_items = []
        children_by_parent: Dict[str, List[Dict]] = {}
        for item in all_items:
            parent = item.get('data', {}).get('parentItem')
            if parent:
                children_by_parent.setdefault(parent, []).append(item)
            else:
                top_items.append(item)

        return top_items, children_by_parent

    def get_attachment_info(self, item_key: str,
                           children: Optional[List[Dict]] = None) -> Optional[Dict]:
        """
        Find the primary PDF attachment for an item via the API.

        Args:
            item_key: The parent item key.
            children: Pre-fetched children list (avoids an extra API call).

        Returns:
            Dict with 'key', 'filename', 'content_type' — or None.
        """
        if children is None:
            try:
                children = self.client.children(item_key)
            except Exception:
                return None

        preferred = ('application/pdf', 'image/png', 'image/jpeg', 'image/tiff')

        for child in (children or []):
            if child['data'].get('itemType') != 'attachment':
                continue
            ct = child['data'].get('contentType', '')
            if ct in preferred:
                return {
                    'key': child['key'],
                    'filename': child['data'].get('filename', ''),
                    'content_type': ct,
                    'link_mode': child['data'].get('linkMode', ''),
                }
        return None

    def download_attachment(self, attachment_key: str) -> Optional[bytes]:
        """
        Download the file content for an attachment from Zotero's cloud.

        Args:
            attachment_key: The Zotero key of the attachment item.

        Returns:
            File bytes, or None on failure.
        """
        try:
            return self.client.file(attachment_key)
        except Exception as e:
            log.warning(f"Failed to download attachment {attachment_key}: {e}")
            return None

    def verify_connection(self) -> bool:
        """
        Test connection to Zotero.

        Returns:
            True if connected successfully
        """
        try:
            items = self.get_all_items()
            return len(items) > 0
        except Exception:
            return False


def main():
    """Quick test of Zotero connection."""
    from dotenv import load_dotenv
    load_dotenv()

    library = ZoteroLibrary()
    lib_display = f"{library.library_type} library (ID: {library.library_id})"
    print(f"Connecting to Zotero web API ({lib_display})...")

    if library.verify_connection():
        items = library.get_all_items()
        print(f"Connected: {len(items)} items found")

        if library.collection_name:
            print(f"  Collection: {library.collection_name}")

        if items:
            print(f"\nFirst 3 items:")
            for i, item in enumerate(items[:3], 1):
                key = item.get('key', 'N/A')
                title = item.get('data', {}).get('title', 'N/A')[:70]
                print(f"  {i}. [{key}] {title}")
    else:
        print("Connection failed")


if __name__ == '__main__':
    main()
