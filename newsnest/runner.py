"""The work loop: a 60 s tick re-reads config, fetches due sources, then runs the AI steps.

Schedules are evaluated on every tick, so interval changes in config.yaml apply without a restart.

    python -m newsnest.runner              # one tick now (dev; never leaves a scheduler running)
    python -m newsnest.runner --force      # fetch every source regardless of its schedule
    python -m newsnest.runner --no-ai      # fetch and store only
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from newsnest.bootstrap import config_dir, ensure_dirs
from newsnest.config import Config, LoadedConfig, Source, load_config
from newsnest.discovery import HOST_SPACING, discovery_sources, retire_candidates
from newsnest.drift import refresh_drift
from newsnest.db import dumps, get_meta, iso, migrate, parse_iso, session, set_meta, transaction, utcnow
from newsnest.grouping import GROUPING_VERSION, regroup
from newsnest import icons
from newsnest.fetchers import build_registry
from newsnest.fetchers.base import FetchContext, make_client, run_fetch
from newsnest.notify import get_notifier
from newsnest.pipeline import ingest, record_run
from newsnest.watches import Prefilter

log = logging.getLogger(__name__)
_tick_lock = threading.Lock()


def is_due(source: Source, state: sqlite3.Row | None, cfg: Config, now: datetime) -> bool:
    if state is None or not state["last_run"]:
        return True
    blocked = parse_iso(state["blocked_until"])
    if blocked and blocked > now:
        return False
    minutes = source.params.get("interval") or cfg.poll_minutes(source.category)
    # 30 s slack so a 15 min schedule on a 60 s tick does not drift to 16 min
    return now - parse_iso(state["last_run"]) >= timedelta(minutes=minutes, seconds=-30)


def fetch_sources(conn: sqlite3.Connection, loaded: LoadedConfig, force: bool = False) -> dict:
    cfg = loaded.config
    now = utcnow()
    states = {r["source_id"]: r for r in conn.execute("SELECT * FROM source_state")}
    all_sources = loaded.sources + discovery_sources(conn, cfg)
    feeds = [s for s in all_sources if s.enabled
             and (force or is_due(s, states.get(s.id), cfg, now))]
    have_meta = icons.known_sources(conn)
    for s in feeds:  # conditional requests: unchanged feeds answer 304 with no body
        st = states.get(s.id)
        # ...except once for a feed whose site and logo aren't known yet (installs from before source icons)
        if st is not None and (s.id in have_meta or not icons.wants_icon(s)):
            s.params.update(etag=st["etag"], last_modified=st["last_modified"], prev_raw=st["raw_count"])

    registry = build_registry()
    prefilter = Prefilter(cfg)
    summary = {"fetched": 0, "new": 0, "failed": []}
    last_hit: dict[str, float] = {}
    with make_client(cfg) as client:
        ctx = FetchContext(cfg, client)
        for source in sorted(feeds, key=lambda s: urlsplit(s.url).hostname or ""):
            host = urlsplit(source.url).hostname or ""
            wait = last_hit.get(host, 0) + HOST_SPACING - time.monotonic()
            if wait > 0:  # several feeds on one host (NYT, Guardian, Reddit): don't hammer it
                time.sleep(wait)
            summary["fetched"] += 1
            _fetch_one(conn, source, registry, ctx, prefilter, cfg, summary)
            last_hit[host] = time.monotonic()
        if looked := icons.refresh(conn, all_sources, client, utcnow()):
            summary["icons"] = looked
    return summary


def _fetch_one(conn, source, registry, ctx, prefilter, cfg, summary) -> None:
    result = run_fetch(registry[source.type], source, ctx)
    new_ids: list[int] = []
    if result.items:
        new_ids = ingest(conn, source, result.items, prefilter, utcnow())
    record_run(conn, source, result, len(new_ids), cfg, utcnow())
    if result.feed_meta is not None and icons.wants_icon(source):
        icons.note_feed(conn, source, result.feed_meta)
    summary["new"] += len(new_ids)
    if result.status.is_failure:
        summary["failed"].append(f"{source.id}: {result.error}")
        log.warning("fetch failed", extra={"source": source.id, "status": result.status.value,
                                           "error": result.error})


def fetch_now(source_id: str) -> dict:
    """"Check now" on a feed's page: fetch that one feed right away, as the work loop would. Its new stories are
    scored on the next tick. Answers {"new": n} or {"error": …}; never waits for a tick that is running."""
    if not _tick_lock.acquire(blocking=False):
        return {"error": "the app is fetching right now; try again in a minute"}
    try:
        loaded = load_config(config_dir())
        source = next((s for s in loaded.sources if s.id == source_id), None)
        if not loaded.config or source is None:
            return {"error": "no such feed"}
        cfg = loaded.config
        with session() as conn:
            st = conn.execute("SELECT * FROM source_state WHERE source_id = ?", (source_id,)).fetchone()
            blocked = parse_iso(st["blocked_until"]) if st else None
            if blocked and blocked > utcnow():   # the site asked us to wait: asking again only makes it longer
                return {"error": f"the site asked to wait until {blocked:%H:%M} UTC"}
            if st is not None and (source_id in icons.known_sources(conn) or not icons.wants_icon(source)):
                source.params.update(etag=st["etag"], last_modified=st["last_modified"], prev_raw=st["raw_count"])
            summary = {"fetched": 1, "new": 0, "failed": []}
            with make_client(cfg) as client:
                _fetch_one(conn, source, build_registry(), FetchContext(cfg, client), Prefilter(cfg), cfg, summary)
            if summary["failed"]:
                return {"error": summary["failed"][0].split(": ", 1)[-1]}
            return {"new": summary["new"]}
    except Exception as e:   # a page action: say what went wrong instead of a server error
        log.exception("fetch now failed", extra={"source": source_id})
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        _tick_lock.release()


def tick(force: bool = False, ai: bool = True) -> dict:
    """One pass of the work loop. Never raises; errors land in the status panel."""
    if not _tick_lock.acquire(blocking=False):
        return {"skipped": "previous tick still running"}
    try:
        loaded = load_config(config_dir())
        with session() as conn:
            migrate(conn)
            if get_meta(conn, "regroup") != GROUPING_VERSION:  # matcher changed: regroup the recent past once
                with transaction(conn):
                    log.info("regrouped stories", extra=regroup(conn, 72, utcnow()))
                    set_meta(conn, "regroup", GROUPING_VERSION)
            set_meta(conn, "last_tick", iso(utcnow()))
            set_meta(conn, "config_errors", dumps(loaded.errors) if loaded.errors else None)
            if loaded.errors:
                log.error("config invalid", extra={"errors": loaded.errors})
                return {"config_errors": loaded.errors}
            summary = fetch_sources(conn, loaded, force=force)
            try:  # what you've read lately, worked out ahead so rankings don't wait for it (newsnest.drift)
                if refresh_drift(conn, loaded.config):
                    summary["drift"] = "refreshed"
            except Exception:
                log.exception("drift refresh failed")
            today = datetime.now().date().isoformat()
            if get_meta(conn, "discovery_retire_day") != today:
                set_meta(conn, "discovery_retire_day", today)
                summary["retired"] = retire_candidates(conn, loaded.config)
            if ai:
                from newsnest.ai_steps import run_ai_steps
                summary["ai"] = run_ai_steps(conn, loaded.config, get_notifier(), force=force)
            set_meta(conn, "tick_error", None)
            log.info("tick done", extra=summary)
            return summary
    except Exception as e:  # the scheduler must survive anything; the failure is recorded for the banner
        log.exception("tick crashed")
        try:
            with session() as conn:
                set_meta(conn, "tick_error", f"{utcnow():%Y-%m-%d %H:%M} UTC: {type(e).__name__}: {e}")
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        _tick_lock.release()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="fetch every source now")
    ap.add_argument("--no-ai", action="store_true", help="skip scoring and summaries")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_dirs()
    print(tick(force=args.force, ai=not args.no_ai))


if __name__ == "__main__":
    main()
