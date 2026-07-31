# Botsensai

An agentic system that watches Solana memecoin launchpads, scores brand-new
tokens on **33 signals that standard token APIs do not publish**, backtests those
signals honestly, paper-trades them, and writes evidence-sourced content about
them.

It cannot trade real money. There is no signing code anywhere in this repository
and a test enforces that.

---

## Why 33 more numbers

Every market-data API reports the same things: price, volume, liquidity,
transaction count, holder count. All of them are cheap to manufacture. One person
with a script is two hundred holders and four hundred transactions before the
chart has a second candle. A system built on those numbers is not measuring the
token; it is measuring the marketing budget.

The signals here were chosen by a single criterion — **cost to fake** — and each
one carries a written account of how a team could fake it and what the
counter-measure is. That field is mandatory: registering a metric without it
raises.

| family | n | the question it answers |
|---|---|---|
| `onchain_topology` | 7 | How many *independent actors* are behind the holder set, not how many addresses? |
| `social_authenticity` | 9 | Did 400 people reply, or one person with 400 accounts? |
| `community_production` | 5 | Has anyone unconnected to the team done unpaid creative work for this? |
| `narrative` | 5 | Is the idea new, wanted right now, and findable? |
| `team_credibility` | 3 | What has this deployer done before, and what are they doing with their own bag right now? |
| `execution_quality` | 4 | Could a position our size actually be *exited*? |

`botsensai explain reply_template_ratio` prints any metric's thesis and its
gameability note.

---

## Install and first run

```bash
pip install -e ".[dev]"

botsensai doctor                      # which surfaces are actually reachable
botsensai metrics                     # the full suite
botsensai sweep                       # one live pass: discover → screen → enrich → score
botsensai backtest --synthetic        # prove the pipeline executes end to end

botsensai x-setup                     # optional: set up the logged-in X profile
botsensai x-session                   # check whether that session is still valid
```

Optional extras: `pip install -e ".[browser]" && playwright install chromium`
enables the web-use collectors. `pip install -e ".[ml]"` enables the weight
fitter's optional models.

### Checks

```bash
python scripts/backpressure.py        # every gate below, in one run, with the numbers
python -m pytest                      # the suite
python -m ruff check src tests scripts   # lint
python -m mypy src                    # types (see below for why `src` and not `tests`)
python scripts/audit.py               # dependency vulnerabilities, scoped to our closure
python scripts/benchmark.py           # how the store's read paths scale as the corpus grows
```

`scripts/backpressure.py` runs nine gates — tests, lint, typecheck, audit,
coverage, complexity, duplication, performance, metric registry — and prints
what each one measured rather than whether it was claimed. Exit 0 means every
gate is green. It exists because a check that has never been installed and a
check that passes are indistinguishable from a summary; both had happened.
Complexity comes from `ruff --select C901` and is a ratchet at the current worst
function, and duplication is a six-line sliding window over `src`; the two
thresholds and the coverage floor are constants at the top of the file with the
date they were measured.

Types are gated on `src` only. `tests/` and `scripts/` are linted but not
type-checked: mypy skips the body of an unannotated function by default, which
is most of the suite, so gating them would buy annotation churn rather than
safety. `[tool.mypy]` deliberately sets no `python_version` — pinning 3.11 on a
3.13 interpreter made mypy abort inside numpy's stubs before checking anything,
and ruff's `target-version` is what holds the 3.11 floor.

`scripts/audit.py` exists because bare `pip-audit` audits the whole
interpreter — on a conda base environment that is mostly packages this repo
neither depends on nor can fix. It exits 0 clean, 1 on findings, 2 if the
scanner itself is missing.

`scripts/benchmark.py` prints microseconds per call for each hot read path at
increasing corpus sizes. It is a measurement, not a gate; the gate is
`tests/test_performance.py`, which fails if any of those paths starts planning a
full table scan.

A live `botsensai sweep` on 2026-07-25 pulled 165 launches, screened them to
eight, collected 2,583 real trades enriching those, scored them, and entered
nothing — the top candidate scored 0.61 against a 0.68 threshold. That refusal is
the system working as designed. `botsensai doctor` reports 8 of 8 surfaces
reachable.

---

## How it works

```
discover      cheap, broad      ~165 launches/sweep from pump.fun, Dexscreener, GeckoTerminal
   ↓
screen        free, local       ~12 survivors — age, liquidity floor, launch effort, ticker contention
   ↓
enrich        expensive         trades, holders, security, social — in rank order until the budget runs out
   ↓
score         free              33 metrics → composite + hard veto gates
   ↓
decide        risk-gated        convex sizing, enforced limits, modelled fills
   ↓
remember      durable           regime notes, entry rationale, post-mortems
```

The screening step is load-bearing. The most valuable endpoint in the stack
allows 60 calls per minute against thousands of tokens per hour; enriching
everything exhausts it in seconds. Screening costs nothing and removes over 90%
of the feed.

`docs/ARCHITECTURE.md` has the full design. `docs/DATA_SOURCES.md` has every
endpoint, field name and rate limit, verified live rather than copied from a
tutorial — which matters here, because most published guides to the pump.fun API
now describe endpoints that return 404.

