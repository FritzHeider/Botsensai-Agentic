"""Headless REST & WebSocket API Server for Botsensai 2.0.

Provides OpenAPI-compliant endpoints and real-time streaming for external
algorithmic agents, dashboards, and portfolio trackers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request

from botsensai.config import Settings, get_settings
from botsensai.inspector import inspect_token
from botsensai.metrics import metric_catalogue
from botsensai.models import utcnow
from botsensai.store.db import Database

router = APIRouter(prefix="/api/v1", tags=["Botsensai v1 API"])


def _get_active_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("/metrics")
async def get_metrics_catalogue() -> list[dict[str, Any]]:
    """Return catalogue of all registered adversarial signals."""
    return metric_catalogue()


@router.get("/regime")
async def get_system_regime(request: Request) -> dict[str, Any]:
    """Return active market regime and dataset stats."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        counts = db.counts()
        return {
            "trading_mode": settings.trading_mode.value,
            "weights_version": settings.scoring.weights_version,
            "counts": counts,
            "timestamp": utcnow().isoformat(),
        }
    finally:
        db.close()


@router.get("/tokens/{mint}/score")
async def get_token_score(request: Request, mint: str) -> dict[str, Any]:
    """Score a token by mint address and return full adversarial dossier."""
    settings = _get_active_settings(request)
    launch, score = await inspect_token(mint, settings, deep_enrich=True)
    if not launch or not score:
        return {"error": "Token not found or scoring failed", "mint": mint}

    return {
        "symbol": launch.token.symbol or "?",
        "mint": launch.token.mint,
        "composite": score.composite,
        "coverage": score.coverage,
        "regime": score.regime,
        "vetoes": list(score.vetoes or []),
        "explanation": score.explanation,
        "timestamp": score.as_of.isoformat(),
    }


@router.get("/candidates")
async def get_top_candidates(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    """Retrieve top ranked token candidates from recent sweeps."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        scores = db.recent_scores(limit=limit)
        return [
            {
                "token_key": r["token_key"],
                "composite": r["composite"],
                "coverage": r["coverage"],
                "vetoes": list(r["vetoes"] or []),
                "as_of": r["as_of"].isoformat() if hasattr(r["as_of"], "isoformat") else str(r["as_of"]),
            }
            for r in scores
        ]
    finally:
        db.close()


def create_headless_api_app(settings: Settings | None = None) -> FastAPI:
    """Instantiate headless FastAPI application."""
    active_settings = settings or get_settings()
    app = FastAPI(
        title="Botsensai 2.0 Headless API",
        description="REST and WebSocket programmatic interface for adversarial memecoin scoring",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = active_settings
    app.include_router(router)
    return app


__all__ = ["create_headless_api_app", "router"]
