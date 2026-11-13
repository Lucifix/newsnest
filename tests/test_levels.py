"""How much of each interest (newsnest.levels): the shift, what it moves across the cut-off, what it never
touches, how levels follow their line, and the Profile page."""

from fastapi.testclient import TestClient  # noqa: F401  (the client fixture comes from test_web)

from newsnest import feedsession
from newsnest.db import connect, iso
from newsnest.digest import make_cards
from newsnest.drift import Drift
from newsnest.feed import build_feed, rank_feed, window_rows
from newsnest.interests import norm
from newsnest.levels import Levels, forget_removed, level_rows, load_levels, set_level
from newsnest.missed import explain_known
from newsnest.profile import write_profile
from newsnest.scoring import carry_interest_labels, final_score
from tests.test_feed import NOW, SOURCES, add, db, ids_in  # noqa: F401  (db is a fixture)
from tests.test_web import client  # noqa: F401  (a fixture)

WATCHES = "Wristwatches: new releases and reviews"
CARRY = "Everyday carry"
INTERESTS = f"## Want\n- news\n- {WATCHES}\n- {CARRY}\n"
W = norm(WATCHES)


def about(conn, item_id, line):
    conn.execute("UPDATE items SET interest = ? WHERE id = ?", (norm(line), item_id))


def setup(db, cfg):
    write_profile("interests.md", INTERESTS)
    cfg.feed.explore_every = 0   # no exploring slots and no new-source tryouts: only the cut-off decides
    cfg.feed.new_source_stories = 0
    for s in SOURCES:
        s.daily_cap = None


def test_final_score_adds_the_shift(cfg):
    row = {"topic": 5, "importance": 5, "personal": 5}
    assert final_score(row, 1, cfg) == 5.0
    assert final_score(row, 1, cfg, -0.5) == 4.5
    assert final_score(row, 2, cfg, 1.0) == 7.0   # with the cross-outlet bonus
    assert final_score({"topic": None}, 1, cfg, 1.0) is None


def test_a_level_moves_a_story_across_the_cut_off(db, cfg):
    setup(db, cfg)
    watch = add(db, cfg, "a", "Nova ura Omega Speedmaster", "https://a.si/1", topic=5, importance=5, personal=5)
    carry = add(db, cfg, "b", "Nov žepni nož Victorinox", "https://b.si/1", topic=4, importance=5, personal=5)
    plain = add(db, cfg, "c", "Vlada sprejela proračun", "https://c.si/1", topic=5, importance=5, personal=5)
    about(db, watch, WATCHES)
    about(db, carry, CARRY)
    assert set(ids_in(build_feed(db, cfg, SOURCES))) == {watch, plain}   # carry scored 4.6
    set_level(db, WATCHES, -1)
    set_level(db, CARRY, 1)
    feed = build_feed(db, cfg, SOURCES)
    assert set(ids_in(feed)) == {carry, plain}
    assert {it.card.lead.id: it.card.final for it in feed.items} == {carry: 5.1, plain: 5.0}
    set_level(db, WATCHES, 0)   # Normal again: the row goes
    assert db.execute("SELECT COUNT(*) FROM interest_levels WHERE interest = ?", (W,)).fetchone()[0] == 0
    assert watch in ids_in(build_feed(db, cfg, SOURCES))


def test_top_stories_and_watches_get_in_whatever_the_level(db, cfg):
    setup(db, cfg)
    top = add(db, cfg, "a", "Potres v Ljubljani", "https://a.si/1", topic=2, importance=9, personal=2)
    watched = add(db, cfg, "b", "Nova ura Omega Speedmaster", "https://b.si/1", topic=2, importance=2, personal=2)
    db.execute("UPDATE items SET prefilter = 'must_show' WHERE id = ?", (watched,))
    for item_id in (top, watched):
        about(db, item_id, WATCHES)
    set_level(db, WATCHES, -2)
    assert set(ids_in(build_feed(db, cfg, SOURCES))) == {top, watched}


