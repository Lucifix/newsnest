# NewsNest localization: remove the Slovenia hardcoding

## Context

NewsNest (`github.com/Lucifix/newsnest`, Python 3.12, FastAPI + Jinja + SQLite) is about to go public. Today it
assumes every user lives in Ljubljana, Slovenia: the starter config ships Slovenian feeds, Claude's prompts say
"a reader who lives in Ljubljana, Slovenia", the "Local" section only accepts Slovenian-language feeds (a
hand-written Slovenian word detector plus a `.si` domain rule), headline grouping uses a hand-written
Slovenian + English stopword list, and the default time zone is `Europe/Ljubljana`.

Goal: a user anywhere (Germany, US, Poland, Japan…) can install it and get a version that fits where they
live, with no code changes and no word lists. Slovenia becomes one optional "local news pack". The owner's
own install keeps working exactly as before once they enter Slovenia/Ljubljana as their region.

### Decisions already made (do not reopen)

1. **Region is a setting** in `config/config.yaml` (`region: {country, city, tld}` + the existing
   `timezone`), edited from the web UI. The Settings page already writes config.yaml through
   `newsnest/config_edit.py::edit_config` (keeps comments, validates, writes `.bak`); reuse it.
2. **Stopwords are learned** from the user's own headlines (frequency), not from per-language lists. A small
   built-in English list stays as a seed. No `languages.py` registry.
3. **The Local language check only warns.** Nothing is ever moved to World automatically any more. The Feeds
   page flags a Local feed whose headlines don't look like the user's other Local feeds; the user moves it
   with the existing section select.
4. **"Your local language" is learned** from the feeds already filed under Local (their most frequent
   words). No language codes anywhere.
5. **Packs ship for Slovenia, Germany, United States, United Kingdom.**
6. **Region is entered in two places:** Settings → General, and a first step of the onboarding page
   (`/profile` while no profile exists), which then offers the matching pack.
7. The backup .zip idea is **out of scope** (later task).

### Terms

- **Local** = section key `"news"` (label "Local"); **World** = `"world"`. See `newsnest/sections.py`.
- **Pack** = curated list of feeds added in one tap. Existing example: `LIFESTYLE_PACK` in
  `newsnest/packs.py` with `missing_from()` / `add_pack()` and route `POST /settings/pack/lifestyle` in
  `newsnest/web.py`.
- **Tokens** = folded 6-char stems of a headline from `textmatch.title_tokens()`, stored as JSON in
  `items.tokens`, used only by headline grouping (`newsnest/grouping.py::StoryIndex`).

## Work packages

Run in this order. WP2 and WP7 can run in parallel with anything after WP1. **WP3 and WP5 both edit
`newsnest/web.py`, `templates/_settings_ui.html` and `templates/settings_feeds.html`: never run them at the
same time.** WP6 changes `config.example/`, which every test copies (`tests/conftest.py::env`), so run it
last and fix tests in the same package.

---

### WP1: Region config and pack structure (foundation, do first)

**`newsnest/config.py`**
- Add:
  ```python
  class RegionCfg(_Strict):
      country: str = ""   # "Slovenia"; empty = no local focus, prompts stay neutral
      city: str = ""      # "Ljubljana" (optional)
      tld: str = ""       # ".si": a feed on this domain always counts as local (optional)

      @property
      def place(self) -> str:
          """"Ljubljana, Slovenia", "Slovenia", or "" when unset."""
  ```
  `tld` is normalised to lowercase with a leading dot (validator); empty stays empty.
- `Config.region: RegionCfg = Field(default_factory=RegionCfg)`.
- `Config.timezone` default `"Europe/Ljubljana"` → `"UTC"`. Add a validator that rejects unknown zones
  (`zoneinfo.ZoneInfo(name)` raises → `ValueError("unknown time zone …")`), so the Settings form gets a clear
  error via the existing `ConfigEditError/ValueError` redirect in `settings_action`.
- `ScoringCfg.categories` default: replace `"Ljubljana", "Slovenia"` with `"Local", "National"`.
- `WatchCfg` comment: replace the `ljubljan` example with `"electr" matches electric, electricity`.

**`newsnest/web.py`** line ~238: fallback `ZoneInfo("Europe/Ljubljana")` → `ZoneInfo("UTC")`.
**`docker-compose.yml`**: `TZ=${TZ:-Europe/Ljubljana}` → `TZ=${TZ:-UTC}`.

