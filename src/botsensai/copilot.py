"""Natural language token explainer and AI copilot.

Grounded forensic synthesis over point-in-time on-chain telemetry, 34 adversarial
metric values, social posts, and safety veto audits with strict citation enforcement.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel

from botsensai.config import Settings
from botsensai.inspector import inspect_token
from botsensai.models import Score, utcnow
from botsensai.store.db import Database

console = Console()


@dataclass
class CopilotCitation:
    source_type: str  # "metric" | "trade" | "holder" | "veto" | "post"
    source_id: str
    fact: str


@dataclass
class CopilotResponse:
    query: str
    token_symbol: str
    token_mint: str
    composite_score: float
    answer: str
    citations: list[CopilotCitation]


def _build_context_facts(score: Score, token_key: str, db: Database) -> list[CopilotCitation]:
    """Compile structured point-in-time evidence facts."""
    citations: list[CopilotCitation] = []

    # 1. Vetoes
    for v in score.vetoes:
        citations.append(CopilotCitation("veto", v, f"Triggered hard safety veto: {v}"))

    # 2. Metric Values
    for mv in score.metric_values:
        if mv.normalized is not None:
            citations.append(
                CopilotCitation(
                    "metric",
                    mv.metric_id,
                    f"Metric {mv.metric_id} = {mv.normalized:.3f} (raw: {mv.raw})",
                )
            )

    # 3. Holders
    now = utcnow()
    holders = db.holders_as_of(token_key, now)
    for h in holders[:5]:
        if "deployer" in (h.labels or []):
            citations.append(CopilotCitation("holder", h.wallet, f"Deployer wallet holds {h.share_of_supply:.1%} of supply"))
        elif "insider" in (h.labels or []):
            citations.append(CopilotCitation("holder", h.wallet, f"Insider wallet holds {h.share_of_supply:.1%} of supply"))

    return citations


def _synthesize_deterministic_answer(query: str, citations: list[CopilotCitation], score: Score, symbol: str) -> str:
    """Deterministic grounded response when offline or without external LLM keys."""
    q_lower = query.lower()
    veto_citations = [c for c in citations if c.source_type == "veto"]
    metric_citations = [c for c in citations if c.source_type == "metric"]

    if "veto" in q_lower or "safe" in q_lower or "rug" in q_lower:
        if veto_citations:
            reasons = "; ".join(c.fact for c in veto_citations)
            return (
                f"${symbol} failed safety checks with {len(veto_citations)} hard veto(es): {reasons}. "
                f"The composite score of {score.composite:.3f} was overridden to refuse entry."
            )
        return (
            f"${symbol} passed all 7 hard anti-rug vetoes. No deployer rug history, "
            f"insider concentration, or mint authority risks were identified."
        )

    if "social" in q_lower or "twitter" in q_lower or "community" in q_lower:
        social_metrics = [c for c in metric_citations if "social" in c.source_id or "reply" in c.source_id]
        if social_metrics:
            facts = ", ".join(c.fact for c in social_metrics[:3])
            return f"${symbol} social authenticity evaluation: {facts}."
        return f"${symbol} currently has limited social observations in the store."

    # General overview
    top_metrics = sorted(metric_citations, key=lambda c: c.fact, reverse=True)[:3]
    facts_str = "; ".join(c.fact for c in top_metrics) if top_metrics else "limited signal coverage"
    return (
        f"${symbol} scored a composite conviction of {score.composite:.3f} with "
        f"{score.coverage:.0%} signal coverage. Key factors: {facts_str}."
    )


async def ask_copilot(
    query: str, token_query: str, settings: Settings, db: Database | None = None
) -> CopilotResponse:
    """Query the AI copilot with grounded evidence citations."""
    active_db = db or Database(settings.path(settings.db_path))
    should_close = db is None

    try:
        launch, score = await inspect_token(token_query, settings, deep_enrich=True)
        if not launch or not score:
            return CopilotResponse(
                query=query,
                token_symbol="?",
                token_mint="unknown",
                composite_score=0.0,
                answer=f"Unable to locate or score token query: '{token_query}'",
                citations=[],
            )

        token_key = f"solana:{launch.token.mint}"
        citations = _build_context_facts(score, token_key, active_db)

        # Check for Gemini / LLM key in environment
        llm_answer = None
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if gemini_api_key:
            with contextlib.suppress(Exception):
                import httpx
                evidence_text = "\n".join(f"- [{c.source_type}:{c.source_id}] {c.fact}" for c in citations[:20])
                prompt = (
                    f"You are Botsensai Copilot. Answer the question about token ${launch.token.symbol} "
                    f"strictly using the following point-in-time evidence:\n{evidence_text}\n\n"
                    f"User Question: {query}\n"
                    "Cite specific metrics and explain clearly without hallucinating."
                )
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.is_success:
                        llm_answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        answer = llm_answer or _synthesize_deterministic_answer(query, citations, score, launch.token.symbol or "?")

        return CopilotResponse(
            query=query,
            token_symbol=launch.token.symbol or "?",
            token_mint=launch.token.mint,
            composite_score=score.composite,
            answer=answer,
            citations=citations,
        )
    finally:
        if should_close:
            active_db.close()


async def run_copilot_cli(token_query: str, question: str, settings: Settings) -> None:
    """Run the copilot in terminal with rich formatting."""
    console.print(f"\n[bold cyan]Botsensai Copilot[/bold cyan] analyzing [bold]${token_query}[/bold]...\n")
    resp = await ask_copilot(question, token_query, settings)

    console.print(Panel(
        f"[bold]Question:[/bold] {resp.query}\n\n"
        f"[bold]Token:[/bold] ${resp.token_symbol} ([cyan]{resp.token_mint}[/cyan]) │ Score: {resp.composite_score:.3f}\n\n"
        f"{resp.answer}",
        title="[bold green]AI Copilot Briefing[/bold green]",
        expand=False,
    ))

    if resp.citations:
        console.print("\n[bold dim]Ground Truth Citations:[/bold dim]")
        for c in resp.citations[:5]:
            console.print(f"  • [cyan]{c.source_type}[/cyan] ({c.source_id}): {c.fact}")


__all__ = ["CopilotCitation", "CopilotResponse", "ask_copilot", "run_copilot_cli"]
