# Dedicated Edge profile collector

1. Create a dedicated Edge UI profile named **1 Winning Pod**. The extension cannot
   inspect profile names: installing and using it only in that profile is your
   responsibility. Browser extension APIs are isolated to the profile where it is
   installed; incognito use is disabled.
2. From the repository checkout, run `.\tools\install-edge-extension.ps1` to copy
   the extension to `%LOCALAPPDATA%\The-Castle\edge-extension`, outside any session
   worktree. In that profile, open `edge://extensions`, enable Developer mode,
   choose **Load unpacked**, and select that permanent directory.
   Give the extension ID to the native-host setup process so it can register the
   allowed extension origin.
3. Install/register the local native host `com.thecastle.ravens_feed` before Start.
   From the repository checkout, run
   `python -m ravens_bot.x_native_host --register <extension ID> --output "$env:LOCALAPPDATA\The-Castle\x-feed"`, replacing the
   placeholder with the ID shown in `edge://extensions`. This extension does not
   install the host or select its output path.
4. Manually sign in at X, open exactly `https://x.com/Ravens` (case-insensitive,
   optional trailing slash/query), then use the extension popup's **Start** button.
   Keep this dedicated window open. No remote browser debugging is needed.
5. Check the popup for the last host-confirmed update, count, and errors. **Pause**
   disables the alarm, forgets the tab, and invalidates the native feed. Use it for
   maintenance or to intentionally leave collection off; reinstalling is unnecessary.

## Permanent installation and updates

The loaded extension folder must survive Copilot session/worktree cleanup. The
installer copies the runtime files and this guide to the same per-user path from
any checkout; it verifies file hashes and does not alter the native host, feed,
or browser profile. It does not automatically download future changes.

To migrate an extension loaded from a worktree, first **Stop/Pause** the old copy
and disable it in `edge://extensions`. Run the installer, then **Load unpacked**
from `%LOCALAPPDATA%\The-Castle\edge-extension`. Moving folders changes the
unpacked extension ID and creates separate extension storage: register the new
ID with the native host, retain the same feed output directory, and Start on the
Ravens page. Keep only one collector enabled. Copy any old diagnostics before
removing the old extension; its history does not migrate automatically.

For future updates, Pause, run `.\tools\install-edge-extension.ps1` from the
checkout containing the desired version, and Reload the permanent copy in Edge.
Check the displayed version, approve any added permissions, and Start / Resume.
The path and extension ID stay stable, so native-host registration is unchanged
unless the host itself, Python, or feed output location changes. Reload alone
does not copy updates from a different checkout.

Auto-refresh is **off until the first Start**. Start/Pause intent is kept in
`storage.local` and survives extension reloads, updates, and Edge restarts. Runtime
tab/generation/status remain in `storage.session`. The extension requests
`https://x.com/*` host permission so it can recover without another toolbar click;
every collection still validates the exact Ravens profile URL. Approve the new
permission when updating to **1.1.0**.

Every minute it validates and reloads only the tracked Ravens tab, waits for loading,
then reads up to eight bounded scroll positions. Transient load, extraction, and
native-host failures invalidate the feed and retry after 1, 2, 4, 8, then at most
15 minutes between attempts. Success resets the delay. Closing/navigating away from
the tracked tab schedules recovery: an existing approved Ravens tab is reused or
one is opened inactive. The extension never reloads an unrelated tab or selects
the recovered tab. Keep the dedicated Edge profile open; collection resumes when
Edge opens again, but cannot launch Edge or run while the browser/device is off.

Sign-in screens and X challenges require manual attention. The popup shows
`needs_attention`; the extension checks again on minute alarms without reloading
the page, automating sign-in, or bypassing challenges. Resolve them and return to
`https://x.com/Ravens`; collection resumes automatically. **Pause** persists across
restarts and prevents recovery until you explicitly Start again. If the native
host is unavailable, invalidation cannot be guaranteed; the existing freshness
limit still protects consumers.

The popup's **Last updated** and `ravens.json`'s `fetched_at` advance on every
successful collection, even when no post is new. They do not advance when reading
fails or no recent complete posts can be extracted. For manager-only alerts,
enable the `ravensXFeed` monitor in the live **ampx1-watchdog** configuration,
pointed at the same public file. Its scheduled checks, failure grace period,
private notification settings, and delivery state determine alert timing.
Installing/merging that integration does not enable it. No health alert is sent
to the bot's Discord announcement channels or by this extension.

Collection holds a single native connection open, reusing the Python writer
across refreshes rather than starting a console process each minute. It never
focuses a window or selects a tab. Pause/error closes the connection after the
invalidation response; a browser/worker restart or disconnected host may need
a new process. To apply the persistent-writer change, Pause, rerun native-host registration, reload
the extension, and Start. Updating only the extension leaves an old one-shot
host launching on each refresh. This change removes those repeated launches;
it cannot rule out browser-rendering or graphics-driver flicker.

## Diagnosing an empty collection

Increment `manifest.json`'s extension version for every shipped extension update,
including fixes, so the installed revision can be identified in `edge://extensions`.
Use patch increments for fixes and minor increments for new features. Deploy the
updated manifest with the changed extension files; Edge shows the new version
after Reload.

After an extension update, use **Reload** for this extension in
`edge://extensions`. Return to the signed-in `https://x.com/Ravens` tab and press
**Start**, then wait for collection to finish (normally roughly ten seconds).
Reopen the popup if needed and press **Copy diagnostics**, then paste the result
into your reply. The button copies the currently displayed status, fixed error,
extraction counts, refresh lifecycle, and persistent event history as a single JSON snapshot. It does not
copy posts, credentials, or tab identifiers and needs no added permission.
Copy success/failure feedback remains visible across status refreshes.
Do not share page HTML, cookies, account information,
or browser logs.

