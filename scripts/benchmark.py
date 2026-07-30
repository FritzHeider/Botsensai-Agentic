#!/usr/bin/env python
"""Measure how the store's hot read paths scale as the corpus grows.

The backtester does not run one query. It walks every launch and, per launch,
asks for snapshots, trades, holders, posts, metric values, the deployer's prior
record and each wallet's prior history — all filtered on both `as_of` and
`observed_at`. That is a per-token query in a loop, so a single read path that
degrades from an index seek to a table scan turns the whole backtest from
linear into quadratic, and it does it silently: the suite still passes, the
numbers are still correct, the run just takes longer every week as the corpus
grows.

This script seeds throwaway stores at increasing sizes and times each read path
at each size, so the growth curve is visible rather than inferred. Read the
`us/call` columns across a row: roughly flat means the path is index-bound and
will keep scaling; roughly proportional to the size multiplier means it is
scanning and will not.

`tests/test_performance.py` pins the structural half of this (no hot path may
plan a full table scan). This script supplies the numbers behind it.

    python scripts/benchmark.py                  # default sizes
    python scripts/benchmark.py --sizes 200 2000
"""

from __future__ import annotations

import argparse
import random
import statistics
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from botsensai.models import (
    HolderRecord,
    Launch,
    MarketSnapshot,
    MetricValue,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)
from botsensai.store.db import Database

SNAPSHOTS_PER_TOKEN = 4
TRADES_PER_TOKEN = 8
HOLDERS_PER_TOKEN = 5
POSTS_PER_TOKEN = 3
METRICS_PER_TOKEN = 33  # the live registry size, so the table grows as it really does


