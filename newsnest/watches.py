"""Keyword watches and prefilters, matched locally against every collected item.

Watches are keyword topics: a watched item is tagged with the watch name and
always shown. mute_keywords drop an item before AI; must_show_keywords (and watches) win over mute.
"""

from __future__ import annotations

from newsnest.config import Config, WatchCfg
from newsnest.models import Item
from newsnest.textmatch import keyword_pattern, matches


class WatchMatcher:
    def __init__(self, watches: list[WatchCfg]):
        self._patterns = [(w.name, keyword_pattern(w.keywords)) for w in watches]

    def match(self, item: Item) -> list[str]:
        return [name for name, pattern in self._patterns if matches(pattern, item.title, item.snippet)]


class Prefilter:
    def __init__(self, cfg: Config):
        self.watches = WatchMatcher(cfg.watches)
        self.mute = keyword_pattern(cfg.prefilter.mute_keywords)
        self.must_show = keyword_pattern(cfg.prefilter.must_show_keywords)

    def classify(self, item: Item) -> tuple[str | None, list[str]]:
        """Returns (prefilter, watches): prefilter is 'must_show', 'muted' or None."""
        watched = self.watches.match(item)
        if watched or matches(self.must_show, item.title, item.snippet):
            return "must_show", watched
        if matches(self.mute, item.title, item.snippet):
            return "muted", watched
        return None, watched
