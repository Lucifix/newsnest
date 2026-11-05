"""The OpenAI-compatible backend, the profile override, prices and lenient JSON parsing."""

import json
import re
from types import SimpleNamespace

import pytest

from newsnest.ai import (AIClient, AIError, AIUnavailable, Refused, Truncated, Usage, check_required, cost_usd,
                         parse_json, setup_hint, spent_today)
from newsnest.config import AICfg, ProviderCfg, load_config
from newsnest.profile import write_profile
from newsnest.profile_ai import SCHEMA, draft_profile
from newsnest.scoring import run_scoring
from tests.conftest import FakeMessages, fake_openai_client
from tests.test_scoring import seed_items

OLLAMA = "http://ollama:11434/v1"


def openai_cfg(cfg, **kw):
    cfg.ai = AICfg(provider="openai", **kw)
    cfg.scoring.model = "qwen3:14b"
    return cfg


def echo_openai(kwargs, drop=None):
    """Scores every id in the user message (the OpenAI backend flattens the blocks into one string)."""
    ids = [int(i) for i in re.findall(r"id=(\d+)", kwargs["messages"][1]["content"])]
    items = [{"id": i, "topic": 7, "importance": 5, "personal": 6, "reason": "test", "category": "Local",
              "event": ""} for i in ids]
    for item in items[:1]:
        item.pop(drop, None)
    return json.dumps({"items": items}), "stop"


def test_openai_request_shape_for_a_local_server(conn, cfg):
    openai_cfg(cfg, base_url=OLLAMA, prices={"*": (0, 0)})
    write_profile("interests.md", "## Want\n- Ljubljana\n")
    seed_items(conn, cfg, 3)
    ai = AIClient(cfg, conn, fake_openai_client(echo_openai))
    assert run_scoring(conn, cfg, ai)["scored"] == 3
    call = ai.client.chat.completions.calls[0]
    assert call["model"] == "qwen3:14b"
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert all(isinstance(m["content"], str) for m in call["messages"])
    assert "cache_control" not in json.dumps(call)
    assert "<recent_ratings>" in call["messages"][1]["content"] and "<items>" in call["messages"][1]["content"]
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] and fmt["json_schema"]["name"] == "score"
    assert call["max_tokens"] == 16000 and "max_completion_tokens" not in call
    assert "reasoning_effort" not in call
    row = conn.execute("SELECT * FROM ai_calls").fetchone()
    assert row["provider"] == "ollama:11434" and row["cost_usd"] == 0
    assert row["input_tokens"] == 1000 and row["cache_read_tokens"] == 2000 and row["stop_reason"] == "stop"


def test_openai_itself_gets_max_completion_tokens_and_effort_when_asked(conn, cfg):
    openai_cfg(cfg, send_effort=True)
    ai = AIClient(cfg, conn, fake_openai_client(lambda kw: ('{"a": 1}', "stop")))
    ai.call(purpose="t", model="gpt-5-mini", system=[{"type": "text", "text": "s"}],
            messages=[{"role": "user", "content": "u"}], max_tokens=100, effort="medium")
    call = ai.client.chat.completions.calls[0]
    assert call["max_completion_tokens"] == 100 and "max_tokens" not in call
    assert call["reasoning_effort"] == "medium" and "response_format" not in call


def test_json_mode_puts_the_schema_in_the_prompt(conn, cfg):
    openai_cfg(cfg, base_url=OLLAMA, json_mode="json")
    ai = AIClient(cfg, conn, fake_openai_client(lambda kw: ('{"a": 1}', "stop")))
    ai.call(purpose="t", model="m", system=[{"type": "text", "text": "rules"}],
            messages=[{"role": "user", "content": "u"}], max_tokens=100, schema={"type": "object"})
    call = ai.client.chat.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["content"].startswith("rules") and '{"type": "object"}' in call["messages"][0]["content"]


