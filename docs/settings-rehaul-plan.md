# Settings + Profile rehaul: iOS Settings style, split into pages, phone-first

## Context
`/settings` (`newsnest/templates/settings.html`, one route at `newsnest/web.py:713`) puts nine panels on one page:
General, Discovered sources (33-row table), Learned from your feed (~90 rows), Feeds (40+ rows, each with a 4-button
switch), Watches, Keyword filters, Listing searches, GitHub releases and Google News. It looks bad and is hard to use.
- Endless scroll on a phone.
- Paragraph-long explanations.
- Rows built as `<li>` text with an inline `<form>` button, so "Reset" / "Remove" wrap under the text (screenshot).
- A desktop table squeezed to 390px.
- "Add the starter pack (42 feeds)" never goes away: it is rendered unconditionally (`settings.html:52`).

Decisions from the user:
- **Scope:** a full visual rehaul of Settings and Profile. The new components are shared so other pages can adopt them later.
- **Visual direction:** iOS Settings style. The dock and tabs already copy Apple News.

Done in **three parts**, one PR each, so the look can be checked on a phone after Part 1 before it is applied everywhere.

---

## The design language (built in Part 1, used by every page)
All of it goes in `newsnest/static/app.css` as a new `/* ---------- settings: grouped lists ---------- */` block, using the
existing tokens (`--surface`, `--surface-2`, `--border`, `--muted`, `--text-2`, `--accent`, `--danger`, `--radius`) so
light and dark both work. Jinja macros go in a new `newsnest/templates/_settings_ui.html`.

**Page**
- Large title (existing `.pagehead h1`) plus at most one short muted line. No paragraphs.
- Content column max 680px. Gray page background with white groups, like iOS.

**Group** (`.group`, macro `group(title, footnote)`)
- Small uppercase gray header above (existing `.sec` typography).
- A rounded container (`--surface`, 12px radius, no heavy shadow).
- Optional footnote under it in 0.82rem gray, max 1–2 lines. It replaces today's explanation paragraphs.

**Row** (`.row`, macro `row(...)`)
- Grid `[leading?] [main] [trailing]`, min-height 52px, padding 12px 16px.
- Hairline separator inset 16px from the left; none after the last row.
- **main**: title (1rem, 600, one line, ellipsis) plus an optional subtitle (0.84rem gray, one line, ellipsis).
- **trailing** is `flex: none` and never wraps. This is the fix for the screenshot.
- **leading** (optional): a 28px rounded-square icon tile, or a letter avatar for feeds and outlets.

**Row variants**
| Variant | Trailing | Behaviour |
| --- | --- | --- |
| Link | gray value + chevron › | the whole row is the `<a>` |
| Toggle | iOS switch (styled `input[type=checkbox][role=switch]`) | autosaves on change |
| Value / select | the current value as gray text + ⌄; a transparent native `<select>` overlays it | autosaves; native picker on phones |
| Number | right-aligned small input with a unit ("5.5", "$0.50") | autosaves on change |
| Action | none; centred or left-aligned accent text ("Add a feed…") or red text ("Forget all tuning") | button |
| Menu | ⋯ icon button (40px hit area) | opens a bottom sheet on phones / popover on desktop |

- **Row ⋯ menu:** reuse the existing card-menu sheet (`.sheet`, `.sheet-backdrop`, `.pop-item` in `app.css:774`; open
  and position logic in `app.js:301`). Generalise it to read its items from a `<template>` inside the row, so every
  row with several actions (Pause, Move to…, Keep, Remove) has one tidy ⋯ instead of a strip of buttons.
- **Expanding row** (`<details class="row-expand">`): for "Add a feed", "Import OPML" or "Show 64 small adjustments".
  The summary is an action row; the body holds form fields laid out as rows (label left, input right on desktop,
  stacked on phones) with one Save button.
- **Badges:** count pill (reuse `.pill`) and a warn dot (reuse `.alert-dot`).

**Feedback**
- Autosaving controls submit via the existing `form[data-autosubmit]` handler (`app.js:75`). The server redirects back
  with `?saved=`, and the page shows a **toast** using the existing `toast()` in `app.js` instead of a callout stuck at
  the top. Errors stay as a red callout at the top.
- Destructive actions keep `data-confirm`.

---

## Part 1: Shell, navigation, design components, General, starter-pack bug

### 1. Pages and routes
| URL | Page |
| --- | --- |
| `/settings` | Phone: the **root list** (iOS-style menu). Desktop: sidebar plus General |
| `/settings/general` | General |
| `/settings/feeds` | Feeds |
| `/settings/discover` | Discover new sources |
| `/settings/topics` | Watches and keyword filters |
| `/settings/more` | GitHub releases, Google News, listing searches |

