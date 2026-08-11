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

- [x] **P2-01 — X search depth via web-use** *(done 2026-08-04)*
  `WebUseDriver.harvest_json` drains captured responses *between* scrolls and
  lets the caller stop, which is the capability `capture_json` lacked: it
  returns everything only after the last scroll, by which point the cost is
  already paid. `XCollector.harvest_posts` absorbs each round, dedupes by post
  id, and stops on the target (200), the deadline, or 2 idle scrolls. Shared by
  the anonymous path, the session search and the session conversation reader, so
  all three dedupe identically.
  Two departures from the task text, both deliberate:
  - The 3-minute ceiling applies to the session path; anonymous gets 25s.
    MEASURED live: an anonymous `$CHEEMS` search returns 0 posts in 9.0s
    because x.com/search serves a logged-out visitor a login wall. See DEC-013.
  - Dedupe keeps the *richest* parse of a post, not the first. The walker yields
    both a tweet node and its own `legacy` sub-dict; keeping the wrong one files
    the post under `unknown` with no creation date, which is a fabricated author,
    not a lost one.
  `AuthenticatedXCollector._enrich_budget_seconds` now derives from the search
  deadline instead of a flat 45s, or the base class would kill enrich mid-sweep
  the first time a harvest used its ceiling.
  Depth is only *real* with `x_session` configured; anonymously this remains a
  login wall, and the plan should not pretend otherwise.
  _Depends on: none._
  _Accept:_ `python -m pytest tests/test_x_collector.py -q` exits 0 against recorded fixtures in `tests/fixtures/x/`. ✔ 19 passed

- [x] **P2-02 — Perceptual image hashing** *(done 2026-08-04)*
  `media/phash.py` is the 64-bit DCT hash the task asked for — area-average to
  32x32, numpy DCT-II, threshold on the median of the 63 non-DC coefficients —
  and `media/hasher.py` fetches on a budget: thumbnails, a byte cap enforced
  *while streaming*, a per-post and per-sweep image count, a wall-clock deadline
  and an LRU cache by URL. Wired into `Pipeline.enrich` before `insert_posts`,
  because that is the only path into the store and a post row has no key to come
  back to.
  Four things this needed that the task text did not anticipate:
  - **There is no image library on this interpreter** — no PIL, no cv2, no
    imageio. `media/imagecodec.py` decodes PNG (zlib + all five scanline
    filters) and JPEG **to its DC coefficients only**, which is the image at
    exactly 1/8 scale for no inverse DCT at all. See DEC-014.
  - **X serves progressive JPEGs — all of them.** MEASURED: every image
    `pbs.twimg.com` returned was SOF2. The first version of this decoder refused
    progressive and passed its whole test suite while producing *zero* coverage
    on the surface that matters most. Progressive turns out to be the *cheaper*
    format here: its DC coefficients are a leading scan of their own, so there
    are no AC terms to decode and discard.
  - **`derivative_remix_depth` had to change with it.** It bucketed by hash
    prefix, which is meaningless for perceptual hashes — near-identical images
    differ by a few bits in arbitrary positions and so almost never share a
    prefix. Now single-linkage Hamming clustering at ≤10 bits of 64.
  - **4chan's native MD5 is not a perceptual hash** and is now stored as
    `md5:<digest>` while perceptual ones are `p:<digest>`. Unnamespaced, eight
    re-encodes of one picture read as eight visual ideas — the exact artefact
    the metric exists to avoid.
  MEASURED live: 39 of 40 real /biz/ catalog images hashed in 2.7s (the failure
  was a 404 on a deleted post); unrelated /biz/ images sit 16–44 bits apart,
  median 32, with 0 of 741 pairs inside the clustering threshold. Real X media
  hashes after the progressive fix. Decode cost 1.2ms at 96px, 9.2ms at 256px,
  863ms for a noisy 1200x1200 — which is what the thumbnail preference is for.
  Known limits, written down rather than smoothed over: GIF and WebP are refused
  (4chan thumbnails are always JPEG, so this costs little today); arithmetic and
  lossless JPEG are refused; a progressive file whose first scan is not its DC
  scan is refused rather than approximated.
  _Depends on: none._
  _Accept:_ `python -m pytest tests/test_phash.py -q` exits 0, asserting that a resized and re-encoded copy of an image hashes within Hamming distance 6, and an unrelated image does not. ✔ 55 passed; alpha.png vs alpha_small.jpg (96px, quality 35) = **2**, alpha vs beta = **34**

