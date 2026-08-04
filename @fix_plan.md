# Botsensai task queue

Worked top to bottom by the Ralph loop. One task per iteration. A task is done
only when its acceptance command exits 0 and the work is committed.

Phases are ordered so that each one unblocks the next. Resist the temptation to
jump to the interesting tasks in phase 4 — without phase 1 there is no data to
fit on, and fitting on nothing produces confident nonsense.

**IN PROGRESS**: _(none — the loop writes the task id here when it splits a task)_

---

## Phase 0 — Unblock the loop's own gate

The loop refuses every `build.done` whose reported cyclomatic complexity is
above **10**. That number is not ours: it is `QualityReport::COMPLEXITY_THRESHOLD`,
a hardcoded `f64` in ralph 2.10.1 (`crates/ralph-core/src/event_parser.rs:163`),
read by `BackpressureEvidence::all_passed()`. No config key raises it. Until the
maximum in `src` is ≤ 10, no iteration's work can be accepted — so these come
before the data tasks even though they add no signal. See DEC-011.

Only the *maximum* is reported, so these are worked worst-first: lowering an 11
while an 18 stands moves the reported number not at all. Each chunk lowers
`MAX_COMPLEXITY` in `scripts/backpressure.py` to the new measured maximum.

- [x] **P0-01 — The four worst functions (31, 28, 22, 20)**
  Done 2026-08-03. `util.synthetic.generate_token` 31→4 (section builders plus
  per-archetype tables instead of nested ternaries), `backtest.engine.run` 28→7
  (`_RunState`/`_RegimeCache` and one method per replay phase),
  `scoring.composite.VetoEngine.evaluate` 22→7 (vetoes grouped by domain, and a
  single dedupe replacing four `not in vetoes` guards),
  `collectors.pumpfun_ws.run` 20→6 (`_Budget`, `_Episode`, `_wait_before_redial`).
  Repo maximum 31 → 18; `MAX_COMPLEXITY` lowered to match.
  _Accept:_ `python -m ruff check --select C901 --config "lint.mccabe.max-complexity=18" src` exits 0. ✔ Also `python scripts/backpressure.py` 9/9 green, 237 tests pass, and both refactors were verified output-identical rather than assumed: 36 synthetic tokens + a 12-token cohort digest-identical, and a 40-token synthetic backtest identical on every field (digest `85ff1fc8…`, 2400 evaluated, 6 entered, pnl 0.065458).

- [x] **P0-02 — The collector and CLI offenders (18, 18, 18, 17, 13, 13, 12, 12, 12)**
  Done 2026-08-03. Every collector is now ≤ 10, not ≤ 13:
  `social.enrich` 18→7 (`_catalog_matches`, `_attribute_post`),
  `pumpfun.enrich` 18→5 (one `_enrich_*` per sub-surface),
  `dexscreener.discover` 18→3 (`_fetch_boosts`, `_merge_boosts`,
  `_fetch_raw_list`, `_collect_pairs` — the last shared with `enrich` 13→6),
  `browser.visit` 17→6 (`_response_listener`, `_route_blocker`, `_drive_page`),
  `pumpfun._parse_trade` 13→6 (`_trade_as_of`/`_trade_side`/`_trade_amounts`),
  `pumpfun.discover` 12→4, `pumpfun._parse_holders` 12→6 (`_holder_rows`,
  `_holder_labels`, `_labelled_share`), `geckoterminal.enrich` 12→3
  (`_enrich_batch`, `_entry_to_snapshot`, `_enrich_token_info`),
  `_pool_to_records` 11→7 (`_launchpad_from_pool` table).
  Also `scoring.fit.fit` 15→7 (`_search`/`_tune_metrics`/`_tune_families`) and
  `execution.broker.check_entry` 14→9 (`_size_within_limits`, `_token_veto`),
  because this task's own acceptance command is repo-wide and those two were
  the only functions left above 13. They are struck from P0-03 below.
  Repo maximum 18 → 13; `MAX_COMPLEXITY` lowered to match.
  _Depends on: P0-01._
  _Accept:_ `python -m ruff check --select C901 --config "lint.mccabe.max-complexity=13" src` exits 0, and `python -m pytest tests/test_collectors.py -q` exits 0. ✔ Also `python scripts/backpressure.py` 9/9 green (`complexity: 13`, coverage 65%), 247 tests pass, and the split was verified behaviour-preserving rather than assumed: a probe digesting a 40-token backtest (2400 evaluated, 7 entered, pnl 0.042817437), a 260-example weight fit, a 480-row `check_entry` grid, the record parsers, nine collector surface runs against a canned transport and four browser visits is identical on both sides of the refactor (`bd9a9ba18d434791`).
  New: `tests/test_collector_surfaces.py` — 10 tests driving `discover`/`enrich`
  through a fake transport, pinning the never-raise and partial-result contract
  that this split put at risk. They pass unmodified against pre-refactor HEAD,
  and two deliberate mutations (dropping a `degraded=True`, guessing an owner
  for an address-only 4chan post) each turn one red.

