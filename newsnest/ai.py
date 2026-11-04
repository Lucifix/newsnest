"""AI access with a hard daily spend cap and per-call usage accounting.

Every call (scoring, summaries, "Missed this", profile draft, weekly proposal) goes
through AIClient.call(), which checks the cap first and records tokens and cost afterwards.
Failures are recorded too, so the status panel shows them instead of items silently staying unscored.

Two backends: Claude through the Anthropic API, and any OpenAI-compatible API (OpenAI, OpenRouter,
Ollama, LM Studio, vLLM...). Callers write Anthropic-style system blocks and messages; the OpenAI backend
flattens them. The profile jobs (tier="profile") may use a different provider than scoring (ai.profile).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import openai

from newsnest.config import Config, ProviderCfg
from newsnest.db import dumps, iso, set_meta, utcnow

log = logging.getLogger(__name__)

# USD per million tokens (input, output), prompts up to 100K tokens. Cache writes cost 1.25x input,
# cache reads 0.1x input. Unknown models are priced like Opus so the cap errs on the safe side;
# ai.prices in config.yaml prices other models (or {"*": [0, 0]} for local ones).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
_FALLBACK_PRICE = PRICES["claude-opus-5-5"]
# (cache write, cache read) as multiples of the input price. OpenAI-compatible APIs have no cache writes and
# discount cached input by 50-90% depending on the model: 0.5 keeps the cap on the safe side.
_CACHE_RATES = {"anthropic": (1.25, 0.1), "openai": (1.25, 0.5)}


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


@dataclass
class Usage:
    input: int = 0          # uncached input tokens
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass
class Reply:
    """One model answer, whatever the backend."""
    text: str
    stop: str               # "end" | "refusal" | "max_tokens"
    raw_stop: str | None    # the API's own stop/finish reason, stored with the call
    usage: Usage | None     # None: the server reported no usage
    refusal: str | None = None
    request_id: str | None = None


def model_price(model: str, prices: dict | None = None) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None when the model's price is unknown."""
    prices = prices or {}
    if model in prices:
        return tuple(prices[model])
    if model in PRICES:
        return PRICES[model]
    if "*" in prices:
        return tuple(prices["*"])
    return None


def cost_usd(model: str, usage: Usage, *, provider: str = "anthropic", prices: dict | None = None) -> float:
    p_in, p_out = model_price(model, prices) or _FALLBACK_PRICE
    write_rate, read_rate = _CACHE_RATES[provider]
    return (usage.input * p_in + usage.cache_write * p_in * write_rate + usage.cache_read * p_in * read_rate
            + usage.output * p_out) / 1e6


def local_day(cfg: Config, now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(cfg.timezone)).date().isoformat()


def spent_today(conn: sqlite3.Connection, cfg: Config) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_calls WHERE day = ?",
                       (local_day(cfg),)).fetchone()
    return float(row["s"])


def ai_setup(cfg: Config) -> list[dict]:
    """Which provider and model run each kind of job, for the status and settings pages."""
    tiers = (("main", "Scoring", cfg.scoring.model), ("profile", "Profile", cfg.scoring.profile_model))
    return [{"tier": tier, "label": label, "provider": cfg.ai.for_tier(tier).label, "model": model,
             "priced": model_price(model, cfg.ai.prices) is not None} for tier, label, model in tiers]


def setup_hint(cfg: Config) -> str:
    """What has to be in place before the first AI call."""
    p = cfg.ai
    if p.provider == "openai" and p.base_url:
        need = f"the AI server at {p.label} is reachable"
    else:
        need = f"{p.key_env} is set"
    return f"Scoring starts once a profile exists and {need}."


def _missing_key(p: ProviderCfg) -> AIUnavailable:
    return AIUnavailable(f"{p.key_env} is not set in the container environment")


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, client, p: ProviderCfg):
        self.client = client
        self.p = p

    @classmethod
    def connect(cls, p: ProviderCfg) -> "AnthropicBackend":
        key = os.environ.get(p.key_env)
        if not key:
            raise _missing_key(p)
        return cls(anthropic.Anthropic(api_key=key, max_retries=2, timeout=p.timeout_seconds), p)

    def send(self, *, purpose: str, model: str, system: list[dict], messages: list[dict], max_tokens: int,
             effort: str, schema: dict | None) -> Reply:
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            r = self.client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                            output_config=output_config)
        except anthropic.APIStatusError as e:
            raise AIError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AIError(f"cannot reach the Claude API: {e}") from e
        stop = {"refusal": "refusal", "max_tokens": "max_tokens"}.get(r.stop_reason, "end")
        refusal = None
        if stop == "refusal":
            category = getattr(r.stop_details, "category", None) if r.stop_details else None
            refusal = f"request declined by safety classifier ({category})"
        u = r.usage
        usage = Usage(u.input_tokens or 0, u.output_tokens or 0, u.cache_creation_input_tokens or 0,
                      u.cache_read_input_tokens or 0) if u is not None else None
        return Reply("".join(b.text for b in r.content if b.type == "text"), stop, r.stop_reason, usage,
                     refusal, getattr(r, "_request_id", None))