- [x] **P2-03 — Instagram and TikTok collectors** *(done 2026-08-04)*
  `collectors/longtail.py` adds `InstagramCollector` and `TikTokCollector`, both
  registered in `ALL_COLLECTORS` and in the default sweep. The task said to
  expect frequent failure and make degradation normal; the live probes said it
  more strongly than that, so most of the work is in failing *correctly*.

  MEASURED live, headless anonymous, 2026-08-04 (probes in `/var/tmp/p2_03_probe*.py`):
  - **Instagram is refused in two disguises.** `/explore/tags/memecoin/`
    redirects to `/accounts/login/`; `/explore/tags/wif/`, in the same run,
    redirects to `/popular/wif/` — **HTTP 200 with 15 KB of encyclopedia prose
    about Wi-Fi** and zero posts. The second is the more dangerous, because it
    does not look like a failure. `api/v1/tags/web_info/` answers **200 with
    `text/html`**, 605 KB of login shell.
  - **TikTok gates everything token-scoped.** `/tag/{tag}` → `api/challenge/detail/`
    **403** (identical under a real Chrome UA, so not UA sniffing); `/search` is
    login-gated by TikTok's own `searchVideoForLoggedin` flag; `/explore` pays
    out **one** 200 of 7 items per fresh browser context then **50 consecutive
    403s**, and is not token-scoped anyway.
  - **`oembed` works**: 20/20 200s in 5.2 s (~230/min), bogus id → clean 400.
    Clamped to 60/min in `Settings`.
  - **TikTok ids are snowflakes**: `id >> 32` is unix seconds, verified against
    a video of known age (`6718…5173` → 2019-07-27).

  Three departures from the task text, all forced by the measurements:
  - **Two dead hashtag pages would have left the metric exactly as blind.** The
    route that works is *other people's posts*:
    `Pipeline._follow_offplatform_links` pulls `tiktok.com` links out of the X,
    Telegram, 4chan and pump.fun-chat text already collected and resolves each
    through oembed (12 per sweep). The snowflake decode is what makes a bare
    link into dated evidence.
  - **Neither collector opens a page without a session.** Ten seconds per token
    to rediscover a measured wall displaces collection that works, so the
    anonymous case degrades immediately — but it *does* degrade, by name,
    because `cross_platform_propagation_lag` scores absence bearishly and a
    blind surface would manufacture that reading.
  - **A driver bug had to be fixed to make this reliable.** `PageResult.final_url`
    was read at `domcontentloaded`, before the page's own JavaScript ran, so
    Instagram's post-hydration redirect was visible only when it won a race. The
    same collector saw `/accounts/login/` on one visit and the original path on
    the next. `_drive_page` now re-reads the URL after the waits.

  25 mutation probes against the guards, all red, including four that were green
  on the first pass and had to be fixed: a walker depth limit tighter than
  Instagram's own GraphQL envelope (so one of the two shapes silently parsed to
  nothing), a redundant budget branch that was decoration rather than a guard,
  an unreachable-date check nothing exercised, and a `/popular/` check that was
  passing on a different signal than the one it named.
  _Depends on: P2-01._
  _Accept:_ `python -m pytest tests/test_social_longtail.py -q` exits 0 against fixtures; `python -m botsensai.cli doctor` still exits 0 when both are unreachable. ✔ **36 passed**; doctor exit 0 both live (9/10 reachable — instagram down, tiktok ok) and with all network blocked (2/10 reachable, exit 0)

