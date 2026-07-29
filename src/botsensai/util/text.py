"""Text primitives for social-authenticity and narrative metrics.

Zero heavyweight NLP dependencies on purpose: these run on every comment of
every candidate token, thousands of times an hour, and must stay cheap. The
techniques here (shingling, MinHash-free Jaccard on small sets, template
skeletonization) are the ones that actually catch reply farms.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MENTION_RE = re.compile(r"[@＠][A-Za-z0-9_]{1,30}")
_HASHTAG_RE = re.compile(r"[#＃][\w]{1,60}", re.UNICODE)
_CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9]{0,14}\b")
_NUMBER_RE = re.compile(r"\d[\d,._]*(?:[kmbtxKMBTX])?\b")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9']+")
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "]",
    flags=re.UNICODE,
)

# Deliberately small. These are the phrases that dominate paid reply farms and
# are near-absent from organic conversation.
SHILL_PHRASES: tuple[str, ...] = (
    "to the moon",
    "next 100x",
    "lfg",
    "wagmi",
    "early gem",
    "dont miss",
    "don't miss",
    "aped in",
    "sending it",
    "bullish af",
    "this is the one",
    "10000x",
    "buy now",
    "last chance",
    "dev based",
    "based dev",
    "lock in",
    "cook",
    "chart looking",
    "gem found",
)

CONVICTION_PHRASES: tuple[str, ...] = (
    "i bought",
    "i sold",
    "my entry",
    "took profit",
    "still holding",
    "added more",
    "cut my",
    "down bad",
    "up ",
    "average",
)

STOPWORDS: frozenset[str] = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "he", "her", "his", "i", "if", "in", "is", "it", "its", "me", "my", "not", "of", "on", "or", "our", "so", "than", "that", "the", "their", "them", "there", "these", "they", "this", "to", "up", "was", "we", "were", "what", "when", "which", "who", "will", "with", "you", "your", "just", "im", "dont", "u", "ur"]
)


def normalize(text: str) -> str:
    """Case-fold, strip accents and zero-width characters, collapse whitespace.

    Zero-width and homoglyph insertion is the standard evasion used to defeat
    naive duplicate detection in comment farms, so it is stripped here.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("​", "").replace("‌", "").replace("‍", "")
    text = text.replace("﻿", "").replace(" ", " ")
    return _WS_RE.sub(" ", text.casefold()).strip()


def skeletonize(text: str) -> str:
    """Reduce a post to its structural template.

    URLs, mentions, hashtags, cashtags, numbers and emoji all become tokens, so
    "gm @alice $BONK to 100x 🚀" and "gm @bob $WIF to 50x 🔥" collapse to the
    same skeleton. Identical skeletons across many accounts is the single
    strongest tell of a scripted campaign.
    """
    t = normalize(text)
    t = _URL_RE.sub(" <url> ", t)
    t = _MENTION_RE.sub(" <at> ", t)
    t = _CASHTAG_RE.sub(" <tick> ", t)
    t = _HASHTAG_RE.sub(" <tag> ", t)
    t = _NUMBER_RE.sub(" <num> ", t)
    t = _EMOJI_RE.sub(" <emo> ", t)
    t = re.sub(r"[^\w<>\s]", " ", t)
    return _WS_RE.sub(" ", t).strip()


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    words = _WORD_RE.findall(normalize(text))
    if drop_stopwords:
        return [w for w in words if w not in STOPWORDS and len(w) > 1]
    return words


def shingles(text: str, k: int = 4) -> set[str]:
    """Character k-shingles of the skeleton, for near-duplicate detection."""
    s = skeletonize(text)
    if len(s) < k:
        return {s} if s else set()
    return {s[i : i + k] for i in range(len(s) - k + 1)}


def similarity(a: str, b: str, k: int = 4) -> float:
    """Jaccard similarity on character shingles, 0..1."""
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def template_cluster_share(
    texts: Sequence[str], min_cluster: int = 3, fuzzy: bool = True
) -> float:
    """Fraction of texts belonging to a repeated-template cluster.

    This is the primary bot-comment detector. Two passes: exact skeleton match
    catches copy-paste farms, then a simhash near-duplicate pass catches the
    farms that paraphrase slightly to evade exact matching. It is robust to
    token, number and emoji substitution but cheap enough to run on every reply
    of every candidate token.
    """
    if len(texts) < min_cluster:
        return 0.0
    usable = [t for t in texts if t and t.strip()]
    skeletons = [skeletonize(t) for t in usable]
    keep = [i for i, s in enumerate(skeletons) if len(s) >= 6]
    if len(keep) < min_cluster:
        return 0.0

    counts = Counter(skeletons[i] for i in keep)
    exact_members = {i for i in keep if counts[skeletons[i]] >= min_cluster}

    if not fuzzy:
        return len(exact_members) / len(keep)

    remaining = [i for i in keep if i not in exact_members]
    fuzzy_members: set[int] = set()
    if len(remaining) >= min_cluster:
        groups = near_duplicate_groups([usable[i] for i in remaining], max_distance=8)
        for group in groups:
            if len(group) >= min_cluster:
                fuzzy_members.update(remaining[j] for j in group)

    return len(exact_members | fuzzy_members) / len(keep)


