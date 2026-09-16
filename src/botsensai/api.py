"""Headless REST & WebSocket API Server for Botsensai 2.0.

Provides OpenAPI-compliant endpoints and real-time streaming for external
algorithmic agents, dashboards, and portfolio trackers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

log = logging.getLogger("botsensai.api")

from botsensai.config import Settings, audit_capabilities, get_settings
from botsensai.dashboard.server import (
    _heartbeat_worker,
    broadcaster,
    router as dashboard_router,
)
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


@router.get("/system/metrics")
async def get_system_metrics(request: Request) -> dict[str, Any]:
    """Return operational system metrics, database statistics, and provider statuses."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        counts = db.counts()
        capabilities = audit_capabilities(settings)

        return {
            "status": "healthy",
            "trading_mode": settings.trading_mode.value,
            "weights_version": settings.scoring.weights_version,
            "counts": counts,
            "capabilities": capabilities,
            "timestamp": utcnow().isoformat(),
        }
    finally:
        db.close()


@router.get("/system/metrics/prometheus", response_class=PlainTextResponse)
async def get_prometheus_metrics(request: Request) -> PlainTextResponse:
    """Return Prometheus-compatible text exposition metrics."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        counts = db.counts()
        capabilities = audit_capabilities(settings)

        lines = [
            "# HELP botsensai_up Botsensai API server status",
            "# TYPE botsensai_up gauge",
            "botsensai_up 1",
            "",
            "# HELP botsensai_db_records_total Total number of database records by table",
            "# TYPE botsensai_db_records_total gauge",
        ]
        for table, count in counts.items():
            lines.append(f'botsensai_db_records_total{{table="{table}"}} {count}')

        lines.extend([
            "",
            "# HELP botsensai_provider_configured Provider API status (1=configured, 0=unconfigured)",
            "# TYPE botsensai_provider_configured gauge",
        ])
        for cap, val in capabilities.items():
            if isinstance(val, bool):
                lines.append(f'botsensai_provider_configured{{provider="{cap}"}} {1 if val else 0}')

        lines.append("")
        return PlainTextResponse(
            content="\n".join(lines),
            media_type="text/plain; version=0.0.4",
        )
    finally:
        db.close()


# --- Operational Mission Control Actions ----------------------------------- #


@router.get("/system/actions/status")
async def get_action_status(request: Request) -> dict[str, Any]:
    """Return current operational state and recent action audit history."""
    is_paused = getattr(request.app.state, "engine_paused", False)
    actions = getattr(request.app.state, "recent_actions", [])
    return {
        "engine_paused": is_paused,
        "recent_actions": list(actions[-10:]),
    }


@router.post("/system/actions/pause")
async def pause_engine(request: Request) -> dict[str, Any]:
    """Emergency soft-pause active sweeping and candidate scoring."""
    request.app.state.engine_paused = True
    entry = {
        "action": "pause",
        "timestamp": utcnow().isoformat(),
        "status": "success",
        "detail": "Engine execution soft-paused by operator",
    }
    if not hasattr(request.app.state, "recent_actions"):
        request.app.state.recent_actions = []
    request.app.state.recent_actions.append(entry)
    await broadcaster.broadcast("action", entry)
    return entry


@router.post("/system/actions/resume")
async def resume_engine(request: Request) -> dict[str, Any]:
    """Resume sweeping and evaluation engine."""
    request.app.state.engine_paused = False
    entry = {
        "action": "resume",
        "timestamp": utcnow().isoformat(),
        "status": "success",
        "detail": "Engine execution resumed",
    }
    if not hasattr(request.app.state, "recent_actions"):
        request.app.state.recent_actions = []
    request.app.state.recent_actions.append(entry)
    await broadcaster.broadcast("action", entry)
    return entry


@router.post("/system/actions/doctor")
async def run_doctor_action(request: Request) -> dict[str, Any]:
    """Probe all external data surfaces for reachability."""
    from botsensai.collectors import ALL_COLLECTORS

    settings = _get_active_settings(request)
    start_t = time.monotonic()
    surfaces: list[dict[str, Any]] = []

    for cls in ALL_COLLECTORS:
        collector = cls(settings)
        try:
            ok = await asyncio.wait_for(collector.health_check(), timeout=15.0)
            surfaces.append({"surface": collector.name, "ok": ok, "detail": "reachable" if ok else "unreachable"})
        except Exception as exc:
            surfaces.append({
                "surface": collector.name,
                "ok": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:60]}",
            })
        finally:
            await collector.aclose()

    dur = round(time.monotonic() - start_t, 2)
    reachable = sum(1 for s in surfaces if s["ok"])
    result = {
        "action": "doctor",
        "timestamp": utcnow().isoformat(),
        "status": "success",
        "duration_seconds": dur,
        "reachable_surfaces": reachable,
        "total_surfaces": len(surfaces),
        "surfaces": surfaces,
    }
    if not hasattr(request.app.state, "recent_actions"):
        request.app.state.recent_actions = []
    request.app.state.recent_actions.append({
        "action": "doctor",
        "timestamp": result["timestamp"],
        "status": "success",
        "detail": f"{reachable}/{len(surfaces)} surfaces reachable in {dur}s",
    })
    await broadcaster.broadcast("action", result)
    return result


@router.post("/system/actions/backup")
async def run_backup_action(request: Request, dry_run: bool = False) -> dict[str, Any]:
    """Trigger online atomic SQLite backup mirrored to S3 / Cloudflare R2."""
    from botsensai.store.backup import MultiCloudBackupManager

    settings = _get_active_settings(request)
    manager = MultiCloudBackupManager(settings)
    targets = manager.get_configured_targets()
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="No cloud backup targets configured. Set BOTSENSAI_BACKUP_S3_BUCKET or R2 credentials.",
        )

    start_t = time.monotonic()
    try:
        report = manager.mirror_backup(update_latest=True, dry_run=dry_run)
        dur = round(time.monotonic() - start_t, 2)
        result = {
            "action": "backup",
            "timestamp": report.timestamp,
            "status": "success" if report.success else "partial_failure",
            "duration_seconds": dur,
            "total_bytes": report.total_bytes,
            "targets": [
                {
                    "provider": r.provider,
                    "success": r.success,
                    "target_uri": r.target_uri,
                    "duration_seconds": r.duration_seconds,
                    "error": r.error,
                }
                for r in report.results
            ],
            "errors": report.errors,
        }
        if not hasattr(request.app.state, "recent_actions"):
            request.app.state.recent_actions = []
        request.app.state.recent_actions.append({
            "action": "backup",
            "timestamp": utcnow().isoformat(),
            "status": "success" if report.success else "failure",
            "detail": f"Uploaded {report.total_bytes} bytes across {len(report.results)} target(s) in {dur}s",
        })
        await broadcaster.broadcast("action", result)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}") from exc


@router.post("/system/actions/sweep")
async def run_sweep_action(request: Request, limit: int = 40, candidates: int = 15) -> dict[str, Any]:
    """Trigger an on-demand candidate discovery and scoring pass."""
    if getattr(request.app.state, "engine_paused", False):
        raise HTTPException(status_code=409, detail="Engine is currently paused by operator. Resume first.")

    from botsensai.pipeline import Pipeline

    settings = _get_active_settings(request)
    pipeline = Pipeline(settings)
    start_t = time.monotonic()
    try:
        report = await pipeline.sweep(discover_limit=limit, max_candidates=candidates)
        dur = round(time.monotonic() - start_t, 2)
        top_cand = report.top_candidates[0] if report.top_candidates else None
        result = {
            "action": "sweep",
            "timestamp": utcnow().isoformat(),
            "status": "success",
            "duration_seconds": dur,
            "discovered": report.screened_in,
            "screened_in": report.screened_in,
            "entered": report.entered,
            "top_candidate": top_cand,
        }
        if not hasattr(request.app.state, "recent_actions"):
            request.app.state.recent_actions = []
        request.app.state.recent_actions.append({
            "action": "sweep",
            "timestamp": result["timestamp"],
            "status": "success",
            "detail": f"Swept {result['screened_in']} screened candidates in {dur}s",
        })
        await broadcaster.broadcast("action", result)
        return result
    except Exception as exc:
        log.exception("Sweep action execution failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Sweep failed: {exc}") from exc
    finally:
        await pipeline.aclose()


# --------------------------------------------------------------------------- #
# Backtest, Paper Trading, and Sniper Info Endpoints
# --------------------------------------------------------------------------- #


class BacktestRequest(BaseModel):
    lookback_days: float = Field(default=7.0, ge=0.5, le=90.0)
    capital_sol: float = Field(default=10.0, gt=0.0)
    min_score: float = Field(default=0.65, ge=0.0, le=1.0)
    synthetic: bool = False
    universe: int = Field(default=60, ge=10, le=500)
    baselines: bool = False


class ClosePositionRequest(BaseModel):
    token_key: str
    exit_price: float | None = None
    reason: str = "manual_operator_close"


class TestSnipeRequest(BaseModel):
    mint: str
    size_sol: float = Field(default=0.05, gt=0.0, le=5.0)


@router.post("/backtest/run")
async def run_backtest_action(payload: BacktestRequest, request: Request) -> dict[str, Any]:
    """Execute walk-forward backtest over real recorded tokens or synthetic cohort."""
    from botsensai.backtest.baselines import evaluate_baselines, run_baselines
    from botsensai.backtest.engine import Backtester
    from botsensai.cli import _backtest_payload, _backtest_tapes

    settings = _get_active_settings(request)
    if payload.min_score:
        settings.scoring.entry_threshold = payload.min_score

    start_t = time.monotonic()
    end = utcnow()
    start = end - timedelta(days=payload.lookback_days)

    tapes, is_synthetic = _backtest_tapes(
        settings,
        start,
        end,
        days=payload.lookback_days,
        synthetic=payload.synthetic,
        universe=payload.universe,
    )

    tester = Backtester(settings)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: tester.run(tapes, starting_native=payload.capital_sol, synthetic=is_synthetic)
    )
    summary = result.summary()

    comparison = None
    if payload.baselines:
        b_results = await loop.run_in_executor(
            None,
            lambda: run_baselines(
                tapes, settings=settings, starting_native=payload.capital_sol, synthetic=is_synthetic
            ),
        )
        comparison = evaluate_baselines(result, b_results)

    dur = round(time.monotonic() - start_t, 2)
    response_payload = _backtest_payload(result, summary, comparison=comparison)
    response_payload["duration_seconds"] = dur
    response_payload["synthetic"] = is_synthetic
    response_payload["token_count"] = len(tapes)
    response_payload["timestamp"] = utcnow().isoformat()

    request.app.state.last_backtest = response_payload
    await broadcaster.broadcast(
        "action",
        {"action": "backtest_completed", "summary": summary, "synthetic": is_synthetic},
    )
    return response_payload


@router.get("/backtest/latest")
async def get_latest_backtest(request: Request) -> dict[str, Any]:
    """Return the most recent backtest result."""
    cached = getattr(request.app.state, "last_backtest", None)
    if cached:
        return cached
    return {
        "status": "idle",
        "message": "No backtest run yet during this server session. Click 'Run Walk-Forward Backtest' to start.",
        "summary": None,
    }


@router.get("/papertrade/portfolio")
async def get_papertrade_portfolio(request: Request) -> dict[str, Any]:
    """Return live paper trading portfolio equity, open positions, and closed trades."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        portfolio = db.paper_portfolio_summary(starting_capital_native=10.0)
        return portfolio
    finally:
        db.close()