- [x] **P2-04 — Curate the Telegram call-channel watchlist**
  The reader works; what is missing is the list. Seed
  `collectors.telegram.extra.call_channels` from Dexscreener `links` of type
  `telegram` plus manual curation, then measure each channel's lead time against
  subsequent price action so the useless ones can be dropped.
  Shipped as `botsensai/watchlist.py` plus `Database.social_links` /
  `posts_by_platform` / `posts_matching`, wired into
  `Pipeline._wire_social_handles` (which had been setting `call_channels` to a
  literal `[]` every sweep) and exposed as `botsensai.cli channels`.
  Seeding costs no requests: Dexscreener's `info.socials` entry of type
  `telegram` is already folded into `Launch.telegram`, and the discriminator
  between a token's own room and a call room is **reuse** — measured on the real
  store, `t.me/CRYPTOLIQUIDBNB` is the published link of 7 distinct tokens and
  `t.me/pisklauren` of 3, while every other link in 120 launches belongs to
  exactly one. Ranking reuses the labeller's `choose_denomination` →
  `points_from_snapshots` → `collapse_path` rather than reading `market_snapshots`
  as a path, because a naive `MAX(price)` after a call scores the 6.8-million-x
  intra-sweep disagreements that `collapse_path` exists to absorb. A channel is
  dropped only on positive evidence (>=3 measured calls, median peak multiple
  <=1.0); one that was never measurable is reported unmeasured and kept.
  21 mutation probes against the guards, all red — four were green on the first
  pass and were real holes: a `startswith("+")` invite check that `_HANDLE_RE`
  already refused (decoration, removed), a `median is not None` clause nothing
  reached until `min_calls=0`, LIKE-wildcard escaping tested with a fragment
  containing no wildcards, and the collector's own `[:6]` channel budget, which
  had never been exercised since it was written.
  _Depends on: P1-01._
  _Accept:_ `python -m botsensai.cli channels --rank` exits 0 and prints per-channel median lead time in seconds. ✔ exit 0; `corgiportalonsol` 3 calls / 3 measured / median lead **6432 s** / median peak 6.99x / 100% led, `makepumpga` 1 call / median lead **7415 s** / 1.00x / 0% led, and 5 channels reported unmeasured rather than as zeros. `python -m pytest tests/test_channel_watchlist.py -q` → 52 passed. Live-read the three seeded channels 2026-08-04: `cryptoliquidbnb` 4 messages in 0.97 s, `www_usdolly_rocks` 8 messages in 0.33 s (2 naming tokens), `pisklauren` 0 — a discovered handle can be unreadable, and nothing drops it yet (see P2-07).

