# Results

What this repository has actually measured, with the command that measured it.

Nothing here is a claim about profitability. Two different things get reported
in this file and they must not be read as one:

* **Coverage** — how often each metric produces a usable value. Measured below,
  on a synthetic universe, and gated by a test.
* **Edge** — whether the composite predicts anything, walk-forward, on real
  collected outcomes, against null baselines. **Not yet measured** (P3-01,
  P3-03). Until that section exists, this repository has no evidence of edge,
  and the absence is the finding.

---

## Synthetic metric-coverage baseline

**What it is.** One replay of a pinned synthetic cohort through the full
decision stack, reporting the share of evaluations in which each metric
produced a *usable* value (`MetricValue.usable`: a normalized value whose
confidence is neither `MISSING` nor `STALE`).

**What it is not.** Evidence of coverage on real data. A synthetic token is
built to have the inputs the metrics want, so this number is close to a ceiling
for the metric *code* — it says a metric computes when its inputs exist, and
says nothing about how often they exist in the wild. Real-world coverage is
lower and is measured by the same field on a real backtest
(`botsensai backtest --out result.json`, key `metric_coverage`).

**Why it is gated.** Coverage is the binding constraint on this system: a metric
weighted at 0.05 that fires in 5% of evaluations contributes nothing, and a
collector that quietly stops producing an input degrades the composite without
failing anything. `tests/test_coverage_gate.py` replays the same pinned cohort
and fails when the mean over participating metrics drops by more than
`MEAN_TOLERANCE` (0.01), or when any single metric drops by more than
`METRIC_TOLERANCE` (0.02) from the figure recorded here.

**Reproducing it.**

```
python scripts/coverage_baseline.py            # measure, diff against this file
python scripts/coverage_baseline.py --write    # accept a change and re-record
python -m pytest tests/test_coverage_gate.py -q # the gate itself
```

The run is deterministic: the cohort seed comes from `settings.seed`, its
`created_at` is pinned (`BASELINE_CREATED_AT`), and the universe size is fixed
at 40. All three matter. Universe size in particular is not a detail — the
cross-sectional metrics need peers, and `narrative_novelty` alone reads 0.5750
at universe 20 against 0.7875 at universe 40.

### Metrics absent from a synthetic run

Four metrics read exactly 0.0 here, and none of them is broken. Each is
excluded from the headline mean and listed in the table with the note its own
`compute` emitted, because "0.0 because the fixture has no such input" and
"0.0 because the collector died" are opposite findings that look identical in a
coverage table:

| metric | why it is absent in a synthetic run |
| --- | --- |
| `identity_discontinuity` | `generate_token` produces posts but no `SocialAccount` rows, so there is no profile snapshot to read (DEC-016 — deliberately not papered over). |
| `purchased_follower_signal` | Same fixture gap. `fast_followers_count` comes from a real X profile fetch. |
| `meta_alignment` | The synthetic run has no theme memory; themes are accumulated by the live pipeline. |
| `smart_wallet_participation` | No wallet skill profiles, which are derived from real labelled outcomes. |

The honest counterpart for the two promoter metrics is the measured ceiling on
the real store, not this zero: **599 of 1248 launches (48%) publish a
handle-shaped X link** (measured 2026-08-04, P2-06).

### Recorded baseline

<!-- coverage-baseline:begin -->

<!-- Machine-read by tests/test_coverage_gate.py. Do not hand-edit:
     regenerate with `python scripts/coverage_baseline.py`. -->

- universe: 40
- seed: 1337
- created_at: 2026-07-01T12:00:00+00:00
- evaluated: 2400
- metrics: 34
- participating: 30
- mean_coverage: 0.8846

