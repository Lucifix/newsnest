"""Small surprises. Nothing here changes what the feed shows or how it ranks.

The end of the feed now and then carries one more line under its heading, in about one feed session in
ONE_IN. The line is picked from the session's id, so it holds still across reloads, tab switches and Back,
like the feed itself; a new session can bring another line, or none.

The other one lives in the browser (app.js): the logo on the About page.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

ONE_IN = 5

# Under "That's everything": the reader scrolled to the end of the feed
END = (
    "You reached the bottom of the nest.",
    "The end. A real one.",
    "This feed has a bottom. You found it.",
    "It doesn't refill itself. By design.",
    "No more scroll. Try a walk.",
)
# Under "You're all caught up": nothing unread
EMPTY = (
    "The nest is tidy.",
    "No news is good news.",
    "Quiet out there. Enjoy it.",
    "Nothing to see here. Go outside.",
    "You win this round, internet.",
)
NIGHT = "The news is asleep. You could be too."   # caught up between midnight and 5
EARLY = "Up before the news. Impressive."         # caught up between 5 and 7


def caughtup_aside(seed: str | None, empty: bool, now_local: datetime | None = None) -> str | None:
    """The extra line for the end of the feed, or None (most of the time). `empty` says the feed has nothing
    unread. now_local is the reader's wall clock when it is known; the lines about the hour are left out
    without it, and show in every other session, since a feed read empty at 3 a.m. is rare enough."""
    if not seed:
        return None
    n = int.from_bytes(hashlib.blake2s(seed.encode("utf-8"), digest_size=4).digest(), "big")
    if empty and now_local is not None and now_local.hour < 7 and n % 2 == 0:
        return NIGHT if now_local.hour < 5 else EARLY
    if n % ONE_IN:
        return None
    lines = EMPTY if empty else END
    return lines[(n // ONE_IN) % len(lines)]