- `newsnest/web.py`: add `GET /settings/{page}` with a whitelist and a per-page context helper, so each page only runs
  its own queries (`candidate_rows` only on Discover, `foreign_local` only on Feeds). `POST /settings/{action}`
  (`web.py:772`) stays. GET and POST don't clash, but keep page keys distinct from action names.
- Add `ACTION_PAGE = {"general": "general", "feed_add": "feeds", "watch_add": "topics", ...}` next to `settings_action`.
  Use it for the success and error redirects (`web.py:843`, `web.py:856`), `lifestyle_pack` (`web.py:751`) and the
  `/discovery/*` handlers (→ `/settings/discover`).
- Templates: `_settings_layout.html` (extends `base.html`: nav, title, error callout, toast trigger) plus one template
  per page. `settings.html` is removed.

### 2. Navigation (iOS / macOS System Settings pattern)
- **Root list:**
  - Group 1: General.
  - Group "Sources": Feeds (gray "42"), Discover (gray "33 on trial", plus a dot when a source earned a place), Topics & filters.
  - Group "Advanced": More sources.
  - Group "You": Profile & tuning → `/profile`.
  - Each row gets a coloured icon tile (inline SVGs, like the ⋯ menu icons in `base.html`).
- **Phone:** `/settings` shows only the root list. Sub-pages show a "‹ Settings" back link above the title.
- **Desktop (≥ 900px):** a two-column grid. The root list becomes a sticky 260px sidebar with the current page
  highlighted; content on the right. `/settings` shows General in the content area.
- `base.html`: the Settings item in the ⋯ menu is current for any `/settings*` path.

### 3. General page
- Group "Reading":
  - Mark read when I scroll past (toggle).
  - Calibration mode (toggle, subtitle "Show everything with scores").
- Group "Scoring":
  - Hide stories below (number).
  - Summary language (value row).
- Group "AI": Daily budget (number, "$"). Footnote: "Spent today: $x" if cheap to get, else nothing.
- The whole page is one `<form data-autosubmit>` posting to the existing `/settings/general`, so there is no Save button.

### 4. Starter-pack bug
- `newsnest/discovery.py`: add `starter_missing(conn, cfg)`. It returns the starter OPML feeds whose `_key(url)` is not in
  `known_feed_keys()` (`discovery.py:134`).
- Discover root row and page use it:
  - Nothing missing → no button. The "Add sources" group just says "Starter pack added ✓".
  - Some missing → "Add N new starter feeds".
- Replace `starter_count` in `settings_page`.

### 5. Tests (Part 1)
- `tests/test_web.py`: page smoke list includes the new pages; settings redirects now go to `/settings/<page>`.
- `tests/test_discovery.py`: `/settings/discover`; the starter button is gone after `POST /discovery/starter`.
- `tests/test_auth.py`: one new sub-page requires login.
- `tests/test_feed.py:191` still posts to `/settings/general`.

---

## Part 2: Feeds, Discover, Topics, More sources in the new style

**Feeds** (`/settings/feeds`)
- Top: a warn callout if any non-Slovenian feed is under Local, with a "Show" link that scrolls to it.
- Action row "Add a feed…" expands to Name, URL and Section rows plus an Add button.
- One collapsible group per section: Local · 10, World · 8, Tech · 18, Lifestyle · 6.
  - Open by default only if it has a flagged feed or the feed just changed (`?changed=`).
- Feed row:
  - Letter avatar; name; subtitle = host · "Paused" · daily cap.
  - Trailing: the section as a value-select ("Tech ⌄"), which autosaves via the existing `/settings/feed_section`,
    plus ⋯ (Pause/Resume, Remove).
  - Paused rows dimmed; flagged rows get a warn subtitle "Not in Slovenian. Move to World".
  - This replaces the 4-button `.seg` switch per row.
- Lifestyle pack offer: a promo row at the top of the Lifestyle group ("Lifestyle & gear pack · 18 feeds",
  Add button, interest checkbox in an expanding row).

**Discover** (`/settings/discover`)
- Short title line: "Feeds on trial show only their best stories (6.0+, max 4/day each)." The rest goes into footnotes.
- Group "Earned a place" (only if any): a row per source with an accent "Keep" button.
- Group "On trial · 33":
  - Row: name; subtitle "Tech · ~49/day · 381 stories".
  - Trailing: a progress label **"1 of 3"** (opens/likes toward promotion), then ⋯ (Keep as regular source, Remove).
  - Sorted by progress, so the ones closest to promotion come first.
  - Footnote: "Opened or liked 3 times in a week → suggested. Nothing good in 21 days → retired."
