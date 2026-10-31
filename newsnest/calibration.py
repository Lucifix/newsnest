"""Calibration data: how final scores line up with my 👍/👎, to help pick thresholds and weights."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from newsnest.config import Config
from newsnest.db import iso, parse_iso, utcnow
from newsnest.scoring import final_score

BINS = list(range(0, 12))  # final runs 0-10 plus the cross-source bonus


def subscore_labels(cfg: Config) -> tuple[tuple[str, str], ...]:
    country = cfg.region.country
    return (("topic", "Fits your interests"), ("importance", f"Big news for {country}" if country else "Big news in general"),
            ("personal", "You'd want to read it"))


@dataclass
class ThresholdRow:
    threshold: float
    kept_up: float | None      # share of 👍 items at or above the threshold
    hidden_down: float | None  # share of 👎 items below it
    per_day: float             # items per day that would be shown
    current: bool


def calibration_data(conn: sqlite3.Connection, cfg: Config, days: int = 30) -> dict:
    tz = ZoneInfo(cfg.timezone)
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.score_status = 'scored' AND i.fetched_at >= ?""",
        (iso(utcnow() - timedelta(days=days)),)).fetchall()
    scored = [(r, final_score(r, r["source_count"], cfg)) for r in rows]
    up = [f for r, f in scored if r["rating"] == 1]
    down = [f for r, f in scored if r["rating"] == -1]
    hist = {b: {"up": 0, "down": 0, "unrated": 0} for b in BINS}
    for r, f in scored:
        b = min(int(f), BINS[-1])
        hist[b]["up" if r["rating"] == 1 else "down" if r["rating"] == -1 else "unrated"] += 1

    active_days = max(1, len({parse_iso(r["fetched_at"]).astimezone(tz).date() for r, _ in scored}))
    table = []
    for t in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]:
        table.append(ThresholdRow(
            threshold=t,
            kept_up=sum(f >= t for f in up) / len(up) if up else None,
            hidden_down=sum(f < t for f in down) / len(down) if down else None,
            per_day=sum(f >= t for _, f in scored) / active_days,
            current=t == cfg.digest.min_final))
    suggested = None
    if len(up) >= 5 and len(down) >= 5:
        suggested = max(table, key=lambda row: row.kept_up + row.hidden_down).threshold

    def means(rating: int) -> dict | None:
        sel = [r for r, _ in scored if r["rating"] == rating]
        if not sel:
            return None
        return {k: round(sum(r[k] for r in sel) / len(sel), 1) for k in ("topic", "importance", "personal")}

    # Which sub-score tells 👍 from 👎 best: the one with the biggest gap deserves the most weight
    up_m, down_m = means(1), means(-1)
    subscores = []
    if up_m and down_m:
        for key, label in subscore_labels(cfg):
            subscores.append({"key": key, "label": label, "up": up_m[key], "down": down_m[key],
                              "gap": round(up_m[key] - down_m[key], 1), "weight": getattr(cfg.scoring.weights, key)})
        best = max(subscores, key=lambda s: s["gap"])
        best["best"] = True

    peak = max([max(h["up"], h["down"]) for h in hist.values()] + [1])
    return {"subscores": subscores, "hist": hist, "peak": peak, "table": table, "suggested": suggested, "n_scored": len(scored),
            "n_up": len(up), "n_down": len(down), "means_up": means(1), "means_down": means(-1),
            "active_days": active_days, "chart": histogram_chart(hist, peak),
            "chart_narrow": histogram_chart(hist, peak, width=360, height=240)}


def _nice_ticks(peak: int) -> list[int]:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if peak / step <= 4:
            return list(range(0, peak + step, step))
    return [0, peak]


def histogram_chart(hist: dict, peak: int, width: int = 640, height: int = 220) -> dict:
    """Geometry for a grouped histogram of final scores, 👍 vs 👎 per bin (SVG user units). Phones get a
    narrower one drawn at about their real width, so the labels stay readable instead of shrinking."""
    left, right, top, bottom = 34, 6, 10, 26
    ticks = _nice_ticks(peak)
    ymax = ticks[-1] or 1
    plot_h = height - top - bottom
    base = top + plot_h
    slot = (width - left - right) / len(BINS)
    pad = 6 if slot > 40 else 3
    bar_w = (slot - 2 * pad - 2) / 2
    bars = []
    for i, b in enumerate(BINS):
        for j, series in enumerate(("up", "down")):
            count = hist[b][series]
            if not count:
                continue
            h = plot_h * count / ymax
            x = left + i * slot + pad + j * (bar_w + 2)  # 2px gap between the pair
            y = base - h
            r = min(4, h / 2, bar_w / 2)
            path = (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                    f"H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base:.1f} Z")
            bars.append({"path": path, "series": series, "bin": b, "count": count,
                         "hit_x": left + i * slot, "hit_w": slot})
    grid = [{"y": base - plot_h * t / ymax, "label": t} for t in ticks]
    xlabels = [{"x": left + i * slot + slot / 2, "label": b} for i, b in enumerate(BINS)]
    return {"width": width, "height": height, "bars": bars, "grid": grid, "xlabels": xlabels,
            "base": base, "left": left, "right": width - right}
