import json
from datetime import datetime, timedelta, timezone

import pytest

from newsnest.ai import AIClient, BudgetExceeded, cost_usd, spent_today
from newsnest.config import Source
from newsnest.db import iso
from newsnest.pipeline import ingest
from newsnest.models import Item
from newsnest.profile import write_profile
from newsnest.scoring import final_score, run_scoring
from newsnest.watches import Prefilter
from tests.conftest import echo_scores, fake_client

NOW = datetime.now(timezone.utc)


def seed_items(conn, cfg, n):
    s = Source(id="delo", type="rss", category="news", name="Delo", url="https://delo.si/rss")
    items = [Item("delo", "news", f"Novica številka {i} o temi {i * 7}", f"https://delo.si/{i}", f"g{i}", NOW,
                  snippet="kratek opis") for i in range(n)]
    ingest(conn, s, items, Prefilter(cfg), NOW)


def test_no_profile_means_no_scoring(conn, cfg):
    seed_items(conn, cfg, 3)
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    assert run_scoring(conn, cfg, ai) == {"skipped": "no profile yet"}
    assert ai.client.messages.calls == []


def test_scoring_batches_and_stores(conn, cfg):
    write_profile("interests.md", "## Want\n- Ljubljana\n")
    seed_items(conn, cfg, 45)
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    out = run_scoring(conn, cfg, ai)
    assert out["scored"] == 45
    assert len(ai.client.messages.calls) == 3  # batches of 20
    call = ai.client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "temperature" not in call
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][1]["cache_control"] == {"type": "ephemeral"}
    row = conn.execute("SELECT * FROM items LIMIT 1").fetchone()
    assert row["score_status"] == "scored" and row["ai_category"] == "Ljubljana"


def test_refusal_splits_batch_and_marks_only_the_culprit(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 4)
    bad_id = conn.execute("SELECT id FROM items ORDER BY id LIMIT 1").fetchone()["id"]

    def handler(kwargs):
        if f"id={bad_id} " in kwargs["messages"][0]["content"][-1]["text"]:
            return "", "refusal"
        return echo_scores(kwargs)

    run_scoring(conn, cfg, AIClient(cfg, conn, fake_client(handler)))
    statuses = dict(conn.execute("SELECT id, score_status FROM items").fetchall())
    assert statuses.pop(bad_id) == "refused"
    assert set(statuses.values()) == {"scored"}


def test_missing_ids_are_retried_then_given_up(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 2)
    ai = AIClient(cfg, conn, fake_client(lambda kw: (json.dumps({"items": []}), "end_turn")))
    for _ in range(3):
        run_scoring(conn, cfg, ai, max_batches=1)
    assert {r[0] for r in conn.execute("SELECT score_status FROM items")} == {"error"}


def test_scoring_waits_for_a_fuller_batch(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    cfg.scoring.min_batch, cfg.scoring.max_wait_minutes = 5, 20
    seed_items(conn, cfg, 3)
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    assert "waiting" in run_scoring(conn, cfg, ai)
    assert ai.client.messages.calls == []
    # A manual refresh scores at once
    assert run_scoring(conn, cfg, ai, force=True)["scored"] == 3


def test_scoring_runs_once_the_batch_is_full(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    cfg.scoring.min_batch = 5
    seed_items(conn, cfg, 5)
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    assert run_scoring(conn, cfg, ai)["scored"] == 5
    assert len(ai.client.messages.calls) == 1


def test_scoring_runs_when_the_oldest_item_waited_long_enough(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    cfg.scoring.min_batch, cfg.scoring.max_wait_minutes = 5, 20
    seed_items(conn, cfg, 2)
    conn.execute("UPDATE items SET fetched_at = ?", (iso(NOW - timedelta(minutes=21)),))
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    assert run_scoring(conn, cfg, ai)["scored"] == 2


def test_budget_cap_stops_calls_and_keeps_items_pending(conn, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 5)
    cfg.budget.daily_usd = 0.0
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    with pytest.raises(BudgetExceeded):
        run_scoring(conn, cfg, ai)
    assert ai.client.messages.calls == []
    assert {r[0] for r in conn.execute("SELECT score_status FROM items")} == {"pending"}


def test_cost_accounting_includes_cache_rates(conn, cfg):
    from types import SimpleNamespace
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000,
                            cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_usd("claude-haiku-5-5", usage) == pytest.approx(0.10 + 0.50 + 0.125 + 0.01)
    write_profile("interests.md", "## Want\n- news\n")
    seed_items(conn, cfg, 1)
    run_scoring(conn, cfg, AIClient(cfg, conn, fake_client(echo_scores)))
    # fake usage per call: 1000 in, 500 out, 2000 cache read
    assert spent_today(conn, cfg) == pytest.approx((1000 * 0.10 + 2000 * 0.01 + 500 * 0.50) / 1e6)


def test_final_score_weights_and_cross_source_bonus(cfg):
    row = {"topic": 10, "importance": 5, "personal": 5}
    assert final_score(row, 1, cfg) == 7.0  # 0.4*10 + 0.3*5 + 0.3*5
    assert final_score(row, 2, cfg) == 8.0
    assert final_score({"topic": None}, 3, cfg) is None