def lexical_diversity(texts: Iterable[str]) -> float:
    """Type-token ratio across a corpus, length-corrected, 0..1.

    Organic communities generate a wide vocabulary. Farms recycle one.
    """
    all_tokens: list[str] = []
    for t in texts:
        all_tokens.extend(tokens(t))
    n = len(all_tokens)
    if n < 10:
        return 0.0
    types = len(set(all_tokens))
    # Root TTR (Guiraud) corrects for the fact that raw TTR falls with length.
    return min(1.0, types / math.sqrt(n) / 10.0)


def shill_density(text: str) -> float:
    """Fraction of known promotional phrases present, 0..1."""
    t = normalize(text)
    if not t:
        return 0.0
    hits = sum(1 for p in SHILL_PHRASES if p in t)
    return min(1.0, hits / 3.0)


def conviction_density(text: str) -> float:
    """Presence of first-person position language.

    Someone saying "I averaged down at 40k" is revealing real exposure; someone
    saying "LFG 100x" is not. This separates community from noise.
    """
    t = normalize(text)
    if not t:
        return 0.0
    hits = sum(1 for p in CONVICTION_PHRASES if p in t)
    return min(1.0, hits / 2.0)


def effort_score(text: str) -> float:
    """Rough proxy for how much a human invested in a message, 0..1.

    Combines length, vocabulary richness, and absence of pure-emoji content.
    A 4-word all-emoji reply scores near zero; a 40-word argument scores high.
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0
    stripped = _EMOJI_RE.sub("", raw).strip()
    if not stripped:
        return 0.0
    toks = tokens(stripped)
    if not toks:
        return 0.0
    length_component = min(1.0, len(toks) / 25.0)
    unique_component = len(set(toks)) / len(toks)
    emoji_penalty = 1.0 - min(1.0, len(_EMOJI_RE.findall(raw)) / max(1, len(toks)))
    return max(0.0, min(1.0, 0.5 * length_component + 0.3 * unique_component + 0.2 * emoji_penalty))


def extract_cashtags(text: str) -> list[str]:
    return [m.upper().lstrip("$") for m in _CASHTAG_RE.findall(text or "")]


def extract_hashtags(text: str) -> list[str]:
    return [m.lstrip("#＃").lower() for m in _HASHTAG_RE.findall(text or "")]


def extract_mentions(text: str) -> list[str]:
    return [m.lstrip("@＠").lower() for m in _MENTION_RE.findall(text or "")]


def extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def contains_address(text: str) -> bool:
    """Detects a base58 Solana mint or an 0x EVM address in free text."""
    if not text:
        return False
    if re.search(r"\b0x[a-fA-F0-9]{40}\b", text):
        return True
    return bool(re.search(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text))


def simhash(text: str, bits: int = 64) -> int:
    """Locality-sensitive hash of the skeleton, for cheap near-dup bucketing."""
    feats = shingles(text, k=4)
    if not feats:
        return 0
    vector = [0] * bits
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def near_duplicate_groups(texts: Sequence[str], max_distance: int = 6) -> list[list[int]]:
    """Group indices of near-duplicate texts using simhash Hamming distance.

    O(n^2) but n is the number of replies on one token in one window, typically
    under a few thousand, and the constant is a single XOR.
    """
    hashes = [simhash(t) for t in texts]
    assigned: list[int | None] = [None] * len(texts)
    groups: list[list[int]] = []
    for i in range(len(texts)):
        if assigned[i] is not None or hashes[i] == 0:
            continue
        group = [i]
        assigned[i] = len(groups)
        for j in range(i + 1, len(texts)):
            if assigned[j] is None and hashes[j] != 0:
                if hamming(hashes[i], hashes[j]) <= max_distance:
                    group.append(j)
                    assigned[j] = len(groups)
        groups.append(group)
    return [g for g in groups if len(g) > 1]


def novelty_vs_corpus(text: str, corpus: Sequence[str], k: int = 4) -> float:
    """1 minus the maximum similarity to anything in the corpus, 0..1.

    Used for narrative originality: a ticker whose name and description are a
    trivial remix of last week's winner scores low.
    """
    if not corpus:
        return 1.0
    target = shingles(text, k)
    if not target:
        return 0.0
    best = 0.0
    for other in corpus:
        o = shingles(other, k)
        union = target | o
        if not union:
            continue
        sim = len(target & o) / len(union)
        if sim > best:
            best = sim
            if best >= 0.999:
                break
    return max(0.0, 1.0 - best)


__all__ = [
    "CONVICTION_PHRASES",
    "SHILL_PHRASES",
    "STOPWORDS",
    "contains_address",
    "conviction_density",
    "effort_score",
    "extract_cashtags",
    "extract_hashtags",
    "extract_mentions",
    "extract_urls",
    "hamming",
    "lexical_diversity",
    "near_duplicate_groups",
    "normalize",
    "novelty_vs_corpus",
    "shill_density",
    "shingles",
    "similarity",
    "simhash",
    "skeletonize",
    "template_cluster_share",
    "tokens",
]
