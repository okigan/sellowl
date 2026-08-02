"""One-time manual eBay sign-in for the scraping profile.

Run from backend/:  uv run python scripts/browser_login.py

eBay puts sold/completed listings behind a login, and sold comps are the
load-bearing half of this app's pricing -- without a session the scraper can
read a seller's own listings but not the completed sales to price them
against.

This opens the persistent browser profile and waits. **You** sign in, in the
window, by hand. Nothing here reads, types, stores, or transmits your
credentials; the only thing that persists is the browser's own cookie jar in
`scrape_profile_dir`, exactly as if you had opened Chrome yourself.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from playwright.async_api import async_playwright

from sellowl.config import Settings

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def main() -> None:
    settings = Settings()
    profile = settings.scrape_profile_dir
    if not profile:
        raise SystemExit("SCRAPE_PROFILE_DIR is empty; set it so the session can persist.")

    print(f"Opening a browser using profile: {profile}")
    print("Sign in to eBay in the window that opens, then come back here and press Enter.")
    print("(Nothing in this script touches your credentials.)\n")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            profile,
            headless=False,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
            user_agent=UA,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.ebay.com/signin/", wait_until="domcontentloaded")
        await asyncio.get_event_loop().run_in_executor(
            None, input, "Press Enter when signed in... "
        )

        # Prove it worked against the thing we actually need, rather than
        # trusting that a login page went away.
        await page.goto(
            "https://www.ebay.com/sch/i.html?_nkw=usb+flash+drive&LH_Sold=1&LH_Complete=1&_ipg=60",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(3000)
        title = await page.title()
        cards = await page.locator("li.s-item, li.s-card").count()
        await ctx.close()

    if "sign in" in title.lower() or cards == 0:
        print(f"\nStill blocked (title={title!r}, cards={cards}). Sold comps will be unavailable.")
    else:
        print(f"\nSold listings reachable: {cards} cards. The session is saved in {profile}.")


asyncio.run(main())