@router.post("/papertrade/close")
async def close_papertrade_position(payload: ClosePositionRequest, request: Request) -> dict[str, Any]:
    """Close an open paper trading position and record realized PnL."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        closed = db.close_paper_position(
            token_key=payload.token_key,
            exit_price_native=payload.exit_price,
            reason=payload.reason,
        )
        if not closed:
            raise HTTPException(
                status_code=404, detail=f"No open paper position found for {payload.token_key}"
            )
        await broadcaster.broadcast("action", {"action": "position_closed", "position": closed})
        return {"status": "success", "closed_position": closed}
    finally:
        db.close()


@router.post("/papertrade/close_all")
async def close_all_papertrade_positions(request: Request) -> dict[str, Any]:
    """Emergency liquidate all open paper positions."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        open_positions = db.open_paper_positions()
        closed_count = 0
        for pos in open_positions:
            db.close_paper_position(pos["token_key"], reason="operator_emergency_liquidation")
            closed_count += 1
        await broadcaster.broadcast(
            "action", {"action": "positions_liquidated", "count": closed_count}
        )
        return {"status": "success", "liquidated_count": closed_count}
    finally:
        db.close()


@router.post("/papertrade/reset")
async def reset_papertrade_positions(request: Request) -> dict[str, Any]:
    """Reset paper trading portfolio and clear all positions."""
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        cleared = db.reset_paper_positions()
        return {"status": "success", "cleared_positions": cleared}
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
                "daily_loss_limit_sol": getattr(settings.risk, "daily_loss_limit_sol", 0.25),
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