- [x] **P2-07 — Drop unreadable call channels**
  Found while accepting P2-04: `t.me/pisklauren` is the published Telegram link
  of three distinct launches, so it is discovered and seeded, and it returns zero
  messages every time. `is_useless` cannot drop it — it drops on a bad *price*
  record, and a channel that never yields a message never makes a call. So an
  unreadable handle holds one of six watchlist slots and spends a fetch per sweep
  forever. The fix needs per-channel history, which nothing stores today:
  `collector_runs` is per surface. Add a per-channel read record and drop on
  *persistent* failure only — a transient fetch failure must not evict a channel.
  Shipped as `models.ChannelRead` + the `channel_reads` table (schema 6) +
  `Database.record_channel_reads` / `channel_reads`, written by
  `Pipeline.enrich` from what `TelegramChannelCollector.read_channel` reports,
  and consumed by `watchlist.empty_read_streak` → `ChannelRank.empty_reads` →
  `drop_reason`, which `is_useless` now delegates to.
  A live probe (`/var/tmp/p2_07_probe.py`) decided the design: `t.me/s/` has **no
  404 anywhere**. An unreadable handle answers HTTP 200 with a real page and zero
  messages — `pisklauren` 11,595 bytes ("View @pisklauren"), an unregistered
  handle 9,897 bytes ("Contact @…") — while `durov` returns 20 messages and
  `cryptoliquidbnb` 4. So a *failed fetch carries no information about a channel*
  and is skipped rather than counted: counting it would evict the entire
  watchlist on one bad night, since every channel fails together when the surface
  does. Streaks are bounded to the same 30-day window as the calls, so an
  eviction expires and a room that goes public later earns fresh attempts.
  16 mutation probes against the guards, all red — three were green on the first
  pass and all three were real: `rank_channels` dropped the expiry window on its
  way to `channel_read_streaks` (making every eviction permanent), `read_channels`
  returned raw store keys so `joinchat` and `Foo`-vs-`foo` would rank as channels,
  and the CLI's ranked set omitted the attempted-but-never-collected channels —
  the only ones the report exists to explain.
  _Depends on: P2-04._
  _Accept:_ `python -m botsensai.cli channels --rank` marks a channel with N consecutive empty reads and excludes it from the seeded watchlist; a channel with one empty read is still seeded. ✔ Exercised live on the real store: three spaced reads of the seeded watchlist (`/var/tmp/p2_07_live.py`, 60 s apart to clear the 45 s response cache) recorded `cryptoliquidbnb` 4/4/4 messages, `www_usdolly_rocks` 8/8/8, `pisklauren` **0/0/0**; the command then exits 0 with `pisklauren` marked `!` at 3 empty reads, `dropped on evidence: pisklauren — 3 consecutive empty reads`, and `watchlist (2/6): cryptoliquidbnb, www_usdolly_rocks`. `python -m pytest tests/test_channel_watchlist.py -q` → 72 passed (20 new). Full suite 464 passed; 9/9 backpressure gates green, complexity still 10.

- [x] **P2-06 — Fast-follower and identity-discontinuity metrics**
  The X profile payload carries `fast_followers_count` (X's own count of
  burst-acquired followers) and enough history to detect a repurposed account —
  a large gap between `user.created_at` and the oldest retrievable post, with a
  low `statuses_count`, means the archive was wiped. Both are stronger evidence
  than the ratio heuristics currently standing in for them.
  The fast-follower half looked already done and was not: `purchased_follower_signal`
  existed, but its only input was `Pipeline._fast_follower_share`, a **process-local
  dict**. Nothing persisted it, so the metric was computable live and MISSING in
  every backtest — and `SocialAccount`, collected by `XCollector._timeline_leg`
  and `AuthenticatedXCollector.enrich`, was dropped on the floor because no table
  existed for it. Both halves therefore needed a store first: `social_accounts`
  (schema 7) + `Database.insert_accounts` / `accounts_as_of` →
  `MetricContext.accounts` / `.promoter`, with `token_key` and `role` on the row
  so an engager fleet is never scored as the token's own account. The session
  collector now also takes the promoter profile from the public syndication host,
  which is not a fallback: GraphQL's user object carries no `fast_followers_count`
  at all, so the authenticated path is the *weaker* source for the one field here.
  The plan's literal formulation does not work and was replaced (DEC-015). "Gap
  between `created_at` and the oldest retrievable post" is ~100% for **every
  account alive**, because the endpoint returns the head of a timeline and stops —
  measured, `@elonmusk` reads 100% silent over 6,272 days. The second clause is
  what rescues it: archive **coverage**, `timeline_posts / post_count`. A first
  implementation using the posting *rate* against the lifetime rate was withdrawn
  during testing — 40 posts inside an hour is 192x a prolific account's lifetime
  rate, so anyone posting a thread scored 0.996. Coverage is burst-invariant and
  `test_identity_discontinuity_is_burst_invariant` keeps it that way. The window
  is measured in the collector *before* `_about_this_token` filtering and stored
  on the account row, because dropping an account's off-topic posts has the exact
  fingerprint of a wiped archive.
  13 mutation probes against the guards, all red. Three were green on the first
  pass and all three were real: the silent-prefix term was unexercised because
  every fixture had `silent_share ≈ 1.0`; the freshest-snapshot pick passed under
  a plain `promoters[0]` because the fixture happened to list the fresh row first;
  and the outer `observed_at <= ?` in `accounts_as_of` was genuinely redundant
  beside the bounded subquery, so it was removed rather than tested.
  _Depends on: P2-01._
  _Accept:_ `python -m pytest tests/test_metrics.py -q -k "fast_follower or discontinuity"` exits 0. ✔ **14 passed, 30 deselected**. Full suite **484 passed** (23 new), ruff clean, mypy clean, registry **34** metrics. Live-measured 2026-08-04: `@elonmusk` 365,918 bytes → 40 posts, `post_count` 106,633, joined 2009-06-02; through the real store and metric that is raw **0.000375** → normalized **0.9425** (clean), while the same 40-post window against a 45-post five-year-old account is raw **0.888706** → normalized **0.0133** — identical 100% silent prefixes, so coverage is doing all the work. `@SPIDORKMEME`, published as a real launch's X link, answers **2,227 bytes** with no user object: a published link is not an account, and 4 of the 4 newest handle-shaped links resolved to nothing. Ceiling on the store: **599/1248 launches (48%)** publish a handle-shaped X link.
  _Follow-up for P2-05:_ both promoter metrics report **0% coverage in a synthetic
  backtest**, because `generate_token` produces posts but no accounts. That is a
  fixture gap, not a metric gap, and it was deliberately not papered over —
  see DEC-016. Whoever sets the coverage baseline should either complete the
  fixture or record the baseline with these two excluded and say so.

