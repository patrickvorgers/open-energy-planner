"""Private bootstrap-file replacement. Durable application data belongs in the active database."""

import json
import os


class BootstrapDurabilityError(OSError):
    """The replacement is visible, but its directory entry could not be durably flushed."""


def atomic_json(target, document, *, mode=None):
    """Flush an adapter-owned JSON file, replace its target, then flush the parent directory.

    Callers own serialization. Windows skips the directory flush.

    Raises:
        BootstrapDurabilityError: Replacement is already visible, but its parent
            directory could not be flushed. The new document must be treated as current.
        OSError: Writing or replacing the temporary file failed before replacement.
    """
    pending = target.with_suffix(".tmp")
    with pending.open("w", encoding="utf-8") as stream:
        if mode is not None:
            os.chmod(pending, mode)
        json.dump(document, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, target)
    if os.name != "nt":
        try:
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise BootstrapDurabilityError(
                "Bootstrap replacement durability is uncertain"
            ) from None
