"""Real-time FastAPI & WebSocket live dashboard server for Botsensai.

Provides a self-contained, zero-build real-time monitoring interface with
WebSocket event streaming for mint discovery, scoring, vetoes, and paper trades.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from botsensai.config import Settings, get_settings
from botsensai.dashboard.data import build_snapshot
from botsensai.dashboard.graph import build_topology_graph, render_topology_html
from botsensai.metrics import metric_catalogue
from botsensai.models import utcnow
from botsensai.store.db import Database

TEMPLATE_DIR = Path(__file__).parent / "templates"


class EventBroadcaster:
    """Thread-safe and async-safe WebSocket event broadcast manager."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {
            "type": event_type,
            "timestamp": utcnow().isoformat(),
            "data": data,
        }
        message = json.dumps(payload, default=str)
        async with self._lock:
            dead: list[WebSocket] = []
            for connection in self.active_connections:
                try:
                    await connection.send_text(message)
                except Exception:
                    dead.append(connection)
            for d in dead:
                if d in self.active_connections:
                    self.active_connections.remove(d)


broadcaster = EventBroadcaster()
router = APIRouter()


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _get_active_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> str:
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        snapshot = build_snapshot(settings, db)
    finally:
        db.close()

    env = _jinja_env()
    template_name = (
        "live_dashboard.html.j2"
        if (TEMPLATE_DIR / "live_dashboard.html.j2").exists()
        else "dashboard.html.j2"
    )
    template = env.get_template(template_name)
    return template.render(snap=snapshot)


@router.get("/api/snapshot")
async def get_snapshot(request: Request) -> dict[str, Any]:
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        return build_snapshot(settings, db)
    finally:
        db.close()


@router.get("/api/metrics")
async def list_metrics() -> list[dict[str, Any]]:
    return metric_catalogue()


@router.get("/api/candidates")
async def get_candidates(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        scores = db.recent_scores(limit=limit)
        return [
            {
                "token_key": r["token_key"],
                "symbol": r["token_key"].split(":")[-1][:12],
                "composite": r["composite"],
                "coverage": r["coverage"],
                "regime": r["regime"],
                "vetoes": list(r["vetoes"] or []),
                "as_of": r["as_of"].isoformat() if isinstance(r["as_of"], datetime) else r["as_of"],
                "explanation": r["explanation"],
            }
            for r in scores
        ]
    finally:
        db.close()


@router.get("/api/tokens/{mint}/graph")
async def get_token_graph(request: Request, mint: str) -> dict[str, Any]:
    settings = _get_active_settings(request)
    token_key = f"solana:{mint}" if not mint.startswith("solana:") else mint
    db = Database(settings.path(settings.db_path))
    try:
        graph = build_topology_graph(token_key, db)
        return graph.to_dict()
    finally:
        db.close()


@router.get("/tokens/{mint}/graph", response_class=HTMLResponse)
async def view_token_graph(request: Request, mint: str) -> str:
    settings = _get_active_settings(request)
    token_key = f"solana:{mint}" if not mint.startswith("solana:") else mint
    db = Database(settings.path(settings.db_path))
    try:
        graph = build_topology_graph(token_key, db)
        return render_topology_html(graph)
    finally:
        db.close()


@router.get("/api/health")
async def get_health(request: Request) -> dict[str, Any]:
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        counts = db.counts()
        return {"status": "ok", "timestamp": utcnow().isoformat(), "counts": counts}
    finally:
        db.close()


@router.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    await broadcaster.connect(websocket)
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    try:
        db = Database(settings.path(settings.db_path))
        snapshot = build_snapshot(settings, db)
        db.close()
        await websocket.send_text(
            json.dumps(
                {"type": "init", "timestamp": utcnow().isoformat(), "data": snapshot},
                default=str,
            )
        )
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket)
    except Exception:
        await broadcaster.disconnect(websocket)


async def _heartbeat_worker(settings: Settings) -> None:
    while True:
        await asyncio.sleep(5.0)
        try:
            db = Database(settings.path(settings.db_path))
            counts = db.counts()
            db.close()
            await broadcaster.broadcast("heartbeat", {"counts": counts})
        except Exception:
            pass


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the live dashboard FastAPI application."""
    active_settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncGenerator[None, None]:
        task = asyncio.create_task(_heartbeat_worker(active_settings))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(
        title="Botsensai Live Dashboard",
        description="Real-time agentic memecoin recon & scoring interface",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.include_router(router)
    return app


__all__ = ["EventBroadcaster", "broadcaster", "create_app"]
