"""Perceptual hashing: a 64-bit fingerprint of what a picture looks like.

`derivative_remix_depth` asks a question no cryptographic hash can answer — not
"is this the same file" but "is this the same *idea*". A meme recut, recaptioned
or re-encoded is a different file on every byte and the same picture to a human,
and the whole signal is in how far a token's imagery has drifted from the asset
it launched with.

The construction is the standard DCT hash, and the reasons for each step matter:

1. Resample to 32x32 by **area average**, so the hash of a thumbnail equals the
   hash of the full-size original. Point sampling would alias and break exactly
   the invariance the metric depends on.
2. 2-D DCT-II, keep the top-left 8x8. Low frequencies are the composition;
   the high frequencies are the JPEG artefacts, resize ringing and watermark.
3. Threshold against the **median of the 63 non-DC coefficients**. The median is
   what buys invariance to brightness and contrast: adding a constant to an
   image moves only its DC term, and scaling it multiplies every coefficient
   alike, so the *ordering* the threshold reads is untouched. Excluding DC from
   the median keeps that argument exact rather than nearly-exact — measured over
   206 images it never moved a single bit, so it is a principle worth keeping
   and not a behaviour any test can pin.

Two hashes from different sources are only comparable if they were produced the
same way, so every stored value carries a namespace prefix: `p:` for a
perceptual hash and `md5:` for an exact-content hash that a surface handed us
(4chan publishes one). Mixing the two in a distance computation is meaningless,
and prefixing is what stops that happening silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache

import numpy as np

__all__ = [
    "EXACT_PREFIX",
    "PERCEPTUAL_PREFIX",
    "cluster_by_distance",
    "exact_label",
    "hamming_distance",
    "perceptual_hash",
    "perceptual_label",
    "perceptual_values",
]

#: Namespace for a hash produced by `perceptual_hash`.
PERCEPTUAL_PREFIX = "p:"
#: Namespace for an exact-content hash published by a surface.
EXACT_PREFIX = "md5:"

_GRID = 32
_KEEP = 8
_BITS = _KEEP * _KEEP


def perceptual_label(digest: str) -> str:
    return f"{PERCEPTUAL_PREFIX}{digest}"


def exact_label(digest: str) -> str:
    return f"{EXACT_PREFIX}{digest}"


def perceptual_values(hashes: Iterable[str]) -> list[str]:
    """Keep only perceptual hashes, stripped of their prefix."""
    return [h[len(PERCEPTUAL_PREFIX) :] for h in hashes if h.startswith(PERCEPTUAL_PREFIX)]


@lru_cache(maxsize=4)
def _dct_matrix(size: int) -> np.ndarray:
    """Orthonormal DCT-II basis, so `M @ x @ M.T` is the separable 2-D transform."""
    n = np.arange(size, dtype=np.float64)
    k = n.reshape(-1, 1)
    basis = np.cos(np.pi * (2.0 * n + 1.0) * k / (2.0 * size))
    basis *= np.sqrt(2.0 / size)
    basis[0] /= np.sqrt(2.0)
    return basis


def _resample_matrix(n_in: int, n_out: int) -> np.ndarray:
    """Area-overlap weights mapping `n_in` samples onto `n_out` bins."""
    edges = np.linspace(0.0, float(n_in), n_out + 1)
    lo = edges[:-1].reshape(-1, 1)
    hi = edges[1:].reshape(-1, 1)
    index = np.arange(n_in, dtype=np.float64).reshape(1, -1)
    overlap = np.clip(np.minimum(hi, index + 1.0) - np.maximum(lo, index), 0.0, None)
    totals = overlap.sum(axis=1, keepdims=True)
    # An output bin narrower than one input pixel still overlaps exactly one, so
    # totals are strictly positive; the guard is for degenerate inputs only.
    totals[totals == 0.0] = 1.0
    return overlap / totals


def resample(image: np.ndarray, size: int = _GRID) -> np.ndarray:
    """Area-average `image` to `size` x `size`, up or down."""
    if image.ndim != 2 or image.size == 0:
        raise ValueError("perceptual hashing needs a non-empty 2-D luma array")
    rows = _resample_matrix(image.shape[0], size)
    cols = _resample_matrix(image.shape[1], size)
    return rows @ image.astype(np.float64) @ cols.T


def perceptual_hash(image: np.ndarray) -> str:
    """Return the 16-hex-character (64-bit) DCT hash of a 2-D luma array."""
    grid = resample(image, _GRID)
    basis = _dct_matrix(_GRID)
    coefficients = (basis @ grid @ basis.T)[:_KEEP, :_KEEP].ravel()
    threshold = float(np.median(coefficients[1:]))
    bits = coefficients > threshold
    # Bit 0 is the DC term and is therefore all but always set. It is kept so the
    # hash is exactly 64 bits and 16 hex characters; a bit that never varies adds
    # a constant 0 to every distance and changes no comparison.
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{_BITS // 4}x}"


def hamming_distance(left: str, right: str) -> int | None:
    """Bit distance between two hex digests, or None if they are not comparable."""
    if len(left) != len(right):
        return None
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def cluster_by_distance(hashes: Sequence[str], max_distance: int) -> list[int]:
    """Single-linkage cluster of hex digests; returns one label per input.

    Single linkage is the right shape here: a remix chain drifts, so A near B and
    B near C belong to one visual lineage even when A and C are far apart.
    Labels are consecutive from 0 in first-appearance order, so `Counter` over
    the result gives cluster populations directly.
    """
    parent = list(range(len(hashes)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            distance = hamming_distance(hashes[i], hashes[j])
            if distance is not None and distance <= max_distance:
                parent[find(i)] = find(j)

    labels: dict[int, int] = {}
    out: list[int] = []
    for i in range(len(hashes)):
        root = find(i)
        out.append(labels.setdefault(root, len(labels)))
    return out