- [x] **P0-03 — The remaining nine, down to 10**
  Done 2026-08-03. All nine split, and the ratchet has reached its destination:
  `util.http.request` 13→7 (`_without_network` for the cache/offline
  short-circuits, `_attempt` for one paced round trip, `_backoff`),
  `cli.backtest` 13→2 (`_backtest_tapes`, `_print_backtest_table`,
  `_print_backtest_caveats`, `_backtest_payload`),
  `pipeline.sweep` 13→6 (`_sweep_discover`, `_sweep_enrich`, `_rank`,
  `_manage_open_positions`), `pipeline.collect` 13→8 (`_stop_before_sweep`,
  `_sweep_budget`, `_sweep_within_budget`, `_nap_seconds`),
  `cli.collect` 12→6 (`_report_collection`, `_report_collection_gaps`),
  `credibility.InsiderSupplyOverhang.compute` 12→5 (`_linked_wallets`,
  `_net_purchased`, `_overhang`),
  `community.CrossPlatformPropagationLag.compute` 12→7 (`_origin_timestamp`,
  `_organic_spread`), `media.generator.blog_post` 12→2 (`_split_evidence` plus
  one builder per section, as P0-01 did for `generate_token`),
  `topology.FunderGraphDispersion.compute` 11→4 (`_funder_degree`,
  `_funding_graph`, `_cluster_shares`).
  **Repo maximum 13 → 10 over 725 functions, and `python scripts/backpressure.py`
  now reports `complexity: 10` with 9/9 green — the first payload this repo has
  produced that the loop's own gate can accept.** The ceiling is not ours: it is
  `QualityReport::COMPLEXITY_THRESHOLD`, a hardcoded 10.0 in ralph 2.10.1.
  `MAX_COMPLEXITY` in `scripts/backpressure.py` is now 10 and is a ceiling to
  hold rather than a ratchet to lower.
  The split was verified behaviour-preserving rather than assumed: a probe
  (`/var/tmp/p0_03_probe.py`) digesting 17 `PacedClient.request` cases, 8 funder
  graphs, 13 insider-overhang cases, 11 propagation-lag cases, 11
  sweep/collect sessions, 13 blog posts and 4 CLI invocations is identical on
  both sides of the refactor (`ALL 2058f6956e74cc7b`, every section digest
  equal). Four deliberate mutations — ignoring `Retry-After`, dropping the
  zero-balance guard, ignoring the findings-section cap, dropping the
  post sort — each changed a digest or crashed, so the probe is sensitive
  rather than merely green. Two traps: the CLI banner prints `settings.db_path`
  from a **process-wide singleton** that the pipeline section had already
  repointed at its own tempdir, and `tempfile` names differ per run; both had
  to be masked before the CLI section was stable across two runs of identical
  code.
  _Depends on: P0-02._
  _Accept:_ `python -m ruff check --select C901 --config "lint.mccabe.max-complexity=10" src` exits 0, and `python scripts/backpressure.py` prints `complexity: 10` or lower with 9/9 green. ✔ Both. 258 tests pass, coverage 65% → 73%, mypy clean over 51 files, duplication 1.8%.
  New: `tests/test_http_client.py` — 11 tests pinning the retry contract of
  `PacedClient.request`, which every collector's HTTP goes through and which
  had **no direct test at all** before this split. They pass unmodified against
  pre-refactor HEAD, and three deliberate mutations (ignoring `Retry-After`,
  retrying a hard 4xx, dropping the GET cache) each turn one red.

---

## Phase 1 — Get real data on disk

Nothing downstream means anything until there is a corpus. This phase is
unglamorous and is the whole ballgame.

