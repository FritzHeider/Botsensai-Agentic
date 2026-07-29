"""Content generation: turning scores into publishable writing.

The brief called for a media generator alongside the trader. The thing that
makes this one worth having is that it writes from the system's actual evidence
rather than from a template with the ticker swapped in — every claim in a
generated post traces back to a metric value, a veto reason, or a stored memory,
and the generator refuses to make a claim it cannot source.

Three non-negotiables, implemented rather than merely documented:

* **Disclosure is automatic and cannot be switched off from a template.** If the
  system holds or has held a position in the token, the post says so. Publishing
  favourable commentary about something you hold without saying you hold it is
  the specific behaviour that anti-touting rules exist to prevent, and it is
  trivially avoidable.
* **No price predictions, ever.** The generator describes what was measured. It
  does not say what will happen. This is both a legal posture and an accuracy
  one: the system does not know.
* **Uncertainty is stated, not smoothed.** A post about a token scored on 40%
  metric coverage says the read was thin. Confident prose about a thin read is
  the failure mode that makes automated content worthless.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botsensai.config import MediaSettings, Settings, get_settings
from botsensai.memory.store import MemoryStore
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.models import (
    ContentPiece,
    Launch,
    MemoryKind,
    Score,
)
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: Phrases that must never appear in generated output. Checked after rendering,
#: so a template change cannot quietly reintroduce them.
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bguarantee\w*\b", re.I),
    re.compile(r"\bwill\s+(?:moon|pump|100x|10x|explode|skyrocket)\b", re.I),
    re.compile(r"\bcan'?t\s+lose\b", re.I),
    re.compile(r"\brisk[- ]free\b", re.I),
    re.compile(r"\bsure\s+thing\b", re.I),
    # "financial advice" is prohibited as a claim but mandatory as a disclaimer,
    # so the negative lookbehind lets the disclaimer form through and blocks the
    # assertion. The disclosure block is also excised before scanning, below.
    re.compile(r"(?<!not )(?<!isn't )(?<!is not )\bfinancial\s+advice\b", re.I),
    re.compile(r"\bape\s+in\b", re.I),
    re.compile(r"\bnot\s+gonna\s+lie\b", re.I),
)


@dataclass
class Evidence:
    """One sourced claim. The generator can only write from these."""

    claim: str
    metric_id: str | None
    value: float | None
    confidence: str
    source: str

    def cite(self) -> str:
        if self.value is None:
            return f"{self.claim} [{self.source}]"
        return f"{self.claim} ({self.value:.2f}, {self.confidence}) [{self.source}]"


class ContentGenerator:
    """Produces blog posts, threads and alerts from scoring output."""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: MetricRegistry | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.media: MediaSettings = self.settings.media
        self.registry = registry or build_registry()
        self.memory = memory

    # -- evidence ----------------------------------------------------------- #

    def gather_evidence(self, score: Score, top_n: int = 8) -> list[Evidence]:
        """Turn a score into a list of sourced, quotable observations."""
        evidence: list[Evidence] = []
        by_id = {v.metric_id: v for v in score.metric_values}

        ranked = sorted(score.contributions.items(), key=lambda kv: kv[1], reverse=True)
        for metric_id, _contribution in ranked[:top_n]:
            value = by_id.get(metric_id)
            metric = self.registry.get(metric_id)
            if value is None or metric is None or value.normalized is None:
                continue
            direction = "supports" if value.normalized >= 0.5 else "argues against"
            evidence.append(
                Evidence(
                    claim=f"{metric.name} {direction} the case ({metric.thesis.split('.')[0].lower()})",
                    metric_id=metric_id,
                    value=value.normalized,
                    confidence=value.confidence.value,
                    source=", ".join(metric.sources) or "internal",
                )
            )

        for veto in score.vetoes:
            evidence.append(
                Evidence(
                    claim=f"hard rejection: {veto.value.replace('_', ' ')}",
                    metric_id=None,
                    value=None,
                    confidence="verified",
                    source="veto gate",
                )
            )

        missing = [v.metric_id for v in score.metric_values if not v.usable]
        if missing:
            evidence.append(
                Evidence(
                    claim=(
                        f"{len(missing)} of {len(score.metric_values)} measurements were "
                        "unavailable, so this read is partial"
                    ),
                    metric_id=None,
                    value=score.coverage,
                    confidence="verified",
                    source="coverage",
                )
            )
        return evidence

    # -- rendering ---------------------------------------------------------- #

    @staticmethod
    def _label(score: Score) -> str:
        """Plain-language verdict. Deliberately not a recommendation."""
        if score.vetoed:
            return "screened out"
        if score.coverage < 0.4:
            return "insufficient data to judge"
        if score.composite >= 0.75:
            return "unusually strong on the measures tracked here"
        if score.composite >= 0.6:
            return "better than most of the launch feed"
        if score.composite >= 0.45:
            return "unremarkable"
        return "weak on the measures tracked here"

    def blog_post(
        self,
        launch: Launch,
        score: Score,
        holds_position: bool = False,
        market_context: dict[str, Any] | None = None,
    ) -> ContentPiece:
        """A full write-up of one token, sourced to the measurements."""
        evidence = self.gather_evidence(score)
        symbol = launch.token.symbol or launch.token.mint[:8]
        name = launch.token.name or symbol
        age_minutes = (score.as_of - launch.created_at).total_seconds() / 60.0

        strong = [e for e in evidence if e.value is not None and e.value >= 0.6 and e.metric_id]
        weak = [e for e in evidence if e.value is not None and e.value < 0.4 and e.metric_id]

        lines: list[str] = []
        lines.append(f"# {name} (${symbol}): what the data actually says")
        lines.append("")
        lines.append(
            f"This is an automated read of ${symbol}, taken {age_minutes:.0f} minutes after it "
            f"launched on {launch.launchpad.value}. It is a description of measurements, not a "
            f"recommendation, and the measurements are listed so you can disagree with them."
        )
        lines.append("")

        lines.append("## The short version")
        lines.append("")
        lines.append(
            f"On a composite built from {len(score.metric_values)} signals, ${symbol} scores "
            f"{score.composite:.2f} out of 1.00 and reads as {self._label(score)}. "
            f"The market backdrop at the time of measurement was {score.regime}. "
            f"Metric coverage was {score.coverage:.0%}, which is the fraction of signals that had "
            f"enough underlying data to produce a value at all."
        )
        lines.append("")

        if score.vetoes:
            lines.append("## Why it was rejected outright")
            lines.append("")
            reasons = [v.value.replace("_", " ") for v in score.vetoes]
            lines.append(
                "Some findings end the analysis rather than lowering a score. "
                f"For ${symbol}: {', '.join(reasons)}. "
                "These are structural facts about the token or its deployer rather than "
                "judgements about its prospects, and no amount of positive signal elsewhere "
                "offsets them."
            )
            lines.append("")

        if strong:
            lines.append("## What is working")
            lines.append("")
            for item in strong[:5]:
                metric = self.registry.get(item.metric_id or "")
                if metric is None:
                    continue
                lines.append(
                    f"**{metric.name}** — {metric.thesis} Measured at {item.value:.2f} with "
                    f"{item.confidence} confidence, from {item.source}."
                )
                lines.append("")

        if weak:
            lines.append("## What is not")
            lines.append("")
            for item in weak[:4]:
                metric = self.registry.get(item.metric_id or "")
                if metric is None:
                    continue
                lines.append(
                    f"**{metric.name}** — measured at {item.value:.2f}. {metric.thesis}"
                )
                lines.append("")

        lines.append("## How to read this sceptically")
        lines.append("")
        lines.append(
            "Every measurement above can be gamed, and the ones that are cheapest to game are "
            "the ones you should trust least. Engagement counts can be bought. Holder counts can "
            "be manufactured by one person with a script. What is expensive to fake is unpaid "
            "creative labour from unconnected accounts, and independent funding behind the "
            "holder set — which is why those carry the most weight here."
        )
        lines.append("")
        if score.coverage < 0.6:
            lines.append(
                f"This particular read is thin: only {score.coverage:.0%} of the signal set "
                "produced a usable value, so treat the composite as a weak prior rather than "
                "a conclusion."
            )
            lines.append("")

        if self.memory is not None:
            briefing = self.memory.briefing(as_of=score.as_of, limit=4)
            if briefing and briefing != "No prior learnings recorded yet.":
                lines.append("## What this system has learned recently")
                lines.append("")
                lines.append(briefing)
                lines.append("")

        disclosure = self.media.disclosure_text
        if holds_position:
            disclosure = (
                f"Position disclosure: the operator of this system holds or has recently held "
                f"${symbol}. {disclosure}"
            )
        lines.append("---")
        lines.append("")
        lines.append(f"*{disclosure}*")

        body = "\n".join(lines)
        piece = ContentPiece(
            token=launch.token,
            kind="blog",
            title=f"{name} (${symbol}): what the data actually says",
            body=body,
            hashtags=[f"#{symbol}", "#solana", "#memecoins"],
            disclosure=disclosure,
            facts_cited=[e.cite() for e in evidence],
            holds_position=holds_position,
            image_prompt=(
                f"Editorial data-visualization illustration representing a market signal score of "
                f"{score.composite:.2f} for a token called {name}. Abstract, no text, no logos."
            ),
        )
        self._enforce(piece)
        return piece

    def thread(
        self, launch: Launch, score: Score, holds_position: bool = False, max_posts: int = 6
    ) -> ContentPiece:
        """A short social thread. Same evidence discipline, less room."""
        symbol = launch.token.symbol or launch.token.mint[:8]
        evidence = self.gather_evidence(score, top_n=6)
        strong = [e for e in evidence if e.value is not None and e.value >= 0.6 and e.metric_id]

        posts: list[str] = []
        posts.append(
            f"${symbol} scored {score.composite:.2f}/1.00 on an automated read of "
            f"{len(score.metric_values)} signals none of the standard APIs publish. "
            f"Coverage {score.coverage:.0%}. Here is what moved it. 🧵"
        )
        for item in strong[: max_posts - 2]:
            metric = self.registry.get(item.metric_id or "")
            if metric is None:
                continue
            posts.append(f"{metric.name}: {item.value:.2f}. {metric.thesis.split('.')[0]}.")

        if score.vetoes:
            posts.append(
                "Rejected on: " + ", ".join(v.value.replace("_", " ") for v in score.vetoes) + "."
            )

        disclosure = self.media.disclosure_text
        if holds_position:
            disclosure = f"Disclosure: position held in ${symbol}. {disclosure}"
        posts.append(disclosure)

        piece = ContentPiece(
            token=launch.token,
            kind="thread",
            title=f"${symbol} signal read",
            body="\n\n---\n\n".join(posts[:max_posts]),
            hashtags=[f"#{symbol}", "#solana"],
            disclosure=disclosure,
            facts_cited=[e.cite() for e in evidence],
            holds_position=holds_position,
        )
        self._enforce(piece)
        return piece

    def alert(self, launch: Launch, score: Score, action: str) -> ContentPiece:
        """One-line operational alert. No persuasion, just what happened.

        The disclosure rides along even here. An alert is the format most likely
        to be forwarded out of context, which makes it the one that most needs
        the disclaimer attached to the text itself rather than to the channel.
        """
        symbol = launch.token.symbol or launch.token.mint[:8]
        holds = action.lower() in ("buy", "entered", "add", "bought")
        disclosure = self.media.disclosure_text
        if holds:
            disclosure = f"Position disclosure: position opened in ${symbol}. {disclosure}"

        body = (
            f"{action.upper()} ${symbol} — score {score.composite:.3f}, coverage "
            f"{score.coverage:.0%}, regime {score.regime}. "
            f"{(score.explanation or '')[:200]}\n\n{disclosure}"
        )
        piece = ContentPiece(
            token=launch.token,
            kind="alert",
            title=f"{action} ${symbol}",
            body=body,
            disclosure=disclosure,
            holds_position=holds,
        )
        self._enforce(piece)
        return piece

    def recap(
        self,
        scores: Sequence[tuple[Launch, Score]],
        window_label: str = "the last hour",
        regime: str = "unknown",
    ) -> ContentPiece:
        """A digest across many tokens. The most publishable recurring format."""
        ranked = sorted(scores, key=lambda pair: pair[1].composite, reverse=True)
        vetoed = [pair for pair in scores if pair[1].vetoed]

        lines: list[str] = []
        lines.append(f"# Launch feed recap: {window_label}")
        lines.append("")
        lines.append(
            f"{len(scores)} launches were measured in {window_label}. "
            f"{len(vetoed)} were rejected outright on structural grounds. "
            f"The market regime read as {regime}."
        )
        lines.append("")

        if ranked:
            lines.append("## Highest scoring")
            lines.append("")
            for launch, score in ranked[:5]:
                symbol = launch.token.symbol or launch.token.mint[:8]
                lines.append(
                    f"- **${symbol}** — {score.composite:.2f} ({score.coverage:.0%} coverage). "
                    f"{self._label(score)}."
                )
            lines.append("")

        if vetoed:
            reasons: dict[str, int] = {}
            for _, score in vetoed:
                for veto in score.vetoes:
                    reasons[veto.value] = reasons.get(veto.value, 0) + 1
            lines.append("## Why things were rejected")
            lines.append("")
            for reason, count in sorted(reasons.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"- {reason.replace('_', ' ')}: {count}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(f"*{self.media.disclosure_text}*")

        piece = ContentPiece(
            kind="recap",
            title=f"Launch feed recap: {window_label}",
            body="\n".join(lines),
            disclosure=self.media.disclosure_text,
        )
        self._enforce(piece)
        return piece

    # -- guardrails --------------------------------------------------------- #

    def _enforce(self, piece: ContentPiece) -> None:
        """Post-render checks. Raises rather than publishing something unsafe.

        Running these after rendering rather than trusting the templates means a
        future template edit cannot silently reintroduce a prediction or drop the
        disclosure.
        """
        if self.media.require_disclosure and self.media.disclosure_text not in piece.body:
            if piece.disclosure not in piece.body:
                raise ValueError(
                    f"generated {piece.kind} is missing its disclosure; refusing to emit it"
                )

        # Scan the body with the disclosure removed. The disclaimer necessarily
        # contains language the body itself must not use, and scanning it would
        # make a correctly-disclosed post fail its own safety check.
        scannable = piece.body
        for boilerplate in (self.media.disclosure_text, piece.disclosure):
            if boilerplate:
                scannable = scannable.replace(boilerplate, " ")

        for pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(scannable)
            if match:
                raise ValueError(
                    f"generated {piece.kind} contains prohibited phrasing "
                    f"{match.group(0)!r}; refusing to emit it"
                )

    # -- output ------------------------------------------------------------- #

    def save(self, piece: ContentPiece, directory: str | Path | None = None) -> Path:
        target = Path(directory) if directory else self.settings.path(self.media.output_dir)
        target.mkdir(parents=True, exist_ok=True)
        symbol = (piece.token.symbol if piece.token else None) or "feed"
        safe_symbol = re.sub(r"[^A-Za-z0-9_-]", "", symbol) or "feed"
        stamp = piece.created_at.strftime("%Y%m%d-%H%M%S")
        path = target / f"{stamp}-{piece.kind}-{safe_symbol}.md"
        path.write_text(piece.body, encoding="utf-8")
        log.info("media.saved", kind=piece.kind, path=str(path))
        return path

    def remember_publication(self, piece: ContentPiece) -> None:
        """Record what was published, so the bot does not repeat itself."""
        if self.memory is None or piece.token is None:
            return
        self.memory.remember(
            MemoryKind.OBSERVATION,
            subject=piece.token.key,
            title=f"published {piece.kind}: {piece.title}",
            body=piece.body[:400],
            tags=["published", piece.kind],
            confidence=0.9,
        )


__all__ = ["FORBIDDEN_PATTERNS", "ContentGenerator", "Evidence"]
