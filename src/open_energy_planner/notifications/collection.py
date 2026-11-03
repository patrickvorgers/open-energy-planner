"""Collect module-owned current problems without knowing their causes or recovery rules."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .model import NoticeText, Notification

_LOGGER = logging.getLogger(__name__)
ReadNotices = Callable[[], tuple[Notification, ...] | None]
ExplainCause = Callable[[str, str | None], NoticeText | None]


@dataclass(frozen=True)
class Source:
    """Bind one module's synchronous read and optional propagated-cause explanation."""

    owner: str
    read: ReadNotices
    explain: ExplainCause | None = None
    fallback: bool = False


class NotificationCenter:
    """Own startup registration and a disposable last-known cache for source failures.

    Sources run synchronously in one event-loop turn. They read existing state and
    must not refresh inputs, perform network requests or mutate product data.
    Successful empty reads confirm recovery. Unknown or failed reads retain that
    source's previous notices as stale. No inbox, dismissal or database is owned here.
    Authoritative sources take precedence over dependent fallback explanations
    sharing the same cause key, independently of registration order.
    """

    def __init__(self):
        """Start without module assumptions, cached notices or persistent history."""
        self._sources: dict[str, Source] = {}
        self._last: dict[str, tuple[Notification, ...]] = {}
        self._failed: set[tuple[str, str]] = set()
        self._started = False

    def register(
        self, owner: str, read: ReadNotices, *, explain: ExplainCause | None = None, fallback=False
    ):
        """Connect a unique module during app construction, before any collection."""
        if self._started:
            raise RuntimeError("Notification sources must be registered before reads")
        if not isinstance(owner, str) or not owner.strip() or owner in self._sources:
            raise ValueError("Notification source ownership must be unique")
        if not callable(read) or (explain is not None and not callable(explain)):
            raise TypeError("Notification sources and explanations must be callable")
        self._sources[owner] = Source(owner, read, explain, bool(fallback))

    def _ordered(self):
        """Let domain owners claim a cause before dependent generic fallbacks."""
        return sorted(self._sources.values(), key=lambda source: source.fallback)

    def _failure(self, identity, error):
        """Remember a failure transition and retain its stack without the private exception payload."""
        first = identity not in self._failed
        self._failed.add(identity)
        # Provider exception text can contain private input. Retain stack frames only.
        return (
            (RuntimeError, RuntimeError("Notification source unavailable"), error.__traceback__)
            if first
            else None
        )

    def explain(self, reason: str, detail: str | None = None) -> NoticeText | None:
        """Delegate a propagated cause to its owner without a central code registry."""
        for source in self._ordered():
            if source.explain is None:
                continue
            try:
                text = source.explain(reason, detail)
                if text is not None and not isinstance(text, NoticeText):
                    raise TypeError("Invalid notification explanation")
                self._failed.discard((source.owner, "explain"))
                if text is not None:
                    return text
            except Exception as error:
                if evidence := self._failure((source.owner, "explain"), error):
                    _LOGGER.exception(
                        "Notification source %s failed (%s)",
                        source.owner,
                        type(error).__name__,
                        exc_info=evidence,
                    )
        return None

    def document(self) -> dict:
        """Read every source, deduplicate cause keys and retain failed-source evidence."""
        self._started = True
        notices = {}
        unavailable = []
        for source in self._ordered():
            try:
                current = source.read()
                if current is None:
                    unavailable.append(source.owner)
                else:
                    current = tuple(current)
                    if any(not isinstance(notice, Notification) for notice in current):
                        raise TypeError("Invalid notification source result")
                    if len({notice.key for notice in current}) != len(current):
                        raise ValueError("Duplicate notification keys in a source")
                    self._last[source.owner] = current
                    self._failed.discard((source.owner, "read"))
            except Exception as error:
                if evidence := self._failure((source.owner, "read"), error):
                    _LOGGER.exception(
                        "Notification source %s failed (%s)",
                        source.owner,
                        type(error).__name__,
                        exc_info=evidence,
                    )
                unavailable.append(source.owner)
            for notice in self._last.get(source.owner, ()):
                notices.setdefault(
                    notice.key,
                    notice.document(
                        source.owner, stale=source.owner in unavailable, fallback=source.fallback
                    ),
                )
        return {"notifications": list(notices.values()), "unavailable_sources": unavailable}
