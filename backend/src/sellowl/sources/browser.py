"""Self-hosted eBay scraping via a real browser. No Apify.

Why a browser and not httpx: eBay serves a soft "Error Page | eBay" to plain
HTTP clients on the first request, before any rate limit could apply -- it is
fingerprinting, not throttling. A headless-shell browser is blocked the same
way. What works is a real browser that behaves like one: land on the homepage,
let cookies settle, and only then navigate.

Volume policy is deliberately low and slow (`scrape_min_interval_s`, one
request at a time). This is someone else's infrastructure; the app's whole
job can be done with a few dozen page loads per store, and there is no
version of "faster" here that is worth getting the IP blocked for.

Known limits, both structural rather than bugs to fix later:

- **Sold listings require a signed-in eBay account.** `/sch/...&LH_Sold=1`
  redirects to "Sign in or Register". Worth being precise about, because it
  looks like a bot block and isn't: *without* stealth eBay answers with
  "Security Measure | eBay", *with* stealth the same request reaches the
  genuine sign-in page. Anti-detection gets you to the wall, not through it.
  Sold comps are the load-bearing half of this app's pricing, so this
  matters: without a session, this source can supply a seller's own listings
  but not the completed sales to price them against. The context is
  persistent (`scrape_profile_dir`) precisely so a human can log in once, by
  hand, and have it stick -- this code never handles credentials.
- **Facebook Marketplace is not implemented here.** It is login-walled and
  heavily JS-driven; pretending otherwise would mean silently returning no
  local comps. It returns empty and says so, which the pipeline already
  tolerates (the venue recommendation degrades, the job does not fail).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..config import Settings
from ..logging import get_logger
from .sold import sold_search_url
from .store import seller_from_url, seller_search_url

log = get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Placeholder promo cards eBay injects into every result page. They carry a
# fake /itm/123456 link and a "Shop on eBay" title.
_PLACEHOLDER_TITLES = {"shop on ebay", "new listing"}

# Pulled out of each result card in page context. Kept as one expression so a
# result page costs a single round trip rather than one per field.
_EXTRACT = """() => {
  const clean = t => (t || '').replace(/Opens in a new window or tab/gi, '').trim();
  const out = [];
  for (const li of document.querySelectorAll('li.s-item, li.s-card')) {
    const q = s => li.querySelector(s);
    const txt = s => { const e = q(s); return e ? clean(e.textContent) : ''; };
    const a = q('a.s-item__link, a.su-link, a[href*="/itm/"]');
    const img = q('img');
    const href = a ? a.href : '';
    const m = href.match(/\\/itm\\/(\\d{9,15})/);
    out.push({
      itemId: m ? m[1] : '',
      title: txt('.s-item__title, .su-styled-text.primary, [role="heading"]'),
      price: txt('.s-item__price, .s-card__price, .su-styled-text.positive'),
      condition: txt('.SECONDARY_INFO, .s-item__subtitle'),
      soldDate: txt('.s-item__caption--signal, .s-item__caption, .su-styled-text.secondary'),
      url: href.split('?')[0],
      image: img ? (img.src || img.getAttribute('data-src') || '') : '',
    });
  }
  return out;
}"""


def _usable(row: dict[str, Any]) -> bool:
    title = (row.get("title") or "").strip().lower()
    return bool(row.get("itemId")) and bool(title) and title not in _PLACEHOLDER_TITLES


class BrowserScraper:
    """One browser, reused, with a floor on how often it may fetch a page."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._pw: Any = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._last_fetch = 0.0
        self._warm = False

    async def _context(self) -> BrowserContext:
        if self._ctx is not None:
            return self._ctx
        self._pw = await async_playwright().start()
        profile = self._s.scrape_profile_dir
        if profile:
            # Persistent so a human-performed eBay login survives restarts;
            # see the module docstring on sold listings.
            self._ctx = await self._pw.chromium.launch_persistent_context(
                profile,
                headless=self._s.scrape_headless,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
                user_agent=_UA,
            )
        else:
            self._browser = await self._pw.chromium.launch(headless=self._s.scrape_headless)
            self._ctx = await self._browser.new_context(
                locale="en-US", viewport={"width": 1440, "height": 900}, user_agent=_UA
            )
        # playwright-stealth patches the handful of properties that give a
        # driven browser away (navigator.webdriver, chrome runtime, plugin and
        # codec lists, ...). Measured, not cargo-culted: without it eBay
        # answers a sold-listings request with "Security Measure | eBay"; with
        # it the same request reaches the real "Sign in or Register" page. It
        # defeats the bot check -- it cannot defeat an auth requirement.
        if self._s.scrape_stealth:
            try:
                from playwright_stealth import Stealth

                await Stealth().apply_stealth_async(self._ctx)
            except Exception as exc:  # noqa: BLE001 - stealth is an optimisation
                log.warning("stealth_unavailable", error=str(exc))
        await self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        return self._ctx

    async def _pace(self) -> None:
        wait = self._s.scrape_min_interval_s - (time.monotonic() - self._last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_fetch = time.monotonic()

    async def _warm_up(self, page: Page) -> None:
        """Land on the homepage first. Deep-linking straight into /sch/ from a
        cold context is what gets served the error page."""
        if self._warm:
            return
        await page.goto("https://www.ebay.com/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)
        self._warm = True

    async def fetch_cards(self, url: str) -> list[dict[str, Any]]:
        """One result page -> raw rows, in the shape the parsers already expect."""
        async with self._lock:  # one page at a time, on purpose
            ctx = await self._context()
            page = await ctx.new_page()
            try:
                await self._warm_up(page)
                await self._pace()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(self._s.scrape_settle_ms)
                title = await page.title()
                if "sign in" in title.lower():
                    log.warning("scrape_requires_login", url=url[:90], page_title=title)
                    return []
                if response is not None and response.status >= 400:
                    log.warning("scrape_blocked", url=url[:90], status=response.status)
                    return []
                rows = [r for r in await page.evaluate(_EXTRACT) if _usable(r)]
                log.info("scraped", url=url[:90], rows=len(rows))
                return rows
            finally:
                await page.close()

    async def close(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
            self._ctx = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


class EbayBrowserSource:
    """`CompSource` backed by the browser scraper. See sources/protocol.py."""

    def __init__(self, settings: Settings, scraper: BrowserScraper | None = None) -> None:
        self._s = settings
        self._scraper = scraper or BrowserScraper(settings)

    async def store_listings(self, store_url: str, limit: int) -> list[dict[str, Any]]:
        seller = seller_from_url(store_url)
        url = seller_search_url(seller) if seller else store_url
        return (await self._scraper.fetch_cards(url))[:limit]

    async def sold_comps(self, query: str, limit: int, days_back: int) -> list[dict[str, Any]]:
        rows = await self._scraper.fetch_cards(sold_search_url(query))
        if not rows:
            # Almost always the sign-in wall rather than a genuinely empty
            # result. Saying so beats a silent zero, because the pricing stage
            # cannot tell "no comps exist" from "we were not allowed to look".
            log.warning("sold_comps_unavailable", query=query[:60])
        return rows[:limit]

    async def local_comps(self, metro: str, query: str, limit: int) -> list[dict[str, Any]]:
        log.info("local_comps_unsupported_by_browser_source", metro=metro)
        return []

    async def close(self) -> None:
        await self._scraper.close()