**`newsnest/packs.py`**: add the local-pack structure (data for SI here; DE/US/UK come in WP7):
```python
@dataclass(frozen=True)
class PackFeed:
    name: str
    url: str
    daily_cap: int | None = None
    category: str | None = None   # NEW: overrides the pack's section (e.g. a country's tech sites)

@dataclass(frozen=True)
class LocalPack:
    code: str          # "si"
    country: str       # "Slovenia" (matched case-insensitively against region.country)
    timezone: str      # suggested zone, "Europe/Ljubljana"
    tld: str           # ".si", or "" (US: "")
    feeds: list[PackFeed]

LOCAL_PACKS: dict[str, LocalPack]          # code -> pack
def pack_for(country: str) -> LocalPack | None   # by country name or code, case-insensitive
```
- `add_pack()`: use `p.category or category` for each feed, both for adding and for the "move into the
  pack's section" loop.
- Slovenia pack: move every feed under `# --- Slovenian news ---` and `# --- Slovenian tech ---` in
  `config.example/config.yaml` into `LOCAL_PACKS["si"]`, keeping names, URLs and `daily_cap`s; the three
  tech sites get `category="tech"`. Do **not** remove them from config.example yet (WP6 does that).

**Done when:** `pytest` passes; a config without `region:` loads; a config with
`region: {country: Slovenia, city: Ljubljana, tld: .si}` loads; `timezone: Mars/Base` is rejected.

---

### WP2: Learned stopwords for grouping

**`newsnest/textmatch.py`**
- Rename `_STOPWORDS` → `SEED_STOPWORDS` and keep **only the English lines** (the last three lines of the
  current set). Delete the Slovenian lines.
- `title_tokens(title)` stops filtering with the stopword set: it returns every folded word of length ≥ 3
  (cut to 6 chars) and every number of length ≥ 2, exactly as today minus the stopword check.
  Update its docstring to a neutral example (keep the point that prefix stems fold inflections).
- Fix the `keyword_pattern` docstring ("so stems catch every inflection", no "Slovenian").

**`newsnest/grouping.py`**
- Filtering moves to match time, inside `StoryIndex`:
  - New constant `COMMON_SHARE = 0.02` (tune, see below): a stem in at least
    `COMMON_SHARE * max(len(self.entries), CORPUS_FLOOR)` open headlines is filler.
  - New method `is_common(token) -> bool`: `token in SEED_STOPWORDS or self.df[token] >= limit`.
  - In `match()`: first `tokens = frozenset(t for t in tokens if not self.is_common(t))`, then the existing
    logic. Replace the hard-coded `len(posting) <= 400` cap with `is_common`. When computing `w_other` for an
    entry, also skip common tokens so both sides are measured the same way.
- `GROUPING_VERSION = "3"` so `runner.py` regroups the last 72 h once after deploy.
- Update the module docstring: replace `"sloven"` with a neutral example and say thresholds were tuned on
  Slovenian headlines.
- Items stored before this change have tokens without the old Slovenian stopwords. That is fine: the
  learned filter handles both. No migration.

**Tuning check (required before merging):**
1. Ask the user for a database backup (Settings → Install, backup & log out → Download database backup). Put it
   in the scratchpad, never in the repo.
2. Write a throwaway script (scratchpad, not committed) that runs `regroup()` over the last 72 h of that
   copy twice: once with the old tokenizer (git `main` version of textmatch/grouping), once with the new.
   Compare: number of stories, and list the merges that differ (headline pairs).
3. Adjust `COMMON_SHARE` (try 0.01–0.03) until the new grouping is at least as good as the old one on a
   manual read of the differences. Report the chosen value and the diff summary in the PR description.

**Tests (`tests/test_pipeline.py`, `tests/test_feed.py`):** existing Slovenian grouping tests must still pass.
Add: (a) a German set of 5 headlines about one event plus 20 unrelated German headlines groups the 5
together and nothing else; (b) a stem that appears in > `COMMON_SHARE` of headlines does not make two
unrelated headlines merge.

**Done when:** tests pass and the tuning report is written.

---

### WP3: Local language check, warn only (needs WP1)