- [x] **P2-05 — Coverage regression gate**
  Add a test that runs a synthetic backtest and fails if mean metric coverage
  drops below the current committed baseline. Record the baseline in
  `docs/RESULTS.md`. This stops coverage silently rotting as collectors drift.
  The gate could not be built as specified until the fixture under it was
  reproducible, and it was not (DEC-017): `generate_token` seeded its RNG with
  `hash(archetype)`, and `hash` of a `str` is salted by `PYTHONHASHSEED`, while
  `_wallet_ages` iterated a *set* of wallet strings and so drew from the RNG in
  a per-process order. A generator documented as seeded produced different
  tokens in every process — measured across two hash seeds,
  `derivative_remix_depth` read **0.0646 and 0.0413** on the same command, a 36%
  relative swing. Fixed to `ARCHETYPES.index(archetype)` and `dict.fromkeys`;
  identical coverage now across three hash seeds.
  The mean alone is not the gate the task description implies. Over 30
  participating metrics one metric can lose **0.30** of coverage before the mean
  moves by `MEAN_TOLERANCE` (0.01), so there is also a per-metric floor
  (`METRIC_TOLERANCE` 0.02) — measured, raising `holder_distribution_health`'s
  minimum from 8 tradeable holders to 20 drops it 0.9833 → 0.9517 and moves the
  mean by 0.0010: mean gate green, per-metric gate red and naming the metric.
  Four metrics read exactly 0.0 in any synthetic run and are excluded from the
  headline mean and recorded with the note the metric itself emitted, because
  "0.0 for want of a fixture" and "0.0 because the collector died" are opposite
  findings that look identical in a coverage table. This closes P2-06's
  follow-up: the two promoter metrics are recorded as excluded, with the reason
  and with the real ceiling (599/1248 launches, 48%) beside them, rather than
  papered over — `BacktestResult.absence_reasons` is new and is what makes that
  distinction machine-readable instead of editorial.
  13 mutation probes; 12 red first pass, and the one that went green was a bad
  probe rather than a hole (holders 8 → 12 is not a degradation — synthetic
  tokens comfortably carry 12 holders), re-run at 20 and red. The probe run also
  exposed a trap worth naming: a same-length mutation restored inside one second
  leaves a **valid `.pyc`** (invalidation is on source mtime-seconds + size), so
  the mutated module goes on being imported after the restore. It made one probe
  measure a universe-20 run against a universe-40 baseline. Probe harnesses in
  this repo must purge `__pycache__` around every mutation.
  _Depends on: P2-01, P2-02._
  _Accept:_ `python -m pytest tests/test_coverage_gate.py -q` exits 0. ✔ **19 passed** in 17.0 s. Baseline recorded at universe 40 / seed 1337 / pinned `created_at` 2026-07-01T12:00:00Z: **2400 evaluations, 30/34 metrics participating, mean coverage 0.8846**. `python scripts/coverage_baseline.py` (no `--write`) exits 0 with `recorded baseline matches`. Full suite **503 passed** (19 new), ruff clean, mypy clean, registry 34, `doctor` 9/10 surfaces reachable, `backtest --synthetic --universe 40` exits 0.