- [x] **P1-01 — Continuous collection daemon**
  Done 2026-07-30. `botsensai collect --hours N` loops `Pipeline.sweep` until the
  window closes. Every sweep writes a `collector_runs` heartbeat under
  `surface="sweep"` — including the ones that failed, hung or were interrupted —
  and `Database.collection_gaps()` reads those back, which is what makes "the
  daemon was dead from 03:00" distinguishable from "the market was quiet".
  Deviation from the description: a sweep is not *started* when the window has
  less time left than the previous sweep took, instead of being cut off at the
  deadline. A real sweep takes ~102s, so cutting off would write a failed
  heartbeat on every single run and leave a permanent false `surface_health`
  alarm on the dashboard. A hung sweep is still hard-capped at
  `remaining + 15s`. Reasoning in `.ralph/agent/decisions.md` DEC-001.
  Consequence: `--hours 0.05` is one sweep, not two.
  Add `botsensai collect --hours N` that runs the pipeline sweep on a loop,
  persists everything, and survives individual collector failures without
  stopping. Must write a heartbeat row to `collector_runs` each sweep so gaps
  are detectable after the fact.
  _Depends on: none._
  _Accept:_ `timeout 300 python -m botsensai.cli collect --hours 0.05 && python -c "from botsensai.store.db import Database; from botsensai.config import get_settings as g; c=Database(g().path(g().db_path)).counts(); assert c['launches']>50, c; print(c)"` exits 0. ✔ 103s, exit 0. One sweep: 166 discovered → 20 screened → 20 scored → 1 entered, `stopped because: deadline`, 0 sweeps with errors. Store 804 → 879 launches, +2855 trades, +246 snapshots, +74 posts. Loop behaviour covered by `python -m pytest tests/test_collect.py -q` (10 passed).

