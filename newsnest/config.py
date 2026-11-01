"""Load and validate config/config.yaml.

The config folder is re-read on every run, so edits apply without a rebuild.
A broken config is reported as an error, never a crash with a stack trace.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

class _Strict(BaseModel):
    # Typos in config.yaml should be reported, not silently ignored
    model_config = ConfigDict(extra="forbid")


# Sections: the groups a feed belongs to, shown as tabs and chips. The key is stored with every story, so it
# never changes; the label is what the reader sees. "news" (Local) and "world" are built in: they can be
# renamed but not removed, and they share the News tab.
NEWS, WORLD = "news", "world"
ICONS = ("chip", "watch", "ball", "flask", "briefcase", "film", "leaf", "star")
RESERVED_KEYS = ("all", "headlines", "cat", "top")   # filter names of the feed page, block names of the digest
MAX_SECTIONS = 12
MAX_TABS = 4                                  # in the dock next to For you; News counts as one
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


class SectionCfg(_Strict):
    key: str
    label: str
    icon: str = "star"
    # Shown in the dock. Off: the section is a chip under For you. Ignored for Local and World (the News tab).
    tab: bool = True
    # Poll its feeds this often; empty = the schedules: values
    poll_minutes: int | None = Field(default=None, ge=1)

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        if not _KEY.match(v) or v in RESERVED_KEYS:
            raise ValueError(f"section key {v!r} must be 1-24 lowercase letters, digits or dashes (not {', '.join(RESERVED_KEYS)})")
        return v

    @field_validator("label")
    @classmethod
    def _label_ok(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or len(v) > 24:
            raise ValueError("a section name needs 1-24 characters")
        return v

    @field_validator("icon")
    @classmethod
    def _icon_ok(cls, v: str) -> str:
        if v not in ICONS:
            raise ValueError(f"unknown icon {v!r}, use one of: {', '.join(ICONS)}")
        return v


# What a config without a `sections:` list has always had: the order is the dock's (Tech, News, Lifestyle).
LEGACY_SECTIONS = (SectionCfg(key="tech", label="Tech", icon="chip"), SectionCfg(key=NEWS, label="Local"),
                   SectionCfg(key=WORLD, label="World"), SectionCfg(key="lifestyle", label="Lifestyle", icon="watch"))
# Sections that packs and OPML folders know by name, created on demand when a pack needs one that isn't there
KNOWN_SECTIONS = {s.key: s for s in LEGACY_SECTIONS}


class FeedCfg(_Strict):
    id: str
    name: str | None = None
    url: str
    category: str   # a section key
    enabled: bool = True
    daily_cap: int | None = None
    # Rewrite http:// item links to https:// (for feeds that still publish http links)
    https_links: bool = False
    # Plain-http feed URLs are rejected unless explicitly allowed
    allow_http: bool = False

    @model_validator(mode="after")
    def _https_only(self) -> "FeedCfg":
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"feed '{self.id}' needs a web address starting with https://")
        if self.url.startswith("http://") and not self.allow_http:
            raise ValueError(f"feed '{self.id}' uses http://: use https, or set allow_http: true")
        return self


class WatchCfg(_Strict):
    name: str
    # Case-insensitive, matched at the start of a word: "electr" matches electric, electricity
    keywords: list[str]


class GithubReleasesCfg(_Strict):
    category: str = "tech"   # a section key
    repos: list[str] = Field(default_factory=list)
    daily_cap: int | None = None
    # Betas and release candidates (2026.10.0b5, v10.9.0-rc1) are skipped unless true
    include_prereleases: bool = False


class SchedulesCfg(_Strict):
    news_minutes: int = 15
    tech_minutes: int = 30
    world_minutes: int = 20

    def poll_minutes(self, category: str) -> int:
        return {"news": self.news_minutes, "world": self.world_minutes}.get(category, self.tech_minutes)


class CategoryCfg(_Strict):
    daily_cap_per_source: int | None = None


class PrefilterCfg(_Strict):
    mute_keywords: list[str] = Field(default_factory=list)
    must_show_keywords: list[str] = Field(default_factory=list)


class WeightsCfg(_Strict):
    topic: float = 0.4
    importance: float = 0.3
    personal: float = 0.3


class ScoringCfg(_Strict):
    enabled: bool = True
    model: str = "claude-haiku-5-5"
    # Thinking effort for scoring and summaries: low | medium | high
    effort: Literal["low", "medium", "high"] = "low"
    batch_size: int = 20
    # Wait for this many new items before a scoring call (each call carries a fixed prompt)...
    min_batch: int = Field(default=15, ge=1)
    # ...unless the oldest one has waited this long. A manual refresh always scores at once.
    max_wait_minutes: int = Field(default=20, ge=0)
    weights: WeightsCfg = Field(default_factory=WeightsCfg)
    cross_source_bonus: float = 1.0
    summary_threshold: float = 6.0
    top_story_importance: int = 8
    summary_language: str = "English"
    calibration_mode: bool = True
    # Fixed list so "news by category" stays a handful of sections; Claude must pick one of these
    categories: list[str] = Field(default_factory=lambda: [
        "Local", "National", "Politics", "Economy", "World", "Crime & accidents", "Health",
        "Science", "Culture", "Sports", "Celebrities & lifestyle", "AI", "Software & releases",
        "Hardware & gadgets", "Security", "Tech business",
        "Gear & EDC", "Watches", "Cars & motoring", "Style & grooming", "Food & drink", "Design & home",
        "Travel & outdoors", "Other",
    ])
    # Model for the rare, quality-sensitive jobs: drafting the profile and the weekly interests.md proposal
    profile_model: str = "claude-opus-5-5"
    # Days between automatic interests.md proposals (0 = off)
    proposal_every_days: int = 7

    @model_validator(mode="after")
    def _categories(self) -> "ScoringCfg":
        if "Other" not in self.categories:
            self.categories.append("Other")
        return self


class DigestCfg(_Strict):
    # Outside calibration mode, items with a final score below this are hidden (must-show items never are)
    min_final: float = 5.0


class HomeFeedCfg(_Strict):
    # The home feed holds unread stories from this many hours back (not the calendar day)
    window_hours: float = 36
    # Freshness halves every this many hours...
    half_life_hours: float = 24
    # ...but never below this share of the score, so a strong story from yesterday beats a weak fresh one
    freshness_floor: float = 0.5
    # A story shown this many times without being opened drops out (top stories only sink)
    seen_drop_after: int = 3
    # About one card in this many is a story from a category the feed rarely shows, even below
    # digest.min_final, so interests can grow beyond the profile (0 turns it off)
    explore_every: int = 8
    # ...but never a story scored below this (advertorials, clickbait)
    explore_min_final: float = 3.0
    # A source added by hand gets its best few stories into the feed for this many days, whatever they
    # score, so 👍/👎 can decide whether it stays
    new_source_days: int = 14
    new_source_stories: int = 2
    # Mark stories read once they have been on screen and are scrolled past (not counted as "opened")
    auto_read_on_scroll: bool = False


class DiscoveryCfg(_Strict):
    # Candidate sources from OPML imports / the starter pack (managed in Settings)
    enabled: bool = True
    # Candidates are polled this often (gentler than regular feeds)
    poll_minutes: int = 90
    # A discovered story reaches the feed only at or above this score (top stories always do)
    min_final: float = 6.0
    # At most this many stories per discovered source per day
    daily_cap_per_source: int = 4
    # Score at most this many items per discovered source per 24 h (newest first); the rest are never shown
    # anyway. Watched and must-show items are always scored. 0 = no limit.
    scored_per_source: int = Field(default=12, ge=0)
    # Suggest promoting a candidate after this many opens or 👍 of its stories within a week
    promote_after: int = 3
    # Retire a candidate that produced nothing above min_final within this many days
    retire_after_days: int = 21


class BudgetCfg(_Strict):
    daily_usd: float = 0.50


class HeartbeatCfg(_Strict):
    news_empty_hours: float = 3


class HttpCfg(_Strict):
    user_agent: str = "Mozilla/5.0 (compatible; NewsNest/0.1; self-hosted feed reader)"
    timeout_seconds: float = 20


class RegionCfg(_Strict):
    # Where the reader lives. Empty country = no local focus, and the AI prompts stay neutral.
    country: str = ""
    city: str = ""
    # A feed on this domain always counts as Local, e.g. ".si" (optional)
    tld: str = ""

    @field_validator("country", "city")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("tld")
    @classmethod
    def _tld(cls, v: str) -> str:
        v = v.strip().lower()
        return f".{v.lstrip('.')}" if v else ""

    @property
    def place(self) -> str:
        """"Ljubljana, Slovenia", "Slovenia", or "" when no country is set."""
        if not self.country:
            return ""
        return f"{self.city}, {self.country}" if self.city else self.country


class Config(_Strict):
    timezone: str = "UTC"
    region: RegionCfg = Field(default_factory=RegionCfg)
    # Feed entries older than this are dropped at fetch time (some feeds carry evergreen pages)
    max_item_age_days: float = 3
    http: HttpCfg = Field(default_factory=HttpCfg)
    schedules: SchedulesCfg = Field(default_factory=SchedulesCfg)
    # None: the sections every version before this one had (LEGACY_SECTIONS). The first edit in Settings
    # writes the list into the file.
    sections: list[SectionCfg] | None = None
    feeds: list[FeedCfg] = Field(default_factory=list)
    github_releases: GithubReleasesCfg = Field(default_factory=GithubReleasesCfg)
    categories: dict[str, CategoryCfg] = Field(default_factory=dict)
    watches: list[WatchCfg] = Field(default_factory=list)
    prefilter: PrefilterCfg = Field(default_factory=PrefilterCfg)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    digest: DigestCfg = Field(default_factory=DigestCfg)
    feed: HomeFeedCfg = Field(default_factory=HomeFeedCfg)
    discovery: DiscoveryCfg = Field(default_factory=DiscoveryCfg)
    budget: BudgetCfg = Field(default_factory=BudgetCfg)
    heartbeat: HeartbeatCfg = Field(default_factory=HeartbeatCfg)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        v = v.strip()
        try:
            ZoneInfo(v)
        except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"unknown time zone {v!r}, e.g. Europe/Berlin or America/New_York") from None
        return v

    @property
    def all_sections(self) -> list[SectionCfg]:
        """The sections in order: the configured list, or the legacy one."""
        return list(LEGACY_SECTIONS) if self.sections is None else self.sections

    def poll_minutes(self, category: str) -> int:
        """How often a section's feeds are polled: its own setting, else the schedules: values."""
        for s in self.all_sections:
            if s.key == category and s.poll_minutes:
                return s.poll_minutes
        return self.schedules.poll_minutes(category)

    @model_validator(mode="after")
    def _sections_ok(self) -> "Config":
        if self.sections is not None:
            keys = [s.key for s in self.sections]
            if dupes := sorted({k for k in keys if keys.count(k) > 1}):
                raise ValueError(f"sections: duplicate key {', '.join(dupes)}")
            # Local and World are built in: put back one that was deleted by hand
            self.sections[:0] = [SectionCfg(key=k, label=KNOWN_SECTIONS[k].label) for k in (NEWS, WORLD) if k not in keys]
            if len(self.sections) > MAX_SECTIONS:
                raise ValueError(f"sections: at most {MAX_SECTIONS} sections")
            tabs = 1 + sum(s.tab for s in self.sections if s.key not in (NEWS, WORLD))
            if tabs > MAX_TABS:
                raise ValueError(f"sections: at most {MAX_TABS} tabs, and News is one of them. "
                                 "Turn off 'tab' for a section to make it a chip under For you")
        known = {s.key for s in self.all_sections}
        for f in self.feeds:
            if f.category not in known:
                raise ValueError(f"feed '{f.id}' is in section '{f.category}', which isn't listed under sections:")
        if self.github_releases.repos and self.github_releases.category not in known:
            raise ValueError(f"github_releases is in section '{self.github_releases.category}', which isn't listed under sections:")
        return self