- Group "Add sources": starter-pack row (only if missing feeds), "Import OPML…" expanding row (link or file, section select).
- Group "Not on trial · N": collapsed, with a status chip per row ("Removed", "Retired", "Failed: reason") and a
  "Try again" row action.

**Topics & filters** (`/settings/topics`)
- Group "Watches":
  - Row: name; subtitle = keywords (ellipsis); trailing remove icon.
  - "Add a watch…" expanding row.
  - Footnote: "Matches are tagged, never hidden. Use word stems: ljubljan."
- Groups "Mute words" and "Always show":
  - Words as removable chips (reuse `.chip.small`) plus an add field.
  - A small chip-editor script in `app.js` keeps the hidden textarea in sync and autosubmits to the existing `/settings/keywords`.
  - Works without JS, because the textarea is shown in `<noscript>`.

**More sources** (`/settings/more`)
- Group "Google News": an On/Off toggle row (existing `/settings/gnews_toggle`), query rows (query, subtitle
  edition · section, ⋯ Remove), "Add a query…".
- Group "GitHub releases": repo rows plus "Add a repository…".
- Group "Listing searches": rows (name, subtitle site · method, ⋯ Remove), "Add a search…", and a footnote about phase 2.

**Tests (Part 2):** update the page-text assertions in `tests/test_web.py` (lifestyle pack, feed rows, watches) and
`tests/test_discovery.py`. Add a test for the "N of 3" progress value.

---

## Part 3: Profile rehaul, "How your feed is tuned", polish

**Profile** (`newsnest/templates/profile.html`), in the same grouped style:
- Group "Waiting for you": proposal rows → link rows with a date subtitle.
- Group "Your interests": `interests.md` as a link row that opens an editor (expanding row with a full-width textarea
  and Save). The same pattern applies to `seed_examples.md` and `listing_preferences.md` under "Advanced".
  The first screen is no longer three giant textareas.
- Onboarding (rate headlines) keeps its flow but uses rows.

**"How your feed is tuned"** (replaces "Learned from your feed"; moved out of Settings)
- One-line intro: "NewsNest shows more or less from these, based on what you open, like and pick in a story's ⋯ menu.
  Nothing is hidden."
- Two groups: **"Showing more"** and **"Showing less"**, each sorted by strength.
- Row:
  - Leading: letter avatar (outlet) or tag icon (topic). Title = name; subtitle = "Outlet" / "Topic".
  - Trailing: a 3-dot strength meter (●○○ a little · ●●○ more · ●●● much more; same for less, in the danger colour)
    and an ↺ icon button, aria-label/title "Back to normal". The exact ×factor is shown only as a tooltip.
- Weights within ±5% of 1.0 are hidden behind an expanding row "Show N small adjustments". At most 10 per group
  before a "Show all" row.
- Last row: red action row "Forget all tuning" (existing `data-confirm`).
- Empty state row: "Nothing yet. Use ⋯ on a story → More about / Less from."
- Add a `weight_strength(w) -> (direction, level)` helper in `newsnest/feed.py`, next to `IMPLICIT_MAX`, `WEIGHT_MIN` and
  `WEIGHT_MAX`, so the template does no maths. `/api/weights/reset` (`web.py:573`) redirects to `/profile#tuning`.
  The settings route stops loading weights.

**Polish**
- Check every Settings page and Profile at 390px and 1280px, light and dark. Fix spacing, focus rings, tap targets
  of 44px or more, and long names. This also closes the TODO item "Visual check of every page at phone width".
- Remove now-unused CSS (`.seg`, `.actions-cell`, `.pack`, settings-only `.grid2`) once nothing references it.

**Tests (Part 3):**
- Unit test for `weight_strength`.
- `/profile` shows "How your feed is tuned"; a reset redirects to `/profile#tuning`.
- Small adjustments are collapsed.
- `tests/test_feed.py:148` assertion updated.

---

## Verification (every part)
1. Run `.venv/bin/pytest -q`; everything passes.
2. Start the app (`NEWSNEST_SCHEDULER=off`, a seeded config with many feeds, candidates and weights) and take Playwright
   screenshots at 390×844 and 1280×900, light and dark, of every page touched in that part. Show them to the user in
   the PR description.
3. Check by hand in the browser:
   - No horizontal scroll, and no trailing control ever wraps under its text.
   - Toggles, selects and numbers autosave and show a toast.
   - The ⋯ sheet opens on phone and the popover on desktop.
   - The starter button is gone after adding the pack.
   - Each POST lands back on the right page.
