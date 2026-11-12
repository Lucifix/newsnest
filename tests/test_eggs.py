from datetime import datetime

from fastapi.testclient import TestClient

from newsnest import eggs


def test_the_extra_line_is_rare_and_holds_still_for_a_session():
    seeds = [f"session-{i}" for i in range(2000)]
    shown = [eggs.caughtup_aside(s, empty=False) for s in seeds]
    hits = [line for line in shown if line]
    assert 0.15 < len(hits) / len(seeds) < 0.25          # about one session in five
    assert set(hits) == set(eggs.END)                    # every line gets its turn
    assert shown == [eggs.caughtup_aside(s, empty=False) for s in seeds]  # same session, same answer
    assert {eggs.caughtup_aside(s, empty=True) for s in seeds} == {None, *eggs.EMPTY}
    assert eggs.caughtup_aside("", empty=True) is None and eggs.caughtup_aside(None, empty=False) is None


def test_lines_about_the_hour_need_a_known_clock_and_an_empty_feed():
    seeds = [f"session-{i}" for i in range(400)]
    night, early, noon = datetime(2026, 11, 12, 3, 10), datetime(2026, 11, 12, 6, 0), datetime(2026, 11, 12, 12, 0)
    at_night = [eggs.caughtup_aside(s, True, night) for s in seeds]
    assert 0.4 < at_night.count(eggs.NIGHT) / len(seeds) < 0.6
    assert eggs.EARLY in {eggs.caughtup_aside(s, True, early) for s in seeds}
    for line in (eggs.NIGHT, eggs.EARLY):
        assert line not in {eggs.caughtup_aside(s, True, noon) for s in seeds}    # daytime
        assert line not in {eggs.caughtup_aside(s, False, night) for s in seeds}  # stories still unread
        assert line not in {eggs.caughtup_aside(s, True) for s in seeds}          # no clock


def test_lines_fit_a_phone_without_wrapping_twice():
    for line in (*eggs.END, *eggs.EMPTY, eggs.NIGHT, eggs.EARLY):
        assert len(line) <= 38 and line.endswith(".")


def test_feed_end_keeps_its_heading_and_adds_the_line_only_when_picked(env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    with TestClient(web.app) as client:
        monkeypatch.setattr(eggs, "ONE_IN", 10**9)   # never
        page = client.get("/").text
        assert "You're all caught up" in page and "caughtup-aside" not in page
        monkeypatch.setattr(eggs, "ONE_IN", 1)       # always
        page = client.get("/").text
        assert "You're all caught up" in page
        line = page.split('<p class="caughtup-aside">')[1].split("</p>")[0]
        # config.example's time zone is UTC, so the hour is never used
        assert line in eggs.EMPTY
        assert client.get("/").text.split('<p class="caughtup-aside">')[1].split("</p>")[0] == line  # a reload keeps it


def test_about_logo_is_drawn_in_the_page_and_says_nothing_until_tapped(env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    with TestClient(web.app) as client:
        page = client.get("/settings/about").text
        assert "data-egg" in page and 'class="egg-dot"' in page
        assert '<p class="about-egg" data-egg-line role="status"></p>' in page  # the line arrives with the bird