def test_a_card_gets_one_shift_from_its_interest(db, cfg):
    """An article that matched nothing can't lead the card past the level: the card's interest decides one
    shift for all of its articles, and the lead stays the one it would be anyway."""
    setup(db, cfg)
    matched = add(db, cfg, "a", "Papežu Leonu XIV. se rak ni razširil", "https://a.si/1", topic=5, importance=5, personal=5)
    other = add(db, cfg, "b", "Papežu se rak ni razširil", "https://b.si/1", topic=7, importance=5, personal=5)
    about(db, matched, WATCHES)
    set_level(db, WATCHES, -1)
    [card] = make_cards(window_rows(db, iso(NOW.replace(year=NOW.year - 1))), cfg, {}, load_levels(db))
    assert card.lead.id == other and card.interest == W and card.shift == -0.5
    assert card.final == 6.8 - 0.5 and card.others[0].final == 6.0 - 0.5   # 2 outlets: +1 each
    assert card.interest_label == "Wristwatches"
    # without levels (drift picks leads this way) nothing moves
    [plain] = make_cards(window_rows(db, iso(NOW.replace(year=NOW.year - 1))), cfg, {})
    assert plain.lead.id == other and plain.final == 6.8 and plain.shift == 0


def test_a_level_only_counts_while_its_line_is_in_the_file(db, cfg):
    setup(db, cfg)
    set_level(db, WATCHES, -2)
    assert load_levels(db).shift_of(W) == -1.0
    write_profile("interests.md", "## Want\n- news\n")
    assert load_levels(db).shift_of(W) == 0.0


def test_a_level_change_starts_a_fresh_feed_session(db, cfg):
    setup(db, cfg)
    add(db, cfg, "a", "Nova ura Omega Speedmaster", "https://a.si/1", topic=5, importance=5, personal=5)
    sid, _ = feedsession.start(db, cfg, SOURCES)
    assert feedsession.resume(db, cfg, SOURCES, sid) is not None
    set_level(db, WATCHES, 1)
    assert feedsession.resume(db, cfg, SOURCES, sid) is None


def test_a_level_follows_a_suggested_reword_and_leaves_with_its_line(db, cfg):
    setup(db, cfg)
    set_level(db, WATCHES, -1)
    new = "Wristwatches: only notable releases"
    text = INTERESTS.replace(WATCHES, new)
    carry_interest_labels(db, [{"op": "reword", "section": "want", "old": WATCHES, "new": new}], text)
    write_profile("interests.md", text)
    assert load_levels(db).steps == {norm(new): -1}
    assert forget_removed(db, text) == []
    # removed (or reworded by hand): dropped, and the page is told what went
    assert forget_removed(db, "## Want\n- news\n") == [("Wristwatches", "Less")]
    assert db.execute("SELECT COUNT(*) FROM interest_levels").fetchone()[0] == 0


def test_a_turned_down_interest_is_never_the_revisit_story(db, cfg, monkeypatch):
    setup(db, cfg)
    cfg.feed.explore_every = 4
    for i in range(20):
        add(db, cfg, "abcde"[i % 5], f"lokalno {i} novica {i * 13}", f"https://x.si/l{i}", cat="Local",
            topic=9, importance=5, personal=7)
    watches = [add(db, cfg, "abcde"[i], f"ura {i} model {i * 7}", f"https://x.si/w{i}", cat="Lifestyle",
                   topic=7, importance=5, personal=7) for i in range(3)]
    for item_id in watches:
        about(db, item_id, WATCHES)
    lines = {norm(line): line for line in ("news", WATCHES, CARRY)}
    drift = Drift(active=True, weights={f"interest:{W}": 0.6}, lines=lines)
    monkeypatch.setattr("newsnest.feed.current_drift", lambda conn, cfg, now=None: drift)
    assert [it.card.lead.id in watches for it in rank_feed(db, cfg, SOURCES).items if it.extra == "revisit"] == [True]
    set_level(db, WATCHES, -1)
    assert not any(it.extra == "revisit" for it in rank_feed(db, cfg, SOURCES).items)


def test_daily_counts_follow_the_level(db, cfg):
    setup(db, cfg)
    for i in range(3):
        about(db, add(db, cfg, "abc"[i], f"ura {i} model {i * 7}", f"https://x.si/w{i}",
                      topic=5, importance=5, personal=5), WATCHES)
    rows = {r["label"]: r for r in level_rows(db, cfg)}
    assert list(rows) == ["news", "Wristwatches", "Everyday carry"]
    assert rows["Wristwatches"]["count"] == "Less than 1 a day" and rows["Wristwatches"]["step"] == 0
    assert rows["Everyday carry"]["count"] == "None in the last 7 days"
    set_level(db, WATCHES, -1)
    rows = {r["label"]: r for r in level_rows(db, cfg)}
    assert rows["Wristwatches"]["count"] == "None in the last 7 days" and rows["Wristwatches"]["step"] == -1