@dataclass
class Source:
    """One concrete thing to poll, flattened out of the config sections."""

    id: str
    type: str          # fetcher registry key
    category: str
    name: str
    url: str
    enabled: bool = True
    daily_cap: int | None = None
    params: dict = field(default_factory=dict)


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "x"


def build_sources(cfg: Config) -> list[Source]:
    """Expand config sections into a flat list of sources with stable ids."""
    def cap(explicit: int | None, category: str) -> int | None:
        if explicit is not None:
            return explicit
        cat = cfg.categories.get(category)
        return cat.daily_cap_per_source if cat else None

    sources: list[Source] = []
    for f in cfg.feeds:
        sources.append(Source(id=f.id, type="rss", category=f.category, name=f.name or f.id,
                              url=f.url, enabled=f.enabled, daily_cap=cap(f.daily_cap, f.category),
                              params={"https_links": f.https_links}))

    gh = cfg.github_releases
    for repo in gh.repos:
        sources.append(Source(id=f"gh-{slugify(repo.replace('/', '-'))}", type="github_releases",
                              category=gh.category, name=f"{repo} releases",
                              url=f"https://github.com/{repo}/releases.atom",
                              daily_cap=cap(gh.daily_cap, gh.category),
                              params={"repo": repo, "include_prereleases": gh.include_prereleases}))

    seen: set[str] = set()
    for s in sources:
        if s.id in seen:
            raise ValueError(f"duplicate source id '{s.id}': give one of them an explicit id")
        seen.add(s.id)
    return sources


