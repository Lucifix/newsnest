# Changing interests

Status: built. Part 1 collects the signals, part 2 makes the feed adapt (`newsnest/drift.py`).

## Problem

Interests come and go. Basketball can be a big thing for a few months, fade while something unrelated
takes over, and come back a year later, or never. NewsNest should follow that without asking: no
"pause this topic?" questions, no prompts. The reader just reads, and the feed keeps up.

Today it doesn't:

- **Learned boosts never wear off.** Every open and 👍 multiplies the outlet and category weight a bit
  (`nudge()` in `feed.py`), up to 1.5, and nothing brings it down again. One install had 17 weights at
  that cap after 17 days, so the boosts no longer tell anything apart.
- **Taps alone would be just as wrong.** Most stories are read from the headline and the summary and never
  opened. On that same install 7% of the stories on screen were tapped, and ratings fell from 26 a day to
  a handful. A tally of taps alone sinks Sports, World and Politics to the floor, though those are the
  headlines read every day.
- **Categories are too coarse.** Basketball and football are both "Sports".
- **Exploring can't find a returning interest.** `_widen` leaves out categories whose weight is below 1,
  so once a topic has faded the feed stops showing it and can't tell when the reader is back.

## What research and big news apps do

1. **Most people read headlines, not articles.** About 6 in 10 Americans said that in the past week they
   had read nothing beyond headlines (American Press Institute / AP-NORC, 2014). In a news aggregator
   app, longer summaries led to fewer click-throughs (Dellarocas et al., 2016): a good summary replaces
   the click.
2. **Time on screen is a better signal than clicks.** Facebook (2015) ranks posts partly by how long they
   stay on screen, compared with the same person's usual time, because people read serious stories
   without reacting to them. Yahoo used reading time instead of clicks (Yi et al., 2014); Lagun & Lalmas
   (2016) measure attention by how long each part of a page is in view.
3. **Keep a slow and a fast profile.** The Daily Learner (Billsus & Pazzani, 2000) and Google News (Liu et
   al., 2010) combine a long-term profile with one from recent behaviour. Here the interests file is the
   slow one.
4. **Old behaviour should count less, smoothly** (Ding & Li, 2005): a half-life, not permanent nudges.
5. **Keep interests apart and don't delete a quiet one** (Pinterest PinnerSage, 2020).
6. **A skip is a weak, uncertain "no"** (Joachims et al., 2017): smooth it and limit it.
7. **Keep exploring**, or interests narrow and a return goes unnoticed (Jiang et al., 2019).

## How it works

1. **Each story gets a label**: the line of the `## Want` list it matches (the scoring call answers with
   its number). This gives basketball-level detail in the reader's own words.
2. **The app records how the reader reads**: how long each card is on screen while they're active,
   opening its key points, saving, tapping, 👍 and 👎.
3. **For each interest it keeps a tally** where recent reading counts more and old reading fades
   (half-life 14 days), compared with the reader's own usual.
4. **The feed order shifts gently**: a card moves at most 40% up or down. Nothing is hidden; top
   stories and watch matches are never pushed down.
5. **Once per feed visit, one story from a faded interest** is placed early. Read it and the interest
   comes back within days.

Every install first collects 14 days (and 500 stories) of this data before its feed adapts, so nobody has
to switch anything on. It can be turned off in Settings. The interests file is never changed by it, and
the card menu's "more / less" choices stay exactly as they are.

## Definitions

