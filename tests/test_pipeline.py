from datetime import datetime, timedelta, timezone

from newsnest.config import Source
from newsnest.db import iso
from newsnest.models import FetchResult, FetchStatus, Item
from newsnest.pipeline import ingest, record_run, source_health
from newsnest.textmatch import keyword_pattern, matches, normalize_url, title_tokens
from newsnest.watches import Prefilter

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


def item(source, title, url, snippet="", pub=None):
    return Item(source_id=source, category="news", title=title, url=url, guid=url, published=pub or NOW, snippet=snippet)


def src(sid, category="news"):
    return Source(id=sid, type="rss", category=category, name=sid, url=f"https://{sid}.example/rss")


def test_normalize_url_strips_tracking_and_www():
    assert normalize_url("http://www.Monitor.si/novica/x/252005/?utm_source=feed&utm_medium=rss#top") == \
        "https://monitor.si/novica/x/252005"
    assert normalize_url("https://a.si/p?b=2&a=1") == normalize_url("https://a.si/p/?a=1&b=2")


def test_keyword_pattern_matches_stems_and_diacritics():
    p = keyword_pattern(["ljubljan", "zurnal"])
    assert matches(p, "Nova parkirna mesta v Ljubljani")
    assert matches(p, "Žurnal poroča")
    assert not matches(p, "Maribor dobil nov most")


def test_title_tokens_keeps_every_word_and_leaves_filler_to_the_index():
    assert {"the", "munich"} <= title_tokens("The Munich-based Müller's 42 plans")
    assert {"42"} <= title_tokens("The Munich-based Müller's 42 plans") and "a" not in title_tokens("a b 7")


def test_title_tokens_fold_inflections():
    assert {"ljublj", "parkir"} <= title_tokens("Ljubljana uvaja nove parkirne cone")
    assert "ljublj" in title_tokens("V Ljubljani zaprli cesto")


def test_ingest_dedupes_by_normalized_url(conn, cfg):
    pf = Prefilter(cfg)
    a = item("rtvslo", "Papežu Leonu XIV. se rak ni razširil", "https://www.rtvslo.si/a?utm_source=x")
    assert len(ingest(conn, src("rtvslo"), [a], pf, NOW)) == 1
    again = item("rtvslo", "Papežu Leonu XIV. se rak ni razširil", "https://rtvslo.si/a")
    assert ingest(conn, src("rtvslo"), [again], pf, NOW) == []


def test_same_story_across_sources_is_grouped(conn, cfg):
    pf = Prefilter(cfg)
    ingest(conn, src("24ur"), [item("24ur", "Papežu Leonu XIV. se rak ni razširil", "https://24ur.com/1")], pf, NOW)
    ingest(conn, src("n1"), [item("n1", "Papežu se rak ni razširil", "https://n1info.si/1")], pf, NOW)
    ingest(conn, src("delo"), [item("delo", "Maribor dobil nov most čez Dravo", "https://delo.si/1")], pf, NOW)
    rows = conn.execute("SELECT source_id, story_id FROM items ORDER BY id").fetchall()
    assert rows[0]["story_id"] == rows[1]["story_id"] != rows[2]["story_id"]
    assert conn.execute("SELECT source_count FROM stories WHERE id = ?", (rows[0]["story_id"],)).fetchone()[0] == 2


def test_same_outlet_follow_up_joins_but_a_parallel_angle_does_not(conn, cfg):
    pf = Prefilter(cfg)
    t0 = NOW - timedelta(hours=8)
    ingest(conn, src("24ur"), [item("24ur", "Vlada sprejela rebalans proračuna za leto 2027", "https://24ur.com/1", pub=t0)], pf, NOW)
    # a different angle from the same outlet within minutes stays separate...
    ingest(conn, src("24ur"), [item("24ur", "Rebalans proračuna: kaj prinaša upokojencem", "https://24ur.com/2", pub=t0 + timedelta(minutes=10))], pf, NOW)
    assert conn.execute("SELECT COUNT(DISTINCT story_id) FROM items").fetchone()[0] == 2
    # ...a clear follow-up hours later joins the story
    ingest(conn, src("24ur"), [item("24ur", "Vlada sprejela rebalans proračuna za leto 2027, opozicija kritična", "https://24ur.com/3", pub=t0 + timedelta(hours=5))], pf, NOW)
    stories = [r[0] for r in conn.execute("SELECT story_id FROM items ORDER BY id")]
    assert stories[2] == stories[0] != stories[1]


