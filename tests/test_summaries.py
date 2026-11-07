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


def run_one(conn, cfg, row, handler, text=ARTICLE, monkeypatch=None, paid=False):
    import newsnest.summaries
    monkeypatch.setattr(newsnest.summaries, "extract_article",
                        lambda ctx, url: newsnest.summaries.Article(text, None, None, paid))
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


def ld(*entities):
    return "".join(f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in entities)


# How publishers mark subscriber articles for search engines (Google's paywalled-content markup)
PAID_PAGES = {
    "article": ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "x",
                   "isAccessibleForFree": False,
                   "hasPart": {"@type": "WebPageElement", "isAccessibleForFree": False, "cssSelector": ".paywall"}}),
    "string value": ld({"@type": "NewsArticle", "isAccessibleForFree": "False"}),
    "only the part": ld({"@type": "Article", "hasPart": [{"@type": "WebPageElement", "isAccessibleForFree": "false"}]}),
    "graph": ld({"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "x"},
                                                             {"@type": "NewsArticle", "isAccessibleForFree": False}]}),
    "main entity": ld({"@type": "WebPage", "mainEntity": {"@type": "NewsArticle", "isAccessibleForFree": False}}),
    "list": ld([{"@type": "BreadcrumbList"}, {"@type": "NewsArticle", "isAccessibleForFree": False}]),
    "microdata": '<article itemscope itemtype="https://schema.org/NewsArticle">'
                 '<meta itemprop="isAccessibleForFree" content="false"></article>',
}
FREE_PAGES = {
    "marked free": ld({"@type": "NewsArticle", "isAccessibleForFree": True}),
    "marked free as text": ld({"@type": "NewsArticle", "isAccessibleForFree": "True"}),
    "no markup": "<html><body><p>Just an article.</p></body></html>",
    "no flag": ld({"@type": "NewsArticle", "headline": "x"}),
    # a free article whose sidebar lists a subscriber one: only the page's own article counts
    "paid article in a list": ld({"@type": "NewsArticle", "isAccessibleForFree": True},
                                 {"@type": "ItemList", "itemListElement": [
                                     {"@type": "ListItem", "item": {"@type": "NewsArticle", "isAccessibleForFree": False}}]}),
    "broken json": '<script type="application/ld+json">{"isAccessibleForFree": false,</script>',
}


@pytest.mark.parametrize("name", PAID_PAGES)
def test_publisher_paywall_markup_is_recognised(name):
    from newsnest.summaries import publisher_marks_paid
    assert publisher_marks_paid(f"<html><head>{PAID_PAGES[name]}</head><body></body></html>")


@pytest.mark.parametrize("name", FREE_PAGES)
def test_free_pages_are_not_marked(name):
    from newsnest.summaries import publisher_marks_paid
    assert not publisher_marks_paid(f"<html><head>{FREE_PAGES[name]}</head><body></body></html>")


def test_extract_article_reports_paid_even_when_only_a_teaser_is_readable(cfg, monkeypatch):
    import newsnest.summaries

    class Page:
        text = f"<html><head>{PAID_PAGES['article']}</head><body><p>Subscribe to read.</p></body></html>"
        url = "https://news.example/a"
    monkeypatch.setattr(newsnest.summaries, "http_get", lambda ctx, url: Page())
    a = newsnest.summaries.extract_article(FetchContext(cfg, None), Page.url)
    assert a.text is None and a.paid


def test_paid_flag_is_stored_with_the_item(conn, cfg, monkeypatch):
    _, _, row = run_one(conn, cfg, seed(conn, cfg), answer(), monkeypatch=monkeypatch, paid=True)
    assert json.loads(row["extra"])["paid"] is True
    _, _, free = run_one(conn, cfg, conn.execute("SELECT * FROM items WHERE url='https://24ur.com/x'").fetchone(),
                         answer(), monkeypatch=monkeypatch)
    assert "paid" not in json.loads(free["extra"] or "{}")


@pytest.fixture
def client(env, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    conn = connect()
    migrate(conn)
    seed(conn, cfg)
    conn.close()
    import newsnest.summaries
    import newsnest.web
    monkeypatch.setattr(newsnest.summaries, "extract_article",
                        lambda ctx, url: newsnest.summaries.Article(ARTICLE, None, None, "paid" in url))
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
    # the publisher's snippet stays on the card, so the AI's lede goes inside the box, labelled as the AI's
    assert '<p class="tldr-lede"><span class="tag ai">AI summary</span>' in body["html"]
    assert 'class="summary lede"' not in body["html"]
    page = client.get("/?f=all").text  # stored: the next load shows it without another call
    assert page.count("Get key points") == 1 and "Reopens in spring" in page
    # asking for key points is a sign of interest, like opening them (newsnest.drift)
    conn = connect()
    assert conn.execute("SELECT expanded_at FROM items WHERE id = 1").fetchone()[0] is not None
    assert conn.execute("SELECT expanded_at FROM items WHERE id = 2").fetchone()[0] is None
    card = page[page.index('id="item-1"'):]
    assert card.index('class="snippet') < card.index('class="tldr"')  # the snippet is kept, above the key points
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


def test_subscriber_articles_show_a_lock_and_keep_their_place(client):
    """The lock is only a label: the story stays where it was and the card still opens the same article."""
    conn = connect()
    conn.execute("UPDATE items SET url = 'https://24ur.com/paid' WHERE id = 1")
    before = client.get("/?f=all").text
    assert 'aria-label="Subscriber article"' not in before
    client.post("/api/items/1/summarize", headers={"Accept": "application/json"})
    client.post("/api/items/2/summarize", headers={"Accept": "application/json"})
    page = client.get("/?f=all").text
    page = page[page.index('id="stream"'):]  # the feed (the wide-screen front page before it repeats stories)
    assert page.count('aria-label="Subscriber article"') == 1
    lock = page.index('aria-label="Subscriber article"')
    assert page.rindex('id="item-', 0, lock) == page.index('id="item-1"')  # on the paid story's card
    def paid_first(p):
        return p.index('id="item-1"') < p.index('id="item-2"')
    assert paid_first(before) == paid_first(page)
    assert '<a href="/go/1"' in page
