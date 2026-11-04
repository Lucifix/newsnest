# Stable feed sessions

Status: phases 1 and 2 built (`newsnest/feedsession.py`, tests in `tests/test_feedsession.py`); phase 3 later.

## Problem

The home feed is a live query. Every view (page load, tab switch, Back, a reload after the phone dropped
the page) re-runs the full ranking against the database as it is at that moment, and much of that
database changes while you scan:

- opening a story marks it read, so it is excluded;
- a card on screen for a second counts as an impression: ×0.85 and the "unseen" boost is gone (about
  0.77× in all), so everything you scrolled past sinks below what you have not reached yet;
- the first open of a story nudges its outlet and category weights (`OPEN_NUDGE`);
- chip order follows the categories you open (`_affinity`);
- trial status of new sources depends on impressions (`_new_sources`);
- new articles arrive every 15–30 minutes, AI scores and event labels land after fetching;
- freshness decays with the clock.

The result: open an article, come back, and it is gone and the page is reordered; switch tabs and back,
and the order is different. A stopgap that rewinds each signal by timestamp would not hold (seen counts,
weights and affinity have no history), so the fix is structural.

## Design

The ranking is computed once per session, stored, and every view renders that stored ranking with the
current state of each story overlaid. This is how Discover-style feeds and RSS readers behave.

**A session starts** when:
- the device has no session cookie (first visit, cleared cookies), or the session is unknown;
- more than `feed.session_idle_minutes` (default 45) passed since the device last used it;
- the reader asks for it: tapping the tab they are on (`/?refresh=1`) or Mark all read;
- the config or source list changed (config fingerprint);
- the stored format is from another release (format version).

**Frozen in a session**: which stories are in the feed and their order, tab and chip membership, chip
order and counts, the "why this ranks here" lines, the extra (new source / exploring) slots, the lead
article of each card.

**Live**: read (dimmed, stays in place), saved, rating, hidden and "not interested" (removed at once),
summaries, new outlets joining a story (shown on the card, it does not move).

**Per device**: each browser or installed app has its own session cookie.

Signals (impressions, reads, opens, weights) are recorded exactly as before; they shape the next session.

## Phases

1. **Session store.** `feed_sessions` table (additive migration). `build_feed` splits into `rank_feed`
   (the scoring loop, all tabs) and `view_feed` (tabs and chips, no database); `build_feed` stays as
   `view_feed(rank_feed(...))`, so the swipe deck and the engine tests are unchanged. `/`, tab switches,
   Back and `/feed/more` read the session; Mark all read reads it and then ends it. The in-memory
   snapshot is retired. Kill switch: `feed.stable_sessions: false` builds a new session on every page
   load, which is the old behaviour.
2. **Refresh.** Needed because a reload, and the browser's own pull to refresh (a reload), now keep the
   order on purpose: without it, an installed app shows no new stories until the reader has been away
   for the idle time. Every way in asks for a new session (`/?refresh=1`, which redirects back to the
   same tab or chip):
   - **Pull to refresh** on the feed (touch): pull down from the very top past the mark and let go.
     Only at the top, only a vertical pull (a sideways swipe on the chips is not one), never with search,
     a sheet or the menu open. The browser's pull-to-reload is switched off on the feed page only
     (`overscroll-behavior-y: none`), so the two never both fire.
   - **Refresh** in the ⋯ menu: on the feed a new session, on every other page a reload as before. The
     installed app's separate top bar Refresh button is gone: with pull to refresh it was one control too
     many, and the menu item covers every page.
   - **Tap the tab you are on**, as in most feed apps; from a part or chip inside the tab the first tap
     goes back to its All.
3. **"N new stories" pill** (later): stories fetched after the session started that would make the feed;
   a top story is named, so breaking news is not hidden until the next session.

## Risks and how they are covered

| Risk | Mitigation |
|---|---|
| Event labels fold two cards into one mid-session | Dedupe at render: the earlier position keeps the card |
| A source removed, sections edited | Config fingerprint → new session; missing items are skipped |
| A release changes the stored shape | Format version → new session, never a crash |
| Rollback to an older image | The old code ignores the table and the cookie; `migrate()` accepts a newer database. Do not set `stable_sessions` / `session_idle_minutes` in config.yaml before rolling back (older releases reject unknown keys) |
| Database growth | Sessions unused for 24 h are deleted, at most 20 kept |
| A write per feed request (`last_used_at`) | SQLite WAL, one worker; `/api/seen` already writes |
| Someone prefers immediate removal | Kill switch, tap the tab |

Trade-off: new stories wait for the next session or a tap on the tab (phase 3 handles breaking news). Reading a
single article for longer than the idle time brings back a fresh ranking.

## Verification

- Existing tests stay green; web tests whose meaning changes on purpose are listed in the PR.
- New tests: reload after opening keeps the order with the story dimmed; tab round trip is identical;
  impressions do not reorder within a session; after the idle time the signals apply; not interested
  removes at once; event-merge dedupe; removed source; config change; format mismatch; Mark all read
  empties the feed; kill switch off = old behaviour; migration on an existing database.
- Against a copy of a real database: the session ranking equals `build_feed` at the same moment; timing.
- Playwright at phone width, light and dark: open a card, reload, switch tabs, Back.
