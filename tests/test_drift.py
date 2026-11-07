"""Following what the reader is into lately (newsnest.drift): the maths on simulated readers, then the
signals read from the database, the ranking, the revisit story and the pages."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from newsnest.db import iso, set_meta
from newsnest.drift import (CardSignals, Drift, compute_drift, compute_weights, engagement, linger_threshold,
                            load_signals, short_label)
from newsnest.feed import rank_feed, view_feed
from tests.test_feed import NOW, SOURCES, add, db  # noqa: F401  (db is a fixture)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BB = "interest:luka dončić and nba basketball"


# ---------- the maths, on simulated readers ----------

def day_cards(day, n, key, *, tapped=0, lingered=0, view_quick=500, view_long=10_000, source="bg"):
    """n cards shown on `day`: the first `tapped` were opened, the next `lingered` stayed on screen long."""
    at = T0 + timedelta(days=day, hours=12)
    out = []
    for i in range(n):
        out.append(CardSignals(key=key, source=source, at=at, tapped=i < tapped,
                               view_ms=view_long if tapped <= i < tapped + lingered else view_quick))
    return out


def background(days, tap_rate):
    """280 cards a day across a few categories: tap_rate of them opened, a quarter lingered on."""
    cards = []
    for d in days:
        for j, cat in enumerate(("category:World", "category:AI", "category:Science", "category:Politics")):
            cards += day_cards(d, 70, cat, tapped=round(70 * tap_rate), lingered=18, source=f"bg{j}")
    return cards


def weights_at(cfg, signals, day):
    now = T0 + timedelta(days=day + 1)
    start = now - timedelta(days=cfg.drift.window_days)
    return compute_weights([s for s in signals if start <= s.at <= now], cfg, now)


@pytest.mark.parametrize("tap_rate", [0.03, 0.07, 0.25])
def test_an_interest_fades_and_comes_back(cfg, tap_rate):
    """Basketball read for 60 days, then only scrolled past for 60, then read again."""
    for mode in ("linger", "tap"):
        bg = background(range(134), tap_rate)
        bb = []
        for d in range(134):
            reading = d < 60 or d >= 120
            if not reading:
                bb += day_cards(d, 3, BB, source="bb")
            elif mode == "linger":
                bb += day_cards(d, 3, BB, lingered=3, source="bb")
            else:
                bb += day_cards(d, 3, BB, tapped=3, view_quick=10_000, source="bb")
        signals = bg + bb
        w = {d: weights_at(cfg, signals, d)[BB] for d in (59, 119, 122, 126, 133)}
        assert w[59] >= 1.1, (mode, tap_rate, w)
        assert w[119] <= 0.7, (mode, tap_rate, w)
        if mode == "tap":
            assert w[126] >= 1.0, (mode, tap_rate, w)
        elif tap_rate < 0.25:
            assert w[133] >= 1.0, (mode, tap_rate, w)
        else:  # a reader who taps a lot is below their own usual when they only linger: it still climbs
            assert w[119] <= w[122] <= w[126] < w[133], w
        for weights in (weights_at(cfg, signals, d) for d in (59, 119, 133)):
            assert all(0.6 <= v <= 1.4 for k, v in weights.items() if not k.startswith("source:"))
            assert all(0.8 <= v <= 1.25 for k, v in weights.items() if k.startswith("source:"))


def test_a_headline_reader_is_not_sunk(cfg):
    """Sports read from the headline and summary (lingered on, never opened) while other topics get taps."""
    signals = background(range(60), 0.07)
    for d in range(60):
        signals += day_cards(d, 10, "category:Sports", lingered=6, source="sport")
    assert weights_at(cfg, signals, 59)["category:Sports"] >= 1.0


def test_a_few_signals_on_a_quiet_interest_change_little(cfg):
    bg = background(range(60), 0.07)
    at = T0 + timedelta(days=59, hours=18)
    quiet = "interest:ljubljana airport"
    skips = [CardSignals(key=quiet, source="x", at=at, view_ms=400) for _ in range(3)]
    assert abs(weights_at(cfg, bg + skips, 59)[quiet] - 1.0) < 0.1
    tap = [CardSignals(key=quiet, source="x", at=at, view_ms=400, tapped=True)]
    assert 1.0 < weights_at(cfg, bg + tap, 59)[quiet] <= 1.3
    down = [CardSignals(key=quiet, source="x", at=at, view_ms=400, disliked=True)]
    assert 0.6 <= weights_at(cfg, bg + down, 59)[quiet] < 1.0


def test_lingered_is_the_readers_own_top_quarter():
    few = [CardSignals(key="k", source="s", at=T0, view_ms=50_000) for _ in range(199)]
    assert linger_threshold(few) is None  # too little to know what "longer than usual" is
    spread = [CardSignals(key="k", source="s", at=T0, view_ms=(i + 1) * 100) for i in range(400)]
    assert linger_threshold(spread) == 30_000
    assert sum(engagement(s, 30_000) > 0 for s in spread) == 101
    slow = [CardSignals(key="k", source="s", at=T0, view_ms=1_000) for _ in range(300)]
    assert linger_threshold(slow) == 3_000  # the floor: a second on everything is not lingering


def test_engagement_values():
    s = CardSignals(key="k", source="s", at=T0, view_ms=0)
    assert engagement(s, None) == 0
    s.tapped = s.liked = s.saved = s.expanded = True
    s.view_ms = 9_000
    assert engagement(s, 3_000) == 3  # at most 3
    s.disliked = True
    assert engagement(s, 3_000) == -1  # 👎 wins


def test_no_engagement_at_all_means_nothing_to_compare(cfg):
    signals = [CardSignals(key="category:World", source="s", at=T0, view_ms=100) for _ in range(50)]
    assert compute_weights(signals, cfg, T0 + timedelta(days=1)) == {}


def test_card_multiplier_and_labels():
    d = Drift(active=True, weights={"category:Sports": 0.6, "source:a": 0.8})
    assert d.min_weight == 0.6 and d.max_weight == 1.4
    assert short_label("AI-assisted software development: Coding agents, developer workflows") == \
        "AI-assisted software development"
    assert short_label("Luka Dončić and NBA basketball") == "Luka Dončić and NBA basketball"
    assert short_label("Cybersecurity incidents, critical vulnerabilities and emergency patches (e.g. PHP)") == \
        "Cybersecurity incidents, critical…"
    assert d.faded() == {"category:Sports"}


# ---------- signals from the database ----------

def shown(db, item_id, when=NOW, view_ms=0, **cols):
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    db.execute(f"UPDATE items SET seen_count = 1, last_seen_at = :at, view_ms = :v{', ' + sets if sets else ''} WHERE id = :id",
               {"at": iso(when), "v": view_ms, "id": item_id, **cols})


def test_signals_are_counted_once_per_card(db, cfg):
    since = NOW - timedelta(days=1)
    # one story from four outlets
    ids = [add(db, cfg, s, "Nova bolnišnica v Ljubljani odpira vrata", f"https://{s}.si/h", cat="Health")
           for s in "abcd"]
    assert len({r[0] for r in db.execute("SELECT story_id FROM items")}) == 1
    for i in ids:
        shown(db, i, view_ms=4000)
    db.execute("UPDATE items SET opened_at = ? WHERE id = ?", (iso(NOW), ids[2]))
    # two stories joined by an event label
    x = add(db, cfg, "a", "Pogačar zmagal na dirki po Franciji", "https://a.si/t", cat="Sports")
    y = add(db, cfg, "b", "Rumena majica ostaja v Sloveniji", "https://b.si/t", cat="Sports")
    db.execute("UPDATE items SET event = 'tour-de-france' WHERE id IN (?, ?)", (x, y))
    shown(db, x)
    # rated in the swipe deck only, never on screen in the feed: not counted
    z = add(db, cfg, "c", "Novi davki na nepremičnine", "https://c.si/z", cat="Economy")
    db.execute("UPDATE items SET rating = 1, rated_at = ? WHERE id = ?", (iso(NOW), z))
    signals = load_signals(db, cfg, since, {})
    assert sorted(s.key for s in signals) == ["category:Health", "category:Sports"]
    health = next(s for s in signals if s.key == "category:Health")
    assert health.tapped and health.view_ms == 4000


def test_a_label_counts_only_while_its_line_is_in_the_file(db, cfg):
    i = add(db, cfg, "a", "Dončić dal 40 točk", "https://a.si/b", cat="Sports")
    db.execute("UPDATE items SET interest = 'luka dončić and nba basketball' WHERE id = ?", (i,))
    shown(db, i)
    lines = {"luka dončić and nba basketball": "Luka Dončić and NBA basketball"}
    assert [s.key for s in load_signals(db, cfg, NOW - timedelta(days=1), lines)] == [BB]
    assert [s.key for s in load_signals(db, cfg, NOW - timedelta(days=1), {})] == ["category:Sports"]


def test_not_interested_then_undo_is_not_a_dislike(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    i = add(db, cfg, "a", "Še ena zgodba o vremenu", "https://a.si/w", cat="Weather")
    shown(db, i)
    from newsnest.web import app
    with TestClient(app) as client:
        client.post("/api/feedback", data={"action": "not_interested", "ids": str(i)})
        assert load_signals(db, cfg, NOW - timedelta(days=1), {})[0].disliked
        client.post("/api/feedback", data={"action": "undo_not_interested", "ids": str(i)})
    assert not load_signals(db, cfg, NOW - timedelta(days=1), {})[0].disliked


def test_warm_up(db, cfg):
    cfg.drift.warmup_stories = 3
    lines = {}
    assert not compute_drift(db, cfg, NOW, lines).active
    assert compute_drift(db, cfg, NOW, lines).warmup_days_left == 14
    ids = [add(db, cfg, s, f"Zgodba {s} o temi {s}", f"https://{s}.si/x", cat=c) for s, c in zip("abc", ("A", "B", "C"))]
    for i in ids:
        shown(db, i, view_ms=1000)
    db.execute("UPDATE items SET opened_at = ? WHERE id = ?", (iso(NOW), ids[0]))
    set_meta(db, "attention_since", iso(NOW - timedelta(days=13, hours=1)))
    d = compute_drift(db, cfg, NOW, lines)
    assert not d.active and d.warmup_days_left == 1
    set_meta(db, "attention_since", iso(NOW - timedelta(days=15)))
    cfg.drift.warmup_stories = 4  # days are done, stories are not
    d = compute_drift(db, cfg, NOW, lines)
    assert not d.active and d.warmup_days_left == 0
    cfg.drift.warmup_stories = 3
    d = compute_drift(db, cfg, NOW, lines)
    assert d.active and d.weights["category:A"] > 1 > d.weights["category:B"]
    cfg.drift.enabled = False
    assert compute_drift(db, cfg, NOW, lines) == Drift()


def test_rankings_use_the_stored_weights_while_they_are_fresh(db, cfg, monkeypatch):
    from newsnest.drift import current_drift, refresh_drift
    from newsnest.profile import write_profile
    import newsnest.drift as drift_mod
    calls = []
    real = drift_mod.compute_drift
    monkeypatch.setattr(drift_mod, "compute_drift", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert refresh_drift(db, cfg, NOW) is not None and len(calls) == 1      # nothing stored yet
    assert refresh_drift(db, cfg, NOW + timedelta(minutes=10)) is None        # fresh: the scheduler skips it
    assert current_drift(db, cfg, NOW + timedelta(hours=1)).warmup_days_left == 14 and len(calls) == 1
    assert refresh_drift(db, cfg, NOW + timedelta(minutes=31)) is not None and len(calls) == 2
    write_profile("interests.md", "## Want\n- news\n- basketball\n")          # other Want lines: other keys
    current_drift(db, cfg, NOW + timedelta(minutes=32))
    assert len(calls) == 3
    current_drift(db, cfg, NOW + timedelta(hours=4))                          # the scheduler is off: too old
    assert len(calls) == 4
    cfg.drift.enabled = False
    assert current_drift(db, cfg, NOW) == Drift() and len(calls) == 4


def test_the_signal_lookup_uses_indexes(db, cfg):
    plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + _signal_sql(), {"s": iso(NOW)})]
    assert not any(step.startswith("SCAN items") for step in plan), plan


def _signal_sql():
    import inspect
    import newsnest.drift as drift_mod
    src = inspect.getsource(drift_mod.load_signals)
    return src.split('"""WITH', 1)[1].split('""", {"s"', 1)[0].join(["WITH", ""])


# ---------- ranking ----------

@pytest.fixture
def drifted(monkeypatch):
    """Rank with these drift weights, as if the reader had read that way lately."""
    def use(weights, dislikes=()):
        d = Drift(active=True, weights=weights, recent_dislikes=set(dislikes))
        monkeypatch.setattr("newsnest.feed.current_drift", lambda conn, cfg, now=None: d)
        return d
    return use


def test_a_faded_interest_sinks_but_big_news_does_not(db, cfg, drifted):
    a = add(db, cfg, "a", "Tekma lige NBA", "https://a.si/1", cat="Sports", topic=7, importance=5, personal=7)
    b = add(db, cfg, "b", "Nova pravila parkiranja", "https://b.si/1", cat="Local", topic=7, importance=5, personal=7)
    t = add(db, cfg, "c", "Potres v Ljubljani", "https://c.si/1", cat="Sports", topic=7, importance=10, personal=7)
    off = rank_feed(db, cfg, SOURCES)
    drifted({"category:Sports": 0.6})
    on = rank_feed(db, cfg, SOURCES)
    rank = {it.card.lead.id: it for it in on.items}
    before = {it.card.lead.id: it for it in off.items}
    assert rank[a].rank < rank[b].rank and before[a].rank == before[b].rank
    assert rank[t].rank == before[t].rank  # a top story is never lowered
    assert (-1, "You've read less about Sports lately") in rank[a].why
    assert not any("lately" in text for _, text in rank[t].why)
    # order changes, not which stories are in or how they score
    assert sorted((it.card.lead.id, it.card.final, it.below) for it in on.items) == \
        sorted((it.card.lead.id, it.card.final, it.below) for it in off.items)
    assert on.dropped == off.dropped


def test_old_boosts_step_aside_while_drift_is_on(db, cfg, drifted):
    a = add(db, cfg, "a", "Tekma lige NBA", "https://a.si/1", cat="Sports", topic=7, importance=5, personal=7)
    db.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES ('category', 'Sports', 1.5, ?)", (iso(NOW),))
    boosted = rank_feed(db, cfg, SOURCES).items[0]
    assert ("You tend to like Sports" in [t for _, t in boosted.why])
    drifted({})
    plain = rank_feed(db, cfg, SOURCES).items[0]
    assert plain.card.lead.id == a and plain.rank < boosted.rank
    assert not any("Sports" in t for _, t in plain.why)


def many(db, cfg, n, cat, prefix, topic=7):
    return [add(db, cfg, "abcde"[i % 5], f"{prefix} {i} {cat} {i * 13}", f"https://x.si/{prefix}{i}", cat=cat,
                topic=topic, importance=5, personal=7) for i in range(n)]


def test_one_story_from_a_faded_interest_is_placed_early(db, cfg, drifted):
    cfg.feed.explore_every = 4
    for s in SOURCES:
        s.daily_cap = None
    many(db, cfg, 20, "Local", "lokalno", topic=9)
    sports = many(db, cfg, 3, "Sports", "sport", topic=6)
    drifted({"category:Sports": 0.6})
    ranking = rank_feed(db, cfg, SOURCES)
    revisits = [it for it in ranking.items if it.extra == "revisit"]
    assert len(revisits) == 1 and revisits[0].card.lead.id in sports
    feed = view_feed(ranking, cfg, "all")
    assert feed.items[3].extra == "revisit"   # after the first three cards
    assert (0, "Something you used to read, shown now and then") not in feed.items[3].why  # a template note, not a why line
    cat = view_feed(ranking, cfg, "cat:news:Sports")
    assert [it.card.lead.id for it in cat.items] == [it.card.lead.id for it in ranking.items if it.category == "Sports"]


def test_no_revisit_when_off_warming_up_or_turned_down(db, cfg, drifted, monkeypatch):
    cfg.feed.explore_every = 4
    many(db, cfg, 20, "Local", "lokalno", topic=9)
    many(db, cfg, 3, "Sports", "sport", topic=6)
    cfg.drift.revisit_slots = 0
    drifted({"category:Sports": 0.6})
    assert not any(it.extra == "revisit" for it in rank_feed(db, cfg, SOURCES).items)
    cfg.drift.revisit_slots = 1
    drifted({"category:Sports": 0.6}, dislikes={"category:Sports"})
    assert not any(it.extra == "revisit" for it in rank_feed(db, cfg, SOURCES).items)
    monkeypatch.setattr("newsnest.feed.current_drift", lambda conn, cfg, now=None: Drift(warmup_days_left=3))
    assert not any(it.extra == "revisit" for it in rank_feed(db, cfg, SOURCES).items)


def test_revisit_survives_a_stored_feed_session(db, cfg, drifted):
    from newsnest import feedsession
    cfg.feed.explore_every = 4
    many(db, cfg, 20, "Local", "lokalno", topic=9)
    many(db, cfg, 3, "Sports", "sport", topic=6)
    drifted({"category:Sports": 0.6})
    sid, ranking = feedsession.start(db, cfg, SOURCES)
    again = feedsession.resume(db, cfg, SOURCES, sid)
    assert [it.extra for it in again.items] == [it.extra for it in ranking.items]
    assert "revisit" in [it.extra for it in again.items]


# ---------- pages ----------

def test_profile_shows_warm_up_then_lately(db, cfg, env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.web import app
    with TestClient(app) as client:
        page = client.get("/profile").text
        assert "Learning what you read lately" in page and "Ready in about 14 days" in page
        d = Drift(active=True, weights={"category:Sports": 0.62, "interest:x": 1.3, "source:a": 1.2},
                  lines={"x": "Luka Dončić and NBA basketball: games, trades"})
        monkeypatch.setattr("newsnest.web.current_drift", lambda conn, cfg: d)
        page = client.get("/profile").text
        lately = page.split(">Lately<", 1)[1].split("</section>", 1)[0] if ">Lately<" in page else page
        assert "Luka Dončić and NBA basketball, a" in lately and "Sports" in lately


def test_settings_switch_turns_drift_off_and_back_on(db, cfg, env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.web import app
    form = {"min_final": "5", "daily_usd": "0.5", "summary_language": "English"}
    with TestClient(app) as client:
        assert 'name="follow_lately" checked' in client.get("/settings/general").text
        client.post("/settings/general", data=form)
        text = (env.config / "config.yaml").read_text(encoding="utf-8")
        assert "drift:\n  enabled: false" in text
        assert 'name="follow_lately" checked' not in client.get("/settings/general").text
        client.post("/settings/general", data={**form, "follow_lately": "on"})
        text = (env.config / "config.yaml").read_text(encoding="utf-8")
        assert "\ndrift:" not in text  # back to the default, nothing written