def test_news_and_tech_items_join_one_story(conn, cfg):
    pf = Prefilter(cfg)
    ingest(conn, src("rtvslo"), [item("rtvslo", "Kritična ranljivost PHP2Shell prizadela milijone spletnih strani", "https://rtv.si/1")], pf, NOW)
    tech = Source(id="monitor", type="rss", category="tech", name="Monitor", url="https://monitor.si/rss")
    ingest(conn, tech, [Item("monitor", "tech", "PHP2Shell: ranljivost prizadela milijone strani, posodobite PHP", "https://monitor.si/1", "m", NOW)], pf, NOW)
    assert conn.execute("SELECT COUNT(DISTINCT story_id) FROM items").fetchone()[0] == 1


def test_a_rare_shared_word_matters_more_than_common_ones(conn, cfg):
    pf = Prefilter(cfg)
    ingest(conn, src("a"), [item("a", "Ustavno sodišče razveljavilo del zakona o božičnici", "https://a.si/1")], pf, NOW)
    ingest(conn, src("b"), [item("b", "Ustavno sodišče sprejelo pritožbo glede ugrabitve RTV", "https://b.si/1")], pf, NOW)
    assert conn.execute("SELECT COUNT(DISTINCT story_id) FROM items").fetchone()[0] == 2  # two ordinary words: not enough
    ingest(conn, src("c"), [item("c", "Boris Mijič zaprosil za nadomestilo plače", "https://c.si/1")], pf, NOW)
    ingest(conn, src("d"), [item("d", "Mijič res zaprosil za nadomestilo, koliko bo prejel", "https://d.si/1")], pf, NOW)
    rows = conn.execute("SELECT source_id, story_id FROM items").fetchall()
    assert dict(rows)["c"] == dict(rows)["d"] and dict(rows)["c"] != dict(rows)["a"]


def test_regroup_merges_existing_items(conn, cfg):
    from newsnest.grouping import regroup
    pf = Prefilter(cfg)
    ingest(conn, src("a"), [item("a", "Slavko Moharič ne bo nadomestni poslanec Resnice", "https://a.si/1")], pf, NOW)
    ingest(conn, src("b"), [item("b", "Moharič ne bo poslanec Resnice, ostaja v lokalni politiki", "https://b.si/1")], pf, NOW)
    # pretend the old matcher kept them apart
    for i in (1001, 1002):
        conn.execute("INSERT INTO stories (id, category, first_seen, last_seen) VALUES (?, 'news', ?, ?)", (i, iso(NOW), iso(NOW)))
    conn.execute("UPDATE items SET story_id = id + 1000")
    conn.execute("DELETE FROM stories WHERE id < 1000")
    out = regroup(conn, 72, NOW)
    assert out["stories_before"] == 2 and out["stories_after"] == 1
    assert conn.execute("SELECT source_count FROM stories").fetchone()[0] == 2


