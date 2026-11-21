"""Reusable immutable, content-addressed document series and daily blocks."""

import hashlib
import json
from contextlib import contextmanager
from threading import RLock

ISO_DATE_PREFIX_LENGTH = 10
SERIES_ARCHIVE_READ_BATCH_BLOCKS = 128


def version(value):
    """Preserve the stable JSON hash of immutable evidence."""
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


class SeriesArchive:
    """Keep manifest and block publication within one document-owner transaction."""

    def __init__(self, documents):
        """Borrow the namespace without taking engine ownership."""
        self.documents = documents
        self._writes = RLock()

    def archive(self, kind, ref, value):
        """Reuse exact immutable evidence. Never overwrite a conflicting saved version."""
        with self.transaction():
            target = f"{kind}-{ref}.json"
            if self.documents.exists(target):
                if self.documents.read(target) != value:
                    raise ValueError("Evidence archive mismatch")
            else:
                self.documents.write(target, value)

    @contextmanager
    def transaction(self):
        """Serialize concurrent archive writes before starting their shared SQL transaction."""
        with self._writes, self.documents.transaction():
            yield

    def archive_series(self, kind, ref, rows, *, entity=None):
        """Reuse unchanged daily evidence blocks instead of copying an annual history on each refresh.

        Weather values and receipt runs are separate: repeated retrieval does not
        duplicate historical values, while their actual knowledge times remain
        reconstructible. Source identity stays in each history block.

        Blocks and their manifest commit atomically. Existing manifests and all
        referenced bodies are compared with the supplied evidence through bounded
        reads. Conflicting or missing published blocks fail without rewriting.
        Valid blocks from an older incomplete write can finish a missing manifest.
        """
        days = {}
        for row in rows:
            days.setdefault(row["start"][:ISO_DATE_PREFIX_LENGTH], []).append(row)
        blocks, bodies = [], {}
        for day, items in days.items():
            values, receipts = [], []
            for index, item in enumerate(items):
                value = dict(item)
                received = value.pop("received_at", None)
                if received is not None and (not receipts or receipts[-1][1] != received):
                    receipts.append([index, received])
                values.append(value)
            body = {"entity_id": entity, "hours": values}
            block = version(body)
            bodies[f"block-{block}.json"] = body
            blocks.append({"day": day, "block": block, "receipts": receipts})
        target = f"{kind}-{ref}.json"
        manifest = {"entity_id": entity, "blocks": blocks}
        names = tuple(bodies)
        with self.transaction():
            previous = self.documents.read_many((target,))
            published = target in previous
            if published and previous[target] != manifest:
                raise ValueError("Evidence archive mismatch")
            for offset in range(0, len(names), SERIES_ARCHIVE_READ_BATCH_BLOCKS):
                batch = names[offset : offset + SERIES_ARCHIVE_READ_BATCH_BLOCKS]
                accepted = self.documents.read_many(batch)
                for name in batch:
                    if name in accepted:
                        if accepted[name] != bodies[name]:
                            raise ValueError("Evidence archive mismatch")
                    elif published:
                        raise ValueError("Evidence archive mismatch")
                    else:
                        self.documents.write(name, bodies[name])
            if not published:
                self.documents.write(target, manifest)
