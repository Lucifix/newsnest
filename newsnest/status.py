"""Status panel data and the red banner: every way the app can be silently broken, made loud."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from newsnest.ai import local_day, spent_today
from newsnest.config import LoadedConfig
from newsnest.db import get_meta, loads, parse_iso, utcnow
from newsnest.digest import day_bounds
from newsnest.pipeline import Health, source_health


@dataclass
class SourceRow:
    id: str
    name: str
    category: str
    health: Health
    last_run: datetime | None
    last_success: datetime | None
    new_today: int
    raw_count: int | None
    http: int | None


@dataclass
class Status:
    sources: list[SourceRow]
    banner: list[str] = field(default_factory=list)
    ai: dict = field(default_factory=dict)
    last_tick: datetime | None = None
    config_errors: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def collect_status(conn: sqlite3.Connection, loaded: LoadedConfig, now: datetime | None = None) -> Status:
    now = now or utcnow()
    banner: list[str] = []
    config_errors = loaded.errors or loads(get_meta(conn, "config_errors"), []) or []
    if config_errors:
        banner.append("config.yaml has errors: nothing is being fetched until they are fixed")

    last_tick = parse_iso(get_meta(conn, "last_tick"))
    if last_tick is None or now - last_tick > timedelta(minutes=5):
        banner.append("The scheduler has not run for over 5 minutes" if last_tick else "The scheduler has not run yet")
    tick_error = get_meta(conn, "tick_error")  # cleared by the next successful tick
    if tick_error:
        banner.append(f"Last run crashed: {tick_error}")

    rows: list[SourceRow] = []
    cfg = loaded.config
    if cfg:
        tz = ZoneInfo(cfg.timezone)
        start, end = day_bounds(now.astimezone(tz).date(), tz)
        new_today = dict(conn.execute(
            "SELECT source_id, SUM(new_count) FROM fetch_runs WHERE at >= ? AND at < ? GROUP BY source_id",
            (start, end)).fetchall())
        states = {r["source_id"]: r for r in conn.execute("SELECT * FROM source_state")}
        for s in loaded.sources:
            st = states.get(s.id)
            h = source_health(s, st, cfg, now)
            rows.append(SourceRow(s.id, s.name, s.category, h, parse_iso(st["last_run"]) if st else None,
                                  parse_iso(st["last_success"]) if st else None, new_today.get(s.id, 0) or 0,
                                  st["raw_count"] if st else None, st["last_http"] if st else None))
        failing = [r for r in rows if r.health.level == "failing"]
        if failing:
            banner.append(f"{len(failing)} source(s) failing: " + ", ".join(r.name for r in failing))

        ai = loads(get_meta(conn, "ai_status"), {}) or {}
        today = local_day(cfg, now)
        usage = conn.execute(
            """SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input, COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read, COALESCE(SUM(cache_write_tokens), 0) AS cache_write,
               COALESCE(SUM(cost_usd), 0) AS cost, SUM(1 - ok) AS failed FROM ai_calls WHERE day = ?""",
            (today,)).fetchone()
        last_call = conn.execute("SELECT * FROM ai_calls ORDER BY id DESC LIMIT 1").fetchone()
        budget_hit = get_meta(conn, "ai_budget_hit") == today
        pending = conn.execute("SELECT COUNT(*) FROM items WHERE score_status = 'pending'").fetchone()[0]
        ai_view = {"status": ai, "usage": dict(usage), "spent": spent_today(conn, cfg), "cap": cfg.budget.daily_usd,
                   "budget_hit": budget_hit, "last_call": dict(last_call) if last_call else None, "pending": pending}
        if budget_hit:
            banner.append(f"Daily AI budget (${cfg.budget.daily_usd:.2f}) reached: new items are shown unscored until tomorrow")
        at = parse_iso(ai.get("at")) if ai else None
        if ai and not ai.get("ok") and at and now - at < timedelta(hours=6):
            banner.append(f"AI scoring is failing: {ai.get('error')}")
    else:
        ai_view = {}

    counts = {
        "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "stories": conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
        "rated": conn.execute("SELECT COUNT(*) FROM items WHERE rating IS NOT NULL").fetchone()[0],
    }
    return Status(sources=rows, banner=banner, ai=ai_view, last_tick=last_tick, config_errors=config_errors,
                  counts=counts)
