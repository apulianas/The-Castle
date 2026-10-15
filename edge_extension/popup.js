const startButton = document.getElementById("start");
const stopButton = document.getElementById("stop");
const copyButton = document.getElementById("copy-diagnostics");
let pending = false;
function setTextIfChanged(id, text) {
    const node = document.getElementById(id);
    if (node.textContent !== text) node.textContent = text;
}
async function update(action = "status") {
    if (pending) return;
    pending = true;
    startButton.disabled = stopButton.disabled = true;
    try {
        const state = await chrome.runtime.sendMessage({action});
        document.getElementById("status").textContent = state.enabled
            ? `On — ${state.lifecycle?.phase || (state.busy ? "collecting" : "waiting_alarm")}`
            : "Paused — auto-refresh disabled";
        document.getElementById("updated").textContent = "Last updated: " +
            (state.lastUpdated ? new Date(state.lastUpdated).toLocaleString() : "Never");
        document.getElementById("count").textContent = `Last feed count: ${state.count || 0}`;
        document.getElementById("error").textContent = state.error || "";
        document.getElementById("freshness").textContent =
            state.enabled && state.scheduler?.last_update_age_seconds > 120
                ? "Feed is stale: no host-confirmed update in more than 120 seconds. Copy the refresh lifecycle below."
                : "";
        setTextIfChanged("lifecycle", JSON.stringify({
            ...state.lifecycle, ...state.scheduler, retry_at: state.retryAt || null
        }, null, 2));
        setTextIfChanged("history", JSON.stringify(state.history || {events: [], last_failure: null}, null, 2));
        document.getElementById("diagnostics-panel").hidden = !state.diagnostics;
        setTextIfChanged("diagnostics", state.diagnostics
            ? JSON.stringify(state.diagnostics, null, 2) : "");
        startButton.disabled = !!state.enabled;
    } catch {
        document.getElementById("error").textContent = "Could not contact the extension. Reopen this popup.";
        startButton.disabled = false;
    } finally {
        stopButton.disabled = false;
        pending = false;
    }
}
startButton.addEventListener("click", () => update("start"));
stopButton.addEventListener("click", () => update("stop"));
copyButton.addEventListener("click", async () => {
    copyButton.disabled = true;
    const feedback = document.getElementById("copy-feedback");
    try {
        const text = id => document.getElementById(id).textContent;
        const counts = id => text(id) ? JSON.parse(text(id)) : null;
        const snapshot = {
            status: text("status"), last_updated: text("updated"), feed_count: text("count"),
            error: text("error"), freshness: text("freshness"),
            extraction_counts: counts("diagnostics"), refresh_lifecycle: counts("lifecycle"),
            persistent_history: counts("history")
        };
        await navigator.clipboard.writeText(JSON.stringify(snapshot, null, 2));
        feedback.textContent = "Diagnostics copied. Paste them into your reply.";
    } catch {
        feedback.textContent = "Copy failed. Keep this popup focused and try Copy diagnostics again.";
    } finally {
        copyButton.disabled = false;
    }
});
void update();
setInterval(() => { void update(); }, 1000);