GERMAN_EVENT = [
    "Bundesregierung beschließt Rentenreform nach langem Streit",
    "Rentenreform: Bundesregierung einigt sich nach langem Streit",
    "Nach langem Streit beschließt die Bundesregierung die Rentenreform",
    "Koalition einigt sich auf Rentenreform, Streit beigelegt",
    "Rentenreform beschlossen: Bundesregierung beendet langen Streit",
]
GERMAN_OTHER = [
    "Hochwasser in Passau: Pegel steigt weiter an",
    "Bayern München gewinnt Heimspiel gegen Leverkusen",
    "Neue Studie zeigt: Kaffee senkt das Risiko für Diabetes",
    "Deutsche Bahn kündigt Fahrplanwechsel im Dezember an",
    "Wetterdienst warnt vor Glatteis im Norden",
    "Tesla eröffnet neue Ladestation in Hamburg",
    "Bürgermeister von Köln eröffnet Weihnachtsmarkt",
    "Polizei findet vermisste Wanderin in den Alpen",
    "Inflation im Euroraum sinkt auf zwei Prozent",
    "Berliner Philharmoniker kündigen Tournee durch Asien an",
    "Lufthansa streicht hundert Flüge wegen Streik",
    "Forscher entdecken neue Pilzart im Schwarzwald",
    "Mieten in München steigen erneut deutlich",
    "Fußballverband sperrt Trainer nach Beleidigung",
    "Apothekerverband fordert höhere Honorare",
    "Schüler aus Leipzig gewinnen Robotik-Wettbewerb",
    "Autobahn A7 nach Unfall stundenlang gesperrt",
    "Museum in Dresden zeigt seltene Gemälde aus Italien",
    "Hersteller ruft Kinderspielzeug wegen Verletzungsgefahr zurück",
    "Landwirte protestieren mit Traktoren vor dem Rathaus",
]


def test_language_without_word_lists_groups_one_event_and_nothing_else(conn, cfg):
    pf = Prefilter(cfg)
    # interleave so the event headlines are spread among the unrelated ones
    titles = [(f"s{i}", t) for i, t in enumerate(GERMAN_OTHER[:10])]
    for i, t in enumerate(GERMAN_EVENT):
        titles.append((f"e{i}", t))
    titles += [(f"s{i + 10}", t) for i, t in enumerate(GERMAN_OTHER[10:])]
    for n, (sid, t) in enumerate(titles):
        ingest(conn, src(sid), [item(sid, t, f"https://{sid}.example/{n}")], pf, NOW)
    rows = conn.execute("SELECT source_id, story_id FROM items").fetchall()
    event = {r["story_id"] for r in rows if r["source_id"].startswith("e")}
    assert len(event) == 1
    assert sum(r["story_id"] in event for r in rows) == 5
    assert len({r["story_id"] for r in rows}) == 1 + len(GERMAN_OTHER)


def test_a_stem_in_many_headlines_is_filler_not_a_shared_word():
    from newsnest.grouping import COMMON_SHARE, CORPUS_FLOOR, StoryIndex
    limit = int(COMMON_SHARE * CORPUS_FLOOR)

    def build(n_filler):
        index = StoryIndex()
        index.add(1, "a", 0.0, frozenset({"regier", "alphax", "betay"}))
        for i in range(n_filler):
            index.add(2 + i, "f", 0.0, frozenset({"regier", f"uniq{i}a", f"uniq{i}b"}))
        return index

    query = frozenset({"regier", "alphax", "betay"})
    # while "regier" is still rare enough it adds weight and the two headlines merge...
    assert build(5).match(query, "b", 0.0) == 1
    # ...once it is in more than COMMON_SHARE of the headlines only the two other words count: not enough
    index = build(limit + 5)
    assert index.is_common("regier") and not index.is_common("alphax")
    assert index.match(query, "b", 0.0) is None


def test_prefilter_watch_beats_mute(conn, cfg):
    from newsnest.config import WatchCfg
    cfg.watches = [WatchCfg(name="Ljubljana", keywords=["ljubljan"])]
    cfg.prefilter.mute_keywords = ["horoskop", "ljubljan"]
    pf = Prefilter(cfg)
    ingest(conn, src("delo"), [item("delo", "Dnevni horoskop za četrtek", "https://delo.si/h"),
                               item("delo", "Ljubljana zapira Slovensko cesto", "https://delo.si/l")], pf, NOW)
    rows = {r["title"][:5]: r for r in conn.execute("SELECT * FROM items")}
    assert rows["Dnevn"]["prefilter"] == "muted" and rows["Dnevn"]["score_status"] == "muted"
    assert rows["Ljubl"]["prefilter"] == "must_show" and "Ljubljana" in rows["Ljubl"]["watches"]