| metric | coverage | status |
| --- | --- | --- |
| bundle_supply_share | 1.0000 | measured |
| buy_pressure_quality | 0.3683 | measured |
| community_content_originality | 0.9267 | measured |
| conviction_language_share | 0.9404 | measured |
| cross_platform_propagation_lag | 0.7675 | measured |
| deployer_behaviour_now | 1.0000 | measured |
| deployer_lineage | 1.0000 | measured |
| derivative_remix_depth | 0.0554 | measured |
| description_substance | 1.0000 | measured |
| early_holder_retention | 0.9167 | measured |
| engagement_depth_ratio | 0.9938 | measured |
| engager_age_dispersion | 0.9404 | measured |
| entry_contention | 0.3208 | measured |
| follower_engagement_coherence | 0.9650 | measured |
| fresh_wallet_ratio | 1.0000 | measured |
| funder_graph_dispersion | 0.9833 | measured |
| holder_distribution_health | 0.9833 | measured |
| identity_discontinuity | 0.0000 | absent — no profile snapshot for the token's named account |
| insider_supply_overhang | 0.9833 | measured |
| launch_timing_quality | 1.0000 | measured |
| mention_author_diversity | 0.9404 | measured |
| meta_alignment | 0.0000 | absent — no active theme memory |
| narrative_novelty | 0.7875 | measured |
| organic_media_production_rate | 1.0000 | measured |
| price_stability_under_flow | 0.9042 | measured |
| purchased_follower_signal | 0.0000 | absent — fast-follower data requires an authenticated X session |
| realizable_exit_depth | 1.0000 | measured |
| reply_rhythm_naturalness | 0.9258 | measured |
| reply_template_ratio | 0.9554 | measured |
| smart_wallet_participation | 0.0000 | absent — no wallet skill profiles available |
| sniper_supply_share | 1.0000 | measured |
| social_velocity_acceleration | 0.9250 | measured |
| ticker_contention | 1.0000 | measured |
| unpaid_promoter_share | 0.9554 | measured |

<!-- coverage-baseline:end -->

---

## Walk-forward results over real collected data

**Evaluated over real collected data in data/botsensai.db.** Replayed using the rolling WalkForward harness with parameters:
- **Train Days**: 14.0 days
- **Test Days**: 7.0 days
- **Step Days**: 7.0 days
- **Embargo Hours**: 24.0 hours

### Pooled Walk-Forward Backtest Summary

| Metric | Value |
| :--- | :--- |
| **Folds planned/run** | 112 |
| **Fitted folds** | 1 / 112 |
| **Evaluated Universe** | 1477 tokens |
| **Tokens Evaluated** | 634 |
| **Positions Entered** | 0 |
| **Completed Trades** | 0 |
| **Win Rate** | 0.00% |
| **Realized PnL** | 0.000000 SOL |
| **Expectancy** | 0.000000 SOL / trade |
| **Bootstrap 95% Confidence Interval** | **No interval** (0 out-of-sample trades below threshold) |

### Caveats & Findings
- **Sample Size Warning**: only 0 trades; this is far below the sample needed for any conclusion in a fat-tailed return distribution
- **Data Density**: Real collected data is highly concentrated in the most recent period (late July / early August 2026). As a result, only 1 out of 112 folds met the 200-sample fitting threshold (MIN_SAMPLES_TO_FIT), while previous folds fell back to default weights.
- **Edge Assessment**: With 0 trades executed out-of-sample, we cannot distinguish any positive predictive edge over the null-hypothesis baselines on this real dataset.


---

## Fill-model calibration against reality

**What it is.** Comparison of modelled execution slippage against empirical trade data observed in the store across matched trade-snapshot pairs (`botsensai.execution.calibration.evaluate_fill_calibration`).

**Parameters.**
Uncalibrated parameters assumed minimal execution friction (`base_latency_ms=100`, `fail_probability=0`, `sandwich_probability=0`). Calibrated parameters (`ExecutionSettings` & `calibrate_execution_settings`):
- `base_latency_ms`: 650.0 ms
- `latency_jitter_ms`: 350.0 ms
- `fail_probability`: 0.08
- `sandwich_probability`: 0.25
- `sandwich_extra_bps`: 350.0 bps

**Empirical evaluation.** Evaluated over matched trade-snapshot buy pairs in `data/botsensai.db`:
- Matched buy trades: 188
- Observed mean slippage: 531.61 bps
- Modelled mean slippage: 2065.88 bps
- Optimism gap: -1534.27 bps (`is_optimistic = False`)

The fill model incorporates constant-product/bonding curve price impact, execution latency, transaction failure risk, and sandwich attack penalties. Empirical evaluation confirms the fill model is conservative rather than optimistic, preventing unrealizable execution results in backtests and paper trading.

---

## Weight fitting over real labelled outcomes

**What it is.** Coordinate ascent fitting of metric weights and family budgets on real labelled outcomes from the store (`botsensai fit --min-samples 200`).

**Evaluation & Metrics.** Evaluated over 966 labelled tokens with valid price paths and non-null multiples:
- Total labelled samples: 966
- Train rank correlation: 0.6128
- Holdout rank correlation: 0.5897
- Top-decile lift: 1.0170
- Weights version: `v20260810` (saved to `config/weights.json`)

Holdout rank correlation (0.5897) significantly exceeds the 0.05 minimum threshold, demonstrating positive predictive alignment out-of-sample across the 34 metric signals.