---

## Phase 3 — Backtest properly

- [x] **P3-01 — Walk-forward harness**
  Done 2026-08-05. `backtest/walkforward.py`: `plan_folds` / `split_tapes` /
  `WalkForward` / `WalkForwardReport`. Fit on a trailing train window, score the
  next test window with those weights alone, step, repeat; only test windows are
  reported.
  **The assertion this task names is nearly free, and that is the finding.**
  Fold membership is decided by `launch.created_at`, so a fold's train and test
  ranges are disjoint *by construction* — "no token in both" cannot fail unless
  the window arithmetic is broken outright. The reachable leak is that a token
  launched near `train_end` has no label until its outcome horizon elapses, and
  that instant lands inside the test window. So the embargo is a **purge on the
  train side**, `admissible iff created_at + label_horizon + embargo <=
  test_start`, and `label_horizon` is `LabelPolicy.min_age_hours` (the instant
  the label becomes knowable) and deliberately not `Outcome.labeled_at` (the
  wall-clock moment the batch labeller happened to run, which for a corpus
  labelled in one pass would purge every fold to empty for a reason unrelated to
  information).
  Three things the probe round changed rather than confirmed. (1)
  `WalkForwardReport.summary` reported the `(0.0, 0.0)` that
  `bootstrap_expectancy_ci` returns below 5 trades as though it were an
  interval, so every under-sampled run printed "not distinguishable from no
  edge" — a verdict on the strategy, where the truth was that nothing was
  measured. The threshold is now named (`BOOTSTRAP_MIN_TRADES`) and absence is
  reported as absence. (2) `Fold.holds_train` carried a `< train_end` term that
  could never fire, since `train_cutoff = test_start - purge` is at or before
  `train_end` for any non-negative purge; it is removed, and the invariant is
  enforced where it can actually be violated — `plan_folds` now refuses a
  negative embargo, which is the one setting that pushes the cutoff *into* the
  test window and would fit on the tokens about to be graded while still
  labelling the result out-of-sample. (3) Dropping the `train_start` term left
  all nineteen other tests green: `train_days` would have stopped meaning
  anything and the harness would have gone expanding while `fold_table` printed
  a rolling window. `test_the_train_window_rolls_rather_than_expands` closes it.
  `Backtester.tapes_from_synthetic` takes an optional `outcomes` mapping;
  without it every train fold is empty and every fold silently runs on default
  weights, since `split_tapes` drops unlabelled train tapes (an unlabelled tape
  does not enter a fit as missing — `TrainingExample.from_values` reads
  `... or 0.0` and it enters as a confident zero).
  15 mutation probes, 15 red. Library-only so far: no CLI command yet, which
  P3-02's `ablate` and P3-03's `--baselines` are the natural place for.
  _Depends on: P1-03._
  _Accept:_ `python -m pytest tests/test_walkforward.py -q` exits 0, including an assertion that no token key appears in both a train and its own test fold. ✔ **22 passed** in 5.5 s. Pinned fixture: universe 24 / seed 1337 / `created_at` 2026-07-01T12:00:00Z / one launch an hour → 2 folds, train 10 and 6 tokens, test 6 each, 0/2 fitted (24 tokens cannot reach `MIN_SAMPLES_TO_FIT` 200, and the report says so rather than passing `v0-default` off as a fit). Full suite **525 passed** (22 new), ruff clean, mypy clean over 59 files, `scripts/backpressure.py` 9/9 green with complexity 10, registry 34, `doctor` 9/10 surfaces, `backtest --synthetic --universe 40` exits 0 (7 trades, expectancy +0.007801, CI [+0.003356, +0.011626]).

