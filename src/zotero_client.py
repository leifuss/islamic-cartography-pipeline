"""
Wrapper around pyzotero for consistent local Zotero library access.
"""
from typing import List, Dict, Optional
import os
from pathlib import Path

try:
    from pyzotero import zotero as pyzotero_module
except ImportError:
    print("ERROR: pyzotero not installed. Run: pip install pyzotero")
    raise


class ZoteroLibrary:
    """Interface to local Zotero library."""

    def __init__(self, collection_name: Optional[str] = None):
        """
        Initialize connection to Zotero.

        Args:
            collection_name: Filter to specific collection (e.g., "Islamic Cartography")
        """
        self.use_local = os.getenv('ZOTERO_LOCAL', 'true').lower() == 'true'
        self.collection_name = collection_name or os.getenv('COLLECTION_NAME')

        if self.use_local:
            # Connect to local Zotero instance
            # Check if we should use a group library
            group_id = os.getenv('ZOTERO_GROUP_ID')

            if group_id:
                # Connect to group library
                self.client = pyzotero_module.Zotero(
                    library_id=int(group_id),
                    library_type='group',
                    local=True
                )
                self.library_type = 'group'
                self.library_id = int(group_id)
            else:
                # Connect to user library (default)
                # Local access requires library_id=0, library_type='user', local=True
                # This connects to the logged-in user's library (typically userID 0 locally)
                self.client = pyzotero_module.Zotero(
                    library_id=0,
                    library_type='user',
                    local=True
                )
                self.library_type = 'user'
                self.library_id = 0
        else:
            # Connect via web API
            api_key = os.getenv('ZOTERO_API_KEY')
            library_id = os.getenv('ZOTERO_LIBRARY_ID')
            library_type = os.getenv('ZOTERO_LIBRARY_TYPE', 'user')
            if not api_key or not library_id:
                raise ValueError("ZOTERO_API_KEY and ZOTERO_LIBRARY_ID required for web API")
            self.client = pyzotero_module.Zotero(
                library_id=library_id,
                library_type=library_type,
                api_key=api_key
            )
            self.library_type = library_type
            self.library_id = library_id

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
                    import logging
                    logging.getLogger(__name__).warning(
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

    def get_attachment_path(self, item: Dict) -> Optional[Path]:
        """
        Get filesystem path to primary PDF/image attachment.

        Args:
            item: Zotero item dict

        Returns:
            Path to attachment file, or None if no attachment
        """
        # Check if item has children (attachments)
        num_children = item.get('meta', {}).get('numChildren', 0)
        if num_children == 0:
            return None

        # Get children (attachments)
        try:
            children = self.client.children(item['key'])
        except Exception:
            return None

        if not children:
            return None

        # Look for PDF attachments first, then images
        preferred_types = [
            ('application/pdf', '.pdf'),
            ('image/png', '.png'),
            ('image/jpeg', '.jpg'),
            ('image/tiff', '.tiff'),
        ]

        for child in children:
            if child['data'].get('itemType') != 'attachment':
                continue

            content_type = child['data'].get('contentType', '')
            # Check if this is a preferred attachment type
            for pref_type, _ in preferred_types:
                if content_type == pref_type:
                    # Try to get the file path
                    path_str = child['data'].get('path', '')
                    if path_str and path_str.startswith('/'):
                        path = Path(path_str)
                        if path.exists():
                            return path
                    # Also try filename
                    filename = child['data'].get('filename', '')
                    if filename:
                        # Check in Zotero storage directory
                        # Local storage is typically in ~/Zotero/storage/{item_key}/{filename}
                        storage_path = Path.home() / 'Zotero' / 'storage' / child['key'] / filename
                        if storage_path.exists():
                            return storage_path

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
        except Exception as e:
            return False


def main():
    """Quick test of Zotero connection."""
    from dotenv import load_dotenv
    load_dotenv()

    library = ZoteroLibrary()
    lib_display = f"{library.library_type} library (ID: {library.library_id})"
    print(f"Connecting to Zotero ({lib_display})...")

    if library.verify_connection():
        items = library.get_all_items()
        print(f"✓ Successfully connected: {len(items)} items found")

        if library.collection_name:
            print(f"  Collection: {library.collection_name}")

        if items:
            # Show first few items as examples
            print(f"\nFirst 3 items:")
            for i, item in enumerate(items[:3], 1):
                key = item.get('key', 'N/A')
                title = item.get('data', {}).get('title', 'N/A')[:70]
                print(f"  {i}. [{key}] {title}")
    else:
        print("✗ Connection failed")


if __name__ == '__main__':
    main()
