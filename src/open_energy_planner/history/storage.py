"""Keep source-bound cumulative training-history chunks in the existing document namespace."""

import hashlib
import json
from contextlib import contextmanager
from threading import RLock


def history_version(document):
    """Preserve the existing immutable Solar history digest and JSON representation."""
    return hashlib.sha256(json.dumps(document, default=str, sort_keys=True).encode()).hexdigest()


class HistoryArchive:
    """Borrow demand documents for immutable chunks and atomic source-checkpoint updates."""

    def __init__(self, documents):
        """Reuse the app-owned namespace and serialize its history-import writes."""
        self.documents = documents
        self._writes = RLock()

    @contextmanager
    def transaction(self):
        """Keep an accepted chunk and its current pointer in one serialized transaction."""
        with self._writes, self.documents.transaction():
            yield

    def archive(self, kind, reference, document):
        """Retain original evidence and reject a conflicting immutable identity."""
        with self.transaction():
            target = f"{kind}-{reference}.json"
            if self.documents.exists(target):
                if self.documents.read(target) != document:
                    raise ValueError("History evidence archive mismatch")
            else:
                self.documents.write(target, document)
