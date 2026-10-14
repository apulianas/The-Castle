# Dedicated Edge profile collector

1. Create a dedicated Edge UI profile named **1 Winning Pod**. The extension cannot
   inspect profile names: installing and using it only in that profile is your
   responsibility. Browser extension APIs are isolated to the profile where it is
   installed; incognito use is disabled.
2. In that profile, open `edge://extensions`, enable Developer mode, choose **Load
   unpacked**, and select this `edge_extension` directory. Give the extension ID to
   the native-host setup process so it can register the allowed extension origin.
3. Install/register the local native host `com.thecastle.ravens_feed` before Start.
   From the repository checkout, run
   `python -m ravens_bot.x_native_host --register <extension ID>`, replacing the
   placeholder with the ID shown in `edge://extensions`. This extension does not
   install the host or select its output path.
4. Manually sign in at X, open exactly `https://x.com/Ravens` (case-insensitive,
   optional trailing slash/query), then use the extension popup's **Start** button.
   Keep this dedicated window open. No remote browser debugging is needed.
5. Check the popup for the last host-confirmed update, count, and errors. **Stop**
   disables the alarm, forgets the tab, and invalidates the native feed.

Auto-refresh is **off by default**. Start uses the browser's `activeTab` grant and
stores only the tracked tab, a generation token, and status in `storage.session`.
Every minute it validates and reloads only that tab, waits for it to finish
loading, then reads up to eight bounded scroll positions. Closing the tab,
navigating away, signing out, extraction failure, or a native-host failure stops
collection. Restarting Edge requires another explicit Start. The extension cannot
make native writes while Edge is closed; the host/consumer must also reject stale
feeds. If the native host is unavailable, invalidation cannot be guaranteed.

## Diagnosing an empty collection

After an extension update, use **Reload** for this extension in
`edge://extensions`. Return to the signed-in `https://x.com/Ravens` tab and press
**Start**, then wait for collection to finish (normally roughly ten seconds).
Reopen the popup if needed and press **Copy diagnostics**, then paste the result
into your reply. The button copies the currently displayed status, fixed error,
extraction counts, and refresh lifecycle as a single JSON snapshot. It does not
copy posts, credentials, or tab identifiers and needs no added permission.
Copy success/failure feedback remains visible across status refreshes.
Do not share page HTML, cookies, account information,
or browser logs.

Diagnostics are fixed category names with bounded integer counts, kept only in
extension session status and never sent to the native host. Stop/new Start clears
them. Article/selector counts are cumulative observations across up to eight reads,
not unique posts. `own_timestamp_links` counts exact Ravens timestamp links before
quote filtering; `quote_time_rejections` and `missing_outer_time` reveal quote
classification. `other_author_or_link` counts rejected first outer links;
`social_rejections` covers repost or unrecognized social context. Other counters
separate incomplete/empty content, bad dates, old/future posts, and accepted/sent
posts. These diagnostics do not prove the live timeline is fixed.

For stale feeds, also copy **Refresh lifecycle (local timing only)** without
pressing Start again first. It reports the current phase, alarm presence and
next scheduled time, alarms received/skipped, cycle completions, page progress,
worker resumes, and ages of the current operation and last host-confirmed save.
These are local counters/times, not URLs, account information, or page content.
Status stays readable even when a native write is awaiting its response.
`last_alarm_lag_seconds` measures actual delivery against the alarm's scheduled
time. `last_cycle_duration_seconds` includes loading, extraction, and the native
response; `last_native_duration_seconds` isolates the native response wait.
`last_reload_to_save_seconds` measures reload through save confirmation, and
`last_save_gap_seconds` compares consecutive host-confirmed feed timestamps.
These completed-cycle measurements are not estimates based on popup opening time.
The live `busy_seconds` and phase identify a cycle that has not completed.

