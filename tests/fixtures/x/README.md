# X GraphQL fixtures

Four payloads, in the shape X's own web app fetches for itself:

| File | Operation | Contents |
| --- | --- | --- |
| `search_timeline_page1.json` | `SearchTimeline` | 8 posts, authors created 2011–2024 |
| `search_timeline_page2.json` | `SearchTimeline` | 8 posts, **the first 3 identical to page 1** |
| `search_timeline_page3.json` | `SearchTimeline` | 8 posts, authors all created in one week |
| `tweet_detail_conversation.json` | `TweetDetail` | root post + 12 replies with text |

**Provenance, stated plainly.** These are *shaped* like the live payloads, not
captured from them. Capturing `SearchTimeline` requires a logged-in X session
(see `docs/X_SESSION.md`), and this repository holds no session and never will.
What is faithful here is the structure, and the structure is what the code under
test has to survive:

* the `data → search_by_raw_query → search_timeline → timeline → instructions[]
  → entries[] → content → itemContent → tweet_results → result` chain that puts
  tweet nodes at depth 12–14 — measured live against `$CHEEMS` and the reason
  `_walk_for_tweets` caps at 16 rather than 10;
* **both** user shapes in one feed: the current split across
  `core` / `relationship_counts` / `tweet_counts` / `verification`, and the older
  `legacy` blob that is the only one carrying `fast_followers_count`;
* the re-served head of the feed. Page 2 repeats page 1's first three entries
  verbatim because that is what X does on every scroll, and deduping it is the
  difference between 21 posts and 24.

Page 2's repetition and page 3's one-week author cluster are the two behaviours
the tests actually assert on, so changing either changes what the tests mean.

Regenerating: these were written by a throwaway builder script rather than by
hand; there is nothing to re-run, and editing the JSON directly is fine.
