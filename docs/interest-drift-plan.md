# Changing interests

Status: plan, nothing built yet.

## Problem

Interests come and go. Basketball can be a big thing for a few months, fade while something unrelated
takes over, and come back a year later, or never. NewsNest should follow that without asking: no
"pause this topic?" questions, no prompts. The reader just reads, and the feed keeps up.

Today it mostly doesn't:

- **Learned boosts never wear off.** Every open and 👍 multiplies the outlet and category weight a bit
  (`OPEN_NUDGE`, `LIKE_NUDGE` in `feed.py`), up to `IMPLICIT_MAX` = 1.5. Nothing ever brings them back
  down. A season of basketball leaves Sports boosted for good.
- **Categories are too coarse.** The AI puts a story in one of about 13 categories. Basketball, football
  and the Olympics are all "Sports", so even a perfect category signal can't tell "less basketball" from
  "less sport".
- **The interests file is the strongest signal and only changes when the reader edits it** (or accepts a
  weekly proposal). If it says basketball, every basketball story gets a high `personal` score, read or not.
- **Skipping only affects that one story.** A story shown three times without an open drops out
  (`seen_drop_after`), but nothing carries over to the next basketball story.
- **Exploring can't find a returning interest.** The extra stories that widen the feed (`_widen`) leave
  out categories whose weight is below 1 and categories with more 👎 than 👍. Once a topic has faded, the
  feed stops showing it, so it can't tell when the reader is interested again.

What already adapts: the scoring prompt's examples are the latest 10 👍 and 10 👎 (`ratings_text`), and
chip order and `_turned_down` look at the last 30 days only.

## What research and big news apps do

1. **Keep two profiles, a slow one and a fast one.** The Daily Learner (Billsus & Pazzani, 2000) kept a
   long-term model of the reader and a short-term model built from the last 100 rated stories, and used
   the short-term one whenever it had enough evidence. The combination did better than either alone.
   Google News (Liu, Dolan & Pedersen, 2010) studied click logs to see how interests change over time,
   and predicted current interest from the reader's own recent clicks plus what everyone was reading.
   Neural news recommenders such as LSTUR (An et al., 2019) still split users into long- and short-term
   representations. **For NewsNest:** the interests file is the slow profile. What's missing is a fast
   one built from recent behavior.
2. **Old behavior should count less, smoothly.** Time-weighted recommenders (Ding & Li, 2005) give
   older data an exponentially smaller weight and note that some interests fade faster than others.
   **For NewsNest:** weigh every open, 👍, 👎 and skip by its age (a half-life), rather than adding up
   permanent nudges.
3. **A reader has several unrelated interests. Keep them separate and don't delete one that's quiet.**
   Pinterest's PinnerSage (2020) found one profile vector per user isn't enough and keeps a cluster per
   interest. **For NewsNest:** track each interest in the file on its own, and let a quiet one fall back
   to a low weight rather than disappear, so it can come back.
4. **A skip is a weak, uncertain "no".** In learning-to-rank research (Joachims et al.), a story that
   wasn't clicked may never have been looked at, so treating every non-click as a dislike is badly
   biased. Short-video systems (Kuaishou, 2023) say the same about skips. **For NewsNest:** count skips
   only lightly, only once there's enough of them, and with a limit on how far they can push.
5. **Without some exploration, interests collapse.** DeepMind's study of feedback loops (Jiang et al.,
   2019) found that a feed that only shows what it's sure of narrows the reader's interests over time,
   and steady random exploration slows that down. Yahoo's front-page work (Li et al., 2010) and YouTube's
   (Chen et al., 2021) show that exploring pays off over time. **For NewsNest:** a faded interest keeps
   a small, steady chance of being shown. That's how the feed notices it's back.

## Design

Two layers. The interests file stays exactly as the reader wrote it (the slow profile). A new short-term
layer, computed from recent behavior, adjusts the ranking on top of it. It's computed fresh every time
a feed session is ranked: no stored state that drifts, nothing to reset.

### 1. Tag each story with the interest it matches

The scoring call already returns topic, importance, personal, category and event. Add one more field:
`interest`, the bullet from the `## Want` section that the story matches, copied exactly, or empty.
Store it in a new `items.interest` column.

- This gives basketball-level detail without adding categories: the reader's own bullets are the units.
- It costs a few output tokens per story, in a call that's already being made.
- If the reader edits a bullet, its old tag stops matching anything. That interest starts again from
  neutral, which is the right thing after an edit.
- Stories without a tag fall back to their category (step 3).

### 2. Recent engagement per interest

For each interest, add up the last `drift.window_days` (default 120) of signals, each weighted by
`0.5 ^ (age / drift.half_life_days)` (default 21 days):

| Signal | Counts as |
|---|---|
| opened (`opened_at`, a tap, not auto-read) | +1 |
| 👍 | +2 |
| not interested / 👎 | −2 |
| shown in the feed, never opened (`seen_count` > 0, no open) | 1 shown, 0 engaged |

Then compare how often this interest gets opened with how often the reader opens anything:

```
rate     = (engaged + prior × base_rate) / (shown + prior)    # smoothed toward the reader's average
weight   = clamp(rate / base_rate, drift.min_weight, drift.max_weight)
```

- `base_rate` is the reader's open rate over all shown stories in the same window, so a reader who opens
  little isn't punished everywhere.
- `prior` (default 8 impressions) means a handful of skips does almost nothing. It takes a steady pattern.
- Limits: `min_weight` 0.6, `max_weight` 1.4. A faded interest sinks but never disappears, and the file
  still decides most of the score.
