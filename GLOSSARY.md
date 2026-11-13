# NewsNest

A self-hosted news reader for one reader: it collects stories from many outlets, scores them against the
reader's interests and shows them as one ranked feed.

## Language

### Stories and the feed

**Card**:
One story as the reader sees it: every outlet's article about the same news, joined by story or event.
_Avoid_: item (an item is one outlet's article), post

**Lead**:
The card's best-scored article, the one whose headline and outlet the card shows.

**Shown**:
A card that was on screen in the feed long enough to count as seen, or was opened.
_Avoid_: impression, viewed

### What the reader wants

**Interest**:
One line of the Want list in the reader's interests file, in their own words.
_Avoid_: topic, tag, category (a category is the AI's coarse grouping, like Sports)

**Explicit weight**:
A "more / less of this outlet or category" choice the reader made in a card's menu. Only the reader
changes it.

**Level**:
How much of one interest the reader wants: Much less, Less, Normal, More or Much more, set on the Profile
page. Only the reader changes it.
_Avoid_: weight (a weight multiplies a story's rank; a level moves its score)

**Shift**:
What a level adds to the score of every card matched to that interest: −1, −0.5, 0, +0.5 or +1. It counts
before the cut-off, so it changes which stories get in, not only their order.

**Old boost**:
A small permanent lift an outlet or category got from past opens and likes, before drift replaced it.
_Avoid_: nudge, learned weight

### Following changing interests

**Attention time**:
How long a card was in view while the reader was actively reading, shared with any other card on screen
at the same time.
_Avoid_: dwell time, view time

**Lingered**:
A card the reader spent clearly more attention time on than on most cards: in their own top quarter.

**Engagement**:
How much a shown card interested the reader, from scrolled past (none) through lingered, key points
opened, saved, opened and liked, down to not interested.

**Drift**:
How much the reader's recent engagement lifts or lowers an interest, category or outlet compared with
their usual, fading as it gets older.
_Avoid_: nudge, learned boost, short-term profile

**Faded interest**:
An interest the reader has engaged with clearly less than usual lately; it sinks but never disappears.

**Revisit story**:
The one story per feed visit from a faded interest, placed early so a returning interest gets noticed.
