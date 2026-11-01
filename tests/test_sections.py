from datetime import datetime, timezone

import pytest

from newsnest.config import SectionCfg, Source
from newsnest.db import iso
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.sections import foreign_local, looks_local, vocabulary
from newsnest.watches import Prefilter

NOW = datetime.now(timezone.utc)

SLOVENIAN = [
    "Vlada je na seji sprejela nov zakon o plačah, ki bo veljal tudi za javni sektor",
    "V Ljubljani so zaradi del na cesti zaprli most, kot so sporočili iz občine",
    "Pri nesreči na avtocesti je bil en voznik huje poškodovan, policija pa še preiskuje",
    "Cene elektrike se bodo po novem letu zvišale, kar bo prizadelo tudi gospodinjstva",
    "Ministrstvo je za šole pripravilo nova pravila, ki pa jih sindikat ne podpira",
    "Na volitvah bo po anketi zmagala opozicija, ki je tudi v parlamentu najmočnejša",
    "Iz bolnišnice so sporočili, da je stanje pacientov po operaciji stabilno",
    "Kako bo inflacija vplivala na pokojnine, ki so se letos že zvišale",
    "Policija je pri kontroli zasegla drogo, voznik pa je bil pridržan",
    "Zakaj je na trgu nepremičnin vse več kupcev, ki iščejo stanovanje za najem",
    "Župan je na novinarski konferenci predstavil načrt za nove kolesarske steze",
    "Po poplavah je v občini še vedno veliko škode, ki jo ocenjujejo strokovnjaki",
] * 3

GERMAN = [
    "Die Regierung hat ein neues Gesetz für die Renten beschlossen, das auch für Beamte gilt",
    "In Berlin wird die Brücke wegen Bauarbeiten gesperrt, wie die Stadt mitteilte",
    "Bei einem Unfall auf der Autobahn wurde ein Fahrer schwer verletzt, die Polizei ermittelt",
    "Die Strompreise steigen nach dem Jahreswechsel, das trifft auch die Haushalte",
    "Das Ministerium hat für die Schulen neue Regeln vorgestellt, die Gewerkschaft ist dagegen",
    "Nach der Umfrage wird die Opposition die Wahl gewinnen, sie ist auch im Bundestag stark",
    "Das Krankenhaus teilte mit, dass der Zustand der Patienten nach der Operation stabil ist",
    "Wie wirkt sich die Inflation auf die Renten aus, die in diesem Jahr schon gestiegen sind",
] * 3

ENGLISH = [
    "The government has passed a new pension law that will also apply to civil servants",
    "A bridge in the city is closed for construction work, the council said on Monday",
    "A driver was seriously injured in a crash on the motorway and police are investigating",
    "Electricity prices will rise after the new year, which will also hit households",
    "The ministry has presented new rules for schools, but the union does not support them",
] * 2


def test_looks_local_learns_the_language_from_the_other_feeds():
    sl, de = vocabulary(SLOVENIAN), vocabulary(GERMAN)
    assert looks_local(SLOVENIAN[:10], None, sl) is True
    assert looks_local(ENGLISH, None, sl) is False
    assert looks_local(GERMAN[:8], None, de) is True
    assert looks_local(ENGLISH, None, de) is False
    assert looks_local(ENGLISH, None, set()) is None          # nothing to compare with
    assert looks_local(ENGLISH[:2], None, sl) is None         # too little to go on
    assert looks_local(ENGLISH, "https://www.rtvslo.si/rss", sl, ".si") is True  # the region's domain


def test_languages_without_spaces_are_left_alone():
    japanese = ["政府は新しい年金法を可決した", "市内の橋が工事のため通行止めになった", "高速道路で事故があり運転手が重傷"] * 10
    assert len(vocabulary(japanese)) < 15
    assert looks_local(ENGLISH, None, vocabulary(japanese)) is None


def load_sections(env, block: str | None):
    """Load the example config with its `sections:` list replaced by `block` (None: removed, like an old config)."""
    import re

    from newsnest.config import load_config
    path = env.config / "config.yaml"
    text = re.sub(r"^sections:\n(?:  - .*\n)+", block or "", path.read_text(encoding="utf-8"), flags=re.M)
    path.write_text(text, encoding="utf-8")
    return load_config(env.config)


def test_the_example_config_starts_with_local_world_and_tech(env):
    from newsnest.config import load_config
    from newsnest.sections import dock_tabs, section_keys, section_labels
    cfg = load_config(env.config).config
    assert section_keys(cfg) == ("news", "world", "tech")
    assert section_labels(cfg) == {"news": "Local", "world": "World", "tech": "Tech"}
    assert [(t.key, t.label) for t in dock_tabs(cfg)] == [("headlines", "News"), ("tech", "Tech")]


def test_a_config_without_sections_has_the_four_it_always_had(env):
    from newsnest.sections import dock_tabs, section_keys
    loaded = load_sections(env, None)
    assert not loaded.errors and loaded.config.sections is None
    assert section_keys(loaded.config) == ("tech", "news", "world", "lifestyle")
    # Lifestyle only has a tab once it has a feed, the others have theirs from the example's feeds
    assert [t.key for t in dock_tabs(loaded.config)] == ["tech", "headlines"]
    assert [t.key for t in dock_tabs(loaded.config, current="lifestyle")] == ["tech", "headlines", "lifestyle"]
    assert [t.key for t in dock_tabs(None)] == ["tech", "headlines", "lifestyle"]   # no valid config: every tab is offered


