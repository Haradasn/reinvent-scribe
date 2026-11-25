// Relays the button press in re:Invent Scribe to the background worker.
if (document.querySelector('meta[name="application-name"][content="reinvent-scribe"]')) {
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.data?.type !== "scribe-sync") return;
    window.postMessage({ type: "scribe-sync-ack" }, location.origin);
    const reply = (r) => window.postMessage({ type: "scribe-sync-result", ...r }, location.origin);
    chrome.runtime.sendMessage({ type: "sync", known: ev.data.known })
      .then((r) => reply(r || { error: "拡張機能から応答がありません" }))
      .catch((e) => reply({ error: String(e.message || e) }));
  });
}
