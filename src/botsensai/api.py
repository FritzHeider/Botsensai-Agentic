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


@router.get("/sniper/status")
async def get_sniper_status(request: Request) -> dict[str, Any]:
    """Return sniper execution engine status, parameters, and telemetry."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        pending = db.pending_signals(limit=10)
        recent = db.recent_signals(limit=20)
        from botsensai.execution.jito_tips import JitoTipEngine
        tip_floor = JitoTipEngine.get_instance(settings).current_floor

        return {
            "mode": settings.trading_mode.value.upper(),
            "dry_run": True,
            "safety_invariants": {
                "signing_enabled": False,
                "zero_signing_verified": True,
                "canary_max_sol": settings.risk.max_position_native,
                "max_slippage_bps": settings.risk.max_slippage_bps,
            },
            "profitability_engine": {
                "yellowstone_grpc_active": bool(settings.yellowstone_grpc_endpoint),
                "dynamic_jito_tips": settings.execution.dynamic_jito_tips,
                "jito_tip_floor_p50_lamports": tip_floor.p50_lamports,
                "jito_tip_floor_p75_lamports": tip_floor.p75_lamports,
                "kelly_sizing_enabled": settings.risk.use_kelly_sizing,
                "kelly_fraction": settings.risk.kelly_fraction,
                "momentum_stop_seconds": settings.risk.momentum_stop_seconds,
                "curve_auto_exit_pct": settings.risk.curve_auto_exit_pct,
                "trailing_breakeven_gain_pct": settings.risk.trailing_breakeven_gain_pct,
                "trailing_breakeven_floor_pct": settings.risk.trailing_breakeven_floor_pct,
                "anti_sandwich_bundling": settings.execution.anti_sandwich_private_bundle,
                "dev_bundler_sybil_defense": True,
                "golden_curve_window": "2% - 85%",
            },
            "execution": {
                "jito_tip_lamports": settings.execution.jito_tip_lamports,
                "priority_fee_lamports": settings.execution.priority_fee_lamports,
                "jito_block_engine": "mainnet.block-engine.jito.wtf",
            },
            "outbox": {
                "pending_signals": len(pending),
                "total_recorded_signals": len(recent),
            },
            "recent_signals": recent,
        }
    finally:
        db.close()


@router.get("/sniper/signals")
async def get_sniper_signals(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    """Retrieve recent sniper signals from the execution outbox."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        return db.recent_signals(limit=limit)
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