Diagnostics are fixed category names with bounded integer counts, never sent to
the native host. Pause/new Start clears the current extraction counters, but not
the persistent history. Article/selector counts are cumulative observations across up to eight reads,
not unique posts. `own_timestamp_links` counts exact Ravens timestamp links before
quote filtering; `quote_time_rejections` and `missing_outer_time` reveal quote
classification. `other_author_or_link` counts rejected first outer links;
`social_rejections` covers repost or unrecognized social context. Other counters
separate incomplete/empty content, bad dates, old/future posts, and accepted/sent
posts. These diagnostics do not prove the live timeline is fixed.

For stale feeds, copy **Refresh lifecycle (local timing only)**. It reports the current phase, alarm presence and
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
wakes; a browser restart restores the saved Start/Pause intent.

Each page uses one settling timer rather than a chain that background-tab timer
throttling can multiply. An extraction has a 15-second per-page and 30-second
overall deadline; a suspended/stalled page fails that cycle, invalidates the
feed, and schedules a retry instead of remaining silently busy. This does not disable or bypass Edge's
resource management.
The normal target is a one-minute alarm with roughly eight seconds of page
settling, not a guaranteed one-minute delivery SLA. A late alarm, suspended page,
or slow native response must be diagnosed separately. The feed's 120-second
freshness limit is not extended by this extension.

## Persistent history and recovery

Version **1.1.0** adds **Persistent event history** to the popup and to **Copy
diagnostics**. It retains the last 200 start/pause, worker/browser resume,
tab-recovery, failure, invalidation-failure, and first-save/recovery events in
`storage.local`. The most recent failure is retained separately even after the
event buffer rolls over or collection succeeds. Entries include the extension
version, UTC timestamp, fixed reason code, phase at failure, retry time, last
successful save, operation durations, and allowlisted extraction counters.
No post text, URLs, tab/generation IDs, credentials, arbitrary exception text, or
native-host output is recorded. Logs stay local to the extension, are bounded,
and survive Start/Pause, worker restarts, Reload, and browser restarts. Uninstalling
the extension or clearing its storage removes them.

A native response has a ten-second deadline, including invalidation. If the host
hangs, its connection is closed and the control queue becomes available for
Pause/recovery rather than waiting forever. Recovery reconnects to the host.
The popup remains readable during a pending response.

Older versions only kept a current session snapshot, not a durable event log.
Restarting collection could erase the original error; this update cannot
reconstruct incidents that occurred before it was installed. The native host
reports generic write/validation warnings to stderr, not a persistent log file.
Future incident diagnosis should begin with **Copy diagnostics**, even if collection
has already recovered.

## Local protocol

The extension opens `chrome.runtime.connectNative` to
`com.thecastle.ravens_feed` and sends framed messages over that port:

```json
{"version":1,"posts":[{"url":"https://x.com/Ravens/status/123","text":"Public post text","created_at":"2026-10-13T12:00:00Z","image_urls":["https://pbs.twimg.com/media/example.jpg"]}]}
```

Only posts extracted during the current pass and dated within seven days are
sent, deduplicated and newest-first, at most 300. The host validates and retains
recent posts, stamps the feed, and returns
`{"ok":true,"count":1,"fetched_at":"2026-10-13T12:01:00Z"}` or
`{"ok":false,"error":"human-safe explanation"}`. Pause/error sends
`{"version":1,"error":"collector_unavailable"}` and expects `{"ok":true}`.
Native errors are shown as a fixed safe message, not arbitrary host output.
Each post contains exactly those four fields, with timezone-aware timestamps
not in the future, text limited to 25,000 characters, and at most four public
media images. Overlong text is skipped with an explicit warning, never cut short.
Native writes are serialized through their responses or deadlines, including Pause/error
invalidation, so an earlier in-flight snapshot finishes before it is invalidated.
The host reads successive frames until the port closes. Invalid message payloads
invalidate the feed and return an error; malformed framing also ends the
connection. A clean disconnect leaves the last snapshot to expire under the
existing freshness limit if no explicit Pause/error was received.

No cookies, credentials, HTML, quoted text/images, reposts, external network
requests, page-message bridge, sign-in automation, or challenge bypass is used.
The extension requests `activeTab`, `scripting`, `alarms`, `storage`, and
`nativeMessaging`, plus host permission only for `https://x.com/*`. Browser host
permissions apply to the origin, not a single profile path; the worker and
extractor both restrict collection to the Ravens profile URL.

## Limitations

X's DOM can change. A visible signed-in account marker is required. Unknown social
context is conservatively excluded; pinned posts are accepted when X exposes its
pin marker. Truncated posts are skipped with a popup warning, never expanded or
reported as complete. This is a bounded rendered-timeline reader, not a guarantee
that every announcement is captured. Sleeping devices, suspended/background tabs,
browser alarm throttling, and page loading can delay updates. Recovery is
best-effort, not an always-on service outside Edge. Sign-in/challenges require
manual attention; permanent DOM, registration, permission, or alarm errors may
require repair. The popup reports errors and retry timing; other feeds remain
the fallback.

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
toolbar action in that isolated browser, and exercises real X host permission, alarms,
tab reloads, script injection, session storage, and worker termination/recovery.
Only the native transport is replaced with an in-browser acknowledgement fixture;
HTTP(S) pages are fulfilled locally. No native registration is changed. It checks
two real one-minute alarms and three completed saves. Headless Chromium may report
the target page as visible despite another tab being selected; this test does not
claim to reproduce Edge background/sleep throttling or a real native-host delay.
