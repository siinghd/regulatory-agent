"""Shared Chromium for scraping providers.

One browser process per worker; every scraping session gets a fresh, isolated context (own
cookies, own FileMaker session) and is closed afterwards. The semaphore here caps concurrent
sessions *per process* (per worker) so we stay polite to government portals; it knows nothing of
other workers. The cross-process cap on what matters most, the portal's download step, is the
Redis download lock that agent.worker hands to the UARB provider (agent.limits.Limits.lock).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

log = structlog.get_logger()


class BrowserPool:
    def __init__(self, *, proxy: str | None, max_sessions: int, nav_timeout_ms: int):
        self._proxy = proxy
        self._sem = asyncio.Semaphore(max_sessions)  # per process: see the module docstring
        self._nav_timeout_ms = nav_timeout_ms
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    @property
    def nav_timeout_ms(self) -> int:
        """Default timeout of every page action and navigation in a session."""
        return self._nav_timeout_ms

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    proxy={"server": self._proxy} if self._proxy else None,
                    args=["--disable-dev-shm-usage", "--no-first-run"],
                )
                log.info("browser.launched", proxy=bool(self._proxy))
            return self._browser

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Page]:
        async with self._sem:
            browser = await self._ensure_browser()
            ctx: BrowserContext = await browser.new_context(
                viewport={"width": 1400, "height": 1000},
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36 regulatory-agent (+https://uarb.hsingh.app/bot)"
                ),
            )
            # Everything after the context exists is inside the try, so a failure here (route
            # setup, new_page on a dying browser) can't leak the context.
            try:
                ctx.set_default_timeout(self._nav_timeout_ms)
                ctx.set_default_navigation_timeout(self._nav_timeout_ms)
                # Images, fonts and media are irrelevant to the data and slow over the proxy.
                await ctx.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "font", "media"}
                    else route.continue_(),
                )
                yield await ctx.new_page()
            finally:
                await ctx.close()

    async def close(self) -> None:
        """Idempotent: safe to call twice, or on a pool that never launched a browser."""
        async with self._lock:
            browser, self._browser = self._browser, None
            pw, self._pw = self._pw, None
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if pw is not None:
                    await pw.stop()