| Term | Meaning |
|---|---|
| Card | Items grouped as the feed groups them: shared story or shared event. Every signal counts once per card. |
| Interest | One bullet under `## Want`, compared ignoring case, spacing and a final full stop. |
| Key | The card's interest when it has one that is still in the file, otherwise its category. Unscored cards are skipped. Every card also counts for its lead outlet. |
| Attention time | Milliseconds the card was in view on the home feed while the page was visible and the reader did something in the last 30 s. Split evenly between the cards in view at once. A card is in view when 60% of it shows or it fills half the screen. Capped at 60 s per page load and 300 s in total. |
| Shown | On screen in the feed (or opened) since attention time started being recorded. Ratings from the swipe deck alone don't count. |
| Lingered | Attention time at least 3 s and in the reader's top quarter (75th percentile over shown cards); nothing lingers until 200 cards have a time. |
| Engagement | 👎 / not interested: −1. Otherwise tapped + liked + saved (1 each) + key points opened + lingered (0.5 each), at most 3. Scrolled past: 0. |
| Weight | Per key, with age weight 0.5^(age / 14 days): (engaged + 40 × base) / (shown + 40), divided by base, where base is the reader's average engagement per shown card. Limits 0.6–1.4 per interest or category, 0.8–1.25 per outlet, 0.6–1.4 for a card. |
| Lifted / faded | Weight ≥ 1.1 / ≤ 0.9. |
| Old boost | A weight row in (1.0, 1.5] with no matching "more / less" menu choice: it came from the old nudges. Ignored while the new weights are active, used again if they're turned off. |

## Parts

1. **Collect the signals**: the interest label in scoring, attention time and key-point opens. Nothing
   visible changes.
2. **The feed adapts**: the weights above, the why-panel lines ("You've been reading about … lately"),
   the revisit story, a read-only "Lately" list on the Profile page and a Settings switch. The old nudges
   stop. The scheduler works the weights out every 30 minutes and stores them, so a ranking doesn't wait
   for them (a ranking works them out itself when they're over 3 hours old or made for other settings).

## Known limits

- Attention time is only measured on the home feed list, not on the desktop front page, search or saved.
- Phone and desktop reading mix: splitting time between visible cards helps, but a phone reader looks
  longer per card.
- A card that stays in the feed for days collects more time than one shown once; the top-quarter rule
  and the caps keep this small.
- A Want line reworded by hand starts fresh; one reworded through an accepted weekly proposal keeps its
  history.
- A new interest that isn't in the file only shows through its category.
- A light reader may need more than 14 days to finish the warm-up.

## Sources

- American Press Institute / AP-NORC, *The Personal News Cycle*, 2014 —
  https://www.pbs.org/newshour/politics/myth-media-bubble-burst-americans-still-seek-serious-news-study-finds
- Dellarocas, Sutanto, Calin & Palme, *Attention Allocation in Information-Rich Environments: The Case of
  News Aggregators*, Management Science 2016 —
  https://marketing.wharton.upenn.edu/wp-content/uploads/2016/10/01-23-201420Dellaocas20Title20Paper20Abstract.pdf
- Facebook, *Taking Into Account Time Spent on Stories*, 2015 —
  https://about.fb.com/news/2015/06/news-feed-fyi-taking-into-account-time-spent-on-stories/
- Yi et al., *Beyond Clicks: Dwell Time for Personalization*, RecSys 2014 —
  https://dblp.org/rec/conf/recsys/YiHZLR14.html
- Lagun & Lalmas, *Understanding User Attention and Engagement in Online News Reading*, WSDM 2016 —
  https://dblp.org/rec/conf/wsdm/LagunL16
- Billsus & Pazzani, *User Modeling for Adaptive News Access*, UMUAI 2000 —
  https://www.ics.uci.edu/~pazzani/Publications/BillsusA.pdf
- Liu, Dolan & Pedersen, *Personalized News Recommendation Based on Click Behavior*, IUI 2010 —
  https://research.google/pubs/personalized-news-recommendation-based-on-click-behavior/
- Ding & Li, *Time Weight Collaborative Filtering*, CIKM 2005 —
  https://cseweb.ucsd.edu/classes/fa17/cse291-b/reading/p485-ding.pdf
- Pal et al., *PinnerSage*, KDD 2020 — https://arxiv.org/abs/2007.03634
- Joachims et al., *Unbiased Learning-to-Rank with Biased Feedback*, WSDM 2017 —
  https://www.cs.cornell.edu/~schnabts/downloads/joachims2017ltr.pdf
- Jiang et al., *Degenerate Feedback Loops in Recommender Systems*, AIES 2019 —
  https://arxiv.org/abs/1902.10730
