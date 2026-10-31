"""Step-1 spike: run every source once and print parsed samples.

    python -m newsnest.spike                     # all enabled sources
    python -m newsnest.spike --source rtvslo     # one source
    python -m newsnest.spike --out spike-output.md

No database, no AI.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from newsnest.bootstrap import config_dir, ensure_dirs
from newsnest.config import load_config
from newsnest.fetchers import build_registry
from newsnest.fetchers.base import FetchContext, make_client, run_fetch
from newsnest.models import FetchResult, FetchStatus
from newsnest.watches import WatchMatcher

_ICON = {FetchStatus.OK: "✅", FetchStatus.EMPTY: "⚪", FetchStatus.NOT_IMPLEMENTED: "⏸️"}


def _age(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "?"
    hours = (now - dt).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"


def render(source, result: FetchResult, samples: int, now: datetime) -> str:
    icon = _ICON.get(result.status, "❌")
    out = [f"## {icon} `{source.id}` · {source.name}",
           f"- type `{source.type}`, category `{source.category}`, status **{result.status.value}**, "
           f"HTTP {result.http_status}, {len(result.items)} items, {result.duration_ms} ms",
           f"- url: {source.url}"]
    dated = [i.published for i in result.items if i.published]
    if dated:
        out.append(f"- published range: oldest {_age(min(dated), now)}, newest {_age(max(dated), now)}"
                   f" ({len(result.items) - len(dated)} undated)")
    elif result.items:
        out.append("- published: **no dates in feed**")
    if result.error:
        out.append(f"- error: `{result.error}`")
    for w in result.warnings:
        out.append(f"- warning: {w}")
    for item in result.items[:samples]:
        out.append("")
        out.append(f"> **{item.title}**  ")
        meta = [item.publisher or "?", item.published.isoformat(timespec="minutes") if item.published else "no date"]
        out.append(f"> {' · '.join(meta)}  ")
        out.append(f"> {item.url}  ")
        if item.snippet:
            out.append(f"> _{item.snippet[:220]}_  ")
        extra = {k: v for k, v in item.extra.items() if v not in (None, [], "")}
        if "related" in extra:
            extra["related"] = ", ".join(r["publisher"] for r in extra["related"][:6])
        if extra:
            out.append(f"> `{extra}`")
    return "\n".join(out) + "\n"


def render_watches(cfg, items: list) -> str:
    if not cfg.watches:
        return ""
    matcher = WatchMatcher(cfg.watches)
    hits: dict[str, list] = {w.name: [] for w in cfg.watches}
    for item in items:
        for name in matcher.match(item):
            hits[name].append(item)
    out = ["## Watches (matched locally across all sources)"]
    for name, matched in hits.items():
        sources = sorted({i.source_id for i in matched})
        out.append(f"- **{name}**: {len(matched)} items from {len(sources)} sources ({', '.join(sources)})")
        for item in matched[:4]:
            out.append(f"  - {item.title} _({item.source_id})_")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", help="only these source ids (repeatable)")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    ensure_dirs()
    loaded = load_config(config_dir())
    if loaded.errors:
        print("Config errors:\n" + "\n".join(f"  - {e}" for e in loaded.errors), file=sys.stderr)
        return 2

    sources = loaded.sources
    if args.source:
        unknown = set(args.source) - {s.id for s in sources}
        if unknown:
            print(f"unknown source ids: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        sources = [s for s in sources if s.id in args.source]

    registry = build_registry()
    now = datetime.now(timezone.utc)
    rows, sections, all_items = [], [], []
    with make_client(loaded.config) as client:
        ctx = FetchContext(loaded.config, client)
        for source in sources:
            if source.enabled:
                print(f"fetching {source.id} ...", file=sys.stderr)
                result = run_fetch(registry[source.type], source, ctx)
            else:
                result = FetchResult(source.id, FetchStatus.NOT_IMPLEMENTED,
                                     error="disabled in config")
            dated = [i.published for i in result.items if i.published]
            oldest = _age(min(dated), now) if dated else "-"
            rows.append(f"| {_ICON.get(result.status, '❌')} | `{source.id}` | {source.category} | "
                        f"{result.status.value} | {len(result.items)} | {oldest} | {result.duration_ms} |")
            sections.append(render(source, result, args.samples, now))
            all_items.extend(result.items)

    report = "\n".join([
        f"# Spike run {now.astimezone().isoformat(timespec='seconds')}", "",
        "| | source | category | status | items | oldest | ms |", "|---|---|---|---|---|---|---|", *rows, "",
        render_watches(loaded.config, all_items), *sections,
    ])
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
    failed = [r for r in rows if "❌" in r]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