- [x] **P1-02 — pump.fun new-mint websocket ingestion**
  Done 2026-07-30. `botsensai stream --minutes N` subscribes to
  `subscribeNewToken` and `subscribeMigration` on `wss://pumpportal.fun/api/data`
  and writes each mint on arrival. **Measured on the real store: median latency
  to first observation fell from 88.6s to 1.31s** (p90 1.73s, fastest 0.62s),
  over 57 mints taken off the socket in 120s and then corroborated by one REST
  sweep. All 57 were novel — the socket beat the poller every time.
  Three things the task description did not anticipate:
  1. **The frame carries no timestamp.** No `created_timestamp`, no `blockTime`.
     A stream-discovered launch can only record its receipt time, which is an
     *upper bound* on the mint time, so "latency to first observation" is not
     measurable from the stream alone. It is measurable from the two paths
     together, which forced (2).
  2. `upsert_launch` now takes `MIN(...)` of both `created_at` and `observed_at`
     instead of letting the first writer pin them. Without it the socket's
     approximate `created_at` would displace the REST path's authoritative
     `created_timestamp` permanently and skew every age screen and
     `age_seconds` metric. `observed_at` is unchanged in effect (the first
     write was always the earliest) but is now explicit about why.
  3. `Database.observation_latency()` reports `measured` separately from
     `launches`. A socket-discovered row starts at exactly zero latency because
     nothing yet knows when it was minted; averaging those in would have
     manufactured a spectacular fictional figure. They join `measured` when a
     sweep corroborates them.
  Run it as a second process alongside `collect` — the store is WAL sqlite and
  takes concurrent writers, and keeping them apart means a socket outage cannot
  disturb the sweep loop's deadline handling (DEC-001). Wiring the stream *into*
  `collect` is deliberately left undone.
  Also fixed en route: `backoff_delay` raised the factor to the attempt count
  before clamping, so `2.0 ** 1024` raised `OverflowError` — a crash in exactly
  the situation reconnecting exists for. Found by a test, not by reading.
  Subscribe to `wss://pumpportal.fun/api/data` (`subscribeNewToken`,
  `subscribeMigration`) and write launches the instant they mint rather than up
  to a poll interval late. Reconnect with exponential backoff. Latency to first
  observation is the point: record it.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_ws_ingest.py -q` exits 0 (test uses a local fake websocket server, not the live endpoint). ✔ 13 passed. Live check beyond the plan's requirement: `python -m botsensai.cli stream --minutes 2` exit 0, 1 connection, 60 frames, 57 mints (57 novel), 1 migration, 0 unparsed, store 1021 → 1078 launches.

- [x] **P1-03 — Outcome labeller**
  Done 2026-07-31. `botsensai label --min-age-hours 24` walks aged launches,
  reconstructs each price path from stored snapshots plus GeckoTerminal OHLCV,
  and writes `Outcome` rows. **`outcomes` went 0 → 716 on the real store**,
  which unblocks P3-01, P4-01, P4-02 and P4-03.
  The liquidity tax is the point and it is large: at 0.25 SOL a 10x on 200 USD
  of liquidity realizes **2.11x** (a 79% tax), on 2,000 USD 7.27x, on 20,000 USD
  9.64x. The position is entered at the t0 spot and exited *against the curve*
  via `CurveState.from_snapshot`, so the gap is attributable to exit depth
  alone — the backtester's `FillSimulator` already charges entry impact and
  charging it twice would corrupt the target.
  Four things the description did not anticipate:
  1. **Simultaneous rows read as a price move.** Every collector in a sweep
     writes at once, and 33 of 383 same-token clusters inside 30 seconds
     disagree by more than 3x on price — the worst by 6,771,130x. Walked in
     timestamp order that produced a **149,878x label on a nine-microsecond
     move**, the largest in the store. Paths are now collapsed by gap into
     one median point per cluster (DEC-007); the maximum label fell to 9.23x,
     which is a real 52-minute move the collapse leaves alone.
  2. **An unknown t0 is `None`, not 1.0** (DEC-008). 181 of 716 launches were
     first seen more than 15 minutes after the mint. Survival flags follow the
     same rule and are only claimed over horizons the path reaches.
  3. **The OHLCV budget counts attempts, not successes** — the same bug P1-02
     hit with `max_connections`. Measured live: 12 attempts, 3 pools returned
     candles. A success-counted budget would have spent unbounded calls against
     a 30/min limit shared with every other collector.
  4. `ohlcv()` gained `before_timestamp`. Without it the API returns the newest
     hundred candles, which for a token that died on day one is a hundred flat
     minutes at the wrong end of its life.
  **Known limitation, honestly reported rather than papered over:** median peak
  is 1.00x because the median stored path spans zero minutes — most tokens have
  one observation. `rugged` is 0/716 for the same reason: the corpus never sees
  the liquidity withdrawal. Longer `collect` runs, not a looser rug rule, are
  the fix.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_labeller.py -q` exits 0, and the test asserts that for a token whose price spiked on 200 USD of liquidity, `max_realizable_multiple` is materially below `max_multiple_from_t0`. ✔ 30 passed; `test_a_spike_on_thin_liquidity_is_not_realizable` asserts the realizable figure is under half the peak *and* under the pool's total depth. Live: `python -m botsensai.cli label --min-age-hours 24 --max-ohlcv 12` exit 0, 716 considered / 716 labelled / 0 skipped, 323 graduated, 3 of 12 pools returned candles (74 candles), store outcomes 0 → 716.