After reloading an updated extension, press Start once and leave the Ravens tab
open. Check the popup after at least two minutes: `alarms_seen` and
`cycles_completed` should increase and the last-updated timestamp should advance.
Alarm delivery may be delayed by Edge/device sleep; scheduled time is not a
guarantee. An explicitly started session repairs a missing alarm when its worker
wakes; a browser restart still requires Start.

Each page uses one settling timer rather than a chain that background-tab timer
throttling can multiply. An extraction has a 15-second per-page and 30-second
overall deadline; a suspended/stalled page stops collection and invalidates the
feed instead of remaining silently busy. This does not disable or bypass Edge's
resource management.
The normal target is a one-minute alarm with roughly eight seconds of page
settling, not a guaranteed one-minute delivery SLA. A late alarm, suspended page,
or slow native response must be diagnosed separately. The feed's 120-second
freshness limit is not extended by this extension.

## Local protocol

The extension sends `chrome.runtime.sendNativeMessage` to
`com.thecastle.ravens_feed` with:

```json
{"version":1,"posts":[{"url":"https://x.com/Ravens/status/123","text":"Public post text","created_at":"2026-10-13T12:00:00Z","image_urls":["https://pbs.twimg.com/media/example.jpg"]}]}
```

Only posts extracted during the current pass and dated within seven days are
sent, deduplicated and newest-first, at most 300. The host validates and retains
recent posts, stamps the feed, and returns
`{"ok":true,"count":1,"fetched_at":"2026-10-13T12:01:00Z"}` or
`{"ok":false,"error":"human-safe explanation"}`. Stop/error sends
`{"version":1,"error":"collector_unavailable"}` and expects `{"ok":true}`.
Native errors are shown as a fixed safe message, not arbitrary host output.
Each post contains exactly those four fields, with timezone-aware timestamps
not in the future, text limited to 25,000 characters, and at most four public
media images. Overlong text is skipped with an explicit warning, never cut short.
Native writes are serialized through their responses, including Stop/error
invalidation, so an earlier in-flight snapshot finishes before it is invalidated.

No cookies, credentials, HTML, quoted text/images, reposts, external network
requests, page-message bridge, sign-in automation, or challenge bypass is used.
Only `activeTab`, `scripting`, `alarms`, `storage`, and `nativeMessaging`
permissions are requested, with no host permissions.

## Limitations

X's DOM can change. A visible signed-in account marker is required. Unknown social
context is conservatively excluded; pinned posts are accepted when X exposes its
pin marker. Truncated posts are skipped with a popup warning, never expanded or
reported as complete. This is a bounded rendered-timeline reader, not a guarantee
that every announcement is captured. Sleeping devices, suspended/background tabs,
browser alarm throttling, and page loading can delay updates. Errors require
manual attention and Start; other feeds remain the fallback.

Browser tests (optional Playwright/Chromium, no live X access):

```powershell
.\.x-browser-venv\Scripts\python.exe -m unittest discover -s tests -p test_edge_extension.py -v
```

Opt-in real MV3 integration test (about 150 seconds):

```powershell
$env:EDGE_EXTENSION_REAL_BROWSER_TEST = "1"
.\.x-browser-venv\Scripts\python.exe -m unittest discover -s tests -p test_edge_extension.py -k RealMV3Tests -v
Remove-Item Env:\EDGE_EXTENSION_REAL_BROWSER_TEST
```

This launches a new disposable, offline **test Chromium** profile under this
directory and deletes it afterward. It never opens or attaches to the user's Edge
profile. The test copies the unchanged permission manifest, simulates a genuine
toolbar action in that isolated browser, and exercises real `activeTab`, alarms,
tab reloads, script injection, session storage, and worker termination/recovery.
Only the native transport is replaced with an in-browser acknowledgement fixture;
HTTP(S) pages are fulfilled locally. No native registration is changed. It checks
two real one-minute alarms and three completed saves. Headless Chromium may report
the target page as visible despite another tab being selected; this test does not
claim to reproduce Edge background/sleep throttling or a real native-host delay.
