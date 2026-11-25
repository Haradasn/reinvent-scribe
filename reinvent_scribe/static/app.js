const $ = (id) => document.getElementById(id);

const state = {
  recording: false,
  current: null, // {dirname, date}
  startedAt: null,
  timerId: null,
  selected: null, // chat target {dirname, date}
  viewId: 0, // bumped on clear; late replies for an older view are dropped
  viewing: false, // showing a saved recording
  cleared: false, // ignore late captions after the user cleared the screen
};

// Local date, not UTC: the server names day folders by local time
const today = () => {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

/* ---------- status chips ---------- */
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("chip-model").textContent = `model: ${s.model}`;
    setChip("chip-llm", `LLM: ${s.llm_ok ? "接続可" : "未設定"}`, s.llm_ok);
    setChip("chip-mic", `mic: ${s.mic_ok ? "OK" : "NG"}`, s.mic_ok);
    const badge = $("summary-badge");
    badge.textContent = s.llm_ok ? "オンライン" : "オフライン(字幕のみ)";
    badge.className = `badge ${s.llm_ok ? "online" : "offline"}`;
    syncRecordingState(s);
  } catch {}
}

// Restore recording UI after a page reload while a session is running
function syncRecordingState(s) {
  if (s.recording && s.session && !state.recording) {
    state.recording = true;
    state.current = { dirname: s.session.dirname, date: s.session.date };
    state.selected = state.current;
    state.startedAt = Date.parse(s.session.meta.started_at);
    clearInterval(state.timerId);
    state.timerId = setInterval(tick, 1000);
    state.paused = !!s.paused;
    if (state.paused) { clearInterval(state.timerId); state.pausedAt = Date.now(); }
    setRecorderUI(state.paused ? "paused" : "recording");
    $("rec-title").textContent = `[${s.session.meta.id}] ${s.session.meta.title}`;
  } else if (!s.recording && state.recording) {
    state.recording = false;
    state.paused = false;
    clearInterval(state.timerId);
    setRecorderUI("idle");
    $("rec-title").textContent = "待機中";
  }
}
function setChip(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = `chip ${ok ? "ok" : "ng"}`;
}

/* ---------- waveform (visual pulse while recording) ---------- */
const canvas = $("waveform");
const ctx = canvas.getContext("2d");
let wavePhase = 0;
function drawWave() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (state.recording && !state.paused) {
    ctx.beginPath();
    for (let x = 0; x < canvas.width; x++) {
      const t = x / canvas.width;
      const env = Math.sin(Math.PI * t);
      const y = canvas.height / 2 +
        Math.sin(t * 18 + wavePhase) * env * (8 + Math.sin(wavePhase * 1.7) * 6);
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    const grad = ctx.createLinearGradient(0, 0, canvas.width, 0);
    grad.addColorStop(0, "#7c6cff");
    grad.addColorStop(1, "#38d4ff");
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2;
    ctx.stroke();
    wavePhase += 0.12;
  }
  requestAnimationFrame(drawWave);
}
drawWave();

/* ---------- timer ---------- */
function tick() {
  const sec = Math.floor((Date.now() - state.startedAt) / 1000);
  const h = String(Math.floor(sec / 3600)).padStart(2, "0");
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  $("rec-timer").textContent = `${h}:${m}:${s}`;
}

/* ---------- websocket ---------- */
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if ((state.cleared || state.viewing) && ["segment", "partial", "jp", "summary"].includes(msg.type)) return;
    if (msg.type === "segment") { clearPartial(); addCaption(msg.ts, msg.text); }
    if (msg.type === "partial") showPartial(msg.text);
    if (msg.type === "jp") addJapanese(msg.ts, msg.text);
    if (msg.type === "stt_fallback") { clearPartial(); addSummaryNote("ストリーミング接続が切れたため、ローカル文字起こしに切り替えました"); }
    if (msg.type === "summary") addSummary(msg.text);
    if (msg.type === "hq_done") loadSessions();
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
}
connectWS();

function fmtTs(sec) {
  const s = Math.floor(sec);
  return [s / 3600, (s % 3600) / 60, s % 60]
    .map((v) => String(Math.floor(v)).padStart(2, "0")).join(":");
}