@pytest.mark.parametrize("answer, error", [
    (("", "length"), Truncated),
    (("", "content_filter"), Refused),
    (("", "stop", "I can't help with that"), Refused),
])
def test_finish_reasons_map_to_the_same_errors_as_claude(conn, cfg, answer, error):
    openai_cfg(cfg, base_url=OLLAMA)
    ai = AIClient(cfg, conn, fake_openai_client(lambda kw: answer))
    with pytest.raises(error):
        ai.call(purpose="t", model="m", system=[], messages=[{"role": "user", "content": "u"}], max_tokens=10)
    assert conn.execute("SELECT ok FROM ai_calls").fetchone()["ok"] == 0


def test_missing_usage_is_charged_at_the_estimate(conn, cfg):
    openai_cfg(cfg, base_url=OLLAMA, prices={"m": (1.0, 2.0)})
    ai = AIClient(cfg, conn, fake_openai_client(lambda kw: ('{"a": 1}', "stop"), usage=False))
    result = ai.call(purpose="t", model="m", system=[], messages=[{"role": "user", "content": "x" * 3000}],
                     max_tokens=1000)
    assert result.cost > 0 and spent_today(conn, cfg) == pytest.approx(result.cost)


def test_prices():
    usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
    # built-in Claude price; a configured price wins; "*" covers the rest; unknown = Opus
    assert cost_usd("claude-haiku-5-5", usage) == pytest.approx(0.10 + 0.50 + 0.01)
    assert cost_usd("gpt-x", usage, provider="openai", prices={"gpt-x": (1, 4)}) == pytest.approx(1 + 4 + 0.5)
    assert cost_usd("llama", usage, provider="openai", prices={"*": (0, 0)}) == 0
    assert cost_usd("claude-haiku-5-5", usage, prices={"*": (0, 0)}) == pytest.approx(0.61)
    assert cost_usd("unknown", usage, provider="openai") == pytest.approx(4 + 20 + 2)


