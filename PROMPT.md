# Task: Take Botsensai from a working skeleton to a system with a real, measured edge

You are working on Botsensai, a Python monorepo that discovers brand-new Solana
memecoins, scores them on 32 signals that standard token APIs do not publish,
backtests those signals honestly, paper-trades them, and writes evidence-sourced
content about them.

The skeleton is built and every test passes. What it does **not** have is a
corpus of real data, weights fitted on real outcomes, or any evidence that the
signals predict anything. Your job is to close that gap, in the order given in
`@fix_plan.md`.

## Read these first, every iteration

- `@fix_plan.md` — the task queue. Work the first unchecked task. Nothing else.
- `docs/ARCHITECTURE.md` — how the pieces fit and why.
- `docs/DATA_SOURCES.md` — verified endpoints, field names and rate limits.
- `git log --oneline -15` — what the last iterations actually did.

You have no memory of previous iterations. Everything you need is on disk.

## Requirements

- Python 3.11+, installed with `pip install -e ".[dev]"`.
- New code goes under `src/botsensai/`, new tests under `tests/`.
- Every new metric subclasses `botsensai.metrics.base.Metric`, is registered in
  `botsensai/metrics/__init__.py`, and **must** populate the `gameability` class
  attribute — registration raises without it, by design.
- Every new collector subclasses `botsensai.collectors.base.Collector`, returns a
  `CollectionResult`, and never raises out of `discover` or `enrich`. A dead
  surface sets `degraded=True`; it does not break the sweep.
- Every record carries both `as_of` and `observed_at`. Reads used by the
  backtester filter on both.

## Constraints

- **Never add signing or transaction-submission code.** `tests/test_scoring_and_execution.py::test_repository_contains_no_signing_code` enforces this and must keep passing. Live trading is out of scope for this repository.
- **Never commit a key, a keyfile path, an API token, or a `.env`.** Secrets are read from the environment only.
- **Never return `0.0` from a metric to mean "no data."** Return `(None, 0, reason)`. A zero is a strong bearish claim; absence is not.
- **Never widen a rate limit above the verified value** in `docs/DATA_SOURCES.md`. If you find a higher documented limit, verify it live, update the doc with the evidence, and only then change the code.
- **Never delete or weaken a failing test to make the suite pass.** If a test is genuinely wrong, fix the test and explain why in the commit message.
- **Never report a backtest result without its sample size and confidence interval.** `BacktestResult.summary()` already attaches the warnings; do not strip them.
- Do not add heavyweight dependencies (torch, transformers, a database server). numpy/pandas/scipy/networkx/scikit-learn/lightgbm are available and sufficient.

## Acceptance criteria

Every one of these is a command. Run them; do not assert them.

- `python -m pytest -q` exits 0 with zero failures.
- `python -m ruff check src tests` exits 0.
- `python -c "from botsensai.metrics import build_registry; r=build_registry(); assert len(r) >= 32, len(r)"` exits 0.
- `python -m botsensai.cli doctor` exits 0 and reports at least 4 reachable surfaces.
- `python -m botsensai.cli backtest --synthetic --universe 40` exits 0 and prints a result table.
- `python -m botsensai.cli metrics` exits 0 and lists every registered metric.
- Every task checked off in `@fix_plan.md` has its own acceptance command recorded there, and that command exits 0.

## Iteration rules

- Re-read `PROMPT.md`, `@fix_plan.md` and the relevant source at the start of every iteration. Assume you remember nothing.
- Work **one** task from `@fix_plan.md` per iteration — the first unchecked one whose dependencies are all checked.
- If a task in progress is described in `@fix_plan.md` under "IN PROGRESS", finish that before starting anything new.
- If a task turns out to be too large for one context window, do not half-build it. Split it into two or more smaller tasks in `@fix_plan.md`, commit the split, and end the iteration.
- Before ending an iteration: run the full acceptance-criteria list above, then `git add -A && git commit`. Never end an iteration with uncommitted work.
- In your output, state **specifically** what changed this iteration: which task id, which files, which new tests, and the actual numbers any command printed. Do not write "still working on it" — identical outputs across iterations trip loop detection and kill the run.
- If you are blocked because a data source is unreachable, do not retry it in a loop. Mark the task blocked in `@fix_plan.md` with the error text, move to the next unblocked task, and commit.

## Signs

Short corrections for failure modes seen in previous runs. Read them; they are cheaper than rediscovering the same mistakes.

- **A backtest that looks great is a bug until proven otherwise.** Median memecoin outcome is total loss. If a change makes results dramatically better, your first hypothesis is a look-ahead leak, not an insight. Check `observed_at` filtering and time-restricted deployer history before celebrating.
- **Metric coverage is the real constraint, not metric count.** Adding a 33rd metric that produces a usable value 5% of the time is worse than raising an existing metric's coverage from 30% to 80%. Check `BacktestResult.metric_coverage` before proposing new signals.
- **Fitting on fewer than 200 labelled outcomes is not fitting.** `WeightFitter` refuses below that threshold on purpose. If you want fitted weights, collect more data first; do not lower the threshold.
- **Rate limits are the scarcest resource in the system.** Before adding a call to a sweep, work out what it costs against the budget in `docs/DATA_SOURCES.md` and what it displaces. The 60/min hosts are effectively a handful of tokens per sweep.
- **Prefer raising coverage on the on-chain and topology families over the social ones.** They are cheaper to collect, harder to game, and currently better instrumented.
- **A 2xx with an empty body is a failure, not an absence.** Three X endpoints return HTTP 200 with zero bytes. Any collector that records that as "no activity" poisons every downstream metric silently and forever. New collectors must route success responses through a body check, and `tests/test_collectors.py` guards the known-dead endpoints by name.
- **Verify a rate limit before trusting a published one.** `syndication.twitter.com` hard-429s after twelve requests per fifteen minutes and stays blocked for ten; `cdn.syndication.twimg.com` showed no throttling at forty consecutive calls. Those are the same product. Measure, then encode the measurement in config with a comment saying when it was measured.
- **Exclude program-owned accounts before computing any concentration figure.** The bonding-curve ATA, AMM pool vaults and the burn address are not holders. Include them and every healthy pre-graduation token reads as ~100% concentrated. Use `metrics.topology.tradeable_holders`, never `ctx.holders` directly.
- **Decode HTML entities before extracting tickers.** 4chan and Telegram both encode `$` as `&#036;`; strip tags without decoding and the cashtag regex silently matches nothing while everything appears to work.
- **The base rate drifts by more than half.** pump.fun graduation fell from under 2% to roughly 0.63% between late 2024 and late 2025. Never hardcode a threshold against it — `ScoringSettings.regime_*_graduation_rate` carries the anchor date for a reason.

## Status

- [ ] `python -m pytest -q` passes
- [ ] `python -m ruff check src tests` passes
- [ ] All tasks in `@fix_plan.md` are checked off
- [ ] `docs/RESULTS.md` exists and reports a walk-forward backtest over real collected data, with sample size and confidence interval
- [ ] TASK_COMPLETE
