"""Expire only old, unreferenced forecast families after bounded reference-index backfill."""

import json
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from open_energy_planner.storage.adapters.payloads import decode_payload

from .document_references import (
    DOCUMENT_OWNER,
    EXPIRING_KINDS,
    REFERENCE_PATTERN,
    VALUE_OWNER,
    archive_family,
    forget_document,
    index_document,
    index_value,
    utc_stamp,
)
from .schema import DOCUMENT_LINKS, DOCUMENT_METADATA, DOCUMENTS, VALUES

DOCUMENT_RETENTION_DAYS = 30
REFERENCE_BACKFILL_BATCH_ROWS = 128
DOCUMENT_RETENTION_BATCH_ROWS = 128
PROGRESS_DOCUMENT = "archive-retention.json"


def _after_pair(first, second, cursor):
    """Continue a portable two-column lexical cursor without tuple-comparison assumptions."""
    return or_(first > cursor[0], and_(first == cursor[0], second > cursor[1]))


def _progress(documents):
    """Reject corrupt maintenance state instead of treating an incomplete index as complete."""
    if not documents.exists(PROGRESS_DOCUMENT):
        return {"stage": "documents", "after": None}
    progress = documents.read(PROGRESS_DOCUMENT)
    if (
        not isinstance(progress, dict)
        or set(progress) != {"stage", "after"}
        or progress["stage"] not in {"documents", "values", "complete"}
    ):
        raise ValueError("Invalid document retention progress")
    cursor = progress["after"]
    if cursor is not None:
        if progress["stage"] == "values":
            valid = isinstance(cursor, str) and REFERENCE_PATTERN.fullmatch(cursor)
        else:
            valid = (
                isinstance(cursor, list)
                and len(cursor) == 2
                and all(isinstance(part, str) and part for part in cursor)
            )
        if not valid:
            raise ValueError("Invalid document retention cursor")
    return progress


def _backfill(connection, progress, now):
    """Index one bounded legacy batch, requiring both owner classes before enabling expiry."""
    cursor = progress["after"]
    if progress["stage"] == "documents":
        query = select(DOCUMENTS).order_by(DOCUMENTS.c.namespace, DOCUMENTS.c.name)
        if cursor is not None:
            query = query.where(_after_pair(DOCUMENTS.c.namespace, DOCUMENTS.c.name, cursor))
        rows = connection.execute(query.limit(REFERENCE_BACKFILL_BATCH_ROWS)).mappings().all()
        for row in rows:
            value = json.loads(decode_payload(row["payload"]))
            index_document(connection, row["namespace"], row["name"], value, now)
        return (
            {"stage": "documents", "after": [rows[-1]["namespace"], rows[-1]["name"]]}
            if len(rows) == REFERENCE_BACKFILL_BATCH_ROWS
            else {"stage": "values", "after": None}
        )
    query = select(VALUES).order_by(VALUES.c.digest)
    if cursor is not None:
        query = query.where(VALUES.c.digest > cursor)
    rows = connection.execute(query.limit(REFERENCE_BACKFILL_BATCH_ROWS)).mappings().all()
    for row in rows:
        index_value(connection, row["digest"], json.loads(decode_payload(row["payload"])))
    return (
        {"stage": "values", "after": rows[-1]["digest"]}
        if len(rows) == REFERENCE_BACKFILL_BATCH_ROWS
        else {"stage": "complete", "after": None}
    )


def _referenced(connection, namespace, identity, names):
    """Protect every extant outside owner, conservatively retaining cross-family cycles."""
    owner_documents = DOCUMENTS.alias("reference_owner_documents")
    owner_values = VALUES.alias("reference_owner_values")
    document_owner_exists = (
        select(owner_documents.c.name)
        .where(
            owner_documents.c.namespace == DOCUMENT_LINKS.c.owner_namespace,
            owner_documents.c.name == DOCUMENT_LINKS.c.owner_name,
        )
        .exists()
    )
    value_owner_exists = (
        select(owner_values.c.digest)
        .where(owner_values.c.digest == DOCUMENT_LINKS.c.owner_name)
        .exists()
    )
    outside_family = or_(
        DOCUMENT_LINKS.c.owner_namespace != namespace,
        DOCUMENT_LINKS.c.owner_name.not_in(names),
    )
    return (
        connection.scalar(
            select(DOCUMENT_LINKS.c.target_hash)
            .where(
                DOCUMENT_LINKS.c.target_hash == identity,
                or_(
                    and_(
                        DOCUMENT_LINKS.c.owner_kind == DOCUMENT_OWNER,
                        outside_family,
                        document_owner_exists,
                    ),
                    and_(DOCUMENT_LINKS.c.owner_kind == VALUE_OWNER, value_owner_exists),
                ),
            )
            .limit(1)
        )
        is not None
    )