- [x] **P1-04 — Wallet prior-history index**
  Done 2026-08-03. `WalletPriorIndex` (`onchain/wallet_priors.py`) resolves
  time-restricted prior-trade counts, batched through
  `Database.wallet_prior_counts` and memoized in an LRU;
  `Database.refresh_wallet_profiles` maintains the (until now completely empty)
  `wallet_profiles` table. Wired into `Pipeline.build_context`, which passed a
  literal `{}`.
  **The empty index was not degrading `fresh_wallet_ratio`, it was inverting it.**
  With no priors the metric falls back to `HolderRecord.wallet_age_seconds`, and
  the store holds 187 holder rows against 7,271 trades — so essentially every
  buyer was skipped, leaving numerator 0 over a full denominator. Measured over
  the 25 most recent tokens with ≥10 distinct traders, it returned **0.0000 for
  every one of them**: a hard "no fresh wallets", the most bullish reading the
  metric can emit, on tokens whose buy side is ~89% fresh. Mean is now **0.8862**
  (median 0.9029). `sniper_supply_share` moved far less (mean 0.0389 → 0.0400,
  median unchanged at 0.0000) because it defaults a missing prior to 0 and so was
  merely pinned at its 0.4 weight floor rather than inverted.
  Three things the description did not anticipate:
  1. **`wallet_seen_before` filtered on `as_of` only** (`db.py:829`), where every
     other point-in-time read in the file filters on `as_of` *and* `observed_at`.
     It is the one such read feeding a backtested feature. Not theoretical: all
     7,271 trades have `observed_at > as_of`, and over the 40 most recent
     launches with trades (1,684 wallets) the single-bound count credited
     **2,511 prior trades against 1,987 knowable ones — 524 leaked, 20.9%**.
     Both bounds are now enforced, and the correction is conservative (it can
     only lower a prior count, i.e. make wallets read fresher and the signal more
     bearish).
  2. **The two bounds are what make the cache sound.** Once `observed_before` is
     past, no later write can change the answer — a new row necessarily carries a
     later `observed_at`. So `(wallet, before, observed_before)` is immutable and
     safe to memoize across sweeps; keying on `wallet` alone would serve one
     token's answer to another token's question.
  3. **An unrounded knowledge bound made the LRU dead on arrival** — the live
     path scores each token at its own `utcnow()`, measured at a 0% hit rate.
     The bound is now floored to a 60s bucket, which is sound only because it
     rounds *down* (it can exclude a trade just observed, never include one
     observed too late). Re-scoring one candidate then goes 147 misses → 147 hits
     with no extra queries. The cross-token win is smaller and comes from
     elsewhere: `before` legitimately differs per token, so the LRU cannot share
     across them — batching plus the `first_seen_at` fast path is what pays there
     (523 of 629 wallets answered with no counting query, 629 wallets resolved in
     2 queries instead of 629 round trips).
  Schema: `SCHEMA_VERSION` 3 → 4, adding `ix_trades_wallet_seen(wallet, as_of,
  observed_at)` so the count stays index-only.
  Follow-up left undone on purpose: `Database.deployer_history` (`db.py:798`) has
  the identical single-bound problem. Same class of leak, different feature; it
  deserves its own task rather than being smuggled in here.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_wallet_priors.py -q` exits 0 and asserts the count for a wallet is time-restricted. ✔ 15 passed, exit 0. Time restriction is pinned by `test_prior_count_is_restricted_to_trades_before_the_cutoff` (strict `<`) and the knowledge bound by `test_prior_count_excludes_trades_not_yet_observed`. Probed against the producers, not a formatter: restoring `wallet_priors={}` fails `test_live_context_populates_wallet_priors`, and removing the `observed_at` clause from `wallet_prior_counts` fails 2 tests.

- [x] **P1-05 — Funding-source resolution**
  Done 2026-08-04. `onchain/funding.py`: `FundingSourceResolver` (two RPC calls
  per wallet — oldest signature, then that transaction), `FundingIndex` (the
  store-backed cache plus the policy on what may be clustered),
  `wallet_funding` table (`SCHEMA_VERSION` 4 → 5) with
  `Database.upsert_wallet_funding` / `wallet_funding` / `funder_fanout`. Wired
  into `Pipeline.enrich` (resolve, largest holdings first) and
  `Pipeline.build_context` (attach). Before this, every one of the metrics named
  above ran on `funded_by = None` for **every holder in the store**: the funding
  graph had no edges at all and the cluster Gini was the address Gini.
  **Measured on the real corpus** — 162 holder wallets backfilled, 234 calls in
  241.9 s at 120 req/min with no 429. Of 175 wallets: 77 resolved to a private
  funder, 5 to an exchange, 101 unresolved (below). Of the 6 stored tokens with
  ≥6 holders, **4 moved**, and the largest move is the one the task is for:
  `GXRiEL2Co84NgyyE9bq` went from *25 holders → 25 clusters* to *25 → 20*, so
  `funder_graph_dispersion` **11.02 → 2.53** and `holder_distribution_health`
  **0.5608 → 0.3417**. That token also carries one `cex:`-labelled holder whose
  funder was deliberately **not** used as a cluster key.
  Four things worth keeping:
  1. **"Funded" is broader than "sent SOL".** A holder's oldest transaction is
     often not a transfer but someone paying rent to open its token account and
     filling it — the wallet's lamport balance never moves. Reading only SOL
     movements leaves those wallets looking unfunded. Three readings are tried
     in descending strength (parsed transfer → token-account payer → lamport
     delta) and the one that answered is recorded in `source`: on the real
     corpus that is 65 transfer, 9 account-payer, 1 balance-delta. Without the
     account-payer reading those 9 would have read as no funder.
  2. **`funded_at` bounds the read; `resolved_at` does not.** Funding is a point
     lookup on a wallet already in hand and necessarily precedes that wallet's
     first buy, so the same call at the decision point returns the same answer.
     Bounding on when *we* asked would delete the feature from every backtest
     without making it more honest. The event-time bound is enforced and pinned.
  3. **A shared exchange funder is not a cluster**, and one seed list cannot be
     trusted to know that. Two mechanisms decide it: `EXCHANGE_WALLETS` (public
     explorer labels, unverified here) and `Database.funder_fanout` (measured in
     our own store — a funder behind ≥25 distinct wallets is a dispenser
     whatever it is labelled). The exchange row stays in the table as a fact and
     the holder keeps a `cex:<name>` label; only `funded_by` is withheld.
  4. **A transport failure is never cached as absence.** A chain-answered
     negative is cached (asking again gets the same answer); an RPC error stops
     the batch instead, so a dead endpoint cannot be written into the store as
     "these wallets have no funder". Probed live against a malformed address:
     `resolve_one` raises, `resolve_missing` writes 0 and does not raise.
  **Coverage ceiling, reported not papered over:** 101 of 175 holder wallets
  (58%) are `rpc:history-exceeds-one-page` — more than 1000 signatures, and
  `getSignaturesForAddress` pages newest-first, so finding their oldest
  transaction costs a call per page against the scarcest budget in the system.
  Those are recorded as *unresolved*, not unfunded. The skew is toward fresh
  wallets, which is the population these metrics care about, but a paid provider
  with an enhanced-history endpoint is what lifts it (noted in
  `docs/DATA_SOURCES.md`). It is also why the two 50-holder tokens did not move:
  `funder_graph_dispersion` links a funder only at degree ≥2 *and* ≥8% of
  holders, which needs 4 of 50 from one funder.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_funding_graph.py -q` exits 0, including a case where a shared CEX funder does NOT merge two independent holders. ✔ 22 passed, exit 0; `test_a_shared_cex_funder_does_not_merge_two_independent_holders` is that case, with `test_a_shared_private_funder_does_merge_two_holders` as its control so the assertion cannot pass by clustering nothing. Probed against the producers, not the tests: handing out exchange funders as cluster keys, dropping `funding.apply` from `build_context`, dropping the `funded_at` bound, and caching a transport failure as a resolved answer each turn the suite red.