- [x] **P3-02 — Ablation report**
  Done 2026-08-09. `botsensai ablate --synthetic` (or `--out FILE`) runs
  `scoring.fit.ablation`, prints a 34-metric table sorted measured-first by
  objective contribution, highlights liability metrics in red with a warning
  summary, and outputs caveats.
  _Depends on: P3-01._
  _Accept:_ `python -m botsensai.cli ablate --synthetic` exits 0 and prints one row per registered metric. ✔ Exit 0, 34 rows printed, liabilities flagged loudly. `tests/test_ablation.py` passes 2/2 tests. Full suite 527 passed.

- [x] **P3-03 — Null-hypothesis baselines**
  Done 2026-08-10. Baseline strategies `buy_everything`, `random_entry`, and `buy_highest_volume` in `botsensai.backtest.baselines` wired to `botsensai backtest --baselines`.
  _Depends on: P3-01._
  _Accept:_ `python -m botsensai.cli backtest --synthetic --baselines` exits 0 and prints a comparison table including all three baselines. ✔ Exit 0, printed 3 baseline modes comparison table against composite strategy. `tests/test_baselines.py` passes 5/5 tests.

- [x] **P3-04 — Fill-model calibration against reality**
  Done 2026-08-10. `botsensai.execution.calibration`: `evaluate_fill_calibration` and `calibrate_execution_settings` compare observed trade slippage to simulator output across matched trade-snapshot pairs.
  Evaluated over 188 matched trade-snapshot buy pairs in `data/botsensai.db`: observed mean slippage 531.61 bps vs modelled mean slippage 2065.88 bps (`is_optimistic = False`). Documented in `docs/RESULTS.md`.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_fill_calibration.py -q` exits 0. ✔ Exit 0, 4 tests passed.

---

## Phase 4 — Learn and adapt

- [x] **P4-01 — Fit weights on real labelled outcomes**
  Done 2026-08-10. `botsensai fit --min-samples 200` runs coordinate ascent over labelled outcomes with non-null multiples.
  Evaluated over 966 labelled tokens in `data/botsensai.db`: train rank correlation 0.6128, holdout rank correlation 0.5897, top-decile lift 1.0170. Fitted weights saved to `config/weights.json`. Documented in `docs/RESULTS.md`.
  _Depends on: P1-03, P3-01._
  _Accept:_ `python -m botsensai.cli fit --min-samples 200` exits 0. ✔ Exit 0, 966 samples, holdout rank correlation 0.5897. `tests/test_fit_cli.py` passes 2/2 tests.

- [x] **P4-02 — Heuristic formation from post-mortems**
  Done 2026-08-10. `botsensai.memory.heuristics`: `form_heuristics_from_postmortems` clusters post-mortems by exit reason and metric profile, sets confidence based on cluster size, and cites post-mortems and trade evidence.
  _Depends on: P1-03._
  _Accept:_ `python -m pytest tests/test_heuristics.py -q` exits 0 and asserts every generated heuristic has non-empty `evidence`. ✔ Exit 0, 5 tests passed.


- [x] **P4-03 — Copytrade wallet discovery**
  Done 2026-08-10. `botsensai.onchain.wallet_skill`: `WalletSkillIndex` computes point-in-time realized PnL and entry earliness on runners for wallets, strictly bounded by `as_of < before` and `observed_at <= observed_before`. `Database.wallet_trades_before` fetches past trade history. Wired into `Pipeline.build_context` and `Backtester` to populate `ctx.extra["wallet_skill"]` and `ctx.extra["wallet_typical_size"]`.
  _Depends on: P1-03, P1-04._
  _Accept:_ `python -m pytest tests/test_wallet_skill.py -q` exits 0, including a look-ahead assertion. ✔ Exit 0, 4 tests passed.

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