def test_missed_this_names_the_level(cfg):
    levels = Levels(steps={W: -1}, lines={W: WATCHES})
    row = {"prefilter": None, "score_status": "scored", "interest": W, "reason": "A new watch."}
    assert explain_known(row, 4.6, cfg, levels) == \
        "We had it, scored 4.6, −0.5 for your level on Wristwatches (below the 5.0 display threshold): A new watch."
    assert explain_known({**row, "interest": None}, 4.6, cfg, levels) == \
        "We had it, scored 4.6 (below the 5.0 display threshold): A new watch."


def test_profile_page_sets_a_level_and_notes_a_dropped_one(client):
    client.post("/profile/save/interests.md", data={"text": INTERESTS})
    page = client.get("/profile").text
    # each interest once: its words, its count and its level on one row
    assert page.count(f'<span class="srow-title wrap">{WATCHES}</span>') == 1 and "None in the last 7 days" in page
    r = client.post("/profile/level", data={"line": WATCHES, "step": "-1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/profile?saved=level#interests"
    page = client.get("/profile").text
    assert '<option value="-1" selected>Less</option>' in page and "level-set" in page
    assert client.post("/profile/level", data={"line": "Knitting", "step": "1"}).status_code == 404
    assert client.post("/profile/level", data={"line": WATCHES, "step": "3"}).status_code == 400
    # the text box (Advanced) can still drop a line, and the page says what went with it
    page = client.post("/profile/save/interests.md", data={"text": "## Want\n- news\n"}).text
    assert "Wristwatches is no longer in your list; its level (Less) was removed." in page
    assert f'<span class="srow-title wrap">{WATCHES}</span>' not in page


def test_lines_are_added_reworded_and_removed_in_place(client, env):
    conn = connect()
    (env.config / "interests.md").write_text(f"<!-- keep me -->\n## Want\n- news\n- {WATCHES}\n\n## Skip\n- Sports\n",
                                             encoding="utf-8")
    item = conn.execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    conn.execute("UPDATE items SET interest = ? WHERE id = ?", (W, item))
    client.post("/profile/level", data={"line": WATCHES, "step": "-1"})
    r = client.post("/profile/interest/add", data={"section": "want", "text": "  Everyday   carry "}, follow_redirects=False)
    assert r.headers["location"] == "/profile?saved=interest_add#interests"
    # reworded here, the interest keeps its level and its stories
    new = "Wristwatches: only notable releases"
    client.post("/profile/interest/edit", data={"section": "want", "old": WATCHES, "new": new})
    assert load_levels(conn).steps == {norm(new): -1}
    assert conn.execute("SELECT interest FROM items WHERE id = ?", (item,)).fetchone()[0] == norm(new)
    client.post("/profile/interest/add", data={"section": "skip", "text": "Horoscopes"})
    client.post("/profile/interest/remove", data={"section": "skip", "line": "Sports"})
    assert (env.config / "interests.md").read_text(encoding="utf-8") == (
        f"<!-- keep me -->\n## Want\n- news\n- {new}\n- Everyday carry\n\n## Skip\n- Horoscopes\n")
    # removing an interest takes its level with it
    client.post("/profile/interest/remove", data={"section": "want", "line": new})
    assert load_levels(conn).steps == {}
    page = client.post("/profile/interest/add", data={"section": "want", "text": "news"}).text
    assert "Already in your interests" in page
    # Edit wording opens that one line as a field, filled in
    page = client.get("/profile?edit=want-1").text
    assert page.count('class="line-edit" hidden') == 2  # the other lines stay as words
    assert 'name="new" value="Everyday carry" aria-label="Wording of Everyday carry" maxlength="200" required autofocus' in page


def test_why_panel_shows_the_level(client):
    client.post("/profile/save/interests.md", data={"text": INTERESTS})
    conn = connect()
    conn.execute("UPDATE items SET score_status='scored', topic=5, importance=5, personal=5, reason='r', "
                 "ai_category='Lifestyle', scored_at=?, interest=? WHERE url='https://24ur.com/b'", (iso(NOW), W))
    conn.close()
    client.post("/profile/level", data={"line": WATCHES, "step": "2"})
    page = client.get("/").text
    assert "Your level for Wristwatches" in page and "6.0" in page