- Because old signals fade, two weeks of opening basketball again outweighs months of skipping it a
  year ago. Coming back is quick by design.

### 3. Categories use the same layer

Stories without an interest tag (and every story when the file has no `## Want` section) use the same
calculation, keyed by `ai_category`. This replaces the permanent `OPEN_NUDGE` / `LIKE_NUDGE` boosts.

- Explicit choices from the card menu ("more from…", "less about…") stay exactly as they are: permanent,
  and only the reader changes them.
- Implicit nudges stop being written. The existing rows are untouched at first. A one-time migration
  then sets any weight that came only from nudges (≤ `IMPLICIT_MAX`, no menu action logged in `feedback`)
  back to 1.0, because the new layer covers it.
- Per-outlet boosts work the same way, keyed by `source_id`, with the same limits.

### 4. Ranking

In `rank_feed`, multiply the rank by the interest weight (or the category weight for untagged stories),
next to the existing source and category weights. The "why" panel gets a plain line when it matters,
like the existing taste lines: "You've been reading more about basketball lately" or "You've read less
about basketball lately". That's an explanation, not a question.

Top stories are left out (only boosted, never sunk), as today. Big news gets through whatever the
reader's mood.

### 5. Exploration keeps faded interests alive

`_widen` gets a third kind of extra story, next to "new source" and "exploring": **one story per feed
session from an interest whose weight is below 1**, the best-scoring one available, at most every
`explore_every` cards. It skips interests turned down outright (a 👎 in the last 7 days).

- If the reader opens it, the weight climbs back quickly (step 2).
- If they don't, nothing happens beyond one more light skip. The limit stops it sinking further.
- The current filter that keeps categories with weight < 1 out of exploration is dropped for these
  slots. That filter is what makes a faded interest disappear for good today.

## What this does not solve

- **An interest that isn't in the file at all** (something new, say cycling, catches on) still depends
  on the existing tools: the exploring slots, the AI's `personal` score from recent 👍 examples, and the
  weekly proposal. The short-term layer helps a little through the category fallback (more Sports opens
  lift Sports), but it can't name a new interest. A later step could tag stories with a free-form topic
  as well, so new interests show up in the data. Left out for now: it needs a clustering step and more
  tokens.
- **Seasonal interests** (a team's season every autumn) are handled only by "comes back quickly", not
  predicted. Research on periodic interests (Alibaba's SIM, 2020, and later work) needs far more data
  than one reader produces.
- **Skips are noisy.** A story skipped because the reader was busy counts the same as one skipped from
  lack of interest. The prior and the limits keep this small, but it's never zero.

## How we'll know it works

- **Replay test:** a fixture where basketball is opened every day for 60 days, then never for 60 days,
  then again. Check the weight over time: high, then sinking to around 0.6 within about 6 weeks, then
  back above 1 within about 2 weeks of the return.
- **Stability tests:** a few skips barely move a weight. Explicit menu weights never change on their own.
  Top stories never sink.
- **Live, read-only:** the Profile page's "What the feed learned" section lists "Lately more" and
  "Lately less" with the current weights, so the reader can see it working without being asked anything.

## Phases

1. `interest` tag in scoring + `items.interest` column + tests. Nothing uses it yet; data starts piling up.
2. Short-term layer (step 2–3) behind `drift.enabled` (default on), why-panel lines, Profile page list;
   stop writing implicit nudges.
3. Faded-interest exploration slot (step 5).
4. Migration that resets nudge-only weights to 1.0.

Config (new `drift:` section): `enabled`, `half_life_days` 21, `window_days` 120, `prior` 8,
`min_weight` 0.6, `max_weight` 1.4.

## Sources

- Billsus & Pazzani, *User Modeling for Adaptive News Access*, UMUAI 2000 —
  https://www.ics.uci.edu/~pazzani/Publications/BillsusA.pdf
- Liu, Dolan & Pedersen, *Personalized News Recommendation Based on Click Behavior*, IUI 2010 —
  https://research.google/pubs/personalized-news-recommendation-based-on-click-behavior/
- An et al., *Neural News Recommendation with Long- and Short-term User Representations*, ACL 2019 —
  https://aclanthology.org/P19-1033/
- Ding & Li, *Time Weight Collaborative Filtering*, CIKM 2005 —
  https://cseweb.ucsd.edu/classes/fa17/cse291-b/reading/p485-ding.pdf
- Pal et al., *PinnerSage: Multi-Modal User Embedding Framework for Recommendations at Pinterest*,
  KDD 2020 — https://arxiv.org/abs/2007.03634
- Joachims et al., *Unbiased Learning-to-Rank with Biased Feedback*, WSDM 2017 —
  https://www.cs.cornell.edu/~schnabts/downloads/joachims2017ltr.pdf
- Pan et al., *Learning and Optimization of Implicit Negative Feedback for Industrial Short-video
  Recommender System*, 2023 — https://arxiv.org/abs/2308.13249
- Jiang et al., *Degenerate Feedback Loops in Recommender Systems*, AIES 2019 —
  https://arxiv.org/abs/1902.10730
- Li et al., *A Contextual-Bandit Approach to Personalized News Article Recommendation*, WWW 2010 —
  https://arxiv.org/abs/1003.0146
- Chen et al., *Values of User Exploration in Recommender Systems*, RecSys 2021 —
  https://research.google/pubs/values-of-exploration-in-recommender-systems/
- Pi et al., *Search-based User Interest Modeling with Lifelong Sequential Behavior Data* (SIM), CIKM 2020 —
  https://arxiv.org/abs/2006.05639