---

## Phase 2 — Raise metric coverage

Backtests currently run at roughly 30–55% coverage. Below 50% the composite is
a guess. Every task here is worth more than a new metric.

- [x] **P2-00 — Replace the dead X syndication endpoints** *(done)*
  Three `cdn.syndication.twimg.com` paths return HTTP 200 with a zero-byte body.
  Replaced with `syndication.twitter.com/srv/timeline-profile` (HTML +
  `__NEXT_DATA__`) and `cdn.syndication.twimg.com/tweet-result` with the derived
  token. Empty successes now raise instead of being recorded as absence.
  _Accept:_ `python -m pytest tests/test_collectors.py -q` exits 0. ✔

- [x] **P2-00b — Reddit via Arctic Shift** *(done)*
  Reddit's own `.json` returns 403 to datacenter IPs. Switched to the
  Arctic Shift mirror; `botsensai doctor` went from `down` to reachable. ✔

- [x] **P2-00c — 4chan /biz/ and Telegram collectors** *(done)*
  Both live and returning real posts. 4chan supplies native image MD5s;
  Telegram supplies view counts from the `t.me/s/` preview. ✔

- [ ] **P2-01 — X search depth via web-use**
  `XCollector.search_via_browser` captures one page of GraphQL. Scroll and
  capture until either 200 posts or 3 minutes, dedupe by post id, and extract
  per-reply author creation dates (the input to `engager_age_dispersion`).
  Reply *text* is only available here — no free path carries it — so this is the
  task that unblocks `reply_template_ratio` on X.
  Degrade to syndication-only when no browser is available.
  _Depends on: none._
  _Accept:_ `python -m pytest tests/test_x_collector.py -q` exits 0 against recorded fixtures in `tests/fixtures/x/`.

