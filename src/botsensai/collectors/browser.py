"""Web-use driver: a real browser as the primary data path.

The user's requirement is that Botsensai reaches these surfaces the way a person
does, because the interesting data — pump.fun replies, X engagement counts,
Instagram reach, GMGN wallet labels — is rendered client-side and is either
absent from public APIs or gated behind pricing that makes systematic collection
impractical.

Three capabilities matter here and none of them are available from plain HTTP:

* **XHR interception.** Most of these apps fetch their own JSON from their own
  backend. Watching the network tab gives us that JSON in its native shape,
  already authenticated by the page's own session, without reverse-engineering
  or forging request signatures. This is the single highest-value technique in
  the file and `capture_json` implements it.
* **Session reuse.** Pointing `user_data_dir` at a logged-in Chrome profile lets
  the collector see what the operator can see, and nothing more.
* **Rendered text.** For surfaces with no usable XHR, the DOM after hydration is
  still a data source.

Everything is paced by the same `Pacer` the HTTP client uses, so browser traffic
and API traffic share one budget per surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

from botsensai.config import BrowserSettings
from botsensai.util.logging import get_logger
from botsensai.util.ratelimit import Pacer

log = get_logger(__name__)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : originalQuery(parameters)
);
"""


class BrowserUnavailableError(RuntimeError):
    """Raised when Playwright is not installed or no browser could be launched."""


@dataclass
class CapturedResponse:
    """One JSON response observed on the wire while a page loaded."""

    url: str
    status: int
    body: Any
    method: str = "GET"
    resource_type: str = "xhr"

    def matches(self, pattern: str) -> bool:
        return re.search(pattern, self.url) is not None


@dataclass
class PageResult:
    """Everything one page visit produced."""

    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    text: str = ""
    captured: list[CapturedResponse] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    def json_matching(self, pattern: str) -> list[Any]:
        """Bodies of every captured response whose URL matches `pattern`."""
        return [c.body for c in self.captured if c.matches(pattern) and c.body is not None]

    def first_json(self, pattern: str) -> Any | None:
        for c in self.captured:
            if c.matches(pattern) and c.body is not None:
                return c.body
        return None


def _response_listener(
    url: str, capture_patterns: Sequence[str], captured: list[CapturedResponse]
) -> Callable[[Any], Coroutine[Any, Any, None]]:
    """Build the response handler that fills `captured`.

    Attached before navigation so the app's own bootstrap fetches are seen —
    which is where the data actually lives. It swallows everything: a listener
    that raises would kill the visit that is still loading.
    """

    async def on_response(response: Any) -> None:
        try:
            if capture_patterns and not any(re.search(p, response.url) for p in capture_patterns):
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype and capture_patterns == ():
                return
            body: Any = None
            if "json" in ctype:
                with contextlib.suppress(Exception):
                    body = await response.json()
            if body is None:
                with contextlib.suppress(Exception):
                    body = await response.text()
            captured.append(
                CapturedResponse(
                    url=response.url,
                    status=response.status,
                    body=body,
                    method=response.request.method,
                    resource_type=response.request.resource_type,
                )
            )
        except Exception as exc:  # never let a listener kill the visit
            log.debug("browser.capture_error", url=url, error=str(exc))

    return on_response


def _route_blocker(blocked: set[str]) -> Callable[[Any], Coroutine[Any, Any, None]]:
    """Build the route handler that aborts the blocked resource types."""

    async def route_handler(route: Any) -> None:
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    return route_handler