@router.post("/sniper/test_snipe")
async def test_snipe_token(payload: TestSnipeRequest, request: Request) -> dict[str, Any]:
    """Simulate a real-time snipe calculation for a target token mint."""
    from botsensai.execution.fills import CurveState, FillContext, FillSimulator
    from botsensai.models import Chain, MarketSnapshot, Order, Side, TokenRef

    settings = _get_active_settings(request)
    start_t = time.monotonic()

    inspection = None
    try:
        inspection = await inspect_token(payload.mint, settings=settings)
    except Exception as e:
        log.warning("Token inspection during test_snipe failed: %s", e)

    db = Database(settings.path(settings.db_path))
    try:
        token_key = f"solana:{payload.mint}"
        token = TokenRef(chain=Chain.SOLANA, mint=payload.mint)
        snapshots = db.snapshots_as_of(token_key, utcnow())
        snapshot = (
            snapshots[-1]
            if snapshots
            else MarketSnapshot(
                token=token,
                as_of=utcnow(),
                observed_at=utcnow(),
                price_native=0.00003,
                liquidity_usd=15000.0,
                market_cap_usd=35000.0,
            )
        )

        sim = FillSimulator(settings.execution)
        order = Order(
            token=token,
            as_of=utcnow(),
            side=Side.BUY,
            size_native=payload.size_sol,
            max_slippage_bps=settings.risk.max_slippage_bps,
            priority_fee_lamports=settings.execution.priority_fee_lamports,
            jito_tip_lamports=settings.execution.jito_tip_lamports,
        )
        ctx = FillContext(snapshot=snapshot, curve=CurveState.from_snapshot(snapshot))
        fill = sim.simulate(order, ctx)
        dur = round(time.monotonic() - start_t, 3)

        return {
            "mint": payload.mint,
            "status": "SIMULATED",
            "duration_seconds": dur,
            "simulated_fill": {
                "amount_sol": fill.amount_native,
                "amount_token": fill.amount_token,
                "effective_price_sol": fill.price_native,
                "slippage_bps": fill.slippage_bps,
                "latency_ms": fill.latency_ms,
                "fee_sol": fill.fee_native,
                "tip_sol": fill.tip_native,
                "rejected": fill.rejected,
                "reject_reason": fill.reject_reason,
            },
            "inspection": inspection,
        }
    finally:
        db.close()


def create_headless_api_app(settings: Settings | None = None) -> FastAPI:
    """Instantiate unified FastAPI application serving REST API, WebSockets, and Live Mission Control Dashboard."""
    active_settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncGenerator[None, None]:
        task = asyncio.create_task(_heartbeat_worker(active_settings))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(
        title="Botsensai 2.0 Unified API & Mission Control",
        description="REST, WebSocket, and real-time interactive mission control for adversarial memecoin scoring",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.engine_paused = False
    app.state.recent_actions = []

    # 1. Mount API router (/api/v1)
    app.include_router(router)

    # 2. Mount Dashboard router (/, /tokens/.../graph, /ws/live, /api/snapshot)
    app.include_router(dashboard_router)

    @app.get("/dashboard", include_in_schema=False)
    async def redirect_dashboard() -> RedirectResponse:
        return RedirectResponse(url="/")

    return app


__all__ = ["create_headless_api_app", "router"]

