# Botsensai task queue

Worked top to bottom by the Ralph loop. One task per iteration. A task is done
only when its acceptance command exits 0 and the work is committed.

Phases are ordered so that each one unblocks the next. Resist the temptation to
jump to the interesting tasks in phase 4 — without phase 1 there is no data to
fit on, and fitting on nothing produces confident nonsense.

**IN PROGRESS**: _(none — the loop writes the task id here when it splits a task)_

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

- [ ] **P1-02 — pump.fun new-mint websocket ingestion**
  Subscribe to `wss://pumpportal.fun/api/data` (`subscribeNewToken`,
  `subscribeMigration`) and write launches the instant they mint rather than up
  to a poll interval late. Reconnect with exponential backoff. Latency to first
  observation is the point: record it.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_ws_ingest.py -q` exits 0 (test uses a local fake websocket server, not the live endpoint).

- [ ] **P1-03 — Outcome labeller**
  Add `botsensai label --min-age-hours 24` that walks launches older than the
  cutoff, reconstructs their price path from stored snapshots plus
  GeckoTerminal OHLCV, and writes `Outcome` rows. `max_realizable_multiple`
  must be computed against the liquidity actually available at each point, not
  the raw peak price — the peak is not an exit.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_labeller.py -q` exits 0, and the test asserts that for a token whose price spiked on 200 USD of liquidity, `max_realizable_multiple` is materially below `max_multiple_from_t0`.

- [ ] **P1-04 — Wallet prior-history index**
  `MetricContext.wallet_priors` is currently empty in the live path, which
  silently guts `fresh_wallet_ratio` and `sniper_supply_share`. Build a
  `wallet_profiles` maintainer that counts each wallet's trades strictly before
  a given timestamp, backed by the existing table and an in-memory LRU.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_wallet_priors.py -q` exits 0 and asserts the count for a wallet is time-restricted.

- [ ] **P1-05 — Funding-source resolution**
  `HolderRecord.funded_by` is the input to `funder_graph_dispersion`,
  `holder_distribution_health` and the deployer-cluster logic, and nothing
  populates it live. Resolve each holder's first inbound SOL transfer via
  Helius or RPC `getSignaturesForAddress`, with a persistent cache since the
  answer never changes. Tag known CEX hot wallets so they are excluded from
  clustering rather than collapsing every user into one node.
  _Depends on: P1-01._
  _Accept:_ `python -m pytest tests/test_funding_graph.py -q` exits 0, including a case where a shared CEX funder does NOT merge two independent holders.

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
