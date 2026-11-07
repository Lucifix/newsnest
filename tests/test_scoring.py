import json
from datetime import datetime, timedelta, timezone

import pytest

from newsnest.ai import AIClient, BudgetExceeded, Usage, cost_usd, spent_today
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
    assert row["score_status"] == "scored" and row["ai_category"] == "Local"


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
    usage = Usage(input=1_000_000, output=1_000_000, cache_write=1_000_000, cache_read=1_000_000)
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


def test_prompts_follow_the_region(cfg):
    from newsnest.calibration import subscore_labels
    from newsnest.config import RegionCfg
    from newsnest.profile_ai import draft_system
    from newsnest.scoring import instructions

    cfg.region = RegionCfg()
    cfg.scoring.categories = ["Local", "National", "World", "Other"]
    for text in (instructions(cfg), draft_system(cfg)):
        assert "Slovenia" not in text and "Ljubljana" not in text and "one reader" in text
    assert "in general" in instructions(cfg)
    assert dict(subscore_labels(cfg))["importance"] == "Big news in general"

    cfg.region = RegionCfg(country="Germany", city="Berlin")
    text = instructions(cfg)
    assert "one reader who lives in Berlin, Germany" in text and "for someone living in Germany" in text
    assert '"National" means Germany' in text
    assert "Berlin, Germany" in draft_system(cfg)
    assert dict(subscore_labels(cfg))["importance"] == "Big news for Germany"


def interest_answers(values):
    """An answer that gives the items, in order, these interest values (a missing key for MISSING)."""
    def handler(kwargs):
        import re
        text = kwargs["messages"][0]["content"][-1]["text"]
        ids = [int(i) for i in re.findall(r"id=(\d+)", text)]
        items = []
        for i, value in zip(ids, values):
            item = {"id": i, "topic": 7, "importance": 5, "personal": 6, "reason": "test", "category": "Local"}
            if value is not MISSING:
                item["interest"] = value
            items.append(item)
        return json.dumps({"items": items}), "end_turn"
    return handler


MISSING = object()


def test_scoring_labels_each_item_with_a_want_line(conn, cfg):
    write_profile("interests.md", "## Want\n- Basketball\n- Local  news.\n- basketball\n\n## Skip\n- Celebrities\n")
    seed_items(conn, cfg, 6)
    ai = AIClient(cfg, conn, fake_client(interest_answers([2, 1, 0, 7, "2", MISSING])))
    assert run_scoring(conn, cfg, ai)["scored"] == 6
    call = ai.client.messages.calls[0]
    # numbered once each (the repeated basketball line is dropped), in the cached profile block
    assert "<want_lines>\n1. Basketball\n2. Local  news.\n</want_lines>" in call["system"][1]["text"]
    schema = call["output_config"]["format"]["schema"]["properties"]["items"]["items"]
    assert schema["properties"]["interest"] == {"type": "integer", "enum": [0, 1, 2]}
    assert "interest" in schema["required"]
    rows = conn.execute("SELECT interest, score_status FROM items ORDER BY fetched_at DESC, id DESC").fetchall()
    # 2 and 1 map to their lines (normalized); 0, out of range, a string and a missing key store nothing
    assert [r["interest"] for r in rows] == ["local news", "basketball", None, None, None, None]
    assert {r["score_status"] for r in rows} == {"scored"}  # a missing or wrong label never means a retry


def test_scoring_without_want_lines_offers_only_none(conn, cfg):
    write_profile("interests.md", "## Skip\n- Celebrities\n")
    seed_items(conn, cfg, 2)
    ai = AIClient(cfg, conn, fake_client(echo_scores))
    assert run_scoring(conn, cfg, ai)["scored"] == 2
    call = ai.client.messages.calls[0]
    assert "<want_lines>" not in call["system"][1]["text"]
    schema = call["output_config"]["format"]["schema"]["properties"]["items"]["items"]
    assert schema["properties"]["interest"]["enum"] == [0]


def test_reworded_want_lines_keep_their_labels(conn, cfg):
    from newsnest.scoring import carry_interest_labels
    seed_items(conn, cfg, 3)
    conn.execute("UPDATE items SET interest = 'linux' WHERE id IN (1, 2)")
    conn.execute("UPDATE items SET interest = 'ai coding' WHERE id = 3")
    changes = [{"op": "reword", "section": "want", "old": "Linux", "new": "Linux and homelab"},
               {"op": "reword", "section": "want", "old": "AI coding", "new": "AI agents"},  # edited away before saving
               {"op": "add", "section": "want", "old": "", "new": "Recalls"}]
    moved = carry_interest_labels(conn, changes, "## Want\n- Linux and homelab\n- AI coding\n- Recalls\n")
    assert moved == 2
    assert [r[0] for r in conn.execute("SELECT interest FROM items ORDER BY id")] == [
        "linux and homelab", "linux and homelab", "ai coding"]