---

## The parts that exist to stop you fooling yourself

**Point-in-time correctness.** Every record carries `as_of` (when the fact was
true) and `observed_at` (when we learned it). The backtester filters on both.
That second clause blocks the subtlest leak in the system: a record backfilled
hours after a decision that could not possibly have used it.

**Absence is never bearishness.** A metric with insufficient inputs returns
`MISSING`, not `0.0`. Returning zero for "unknown" is a strong bearish claim made
from no data, and it is the bug that quietly poisons a composite score.

**Vetoes are refusals, not penalties.** A live mint authority, a deployer with
prior rugs, or an exit depth below the intended position size short-circuits the
composite entirely. Encoding these as weights would let a high enough score
override them.

**Fills are charged for properly.** Latency between decision and inclusion, curve
impact as a real constant-product integral, platform and LP fees, priority fees
and Jito tips, a transaction failure rate with fees still burned on failure, and
a probabilistic sandwich penalty. The same simulator serves the paper broker and
the backtester, so their results are comparable rather than being two different
fictions.

**Results come with their sample size.** `BacktestResult` reports a bootstrap
confidence interval rather than a Sharpe ratio, because the return distribution
here is far too skewed for Sharpe to mean anything, and it says so out loud when
the interval spans zero.

**Content cannot be published undisclosed.** The generator writes only from
sourced evidence, refuses to make price predictions, and enforces disclosure
after rendering rather than trusting its own templates.

**A success with an empty body is a failure.** Three widely-used X endpoints
return HTTP 200 with zero bytes. A collector that trusts the status code records
"no social activity" for every token forever and never raises an alarm — which
reads downstream as a bearish claim about the entire market. Empty successes
raise, and a test guards the known-dead endpoints by name.

**Program accounts are not holders.** The bonding-curve account, AMM pool vaults
and the burn address are excluded before any concentration figure is computed.
Include them and every healthy pre-graduation token reads as ~100% concentrated,
which would veto the entire population the system exists to trade.

**An authenticated session is verified, not assumed.** Reply text, bookmarks and
per-engager account ages exist on no free X path, so there is an optional
session-backed collector — gated behind three separate opt-ins, reading a Chrome
profile you logged into by hand. Botsensai never holds a credential, and a test
asserts no login or password-handling code exists. If the session expires the
collector says so and falls back visibly, because an expired session returns
empty results that are indistinguishable from a token nobody is talking about.
`docs/X_SESSION.md` has the setup and the honest account of what it costs.

---

## What is proven, and what is not

**Proven:** the pipeline runs against live endpoints across eight surfaces; the
metrics separate synthetic organic launches from manufactured pumps from rugs
with the expected ordering and margins; point-in-time correctness holds under
direct test; 108 tests pass and the linter is clean.

**Not proven:** that any of this predicts returns. There is no corpus of labelled
outcomes yet, the weights are unfitted priors, and the only backtests that have
run are over synthetic data — which demonstrates that the machinery executes and
says nothing whatsoever about profitability. Every synthetic result is labelled
as such in its own summary.

Closing that gap is the whole content of `@fix_plan.md`. It is a completely
legitimate outcome for the answer to turn out to be no.

---

## Continuing the build autonomously

The remaining work is decomposed into dependency-ordered tasks with
machine-checkable acceptance criteria, ready for ralph-orchestrator:

```bash
git init && git add -A && git commit -m "baseline"
ralph run --max-iterations 30 --max-cost 10.0
```

`PROMPT.md` is the loop spec — requirements, hard constraints, acceptance
commands, and a "signs" section carrying the corrections that were expensive to
learn the first time. `@fix_plan.md` is the queue, phased so that each phase
unblocks the next:

1. **Get real data on disk** — continuous collection, websocket ingestion,
   outcome labelling, wallet history, funding graphs.
2. **Raise metric coverage** — currently 38–55%; below 50% the composite is a guess.
3. **Backtest properly** — walk-forward with embargo, ablation, null-hypothesis
   baselines, fill-model calibration against observed slippage.
4. **Learn and adapt** — fit weights on labelled outcomes, form heuristics from
   post-mortems, discover copytrade wallets.
5. **Operate** — dashboard, scheduled content, published track record, kill switch.

Do not skip to phase 5. A dashboard over an unvalidated signal is a very
convincing way to lose money.

---

## Safety posture

- Live trading is **not implemented**. `LiveBroker` raises; `build_broker` raises
  in live mode; and `test_repository_contains_no_signing_code` fails the suite if
  signing-capable code appears.
- No key, keyfile path or API token is read from config. Secrets come from the
  environment only.
- Only public data is collected, at below-human request rates, with a circuit
  breaker per surface so a rate limit never escalates into a ban.
- Risk limits are enforced in the broker rather than advised in the strategy.
  Position caps, exposure caps, daily loss limits and trade rate limits reject
  orders outright.

Nothing here is financial advice, and nothing here should be pointed at real
money on the strength of a synthetic backtest.
# Botsensai-Agentic
