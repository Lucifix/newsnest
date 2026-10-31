"""Claude API access with a hard daily spend cap and per-call usage accounting.

Every call (scoring, summaries, "Missed this", profile draft, weekly proposal) goes
through AIClient.call(), which checks the cap first and records tokens and cost afterwards.
Failures are recorded too, so the status panel shows them instead of items silently staying unscored.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from newsnest.config import Config
from newsnest.db import dumps, iso, set_meta, utcnow

log = logging.getLogger(__name__)

# USD per million tokens (input, output), prompts up to 100K tokens. Cache writes cost 1.25x input,
# cache reads 0.1x input. Unknown models are priced like Opus so the cap errs on the safe side.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
_FALLBACK_PRICE = PRICES["claude-opus-5-5"]


class AIError(Exception):
    """Any AI failure that should leave items unscored and show up on the status panel."""


class AIUnavailable(AIError):
    pass


class BudgetExceeded(AIError):
    pass


class Refused(AIError):
    pass


class Truncated(AIError):
    pass


def cost_usd(model: str, usage) -> float:
    p_in, p_out = PRICES.get(model, _FALLBACK_PRICE)
    uncached = usage.input_tokens or 0
    write = usage.cache_creation_input_tokens or 0
    read = usage.cache_read_input_tokens or 0
    return (uncached * p_in + write * p_in * 1.25 + read * p_in * 0.1 + (usage.output_tokens or 0) * p_out) / 1e6


def local_day(cfg: Config, now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(cfg.timezone)).date().isoformat()


def spent_today(conn: sqlite3.Connection, cfg: Config) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_calls WHERE day = ?",
                       (local_day(cfg),)).fetchone()
    return float(row["s"])


@dataclass
class CallResult:
    text: str
    stop_reason: str | None
    cost: float


class AIClient:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, client=None):
        self.cfg = cfg
        self.conn = conn
        self.client = client

    @classmethod
    def create(cls, cfg: Config, conn: sqlite3.Connection) -> "AIClient":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise AIUnavailable("ANTHROPIC_API_KEY is not set in the container environment")
        return cls(cfg, conn, anthropic.Anthropic(max_retries=2, timeout=120))

    def remaining_budget(self) -> float:
        return self.cfg.budget.daily_usd - spent_today(self.conn, self.cfg)

    def call(self, *, purpose: str, model: str, system: list[dict], messages: list[dict],
             max_tokens: int, effort: str = "low", schema: dict | None = None) -> CallResult:
        p_in, p_out = PRICES.get(model, _FALLBACK_PRICE)
        prompt_chars = len(json.dumps(system, ensure_ascii=False)) + len(json.dumps(messages, ensure_ascii=False))
        # Rough worst case for this call: ~3 chars per token in, a typical output ceiling out
        estimate = (prompt_chars / 3 * p_in + min(max_tokens, 4000) * p_out) / 1e6
        remaining = self.remaining_budget()
        if remaining < estimate:
            msg = f"daily AI budget of ${self.cfg.budget.daily_usd:.2f} reached"
            set_meta(self.conn, "ai_budget_hit", local_day(self.cfg))
            raise BudgetExceeded(msg)

        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            response = self.client.messages.create(
                model=model, max_tokens=max_tokens, system=system, messages=messages,
                output_config=output_config)
        except anthropic.APIStatusError as e:
            self._record(purpose, model, None, ok=False, error=f"HTTP {e.status_code}: {e.message}")
            raise AIError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            self._record(purpose, model, None, ok=False, error=f"connection error: {e}")
            raise AIError(f"cannot reach the Claude API: {e}") from e

        cost = self._record(purpose, model, response, ok=response.stop_reason not in ("refusal", "max_tokens"))
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise Refused(f"request declined by safety classifier ({category})")
        if response.stop_reason == "max_tokens":
            raise Truncated("response hit max_tokens")
        text = "".join(b.text for b in response.content if b.type == "text")
        return CallResult(text, response.stop_reason, cost)

    def _record(self, purpose: str, model: str, response, ok: bool, error: str | None = None) -> float:
        now = utcnow()
        usage = response.usage if response is not None else None
        cost = cost_usd(model, usage) if usage else 0.0
        self.conn.execute(
            """INSERT INTO ai_calls (at, day, purpose, model, input_tokens, output_tokens, cache_write_tokens,
               cache_read_tokens, cost_usd, ok, stop_reason, error, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (iso(now), local_day(self.cfg, now), purpose, model,
             usage.input_tokens if usage else None, usage.output_tokens if usage else None,
             usage.cache_creation_input_tokens if usage else None, usage.cache_read_input_tokens if usage else None,
             cost, int(ok), response.stop_reason if response is not None else None, error,
             getattr(response, "_request_id", None) if response is not None else None))
        if ok:
            set_meta(self.conn, "ai_status", dumps({"ok": True, "at": iso(now)}))
        else:
            set_meta(self.conn, "ai_status", dumps({"ok": False, "at": iso(now),
                                                    "error": error or f"stop_reason={response.stop_reason}"}))
        return cost


def record_ai_failure(conn: sqlite3.Connection, error: str) -> None:
    set_meta(conn, "ai_status", dumps({"ok": False, "at": iso(utcnow()), "error": error}))


def parse_json(text: str) -> dict:
    """output_config.format guarantees JSON, but guard anyway: a parse failure is an AIError."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError(f"invalid JSON from model: {e}") from e