function addCaption(ts, text) {
  const box = $("captions");
  box.querySelector(".captions-empty")?.remove();
  const div = document.createElement("div");
  div.className = "caption";
  div.dataset.ts = String(ts);
  div.innerHTML = `<span class="ts">${fmtTs(ts)}</span>${escapeHtml(text)}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function addJapanese(ts, text) {
  const box = $("captions");
  const caption = [...box.querySelectorAll(`.caption[data-ts="${ts}"]`)]
    .find((c) => !c.querySelector(".jp-line"));
  const jp = document.createElement("div");
  jp.className = "jp-line";
  jp.textContent = text;
  if (caption) caption.appendChild(jp);
  else {
    const div = document.createElement("div");
    div.className = "caption";
    div.appendChild(jp);
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
}

function addSummary(text, label) {
  const box = $("summaries");
  // each summary is cumulative, so the newest replaces the previous one
  box.replaceChildren();
  const card = document.createElement("div");
  card.className = "summary-card";
  const when = new Date().toTimeString().slice(0, 5);
  card.innerHTML = `<span class="when">${label || `${when} 時点`}</span>${escapeHtml(text)}`;
  box.prepend(card);
}

function showPartial(text) {
  const box = $("captions");
  box.querySelector(".captions-empty")?.remove();
  let el = box.querySelector(".caption.partial");
  if (!el) {
    el = document.createElement("div");
    el.className = "caption partial";
    box.appendChild(el);
  }
  el.textContent = text;
  box.scrollTop = box.scrollHeight;
}

function clearPartial() {
  $("captions").querySelector(".caption.partial")?.remove();
}

function addSummaryNote(text) {
  const note = document.createElement("div");
  note.className = "captions-empty";
  note.textContent = text;
  $("captions").appendChild(note);
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/* ---------- record flow ---------- */
// Main button: idle=start, recording=pause, paused=resume. Stop is its own button.
function setRecorderUI(mode) {
  // mode: "idle" | "recording" | "paused"
  $("btn-record").dataset.state = mode;
  $("btn-record").classList.toggle("recording", mode === "recording");
  $("btn-record").title = { idle: "録音開始", recording: "一時停止", paused: "再開" }[mode];
  $("btn-stop").classList.toggle("hidden", mode === "idle");
  $("rec-timer").classList.toggle("paused", mode === "paused");
  $("btn-clear").classList.toggle("hidden", mode !== "idle" || !hasContent());
}

/* ---------- clear screen (files on disk are untouched) ---------- */
function hasContent() {
  return !!document.querySelector(".caption, .summary-card, .chat-msg");
}

function setPlaceholder(id, text) {
  const div = document.createElement("div");
  div.className = "captions-empty";
  div.textContent = text;
  $(id).replaceChildren(div);
}

function clearScreen() {
  state.viewId++;
  setPlaceholder("captions", "録音を開始すると、ここに英語字幕が流れます");
  setPlaceholder("summaries", "オンライン時、定期的に要約が更新されます");
  $("chat-log").replaceChildren();
  $("chat-input").value = "";
  $("chat-input").placeholder = "文字起こしについて質問する…";
  $("rec-timer").textContent = "00:00:00";
  state.current = null;
  state.selected = null;
  state.viewing = false;
  $("rec-title").textContent = "待機中";
  $("chat-target").textContent = "";
  markSelected();
  $("btn-clear").classList.add("hidden");
}

$("btn-clear").addEventListener("click", () => {
  state.cleared = true;
  clearScreen();
});

$("btn-record").addEventListener("click", async () => {
  if (state.recording && !state.paused) {
    const r = await fetch("/api/sessions/pause", { method: "POST" });
    if (!r.ok) return;
    state.paused = true;
    state.pausedAt = Date.now();
    clearInterval(state.timerId);
    setRecorderUI("paused");
    return;
  }
  if (state.recording && state.paused) {
    const r = await fetch("/api/sessions/resume", { method: "POST" });
    if (!r.ok) { alert("再開に失敗しました"); return; }
    state.paused = false;
    state.startedAt += Date.now() - state.pausedAt;
    state.timerId = setInterval(tick, 1000);
    setRecorderUI("recording");
    return;
  }
  // prefill from schedule
  try {
    const r = await (await fetch("/api/schedule")).json();
    const sug = r.suggestions[0];
    const box = $("suggestion-box");
    if (sug) {
      box.classList.remove("hidden");
      $("btn-suggest").textContent = `▶ 今のセッションを開始: [${sug.session_id}] ${sug.title}`;
      $("btn-suggest").onclick = () => {
        $("start-modal").close();
        startRecording(sug.session_id, sug.title, "");
      };
      $("f-id").value = sug.session_id;
      $("f-title").value = sug.title;
    } else {
      box.classList.add("hidden");
    }
  } catch {}
  $("start-modal").showModal();
});

$("btn-cancel").addEventListener("click", () => $("start-modal").close());

$("start-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("start-modal").close();
  startRecording($("f-id").value.trim(), $("f-title").value.trim(), $("f-memo").value.trim());
});

async function startRecording(id, title, memo) {
  const r = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: id, title, memo }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert(err.error || "開始に失敗しました");
    refreshStatus();
    return;
  }
  const data = await r.json();
  clearScreen();
  state.cleared = false;
  state.recording = true;
  state.paused = false;
  state.current = { dirname: data.dirname, date: data.date };
  state.selected = state.current;
  state.startedAt = Date.now();
  state.timerId = setInterval(tick, 1000);
  setRecorderUI("recording");
  $("rec-title").textContent = `[${id}] ${title}`;
  $("chat-target").textContent = `[${id}] ${title}`;
  $("list-date").value = today();
  loadSessions();
}

$("btn-stop").addEventListener("click", () => stopRecording());

async function stopRecording() {
  const r = await fetch("/api/sessions/stop", { method: "POST" });
  if (!r.ok && r.status !== 409) {
    alert("停止に失敗しました");
    return;
  }
  state.recording = false;
  state.paused = false;
  clearInterval(state.timerId);
  setRecorderUI("idle");
  $("rec-title").textContent = "待機中";
  const note = document.createElement("div");
  note.className = "captions-empty";
  note.textContent = "停止しました。高精度文字起こしをバックグラウンドで実行中 — 完了すると一覧に HQ✓ が付きます";
  $("captions").appendChild(note);
  $("list-date").value = today();
  loadSessions();
}

/* ---------- sessions ---------- */
async function loadSessions() {
  const r = await (await fetch(`/api/sessions?date=${$("list-date").value || today()}`)).json();
  const list = $("session-list");
  list.innerHTML = "";
  for (const s of r.sessions) {
    const li = document.createElement("li");
    const hq = s.hq
      ? '<span class="hq-tag">HQ✓</span>'
      : '<span class="hq-wait">HQ処理中…</span>';
    li.innerHTML =
      `<span class="sid">${escapeHtml(s.meta.id)}</span>` +
      `<span>${escapeHtml(s.meta.title)}</span>` +
      (s.meta.ended_at ? hq : '<span class="hq-wait">録音中</span>') +
      `<button class="memo-btn" title="メモ編集">✎</button>` +
      `<button class="memo-btn del-btn" title="削除">🗑</button>`;
    li.querySelector(".memo-btn").onclick = () => openMemo(s);
    li.querySelector(".del-btn").onclick = async () => {
      if (!confirm(`[${s.meta.id}] ${s.meta.title} を削除しますか?(録音・文字起こしも消えます)`)) return;
      const date = s.meta.started_at.slice(0, 10);
      const r = await fetch(`/api/sessions/${date}/${s.dirname}`, { method: "DELETE" });
      if (!r.ok) alert((await r.json().catch(() => ({}))).error || "削除に失敗しました");
      if (r.ok && !state.recording && state.selected?.dirname === s.dirname) clearScreen();
      loadSessions();
    };
    li.onclick = (e) => {
      if (e.target.closest(".memo-btn")) return;
      openSession(s.meta.started_at.slice(0, 10), s.dirname);
    };
    li.dataset.dirname = s.dirname;
    list.appendChild(li);
  }
  markSelected();
}

function markSelected() {
  for (const li of $("session-list").children)
    li.classList.toggle("selected", li.dataset.dirname === state.selected?.dirname);
}

// Show a saved recording: chat always; captions/summary only when not recording
async function openSession(date, dirname) {
  const view = ++state.viewId;
  const r = await fetch(`/api/sessions/${date}/${dirname}`);
  if (!r.ok || view !== state.viewId) return;
  const d = await r.json();
  if (view !== state.viewId) return;
  state.selected = { date, dirname };
  markSelected();
  $("chat-target").textContent = `[${d.meta.id}] ${d.meta.title}`;
  $("chat-input").placeholder = `[${d.meta.id}] について質問する…`;
  $("chat-log").replaceChildren();
  for (const m of d.chat) addChat(m.role, m.content);
  if (state.recording) return;
  state.viewing = true;
  $("rec-title").textContent = `[${d.meta.id}] ${d.meta.title}(保存済み)`;
  if (d.segments.length) $("captions").replaceChildren();
  else setPlaceholder("captions", "この録音の文字起こしはありません");
  for (const s of d.segments) {
    addCaption(s.ts, s.text);
    if (s.ja) addJapanese(s.ts, s.ja);
  }
  if (d.summary) addSummary(d.summary, "保存済みの要約");
  else setPlaceholder("summaries", "この録音の要約はありません");
  $("captions").scrollTop = 0;
  $("btn-clear").classList.remove("hidden");
}

/* ---------- memo ---------- */
let memoTarget = null;
function openMemo(s) {
  memoTarget = s;
  $("memo-edit").value = s.meta.memo || "";
  $("memo-modal").showModal();
}
$("btn-memo-close").addEventListener("click", () => $("memo-modal").close());
$("btn-memo-save").addEventListener("click", async () => {
  const date = memoTarget.meta.started_at.slice(0, 10);
  await fetch(`/api/sessions/${date}/${memoTarget.dirname}/memo`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ memo: $("memo-edit").value }),
  });
  $("memo-modal").close();
  loadSessions();
});

/* ---------- daily report ---------- */
const report = { tab: "slack", slack: "", md: "" };
try { $("f-mention").value = localStorage.getItem("mention") || ""; } catch {}

function showReport(tab = report.tab) {
  report.tab = tab;
  $("tab-slack").setAttribute("aria-selected", tab === "slack");
  $("tab-md").setAttribute("aria-selected", tab === "md");
  $("report-body").textContent = report[tab];
  $("btn-copy").disabled = !report[tab];
}
$("tab-slack").addEventListener("click", () => showReport("slack"));
$("tab-md").addEventListener("click", () => showReport("md"));

$("btn-report").addEventListener("click", () => {
  report.slack = report.md = "";
  showReport("slack");
  $("report-body").textContent = "上司のメンションを入力して「生成する」を押してください";
  $("report-modal").showModal();
});
$("btn-regen").addEventListener("click", async () => {
  const mention = $("f-mention").value.trim();
  try { localStorage.setItem("mention", mention); } catch {}
  $("btn-regen").disabled = true;
  $("btn-copy").disabled = true;
  $("report-body").textContent = "生成中…";
  try {
    const r = await fetch(`/api/daily-report?date=${$("list-date").value || today()}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mention }),
    });
    if (!r.ok) throw new Error();
    const data = await r.json();
    report.slack = data.slack;
    report.md = data.markdown;
    showReport();
  } catch {
    $("report-body").textContent = "生成に失敗しました";
  }
  $("btn-regen").disabled = false;
});
$("btn-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(report[report.tab]);
    $("btn-copy").textContent = "コピーしました ✓";
  } catch {
    $("btn-copy").textContent = "コピーできませんでした";
  }
  setTimeout(() => ($("btn-copy").textContent = "コピー"), 1500);
});
$("btn-report-close").addEventListener("click", () => $("report-modal").close());

