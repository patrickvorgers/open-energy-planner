"""Timestamp container diagnostics and report application lifecycle without storing another log archive."""

import logging
import sys
import time
from copy import copy


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