- [ ] **P2-02 — Perceptual image hashing**
  `derivative_remix_depth` needs `SocialPost.media_hashes` populated and
  currently gets nothing. Add a pHash implementation (numpy DCT, no new
  dependency), fetch media with a strict size cap and timeout, and cache by URL.
  _Depends on: none._
  _Accept:_ `python -m pytest tests/test_phash.py -q` exits 0, asserting that a resized and re-encoded copy of an image hashes within Hamming distance 6, and an unrelated image does not.

- [ ] **P2-03 — Instagram and TikTok collectors**
  Both via `WebUseDriver`, capturing the JSON their own pages fetch. Public
  hashtag and search pages only. Expect frequent failure and make degradation
  the normal path, not an exception. These two feed
  `cross_platform_propagation_lag`, which is currently near-blind.
  _Depends on: P2-01._
  _Accept:_ `python -m pytest tests/test_social_longtail.py -q` exits 0 against fixtures; `python -m botsensai.cli doctor` still exits 0 when both are unreachable.

- [ ] **P2-04 — Curate the Telegram call-channel watchlist**
  The reader works; what is missing is the list. Seed
  `collectors.telegram.extra.call_channels` from Dexscreener `links` of type
  `telegram` plus manual curation, then measure each channel's lead time against
  subsequent price action so the useless ones can be dropped.
  _Depends on: P1-01._
  _Accept:_ `python -m botsensai.cli channels --rank` exits 0 and prints per-channel median lead time in seconds.

- [ ] **P2-06 — Fast-follower and identity-discontinuity metrics**
  The X profile payload carries `fast_followers_count` (X's own count of
  burst-acquired followers) and enough history to detect a repurposed account —
  a large gap between `user.created_at` and the oldest retrievable post, with a
  low `statuses_count`, means the archive was wiped. Both are stronger evidence
  than the ratio heuristics currently standing in for them.
  _Depends on: P2-01._
  _Accept:_ `python -m pytest tests/test_metrics.py -q -k "fast_follower or discontinuity"` exits 0.

- [ ] **P2-05 — Coverage regression gate**
  Add a test that runs a synthetic backtest and fails if mean metric coverage
  drops below the current committed baseline. Record the baseline in
  `docs/RESULTS.md`. This stops coverage silently rotting as collectors drift.
  _Depends on: P2-01, P2-02._
  _Accept:_ `python -m pytest tests/test_coverage_gate.py -q` exits 0.

---

## Phase 3 — Backtest properly

- [ ] **P3-01 — Walk-forward harness**
  `BacktestSettings` describes train/test/step/embargo and nothing consumes it.
  Implement rolling walk-forward: fit weights on the train window, evaluate on
  the test window, step forward, with the embargo enforced so a token spanning
  the boundary cannot appear in both.
  _Depends on: P1-03._
  _Accept:_ `python -m pytest tests/test_walkforward.py -q` exits 0, including an assertion that no token key appears in both a train and its own test fold.

- [ ] **P3-02 — Ablation report**
  Wire `scoring.fit.ablation` to a CLI command that drops each metric in turn
  and reports the change in holdout objective. Any metric whose removal
  improves the objective is a liability — flag those loudly in the output.
  _Depends on: P3-01._
  _Accept:_ `python -m botsensai.cli ablate --synthetic` exits 0 and prints one row per registered metric.

- [ ] **P3-03 — Null-hypothesis baselines**
  Add baseline strategies to the backtester: random entry, buy-everything, and
  buy-highest-volume. If the composite does not beat all three on the same
  universe with the same fill model, it has no edge and the report must say so
  in those words.
  _Depends on: P3-01._
  _Accept:_ `python -m botsensai.cli backtest --synthetic --baselines` exits 0 and prints a comparison table including all three baselines.