def seed(db: Database, tokens: int, seed_value: int = 7) -> list[str]:
    """Write a corpus shaped like a real one: many tokens, a few rows each.

    Deployers and wallets are drawn from pools much smaller than the token
    count, because that is what makes `deployer_history` and
    `wallet_seen_before` interesting — a deployer with one launch never
    exercises the index.
    """
    rng = random.Random(seed_value)
    base = utcnow() - timedelta(days=30)
    deployers = [f"deployer{i}" for i in range(max(1, tokens // 10))]
    wallets = [f"wallet{i}" for i in range(max(1, tokens // 4))]
    keys: list[str] = []

    for i in range(tokens):
        mint = f"mint{i:07d}"
        key = f"solana:{mint}"
        keys.append(key)
        ref = TokenRef(chain="solana", mint=mint, symbol=f"S{i%1000}")
        created = base + timedelta(seconds=i * 30)

        db.upsert_launch(
            Launch(
                token=ref,
                launchpad="pumpfun",
                deployer=rng.choice(deployers),
                created_at=created,
                observed_at=created,
                source="benchmark",
            )
        )
        db.insert_snapshots(
            MarketSnapshot(
                token=ref,
                as_of=created + timedelta(seconds=60 * j),
                observed_at=created + timedelta(seconds=60 * j),
                price_usd=0.001 * (j + 1),
                market_cap_usd=1000.0 * (j + 1),
            )
            for j in range(SNAPSHOTS_PER_TOKEN)
        )
        db.insert_trades(
            Trade(
                token=ref,
                signature=f"{mint}-sig{j}",
                as_of=created + timedelta(seconds=30 * j),
                observed_at=created + timedelta(seconds=30 * j),
                wallet=rng.choice(wallets),
                side="buy" if j % 2 else "sell",
                amount_token=1000.0,
                amount_native=0.5,
                slot=1000 + j,
            )
            for j in range(TRADES_PER_TOKEN)
        )
        db.insert_holders(
            HolderRecord(
                token=ref,
                as_of=created + timedelta(seconds=120),
                observed_at=created + timedelta(seconds=120),
                wallet=rng.choice(wallets),
                balance=1000.0,
                share_of_supply=0.01 * (j + 1),
            )
            for j in range(HOLDERS_PER_TOKEN)
        )
        # Two passes of the whole registry, so the correlated "newest as_of per
        # (token, metric)" subquery in metric_values_as_of has something to
        # actually choose between. One pass would make it trivially correct.
        db.insert_metric_values(
            MetricValue(
                metric_id=f"metric_{m:02d}",
                token=ref,
                as_of=created + timedelta(seconds=90 * pass_no),
                observed_at=created + timedelta(seconds=90 * pass_no),
                raw=float(m) + pass_no,
                normalized=0.5,
                confidence="high",
            )
            for pass_no in range(2)
            for m in range(METRICS_PER_TOKEN)
        )
        db.insert_posts(
            [
                SocialPost(
                    platform="x",
                    post_id=f"{mint}-{j}",
                    author=f"author{j}",
                    text=f"gm {mint}",
                    as_of=created + timedelta(seconds=45 * j),
                    observed_at=created + timedelta(seconds=45 * j),
                )
                for j in range(POSTS_PER_TOKEN)
            ],
            token_key=key,
        )

    return keys


def time_call(fn, repeats: int, rounds: int) -> float:
    """Microseconds per call: the *minimum* across rounds of a median-of-repeats.

    Both layers earn their place. The median inside a round discards individual
    slow calls (a page fault, a scheduler preemption). The minimum across rounds
    discards a whole round that ran while something else had the machine.

    A first pass measured this with a single median and the numbers moved by 5x
    between runs — `deployer_history` read 5.1x growth once and 24.6x ten
    minutes later, purely from load. Timings that swing that far are not
    evidence of anything, and quoting the flattering one would be worse than not
    measuring at all.
    """
    fn()  # warm the page cache and the statement cache; never timed
    best = float("inf")
    for _ in range(rounds):
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1e6)
        best = min(best, statistics.median(samples))
    return best


def measure(tokens: int, repeats: int, rounds: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "bench.db"))
        keys = seed(db, tokens)
        as_of = utcnow()
        rng = random.Random(11)

        def pick() -> str:
            return rng.choice(keys)

        results = {
            "launch": time_call(lambda: db.launch(pick()), repeats, rounds),
            "snapshots_as_of": time_call(lambda: db.snapshots_as_of(pick(), as_of), repeats, rounds),
            "trades_as_of": time_call(lambda: db.trades_as_of(pick(), as_of), repeats, rounds),
            "holders_as_of": time_call(lambda: db.holders_as_of(pick(), as_of), repeats, rounds),
            "posts_as_of": time_call(lambda: db.posts_as_of(pick(), as_of), repeats, rounds),
            "metric_values_as_of": time_call(lambda: db.metric_values_as_of(pick(), as_of), repeats, rounds),
            "deployer_history": time_call(
                lambda: db.deployer_history(f"deployer{rng.randrange(max(1, tokens // 10))}", as_of),
                repeats,
                rounds,
            ),
            "wallet_seen_before": time_call(
                lambda: db.wallet_seen_before(f"wallet{rng.randrange(max(1, tokens // 4))}", as_of),
                repeats,
                rounds,
            ),
        }
        db.close()
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[250, 1000, 4000])
    parser.add_argument("--repeats", type=int, default=40)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    sizes = sorted(args.sizes)
    print(
        f"seeding stores at {sizes} tokens; best of {args.rounds} rounds x "
        f"median of {args.repeats} calls, microseconds\n"
    )

    table: dict[int, dict[str, float]] = {}
    for size in sizes:
        started = time.perf_counter()
        table[size] = measure(size, args.repeats, args.rounds)
        print(f"  seeded and measured {size} tokens in {time.perf_counter() - started:.1f}s")
    print()

    paths = list(table[sizes[0]])
    width = max(len(p) for p in paths)
    header = f"{'read path':<{width}}  " + "  ".join(f"{s:>10}" for s in sizes)
    growth_label = f"  x{sizes[-1] // sizes[0]} size"
    print(header + growth_label)
    print("-" * (len(header) + len(growth_label)))

    for path in paths:
        cells = "  ".join(f"{table[s][path]:>10.1f}" for s in sizes)
        first, last = table[sizes[0]][path], table[sizes[-1]][path]
        ratio = last / first if first else float("nan")
        print(f"{path:<{width}}  {cells}  {ratio:>8.2f}x")

    print(
        f"\nA read path that is index-bound holds roughly 1x across a "
        f"{sizes[-1] // sizes[0]}x corpus. One that scans grows with it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
