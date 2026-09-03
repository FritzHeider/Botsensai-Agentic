"""Interactive X login using Playwright's persistent context.

Solves the macOS Keychain encryption mismatch between standard Google Chrome and Playwright.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel

from botsensai.collectors.x_session import AuthenticatedXCollector
from botsensai.config import Settings

console = Console()


async def main() -> None:
    settings = Settings()
    profile_dir = Path.home() / "botsensai-x-profile"
    if settings.browser.user_data_dir:
        profile_dir = Path(settings.browser.user_data_dir).expanduser()

    profile_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel(
            f"Launching Playwright Chromium using profile:\n[cyan]{profile_dir}[/cyan]\n\n"
            "1. Log in to your X throwaway account in the browser window.\n"
            "2. Make sure you see your home feed.\n"
            "3. Close the browser window or press Enter here when done.",
            title="Interactive X Login (Playwright Native)",
            expand=False,
        )
    )

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://x.com/login")

        print("\nBrowser is open! Log in on X, and once you see your home feed, press ENTER here...")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sys.stdin.readline)
        except Exception:
            pass
        finally:
            await context.close()

    console.print("\n[bold]Verifying session with Botsensai...[/bold]")
    settings.browser.user_data_dir = str(profile_dir)
    collector = AuthenticatedXCollector(settings)
    try:
        status = await collector.verify_session(force=True)
    finally:
        await collector.aclose()

    if status.authenticated:
        console.print(Panel(f"[green]{status.explain()}[/green]", title="Success"))
    else:
        console.print(Panel(f"[yellow]{status.explain()}[/yellow]", title="Failed"))


if __name__ == "__main__":
    asyncio.run(main())