def test_keys(cfg, conn, monkeypatch):
    openai_cfg(cfg)
    with pytest.raises(AIUnavailable, match="OPENAI_API_KEY"):
        AIClient.create(cfg, conn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert AIClient.create(cfg, conn).backend().name == "openai"
    # a local server needs no key; the profile override on Anthropic does, but only once it is used
    monkeypatch.delenv("OPENAI_API_KEY")
    openai_cfg(cfg, base_url=OLLAMA, profile=ProviderCfg(provider="anthropic"))
    ai = AIClient.create(cfg, conn)
    with pytest.raises(AIUnavailable, match="ANTHROPIC_API_KEY"):
        ai.backend("profile")
    cfg.ai = AICfg(api_key_env="MY_CLAUDE_KEY")
    with pytest.raises(AIUnavailable, match="MY_CLAUDE_KEY"):
        AIClient.create(cfg, conn)
    # a hosted API behind base_url with a named key: missing means missing, not a dummy key and a 401
    openai_cfg(cfg, base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY")
    with pytest.raises(AIUnavailable, match="OPENROUTER_API_KEY is not set"):
        AIClient.create(cfg, conn)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert AIClient.create(cfg, conn).backend().client.api_key == "sk-or-test"


def test_setup_hint_names_the_key_or_the_local_server(cfg):
    assert "ANTHROPIC_API_KEY is set" in setup_hint(cfg)
    openai_cfg(cfg)
    assert "OPENAI_API_KEY is set" in setup_hint(cfg)
    openai_cfg(cfg, base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY")
    assert "OPENROUTER_API_KEY is set" in setup_hint(cfg)
    openai_cfg(cfg, base_url=OLLAMA)
    assert "the AI server at ollama:11434 is reachable" in setup_hint(cfg)


def test_profile_jobs_use_the_profile_provider(conn, cfg):
    openai_cfg(cfg, base_url=OLLAMA, profile=ProviderCfg(provider="anthropic"))
    draft = json.dumps({"interests_md": "## Want\n- x\n", "rationale": "r", "changed": True})
    client = fake_openai_client(echo_openai)
    client.messages = FakeMessages(lambda kw: (draft, "end_turn"))
    ai = AIClient(cfg, conn, client)
    draft_profile(conn, cfg, ai)
    assert len(client.messages.calls) == 1 and client.chat.completions.calls == []
    assert client.messages.calls[0]["model"] == cfg.scoring.profile_model
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 2)
    run_scoring(conn, cfg, ai)
    assert len(client.chat.completions.calls) == 1
    assert [r[0] for r in conn.execute("SELECT provider FROM ai_calls ORDER BY id")] == ["Anthropic", "ollama:11434"]


def test_a_profile_answer_with_a_missing_key_is_an_ai_error(conn, cfg):
    ai = AIClient(cfg, conn, SimpleNamespace(messages=FakeMessages(lambda kw: ('{"rationale": "r"}', "end_turn"))))
    with pytest.raises(AIError, match="interests_md is missing"):
        draft_profile(conn, cfg, ai)


def test_scoring_skips_entries_with_missing_keys(conn, cfg):
    openai_cfg(cfg, base_url=OLLAMA)
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 3)
    ai = AIClient(cfg, conn, fake_openai_client(lambda kw: echo_openai(kw, drop="topic")))
    assert run_scoring(conn, cfg, ai)["scored"] == 2
    # the malformed one counts as missing from the answer: retried a few times, then given up, no crash
    statuses = sorted(r[0] for r in conn.execute("SELECT score_status FROM items"))
    assert statuses == ["error", "scored", "scored"]
    assert len(ai.client.chat.completions.calls) > 1


def test_loose_answers_do_not_crash_summaries_or_labels():
    from newsnest.events import normalize_label
    from newsnest.summaries import clean_points
    assert clean_points("- one point") == ["one point"] and clean_points(None) == []
    assert normalize_label(7) is None and normalize_label("PHP2Shell flaw") == "php2shell-flaw"


def test_parse_json_tolerates_think_blocks_fences_and_prose():
    assert parse_json('<think>hmm {"no": 1}</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    # only the closing tag (the template opened it), with braces in the thinking
    assert parse_json('maybe {"no": 1} or {\n</think>\n{"a": 1}') == {"a": 1}
    assert parse_json('Here you go: {"a": {"b": 2}} Hope this helps') == {"a": {"b": 2}}
    with pytest.raises(AIError):
        parse_json("no json here")
    with pytest.raises(AIError):
        parse_json("[1, 2]")


def test_check_required():
    check_required({"interests_md": "x", "rationale": "y", "changed": False}, SCHEMA)
    with pytest.raises(AIError, match="changed should be boolean"):
        check_required({"interests_md": "x", "rationale": "y", "changed": "no"}, SCHEMA)
    schema = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}}}
    check_required({"items": [{"id": 1}, {"id": 2.0}]}, schema)
    with pytest.raises(AIError, match=r"items\[1\]\.id is missing"):
        check_required({"items": [{"id": 1}, {}]}, schema)


def test_config_defaults_to_claude_and_checks_the_ai_section(env):
    loaded = load_config(env.config)
    assert not loaded.errors and loaded.config.ai.provider == "anthropic"
    assert loaded.config.ai.key_env == "ANTHROPIC_API_KEY" and loaded.config.ai.for_tier("profile") is loaded.config.ai
    path = env.config / "config.yaml"
    base = path.read_text()
    for section, ok in [
        ("ai:\n  provider: openai\n  base_url: http://ollama:11434/v1\n  prices:\n    '*': [0, 0]\n"
         "  profile:\n    provider: anthropic\n", True),
        ("ai:\n  provider: openai\n", True),
        ("ai:\n  provider: gemini\n", False),
        ("ai:\n  base_url: http://ollama:11434/v1\n", False),        # base_url needs provider: openai
        ("ai:\n  provider: openai\n  base_url: ollama:11434\n", False),
        ("ai:\n  provider: openai\n  prices:\n    m: [-1, 0]\n", False),
        ("ai:\n  provider: openai\n  typo: 1\n", False),
    ]:
        path.write_text(base + "\n" + section)
        loaded = load_config(env.config)
        assert (not loaded.errors) == ok, (section, loaded.errors)
    path.write_text(base + "\nai:\n  provider: openai\n  base_url: http://ollama:11434/v1\n"
                           "  profile:\n    provider: anthropic\n")
    ai = load_config(env.config).config.ai
    assert ai.label == "ollama:11434" and ai.for_tier("profile").label == "Anthropic"
