# Architecture

## The problem this shape solves

Three constraints drive every decision in this codebase.

**The decision window is minutes.** A pump.fun token's entire life is often under
an hour. Whatever you are going to know about it, you have to know it in the
first few minutes, which rules out anything requiring history.

**The data is adversarial.** Every number a token emits can be manufactured by
someone who profits from you believing it. Holder count, engagement, volume,
transaction count — all cheap to fake. A system built on those is not measuring
the token, it is measuring the marketing budget.

**The rate limits are brutally tight.** The most valuable endpoint in the stack
allows 60 calls per minute against a population of thousands of tokens per hour.
Spending it uniformly exhausts it in seconds.

So: a funnel that spends free operations broadly and expensive ones narrowly, a
metric suite chosen for cost-to-fake rather than for availability, and a
backtester built to disprove rather than to confirm.

---

## Layers

```
collectors/     surfaces → normalized records        (rate-limited, degradable)
store/          records → SQLite, point-in-time      (as_of AND observed_at)
metrics/        context → 34 signals                  (pure functions)
scoring/        signals → one decision                (weights, vetoes, regime)
execution/      decision → fills                      (curve impact, fees, failure)
backtest/       history → an honest verdict           (walk-forward, CI, baselines)
media/          decision → sourced writing            (disclosure enforced)
memory/         everything → durable notes            (time-bounded recall)
pipeline.py     the loop that connects them
```

Dependencies point strictly downward. `metrics/` cannot reach the database;
`scoring/` cannot reach a collector. That is what makes the whole stack testable
against fixtures and replayable in a backtest.

---

## The funnel

```
discover      cheap, broad     ~165 launches/sweep from 3 venues
   ↓
screen        free, local      ~12 survivors — age, liquidity floor,
                               launch-effort, ticker contention
   ↓
enrich        expensive        trades, holders, security, social —
                               spent in rank order until budget runs out
   ↓
score         free             34 metrics → composite + vetoes
   ↓
decide        risk-gated       convex sizing, hard limits, paper fills
   ↓
remember      durable          regime notes, entries, post-mortems
```

The screening step is the load-bearing one. Without it the 60/min budgets are
gone in seconds and the system learns nothing about anything. It costs zero API
calls and typically removes over 90% of the feed.

---

## Point-in-time correctness

Every record carries two timestamps:

- **`as_of`** — when the underlying fact was true.
- **`observed_at`** — when Botsensai learned it.

The backtester filters on **both**. That second clause is what stops the subtlest
look-ahead vector in the system: a record whose `as_of` is in the past but which
was backfilled hours later. Filtering on `as_of` alone lets that data into a
decision that could not have used it, and the result is a backtest that cannot
be reproduced live.

The same discipline applies elsewhere:

- Deployer history is queried `before=launch.created_at`. A deployer's full
  lifetime record is not knowable at decision time, and using it is the most
  common way this class of feature silently inflates results.
- Memory recall takes an `as_of` and will not return entries created after it.
  The guard lives in the store rather than trusting every caller.
- Cross-sectional peer normalization accumulates *during* the replay, so a
  metric is only ever ranked against tokens the system had already seen.

There is one record type where the two timestamps genuinely collapse, and it is
worth naming so it is not read as a missing bound. A **profile snapshot**
(`social_accounts`) has no event time distinct from its observation: what an
account's follower count *was* is knowable only by having looked, so the instant
we looked is the instant the fact is about. `accounts_as_of` therefore bounds on
`observed_at` alone — and that single bound is load-bearing, because it is all
that stands between a backtest and a promoter profile read next week. The
account's own `created_at` is a property of the account rather than of the
reading, and bounding on it would hide every account older than the token, which
is all of them.

`tests/test_pipeline_integrity.py` asserts each of these directly.

---

## Metrics

34 signals across six families. The organizing principle is **cost to fake**, and
weights follow it: on-chain topology and team credibility carry the most, and
narrative the least, because a narrative is free to construct and a funding graph
is not.