@dataclass
class LoadedConfig:
    config: Config | None
    sources: list[Source]
    errors: list[str]


log = logging.getLogger(__name__)

# Sections that older versions understood (Google News queries, saved listing searches). Configs that
# still carry them keep loading; the sections are ignored.
_warned: set[str] = set()
_REMOVED_SECTIONS = ("google_news", "listings")


def drop_removed(raw: dict) -> list[str]:
    """Remove the options older versions understood from a parsed config; returns which were there."""
    dropped = [k for k in _REMOVED_SECTIONS if raw.pop(k, None) is not None]
    heartbeat = raw.get("heartbeat")
    if isinstance(heartbeat, dict) and heartbeat.pop("listing_failed_runs", None) is not None:
        dropped.append("heartbeat.listing_failed_runs")
    return dropped


def load_config(config_dir: Path) -> LoadedConfig:
    path = config_dir / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return LoadedConfig(None, [], [f"{path} not found"])
    except yaml.YAMLError as e:
        return LoadedConfig(None, [], [f"{path}: invalid YAML: {e}"])
    if isinstance(raw, dict) and (dropped := drop_removed(raw)):
        if (note := ", ".join(dropped)) not in _warned:  # the config is re-read every tick: say it once
            _warned.add(note)
            log.warning("config.yaml: ignoring options that no longer exist: %s", note)
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        errs = [f"{path}: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        return LoadedConfig(None, [], errs)
    try:
        sources = build_sources(cfg)
    except ValueError as e:
        return LoadedConfig(cfg, [], [f"{path}: {e}"])
    return LoadedConfig(cfg, sources, [])


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def is_blank_markdown(text: str) -> bool:
    """Profile files ship with explanatory HTML comments only; those still count as empty."""
    return not _HTML_COMMENT.sub("", text).strip()
