import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from newsnest.ai import AIClient, BudgetExceeded
from newsnest.config import Source
from newsnest.db import connect, iso, migrate
from newsnest.fetchers.base import FetchContext
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.watches import Prefilter
from tests.conftest import fake_client

NOW = datetime.now(timezone.utc)
ARTICLE = "Mestna občina Ljubljana bo Slovensko cesto zaprla za promet. " * 20


def answer(points=("Closed from 3 November", "Buses 2, 6 and 11 rerouted", "Works cost 4.2 million euros",
                   "Reopens in spring", "A fifth point"), why="Your daily route crosses Slovenska",
           depth="full", tag="guide"):
    payload = {"lede": "Ljubljana closes Slovenska cesta to all traffic for four months.",
               "points": list(points), "why": why, "depth": depth, "depth_tag": tag}
    return lambda kw: (json.dumps(payload), "end_turn")


def seed(conn, cfg, personal=7):
    s = Source(id="24ur", type="rss", category="news", name="24ur", url="https://24ur.com/rss")
    ingest(conn, s, [Item("24ur", "news", "Ljubljana zapira Slovensko cesto", "https://24ur.com/b", "c", NOW,
                          snippet="Promet bo preusmerjen ...")], Prefilter(cfg), NOW)
    ingest(conn, s, [Item("24ur", "news", "Airbnb prepoveduje pomivanje posode", "https://24ur.com/x", "d", NOW,
                          snippet="Gostje ne bodo več ...")], Prefilter(cfg), NOW)
    conn.execute("UPDATE items SET score_status='scored', topic=8, importance=6, personal=?, reason='Ljubljana traffic', "
                 "ai_category='Ljubljana', scored_at=? WHERE url='https://24ur.com/b'", (personal, iso(NOW)))
    return conn.execute("SELECT * FROM items WHERE url='https://24ur.com/b'").fetchone()


def run_one(conn, cfg, row, handler, text=ARTICLE, monkeypatch=None):
    import newsnest.summaries
    monkeypatch.setattr(newsnest.summaries, "extract_article", lambda ctx, url: (text, None, None))
    ai = AIClient(cfg, conn, fake_client(handler))
    status = newsnest.summaries.summarize_one(conn, cfg, ai, FetchContext(cfg, None), row)
    return status, ai, conn.execute("SELECT * FROM items WHERE id = ?", (row["id"],)).fetchone()


def test_key_points_are_stored(conn, cfg, monkeypatch):
    from newsnest.summaries import key_points
    status, ai, row = run_one(conn, cfg, seed(conn, cfg), answer(), monkeypatch=monkeypatch)
    assert status == "done"
    assert row["summary"].startswith("Ljubljana closes Slovenska cesta")
    assert key_points(row) == ["Closed from 3 November", "Buses 2, 6 and 11 rerouted",
                               "Works cost 4.2 million euros", "Reopens in spring"]  # capped at 4
    assert row["summary_why"] == "Your daily route crosses Slovenska"
    assert (row["summary_depth"], row["summary_depth_tag"], row["summary_source"]) == ("full", "guide", "article")
    call = ai.client.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Why it was picked for this reader: Ljubljana traffic" in call["messages"][0]["content"]


def test_personal_angle_only_on_personally_relevant_stories(conn, cfg, monkeypatch):
    _, _, row = run_one(conn, cfg, seed(conn, cfg, personal=3), answer(), monkeypatch=monkeypatch)
    assert row["summary_why"] is None


def test_snippet_only_summary_makes_no_depth_claim(conn, cfg, monkeypatch):
    _, ai, row = run_one(conn, cfg, seed(conn, cfg), answer(depth="enough", tag=""), text=None, monkeypatch=monkeypatch)
    assert row["summary_source"] == "snippet" and row["summary_depth"] is None
    assert "Only the feed snippet is available" in ai.client.messages.calls[0]["messages"][0]["content"]


@pytest.fixture
def client(env, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    conn = connect()
    migrate(conn)
    seed(conn, cfg)
    conn.close()
    import newsnest.summaries
    import newsnest.web
    monkeypatch.setattr(newsnest.summaries, "extract_article", lambda ctx, url: (ARTICLE, None, None))
    monkeypatch.setattr(newsnest.web.AIClient, "create", classmethod(lambda cls, c, k: cls(c, k, fake_client(answer()))))
    with TestClient(newsnest.web.app) as c:
        yield c


def test_cards_offer_the_button_then_show_key_points(client):
    page = client.get("/?f=all").text
    assert page.count("Get key points") == 2
    r = client.post("/api/items/1/summarize", data={"lines": "2"}, headers={"Accept": "application/json"})
    body = r.json()
    assert body["status"] == "done" and body["message"] is None
    assert '<details class="tldr" open>' in body["html"] and "Buses 2, 6 and 11 rerouted" in body["html"]
    # the lede is never clamped, so opening the key points moves nothing above the toggle
    assert "Worth a full read · guide" in body["html"] and "clamp-" not in body["html"]
    page = client.get("/?f=all").text  # stored: the next load shows it without another call
    assert page.count("Get key points") == 1 and "Reopens in spring" in page
    log = connect().execute("SELECT purpose, COUNT(*) FROM ai_calls GROUP BY purpose").fetchall()
    assert [tuple(r) for r in log] == [("summary_tap", 1)]
    client.post("/api/items/1/summarize", headers={"Accept": "application/json"})
    assert connect().execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 1  # already done: no second call


def test_budget_hit_returns_a_friendly_message(client, monkeypatch):
    import newsnest.web

    def over_budget(*args):
        raise BudgetExceeded("daily AI budget of $0.50 reached")
    monkeypatch.setattr(newsnest.web, "summarize_on_demand", over_budget)
    body = client.post("/api/items/1/summarize", headers={"Accept": "application/json"}).json()
    assert body["status"] == "budget" and body["html"] is None and "budget" in body["message"]


def test_older_prose_summaries_still_render(client):
    connect().execute("UPDATE items SET summary_status='done', summary='Old prose summary of the closure.', "
                      "summary_source='article' WHERE id = 1")
    page = client.get("/?f=all").text
    assert "Old prose summary of the closure." in page
    assert 'class="tldr"' not in page and page.count("Get key points") == 1