| family | n | question |
|---|---|---|
| `onchain_topology` | 7 | How many *independent actors* are behind the holder set? |
| `social_authenticity` | 10 | Was this attention produced by people? |
| `community_production` | 5 | Is anyone doing unpaid work for this token? |
| `narrative` | 5 | Is the idea new, wanted, and findable? |
| `team_credibility` | 3 | What has the deployer done, and what are they doing now? |
| `execution_quality` | 4 | Can a position our size actually be *exited*? |

Every metric declares a `gameability` string describing how a team could fake it
and what the counter-measure is. This is enforced: `MetricRegistry.register`
raises without it, and a test asserts the note is substantive. A metric that a
$50 engagement package defeats, with no stated counter-measure, is a liability
dressed as a signal.

Two contracts every metric obeys:

- **Absence is not bearishness.** Missing inputs return `Confidence.MISSING` with
  `normalized=None`. Returning `0.0` for "unknown" is a strong bearish claim made
  from no data, and it is the bug that quietly poisons a composite.
- **Normalized is always higher-is-better.** Bearish metrics invert internally so
  the scorer never needs to know their polarity.

---

## Scoring

Weights are allocated at the **family** level first and distributed within
families second. Ten social-authenticity metrics that all fire on the same
reply farm are one observation, not ten; family-level budgeting caps how much
any single phenomenon can move the total.

Missing metrics are dropped and the remaining weights renormalized **within their
family**, so a family whose data source is down cannot silently hand its budget
to another family. The resulting `coverage` is reported, and the entry gate
refuses to act below a floor.

**Vetoes are refusals, not penalties.** A live mint authority, a deployer with
prior rugs, insider supply over threshold, or an exit depth below the intended
position size all short-circuit the composite entirely. Encoding those as weights
would let a sufficiently attractive score override them, which is precisely the
failure the mechanism exists to prevent.

The default weights are explicitly **a hypothesis, not a model** — `botsensai
weights` says so when nothing has been fitted. `scoring/fit.py` replaces them
with values fitted on labelled outcomes, refuses to fit below 200 samples, and
reports holdout rank correlation alongside the weights so the fit can be
disbelieved.

---

## Execution modelling

The same `FillSimulator` serves the paper broker and the backtester, so a paper
result and a backtest result are directly comparable rather than two different
fictions.

It charges for latency (the price at decision is not the price at inclusion),
curve impact computed properly as a constant-product integral rather than a flat
slippage percentage, platform and LP fees, priority fees and Jito tips, a
transaction failure rate — with fees still burned on failure — and a
probabilistic sandwich penalty.

Bonding curves and AMM pools are both modelled as constant-product markets, which
they are; only the reserves differ. Reserves are reconstructed so that the
curve's spot price **equals** the observed price, because anchoring depth and
price independently produces a curve that disagrees with the market and charges
phantom slippage on the first order.

`LiveBroker` is a deliberate stub that raises. There is no signing code, no key
handling and no transaction submission anywhere in this repository, and a test
asserts it stays that way.

---

## Agentic memory

Append-only, with explicit supersession rather than in-place mutation. A revised
belief is a new entry pointing at the one it replaces, which means a backtest can
ask what the bot believed at 03:14 last Tuesday and get an honest answer instead
of today's hindsight.

Confidence decays with age and updates on evidence, using a bounded additive
update rather than a Bayesian one — the evidence stream here is not independent
(one meta produces many correlated confirmations), and a proper Bayesian update
would race to certainty on what is really one observation repeated.

Memory feeds scoring through `meta_alignment`, which reads the currently-winning
narrative themes out of the bot's own regime notes. That is the point where the
memory layer stops being decoration and starts conditioning trades.

---

## What is proven and what is not

**Proven:** the pipeline runs end to end against live endpoints; the metrics
separate synthetic archetypes with the expected ordering and margins; the fill
model is internally consistent and reproducible; point-in-time correctness holds
under direct test; the content generator cannot emit an undisclosed or
predictive post.

**Not proven:** that any of these signals predict returns. There is no corpus of
labelled outcomes yet, weights are unfitted priors, and the only backtests that
have run are over synthetic data — which demonstrates that the machinery executes
and says nothing whatsoever about profitability. Every synthetic result is
labelled as such in its own summary.

Closing that gap is the entire content of `@fix_plan.md` phases 1 through 3. It
is a legitimate outcome for the answer to be no.