/* ---------- chat ---------- */
$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("chat-input").value.trim();
  if (!q) return;
  const target = state.selected || state.current;
  if (!target) {
    alert("先にセッションを開始するか、一覧から選択してください");
    return;
  }
  $("chat-input").value = "";
  const view = state.viewId;
  addChat("user", q);
  const typing = addChat("assistant typing", "考え中");
  const r = await fetch(`/api/sessions/${target.date}/${target.dirname}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q }),
  });
  const data = await r.json().catch(() => ({}));
  typing.remove();
  if (view !== state.viewId) return; // screen was cleared; reply is in chat.md
  addChat("assistant", r.ok ? data.answer : (data.error || "エラーが発生しました"));
});

function addChat(cls, text) {
  const log = $("chat-log");
  const div = document.createElement("div");
  div.className = `chat-msg ${cls}`;
  div.textContent = text;
  log.appendChild(div);
  if (!state.recording) $("btn-clear").classList.remove("hidden");
  log.scrollTop = log.scrollHeight;
  return div;
}

/* ---------- init ---------- */
$("list-date").value = today();
$("list-date").addEventListener("change", loadSessions);
refreshStatus();
setInterval(refreshStatus, 10000);
loadSessions();
setInterval(loadSessions, 15000); // fallback when WS is unavailable
