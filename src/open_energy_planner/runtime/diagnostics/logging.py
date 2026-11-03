"""Timestamp container diagnostics and report application lifecycle without storing another log archive."""

import json
import logging
import os
import sys
import time
import traceback
from copy import copy

from aiohttp import ClientError, ClientResponseError

from open_energy_planner.core.units import SECONDS_PER_MINUTE


def failure_trace(error):
    """Retain the full stack locations without source lines, locals or private exception values."""
    return "\n".join(
        f"{frame.filename}:{frame.lineno}: {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    )


def owned_validation_message(error, messages):
    """Return a canonical reviewed literal only when the exception has exactly that sole argument."""
    for message in messages:
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] == message:
            return message
    return None


def request_error_message(error, *, logger, operation, safe_messages=(), fallback):
    """Return only owned validation text and diagnose unmatched failures without disclosing values.

    Each request owner explicitly admits its fixed public validation messages.
    Return the canonical message, never exception text. A broad exception catch
    can also include storage/driver defects, so unmatched messages stay private.
    """
    message = owned_validation_message(error, safe_messages)
    if message is not None:
        logger.debug("%s rejected: %s", operation, message)
        return message
    if isinstance(error, json.JSONDecodeError):
        logger.debug("%s rejected: Invalid JSON", operation)
        return fallback
    logger.error(
        "%s failed (%s). Returning the public recovery explanation.\n%s",
        operation,
        type(error).__name__,
        failure_trace(error),
    )
    return fallback


def failure_reason(error):
    """Describe expected transport/I/O failures without provider URLs, credentials or payloads."""
    if isinstance(error, ClientResponseError):
        return f"HTTP {error.status}"
    if isinstance(error, TimeoutError):
        return "request timed out"
    if isinstance(error, ClientError):
        return "connection failed"
    if isinstance(error, OSError):
        return os.strerror(error.errno) if type(error.errno) is int else "I/O unavailable"
    return None


class BackgroundLog:
    """Log first readiness, changed failures and recovery at normal levels, keeping repeats at DEBUG.

    Each instance belongs to one independently retried operation. It never changes
    scheduling, validity, failure handling or persistent state. Callers supply safe
    owned impact text and the actual retry interval in seconds when one is known.
    """

    def __init__(self, logger, operation):
        """Borrow the responsible module's logger and a nonsecret operation label."""
        self.logger, self.operation = logger, operation
        self.failure_key = None
        self.has_succeeded = False

    def exception(self, impact, *, retry_seconds=None, expected=False, reason=None, waiting=False):
        """Report cause, retained behavior and retry without exposing expected exception contents.

        Unexpected defects retain their traceback. Repeated unchanged failures
        move to DEBUG until recovery, avoiding a traceback every worker poll.
        """
        error = sys.exception()
        if error is None:
            raise RuntimeError("Background exception logging requires an active exception handler")
        known_reason = failure_reason(error)
        self._failure(
            impact,
            reason=reason or known_reason or type(error).__name__,
            retry_seconds=retry_seconds,
            waiting=waiting,
            error=error,
            unexpected=not expected and known_reason is None,
        )

    def unavailable(self, impact, *, reason, retry_seconds=None, waiting=False):
        """Report missing usable results without inventing an exception, readiness or a retry policy.

        The caller supplies owned reason/impact text and the existing next attempt.
        This changes only diagnostic state. An empty successful response may still
        retain its normal refresh interval or cache validity.
        """
        self._failure(impact, reason=reason, retry_seconds=retry_seconds, waiting=waiting)

    def _failure(
        self, impact, *, reason, retry_seconds=None, waiting=False, error=None, unexpected=False
    ):
        """Share failure deduplication and formatting for exceptions and unavailable evidence."""
        trace = error.__traceback__ if error is not None else None
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        site = (trace.tb_frame.f_code.co_filename, trace.tb_lineno) if trace is not None else None
        failure_key = type(error) if error is not None else None, reason, impact, site
        repeated = failure_key == self.failure_key
        self.failure_key = failure_key
        retry = ""
        if retry_seconds is not None:
            if retry_seconds % SECONDS_PER_MINUTE == 0:
                amount, unit = retry_seconds / SECONDS_PER_MINUTE, "minute"
            else:
                amount, unit = retry_seconds, "second"
            retry = f" Retrying in {amount:g} {unit}{'' if amount == 1 else 's'}."
        self.logger.log(
            logging.DEBUG
            if repeated
            else logging.INFO
            if waiting
            else logging.ERROR
            if unexpected
            else logging.WARNING,
            "%s %s (%s). %s%s",
            self.operation,
            "pending" if waiting else "unavailable" if error is None else "failed",
            reason,
            impact,
            retry,
            exc_info=(type(error), error, error.__traceback__) if unexpected else None,
        )

    def success(self, summary):
        """Announce first usable results and real recovery, with routine successful refreshes at DEBUG."""
        state = "recovered" if self.failure_key is not None else "ready"
        self.logger.log(
            logging.INFO
            if self.failure_key is not None or not self.has_succeeded
            else logging.DEBUG,
            "%s %s. %s",
            self.operation,
            state,
            summary,
        )
        self.has_succeeded, self.failure_key = True, None


class UTCFormatter(logging.Formatter):
    """Render unambiguous UTC timestamps independently of the container's local time zone."""

    converter = time.gmtime

    def format(self, record):
        """Prefix every message/traceback line without changing records shared with other handlers."""
        rendered = super().format(copy(record))
        header = copy(record)
        header.msg, header.args = "", ()
        header.exc_info = header.exc_text = header.stack_info = None
        prefix = super().format(header)
        lines = rendered.splitlines() or [""]
        return "\n".join([lines[0], *(prefix + line for line in lines[1:])])


def configure_logging():
    """Write timestamped diagnostics to stdout for Supervisor/Docker. Keep routine HTTP access noise disabled."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        UTCFormatter(
            "%(asctime)s.%(msecs)03dZ %(levelname)s [%(name)s] %(message)s", "%Y-%m-%dT%H:%M:%S"
        )
    )
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    logging.getLogger("open_energy_planner").setLevel(logging.INFO)


def register_lifecycle(app, release, source):
    """Report initialization and graceful shutdown, emitting stopped only after other cleanup contexts drain."""
    logger = logging.getLogger(__name__)

    async def lifetime(_app):
        """Bracket all resource contexts, including cleanup after an interrupted initialization."""
        logger.info("Open Energy Planner starting (release=%s, source=%s)", release, source)
        from open_energy_planner.build_info import BUILD_INFO

        if BUILD_INFO.get("build"):
            logger.info(
                "Application build: %s. Source revision: %s.",
                BUILD_INFO["build"],
                BUILD_INFO.get("revision") or "unversioned",
            )
        try:
            yield
        finally:
            logger.info("Open Energy Planner stopped")

    async def started(_app):
        """Confirm application startup callbacks have completed. Socket binding is owned by the server."""
        logger.info("Open Energy Planner started")

    async def stopping(_app):
        """Mark graceful shutdown before request handlers and background resources finish."""
        logger.info("Open Energy Planner stopping")

    app.cleanup_ctx.insert(0, lifetime)
    app.on_startup.append(started)
    app.on_shutdown.append(stopping)
