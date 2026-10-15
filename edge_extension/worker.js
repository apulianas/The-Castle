importScripts("collector.js");

const HOST = "com.thecastle.ravens_feed";
const ALARM = "ravens-refresh";
const KEY = "ravensCollector";
const SETTINGS = "ravensCollectorSettings";
const HISTORY = "ravensCollectorHistory";
const NATIVE_TIMEOUT_MS = 10000;
const LIFECYCLE_EMPTY = {
    phase: "off", worker_resumes: 0, alarms_seen: 0, alarms_skipped_busy: 0,
    alarm_repairs: 0, cycles_started: 0, cycles_completed: 0, pages_completed: 0,
    last_alarm_at: null, last_reload_at: null, last_cycle_started_at: null,
    last_cycle_completed_at: null, last_progress_at: null, native_write_started_at: null,
    last_alarm_scheduled_at: null, last_alarm_lag_seconds: null, last_cycle_duration_seconds: null,
    last_native_duration_seconds: null, last_reload_to_save_seconds: null, last_save_gap_seconds: null
};
const EMPTY = {
    enabled: false, tabId: null, generation: null, busy: null,
    lastUpdated: null, count: 0, error: null, diagnostics: null, lifecycle: null,
    failures: 0, retryAt: null, attention: false
};
function lifecycle(state, updates = {}) {
    return {...state, lifecycle: {...LIFECYCLE_EMPTY, ...state.lifecycle, ...updates}};
}
function increment(value) {
    return Math.min((Number.isSafeInteger(value) && value >= 0 ? value : 0) + 1, 99999);
}
function elapsedSeconds(start, end) {
    const begin = typeof start === "number" ? start : Date.parse(start);
    const finish = typeof end === "number" ? end : Date.parse(end);
    const elapsed = finish - begin;
    return Number.isFinite(elapsed) ? Math.min(999999, Math.max(0, Math.floor(elapsed / 1000))) : null;
}
const DIAGNOSTIC_KEYS = [
    "pages", "articles_seen", "nested_articles", "time_nodes", "own_timestamp_links",
    "quote_time_rejections", "missing_outer_time", "other_author_or_link", "social_rejections",
    "quote_text_skipped", "quote_images_skipped", "truncated", "oversized", "bad_date",
    "empty_content", "accepted", "unique_posts", "older_than_seven_days", "future_dates", "sent_posts"
];
function safeDiagnostics(value) {
    return Object.fromEntries(DIAGNOSTIC_KEYS.map(key => [key,
        Number.isSafeInteger(value?.[key]) && value[key] >= 0 ? Math.min(value[key], 99999) : 0]));
}
const ERRORS = {
    page_changed: "The tracked tab left https://x.com/Ravens. A Ravens tab will be recovered automatically.",
    sign_in_required: "Sign in manually to X in the dedicated Edge profile and return to the Ravens page. Collection will resume automatically.",
    challenge: "X needs your attention. Resolve it manually and return to the Ravens page. Collection will resume automatically.",
    extraction_failed: "The Ravens timeline could not be read. Collection will retry automatically.",
    truncated: "A post was truncated (Show more). It was not sent. Check X manually.",
    oversized: "A post exceeded the 25,000-character feed limit and was not sent. Check X manually.",
    no_posts: "No recent, complete original Ravens posts were visible. Collection will retry automatically.",
    native_failed: "The native feed host is unavailable, not yet registered, or rejected the update. Load this unpacked extension in Edge and give its extension ID to the native-host setup process.",
    native_timeout: "The native feed host did not respond within ten seconds. Collection will reconnect and retry.",
    tab_closed: "The tracked Ravens tab was closed. A Ravens tab will be reopened automatically.",
    tab_recovery_failed: "A Ravens tab could not be recovered. Check the extension's X site permission and keep the dedicated Edge profile open. Collection will retry.",
    reload_failed: "The Ravens tab could not be reloaded. Collection will retry automatically.",
    load_failed: "The Ravens tab did not finish loading. Collection will retry automatically.",
    extraction_timeout: "The Ravens tab's read stalled or was suspended. Collection will retry automatically.",
    alarm_failed: "The refresh alarm is unavailable. Reload the extension and press Start again."
};
let queue = Promise.resolve();
const running = new Set();
function exclusive(action) {
    // Keep the lock through each native response: Stop/error invalidation must be the last write.
    const result = queue.then(action);
    queue = result.catch(() => {});
    return result;
}
async function readState() {
    return (await chrome.storage.session.get(KEY))[KEY] || {...EMPTY};
}
async function saveState(state) {
    await chrome.storage.session.set({[KEY]: state});
}
async function recordEvent(event, state, code = null) {
    const history = (await chrome.storage.local.get(HISTORY))[HISTORY] || {events: [], last_failure: null};
    const entry = {
        at: new Date().toISOString(), version: chrome.runtime.getManifest().version, event, code,
        phase: state.lifecycle?.phase || "off", failures: state.failures || 0,
        retry_at: state.retryAt, last_updated: state.lastUpdated,
        busy_seconds: state.busy ? elapsedSeconds(state.busy, Date.now()) : null,
        cycle_seconds: elapsedSeconds(state.lifecycle?.last_cycle_started_at, Date.now()),
        native_seconds: elapsedSeconds(state.lifecycle?.native_write_started_at, Date.now()),
        diagnostics: state.diagnostics ? safeDiagnostics(state.diagnostics) : null
    };
    await chrome.storage.local.set({[HISTORY]: {
        events: [...history.events, entry].slice(-200),
        last_failure: event === "failure" ? entry : history.last_failure
    }});
}
async function readStatus() {
    const state = await readState();
    const history = (await chrome.storage.local.get(HISTORY))[HISTORY] || {events: [], last_failure: null};
    let alarm;
    let alarmReadFailed = false;
    try { alarm = await chrome.alarms.get(ALARM); }
    catch { alarmReadFailed = true; }
    const secondsSince = value => elapsedSeconds(value, Date.now());
    return {...state, history, scheduler: {
        alarm_present: !!alarm, alarm_read_failed: alarmReadFailed,
        next_alarm_at: Number.isFinite(alarm?.scheduledTime)
            ? new Date(alarm.scheduledTime).toISOString() : null,
        period_minutes: Number.isFinite(alarm?.periodInMinutes) ? alarm.periodInMinutes : null,
        worker_operation_active: running.has(state.generation),
        busy_seconds: state.busy ? secondsSince(state.busy) : null,
        last_update_age_seconds: state.lastUpdated ? secondsSince(state.lastUpdated) : null
    }};
}
async function ensureAlarm() {
    const alarm = await chrome.alarms.get(ALARM);
    if (alarm?.periodInMinutes === 1) return false;
    await chrome.alarms.create(ALARM, {delayInMinutes: 1, periodInMinutes: 1});
    return true;
}
function approvedUrl(value) {
    try {
        const url = new URL(value);
        return url.protocol === "https:" && url.hostname === "x.com" &&
            !url.port && !url.username && !url.password && !url.hash &&
            /^\/ravens\/?$/i.test(url.pathname);
    } catch { return false; }
}
function approvedTab(tab) {
    return tab && !tab.incognito && (approvedUrl(tab.url) ||
        (!tab.url && tab.status === "loading" && approvedUrl(tab.pendingUrl))) &&
        (!tab.pendingUrl || approvedUrl(tab.pendingUrl));
}
function attentionCode(tab) {
    try {
        const url = new URL(tab.pendingUrl || tab.url);
        if (url.origin !== "https://x.com") return null;
        if (/^\/i\/flow\/login\/?$/.test(url.pathname) || /^\/login\/?$/.test(url.pathname)) return "sign_in_required";
        if (/^\/account\/access\/?$/.test(url.pathname)) return "challenge";
    } catch { /* Missing tab URLs are handled as page changes. */ }
    return null;
}
async function recoverTab(state) {
    if (state.tabId !== null) {
        let tab;
        try { tab = await chrome.tabs.get(state.tabId); }
        catch { throw new Error("tab_closed"); }
        if (approvedTab(tab)) return {state, tab};
        throw new Error(attentionCode(tab) || "page_changed");
    }
    let tab;
    try {
        const tabs = await chrome.tabs.query({url: "https://x.com/*"});
        tab = tabs.find(approvedTab) || await chrome.tabs.create({url: "https://x.com/Ravens", active: false});
    } catch { throw new Error("tab_recovery_failed"); }
    if (!approvedTab(tab)) throw new Error("page_changed");
    state = {...state, tabId: tab.id};
    await saveState(state);
    await recordEvent("tab_recovered", state);
    return {state, tab};
}
let nativePort = null;
let nativePending = null;
function closeNativePort() {
    const port = nativePort;
    nativePort = null;
    if (port) port.disconnect();
}
function nativeMessage(payload) {
    // Calls are serialized by the control queue. Reuse one writer instead of
    // launching a Windows console process on every minute's refresh.
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            nativePending = null;
            closeNativePort();
            reject(new Error("native_timeout"));
        }, NATIVE_TIMEOUT_MS);
        const finish = callback => value => { clearTimeout(timer); callback(value); };
        try {
            if (!nativePort) {
                const port = chrome.runtime.connectNative(HOST);
                nativePort = port;
                port.onMessage.addListener(reply => {
                    if (nativePort !== port || !nativePending) return;
                    const pending = nativePending;
                    nativePending = null;
                    if (reply?.ok === true) pending.resolve(reply);
                    else pending.reject(new Error("native_failed"));
                });
                port.onDisconnect.addListener(() => {
                    // Consume Chrome's lastError without exposing host output.
                    void chrome.runtime.lastError;
                    if (nativePort !== port) return;
                    nativePort = null;
                    const pending = nativePending;
                    nativePending = null;
                    if (pending) pending.reject(new Error("native_failed"));
                });
            }
            nativePending = {resolve: finish(resolve), reject: finish(reject)};
            nativePort.postMessage(payload);
        } catch {
            clearTimeout(timer);
            nativePending = null;
            closeNativePort();
            reject(new Error("native_failed"));
        }
    });
}
// Called only in the control queue, so an invalidation follows any earlier native write.
async function disable(error = null, diagnostics = null) {
    const previous = await readState();
    await chrome.storage.local.set({[SETTINGS]: {enabled: false}});
    const state = lifecycle({...EMPTY, lastUpdated: previous.lastUpdated, count: previous.count, error,
        diagnostics: diagnostics ? safeDiagnostics(diagnostics) : null}, {
        ...previous.lifecycle, phase: "off", last_progress_at: new Date().toISOString()
    });
    await saveState(state);
    await recordEvent("paused", state);
    await chrome.alarms.clear(ALARM);
    try {
        await nativeMessage({version: 1, error: "collector_unavailable"});
    } catch (nativeError) {
        state.error = [error, ERRORS.native_failed + " The old feed could not be invalidated."]
            .filter(Boolean).join(" ");
        await saveState(state);
        await recordEvent("failure", state, nativeError.message === "native_timeout" ? "native_timeout" : "native_failed");
    } finally {
        closeNativePort();
    }
    return state;
}
async function checkedState(generation) {
    const state = await readState();
    if (!state.enabled || state.generation !== generation) return null;
    let tab;
    try { tab = await chrome.tabs.get(state.tabId); }
    catch { throw new Error("tab_closed"); }
    if (!approvedTab(tab)) throw new Error(attentionCode(tab) || "page_changed");
    return {state, tab};
}
// The caller holds the queue; keep the user's intent enabled even when a cycle fails.
async function retry(state, code, diagnostics = null) {
    code = Object.hasOwn(ERRORS, code) ? code : "extraction_failed";
    const failures = increment(state.failures);
    const attention = code === "sign_in_required" || code === "challenge";
    const delayMinutes = attention ? 1 : Math.min(15, 2 ** Math.min(failures - 1, 4));
    const next = lifecycle({...state, busy: null, generation: crypto.randomUUID(),
        tabId: code === "tab_closed" || code === "page_changed" ? null : state.tabId,
        error: ERRORS[code], diagnostics: diagnostics ? safeDiagnostics(diagnostics) : state.diagnostics,
        failures, attention, retryAt: new Date(Date.now() + delayMinutes * 60000).toISOString()
    }, {phase: attention ? "needs_attention" : "retry_wait", last_progress_at: new Date().toISOString()});
    await saveState(next);
    await recordEvent("failure", {...next, busy: state.busy, lifecycle: state.lifecycle}, code);
    try { await ensureAlarm(); }
    catch {
        next.error += " " + ERRORS.alarm_failed;
        await saveState(next);
        await recordEvent("failure", next, "alarm_failed");
    }
    try { await nativeMessage({version: 1, error: "collector_unavailable"}); }
    catch (error) {
        next.error += " The old feed could not be invalidated; it must expire under the freshness limit.";
        await saveState(next);
        await recordEvent("invalidation_failed", next, error.message === "native_timeout" ? "native_timeout" : "native_failed");
    } finally { closeNativePort(); }
    return next;
}
async function fail(generation, code, diagnostics = null) {
    return exclusive(async () => {
        const state = await readState();
        if (state.enabled && state.generation === generation) {
            await retry(state, code, diagnostics);
        }
    });
}
async function collect(generation) {
    if (running.has(generation)) return;
    running.add(generation);
    const diagnostics = safeDiagnostics(null);
    const startedAt = Date.now();
    try {
        const began = await exclusive(async () => {
            const current = await checkedState(generation);
            if (!current) return false;
            await saveState(lifecycle(current.state, {
                phase: "waiting_load", cycles_started: increment(current.state.lifecycle?.cycles_started),
                pages_completed: 0, last_cycle_started_at: new Date(startedAt).toISOString(),
                last_progress_at: new Date().toISOString()
            }));
            return true;
        });
        if (!began) return;
        // Wait for this navigation only. Each poll rechecks the tracked URL and generation.
        let loaded = false;
        for (let attempt = 0; attempt < 40; attempt++) {
            const current = await exclusive(() => checkedState(generation));
            if (!current) return;
            if (current.tab.status === "complete") { loaded = true; break; }
            await new Promise(resolve => setTimeout(resolve, 250));
        }
        if (!loaded) throw new Error("load_failed");
        const found = new Map();
        let truncated = 0;
        let oversized = 0;
        for (let page = 0; page < 8; page++) {
            const current = await exclusive(async () => {
                const checked = await checkedState(generation);
                if (checked) await saveState(lifecycle(checked.state, {
                    phase: "extracting", pages_completed: page, last_progress_at: new Date().toISOString()
                }));
                return checked;
            });
            if (!current) return;
            const remaining = 30000 - (Date.now() - startedAt);
            if (remaining <= 0) throw new Error("extraction_timeout");
            let timeout;
            let result;
            try {
                result = await Promise.race([
                    chrome.scripting.executeScript({
                        target: {tabId: current.state.tabId},
                        func: extractRavensPage,
                        args: [page > 0]
                    }),
                    new Promise((_, reject) => {
                        timeout = setTimeout(() => reject(new Error("extraction_timeout")),
                            Math.min(15000, remaining));
                    })
                ]);
            } finally { clearTimeout(timeout); }
            const batch = result?.[0]?.result;
            diagnostics.pages++;
            if (!batch || batch.error) throw new Error(batch?.error || "extraction_failed");
            const counts = safeDiagnostics(batch.diagnostics);
            for (const key of DIAGNOSTIC_KEYS) {
                if (key !== "pages") diagnostics[key] = Math.min(diagnostics[key] + counts[key], 99999);
            }
            truncated += batch.truncated || 0;
            oversized += batch.oversized || 0;
            for (const post of batch.posts) found.set(post.url, post);
        }
        const now = Date.now();
        diagnostics.unique_posts = Math.min(found.size, 99999);
        const posts = [...found.values()].filter(post => {
            const age = now - Date.parse(post.created_at);
            if (age < 0) diagnostics.future_dates++;
            if (age > 7 * 86400000) diagnostics.older_than_seven_days++;
            return age >= 0 && age <= 7 * 86400000;
        }).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) ||
            a.url.localeCompare(b.url)).slice(0, 300);
        if (!posts.length) throw new Error(truncated ? "truncated" : oversized ? "oversized" : "no_posts");
        await exclusive(async () => {
            const current = await checkedState(generation);
            if (!current) return;
            await saveState(lifecycle(current.state, {
                phase: "sending", pages_completed: diagnostics.pages,
                native_write_started_at: new Date().toISOString(), last_progress_at: new Date().toISOString()
            }));
            const reply = await nativeMessage({version: 1, posts});
            if (!Number.isInteger(reply.count) || reply.count < 0 ||
                typeof reply.fetched_at !== "string" ||
                !Number.isFinite(Date.parse(reply.fetched_at))) throw new Error("native_failed");
            diagnostics.sent_posts = posts.length;
            const sending = await readState();
            const completedAt = Date.now();
            const completed = lifecycle({...sending, busy: null, lastUpdated: reply.fetched_at,
                failures: 0, retryAt: null, attention: false,
                diagnostics: safeDiagnostics(diagnostics),
                count: reply.count, error: [truncated ? ERRORS.truncated : null,
                    oversized ? ERRORS.oversized : null].filter(Boolean).join(" ") || null}, {
                phase: "waiting_alarm", cycles_completed: increment(sending.lifecycle?.cycles_completed),
                last_cycle_completed_at: new Date(completedAt).toISOString(),
                last_progress_at: new Date(completedAt).toISOString(),
                last_cycle_duration_seconds: elapsedSeconds(startedAt, completedAt),
                last_native_duration_seconds: elapsedSeconds(sending.lifecycle?.native_write_started_at, completedAt),
                last_reload_to_save_seconds: elapsedSeconds(sending.lifecycle?.last_reload_at, completedAt),
                last_save_gap_seconds: elapsedSeconds(sending.lastUpdated, reply.fetched_at)
            });
            await saveState(completed);
            if (sending.failures || !sending.lastUpdated) await recordEvent(
                sending.failures ? "recovered" : "first_save", completed);
        });
    } catch (error) {
        await fail(generation, error.message, diagnostics);
    } finally {
        running.delete(generation);
    }
}
async function start() {
    return exclusive(async () => {
        const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
        if (!approvedTab(tab)) {
            return disable("Start only on https://x.com/Ravens in your dedicated '1 Winning Pod' Edge profile.");
        }
        // Starting a new generation invalidates any outstanding extraction before enabling it.
        const previous = await readState();
        await chrome.storage.local.set({[SETTINGS]: {enabled: true}});
        const state = lifecycle({...EMPTY, lastUpdated: previous.lastUpdated, count: previous.count,
            enabled: true, tabId: tab.id, generation: crypto.randomUUID(),
            busy: Date.now()}, {phase: "starting", last_progress_at: new Date().toISOString()});
        await saveState(state);
        await recordEvent("started", state);
        try {
            await ensureAlarm();
            await nativeMessage({version: 1, error: "collector_unavailable"});
        } catch (error) { return retry(state, Object.hasOwn(ERRORS, error.message) ? error.message : "alarm_failed"); }
        void collect(state.generation);
        return state;
    });
}
async function refresh(alarm) {
    let generation;
    try {
        await exclusive(async () => {
            let state = await readState();
            if (!state.enabled) { await chrome.alarms.clear(ALARM); return; }
            generation = state.generation;
            const receivedAt = Date.now();
            state = lifecycle(state, {
                alarms_seen: increment(state.lifecycle?.alarms_seen), last_alarm_at: new Date(receivedAt).toISOString(),
                last_alarm_scheduled_at: Number.isFinite(alarm?.scheduledTime)
                    ? new Date(alarm.scheduledTime).toISOString() : null,
                last_alarm_lag_seconds: elapsedSeconds(alarm?.scheduledTime, receivedAt)
            });
            await saveState(state);
            if (state.retryAt && Date.now() < Date.parse(state.retryAt)) return;
            if (state.attention && state.tabId !== null) {
                let tab;
                try { tab = await chrome.tabs.get(state.tabId); }
                catch { throw new Error("tab_closed"); }
                // Do not reload or replace a page while the user is signing in or solving a challenge.
                if (!approvedTab(tab)) {
                    if (attentionCode(tab)) return;
                    throw new Error("page_changed");
                }
            }
            const current = await recoverTab(state);
            // Persisted lease prevents duplicate reloads; a killed worker recovers next minute.
            if (running.has(generation) ||
                (current.state.busy && Date.now() - current.state.busy < 45000)) {
                await saveState(lifecycle(current.state, {
                    alarms_skipped_busy: increment(current.state.lifecycle?.alarms_skipped_busy)
                }));
                return;
            }
            await saveState(lifecycle({...current.state, busy: Date.now()}, {
                phase: state.attention ? "waiting_load" : "reloading",
                last_reload_at: state.attention ? null : new Date().toISOString(),
                last_progress_at: new Date().toISOString()
            }));
            if (!state.attention) {
                try { await chrome.tabs.reload(current.state.tabId); }
                catch { throw new Error("reload_failed"); }
            }
            void collect(generation);
        });
    } catch (error) {
        if (generation) await fail(generation, error.message);
    }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) {
        return false;
    }
    let operation;
    if (message?.action === "start") operation = start();
    else if (message?.action === "stop") operation = exclusive(() => disable());
    // Read-only status must remain available while a native response holds the write queue.
    else if (message?.action === "status") operation = readStatus();
    else return false;
    operation.then(sendResponse, () => sendResponse({...EMPTY, error: "Extension operation failed. Check extension storage and reload the extension."}));
    return true;
});
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === ALARM) void refresh(alarm);
});
chrome.tabs.onRemoved.addListener(tabId => {
    void exclusive(async () => {
        const state = await readState();
        if (state.enabled && state.tabId === tabId) await retry(state, "tab_closed");
    });
});
chrome.tabs.onUpdated.addListener(tabId => {
    void exclusive(async () => {
        const state = await readState();
        if (state.tabId !== tabId) return;
        try { await checkedState(state.generation); }
        catch (error) {
            if (state.attention && (error.message === "sign_in_required" || error.message === "challenge")) return;
            await retry(state, error.message);
        }
    });
});
// Top-level initialization also runs when startup/install events wake a fresh worker.
chrome.runtime.onStartup.addListener(() => {});
chrome.runtime.onInstalled.addListener(() => {});
void exclusive(async () => {
    let state = await readState();
    const settings = (await chrome.storage.local.get(SETTINGS))[SETTINGS];
    if (settings?.enabled === false && state.enabled) {
        await disable();
        return;
    }
    if (!settings && state.enabled) await chrome.storage.local.set({[SETTINGS]: {enabled: true}});
    const restoring = !state.enabled && settings?.enabled === true;
    if (restoring) {
        state = lifecycle({...EMPTY, enabled: true, generation: crypto.randomUUID()}, {phase: "starting"});
        await saveState(state);
    }
    if (!state.enabled) return;
    try {
        const repaired = await ensureAlarm();
        state = lifecycle(state, {
            worker_resumes: increment(state.lifecycle?.worker_resumes),
            alarm_repairs: repaired ? increment(state.lifecycle?.alarm_repairs) : state.lifecycle?.alarm_repairs || 0
        });
        await saveState(state);
        await recordEvent(restoring ? "browser_resumed" : "worker_resumed", state);
        if (restoring) void refresh();
    } catch (error) {
        await retry(state, "alarm_failed");
    }
});