def _text_of(content) -> str:
    """Anthropic-style content (a string or a list of text blocks) as one string."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text")


class OpenAIBackend:
    """Chat Completions on any OpenAI-compatible server. Caching is automatic there (the static prompt
    prefix comes first), so cache_control markers are simply dropped."""
    name = "openai"

    def __init__(self, client, p: ProviderCfg):
        self.client = client
        self.p = p

    @classmethod
    def connect(cls, p: ProviderCfg) -> "OpenAIBackend":
        key = os.environ.get(p.key_env, "")
        if not key and not p.base_url:
            raise _missing_key(p)
        # Local servers (Ollama, LM Studio) need no key, but the SDK insists on one
        return cls(openai.OpenAI(api_key=key or "not-needed", base_url=p.base_url or None, max_retries=2,
                                 timeout=p.timeout_seconds), p)

    def send(self, *, purpose: str, model: str, system: list[dict], messages: list[dict], max_tokens: int,
             effort: str, schema: dict | None) -> Reply:
        system_text = _text_of(system)
        kwargs: dict = {"model": model}
        if schema and self.p.json_mode == "json":
            system_text += ("\n\nReply with only a JSON object (no prose, no code fence) matching this JSON "
                            f"schema:\n{json.dumps(schema, ensure_ascii=False)}")
            kwargs["response_format"] = {"type": "json_object"}
        elif schema:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": purpose, "strict": True, "schema": schema}}
        kwargs["messages"] = [{"role": "system", "content": system_text}] + [
            {"role": m["role"], "content": _text_of(m["content"])} for m in messages]
        # OpenAI itself wants max_completion_tokens (reasoning models reject max_tokens); other servers
        # know max_tokens better
        kwargs["max_tokens" if self.p.base_url else "max_completion_tokens"] = max_tokens
        if self.p.send_effort:
            kwargs["reasoning_effort"] = effort
        try:
            r = self.client.chat.completions.create(**kwargs)
        except openai.APIStatusError as e:
            raise AIError(f"{self.p.label} API error {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise AIError(f"cannot reach the AI at {self.p.label}: {e}") from e
        if not r.choices:
            raise AIError(f"{self.p.label} returned no answer")
        choice = r.choices[0]
        refusal = getattr(choice.message, "refusal", None)
        if refusal or choice.finish_reason == "content_filter":
            stop = "refusal"
            refusal = f"request declined by the model: {str(refusal or 'content filter')[:200]}"
        else:
            stop = "max_tokens" if choice.finish_reason == "length" else "end"
        usage = None
        if (u := r.usage) is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None) or 0
            usage = Usage(input=max(0, (u.prompt_tokens or 0) - cached), output=u.completion_tokens or 0,
                          cache_read=cached)
        return Reply(choice.message.content or "", stop, choice.finish_reason, usage, refusal,
                     getattr(r, "_request_id", None))


_BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend}


@dataclass
class CallResult:
    text: str
    stop_reason: str | None
    cost: float


class AIClient:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, client=None):
        """`client` (tests) stands in for the SDK client of every tier."""
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self._backends: dict[str, AnthropicBackend | OpenAIBackend] = {}

    @classmethod
    def create(cls, cfg: Config, conn: sqlite3.Connection) -> "AIClient":
        ai = cls(cfg, conn)
        ai.backend("main")  # a missing key fails here, before any work is collected for the AI
        return ai

    def backend(self, tier: str = "main") -> AnthropicBackend | OpenAIBackend:
        if tier not in self._backends:
            p = self.cfg.ai.for_tier(tier)
            kind = _BACKENDS[p.provider]
            self._backends[tier] = kind(self.client, p) if self.client is not None else kind.connect(p)
        return self._backends[tier]

    def remaining_budget(self) -> float:
        return self.cfg.budget.daily_usd - spent_today(self.conn, self.cfg)

    def call(self, *, purpose: str, model: str, system: list[dict], messages: list[dict],
             max_tokens: int, effort: str = "low", schema: dict | None = None, tier: str = "main") -> CallResult:
        backend = self.backend(tier)
        p_in, p_out = model_price(model, self.cfg.ai.prices) or _FALLBACK_PRICE
        prompt_chars = len(json.dumps(system, ensure_ascii=False)) + len(json.dumps(messages, ensure_ascii=False))
        # Rough worst case for this call: ~3 chars per token in, a typical output ceiling out
        estimate = (prompt_chars / 3 * p_in + min(max_tokens, 4000) * p_out) / 1e6
        remaining = self.remaining_budget()
        if remaining < estimate:
            msg = f"daily AI budget of ${self.cfg.budget.daily_usd:.2f} reached"
            set_meta(self.conn, "ai_budget_hit", local_day(self.cfg))
            raise BudgetExceeded(msg)

        try:
            reply = backend.send(purpose=purpose, model=model, system=system, messages=messages,
                                 max_tokens=max_tokens, effort=effort, schema=schema)
        except AIError as e:
            self._record(purpose, model, backend, None, ok=False, error=str(e))
            raise

        cost = self._record(purpose, model, backend, reply, ok=reply.stop == "end", estimate=estimate)
        if reply.stop == "refusal":
            raise Refused(reply.refusal or "request declined")
        if reply.stop == "max_tokens":
            raise Truncated("response hit max_tokens")
        return CallResult(reply.text, reply.raw_stop, cost)

    def _record(self, purpose: str, model: str, backend: AnthropicBackend | OpenAIBackend, reply: Reply | None,
                ok: bool, error: str | None = None, estimate: float = 0.0) -> float:
        now = utcnow()
        usage = reply.usage if reply is not None else None
        if usage is not None:
            cost = cost_usd(model, usage, provider=backend.name, prices=self.cfg.ai.prices)
        else:
            # Answered without reporting usage (some local servers): charge the estimate so the cap still holds
            cost = estimate if reply is not None else 0.0
        self.conn.execute(
            """INSERT INTO ai_calls (at, day, purpose, model, provider, input_tokens, output_tokens,
               cache_write_tokens, cache_read_tokens, cost_usd, ok, stop_reason, error, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (iso(now), local_day(self.cfg, now), purpose, model, backend.p.label,
             usage.input if usage else None, usage.output if usage else None,
             usage.cache_write if usage else None, usage.cache_read if usage else None,
             cost, int(ok), reply.raw_stop if reply is not None else None, error,
             reply.request_id if reply is not None else None))
        if ok:
            set_meta(self.conn, "ai_status", dumps({"ok": True, "at": iso(now)}))
        else:
            set_meta(self.conn, "ai_status", dumps({"ok": False, "at": iso(now),
                                                    "error": error or f"stop_reason={reply.raw_stop}"}))
        return cost


