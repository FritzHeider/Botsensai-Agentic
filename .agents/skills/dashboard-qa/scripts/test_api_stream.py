#!/usr/bin/env python3
"""Automated API & Telemetry Verification Suite for Botsensai.

Tests:
1. FastAPI app initialization and OpenAPI schema generation (/docs).
2. /api/v1/metrics catalogue (signal registry health).
3. /api/v1/regime (system trading mode, weights version, DB entity counts).
4. /api/v1/candidates (candidate scoring retrieval).
5. Both in-memory (fast self-contained testing) and live HTTP socket modes.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from botsensai.api import create_headless_api_app
from botsensai.config import Settings

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    console = None  # type: ignore
    HAS_RICH = False


async def test_live_api(base_url: str) -> bool:
    """Test against a running HTTP server."""
    all_passed = True
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # 1. /docs
        docs_res = await client.get("/docs")
        docs_ok = docs_res.status_code == 200 and "swagger" in docs_res.text.lower()
        _report("Documentation UI", "/docs", docs_ok, f"Status {docs_res.status_code}")
        all_passed = all_passed and docs_ok

        # 2. /api/v1/regime
        reg_res = await client.get("/api/v1/regime")
        reg_ok = False
        reg_detail = f"Status {reg_res.status_code}"
        if reg_res.status_code == 200:
            data = reg_res.json()
            reg_ok = "trading_mode" in data and "weights_version" in data
            reg_detail = f"Mode: {data.get('trading_mode')}, Weights: {data.get('weights_version')}"
        _report("System Regime", "/api/v1/regime", reg_ok, reg_detail)
        all_passed = all_passed and reg_ok

        # 3. /api/v1/metrics
        met_res = await client.get("/api/v1/metrics")
        met_ok = False
        met_detail = f"Status {met_res.status_code}"
        if met_res.status_code == 200:
            metrics = met_res.json()
            met_ok = isinstance(metrics, list) and len(metrics) > 0
            met_detail = f"Catalogue contains {len(metrics)} signals"
        _report("Metrics Catalogue", "/api/v1/metrics", met_ok, met_detail)
        all_passed = all_passed and met_ok

        # 4. /api/v1/candidates
        cand_res = await client.get("/api/v1/candidates?limit=10")
        cand_ok = False
        cand_detail = f"Status {cand_res.status_code}"
        if cand_res.status_code == 200:
            cands = cand_res.json()
            cand_ok = isinstance(cands, list)
            cand_detail = f"Retrieved {len(cands)} candidates"
        _report("Candidates Feed", "/api/v1/candidates", cand_ok, cand_detail)
        all_passed = all_passed and cand_ok

    return all_passed


async def test_in_process_api() -> bool:
    """Test API in-memory using ASGITransport without requiring external port binding."""
    settings = Settings()
    app = create_headless_api_app(settings)
    all_passed = True

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. /docs
        docs_res = await client.get("/docs")
        docs_ok = docs_res.status_code == 200 and "swagger" in docs_res.text.lower()
        _report("Documentation UI", "/docs", docs_ok, f"Status {docs_res.status_code}")
        all_passed = all_passed and docs_ok

        # 2. /api/v1/regime
        reg_res = await client.get("/api/v1/regime")
        reg_ok = False
        reg_detail = f"Status {reg_res.status_code}"
        if reg_res.status_code == 200:
            data = reg_res.json()
            reg_ok = "trading_mode" in data and "weights_version" in data
            reg_detail = f"Mode: {data.get('trading_mode')}, Weights: {data.get('weights_version')}"
        _report("System Regime", "/api/v1/regime", reg_ok, reg_detail)
        all_passed = all_passed and reg_ok

        # 3. /api/v1/metrics
        met_res = await client.get("/api/v1/metrics")
        met_ok = False
        met_detail = f"Status {met_res.status_code}"
        if met_res.status_code == 200:
            metrics = met_res.json()
            met_ok = isinstance(metrics, list) and len(metrics) > 0
            met_detail = f"Catalogue contains {len(metrics)} signals"
        _report("Metrics Catalogue", "/api/v1/metrics", met_ok, met_detail)
        all_passed = all_passed and met_ok

        # 4. /api/v1/candidates
        cand_res = await client.get("/api/v1/candidates?limit=10")
        cand_ok = False
        cand_detail = f"Status {cand_res.status_code}"
        if cand_res.status_code == 200:
            cands = cand_res.json()
            cand_ok = isinstance(cands, list)
            cand_detail = f"Retrieved {len(cands)} candidates"
        _report("Candidates Feed", "/api/v1/candidates", cand_ok, cand_detail)
        all_passed = all_passed and cand_ok

    return all_passed


def _report(name: str, path: str, passed: bool, detail: str) -> None:
    if HAS_RICH and console:
        mark = "[green]✓ PASS[/green]" if passed else "[red]✗ FAIL[/red]"
        console.print(f" {mark} [bold]{name:20}[/bold] [cyan]{path:22}[/cyan] [dim]{detail}[/dim]")
    else:
        mark = "PASS" if passed else "FAIL"
        print(f" [{mark}] {name:20} {path:22} {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Botsensai REST/WebSocket API endpoints")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Test live running server at --host:--port",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host of live server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port of live server (default: 8001)",
    )

    args = parser.parse_args()

    if HAS_RICH and console:
        mode_str = f"Live Server at http://{args.host}:{args.port}" if args.live else "In-Process ASGI App"
        console.print(
            Panel(
                f"Mode: [cyan]{mode_str}[/cyan]",
                title="[bold green]Botsensai API Endpoint QA Suite[/bold green]",
                expand=False,
            )
        )

    if args.live:
        url = f"http://{args.host}:{args.port}"
        passed = asyncio.run(test_live_api(url))
    else:
        passed = asyncio.run(test_in_process_api())

    if HAS_RICH and console:
        if passed:
            console.print("\n[bold green]✓ All API endpoint checks passed successfully![/bold green]\n")
        else:
            console.print("\n[bold red]✗ API checks encountered failures.[/bold red]\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
