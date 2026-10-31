"""Delivery behind a small interface. v1 has no pushes; Home Assistant comes later (step 5).

The pipeline only calls notify(); a Home Assistant notifier with quiet hours and daily caps can be
dropped in here without touching anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str                 # top_story
    title: str
    url: str
    details: dict = field(default_factory=dict)


class Notifier(Protocol):
    def notify(self, event: Event) -> None: ...


class LogNotifier:
    """v1: events are only logged; the web page shows them."""

    def notify(self, event: Event) -> None:
        log.info("event", extra={"kind": event.kind, "title": event.title, "url": event.url})


def get_notifier() -> Notifier:
    return LogNotifier()