def record_ai_failure(conn: sqlite3.Connection, error: str) -> None:
    set_meta(conn, "ai_status", dumps({"ok": False, "at": iso(utcnow()), "error": error}))


_THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_json(text: str, schema: dict | None = None) -> dict:
    """The answer as a JSON object; a parse failure is an AIError. Structured output guarantees JSON with
    Claude, but other models may wrap it in <think> blocks, code fences or prose, so those are stripped.
    With `schema`, required keys and basic types are checked as well."""
    cleaned = _THINK.sub("", text).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise AIError(f"invalid JSON from model: {e}") from e
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as e2:
            raise AIError(f"invalid JSON from model: {e2}") from e2
    if not isinstance(data, dict):
        raise AIError("invalid JSON from model: not an object")
    if schema:
        check_required(data, schema)
    return data


_TYPES = {"string": str, "integer": (int, float), "number": (int, float), "boolean": bool, "array": list,
          "object": dict}


def check_required(value, schema: dict, path: str = "answer") -> None:
    """Required keys present and of the right basic type, recursively. Strict structured output makes this a
    no-op; models without it sometimes drop or rename keys."""
    kind = schema.get("type")
    if kind in _TYPES:
        if not isinstance(value, _TYPES[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
            raise AIError(f"invalid answer from model: {path} should be {kind}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise AIError(f"invalid answer from model: {path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                check_required(value[key], sub, f"{path}.{key}")
    elif kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            check_required(item, schema["items"], f"{path}[{i}]")
