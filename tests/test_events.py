import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from newsnest.ai import AIClient
from newsnest.config import Source
from newsnest.db import connect, migrate
from newsnest.events import active_events_text, normalize_label, run_labeling
from newsnest.feed import build_feed
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.profile import write_profile
from newsnest.scoring import run_scoring
from newsnest.watches import Prefilter
from tests.conftest import fake_client

NOW = datetime.now(timezone.utc)
SOURCES = [Source(id=s, type="rss", category="news", name=s.upper(), url=f"https://{s}.si/rss") for s in "abcde"]


@pytest.fixture
def db(env, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    cfg.scoring.calibration_mode = False
    c = connect()
    migrate(c)
    yield c
    c.close()


def add(db, cfg, sid, title, when=NOW):
    src = next(s for s in SOURCES if s.id == sid)
    url = f"https://{sid}.si/{abs(hash(title)) % 10**8}"
    ingest(db, src, [Item(sid, "news", title, url, url, when)], Prefilter(cfg), when)
    return db.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()[0]


def labelling_handler(labels: dict[str, str]):
    """Fake Claude: labels each item by a keyword found in its title; scores everything 7/6/7."""
    def handler(kwargs):
        text = kwargs["messages"][0]["content"][-1]["text"]
        out = []
        for m in re.finditer(r"id=(\d+) \|[^\n]*", text):
            line = m.group(0)
            label = next((lab for key, lab in labels.items() if key in line), "misc")
            out.append({"id": int(m.group(1)), "topic": 7, "importance": 6, "personal": 7, "reason": "t",
                        "category": "Security", "event": label, "text": ""})
        if "event_label" in json.dumps(kwargs["system"]) or "label news items" in json.dumps(kwargs["system"]):
            return json.dumps({"items": [{"id": o["id"], "event": o["event"]} for o in out]}), "end_turn"
        return json.dumps({"items": [{k: v for k, v in o.items() if k != "text"} for o in out]}), "end_turn"
    return handler


def test_label_normalization():
    assert normalize_label("PHP2Shell Vulnerability!") == "php2shell-vulnerability"
    assert normalize_label("") is None and normalize_label(None) is None and normalize_label("none") is None


def test_scoring_stores_event_and_shows_active_events_to_claude(db, cfg):
    a = add(db, cfg, "a", "Hackers pile on PHP2Shell as AWS reveals attacks")
    ai = AIClient(cfg, db, fake_client(labelling_handler({"PHP2Shell": "php2shell-vulnerability"})))
    run_scoring(db, cfg, ai)
    assert db.execute("SELECT event FROM items WHERE id = ?", (a,)).fetchone()[0] == "php2shell-vulnerability"
    assert active_events_text(db).splitlines() == ["php2shell-vulnerability"]
    add(db, cfg, "b", "Ranljivost v PHP-ju prizadela 70 odstotkov interneta, PHP2Shell")
    run_scoring(db, cfg, ai)
    prompt = ai.client.messages.calls[-1]["messages"][0]["content"]
    assert any("<active_events>" in blk["text"] and "php2shell-vulnerability" in blk["text"] for blk in prompt)
    assert ai.client.messages.calls[-1]["output_config"]["format"]["schema"]["properties"]["items"]["items"]["required"][-1] == "event"


def test_cards_merge_by_event_even_when_headlines_share_nothing(db, cfg):
    a = add(db, cfg, "a", "Internet je imel zelo slab dan")
    b = add(db, cfg, "b", "Spletne strani po svetu nedostopne")
    c = add(db, cfg, "c", "Maribor dobil novo knjižnico")
    assert db.execute("SELECT COUNT(DISTINCT story_id) FROM items").fetchone()[0] == 3  # the matcher alone cannot link them
    for i in (a, b):
        db.execute("UPDATE items SET score_status='scored', topic=7, importance=6, personal=7, ai_category='Security', event='php2shell' WHERE id=?", (i,))
    db.execute("UPDATE items SET score_status='scored', topic=7, importance=6, personal=7, ai_category='Culture', event='maribor-knjiznica' WHERE id=?", (c,))
    feed = build_feed(db, cfg, SOURCES)
    assert len(feed.items) == 2
    merged = next(it for it in feed.items if it.card.articles == 2)
    assert merged.card.outlets == 2 and {m.id for m in merged.card.others} | {merged.card.lead.id} == {a, b}


def test_reading_or_hiding_one_article_covers_the_whole_event(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    ids = [add(db, cfg, s, t) for s, t in (("a", "Internet je imel zelo slab dan"), ("b", "Spletne strani po svetu nedostopne"),
                                           ("c", "Napad na spletne storitve po vsem svetu"))]
    db.execute("UPDATE items SET score_status='scored', topic=7, importance=6, personal=7, ai_category='Security', event='php2shell'")
    from newsnest.web import app
    with TestClient(app) as client:
        client.get(f"/go/{ids[0]}", follow_redirects=False)  # opens one article of the event
        assert db.execute("SELECT COUNT(*) FROM items WHERE last_read_at IS NOT NULL").fetchone()[0] == 3
        assert build_feed(db, cfg, SOURCES).items == []
        client.post("/api/read", data={"ids": str(ids[1]), "value": "0"})  # unread one: all come back
        assert len(build_feed(db, cfg, SOURCES).items) == 1
        client.post("/api/feedback", data={"action": "not_interested", "ids": str(ids[2])})
        assert build_feed(db, cfg, SOURCES).items == []  # a new sibling later stays hidden with the card
        add(db, cfg, "d", "Še en članek o istem dogodku")
        db.execute("UPDATE items SET event='php2shell', score_status='scored', topic=7, importance=6, personal=7, ai_category='Security' WHERE source_id='d'")
        assert build_feed(db, cfg, SOURCES).items == []


def test_backlog_labelling_labels_old_scored_items_and_reuses_labels(db, cfg):
    ids = [add(db, cfg, s, t, NOW - timedelta(hours=h)) for s, t, h in (
        ("a", "PHP2Shell hits the internet", 5), ("b", "Ranljivost PHP2Shell prizadela splet", 3), ("c", "Maribor library opens", 2))]
    db.execute("UPDATE items SET score_status='scored', topic=7, importance=6, personal=7, ai_category='Security'")
    ai = AIClient(cfg, db, fake_client(labelling_handler({"PHP2Shell": "php2shell-vulnerability", "Maribor": "maribor-library"})))
    assert run_labeling(db, cfg, ai) == {"labelled": 3}
    labels = [r[0] for r in db.execute("SELECT event FROM items ORDER BY id")]
    assert labels == ["php2shell-vulnerability", "php2shell-vulnerability", "maribor-library"]
    assert run_labeling(db, cfg, ai) == {"labelled": 0}  # nothing left; no extra call
    assert len(ai.client.messages.calls) == 1
    assert ids  # keep flake quiet
