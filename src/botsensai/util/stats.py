"""Statistical primitives shared by the metric implementations.

These are deliberately dependency-light and pure so they can be property-tested.
Every normalizer maps to 0..1 where 1 means "maximally bullish", which is the
contract the scorer relies on.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence

import numpy as np

EPS = 1e-12


# --------------------------------------------------------------------------- #
# Normalizers
# --------------------------------------------------------------------------- #


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if math.isnan(x):
        return lo
    return max(lo, min(hi, x))


def logistic(x: float, midpoint: float = 0.0, steepness: float = 1.0) -> float:
    """Squash any real number to 0..1. The workhorse normalizer.

    `midpoint` is the value that maps to 0.5; `steepness` controls how fast the
    transition happens. Both should be chosen from the empirical distribution of
    the metric, not guessed, which is what `calibrate_logistic` is for.
    """
    z = steepness * (x - midpoint)
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    e = math.exp(max(z, -60.0))
    return e / (1.0 + e)


def calibrate_logistic(samples: Sequence[float]) -> tuple[float, float]:
    """Pick (midpoint, steepness) from a sample so the metric spans its range.

    Midpoint is the median; steepness is set so the interquartile range covers
    roughly 0.25..0.75 of the output. Falls back to steepness 1.0 for degenerate
    samples.
    """
    arr = np.asarray([s for s in samples if s is not None and math.isfinite(s)], dtype=float)
    if arr.size < 4:
        return (float(arr.mean()) if arr.size else 0.0, 1.0)
    q1, med, q3 = np.percentile(arr, [25, 50, 75])
    iqr = float(q3 - q1)
    if iqr < EPS:
        return (float(med), 1.0)
    # logistic(q3) should be ~0.75 => steepness * (q3 - med) = ln(3)
    steepness = math.log(3.0) / max(EPS, float(q3 - med)) if q3 > med else 1.0
    return (float(med), float(min(50.0, max(1e-3, steepness))))


def robust_z(x: float, samples: Sequence[float]) -> float:
    """Median/MAD z-score. Resistant to the fat tails this domain is full of."""
    arr = np.asarray([s for s in samples if s is not None and math.isfinite(s)], dtype=float)
    if arr.size == 0:
        return 0.0
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    scale = 1.4826 * mad
    if scale < EPS:
        std = float(arr.std())
        scale = std if std > EPS else 1.0
    return (x - med) / scale


def percentile_rank(x: float, samples: Sequence[float]) -> float:
    """Fraction of `samples` at or below `x`, in 0..1. Distribution-free."""
    arr = np.asarray([s for s in samples if s is not None and math.isfinite(s)], dtype=float)
    if arr.size == 0:
        return 0.5
    return float((arr <= x).sum()) / float(arr.size)


def saturating(x: float, scale: float) -> float:
    """x/(x+scale), a gentle 0..1 map for non-negative unbounded quantities."""
    if x <= 0 or scale <= 0:
        return 0.0
    return x / (x + scale)


# --------------------------------------------------------------------------- #
# Concentration and diversity
# --------------------------------------------------------------------------- #


def gini(values: Iterable[float]) -> float:
    """Gini coefficient of a non-negative distribution. 0 = perfectly even."""
    arr = np.asarray([v for v in values if v is not None and v >= 0], dtype=float)
    n = arr.size
    if n == 0:
        return 0.0
    total = float(arr.sum())
    if total < EPS:
        return 0.0
    arr = np.sort(arr)
    index = np.arange(1, n + 1, dtype=float)
    return float((2.0 * (index * arr).sum()) / (n * total) - (n + 1.0) / n)


def herfindahl(shares: Iterable[float]) -> float:
    """HHI on shares that should sum to ~1. 1/n = even, 1 = one holder."""
    arr = np.asarray([s for s in shares if s is not None and s >= 0], dtype=float)
    if arr.size == 0:
        return 1.0
    total = float(arr.sum())
    if total < EPS:
        return 1.0
    arr = arr / total
    return float((arr**2).sum())


def shannon_entropy(counts: Iterable[float], normalize: bool = True) -> float:
    """Entropy in nats, optionally divided by ln(n) to land in 0..1."""
    arr = np.asarray([c for c in counts if c is not None and c > 0], dtype=float)
    if arr.size <= 1:
        return 0.0
    p = arr / arr.sum()
    h = float(-(p * np.log(p)).sum())
    if normalize:
        h /= math.log(arr.size)
    return clamp(h)


def effective_number(shares: Iterable[float]) -> float:
    """1/HHI: the number of equally-sized participants the distribution acts like."""
    h = herfindahl(shares)
    return 1.0 / max(EPS, h)


# --------------------------------------------------------------------------- #
# Temporal structure
# --------------------------------------------------------------------------- #


def burstiness(timestamps: Sequence[float]) -> float:
    """Goh-Barabasi burstiness of an event sequence, in -1..1.

    -1 is perfectly periodic (the signature of a scripted bot farm), 0 is
    Poisson, +1 is heavily bursty (the signature of an organic viral spike).
    Returns 0.0 when there are too few events to judge.
    """
    ts = sorted(float(t) for t in timestamps)
    if len(ts) < 4:
        return 0.0
    gaps = np.diff(np.asarray(ts, dtype=float))
    gaps = gaps[gaps >= 0]
    if gaps.size < 3:
        return 0.0
    mu = float(gaps.mean())
    sigma = float(gaps.std(ddof=0))
    if mu < EPS and sigma < EPS:
        return 0.0
    denom = sigma + mu
    if denom < EPS:
        return 0.0
    return float((sigma - mu) / denom)


def periodicity_score(timestamps: Sequence[float], tolerance: float = 0.15) -> float:
    """Fraction of inter-arrival gaps that cluster on a single modal value.

    High values mean the events are on a timer. Real humans do not post every
    47.0 seconds; scripted reply farms do.
    """
    ts = sorted(float(t) for t in timestamps)
    if len(ts) < 5:
        return 0.0
    gaps = np.diff(np.asarray(ts, dtype=float))
    gaps = gaps[gaps > 0]
    if gaps.size < 4:
        return 0.0
    med = float(np.median(gaps))
    if med < EPS:
        return 1.0
    within = np.abs(gaps - med) <= (tolerance * med)
    return float(within.sum()) / float(gaps.size)


def half_life(values: Sequence[float], timestamps: Sequence[float]) -> float | None:
    """Fit exponential decay to a declining series and return the half-life in
    the same units as `timestamps`. Returns None when the series is not
    decaying or is too short."""
    if len(values) != len(timestamps) or len(values) < 4:
        return None
    v = np.asarray(values, dtype=float)
    t = np.asarray(timestamps, dtype=float)
    mask = v > 0
    if mask.sum() < 4:
        return None
    v, t = v[mask], t[mask]
    t = t - t[0]
    try:
        slope, _ = np.polyfit(t, np.log(v), 1)
    except (np.linalg.LinAlgError, ValueError):
        return None
    if slope >= -EPS:
        return None
    return float(math.log(2.0) / -slope)


def ewma(values: Sequence[float], alpha: float = 0.3) -> float:
    """Exponentially weighted mean, most recent observation weighted highest."""
    if not values:
        return 0.0
    acc = float(values[0])
    for v in values[1:]:
        acc = alpha * float(v) + (1.0 - alpha) * acc
    return acc


def velocity(values: Sequence[float], timestamps: Sequence[float]) -> float:
    """Least-squares slope of value against time. Units are value per time unit."""
    if len(values) != len(timestamps) or len(values) < 2:
        return 0.0
    t = np.asarray(timestamps, dtype=float)
    v = np.asarray(values, dtype=float)
    if float(t.std()) < EPS:
        return 0.0
    try:
        slope, _ = np.polyfit(t, v, 1)
    except (np.linalg.LinAlgError, ValueError):
        return 0.0
    return float(slope)


def acceleration(values: Sequence[float], timestamps: Sequence[float]) -> float:
    """Second derivative via a quadratic fit. Positive means the curve is bending up."""
    if len(values) != len(timestamps) or len(values) < 3:
        return 0.0
    t = np.asarray(timestamps, dtype=float)
    v = np.asarray(values, dtype=float)
    if float(t.std()) < EPS:
        return 0.0
    try:
        a, _, _ = np.polyfit(t, v, 2)
    except (np.linalg.LinAlgError, ValueError):
        return 0.0
    return float(2.0 * a)


# --------------------------------------------------------------------------- #
# Distribution anomalies
# --------------------------------------------------------------------------- #


def benford_deviation(values: Iterable[float]) -> float:
    """Chi-square-ish deviation of leading digits from Benford's law, 0..1.

    Naturally occurring financial magnitudes follow Benford. Fabricated or
    round-number-heavy series do not. Useful against manufactured volume.
    """
    digits = []
    for v in values:
        try:
            x = abs(float(v))
        except (TypeError, ValueError):
            continue
        if x < EPS:
            continue
        d = int(str(x).lstrip("0.").lstrip("0")[:1] or 0)
        if 1 <= d <= 9:
            digits.append(d)
    if len(digits) < 30:
        return 0.0
    observed = Counter(digits)
    n = len(digits)
    chi = 0.0
    for d in range(1, 10):
        expected = n * math.log10(1.0 + 1.0 / d)
        obs = observed.get(d, 0)
        chi += (obs - expected) ** 2 / max(EPS, expected)
    # 8 degrees of freedom; chi ~ 20 is already a strong rejection.
    return clamp(chi / 40.0)


def round_number_share(values: Iterable[float], bases: Sequence[float] = (0.1, 0.5, 1.0)) -> float:
    """Fraction of values sitting exactly on a round base. Bots love round sizes."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    hits = 0
    for v in vals:
        for b in bases:
            if b > 0 and abs((v / b) - round(v / b)) < 1e-6:
                hits += 1
                break
    return hits / len(vals)


def duplicate_share(items: Sequence[str]) -> float:
    """Fraction of items that are exact duplicates of another item."""
    if not items:
        return 0.0
    counts = Counter(items)
    dupes = sum(c for c in counts.values() if c > 1)
    return dupes / len(items)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    """Conservative estimate of a rate given a small sample.

    Prevents a 3-of-3 sample from being treated as a 100% rate, which is exactly
    the mistake that makes early-life metrics on brand-new tokens so noisy.
    """
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1.0 + z**2 / trials
    centre = p + z**2 / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * trials)) / trials)
    return clamp((centre - margin) / denom)


__all__ = [
    "EPS",
    "acceleration",
    "benford_deviation",
    "burstiness",
    "calibrate_logistic",
    "clamp",
    "duplicate_share",
    "effective_number",
    "ewma",
    "gini",
    "half_life",
    "herfindahl",
    "jaccard",
    "logistic",
    "percentile_rank",
    "periodicity_score",
    "robust_z",
    "round_number_share",
    "saturating",
    "shannon_entropy",
    "velocity",
    "wilson_lower_bound",
]
