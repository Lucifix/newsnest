"""Data shapes shared by fetchers and (later) the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class FetchStatus(str, Enum):
    """Outcome of one fetch. "empty" and a failure must never look the same."""

    OK = "ok"                    # HTTP 200, parsed, at least one usable item
    EMPTY = "empty"              # HTTP 200, valid feed, genuinely zero entries
    PARSE_ERROR = "parse_error"  # HTTP 200, but the body is not what we expect (format change)
    HTTP_ERROR = "http_error"    # non-2xx that is not a block
    BLOCKED = "blocked"          # 403/429/captcha: back off, never retry aggressively
    NETWORK_ERROR = "network_error"  # DNS, timeout, TLS...
    NOT_IMPLEMENTED = "not_implemented"  # fetcher type with no implementation

    @property
    def is_failure(self) -> bool:
        return self not in (FetchStatus.OK, FetchStatus.EMPTY)


@dataclass
class Item:
    source_id: str
    category: str          # "news" (Local) | "tech" | "world" | "lifestyle"
    title: str
    url: str               # canonical URL
    guid: str              # stable id from the feed, falls back to the url
    published: datetime | None  # timezone-aware UTC
    snippet: str = ""
    publisher: str = ""    # human name of the outlet, e.g. "24ur.com"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    source_id: str
    status: FetchStatus
    items: list[Item] = field(default_factory=list)
    http_status: int | None = None
    # Entries in the response before any filtering; the heartbeat uses this, so a release feed
    # with nothing recent is not mistaken for a dead feed
    raw_count: int = 0
    # Validators for the next conditional request ("changed since last time?")
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
