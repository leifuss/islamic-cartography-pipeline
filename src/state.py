"""
Pipeline state management for checkpointing and resume.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


class PipelineState:
    """Manage pipeline state for resume capability."""

    def __init__(self, state_file: Path = Path('data/.pipeline_state.json')):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> Dict:
        """Load state from disk or create new."""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                return json.load(f)
        else:
            return {
                'completed': [],
                'failed': [],
                'skipped': {},
                'in_progress': None,
                'started_at': None,
                'last_updated': None
            }

    def save(self):
        """Save current state to disk."""
        self.state['last_updated'] = datetime.now().isoformat()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    def mark_completed(self, item_key: str):
        """Mark an item as successfully processed."""
        if item_key not in self.state['completed']:
            self.state['completed'].append(item_key)
        self.state['in_progress'] = None
        self.save()

    def mark_failed(self, item_key: str, error: str):
        """Mark an item as failed."""
        self.state['failed'].append({
            'key': item_key,
            'error': error,
            'timestamp': datetime.now().isoformat()
        })
        self.state['in_progress'] = None
        self.save()

    def mark_skipped(self, item_key: str, reason: str):
        """Mark an item as skipped."""
        self.state['skipped'][item_key] = reason
        self.state['in_progress'] = None
        self.save()

    def mark_in_progress(self, item_key: str):
        """Mark an item as currently being processed."""
        self.state['in_progress'] = item_key
        if not self.state['started_at']:
            self.state['started_at'] = datetime.now().isoformat()
        self.save()

    def is_processed(self, item_key: str) -> bool:
        """Check if an item has already been processed."""
        return (
            item_key in self.state['completed'] or
            item_key in self.state['skipped']
        )

    def get_progress(self) -> Dict:
        """Get summary of progress."""
        return {
            'completed': len(self.state['completed']),
            'failed': len(self.state['failed']),
            'skipped': len(self.state['skipped']),
            'in_progress': self.state['in_progress']
        }


if __name__ == '__main__':
    # Test state management
    state = PipelineState(Path('data/.test_state.json'))

    state.mark_in_progress('TEST001')
    state.mark_completed('TEST001')

    state.mark_in_progress('TEST002')
    state.mark_skipped('TEST002', 'no_attachment')

    print("State test passed!")
    print(json.dumps(state.get_progress(), indent=2))
