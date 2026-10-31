from datetime import datetime, timezone

from newsnest.config import Source
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


def test_foreign_local_flags_the_odd_one_out(conn, cfg):
    pf = Prefilter(cfg)
    sources = [Source(id=s, type="rss", category="news", name=s, url=f"https://{s}.example/rss") for s in ("a", "b", "en")]
    texts = {"a": SLOVENIAN[:12], "b": SLOVENIAN[12:24], "en": ENGLISH}
    for s in sources:
        ingest(conn, s, [Item(s.id, "news", t, f"https://{s.id}.example/{i}", f"{s.id}{i}", NOW)
                         for i, t in enumerate(texts[s.id])], pf, NOW)
    assert foreign_local(conn, sources) == {"en"}
    assert foreign_local(conn, sources[:1]) == set()  # one Local feed can't be judged