class WebUseDriver:
    """Managed Playwright browser shared across collectors.

    Lazily launches on first use so importing this module costs nothing, and so
    the rest of the system works with Playwright absent (every collector has an
    HTTP or fixture fallback).
    """

    def __init__(self, settings: BrowserSettings | None = None) -> None:
        self.settings = settings or BrowserSettings()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()
        self._page_semaphore = asyncio.Semaphore(self.settings.max_pages)
        self.available = True

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        async with self._lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                self.available = False
                raise BrowserUnavailableError(
                    "playwright is not installed. Install with: pip install 'botsensai[browser]' "
                    "&& playwright install chromium"
                ) from exc

            self._playwright = await async_playwright().start()
            launcher = getattr(self._playwright, self.settings.engine)

            launch_kwargs: dict[str, Any] = {
                "headless": self.settings.headless,
                "slow_mo": self.settings.slow_mo_ms or 0,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            if self.settings.executable_path:
                launch_kwargs["executable_path"] = self.settings.executable_path

            context_kwargs: dict[str, Any] = {
                "locale": self.settings.locale,
                "timezone_id": self.settings.timezone_id,
                "viewport": {
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                "ignore_https_errors": False,
            }
            if self.settings.user_agent:
                context_kwargs["user_agent"] = self.settings.user_agent

            try:
                if self.settings.user_data_dir:
                    # Persistent context reuses the operator's logged-in session.
                    self._context = await launcher.launch_persistent_context(
                        self.settings.user_data_dir, **launch_kwargs, **context_kwargs
                    )
                    self._browser = None
                else:
                    self._browser = await launcher.launch(**launch_kwargs)
                    self._context = await self._browser.new_context(**context_kwargs)
            except Exception as exc:
                self.available = False
                await self.stop()
                raise BrowserUnavailableError(f"could not launch {self.settings.engine}: {exc}") from exc

            self._context.set_default_navigation_timeout(self.settings.nav_timeout_ms)
            if self.settings.stealth:
                await self._context.add_init_script(_STEALTH_JS)
            log.info(
                "browser.started",
                engine=self.settings.engine,
                headless=self.settings.headless,
                persistent=bool(self.settings.user_data_dir),
            )

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._context is not None:
                await self._context.close()
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._playwright is not None:
                await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None

    async def __aenter__(self) -> WebUseDriver:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -- core visit --------------------------------------------------------- #

    async def visit(
        self,
        url: str,
        *,
        surface: str = "browser",
        capture_patterns: Sequence[str] = (),
        wait_selector: str | None = None,
        wait_ms: int = 2500,
        scrolls: int = 0,
        scroll_pause_ms: int = 900,
        extract_text: bool = True,
        extract_html: bool = False,
        requests_per_minute: float = 30.0,
        click_selector: str | None = None,
    ) -> PageResult:
        """Load a page, capture matching JSON responses, and return what we saw.

        `capture_patterns` are regexes matched against response URLs. Because the
        listener is attached before navigation, the app's own bootstrap fetches
        are captured, which is where the data actually lives.
        """
        await self.start()
        pacer = Pacer.get(surface, requests_per_minute, self.settings.max_pages)
        pacer.breaker.check()
        await pacer.bucket.acquire()

        result = PageResult(url=url)
        async with self._page_semaphore:
            page = await self._context.new_page()
            captured: list[CapturedResponse] = []
            console: list[str] = []

            on_response = _response_listener(url, capture_patterns, captured)
            page.on("response", lambda r: asyncio.create_task(on_response(r)))
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:500]))

            if self.settings.block_resources:
                await page.route("**/*", _route_blocker(set(self.settings.block_resources)))

            try:
                await self._drive_page(
                    page,
                    result,
                    wait_selector=wait_selector,
                    click_selector=click_selector,
                    wait_ms=wait_ms,
                    scrolls=scrolls,
                    scroll_pause_ms=scroll_pause_ms,
                    extract_text=extract_text,
                    extract_html=extract_html,
                )
                pacer.breaker.record_success()
            except Exception as exc:
                result.error = str(exc)
                pacer.breaker.record_failure()
                log.warning("browser.visit_failed", url=url, error=str(exc))
            finally:
                # Give in-flight response handlers a moment to finish.
                await asyncio.sleep(0.2)
                result.captured = list(captured)
                result.console = console
                with contextlib.suppress(Exception):
                    await page.close()

        return result

    async def _drive_page(
        self,
        page: Any,
        result: PageResult,
        *,
        wait_selector: str | None,
        click_selector: str | None,
        wait_ms: int,
        scrolls: int,
        scroll_pause_ms: int,
        extract_text: bool,
        extract_html: bool,
    ) -> None:
        """Navigate and work the page. Navigation failure raises; every optional
        step after it is suppressed, because a missing selector or a body that
        will not serialise is not a reason to lose the responses already
        captured."""
        response = await page.goto(result.url, wait_until="domcontentloaded")
        result.status = response.status if response else 0
        result.final_url = page.url

        if wait_selector:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(wait_selector, timeout=self.settings.nav_timeout_ms)
        if click_selector:
            with contextlib.suppress(Exception):
                await page.click(click_selector, timeout=5000)
        if wait_ms:
            await page.wait_for_timeout(wait_ms)

        for _ in range(scrolls):
            with contextlib.suppress(Exception):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(scroll_pause_ms)

        if extract_text:
            with contextlib.suppress(Exception):
                result.text = await page.inner_text("body")
        if extract_html:
            with contextlib.suppress(Exception):
                result.html = await page.content()

    async def capture_json(
        self,
        url: str,
        pattern: str,
        *,
        surface: str = "browser",
        wait_ms: int = 3000,
        scrolls: int = 0,
        **kwargs: Any,
    ) -> list[Any]:
        """Convenience: visit `url` and return every JSON body whose request URL
        matched `pattern`. The workhorse for launchpad and analytics collectors."""
        result = await self.visit(
            url,
            surface=surface,
            capture_patterns=[pattern],
            wait_ms=wait_ms,
            scrolls=scrolls,
            extract_text=False,
            **kwargs,
        )
        return result.json_matching(pattern)

    async def evaluate(self, url: str, script: str, *, surface: str = "browser", wait_ms: int = 2000) -> Any:
        """Load a page and run JS in it. Used for reading state the DOM exposes
        but the network does not, e.g. a hydrated store on `window`."""
        await self.start()
        pacer = Pacer.get(surface, 30.0, self.settings.max_pages)
        await pacer.bucket.acquire()
        async with self._page_semaphore:
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                if wait_ms:
                    await page.wait_for_timeout(wait_ms)
                return await page.evaluate(script)
            except Exception as exc:
                log.warning("browser.evaluate_failed", url=url, error=str(exc))
                return None
            finally:
                with contextlib.suppress(Exception):
                    await page.close()


_DRIVER: WebUseDriver | None = None


def get_driver(settings: BrowserSettings | None = None) -> WebUseDriver:
    """Process-wide shared driver. Collectors should use this rather than
    launching their own browser, so one Chromium serves the whole swarm."""
    global _DRIVER
    if _DRIVER is None:
        _DRIVER = WebUseDriver(settings)
    return _DRIVER


async def shutdown_driver() -> None:
    global _DRIVER
    if _DRIVER is not None:
        await _DRIVER.stop()
        _DRIVER = None


__all__ = [
    "BrowserUnavailableError",
    "CapturedResponse",
    "PageResult",
    "WebUseDriver",
    "get_driver",
    "shutdown_driver",
]
