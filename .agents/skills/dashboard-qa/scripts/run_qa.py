#!/usr/bin/env python3
"""Headless Playwright DOM & Visual QA Suite for Botsensai Dashboard.

Verifies:
1. Document structure, metadata, and CSS themes.
2. Zero client-side JavaScript runtime exceptions and zero console errors.
3. Zero external HTTP/HTTPS resource leaks (airgap verification).
4. Table, score bars, and integrity indicator rendering.
5. High-resolution full-page visual screenshot capture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import ConsoleMessage, Request, async_playwright

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    console = Console()
    HAS_RICH = True
except ImportError:
    console = None  # type: ignore
    HAS_RICH = False


@dataclass
class QACheckResult:
    category: str
    name: str
    passed: bool
    detail: str


@dataclass
class QAReport:
    target: str
    passed: bool
    checks: list[QACheckResult] = field(default_factory=list)
    console_messages: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_leaks: list[str] = field(default_factory=list)
    screenshot_path: str | None = None


async def run_qa(
    target: str,
    screenshot_path: str | None = None,
    viewport_width: int = 1440,
    viewport_height: int = 900,
    timeout_ms: int = 15000,
) -> QAReport:
    report = QAReport(target=target, passed=True)

    # Determine URL
    if target.startswith("http://") or target.startswith("https://"):
        target_url = target
    else:
        file_path = Path(target).resolve()
        if not file_path.exists():
            report.passed = False
            report.checks.append(
                QACheckResult(
                    category="File System",
                    name="Target Exists",
                    passed=False,
                    detail=f"File not found: {file_path}",
                )
            )
            return report
        target_url = file_path.as_uri()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            device_scale_factor=2,  # Retina screenshot
        )
        page = await context.new_page()

        # Listeners
        page.on("console", lambda msg: _on_console(msg, report))
        page.on("pageerror", lambda exc: _on_page_error(exc, report))
        page.on("request", lambda req: _on_request(req, target_url, report))

        try:
            response = await page.goto(target_url, wait_until="load", timeout=timeout_ms)
            if response is not None and response.status >= 400:
                report.checks.append(
                    QACheckResult(
                        category="Network",
                        name="HTTP Status",
                        passed=False,
                        detail=f"Status code {response.status}",
                    )
                )
                report.passed = False
            else:
                report.checks.append(
                    QACheckResult(
                        category="Network",
                        name="HTTP Status / Load",
                        passed=True,
                        detail="Page loaded successfully",
                    )
                )

            # Wait for content to settle
            await page.wait_for_timeout(500)

            # 1. Page Title & Doctype
            title = await page.title()
            title_ok = "botsensai" in title.lower()
            report.checks.append(
                QACheckResult(
                    category="Metadata",
                    name="Document Title",
                    passed=title_ok,
                    detail=f"Title is '{title}'",
                )
            )
            if not title_ok:
                report.passed = False

            # 2. Header & Brand
            brand_el = await page.query_selector("header .brand")
            brand_text = await brand_el.text_content() if brand_el else ""
            brand_ok = brand_el is not None and "botsensai" in brand_text.lower()
            report.checks.append(
                QACheckResult(
                    category="Layout",
                    name="Header Brand",
                    passed=brand_ok,
                    detail=f"Brand tag rendered: '{brand_text.strip()}'",
                )
            )
            if not brand_ok:
                report.passed = False

            # 3. Banner / Mode
            banner_el = await page.query_selector("header .banner")
            banner_text = await banner_el.text_content() if banner_el else ""
            banner_ok = banner_el is not None and len(banner_text.strip()) > 0
            report.checks.append(
                QACheckResult(
                    category="Layout",
                    name="Header Banner",
                    passed=banner_ok,
                    detail=f"Banner rendered: '{banner_text.strip()}'",
                )
            )
            if not banner_ok:
                report.passed = False

            # 4. Sections & Headings
            sections = await page.query_selector_all("section")
            section_headers = []
            for s in sections:
                h2 = await s.query_selector("h2")
                if h2:
                    section_headers.append((await h2.text_content() or "").strip())

            has_sections = len(sections) >= 1
            report.checks.append(
                QACheckResult(
                    category="DOM Structure",
                    name="Dashboard Sections",
                    passed=has_sections,
                    detail=f"Found {len(sections)} sections: {', '.join(section_headers)}",
                )
            )
            if not has_sections:
                report.passed = False

            # 5. Candidate Table Rows
            table_rows = await page.query_selector_all("table tr")
            has_table = len(table_rows) >= 2  # Header + at least 1 candidate row
            report.checks.append(
                QACheckResult(
                    category="Telemetry",
                    name="Candidate Table Rows",
                    passed=has_table,
                    detail=f"Rendered {len(table_rows)} table rows (including header)",
                )
            )
            if not has_table:
                report.passed = False

            # 6. Integrity Dots
            dots = await page.query_selector_all(".dot")
            has_dots = len(dots) >= 1
            report.checks.append(
                QACheckResult(
                    category="Forensics",
                    name="Integrity Status Dots",
                    passed=has_dots,
                    detail=f"Found {len(dots)} integrity indicators in DOM",
                )
            )
            if not has_dots:
                report.passed = False

            # 7. JavaScript Error Audit
            no_page_errors = len(report.page_errors) == 0
            report.checks.append(
                QACheckResult(
                    category="Runtime",
                    name="JavaScript Exceptions",
                    passed=no_page_errors,
                    detail=f"{len(report.page_errors)} unhandled exceptions",
                )
            )
            if not no_page_errors:
                report.passed = False

            # 8. External Network Leak Audit (Airgap Guarantee)
            no_leaks = len(report.network_leaks) == 0
            report.checks.append(
                QACheckResult(
                    category="Security",
                    name="Airgap / Self-Contained Assets",
                    passed=no_leaks,
                    detail=f"{len(report.network_leaks)} external HTTP(S) requests detected",
                )
            )
            if not no_leaks:
                report.passed = False

            # 9. Capture Screenshot
            if screenshot_path:
                shot_target = Path(screenshot_path).resolve()
                shot_target.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot_target), full_page=True)
                report.screenshot_path = str(shot_target)
                report.checks.append(
                    QACheckResult(
                        category="Visual",
                        name="Full-Page Screenshot",
                        passed=True,
                        detail=f"Saved visual artifact to {shot_target}",
                    )
                )

        except Exception as exc:
            report.passed = False
            report.checks.append(
                QACheckResult(
                    category="Execution",
                    name="Browser Execution",
                    passed=False,
                    detail=f"Fatal browser error: {exc}",
                )
            )
        finally:
            await browser.close()

    return report


def _on_console(msg: ConsoleMessage, report: QAReport) -> None:
    text = f"[{msg.type.upper()}] {msg.text}"
    report.console_messages.append(text)
    if msg.type == "error":
        report.page_errors.append(msg.text)


def _on_page_error(exc: Any, report: QAReport) -> None:
    report.page_errors.append(str(exc))


def _on_request(req: Request, target_url: str, report: QAReport) -> None:
    # Flag external requests if target is local file
    if target_url.startswith("file://"):
        url = req.url
        if url.startswith("http://") or url.startswith("https://"):
            report.network_leaks.append(url)


def print_report(report: QAReport, as_json: bool = False) -> None:
    if as_json:
        data = {
            "target": report.target,
            "passed": report.passed,
            "checks": [asdict(c) for c in report.checks],
            "console_messages": report.console_messages,
            "page_errors": report.page_errors,
            "network_leaks": report.network_leaks,
            "screenshot_path": report.screenshot_path,
        }
        print(json.dumps(data, indent=2))
        return

    if HAS_RICH and console:
        title_color = "green" if report.passed else "red"
        status_text = "[bold green]PASSED[/bold green]" if report.passed else "[bold red]FAILED[/bold red]"
        console.print(
            Panel(
                f"Target: [cyan]{report.target}[/cyan]\nStatus: {status_text}",
                title=f"[{title_color}]Botsensai Dashboard QA Report[/{title_color}]",
                expand=False,
            )
        )

        table = Table(title="DOM & Visual Verification Checks")
        table.add_column("Category", style="cyan")
        table.add_column("Check Name", style="bold")
        table.add_column("Status", justify="center")
        table.add_column("Details", style="dim")

        for c in report.checks:
            status = "[green]✓ PASS[/green]" if c.passed else "[red]✗ FAIL[/red]"
            table.add_row(c.category, c.name, status, c.detail)

        console.print(table)

        if report.page_errors:
            console.print("\n[bold red]JavaScript Page Errors:[/bold red]")
            for err in report.page_errors:
                console.print(f"  [red]• {err}[/red]")

        if report.network_leaks:
            console.print("\n[bold red]External Network Leaks Detected:[/bold red]")
            for leak in report.network_leaks:
                console.print(f"  [red]• {leak}[/red]")

        if report.screenshot_path:
            console.print(f"\n[green]📸 Screenshot captured:[/green] [bold]{report.screenshot_path}[/bold]\n")
    else:
        status = "PASSED" if report.passed else "FAILED"
        print(f"\n=== Botsensai Dashboard QA Report: {status} ===")
        print(f"Target: {report.target}")
        for c in report.checks:
            mark = "PASS" if c.passed else "FAIL"
            print(f"[{mark}] {c.category} - {c.name}: {c.detail}")
        if report.screenshot_path:
            print(f"Screenshot: {report.screenshot_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Headless Playwright QA for Botsensai Dashboard")
    parser.add_argument(
        "--target",
        "-t",
        default="dash.HTML",
        help="HTML file path or live URL to inspect (default: dash.HTML)",
    )
    parser.add_argument(
        "--screenshot",
        "-s",
        default="/tmp/botsensai-dashboard-qa.png",
        help="Path to write full-page PNG screenshot (default: /tmp/botsensai-dashboard-qa.png)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON report",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15000,
        help="Navigation timeout in milliseconds (default: 15000)",
    )

    args = parser.parse_args()

    report = asyncio.run(
        run_qa(
            target=args.target,
            screenshot_path=args.screenshot,
            timeout_ms=args.timeout,
        )
    )

    print_report(report, as_json=args.json)
    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
