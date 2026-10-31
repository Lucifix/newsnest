"""Fetcher registry: source type (from config) -> fetcher."""

from __future__ import annotations

from newsnest.fetchers.base import Fetcher
from newsnest.fetchers.rss import GithubReleasesFetcher, RssFetcher


def build_registry() -> dict[str, Fetcher]:
    return {
        "rss": RssFetcher(),
        "github_releases": GithubReleasesFetcher(),
    }
