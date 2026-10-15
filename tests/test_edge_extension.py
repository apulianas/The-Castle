"""Offline Chromium DOM/worker tests; no live X, native host, or Node required."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "edge_extension"
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

HARNESS = r"""
() => {
    const event = () => {
        const handlers = [];
        return {addListener: fn => handlers.push(fn),
            fire: (...args) => handlers.forEach(fn => fn(...args)), handlers};
    };
    window.mock = {
        session: {}, local: {}, calls: [], ports: [], reloads: [], injections: [], alarms: {}, scheduledTimes: {},
        tabs: {1: {id: 1, url: "https://x.com/Ravens", status: "complete"}},
        activeId: 1, nativeError: false, nativeReject: false,
        scriptError: null, scriptGate: null, nativeGate: null,
        batch: {posts: [{
            url: "https://x.com/Ravens/status/123",
            text: "Ravens roster news",
            created_at: new Date().toISOString(),
            image_urls: ["https://pbs.twimg.com/media/abc.jpg"]
        }], truncated: 0}
    };
    window.chrome = {
        runtime: {
            id: "test-extension",
            getManifest: () => ({version: "1.1.0"}),
            getURL: path => "chrome-extension://test-extension/" + path,
            onMessage: event(), onStartup: event(), onInstalled: event(),
            connectNative: host => {
                const port = {
                    onMessage: event(), onDisconnect: event(), closed: false,
                    disconnect: () => { port.closed = true; },
                    postMessage: payload => {
                        if (port.closed) throw new Error("port disconnected");
                        mock.calls.push({host, payload: structuredClone(payload)});
                        void (async () => {
                            if (mock.nativeGate) await mock.nativeGate;
                            if (port.closed) return;
                            if (mock.nativeError) {
                                port.closed = true;
                                port.onDisconnect.fire();
                                return;
                            }
                            port.onMessage.fire(mock.nativeReject
                                ? {ok: false, error: "<secret host content>"}
                                : payload.error ? {ok: true} : {
                                    ok: true, count: payload.posts.length,
                                    fetched_at: new Date().toISOString()
                                });
                        })();
                    }
                };
                mock.ports.push(port);
                return port;
            }
        },
        storage: {local: {
            get: async key => ({[key]: structuredClone(mock.local[key])}),
            set: async data => Object.assign(mock.local, structuredClone(data))
        }, session: {
            get: async key => ({[key]: structuredClone(mock.session[key])}),
            set: async data => Object.assign(mock.session, structuredClone(data))
        }},
        tabs: {
            query: async options => structuredClone(options.active
                ? [mock.tabs[mock.activeId]] : Object.values(mock.tabs)),
            create: async options => {
                const id = Math.max(0, ...Object.keys(mock.tabs).map(Number)) + 1;
                const tab = {id, url: options.url, status: "complete", active: options.active};
                mock.tabs[id] = tab;
                return structuredClone(tab);
            },
            get: async id => {
                if (!mock.tabs[id]) throw new Error("missing");
                return structuredClone(mock.tabs[id]);
            },
            reload: async id => { mock.reloads.push(id); },
            onRemoved: event(), onUpdated: event()
        },
        alarms: {
            create: async (name, options) => {
                mock.alarms[name] = options;
                mock.scheduledTimes[name] = Date.now() + (options.delayInMinutes || options.periodInMinutes) * 60000;
            },
            get: async name => mock.alarms[name] ? {
                name, scheduledTime: mock.scheduledTimes[name], ...mock.alarms[name]
            } : undefined,
            clear: async name => { delete mock.alarms[name]; },
            onAlarm: event()
        },
        scripting: {
            executeScript: async options => {
                mock.injections.push({tabId: options.target.tabId, args: options.args});
                if (mock.scriptGate) await mock.scriptGate;
                if (mock.scriptError) throw new Error("sensitive script content");
                return [{result: structuredClone(mock.batch)}];
            }
        }
    };
    window.importScripts = () => {};
    window.request = (action, sender = {
        id: chrome.runtime.id, url: chrome.runtime.getURL("popup.html")
    }) => new Promise(resolve => {
        const accepted = chrome.runtime.onMessage.handlers[0]({action}, sender, resolve);
        if (!accepted) resolve("rejected");
    });
    window.state = () => mock.session.ravensCollector;
    window.alarm = () => chrome.alarms.onAlarm.fire({name: "ravens-refresh"});
    window.retryNow = () => { mock.session.ravensCollector.retryAt = new Date(0).toISOString(); alarm(); };
}
"""


class ManifestTests(unittest.TestCase):
    def test_minimal_permissions_and_no_page_entrypoint(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(set(manifest["permissions"]),
                         {"activeTab", "scripting", "alarms", "storage", "nativeMessaging"})
        self.assertEqual(manifest["host_permissions"], ["https://x.com/*"])
        for key in ("optional_host_permissions", "content_scripts",
                    "externally_connectable", "web_accessible_resources"):
            self.assertNotIn(key, manifest)
        self.assertEqual(manifest["incognito"], "not_allowed")
        code = "\n".join(path.read_text(encoding="utf-8") for path in EXTENSION.glob("*.js"))
        for forbidden in ("fetch(", "XMLHttpRequest", "window.postMessage(", "document.cookie",
                          "chrome.debugger", "chrome.cookies"):
            self.assertNotIn(forbidden, code)
        self.assertEqual(code.count(".postMessage("), code.count("nativePort.postMessage("))


@unittest.skipUnless(HAS_PLAYWRIGHT, "optional Playwright is not installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as error:
            cls.playwright.stop()
            raise unittest.SkipTest(f"optional Chromium unavailable: {type(error).__name__}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body></body></html>"))
        self.page = self.context.new_page()
        self.page.goto("https://x.com/Ravens")

    def tearDown(self):
        self.context.close()

    def extractor(self, body, url=None):
        if url:
            self.page.goto(url)
        self.page.set_content(body)
        self.page.add_script_tag(content=(EXTENSION / "collector.js").read_text(encoding="utf-8"))
        self.page.evaluate("""() => {
            const timer = window.setTimeout.bind(window);
            window.setTimeout = (fn, ms) => timer(fn, Math.min(ms, 1));
        }""")
        return self.page.evaluate("extractRavensPage(false)")

    def worker(self):
        self.page.evaluate(HARNESS)
        self.load_worker()

    def load_worker(self):
        self.page.evaluate("(code) => { (new Function(code))(); }",
                           (EXTENSION / "collector.js").read_text(encoding="utf-8") + "\n" +
                           (EXTENSION / "worker.js").read_text(encoding="utf-8"))

    def recreate_worker(self):
        self.page.evaluate("""() => {
            for (const group of [chrome.runtime, chrome.tabs, chrome.alarms]) {
                for (const value of Object.values(group)) {
                    if (value.handlers) value.handlers.length = 0;
                }
            }
        }""")
        self.load_worker()

    def wait_collected(self):
        self.page.wait_for_function("state()?.enabled && !state().busy && state().lastUpdated")

    def wait_retry(self):
        self.page.wait_for_function("state()?.enabled && state().failures > 0 && !state().busy")
        self.page.wait_for_function("mock.ports.every(port => port.closed)")

    @staticmethod
    def article(status="123", text="Ravens signed a player.", extra="", timestamp=None):
        timestamp = timestamp or "2026-10-13T12:00:00Z"
        return (f'<article data-testid="tweet"><a href="https://x.com/Ravens/status/{status}">'
                f'<time datetime="{timestamp}">Now</time></a>'
                f'<div data-testid="tweetText">{text}</div>{extra}</article>')

    def test_extract_original_pinned_and_exclude_repost_quote_and_foreign_images(self):
        quote = ('<div data-testid="quoteTweet"><a href="https://x.com/Other/status/9">'
                 '<time datetime="2026-10-13T12:00:00Z"></time></a>'
                 '<div data-testid="tweetText">QUOTED FALSE NEWS</div>'
                 '<div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/quote.jpg">'
                 '</div></div>')
        photos = ('<div data-testid="tweetPhoto">'
                  '<img src="https://pbs.twimg.com/media/own.jpg">'
                  '<img src="https://pbs.twimg.com.evil.test/media/evil.jpg">'
                  '<img src="https://pbs.twimg.com/profile_images/avatar.jpg">'
                  '<img src="http://pbs.twimg.com/media/insecure.jpg"></div>')
        body = '<button data-testid="SideNav_AccountSwitcher_Button">Account</button>'
        body += self.article(extra=quote + photos)
        body += self.article("124", "Pinned own news", '<span data-testid="socialContext">Pinned</span>')
        body += self.article("125", "Reposted", '<span data-testid="socialContext">Someone reposted</span>')
        body += self.article("126").replace("x.com/Ravens/status/126", "x.com/Other/status/126")
        result = self.extractor(body)
        self.assertEqual([p["url"].rsplit("/", 1)[1] for p in result["posts"]], ["123", "124"])
        self.assertEqual(result["posts"][0]["text"], "Ravens signed a player.")
        self.assertEqual(result["posts"][0]["image_urls"], ["https://pbs.twimg.com/media/own.jpg"])

    def test_nested_quoted_articles_are_not_independent_posts(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += self.article(extra=self.article("999", "Quoted own post"))
        result = self.extractor(body)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["text"], "Ravens signed a player.")

    def test_timestamp_anchor_role_link_is_not_a_quote_card(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        quote = ('<div role="link"><a role="link" href="https://x.com/Ravens/status/999">'
                 '<time datetime="2026-10-13T12:00:00Z">Now</time></a>'
                 '<div data-testid="tweetText">Quoted own news is not the announcement</div>'
                 '<div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/quote.jpg">'
                 '</div></div>')
        body += self.article(extra=quote).replace(
            '<a href="https://x.com/Ravens/status/123">',
            '<a role="link" href="https://x.com/Ravens/status/123">')
        result = self.extractor(body)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["text"], "Ravens signed a player.")
        self.assertEqual(result["posts"][0]["image_urls"], [])
        self.assertEqual(result["diagnostics"]["own_timestamp_links"], 2)
        self.assertEqual(result["diagnostics"]["quote_time_rejections"], 1)
        self.assertEqual(result["diagnostics"]["quote_text_skipped"], 1)
        self.assertEqual(result["diagnostics"]["quote_images_skipped"], 1)

    def test_role_link_fix_does_not_promote_foreign_author_with_own_quote(self):
        quote = ('<div role="link"><a role="link" href="https://x.com/Ravens/status/999">'
                 '<time datetime="2026-10-13T12:00:00Z">Now</time></a>'
                 '<div data-testid="tweetText">Quoted own news</div></div>')
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += self.article(extra=quote).replace(
            '<a href="https://x.com/Ravens/status/123">',
            '<a role="link" href="https://x.com/Other/status/123">')
        result = self.extractor(body)
        self.assertEqual(result["posts"], [])
        self.assertEqual(result["diagnostics"]["other_author_or_link"], 1)
        self.assertEqual(result["diagnostics"]["own_timestamp_links"], 1)

    def test_quote_card_anchor_is_not_exempted_as_timestamp_anchor(self):
        body = ('<button data-testid="SideNav_AccountSwitcher_Button"></button>'
                '<article data-testid="tweet"><a role="link" href="https://x.com/Ravens/status/999">'
                '<time datetime="2026-10-13T12:00:00Z">Now</time>'
                '<div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/quote.jpg">'
                '</div></a></article>')
        result = self.extractor(body)
        self.assertEqual(result["posts"], [])
        self.assertEqual(result["diagnostics"]["quote_time_rejections"], 1)
        self.assertEqual(result["diagnostics"]["missing_outer_time"], 1)

    def test_diagnostics_distinguish_selector_rejections_without_page_content(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button">PRIVATE ACCOUNT</button>'
        body += self.article("1")
        body += self.article("2", extra='<div data-testid="socialContext">Private actor reposted</div>')
        body += self.article("3", extra='<button data-testid="tweet-text-show-more-link">Show more</button>')
        body += self.article("4", timestamp="2026-10-13T12:00:00")
        body += self.article("5", text="")
        body += self.article("6").replace("x.com/Ravens/status/6", "x.com/Other/status/6")
        body += '<article data-testid="tweet">No time metadata</article>'
        body += self.article("7", text="x" * 25001)
        result = self.extractor(body)
        counts = result["diagnostics"]
        self.assertEqual(counts["articles_seen"], 8)
        self.assertEqual(counts["own_timestamp_links"], 6)
        for key in ("accepted", "social_rejections", "truncated", "bad_date",
                    "empty_content", "other_author_or_link", "missing_outer_time", "oversized"):
            self.assertEqual(counts[key], 1, key)
        self.assertTrue(all(type(value) is int and 0 <= value <= 99999 for value in counts.values()))
        self.assertNotIn("PRIVATE", json.dumps(counts))
        self.assertNotIn("http", json.dumps(counts))

    def test_truncated_outer_post_skipped_but_truncated_quote_does_not_hide_original(self):
        more = '<button data-testid="tweet-text-show-more-link">Show more</button>'
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += self.article(extra=more)
        body += self.article("124", extra=f'<div data-testid="quoteTweet">{more}</div>')
        result = self.extractor(body)
        self.assertEqual(result["truncated"], 1)
        self.assertEqual([p["url"].rsplit("/", 1)[1] for p in result["posts"]], ["124"])

    def test_sign_in_required(self):
        result = self.extractor(self.article())
        self.assertEqual(result, {"error": "sign_in_required"})

    def test_wrong_profile_url_rejected_in_page(self):
        result = self.extractor(self.article(), "https://x.com/Other")
        self.assertEqual(result, {"error": "page_changed"})

    def test_timezone_required(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += self.article(timestamp="2026-10-13T12:00:00")
        self.assertEqual(self.extractor(body)["posts"], [])

    def test_native_text_and_image_limits_without_truncating_text(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += self.article("123", text="x" * 25001)
        photos = '<div data-testid="tweetPhoto">' + "".join(
            f'<img src="https://pbs.twimg.com/media/{index}.jpg">' for index in range(6)
        ) + "</div>"
        body += self.article("124", text="y" * 25000, extra=photos)
        result = self.extractor(body)
        self.assertEqual(result["oversized"], 1)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["text"], "y" * 25000)
        self.assertEqual(len(result["posts"][0]["image_urls"]), 4)
        self.assertEqual(result["posts"][0]["url"], "https://x.com/Ravens/status/124")

    def test_initial_collection_returns_to_timeline_top(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>'
        body += '<div style="height:5000px"></div>' + self.article()
        self.page.set_content(body)
        self.page.evaluate("window.scrollTo(0, 3000)")
        self.assertGreater(self.page.evaluate("window.scrollY"), 0)
        self.page.add_script_tag(content=(EXTENSION / "collector.js").read_text(encoding="utf-8"))
        self.page.evaluate("extractRavensPage(false)")
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)

    def test_settling_does_not_chain_background_throttled_timers(self):
        self.page.set_content('<button data-testid="SideNav_AccountSwitcher_Button"></button>' +
                              self.article())
        self.page.add_script_tag(content=(EXTENSION / "collector.js").read_text(encoding="utf-8"))
        self.page.evaluate("""() => {
            const timer = window.setTimeout.bind(window);
            window.timerDelays = [];
            window.setTimeout = (fn, ms) => {
                timerDelays.push(ms);
                return timer(fn, 1);
            };
        }""")
        result = self.page.evaluate("extractRavensPage(false)")
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(self.page.evaluate("timerDelays"), [1000])

    def test_page_navigation_during_settling_stops_extraction(self):
        body = '<button data-testid="SideNav_AccountSwitcher_Button"></button>' + self.article()
        self.page.set_content(body)
        self.page.add_script_tag(content=(EXTENSION / "collector.js").read_text(encoding="utf-8"))
        result = self.page.evaluate("""async () => {
            setTimeout(() => history.pushState({}, "", "/Other"), 20);
            return await extractRavensPage(false);
        }""")
        self.assertEqual(result, {"error": "page_changed"})

    def test_default_off_start_collect_stop_and_protocol(self):
        self.worker()
        self.assertFalse(self.page.evaluate("request('status')")["enabled"])
        self.assertEqual(self.page.evaluate("mock.calls.length"), 0)
        self.page.evaluate("request('start')")
        self.wait_collected()
        result = self.page.evaluate("({state: state(), calls: mock.calls, alarms: mock.alarms, injections: mock.injections})")
        self.assertEqual(result["alarms"], {"ravens-refresh": {"delayInMinutes": 1, "periodInMinutes": 1}})
        self.assertEqual(len(result["injections"]), 8)
        self.assertEqual(result["injections"][0]["args"], [False])
        self.assertTrue(all(item["tabId"] == 1 for item in result["injections"]))
        self.assertEqual(result["calls"][-1]["host"], "com.thecastle.ravens_feed")
        payload = result["calls"][-1]["payload"]
        self.assertEqual(set(payload), {"version", "posts"})
        self.assertEqual(len(payload["posts"]), 1)
        self.assertEqual(set(payload["posts"][0]), {"url", "text", "created_at", "image_urls"})
        self.page.evaluate("request('stop')")
        self.assertFalse(self.page.evaluate("state().enabled"))
        self.assertIsNone(self.page.evaluate("state().tabId"))
        self.assertEqual(self.page.evaluate("mock.alarms"), {})
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload"),
                         {"version": 1, "error": "collector_unavailable"})
        self.page.evaluate("alarm()")
        self.page.evaluate("request('status')")
        self.assertEqual(self.page.evaluate("mock.reloads"), [])

    def test_start_rejects_noncanonical_urls_and_incognito(self):
        self.worker()
        urls = ["https://x.com/Other", "https://x.com/Ravens/status/1",
                "https://x.com.evil.test/Ravens", "http://x.com/Ravens",
                "https://www.x.com/Ravens", "https://x.com/Ravens/lists",
                "https://user:password@x.com/Ravens", "https://x.com:123/Ravens",
                "https://x.com/Ravens#fragment"]
        for url in urls:
            with self.subTest(url=url):
                self.page.evaluate("url => { mock.tabs[1].url = url; }", url)
                result = self.page.evaluate("request('start')")
                self.assertFalse(result["enabled"])
                self.assertIsNotNone(result["error"])
        self.page.evaluate("mock.tabs[1] = {id: 1, url: 'https://x.com/Ravens', incognito: true}")
        self.assertFalse(self.page.evaluate("request('start')")["enabled"])
        self.assertEqual(self.page.evaluate("mock.injections"), [])
        self.assertEqual(self.page.evaluate("mock.reloads"), [])

    def test_case_slash_query_allowed_and_alarm_only_refreshes_tracked_tab(self):
        self.worker()
        self.page.evaluate("mock.tabs[1].url = 'https://x.com/ravens/?lang=en'")
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.tabs[2] = {id: 2, url: "https://x.com/Other", status: "complete"};
            mock.activeId = 2;
            alarm();
        }""")
        self.page.wait_for_function("mock.reloads.length === 1 && mock.injections.length === 16")
        self.page.evaluate("request('status')")
        self.assertEqual(self.page.evaluate("mock.reloads"), [1])
        self.assertTrue(self.page.evaluate("mock.injections.every(call => call.tabId === 1)"))

    def test_navigation_away_and_pending_navigation_never_reload(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("mock.tabs[1].pendingUrl = 'https://x.com/Other'; alarm()")
        self.wait_retry()
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.assertIn("left", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.error"), "collector_unavailable")

    def test_tab_close_and_navigation_event_schedule_recovery(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("chrome.tabs.onRemoved.fire(1)")
        self.wait_retry()
        self.assertIn("closed", self.page.evaluate("state().error"))
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.tabs[1].url = 'https://x.com/home';
            chrome.tabs.onUpdated.fire(1, {url: mock.tabs[1].url}, mock.tabs[1]);
        }""")
        self.wait_retry()
        self.assertIn("ravens-refresh", self.page.evaluate("mock.alarms"))

    def test_stop_during_extraction_prevents_stale_send(self):
        self.worker()
        self.page.evaluate("() => { mock.scriptGate = new Promise(resolve => { mock.release = resolve; }); }")
        self.page.evaluate("request('start')")
        self.page.wait_for_function("mock.injections.length === 1")
        self.page.evaluate("request('stop')")
        self.page.evaluate("mock.release()")
        self.page.evaluate("request('status')")
        self.page.wait_for_timeout(30)
        self.assertFalse(self.page.evaluate("mock.calls.some(call => call.payload.posts)"))
        self.assertFalse(self.page.evaluate("state().enabled"))

    def test_new_start_generation_blocks_old_extraction_and_duplicate_alarms(self):
        self.worker()
        self.page.evaluate("() => { mock.scriptGate = new Promise(resolve => { mock.release = resolve; }); }")
        first = self.page.evaluate("request('start')")
        self.page.wait_for_function("mock.injections.length === 1")
        self.page.evaluate("alarm(); alarm()")
        self.page.evaluate("request('status')")
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.page.evaluate("request('stop')")
        second = self.page.evaluate("request('start')")
        self.assertNotEqual(first["generation"], second["generation"])
        self.page.wait_for_function("mock.injections.length === 2")
        self.page.evaluate("mock.release(); mock.scriptGate = null")
        self.wait_collected()
        self.assertEqual(self.page.evaluate("mock.calls.filter(call => call.payload.posts).length"), 1)

    def test_native_failure_retries_and_does_not_expose_host_details(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("mock.nativeReject = true; alarm()")
        self.wait_retry()
        error = self.page.evaluate("state().error")
        self.assertIn("native", error)
        self.assertNotIn("secret", error)
        self.assertIn("could not be invalidated", error)
        self.assertIn("ravens-refresh", self.page.evaluate("mock.alarms"))

    def test_native_unavailable_on_start_keeps_retrying(self):
        self.worker()
        self.page.evaluate("mock.nativeError = true")
        result = self.page.evaluate("request('start')")
        self.assertTrue(result["enabled"])
        self.assertIn("native", result["error"])
        self.assertIn("not yet registered", result["error"])
        self.assertIn("extension ID", result["error"])
        self.assertNotIn("sensitive", result["error"])
        self.assertEqual(self.page.evaluate("mock.injections"), [])

    def test_unchanged_refresh_reuses_writer_and_advances_heartbeat_without_focus(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        first = self.page.evaluate("state().lastUpdated")
        connections = self.page.evaluate("mock.ports.length")
        self.page.evaluate("""() => {
            mock.tabs[2] = {id: 2, url: "https://offline.invalid", status: "complete"};
            mock.activeId = 2;
            alarm();
        }""")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertGreater(self.page.evaluate("state().lastUpdated"), first)
        self.assertEqual(self.page.evaluate("mock.ports.length"), connections)
        self.assertEqual(self.page.evaluate("mock.activeId"), 2)
        self.assertEqual(self.page.evaluate("mock.reloads"), [1])
        self.assertTrue(self.page.evaluate("""() => {
            const writes = mock.calls.filter(call => call.payload.posts);
            return JSON.stringify(writes[0].payload) === JSON.stringify(writes[1].payload);
        }"""))
        self.page.evaluate("request('stop')")
        self.assertTrue(self.page.evaluate("mock.ports.every(port => port.closed)"))
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.error"), "collector_unavailable")

    def test_host_disconnect_between_cycles_reconnects_on_next_refresh(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        connections = self.page.evaluate("mock.ports.length")
        self.page.evaluate("mock.ports.at(-1).closed = true; mock.ports.at(-1).onDisconnect.fire(); alarm()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertEqual(self.page.evaluate("mock.ports.length"), connections + 1)

    def test_host_disconnect_during_write_retries_and_invalidates(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.nativeGate = new Promise(resolve => { mock.release = resolve; });
            alarm();
        }""")
        self.page.wait_for_function("mock.calls.filter(call => call.payload.posts).length === 2")
        self.page.evaluate("""() => {
            mock.ports.at(-1).closed = true;
            mock.ports.at(-1).onDisconnect.fire();
            mock.release();
            mock.nativeGate = null;
        }""")
        self.wait_retry()
        self.assertIn("native", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.error"), "collector_unavailable")
        self.assertTrue(self.page.evaluate("mock.ports.every(port => port.closed)"))

    def test_truncation_warning_is_explicit_and_skipped_posts_not_sent(self):
        self.worker()
        self.page.evaluate("mock.batch.truncated = 1")
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.assertIn("truncated", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.posts.length"), 1)
        self.page.evaluate("request('stop')")
        self.page.evaluate("mock.batch.posts = []")
        self.page.evaluate("request('start')")
        self.wait_retry()
        self.assertIn("truncated", self.page.evaluate("state().error"))

    def test_reload_waits_for_load_completion(self):
        self.worker()
        self.page.evaluate("mock.tabs[1].status = 'loading'")
        self.page.evaluate("request('start')")
        self.page.wait_for_timeout(30)
        self.assertEqual(self.page.evaluate("mock.injections"), [])
        self.page.evaluate("mock.tabs[1].status = 'complete'")
        self.wait_collected()

    def test_live_operation_does_not_expire_lease_into_duplicate_refresh(self):
        self.worker()
        self.page.evaluate("() => { mock.scriptGate = new Promise(resolve => { mock.release = resolve; }); }")
        self.page.evaluate("request('start')")
        self.page.wait_for_function("mock.injections.length === 1")
        self.page.evaluate("mock.session.ravensCollector.busy = Date.now() - 90000; alarm()")
        self.page.evaluate("request('status')")
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.page.evaluate("mock.release(); mock.scriptGate = null")
        self.wait_collected()

    def test_stop_invalidation_is_ordered_after_pending_native_write(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.nativeGate = new Promise(resolve => { mock.release = resolve; });
            alarm();
        }""")
        self.page.wait_for_function("mock.calls.filter(call => call.payload.posts).length === 2")
        self.page.evaluate("() => { mock.stopping = request('stop'); }")
        self.page.wait_for_timeout(20)
        self.assertEqual(self.page.evaluate("mock.calls.length"), 3)
        self.assertTrue(self.page.evaluate("!!mock.calls.at(-1).payload.posts"))
        self.page.evaluate("mock.release(); mock.nativeGate = null")
        self.page.evaluate("mock.stopping")
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.error"), "collector_unavailable")
        self.assertFalse(self.page.evaluate("state().enabled"))

    def test_future_posts_are_never_sent_and_snapshots_are_bounded(self):
        self.worker()
        self.page.evaluate("""() => {
            const now = Date.now();
            mock.batch.posts = Array.from({length: 310}, (_, index) => ({
                url: `https://x.com/Ravens/status/${index + 1}`,
                text: "Original post", image_urls: [],
                created_at: new Date(now - index * 1000).toISOString()
            }));
            mock.batch.posts.push({
                url: "https://x.com/Ravens/status/999", text: "Future", image_urls: [],
                created_at: new Date(now + 30000).toISOString()
            });
        }""")
        self.page.evaluate("request('start')")
        self.wait_collected()
        posts = self.page.evaluate("mock.calls.at(-1).payload.posts")
        self.assertEqual(len(posts), 300)
        self.assertFalse(any(post["text"] == "Future" for post in posts))
        self.assertEqual(posts[0]["url"], "https://x.com/Ravens/status/1")
        self.assertEqual(posts[-1]["url"], "https://x.com/Ravens/status/300")

    def test_extraction_error_and_stale_posts_invalidate(self):
        self.worker()
        self.page.evaluate("mock.batch = {error: 'sign_in_required'}")
        self.page.evaluate("request('start')")
        self.wait_retry()
        self.assertIn("Sign in manually", self.page.evaluate("state().error"))
        self.page.evaluate("""mock.batch = {posts: [{
            url: 'https://x.com/Ravens/status/1', text: 'Old', image_urls: [],
            created_at: '2000-01-01T00:00:00Z'
        }]}""")
        self.page.evaluate("request('start')")
        self.wait_retry()
        self.assertIn("No recent", self.page.evaluate("state().error"))
        self.assertFalse(self.page.evaluate("mock.calls.some(call => call.payload.posts)"))

    def test_empty_feed_diagnostics_are_bounded_allowlisted_and_local_only(self):
        self.worker()
        self.page.evaluate("""() => {
            mock.batch.diagnostics = {
                articles_seen: 10, own_timestamp_links: 4, social_rejections: 999999,
                quote_time_rejections: "PRIVATE", bad_date: -1,
                private_url: "https://x.com/Other/status/987654321"
            };
            mock.batch.posts = [{
                url: "https://x.com/Ravens/status/1", text: "Old", image_urls: [],
                created_at: "2000-01-01T00:00:00Z"
            }, {
                url: "https://x.com/Ravens/status/2", text: "Future", image_urls: [],
                created_at: new Date(Date.now() + 60000).toISOString()
            }];
        }""")
        self.page.evaluate("request('start')")
        self.wait_retry()
        counts = self.page.evaluate("state().diagnostics")
        self.assertEqual(counts["pages"], 8)
        self.assertEqual(counts["articles_seen"], 80)
        self.assertEqual(counts["own_timestamp_links"], 32)
        self.assertEqual(counts["social_rejections"], 99999)
        self.assertEqual(counts["quote_time_rejections"], 0)
        self.assertEqual(counts["bad_date"], 0)
        self.assertEqual(counts["unique_posts"], 2)
        self.assertEqual(counts["older_than_seven_days"], 1)
        self.assertEqual(counts["future_dates"], 1)
        self.assertEqual(counts["sent_posts"], 0)
        self.assertNotIn("private_url", counts)
        self.assertTrue(all(type(value) is int and 0 <= value <= 99999 for value in counts.values()))
        self.assertTrue(self.page.evaluate("""mock.calls.every(call =>
            JSON.stringify(call.payload) === JSON.stringify({version: 1, error: "collector_unavailable"}))"""))
        self.page.evaluate("request('stop')")
        self.assertIsNone(self.page.evaluate("state().diagnostics"))

    def test_popup_shows_count_diagnostics_after_empty_collection(self):
        self.worker()
        self.page.evaluate("mock.batch = {posts: [], diagnostics: {articles_seen: 2, missing_outer_time: 2}}")
        self.page.evaluate("request('start')")
        self.wait_retry()
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        self.page.set_content(html.replace('<script src="popup.js"></script>', ""))
        self.page.evaluate("() => { chrome.runtime.sendMessage = message => request(message.action); }")
        self.page.add_script_tag(content=(EXTENSION / "popup.js").read_text(encoding="utf-8"))
        self.page.wait_for_function("!document.getElementById('diagnostics-panel').hidden")
        counts = json.loads(self.page.locator("#diagnostics").inner_text())
        self.assertEqual(counts["articles_seen"], 16)
        self.assertEqual(counts["missing_outer_time"], 16)
        self.assertEqual(counts["pages"], 8)
        self.assertIn("No recent", self.page.locator("#error").inner_text())
        self.page.locator("#stop").click()
        self.page.wait_for_function("document.getElementById('diagnostics-panel').hidden")

    def popup_copy_fixture(self, clipboard_fails=False):
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        self.page.set_content(html.replace('<script src="popup.js"></script>', ""))
        self.page.evaluate("""fails => {
            window.clipboardWrites = [];
            window.popupState = {
                enabled: false, count: 26, lastUpdated: "2026-10-14T02:00:00Z",
                error: "Check the tab manually.", diagnostics: {pages: 8, accepted: 26},
                lifecycle: {phase: "off", cycles_completed: 2},
                scheduler: {alarm_present: false, last_update_age_seconds: 130},
                history: {events: [{event: "failure", code: "load_failed"}],
                    last_failure: {event: "failure", code: "load_failed"}},
                tabId: 98765, generation: "PRIVATE-GENERATION",
                posts: [{text: "PRIVATE-POST"}], credentials: "PRIVATE-CREDENTIALS"
            };
            window.chrome = {runtime: {sendMessage: async () => structuredClone(popupState)}};
            Object.defineProperty(navigator, "clipboard", {configurable: true, value: {
                writeText: async text => {
                    if (fails) throw new Error("PRIVATE-CLIPBOARD-FAILURE");
                    clipboardWrites.push(text);
                }
            }});
        }""", clipboard_fails)
        self.page.add_script_tag(content=(EXTENSION / "popup.js").read_text(encoding="utf-8"))
        self.page.wait_for_function("document.getElementById('diagnostics').textContent.includes('accepted')")

    def test_popup_copy_snapshots_only_rendered_diagnostics_and_preserves_selection(self):
        self.popup_copy_fixture()
        self.page.locator("#copy-diagnostics").click()
        self.page.wait_for_function("document.getElementById('copy-feedback').textContent.startsWith('Diagnostics copied')")
        copied = self.page.evaluate("clipboardWrites[0]")
        payload = json.loads(copied)
        self.assertEqual(set(payload), {"status", "last_updated", "feed_count", "error", "freshness",
                                       "extraction_counts", "refresh_lifecycle", "persistent_history"})
        self.assertEqual(payload["status"], self.page.locator("#status").inner_text())
        self.assertEqual(payload["last_updated"], self.page.locator("#updated").inner_text())
        self.assertEqual(payload["feed_count"], "Last feed count: 26")
        self.assertEqual(payload["error"], "Check the tab manually.")
        self.assertEqual(payload["extraction_counts"], {"pages": 8, "accepted": 26})
        self.assertEqual(payload["refresh_lifecycle"]["cycles_completed"], 2)
        self.assertEqual(payload["persistent_history"]["last_failure"]["code"], "load_failed")
        self.assertNotIn("PRIVATE", copied)
        self.assertNotIn("98765", copied)
        self.page.evaluate("""() => {
            window.selectedDiagnosticNode = document.getElementById("diagnostics").firstChild;
            window.originalLifecycleNode = document.getElementById("lifecycle").firstChild;
            const range = document.createRange();
            range.selectNodeContents(document.getElementById("diagnostics"));
            getSelection().removeAllRanges();
            getSelection().addRange(range);
            window.selectedText = getSelection().toString();
        }""")
        self.page.evaluate("update()")
        self.assertTrue(self.page.evaluate("document.getElementById('diagnostics').firstChild === selectedDiagnosticNode"))
        self.assertTrue(self.page.evaluate("document.getElementById('lifecycle').firstChild === originalLifecycleNode"))
        self.assertTrue(self.page.evaluate("getSelection().toString() === selectedText"))
        self.assertIn("Diagnostics copied", self.page.locator("#copy-feedback").inner_text())

    def test_popup_copy_failure_has_persistent_safe_feedback(self):
        self.popup_copy_fixture(clipboard_fails=True)
        self.page.locator("#copy-diagnostics").click()
        self.page.wait_for_function("document.getElementById('copy-feedback').textContent.startsWith('Copy failed')")
        self.page.evaluate("update()")
        feedback = self.page.locator("#copy-feedback").inner_text()
        self.assertIn("try Copy diagnostics again", feedback)
        self.assertNotIn("PRIVATE", feedback)
        self.assertEqual(self.page.evaluate("clipboardWrites"), [])
        self.assertTrue(self.page.locator("#copy-diagnostics").is_enabled())

    def test_browser_restart_restores_intent_without_session_or_alarm(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("mock.session = {}; mock.alarms = {}; mock.tabs = {}")
        self.recreate_worker()
        self.page.evaluate("chrome.runtime.onStartup.fire()")
        self.wait_collected()
        self.assertTrue(self.page.evaluate("mock.local.ravensCollectorSettings.enabled"))
        self.assertEqual(self.page.evaluate("mock.tabs[state().tabId].url"), "https://x.com/Ravens")
        self.assertFalse(self.page.evaluate("mock.tabs[state().tabId].active"))
        self.assertIn("ravens-refresh", self.page.evaluate("mock.alarms"))
        self.assertTrue(self.page.evaluate(
            "mock.local.ravensCollectorHistory.events.some(event => event.event === 'browser_resumed')"))

    def test_worker_recreation_resumes_session_without_restarting(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        generation = self.page.evaluate("state().generation")
        self.recreate_worker()
        self.assertEqual(self.page.evaluate("request('status')")["generation"], generation)
        self.page.evaluate("alarm()")
        self.page.wait_for_function("mock.injections.length === 16")
        self.assertEqual(self.page.evaluate("mock.reloads"), [1])

    def test_repeating_alarm_survives_repeated_worker_recreation(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        initial_updated = self.page.evaluate("state().lastUpdated")
        for completed in range(2, 5):
            self.recreate_worker()
            self.page.evaluate("alarm()")
            self.page.wait_for_function(f"state().lifecycle.cycles_completed === {completed}")
        result = self.page.evaluate("request('status')")
        self.assertEqual(result["lifecycle"]["cycles_started"], 4)
        self.assertEqual(result["lifecycle"]["cycles_completed"], 4)
        self.assertEqual(result["lifecycle"]["alarms_seen"], 3)
        self.assertEqual(result["lifecycle"]["worker_resumes"], 3)
        self.assertEqual(result["lifecycle"]["phase"], "waiting_alarm")
        self.assertEqual(result["lifecycle"]["pages_completed"], 8)
        self.assertEqual(result["lifecycle"]["alarms_skipped_busy"], 0)
        self.assertTrue(result["scheduler"]["alarm_present"])
        self.assertEqual(result["scheduler"]["period_minutes"], 1)
        self.assertIsNotNone(result["scheduler"]["next_alarm_at"])
        self.assertLess(result["scheduler"]["last_update_age_seconds"], 5)
        self.assertGreater(result["lastUpdated"], initial_updated)
        self.assertEqual(self.page.evaluate("mock.reloads"), [1, 1, 1])
        self.assertEqual(self.page.evaluate("mock.calls.filter(call => call.payload.posts).length"), 4)

    def test_missing_alarm_repairs_only_explicitly_enabled_session(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("delete mock.alarms['ravens-refresh']")
        self.recreate_worker()
        self.page.wait_for_function("state().lifecycle.alarm_repairs === 1")
        self.assertTrue(self.page.evaluate("request('status')")["scheduler"]["alarm_present"])
        self.page.evaluate("alarm()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.page.evaluate("request('stop')")
        self.recreate_worker()
        self.assertFalse(self.page.evaluate("request('status')")["scheduler"]["alarm_present"])
        self.assertFalse(self.page.evaluate("state().enabled"))

    def test_refresh_measurements_distinguish_alarm_lag_from_cycle_time(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.assertIsNone(self.page.evaluate("state().lifecycle.last_save_gap_seconds"))
        self.assertIsNone(self.page.evaluate("state().lifecycle.last_reload_to_save_seconds"))
        self.page.evaluate("""() => {
            mock.session.ravensCollector.lastUpdated = new Date(Date.now() - 130000).toISOString();
            chrome.alarms.onAlarm.fire({name: "ravens-refresh", scheduledTime: Date.now() - 18000});
        }""")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        timing = self.page.evaluate("request('status')")["lifecycle"]
        self.assertIsNotNone(timing["last_alarm_scheduled_at"])
        self.assertGreaterEqual(timing["last_alarm_lag_seconds"], 18)
        self.assertLess(timing["last_alarm_lag_seconds"], 30)
        self.assertGreaterEqual(timing["last_save_gap_seconds"], 130)
        self.assertLess(timing["last_save_gap_seconds"], 140)
        for name in ("last_cycle_duration_seconds", "last_native_duration_seconds",
                     "last_reload_to_save_seconds"):
            self.assertGreaterEqual(timing[name], 0)
            self.assertLess(timing[name], 10)

    def test_reload_status_event_without_url_uses_current_tracked_tab(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            chrome.tabs.reload = async id => {
                mock.reloads.push(id);
                mock.tabs[id].status = "loading";
                chrome.tabs.onUpdated.fire(id, {status: "loading"}, {id, status: "loading"});
                setTimeout(() => {
                    mock.tabs[id].status = "complete";
                    chrome.tabs.onUpdated.fire(id, {status: "complete"}, {id, status: "complete"});
                }, 10);
            };
            alarm();
        }""")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertTrue(self.page.evaluate("state().enabled"))
        self.assertEqual(self.page.evaluate("mock.reloads"), [1])
        self.page.evaluate("""() => {
            mock.tabs[1].url = "https://x.com/Other";
            chrome.tabs.onUpdated.fire(1, {status: "loading"}, {id: 1, status: "loading"});
        }""")
        self.wait_retry()
        self.assertIn("left", self.page.evaluate("state().error"))

    def test_suspended_page_times_out_and_cannot_send_late_snapshot(self):
        self.worker()
        self.page.evaluate("""() => {
            const timer = window.setTimeout.bind(window);
            window.setTimeout = (fn, ms) => timer(fn, ms > 10000 ? 5 : ms);
            mock.scriptGate = new Promise(resolve => { mock.release = resolve; });
        }""")
        self.page.evaluate("request('start')")
        self.wait_retry()
        self.assertIn("stalled or was suspended", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("state().lifecycle.phase"), "retry_wait")
        self.assertFalse(self.page.evaluate("mock.calls.some(call => call.payload.posts)"))
        self.page.evaluate("mock.release(); mock.scriptGate = null")
        self.page.wait_for_timeout(20)
        self.assertFalse(self.page.evaluate("mock.calls.some(call => call.payload.posts)"))
        self.assertEqual(self.page.evaluate("mock.calls.at(-1).payload.error"), "collector_unavailable")

    def test_total_cycle_deadline_prevents_slow_pages_accumulating_indefinitely(self):
        self.worker()
        self.page.evaluate("""() => {
            const actualNow = Date.now;
            let elapsed = 0;
            Date.now = () => actualNow() + elapsed;
            chrome.scripting.executeScript = async options => {
                mock.injections.push({tabId: options.target.tabId});
                elapsed += 11000;
                return [{result: structuredClone(mock.batch)}];
            };
        }""")
        self.page.evaluate("request('start')")
        self.wait_retry()
        self.assertIn("stalled or was suspended", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("mock.injections.length"), 3)
        self.assertFalse(self.page.evaluate("mock.calls.some(call => call.payload.posts)"))

    def test_status_remains_readable_during_pending_native_write(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.nativeGate = new Promise(resolve => { mock.release = resolve; });
            alarm();
        }""")
        self.page.wait_for_function("mock.calls.filter(call => call.payload.posts).length === 2")
        result = self.page.evaluate("request('status')")
        self.assertEqual(result["lifecycle"]["phase"], "sending")
        self.assertEqual(result["lifecycle"]["alarms_seen"], 1)
        self.assertEqual(result["lifecycle"]["cycles_completed"], 1)
        self.assertTrue(result["scheduler"]["worker_operation_active"])
        self.assertIsNotNone(result["lifecycle"]["native_write_started_at"])
        self.page.evaluate("mock.release(); mock.nativeGate = null")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")

    def test_popup_reports_stale_feed_and_alarm_diagnostics(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("mock.session.ravensCollector.lastUpdated = new Date(Date.now() - 130000).toISOString()")
        html = (EXTENSION / "popup.html").read_text(encoding="utf-8")
        self.page.set_content(html.replace('<script src="popup.js"></script>', ""))
        self.page.evaluate("() => { chrome.runtime.sendMessage = message => request(message.action); }")
        self.page.add_script_tag(content=(EXTENSION / "popup.js").read_text(encoding="utf-8"))
        self.page.wait_for_function("document.getElementById('freshness').textContent.includes('stale')")
        timing = json.loads(self.page.locator("#lifecycle").inner_text())
        self.assertEqual(timing["phase"], "waiting_alarm")
        self.assertTrue(timing["alarm_present"])
        self.assertGreaterEqual(timing["last_update_age_seconds"], 130)
        self.assertNotIn("tabId", timing)
        self.assertNotIn("generation", timing)
        self.assertNotIn("http", json.dumps(timing))

    def test_no_webpage_message_entrypoint(self):
        self.worker()
        response = self.page.evaluate(
            "request('start', {id: chrome.runtime.id, url: 'https://x.com/Ravens'})")
        self.assertEqual(response, "rejected")
        self.assertEqual(self.page.evaluate("mock.calls"), [])

    def test_transient_failure_backoff_is_capped_and_success_resets_it(self):
        self.worker()
        self.page.evaluate("mock.batch.posts = []; request('start')")
        self.wait_retry()
        for failures, delay in enumerate((1, 2, 4, 8, 15, 15), start=1):
            self.assertEqual(self.page.evaluate("state().failures"), failures)
            remaining = self.page.evaluate("(Date.parse(state().retryAt) - Date.now()) / 60000")
            self.assertGreater(remaining, delay - 0.1)
            self.assertLessEqual(remaining, delay)
            count = self.page.evaluate("mock.injections.length")
            self.page.evaluate("alarm()")
            self.page.wait_for_timeout(20)
            self.assertEqual(self.page.evaluate("mock.injections.length"), count)
            if failures < 6:
                self.page.evaluate("retryNow()")
                self.page.wait_for_function(f"state().failures === {failures + 1}")
                self.wait_retry()
        self.page.evaluate("""() => {
            mock.batch.posts = [{url: "https://x.com/Ravens/status/123", text: "Recovered",
                created_at: new Date().toISOString(), image_urls: []}];
            retryNow();
        }""")
        self.wait_collected()
        self.assertEqual(self.page.evaluate("state().failures"), 0)
        self.assertIsNone(self.page.evaluate("state().retryAt"))
        self.assertIsNone(self.page.evaluate("state().error"))
        history = self.page.evaluate("request('status')")["history"]
        self.assertEqual(history["last_failure"]["code"], "no_posts")
        self.assertEqual(history["events"][-1]["event"], "recovered")

    def test_closed_tab_is_replaced_without_selecting_it(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            delete mock.tabs[1];
            mock.tabs[2] = {id: 2, url: "https://x.com/Other", status: "complete"};
            mock.activeId = 2;
            chrome.tabs.onRemoved.fire(1);
        }""")
        self.wait_retry()
        self.assertIsNone(self.page.evaluate("state().tabId"))
        self.page.evaluate("retryNow()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertEqual(self.page.evaluate("state().tabId"), 3)
        self.assertEqual(self.page.evaluate("mock.activeId"), 2)
        self.assertFalse(self.page.evaluate("mock.tabs[3].active"))
        self.assertEqual(self.page.evaluate("mock.reloads"), [3])

    def test_recovery_reuses_approved_tab_and_ignores_incognito_and_pending_navigation(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.tabs[1].url = "https://x.com/Other";
            mock.tabs[2] = {id: 2, url: "https://x.com/Ravens", incognito: true};
            mock.tabs[3] = {id: 3, url: "https://x.com/Ravens", pendingUrl: "https://x.com/home"};
            mock.tabs[4] = {id: 4, url: "https://x.com/ravens/?lang=en", status: "complete"};
            chrome.tabs.onUpdated.fire(1, {url: mock.tabs[1].url});
        }""")
        self.wait_retry()
        self.page.evaluate("retryNow()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertEqual(self.page.evaluate("mock.reloads"), [4])
        self.assertEqual(self.page.evaluate("Object.keys(mock.tabs).length"), 4)

    def test_sign_in_redirect_waits_without_reload_or_duplicate_tabs(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            mock.tabs[1].url = "https://x.com/i/flow/login";
            chrome.tabs.onUpdated.fire(1, {url: mock.tabs[1].url});
        }""")
        self.wait_retry()
        self.assertEqual(self.page.evaluate("state().lifecycle.phase"), "needs_attention")
        for _ in range(3):
            self.page.evaluate("retryNow()")
            self.page.wait_for_timeout(20)
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.assertEqual(self.page.evaluate("Object.keys(mock.tabs).length"), 1)
        self.assertEqual(self.page.evaluate("mock.injections.length"), 8)
        self.page.evaluate("mock.tabs[1].url = 'https://x.com/Ravens'; retryNow()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.assertFalse(self.page.evaluate("state().attention"))

    def test_challenge_waits_for_manual_resolution_then_resumes(self):
        self.worker()
        self.page.evaluate("mock.batch = {error: 'challenge'}; request('start')")
        self.wait_retry()
        self.assertEqual(self.page.evaluate("state().lifecycle.phase"), "needs_attention")
        self.page.evaluate("retryNow()")
        self.page.wait_for_function("state().failures === 2")
        self.wait_retry()
        self.assertEqual(self.page.evaluate("mock.reloads"), [])
        self.page.evaluate("""() => {
            mock.batch = {posts: [{url: "https://x.com/Ravens/status/123", text: "Public",
                created_at: new Date().toISOString(), image_urls: []}]};
            retryNow();
        }""")
        self.wait_collected()
        self.assertFalse(self.page.evaluate("state().attention"))

    def test_native_timeout_releases_queue_and_late_reply_cannot_send(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            const timer = window.setTimeout.bind(window);
            window.setTimeout = (fn, ms) => timer(fn, ms === 10000 ? 50 : ms);
            mock.nativeGate = new Promise(resolve => { mock.release = resolve; });
            alarm();
        }""")
        self.wait_retry()
        self.assertIn("ten seconds", self.page.evaluate("state().error"))
        self.assertEqual(self.page.evaluate("request('status')")["history"]["last_failure"]["code"], "native_timeout")
        self.page.evaluate("mock.release(); mock.nativeGate = null; retryNow()")
        self.page.wait_for_function("state().lifecycle.cycles_completed === 2")
        self.page.evaluate("request('stop')")
        self.assertFalse(self.page.evaluate("state().enabled"))

    def test_history_survives_pause_start_and_browser_restart_and_is_bounded(self):
        self.worker()
        self.page.evaluate("mock.batch.error = 'SECRET-ERROR'; request('start')")
        self.wait_retry()
        failure = self.page.evaluate("request('status')")["history"]["last_failure"]
        self.assertEqual(failure["code"], "extraction_failed")
        self.assertEqual(failure["phase"], "extracting")
        self.assertEqual(failure["version"], "1.1.0")
        self.page.evaluate("request('stop'); delete mock.batch.error")
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.assertEqual(self.page.evaluate("request('status')")["history"]["last_failure"], failure)
        self.page.evaluate("request('stop')")
        self.page.evaluate("""() => {
            const history = mock.local.ravensCollectorHistory;
            history.events = Array.from({length: 200}, () => structuredClone(history.events[0]));
            mock.session = {};
            mock.alarms = {};
        }""")
        self.recreate_worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        history = self.page.evaluate("request('status')")["history"]
        self.assertEqual(len(history["events"]), 200)
        self.assertEqual(history["last_failure"], failure)
        for private in ("SECRET", "tabId", "generation", "https://", "Ravens roster news"):
            self.assertNotIn(private, json.dumps(history))

    def test_pause_survives_browser_restart_and_install_event(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("request('stop')")
        self.page.evaluate("mock.session = {}; mock.alarms = {}; mock.tabs = {}")
        self.recreate_worker()
        self.page.evaluate("chrome.runtime.onStartup.fire(); chrome.runtime.onInstalled.fire()")
        self.page.wait_for_timeout(30)
        self.assertFalse(self.page.evaluate("request('status')")["enabled"])
        self.assertFalse(self.page.evaluate("mock.local.ravensCollectorSettings.enabled"))
        self.assertEqual(self.page.evaluate("mock.alarms"), {})
        self.assertEqual(self.page.evaluate("mock.tabs"), {})

    def test_update_migrates_enabled_session_without_clearing_history(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("delete mock.local.ravensCollectorSettings")
        self.recreate_worker()
        self.page.wait_for_function("mock.local.ravensCollectorSettings?.enabled")
        self.assertTrue(self.page.evaluate("state().enabled"))
        self.assertTrue(self.page.evaluate(
            "mock.local.ravensCollectorHistory.events.some(event => event.event === 'first_save')"))

    def test_saved_pause_wins_over_interrupted_session_write(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("mock.local.ravensCollectorSettings.enabled = false")
        self.recreate_worker()
        self.page.wait_for_function("!state().enabled")
        self.assertFalse(self.page.evaluate("request('status')")["enabled"])
        self.assertFalse(self.page.evaluate("mock.local.ravensCollectorSettings.enabled"))

    def test_reload_and_tab_creation_failures_have_specific_safe_codes(self):
        self.worker()
        self.page.evaluate("request('start')")
        self.wait_collected()
        self.page.evaluate("""() => {
            chrome.tabs.reload = async () => { throw new Error("PRIVATE-RELOAD"); };
            alarm();
        }""")
        self.wait_retry()
        self.assertEqual(self.page.evaluate("request('status')")["history"]["last_failure"]["code"], "reload_failed")
        self.page.evaluate("""() => {
            delete mock.tabs[1];
            chrome.tabs.onRemoved.fire(1);
        }""")
        self.page.wait_for_function("state().tabId === null")
        self.wait_retry()
        self.page.evaluate("""() => {
            chrome.tabs.create = async () => { throw new Error("PRIVATE-CREATE"); };
            retryNow();
        }""")
        self.page.wait_for_function(
            "mock.local.ravensCollectorHistory.last_failure.code === 'tab_recovery_failed'")
        self.wait_retry()
        self.assertNotIn("PRIVATE", json.dumps(self.page.evaluate("request('status')")))


@unittest.skipUnless(HAS_PLAYWRIGHT and os.environ.get("EDGE_EXTENSION_REAL_BROWSER_TEST") == "1",
                     "opt-in real MV3 test takes approximately 150 seconds")
class RealMV3Tests(unittest.TestCase):
    def test_real_alarm_reload_and_host_permission(self):
        """Only native I/O is replaced; permissions, alarms, reload, scripts and storage are real."""
        from playwright.sync_api import sync_playwright
        owned = EXTENSION / (".browser-verification-" + uuid.uuid4().hex)
        extension = owned / "extension"
        extension.mkdir(parents=True)
        try:
            for name in ("manifest.json", "collector.js", "worker.js", "popup.html", "popup.js", "popup.css"):
                shutil.copyfile(EXTENSION / name, extension / name)
            original = (extension / "worker.js").read_text(encoding="utf-8")
            native_fixture = r"""
chrome.runtime.connectNative = host => {
    let listener;
    return {
        onMessage: {addListener: fn => { listener = fn; }},
        onDisconnect: {addListener: () => {}},
        disconnect: () => {},
        postMessage: payload => {
            void (async () => {
                const previous = (await chrome.storage.session.get("testNativeWrites")).testNativeWrites || [];
                const fetched_at = new Date().toISOString();
                previous.push({at: fetched_at, kind: payload.posts ? "posts" : "invalidate",
                    count: payload.posts?.length || 0, host_ok: host === "com.thecastle.ravens_feed"});
                await chrome.storage.session.set({testNativeWrites: previous});
                listener(payload.error ? {ok: true} : {ok: true, count: payload.posts.length, fetched_at});
            })();
        }
    };
};
"""
            (extension / "worker.js").write_text(native_fixture + original, encoding="utf-8")
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(owned / "profile"), channel="chromium", headless=True,
                    ignore_default_args=["--disable-extensions", "--disable-background-timer-throttling",
                                         "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"],
                    args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}",
                          "--enable-unsafe-extension-debugging"],
                )
                try:
                    navigations = []

                    def offline_page(route):
                        if route.request.is_navigation_request():
                            navigations.append(route.request.url)
                        if route.request.url == "https://x.com/Ravens":
                            from datetime import datetime, timezone
                            stamp = datetime.now(timezone.utc).isoformat()
                            body = ('<button data-testid="SideNav_AccountSwitcher_Button">Account</button>'
                                    '<article data-testid="tweet">'
                                    '<a role="link" href="https://x.com/Ravens/status/123">'
                                    f'<time datetime="{stamp}">Now</time></a>'
                                    '<div data-testid="tweetText">Offline original Ravens fixture.</div>'
                                    '</article><div style="height:12000px"></div>')
                        else:
                            body = "<p>Offline non-target page</p>"
                        route.fulfill(content_type="text/html", body=body)

                    context.route("https://**/*", offline_page)
                    context.route("http://**/*", offline_page)
                    service = context.service_workers[0] if context.service_workers else context.wait_for_event(
                        "serviceworker", timeout=15000)
                    extension_id = service.url.split("/")[2]
                    ravens = context.pages[0]
                    ravens.goto("https://x.com/Ravens")
                    before = service.evaluate("""async () =>
                        (await chrome.tabs.query({active: true, currentWindow: true}))[0].url || null""")
                    self.assertEqual(before, "https://x.com/Ravens")
                    browser_protocol = context.browser.new_browser_cdp_session()
                    targets = browser_protocol.send("Target.getTargets", {
                        "filter": [{"type": "tab", "exclude": False}, {"exclude": True}]
                    })["targetInfos"]
                    target = next(item["targetId"] for item in targets if item["url"] == "https://x.com/Ravens")
                    browser_protocol.send("Extensions.triggerAction", {"id": extension_id, "targetId": target})
                    approved_tab = service.evaluate("""async () => {
                        const tab = (await chrome.tabs.query({active: true, currentWindow: true}))[0];
                        return {id: tab.id, url: tab.url, windowId: tab.windowId};
                    }""")
                    self.assertEqual(approved_tab["url"], "https://x.com/Ravens")
                    # An inactive copy of the real popup preserves the target tab selected by Start.
                    with context.expect_page() as opening:
                        service.evaluate("""async windowId => await chrome.tabs.create({
                            url: chrome.runtime.getURL("popup.html"), active: false, windowId
                        })""", approved_tab["windowId"])
                    popup = opening.value
                    popup.wait_for_load_state()
                    popup.wait_for_function("!document.getElementById('start').disabled")
                    popup.evaluate("document.getElementById('start').click()")
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        initial = popup.evaluate("""async () =>
                            (await chrome.storage.session.get("ravensCollector")).ravensCollector""")
                        if initial and initial.get("lifecycle", {}).get("cycles_completed") == 1:
                            break
                        popup.wait_for_timeout(250)
                    else:
                        self.fail(f"Initial real collection did not complete: {initial}")
                    observed_start = time.monotonic()
                    popup.close()
                    with context.expect_page() as opening_other:
                        service.evaluate("""async windowId => await chrome.tabs.create({
                            url: "https://offline.invalid/untouched", active: true, windowId
                        })""", approved_tab["windowId"])
                    other = opening_other.value
                    other.wait_for_load_state()
                    selected = service.evaluate("""async windowId =>
                        (await chrome.tabs.query({windowId, active: true}))[0].id""", approved_tab["windowId"])
                    self.assertNotEqual(selected, approved_tab["id"])
                    visibility = ravens.evaluate("document.visibilityState")
                    # Allow a genuine minute alarm, reload and background collection to finish.
                    other.wait_for_timeout(75000)
                    # Exercise browser-managed worker recovery between real alarm deliveries.
                    tab_protocol = context.new_cdp_session(ravens)
                    tab_protocol.send("ServiceWorker.enable")
                    tab_protocol.send("ServiceWorker.stopAllWorkers")
                    remaining = 140 - (time.monotonic() - observed_start)
                    if remaining > 0:
                        other.wait_for_timeout(remaining * 1000)
                    inspection = context.new_page()
                    inspection.goto(f"chrome-extension://{extension_id}/popup.html")
                    result = inspection.evaluate("""async () => {
                        const stored = await chrome.storage.session.get(["ravensCollector", "testNativeWrites"]);
                        return {state: stored.ravensCollector, writes: stored.testNativeWrites || []};
                    }""")
                    state = result["state"]
                    writes = [item for item in result["writes"] if item["kind"] == "posts"]
                    print("\nReal MV3 lifecycle:", json.dumps(state["lifecycle"], sort_keys=True))
                    print("Offline Chromium target visibility:", visibility)
                    self.assertTrue(state["enabled"], state.get("error"))
                    self.assertGreaterEqual(state["lifecycle"]["alarms_seen"], 2, state["lifecycle"])
                    self.assertGreaterEqual(state["lifecycle"]["cycles_completed"], 3, state["lifecycle"])
                    self.assertGreaterEqual(len(writes), 3, state["lifecycle"])
                    self.assertTrue(all(item["host_ok"] for item in result["writes"]))
                    self.assertGreaterEqual(navigations.count("https://x.com/Ravens"), 3)
                    self.assertEqual(navigations.count("https://offline.invalid/untouched"), 1)
                    self.assertLessEqual(state["lifecycle"]["last_save_gap_seconds"], 120)
                finally:
                    context.close()
        finally:
            shutil.rmtree(owned)


if __name__ == "__main__":
    unittest.main()
