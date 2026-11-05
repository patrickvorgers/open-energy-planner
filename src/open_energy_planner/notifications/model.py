"""Share active problem presentation without owning domain decisions or durable state."""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

# Match JavaScript's Number.MAX_SAFE_INTEGER at the JSON boundary.
MAX_NOTIFICATION_LIFETIME_MINUTES = 2**53 - 1


class Severity(StrEnum):
    """Describe importance independently of which operations a problem blocks."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Lifecycle(StrEnum):
    """Retain problems during recovery. A successful source omission confirms resolution."""

    ACTIVE = "active"
    RECOVERING = "recovering"


@dataclass(frozen=True)
class NoticeText:
    """Keep a module's factual explanation and optional recovery destination together."""

    title: str
    message: str
    action_label: str | None = None
    action_path: str | None = None
    level: Severity = Severity.WARNING

    def __post_init__(self):
        """Reject invalid provider output before it can replace last-known evidence."""
        if any(
            not isinstance(value, str) or not value.strip() for value in (self.title, self.message)
        ):
            raise ValueError("A notification needs a title and explanation")
        object.__setattr__(self, "level", Severity(self.level))
        if (self.action_label is None) != (self.action_path is None):
            raise ValueError("A recovery action needs both label and destination")
        if self.action_path is not None and (
            not isinstance(self.action_label, str)
            or not self.action_label.strip()
            or not isinstance(self.action_path, str)
            or not self.action_path.startswith("/")
            or self.action_path.startswith("//")
            or "\\" in self.action_path
        ):
            raise ValueError("A recovery action must stay in the current installation")


@dataclass(frozen=True)
class Notification:
    """Describe one current cause, retaining its key while its operation recovers.

    ``key`` identifies the cause and affected resource, never its wording or read time.
    ``blocks`` names affected operations without changing their admission policy.
    ``since`` is an optional timezone-aware start of the current process-local episode.
    ``detail`` is authenticated diagnostic data and must not contain credentials.
    ``auto_clear_minutes`` describes the source-owned lifetime of a temporary notice.
    Resolution is absence from a successful complete source read, never a dismissal.
    """

    key: str
    code: str
    text: NoticeText
    state: Lifecycle = Lifecycle.ACTIVE
    blocks: tuple[str, ...] = ()
    since: datetime | None = None
    detail: str | None = None
    auto_clear_minutes: int | None = None

    def __post_init__(self):
        """Validate the module-to-collector boundary without interpreting domain codes."""
        if any(not isinstance(value, str) or not value.strip() for value in (self.key, self.code)):
            raise ValueError("A notification needs a stable key and cause code")
        if not isinstance(self.text, NoticeText):
            raise TypeError("A notification needs validated presentation text")
        object.__setattr__(self, "state", Lifecycle(self.state))
        if not isinstance(self.blocks, tuple) or any(
            not isinstance(operation, str) or not operation.strip() for operation in self.blocks
        ):
            raise ValueError("Blocked operations must be explicit identifiers")
        if self.since is not None and (
            not isinstance(self.since, datetime) or self.since.utcoffset() is None
        ):
            raise ValueError("A notification start must include its timezone")
        if self.detail is not None and not isinstance(self.detail, str):
            raise TypeError("Notification details must be text")
        if self.auto_clear_minutes is not None and (
            type(self.auto_clear_minutes) is not int
            or not 0 < self.auto_clear_minutes <= MAX_NOTIFICATION_LIFETIME_MINUTES
        ):
            raise ValueError(
                "A temporary notification needs a positive whole-minute lifetime within the JSON safe-integer range"
            )

    def document(self, owner: str, *, stale: bool = False, fallback: bool = False) -> dict:
        """Serialize display fields and ownership without exposing executable actions."""
        return {
            **asdict(self.text),
            "key": self.key,
            "owner": owner,
            "code": self.code,
            "state": self.state.value,
            "blocks": list(self.blocks),
            "since": self.since.isoformat() if self.since else None,
            "detail": self.detail,
            "stale": stale,
            "fallback": fallback,
            **({"auto_clear_minutes": self.auto_clear_minutes} if self.auto_clear_minutes else {}),
        }
