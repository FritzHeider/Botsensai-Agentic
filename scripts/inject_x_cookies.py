#!/usr/bin/env python3
"""Inject X session cookies directly into Playwright's persistent profile.

Enables headless authenticated X collection on remote EC2 servers without needing a GUI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel

console = Console()

DEFAULT_PROFILE_DIR = "/home/ubuntu/botsensai-x-profile"


def patch_botsensai_config(config_path: Path, profile_dir: Path) -> None:
    """Update config/botsensai.yaml to enable x_session and set user_data_dir."""
    if not config_path.exists():
        console.print(f"[yellow]Config file not found at {config_path}, skipping config patch.[/yellow]")
        return

    text = config_path.read_text(encoding="utf-8")

    # Update browser.user_data_dir
    text = re.sub(
        r"(user_data_dir:\s*).*$",
        rf"\g<1>{profile_dir}",
        text,
        flags=re.MULTILINE,
    )

    # Update only the x_session block specifically
    def fix_x_session(match: re.Match) -> str:
        block = match.group(0)
        block = re.sub(r"(^\s+enabled:\s*).*$", r"\g<1>true", block, flags=re.MULTILINE)
        block = re.sub(r"(^\s+acknowledged_burner:\s*).*$", r"\g<1>true", block, flags=re.MULTILINE)
        return block

    if "x_session:" in text:
        text = re.sub(r"^x_session:\s*\n(?:\s+.*\n)*", fix_x_session, text, flags=re.MULTILINE)
    else:
        text += f"\n\nx_session:\n  enabled: true\n  acknowledged_burner: true\n"

    config_path.write_text(text, encoding="utf-8")
    console.print(f"[green]Successfully patched {config_path} with user_data_dir and enabled x_session.[/green]")


async def inject_cookies(
    auth_token: str,
    ct0: str,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    config_file: str = "config/botsensai.yaml",
) -> bool:
    target_dir = Path(profile_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel(
            f"Injecting cookies into Playwright persistent profile:\n[cyan]{target_dir}[/cyan]",
            title="X Session Cookie Injection",
            expand=False,
        )
    )

    clean_auth = auth_token.strip().strip('"').strip("'")
    clean_ct0 = ct0.strip().strip('"').strip("'")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(target_dir),
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 800},
        )

        # Inject auth cookies for both .x.com and .twitter.com with 1-year expiry
        exp = int(time.time()) + 365 * 86400
        cookies = [
            {
                "name": "auth_token",
                "value": clean_auth,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": exp,
            },
            {
                "name": "ct0",
                "value": clean_ct0,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "expires": exp,
            },
            {
                "name": "auth_token",
                "value": clean_auth,
                "domain": ".twitter.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": exp,
            },
            {
                "name": "ct0",
                "value": clean_ct0,
                "domain": ".twitter.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "expires": exp,
            },
        ]
        await context.add_cookies(cookies)

        page = context.pages[0] if context.pages else await context.new_page()
        console.print("Probing [bold]https://x.com/home[/bold] with injected session...")
        try:
            resp = await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(4.0)
            html = await page.content()
            text = await page.evaluate("() => document.body.innerText")
            # Save storage state (cookies + local storage) for persistent sessions
            state_file = target_dir / "storage_state.json"
            await context.storage_state(path=str(state_file))
            console.print(f"[green]Saved storage state to {state_file}[/green]")
        except Exception as e:
            console.print(f"[yellow]Navigation probe notice: {e}[/yellow]")
            html = await page.content() if page else ""
            text = ""
        finally:
            await context.close()

    # Verify authentication markers
    blob = f"{html}\n{text}"
    authenticated = False
    handle = None

    if 'data-testid="SideNav_AccountSwitcher_Button"' in blob or 'data-testid="AppTabBar_Home_Link"' in blob:
        authenticated = True
    elif '"authenticated":true' in html or '"is_authenticated":true' in html:
        authenticated = True

    # Try extracting handle
    match = re.search(r'data-testid="UserAvatar-Container-([a-zA-Z0-9_]+)"', blob)
    if match:
        handle = match.group(1)

    if authenticated:
        console.print(
            Panel(
                f"[green]Session successfully verified as authenticated![/green]\n"
                f"Handle: @{handle or 'unknown'}\n"
                f"Profile directory: {target_dir}",
                title="Authentication Verified",
            )
        )
        patch_botsensai_config(Path(config_file), target_dir)
        return True
    else:
        console.print(
            Panel(
                "[yellow]Could not definitively confirm login state from probe HTML.[/yellow]\n"
                "Cookies were saved to profile. You can verify with `botsensai x-session`.",
                title="Probe Warning",
            )
        )
        patch_botsensai_config(Path(config_file), target_dir)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject X auth cookies into headless Playwright profile")
    parser.add_argument("--auth-token", default=os.environ.get("X_AUTH_TOKEN"), help="X auth_token cookie value")
    parser.add_argument("--ct0", default=os.environ.get("X_CT0"), help="X ct0 cookie value")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Profile directory")
    parser.add_argument("--config", default="config/botsensai.yaml", help="Path to botsensai.yaml")
    args = parser.parse_args()

    auth = args.auth_token
    ct0 = args.ct0

    if not auth:
        auth = input("Enter X auth_token cookie: ").strip()
    if not ct0:
        ct0 = input("Enter X ct0 cookie: ").strip()

    if not auth or not ct0:
        console.print("[red]Both auth_token and ct0 are required.[/red]")
        sys.exit(1)

    asyncio.run(inject_cookies(auth, ct0, args.profile_dir, args.config))


if __name__ == "__main__":
    main()