**`newsnest/sections.py`**
- Delete `_SL_WORDS`, `_SL_CHARS`, `_WORD`, `_text_slovenian`, `looks_slovenian`, `local_section`.
- Add, reusing `textmatch.fold`:
  ```python
  def local_vocabulary(conn, sources, exclude: str | None = None, days: int = 14, size: int = 60) -> set[str]:
      """The most frequent folded words (any length >= 2) in recent headlines+snippets of Local feeds,
      except `exclude`. This stands in for "the user's local language" without knowing which it is."""

  def looks_local(texts: list[str], url: str | None, vocab: set[str], tld: str = "") -> bool | None:
      """True if the url is on `tld`; None with < 3 texts or an empty/too-small vocab (< 20 words);
      else whether >= 40% of texts share >= 2 distinct words with vocab."""
  ```
  (Same thresholds as today's `looks_slovenian`, so behaviour for the owner stays close.)
- `foreign_local(conn, sources, tld, days=3)`: build the vocabulary per feed with `exclude=sid` (so a wrong
  feed can't vouch for itself) and return ids where `looks_local(...) is False`.
- Update the module docstring: Local is whatever the user files there; a feed that doesn't read like the
  other Local feeds is flagged, never moved.

**`newsnest/discovery.py`**
- `CheckResult.slovenian` → delete; `check_feed` no longer judges language.
- Line ~194: store `r["category"]` unchanged (no `local_section`).
- `_map_folder(folder)` line ~83: replace `("sloven", "lokal", "local", "novice")` with
  `("local", "lokal", "regional")` plus extra words passed in. `_map_folder` is called from
  `parse_opml()`'s inner `walk()` (line ~59), which has no config: add a parameter
  `parse_opml(data, local_words: tuple[str, ...] = ())` and thread it to `_map_folder`. Callers in `web.py`
  (`/discovery/import`) pass the first 5 folded letters of `cfg.region.country` and `cfg.region.city` when
  set; the starter-pack calls pass nothing.

**`newsnest/web.py`**
- Delete `_local_feed_slovenian`. In `settings_action` remove the `feed_add` block that fetched the feed and
  rewrote `category`/`moved` (and the `moved` / `&moved=` redirect bits).
- `discovery_action` "promote": use `row["category"]` as is.
- `settings_context` feeds page: `foreign_local(conn, loaded.sources, cfg.region.tld)`.

**Templates**
- `settings_feeds.html`: `sintro` → "Local is for news from where you live{{ ' (' ~ cfg.region.place ~ ')' if cfg.region.place }}. International news goes to World; gear, style and the good life to Lifestyle." Delete the `moved` callout. Warn callout text: "… under Local {{ "doesn't" / "don't" }} read like your other Local feeds. Show …".
- `_settings_ui.html` `section_options`: hint `(Slovenian outlets)` → `(where you live)`. `feed_row` flagged
  subtitle → "Doesn't read like your other Local feeds. Move it to World or Tech".
- `settings_topics.html` footnote: `ljubljan matches Ljubljani` → `electr matches electric and electricity`.

**Tests:** `tests/test_discovery.py:63-64` assert `.slovenian`: rewrite as `looks_local` unit tests:
Slovenian vocab + Slovenian texts → True; Slovenian vocab + English texts → False; empty vocab → None;
`.si` tld match → True; German vocab + German texts → True. Add a web test: adding an English feed under
Local keeps it under Local (no redirect to World).

---

### WP4: Prompts and labels (needs WP1)

All prompt text must read naturally with `region.place` set ("Berlin, Germany") and unset ("").

**`newsnest/scoring.py` `INSTRUCTIONS`**
- "You rate news items for one reader who lives in Ljubljana, Slovenia." → `{reader}` where
  `reader = f"one reader who lives in {place}" if place else "one reader"`.
- "Most items are in Slovenian; judge the meaning, not the language." → "Items can be in any language;
  judge the meaning, not the language."
- "for someone living in Slovenia" → `{importance_scope}` = `f"for someone living in {country}"` or
  `"in general"`.
- Reason example `"Ljubljana parking; local interest"` → `"city parking rules; local interest"`.
- If the categories include "Local"/"National", add one line: `"Local" means the reader's city or area,
  "National" their country.` (only when `place` is set).
- `system_blocks(cfg)` passes the new fields to `.format(...)`.

**`newsnest/events.py`**: in `EVENT_RULES` replace the examples `"moharic-mandat-resnica"` →
`"city-council-budget-vote"` and `"slovenia-raises-cyber-threat-level"` →
`"government-raises-cyber-threat-level"`. Keep the module docstring (the Slovenian PHP headline is a good
cross-language example).

**`newsnest/profile_ai.py`**
- `PROFILE_FORMAT` example `"Ljubljana traffic, parking and construction"` → `"local traffic, parking and
  construction"`.
- `DRAFT_SYSTEM` becomes `def draft_system(cfg) -> str`: "one reader in Ljubljana, Slovenia" → `{reader}`
  (same rule as scoring); drop "Most headlines are Slovenian."; line ~90 calls `draft_system(cfg)`.

**`newsnest/calibration.py`**: `SUBSCORES` becomes `def subscores_labels(cfg)`; importance label =
`f"Big news for {country}"` or `"Big news in general"`; update the loop at line ~65.

**Comments only**: `models.py:36` example `"24ur.com"` → `"bbc.co.uk"`; `feed.py:9` `"less from 24ur"` →
`"less from <outlet>"`; `feed.py:153` `(Ljubljana)` → `(your city)`; `spike.py:4` `--source rtvslo` →
`--source bbc-world`; `web.py` docstrings at ~242 and ~286 may stay.

**Tests:** add tests asserting `system_blocks(cfg)[0]["text"]` contains "Berlin, Germany" with region set,
and contains neither "Slovenia" nor "Ljubljana" with region unset. Same for `draft_system`.
`grep -rniE "sloven|ljubljan" newsnest/*.py` should only hit comments/docstrings that are deliberate.

---

### WP5: Settings, onboarding and local packs UI (needs WP1; not in parallel with WP3)

**Settings → General (`templates/settings_general.html`, `web.py::settings_action` action `"general"`)**
- New group "Where you live" at the top, with `input_row`s: Country, City, Time zone, Local web domain
  (placeholder `.si`, footnote "Optional. Feeds on this domain always count as Local.").
- Time zone input gets `list="tz-list"` with a `<datalist>` of `zoneinfo.available_timezones()` sorted.
- Saving: `section_map(data, "region")` gets `country`, `city`, `tld` (strip; empty strings are fine);
  `data["timezone"] = form["timezone"].strip() or "UTC"`. Validation errors come back through the existing
  redirect.
- The form autosaves (`data-autosave`), so the new fields just need `name`s.

**Onboarding (`templates/profile.html`, `/profile` route)**
- When `onboarding` and `not cfg.region.country`: show a group "Where do you live?" **above** the rating
  list: Country (with `<datalist>` of `LOCAL_PACKS` country names), City, and a hidden `timezone` filled by
  `app.js` from `Intl.DateTimeFormat().resolvedOptions().timeZone` (fallback: leave the current value).
  Posts to new `POST /settings/region` with `next=/profile`.
- `POST /settings/region`: same region/timezone write as "general"; redirect to `next` only if it is
  exactly `/profile`, else `/settings/general`. If `pack_for(country)` exists and `tld` is empty, also set
  `region.tld = pack.tld`; if the submitted timezone is empty, use `pack.timezone`.
- When `onboarding` and region is set and `pack_for(country)` has missing feeds: show "Add local news for
  {country}: {n} feeds ({names})" with a button posting to `/settings/pack/local/{code}?next=/profile`.
  Copy hint: "Headlines from them show up here within about 15 minutes."

**Local pack route (`web.py`)**: `POST /settings/pack/local/{code}`, modelled on `lifestyle_pack()`:
404 for unknown code; `edit_config` with `add_pack(section_list(data, "feeds"), pack.feeds, "news")`;
`_refile()`; also set `region.tld` from the pack if empty; redirect to `next` (`/profile` only) or
`/settings/feeds?localpack={added}&packmoved={moved}#section-news`.

**Settings → Feeds (`settings_feeds.html`)**
- In the Local section fold, mirror the lifestyle offer: if `pack_for(cfg.region.country)` has missing
  feeds, show an `expand` "Local news pack: {country}" with names and an "Add {n} feeds" button.
- Below it, an `expand` "Packs for other countries" listing every other `LOCAL_PACKS` entry with its own
  add button (small form per pack).
- Success callout for `localpack` query param, like the lifestyle one.
- `settings_context("feeds")` adds `local_pack`, `local_missing`, `other_packs`.

**Tests (`tests/test_web.py`)**: region form saves and reloads; bad time zone shows an error; onboarding
shows the region step when region is empty and hides it when set; pack route adds SI feeds, sets tld, is
idempotent (second call adds 0); unknown pack code → 404; `next` other than `/profile` is ignored.

---

### WP6: Neutral starter config, docs, test fixtures (last)

**`config.example/config.yaml`**
- `timezone: UTC` with comment "Set it in Settings → General".
- Add a commented `region:` block after timezone explaining country/city/tld, all empty.
- Remove the "Slovenian news" and "Slovenian tech" feed blocks (now in the SI pack). Keep World and
  international tech. Comment above feeds: "Local news: add a pack in Settings → Feeds, or your own feeds
  with category: news".
- Remove the Ljubljana watch; keep the `watches:` key with a commented generic example.
- `categories:` list: `Ljubljana`, `Slovenia` → `Local`, `National`.
- The `max_item_age_days` comment mentions Dnevnik: make it generic.

**`config.example/interests.md` / `seed_examples.md`**: neutral English examples ("Local news: traffic,
parking, construction, city decisions", "National economy, taxes, energy prices"; seed headlines in English).

**`README.md`**: Quick start gets a step "Open Settings → General and set where you live, then add your
country's local pack (Slovenia, Germany, US, UK) or your own feeds". Replace the `Europe/Ljubljana` note and
the `TZ` table default (`UTC`). Add a short "Add a local pack for your country" contributing note pointing at
`LOCAL_PACKS` in `newsnest/packs.py`.

**`TODO.md`, `docs/settings-rehaul-plan.md`**: leave as history, but change the TODO "Slovene word forms"
item to "word forms in inflected languages".

**Tests** (they all start from `config.example/`):
- Tests that rely on Slovenian categories (`tests/conftest.py:68` returns category `"Ljubljana"`,
  `tests/test_scoring.py:34,47`) switch to `"Local"`.
- Slovenian headline fixtures in tests stay as they are (they are realistic multilingual data).
- Run the whole suite; anything that depended on a removed feed id gets its own feed in the test setup.

**Owner migration (put in the PR description, not in code):** add to your `config/config.yaml`:
```yaml
region: {country: Slovenia, city: Ljubljana, tld: .si}
timezone: Europe/Ljubljana
```
Your existing feeds and categories stay as they are.

---

### WP7: DE, US, UK pack data (needs WP1's `LocalPack`; parallel-safe, only edits `packs.py` data)

- For each of Germany (`de`, `Europe/Berlin`, `.de`), United States (`us`, `America/New_York`, `""`),
  United Kingdom (`uk`, `Europe/London`, `.uk`): pick 8–10 general news feeds of major national outlets
  with different leanings, plus 0–3 national tech sites (`category="tech"`).
- Each feed must pass `discovery.check_feed()` (https, HTTP 200, entries, a post in the last 2 weeks, not
  a podcast/video). Write a scratchpad script that runs it over the list and keep only passing feeds; give
  busy feeds (> 40 posts/day from `per_day`) a `daily_cap` like the existing packs.
- Add a comment above each pack with the check date, like the `packs.py` module docstring.
- Do not add these feeds anywhere else.

## Verification (end-to-end, after all WPs)

1. `pytest` — all green.
2. `grep -rniE "sloven|ljubljan|rtvslo|24ur|\.si\b" newsnest config.example docker-compose.yml README.md`
   — only hits inside `LOCAL_PACKS["si"]`, deliberate docstrings, and README pack list.
3. Fresh install: empty `config/` and `data/` folders, `docker compose up -d --build` (or
   `python -m newsnest` per README "Development"), open `http://localhost:3095`:
   - no Slovenian feeds, time zone UTC;
   - `/profile` shows "Where do you live?"; enter Germany/Berlin → the Germany pack offer appears; add it →
     German feeds under Local in Settings → Feeds, `region` and `timezone: Europe/Berlin` in config.yaml;
   - add an English feed under Local → it stays under Local; after its first fetch the Feeds page flags it.
4. Owner path: copy the owner's config with the `region:` block from WP6 → Feeds page flags nothing new,
   prompts (visible via calibration / AI call log) say "Ljubljana, Slovenia".
5. Grouping tuning report from WP2 attached to the PR.