- [ ] **P3-04 — Fill-model calibration against reality**
  Compare modelled slippage to slippage actually observed in collected trade
  data at matched sizes, and correct the model's parameters if they are
  optimistic. Document the before and after in `docs/RESULTS.md`.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_fill_calibration.py -q` exits 0.

---

## Phase 4 — Learn and adapt

- [ ] **P4-01 — Fit weights on real labelled outcomes**
  Once `outcomes` holds at least 200 labelled rows, run `WeightFitter`, write
  `config/weights.json`, and record train and holdout rank correlation plus
  top-decile lift in `docs/RESULTS.md`. If holdout correlation is at or below
  0.05, say so plainly rather than shipping the weights.
  **Read before fitting:** `TrainingExample.from_values` reads
  `outcome.max_realizable_multiple or outcome.max_multiple_from_t0 or 0.0`, and
  P1-03 deliberately leaves both `None` on the 181 launches whose t0 price is
  unknown (DEC-008). Filter on a non-null multiple first, or a quarter of the
  corpus enters the fit as confident zeroes.
  _Depends on: P1-03, P3-01._
  _Accept:_ `python -m botsensai.cli fit --min-samples 200` exits 0 (or exits 0 with a clear "insufficient data" message when the corpus is still too small).

- [ ] **P4-02 — Heuristic formation from post-mortems**
  The bot writes post-mortems but never generalizes from them. Add a job that
  clusters post-mortems by exit reason and metric profile and proposes
  `HEURISTIC` memories, with confidence set from the size of the supporting
  cluster. Every proposed heuristic must cite the trades it came from.
  _Depends on: P1-03._
  _Accept:_ `python -m pytest tests/test_heuristics.py -q` exits 0 and asserts every generated heuristic has non-empty `evidence`.

- [ ] **P4-03 — Copytrade wallet discovery**
  `smart_wallet_participation` reads `ctx.extra["wallet_skill"]`, which nothing
  populates. Build the skill scores from collected trade history: realized PnL
  and entry earliness on tokens that subsequently ran, computed strictly from
  trades closed before each evaluation point.
  _Depends on: P1-03, P1-04._
  _Accept:_ `python -m pytest tests/test_wallet_skill.py -q` exits 0, including a look-ahead assertion.

- [ ] **P4-04 — Regime-conditional weight sets**
  Fit and store separate weightings for hot, normal and dead regimes, and
  select at scoring time. Only ship this if each regime has at least 200
  labelled samples of its own; otherwise record why it was skipped.
  _Depends on: P4-01._
  _Accept:_ `python -m pytest tests/test_regime_weights.py -q` exits 0.

---

## Phase 5 — Operate

- [x] **P5-01 — Live status dashboard**
  Done 2026-07-30. `botsensai dashboard --out FILE`. Adds a collection-integrity
  panel not in the original spec: five silent collection bugs on 2026-07-29
  motivated making integrity the primary panel rather than scores.
  Regeneration on every sweep is not wired yet.
  A single self-contained HTML file, showing current candidates with scores and
  vetoes, collector runs, social-post reachability, and metric coverage.
  No server, no build step, no external assets.
  _Depends on: P1-01._
  _Accept:_ `python -m botsensai.cli dashboard --out /tmp/dash.html && python -c "import pathlib; h=pathlib.Path('/tmp/dash.html').read_text(); assert '<html' in h and len(h)>5000"` exits 0.

- [ ] **P5-02 — Scheduled content generation**
  Hourly recap and per-entry alerts written to the content directory, with the
  disclosure guardrails already in `media/generator.py` enforced. Deduplicate
  against `MemoryKind.OBSERVATION` records so the same token is not written up
  twice in a day.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_content_scheduling.py -q` exits 0 and asserts no duplicate publication within 24h.

- [ ] **P5-03 — Paper-trading track record**
  Run paper trading continuously and publish a rolling performance page: every
  entry, its score, its outcome, and cumulative expectancy with a confidence
  interval. This is the artifact that decides whether any of this works.
  _Depends on: P1-01, P3-03._
  _Accept:_ `python -m botsensai.cli track-record --out docs/TRACK_RECORD.md` exits 0 and the output contains a bootstrap confidence interval.

- [ ] **P5-04 — Kill-switch and alerting**
  Trip `RiskSettings.kill_switch` automatically on: daily loss limit breached,
  three consecutive collector sweeps fully degraded, or paper expectancy over
  the last 50 trades below a configured floor. Log loudly when it trips and
  require a manual reset.
  _Depends on: P5-03._
  _Accept:_ `python -m pytest tests/test_kill_switch.py -q` exits 0 for all three trip conditions.

---

## Notes for whoever picks this up

The honest state of the project: the machinery is built and tested, and the
edge is entirely unproven. Phases 1 through 3 exist to find out whether there is
one. It is a completely legitimate outcome for `docs/RESULTS.md` to conclude
that these 32 signals do not predict returns net of fees and slippage — that
finding is worth having, and it is the reason the backtester reports confidence
intervals instead of headline numbers.

Do not skip to phase 5. A dashboard over an unvalidated signal is a very
convincing way to lose money.
