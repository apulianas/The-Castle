"""Host-side X collector. Only public posts, never cookies, cross into Docker."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
FEED_URL = "https://x.com/Ravens"
SNAPSHOT_NAME = "ravens.json"
DEFAULT_OUTPUT = Path(".x-feed")
DEFAULT_PROFILE = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "The-Castle" / "x-browser"
BROWSER_CHANNELS = {"chromium": "chromium", "edge": "msedge"}


def default_profile(browser: str) -> Path:
    return DEFAULT_PROFILE if browser == "chromium" else DEFAULT_PROFILE.with_name("x-edge-browser")

# Read rendered posts only. Quote cards are not announcements by the Ravens.
EXTRACT_POSTS = r"""() => Array.from(document.querySelectorAll('article[data-testid="tweet"]'))
    .map(article => {
        const time = article.querySelector('time');
        const link = time?.closest('a');
        const url = link?.href || '';
        if (!/^https:\/\/x\.com\/ravens\/status\/\d+$/i.test(url)) return null;
        if (article.querySelector('[data-testid="socialContext"]')) return null;
        const quoted = node => {
            for (let p = node.parentElement; p && p !== article; p = p.parentElement) {
                if (p.getAttribute('data-testid') === 'quoteTweet' ||
                    (p.getAttribute('role') === 'link' && p.querySelector('time'))) return true;
            }
            return false;
        };
        const text = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
            .filter(node => !quoted(node)).map(node => node.innerText).join('\n');
        const images = Array.from(article.querySelectorAll('[data-testid="tweetPhoto"] img'))
            .filter(node => !quoted(node)).map(node => node.src)
            .filter(url => url.startsWith('https://pbs.twimg.com/media/'));
        return {url, text, created_at: time.dateTime, image_urls: images};
    }).filter(Boolean)"""


def write_snapshot(directory: Path, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / SNAPSHOT_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def collect(
    profile: Path, directory: Path, login: bool, once: bool, browser: str = "chromium"
) -> None:
    from playwright.async_api import Error as BrowserError, async_playwright

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile.resolve()), channel=BROWSER_CHANNELS[browser], headless=not login,
            viewport={"width": 1280, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(15000)
            if login:
                await page.goto(FEED_URL, wait_until="domcontentloaded")
                LOGGER.info("Sign in to X in the dedicated browser window. Waiting up to 10 minutes.")
                await page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').wait_for(
                    state="visible", timeout=600000
                )
            posts: dict[str, dict[str, Any]] = {}
            while True:
                try:
                    await page.goto(FEED_URL, wait_until="domcontentloaded")
                    await page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').wait_for(
                        state="visible"
                    )
                    await page.locator('article[data-testid="tweet"]').first.wait_for()
                    found: dict[str, dict[str, Any]] = {}
                    for _ in range(8):
                        batch = await page.evaluate(EXTRACT_POSTS)
                        for post in batch:
                            found[post["url"]] = post
                        await page.mouse.wheel(0, 1400)
                        await page.wait_for_timeout(1000)
                    if not found:
                        raise ValueError("No original Ravens posts were visible.")
                    posts.update(found)
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(days=7)
                    recent = sorted(
                        (
                            post for post in posts.values()
                            if datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")) > cutoff
                        ),
                        key=lambda post: post["created_at"], reverse=True,
                    )[:300]
                    posts = {post["url"]: post for post in recent}
                    if not recent:
                        raise ValueError("No recent Ravens posts were visible.")
                    write_snapshot(directory, {
                        "version": 1, "fetched_at": now.isoformat(), "posts": recent,
                    })
                    LOGGER.info("Saved %d public Ravens posts.", len(recent))
                except (BrowserError, ValueError) as exc:
                    # Browser exceptions can contain page data; do not dump them.
                    LOGGER.warning(
                        "X could not be read (%s). Check the dedicated browser; "
                        "sign in again with --login if needed. Bluesky will be used.",
                        type(exc).__name__,
                    )
                    write_snapshot(directory, {"version": 1, "error": "X browser needs attention"})
                    if once:
                        raise RuntimeError("X collection failed; run again with --login.") from None
                    if not context.pages:
                        raise RuntimeError("The dedicated browser was closed.") from None
                    await asyncio.sleep(300)
                    continue
                if once:
                    return
                await asyncio.sleep(30)
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser", choices=BROWSER_CHANNELS, default="chromium",
        help="Use Playwright Chromium (default) or the installed Microsoft Edge.",
    )
    parser.add_argument("--profile", type=Path, help="Override the browser's dedicated profile directory.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--login", action="store_true", help="Open a visible browser for manual sign-in.")
    parser.add_argument("--once", action="store_true", help="Write one snapshot and exit.")
    args = parser.parse_args()
    profile = (args.profile or default_profile(args.browser)).resolve()
    directory = args.output.resolve()
    if profile.is_relative_to(directory) or directory.is_relative_to(profile):
        parser.error("The credential profile and public output directories must not overlap.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from playwright.async_api import Error as BrowserError
    except ImportError:
        parser.exit(1, "Install requirements-x-browser.txt in the host Python environment first.\n")
    try:
        asyncio.run(collect(profile, directory, args.login, args.once, args.browser))
    except BrowserError:
        write_snapshot(directory, {"version": 1, "error": "X browser needs attention"})
        parser.exit(
            1, f"X {args.browser} browser could not start or sign-in timed out. "
            "Close any other collector, check that the selected browser is installed, "
            "and retry --login later.\n",
        )
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    except KeyboardInterrupt:
        LOGGER.info("X collector stopped. The bot will fall back when the snapshot expires.")


if __name__ == "__main__":
    main()