def _state(conn, sid):
    return conn.execute("SELECT * FROM source_state WHERE source_id = ?", (sid,)).fetchone()


def test_heartbeat_uses_raw_entries_not_filtered_items(conn, cfg):
    s = src("gh-jellyfin", "tech")
    # A release feed whose entries are all too old to keep (0 items) still has entries: healthy
    for h in range(6):
        record_run(conn, s, FetchResult(s.id, FetchStatus.OK, raw_count=10), 0, cfg, NOW + timedelta(hours=h))
    assert source_health(s, _state(conn, s.id), cfg, NOW + timedelta(hours=5)).level == "ok"


def test_heartbeat_flags_a_stalled_scheduler(conn, cfg):
    s = src("rtvslo")
    record_run(conn, s, FetchResult(s.id, FetchStatus.OK, raw_count=20), 3, cfg, NOW)
    h = source_health(s, _state(conn, s.id), cfg, NOW + timedelta(hours=2))
    assert h.level == "failing" and "not fetched since" in h.message


def test_heartbeat_empty_feed_for_3h_is_failing(conn, cfg):
    s = src("rtvslo")
    record_run(conn, s, FetchResult(s.id, FetchStatus.OK, raw_count=20), 5, cfg, NOW)
    for h in (1, 2, 3, 4):
        record_run(conn, s, FetchResult(s.id, FetchStatus.EMPTY, raw_count=0), 0, cfg, NOW + timedelta(hours=h))
    h = source_health(s, _state(conn, s.id), cfg, NOW + timedelta(hours=4))
    assert h.level == "failing" and "0 entries" in h.message


def test_heartbeat_two_failures_in_a_row(conn, cfg):
    s = src("delo")
    fail = FetchResult(s.id, FetchStatus.PARSE_ERROR, error="not a valid feed", raw_count=0)
    record_run(conn, s, fail, 0, cfg, NOW)
    assert source_health(s, _state(conn, s.id), cfg, NOW).level == "warning"
    record_run(conn, s, fail, 0, cfg, NOW + timedelta(minutes=15))
    assert source_health(s, _state(conn, s.id), cfg, NOW + timedelta(minutes=15)).level == "failing"
    record_run(conn, s, FetchResult(s.id, FetchStatus.OK, raw_count=5), 1, cfg, NOW + timedelta(minutes=30))
    assert source_health(s, _state(conn, s.id), cfg, NOW + timedelta(minutes=30)).level == "ok"


def test_blocked_feed_pauses_one_hour(conn, cfg):
    s = src("blocked")
    record_run(conn, s, FetchResult(s.id, FetchStatus.BLOCKED, error="HTTP 403"), 0, cfg, NOW)
    assert _state(conn, s.id)["blocked_until"] == iso(NOW + timedelta(hours=1))


def test_entry_image_prefers_media_and_skips_http():
    from newsnest.fetchers.rss import entry_image
    assert entry_image({"media_content": [{"url": "https://a.si/small.jpg", "width": "200"},
                                          {"url": "https://a.si/big.jpg", "width": "1200"}]}) == "https://a.si/big.jpg"
    assert entry_image({"enclosures": [{"href": "https://a.si/e.jpg", "type": "image/jpeg"}]}) == "https://a.si/e.jpg"
    assert entry_image({"summary": '<p><img src="http://a.si/x.jpg"><img src="https://a.si/y.jpg"></p>'}) == "https://a.si/y.jpg"
    assert entry_image({"summary": "no image"}) is None


def test_known_item_gets_image_backfilled(conn, cfg):
    pf = Prefilter(cfg)
    a = item("rtvslo", "Papežu Leonu XIV. se rak ni razširil", "https://rtvslo.si/a")
    ingest(conn, src("rtvslo"), [a], pf, NOW)
    a.extra = {"image": "https://img.rtvcdn.si/a.jpg"}
    assert ingest(conn, src("rtvslo"), [a], pf, NOW) == []
    assert "img.rtvcdn.si" in conn.execute("SELECT extra FROM items").fetchone()[0]