def test_sections_are_validated(env):
    def errors(block):
        return " ".join(load_sections(env, block).errors)
    assert "duplicate key" in errors("sections:\n  - {key: tech, label: A}\n  - {key: tech, label: B}\n")
    assert "must be 1-24" in errors("sections:\n  - {key: All, label: A}\n")
    assert "must be 1-24" in errors("sections:\n  - {key: headlines, label: A}\n")
    assert "unknown icon" in errors("sections:\n  - {key: tech, label: Tech, icon: rocket}\n")
    assert "1-24 characters" in errors("sections:\n  - {key: tech, label: ''}\n")
    # every feed has to be in a section that exists
    assert "feed 'theverge' is in section 'tech', which isn't listed under sections:" in errors("sections:\n  - {key: sports, label: Sports}\n")


def test_local_and_world_come_back_when_left_out(env):
    from newsnest.sections import section_keys
    cfg = load_sections(env, "sections:\n  - {key: tech, label: Tech}\n").config
    assert section_keys(cfg) == ("news", "world", "tech") and cfg.all_sections[0].label == "Local"


def test_at_most_four_tabs_and_news_is_one(env):
    from newsnest.config import Config
    assert Config.model_validate({"sections": [{"key": f"s{i}", "label": f"S{i}"} for i in range(3)]}).sections
    with pytest.raises(ValueError, match="at most 4 tabs"):
        Config.model_validate({"sections": [{"key": f"s{i}", "label": f"S{i}"} for i in range(4)]})
    assert Config.model_validate({"sections": [{"key": f"s{i}", "label": f"S{i}", "tab": i < 3} for i in range(8)]})
    with pytest.raises(ValueError, match="at most 12"):
        Config.model_validate({"sections": [{"key": f"s{i}", "label": f"S{i}", "tab": False} for i in range(11)]})


def test_a_section_with_its_own_poll_interval(env):
    from newsnest.config import load_config
    cfg = load_config(env.config).config
    assert (cfg.poll_minutes("news"), cfg.poll_minutes("world"), cfg.poll_minutes("tech"), cfg.poll_minutes("sports")) == (15, 20, 30, 30)
    cfg = load_sections(env, "sections:\n  - {key: news, label: Local, poll_minutes: 5}\n  - {key: tech, label: Tech}\n").config
    assert (cfg.poll_minutes("news"), cfg.poll_minutes("tech")) == (5, 30)


def test_tab_is_hidden_until_the_section_has_an_enabled_feed(env):
    from newsnest.sections import dock_tabs
    cfg = load_sections(env, None).config
    for f in cfg.feeds:
        f.enabled = f.category != "tech"
    assert [t.key for t in dock_tabs(cfg)] == ["tech", "headlines"]   # the GitHub release feeds are Tech too
    cfg.github_releases.repos = []
    assert [t.key for t in dock_tabs(cfg)] == ["headlines"]
    cfg.sections = [SectionCfg(key="news", label="Local"), SectionCfg(key="world", label="World"),
                    SectionCfg(key="tech", label="Tech", tab=False)]
    for f in cfg.feeds:
        f.enabled = True
    assert [t.key for t in dock_tabs(cfg)] == ["headlines"]   # tab: false


def test_move_section_takes_stored_stories_and_trial_feeds_along(conn, cfg):
    from newsnest.sections import move_section
    pf = Prefilter(cfg)
    gone = Source(id="old", type="rss", category="sports", name="Old", url="https://old.example/rss")
    ingest(conn, gone, [Item("old", "sports", "Final tonight", "https://old.example/1", "o1", NOW)], pf, NOW)
    conn.execute("INSERT INTO candidates (source_id, url, title, category, origin, status, added_at) "
                 "VALUES ('disc-x', 'https://x.example/rss', 'X', 'sports', 'test', 'candidate', ?)", (iso(NOW),))
    assert move_section(conn, "sports", "world") == 1
    assert [r[0] for r in conn.execute("SELECT category FROM items")] == ["world"]
    assert [r[0] for r in conn.execute("SELECT category FROM stories")] == ["world"]
    assert conn.execute("SELECT category FROM candidates").fetchone()[0] == "world"


def test_foreign_local_flags_the_odd_one_out(conn, cfg):
    pf = Prefilter(cfg)
    sources = [Source(id=s, type="rss", category="news", name=s, url=f"https://{s}.example/rss") for s in ("a", "b", "en")]
    texts = {"a": SLOVENIAN[:12], "b": SLOVENIAN[12:24], "en": ENGLISH}
    for s in sources:
        ingest(conn, s, [Item(s.id, "news", t, f"https://{s.id}.example/{i}", f"{s.id}{i}", NOW)
                         for i, t in enumerate(texts[s.id])], pf, NOW)
    assert foreign_local(conn, sources) == {"en"}
    assert foreign_local(conn, sources[:1]) == set()  # one Local feed can't be judged
