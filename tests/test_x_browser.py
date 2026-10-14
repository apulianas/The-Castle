"""Run with the optional host Python: python -m unittest discover -s tests -p test_x_browser.py."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ravens_bot.x_browser import EXTRACT_POSTS, collect, default_profile, main


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Optional host browser dependencies")
class BrowserSelectionTests(unittest.TestCase):
    def test_default_profiles_are_separate(self):
        self.assertEqual(default_profile("chromium").name, "x-browser")
        self.assertEqual(default_profile("edge").name, "x-edge-browser")
        self.assertEqual(default_profile("chromium").parent, default_profile("edge").parent)

    def test_cli_uses_matching_profile_and_browser_for_login_and_collection(self):
        for browser in ("chromium", "edge"):
            for login in (False, True):
                with self.subTest(browser=browser, login=login):
                    args = ["x_browser", "--browser", browser, "--once"]
                    if login:
                        args.append("--login")
                    with patch("sys.argv", args), patch(
                        "ravens_bot.x_browser.collect", new_callable=AsyncMock
                    ) as collector:
                        main()
                    collector.assert_awaited_once_with(
                        default_profile(browser).resolve(), Path(".x-feed").resolve(),
                        login, True, browser,
                    )


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Optional host browser dependencies")
class BrowserExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_publishes_snapshot_and_closes_browser(self):
        post = {
            "url": "https://x.com/Ravens/status/123", "text": "Injury Report",
            "created_at": datetime.now(timezone.utc).isoformat(), "image_urls": [],
        }
        page = MagicMock()
        page.goto = AsyncMock()
        page.evaluate = AsyncMock(return_value=[post])
        page.wait_for_timeout = AsyncMock()
        page.mouse.wheel = AsyncMock()
        page.locator.return_value.wait_for = AsyncMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        context = MagicMock(pages=[page])
        context.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("playwright.async_api.async_playwright", return_value=manager):
                await collect(root / "profile", root / "feed", login=False, once=True)
            payload = json.loads((root / "feed" / "ravens.json").read_text())
            self.assertEqual(payload["posts"], [post])
            self.assertEqual(payload["version"], 1)
            context.close.assert_awaited_once()
            playwright.chromium.launch_persistent_context.assert_awaited_once_with(
                str((root / "profile").resolve()), channel="chromium", headless=True,
                viewport={"width": 1280, "height": 1000},
            )
            page.mouse.wheel.assert_awaited()
            with patch("playwright.async_api.async_playwright", return_value=manager):
                await collect(root / "edge-profile", root / "feed", login=True, once=True, browser="edge")
            playwright.chromium.launch_persistent_context.assert_awaited_with(
                str((root / "edge-profile").resolve()), channel="msedge", headless=False,
                viewport={"width": 1280, "height": 1000},
            )
            page.evaluate.return_value = []
            with patch("playwright.async_api.async_playwright", return_value=manager):
                with self.assertRaisesRegex(RuntimeError, "collection failed"):
                    await collect(root / "profile", root / "feed", login=False, once=True)
            payload = json.loads((root / "feed" / "ravens.json").read_text())
            self.assertIn("error", payload)
            self.assertNotIn("posts", payload)

    async def test_only_own_posts_and_no_quoted_text_or_photos(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content("""
                    <article data-testid="tweet">
                      <a href="https://x.com/Ravens/status/123"><time datetime="2026-10-13T17:00:00Z"></time></a>
                      <div data-testid="tweetText">We have signed WR Chris Moore.</div>
                      <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/own"></div>
                      <div role="link" tabindex="0">
                        <a href="https://x.com/Someone/status/456"><time datetime="2026-10-12T17:00:00Z"></time></a>
                        <div data-testid="tweetText">We have released WR Chris Moore.</div>
                        <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/quote"></div>
                      </div>
                    </article>
                    <article data-testid="tweet">
                      <a href="https://x.com/Someone/status/789"><time datetime="2026-10-13T17:00:00Z"></time></a>
                      <div data-testid="tweetText">We have signed a player.</div>
                    </article>
                    <article data-testid="tweet">
                      <div data-testid="socialContext">Ravens reposted</div>
                      <a href="https://x.com/Ravens/status/101"><time datetime="2026-10-13T17:00:00Z"></time></a>
                      <div data-testid="tweetText">Old announcement</div>
                    </article>
                """)
                self.assertEqual(await page.evaluate(EXTRACT_POSTS), [{
                    "url": "https://x.com/Ravens/status/123",
                    "text": "We have signed WR Chris Moore.",
                    "created_at": "2026-10-13T17:00:00Z",
                    "image_urls": ["https://pbs.twimg.com/media/own"],
                }])
            finally:
                await browser.close()
