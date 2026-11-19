"""Index conservative immutable-document references beside their owning database writes."""

import re
from datetime import UTC, datetime

from sqlalchemy import select

from .schema import DOCUMENT_LINKS, DOCUMENT_METADATA

REFERENCE_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
DOCUMENT_OWNER = "document"
VALUE_OWNER = "value"
EXPIRING_KINDS = (
    "forecast",
    "consumer-forecast",
    "quality-components",
    "quality-candidates",
    "quality-source-models",
    "source-choice",
    "ml-training",
    "historical-fit",
    "historical-assessment-current",
    "historical-assessment",
    "model-parameters",
    "daily-models",
)
EXPIRING_NAMESPACES = frozenset({"solar", "demand"})


def utc_stamp(instant):
    """Require an aware clock and use the archive's fixed-width UTC comparison keys."""
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("Document retention requires an aware clock")
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def reference_hashes(value):
    """Conservatively protect SHA-256 references in JSON strings, including mapping keys.

    The index intentionally admits unrelated hash-shaped strings. False positives
    retain extra evidence instead of permitting an unsafe deletion. Following
    every live owner's links also protects transitive dependencies without
    repeatedly reconstructing large retained forecast documents.
    """
    references, pending = set(), [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            references.update(REFERENCE_PATTERN.findall(item))
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return references


def archive_family(namespace, name):
    """Recognize only explicitly admitted hash-addressed forecast and diagnostic families."""
    if namespace in EXPIRING_NAMESPACES:
        for kind in EXPIRING_KINDS:
            prefix = kind + "-"
            if name.startswith(prefix) and name.endswith(".json"):
                identity = name[len(prefix) : -len(".json")]
                if REFERENCE_PATTERN.fullmatch(identity):
                    return kind, identity
    return None, None


def inline_native_weights(value):
    """Keep legacy demand originals with embedded native weights outside automatic expiry."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "model_json" in item:
                return True
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def _replace_links(connection, owner_kind, owner_namespace, owner_name, value):
    """Replace one validated owner's outgoing references in its already-open transaction."""
    connection.execute(
        DOCUMENT_LINKS.delete().where(
            DOCUMENT_LINKS.c.owner_kind == owner_kind,
            DOCUMENT_LINKS.c.owner_namespace == owner_namespace,
            DOCUMENT_LINKS.c.owner_name == owner_name,
        )
    )
    references = reference_hashes(value)
    if references:
        connection.execute(
            DOCUMENT_LINKS.insert(),
            [
                {
                    "owner_kind": owner_kind,
                    "owner_namespace": owner_namespace,
                    "owner_name": owner_name,
                    "target_hash": reference,
                }
                for reference in sorted(references)
            ],
        )


def index_document(connection, namespace, name, value, now=None):
    """Keep first-storage age and current outgoing links atomic with a document write.

    Old databases acquire their age when first indexed. Embedded model or
    provider timestamps cannot establish when that particular archive was saved.
    Mutable pointers retain their first-storage age but are never expiry candidates.
    """
    key = (DOCUMENT_METADATA.c.namespace == namespace) & (DOCUMENT_METADATA.c.name == name)
    if connection.scalar(select(DOCUMENT_METADATA.c.name).where(key)) is None:
        kind, identity = archive_family(namespace, name)
        if namespace == "demand" and inline_native_weights(value):
            kind, identity = None, None
        connection.execute(
            DOCUMENT_METADATA.insert().values(
                namespace=namespace,
                name=name,
                created_at=utc_stamp(datetime.now(UTC) if now is None else now),
                identity=identity,
                kind=kind,
            )
        )
    _replace_links(connection, DOCUMENT_OWNER, namespace, name, value)


def index_value(connection, digest, value):
    """Protect document references held by one shared original-plan JSON leaf."""
    _replace_links(connection, VALUE_OWNER, "", digest, value)


def forget_document(connection, namespace, name):
    """Remove one document's auxiliary index rows inside the same deletion transaction."""
    connection.execute(
        DOCUMENT_LINKS.delete().where(
            DOCUMENT_LINKS.c.owner_kind == DOCUMENT_OWNER,
            DOCUMENT_LINKS.c.owner_namespace == namespace,
            DOCUMENT_LINKS.c.owner_name == name,
        )
    )
    connection.execute(
        DOCUMENT_METADATA.delete().where(
            DOCUMENT_METADATA.c.namespace == namespace, DOCUMENT_METADATA.c.name == name
        )
    )


def forget_value(connection, digest):
    """Remove links only after their shared original-plan leaf becomes unreachable."""
    connection.execute(
        DOCUMENT_LINKS.delete().where(
            DOCUMENT_LINKS.c.owner_kind == VALUE_OWNER,
            DOCUMENT_LINKS.c.owner_namespace == "",
            DOCUMENT_LINKS.c.owner_name == digest,
        )
    )