def _old_family(connection, namespace, identity, cutoff):
    """Verify complete companion metadata and first-storage ages before deleting any family member."""
    rows = (
        connection.execute(
            select(DOCUMENT_METADATA)
            .join(
                DOCUMENTS,
                (DOCUMENTS.c.namespace == DOCUMENT_METADATA.c.namespace)
                & (DOCUMENTS.c.name == DOCUMENT_METADATA.c.name),
            )
            .where(
                DOCUMENT_METADATA.c.namespace == namespace,
                DOCUMENT_METADATA.c.identity == identity,
                DOCUMENT_METADATA.c.kind.in_(EXPIRING_KINDS),
            )
            .order_by(DOCUMENT_METADATA.c.name)
            .limit(len(EXPIRING_KINDS) + 1)
        )
        .mappings()
        .all()
    )
    if len(rows) > len(EXPIRING_KINDS):
        raise ValueError("Invalid document retention family size")
    for row in rows:
        if archive_family(namespace, row["name"]) != (row["kind"], identity):
            raise ValueError("Invalid document retention family")
        if utc_stamp(datetime.fromisoformat(row["created_at"])) != row["created_at"]:
            raise ValueError("Invalid document retention age")
    return rows if all(row["created_at"] < cutoff for row in rows) else []


def _expire(connection, progress, now):
    """Inspect and delete bounded old families using indexed incoming links and durable keyset paging."""
    cutoff = utc_stamp(now - timedelta(days=DOCUMENT_RETENTION_DAYS))
    query = select(DOCUMENT_METADATA).where(
        DOCUMENT_METADATA.c.kind.in_(EXPIRING_KINDS), DOCUMENT_METADATA.c.created_at < cutoff
    )
    if progress["after"] is not None:
        query = query.where(
            _after_pair(DOCUMENT_METADATA.c.namespace, DOCUMENT_METADATA.c.name, progress["after"])
        )
    rows = (
        connection.execute(
            query.order_by(DOCUMENT_METADATA.c.namespace, DOCUMENT_METADATA.c.name).limit(
                DOCUMENT_RETENTION_BATCH_ROWS
            )
        )
        .mappings()
        .all()
    )
    removed, cursor = 0, None
    for row in rows:
        family = _old_family(connection, row["namespace"], row["identity"], cutoff)
        if removed + len(family) > DOCUMENT_RETENTION_BATCH_ROWS:
            return removed, {"stage": "complete", "after": cursor or progress["after"]}
        cursor = [row["namespace"], row["name"]]
        names = [member["name"] for member in family]
        if not names or _referenced(connection, row["namespace"], row["identity"], names):
            continue
        for name in names:
            connection.execute(
                DOCUMENTS.delete().where(
                    DOCUMENTS.c.namespace == row["namespace"], DOCUMENTS.c.name == name
                )
            )
            forget_document(connection, row["namespace"], name)
        removed += len(names)
    return removed, {
        "stage": "complete",
        "after": cursor if len(rows) == DOCUMENT_RETENTION_BATCH_ROWS else None,
    }


def maintain_documents(documents, now):
    """Backfill once, then expire only unreferenced 30-day forecast details in shared writer order.

    The caller's document transaction serializes new reference publication,
    metadata backfill, family deletion and cursor updates. Failed decoding or
    malformed state rolls the whole batch back. Retained originals, daily quality
    contexts and current model pointers remain live roots. No VACUUM runs here.
    """
    utc_stamp(now)
    with documents.transaction():
        progress = _progress(documents)
        connection = documents.pending.connection
        if progress["stage"] != "complete":
            documents.write(PROGRESS_DOCUMENT, _backfill(connection, progress, now))
            return 0
        removed, progress = _expire(connection, progress, now)
        documents.write(PROGRESS_DOCUMENT, progress)
        return removed
