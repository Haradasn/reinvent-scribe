const $ = (id) => document.getElementById(id);

const state = {
  recording: false,
  current: null, // {dirname, date}
  startedAt: null,
  timerId: null,
  selected: null, // chat target {dirname, date}
  viewId: 0, // bumped on clear; late replies for an older view are dropped
  viewing: false, // showing a saved recording
};

// Local date, not UTC: the server names day folders by local time
const today = () => {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};

/* ---------- status chips ---------- */
const LLM_ERR = { busy: "混雑中(自動で再試行)", rate_limited: "利用上限", auth: "APIキーエラー", payment: "残高不足", network: "接続なし" };
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    if (s.stt) setChip("chip-model", s.stt === "online" ? "字幕: オンライン" : "字幕: ローカル", s.stt === "online");
    else { $("chip-model").textContent = "字幕: 待機中"; $("chip-model").className = "chip"; }
    const ai = !s.llm_ok ? "未設定" : s.llm_error ? LLM_ERR[s.llm_error] || "エラー" : "正常";
    setChip("chip-llm", `翻訳・要約: ${ai}`, s.llm_ok && !s.llm_error);
    setChip("chip-mic", `mic: ${s.mic_ok ? "OK" : "NG"}`, s.mic_ok);
    const badge = $("summary-badge");
    badge.textContent = ai;
    badge.className = `badge ${s.llm_ok && !s.llm_error ? "online" : "offline"}`;
    state.mailOk = s.mail_ok;
    state.webOk = s.web_ok;
    syncRecordingState(s);
  } catch {}
}

// Restore recording UI after a page reload while a session is running
function syncRecordingState(s) {
  if (s.recording && s.session && !state.recording) {
    state.recording = true;
    state.current = { dirname: s.session.dirname, date: s.session.date };
    state.currentMeta = s.session.meta;
    setDate(s.session.meta.schedule_date || s.session.date);
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

/* ---------- input level (real microphone, newest on the right) ---------- */
const canvas = $("waveform");
const ctx = canvas.getContext("2d");
const level = { bars: [], at: 0, warning: null };
const BARS = 64;

function onLevel(msg) {
  // log scale: quiet speech (~100) and loud speech (~3000) both stay readable
  level.bars.push(Math.min(1, Math.max(0.03, Math.log10(Math.max(msg.rms, 10) / 20) / 2.3)));
  if (level.bars.length > BARS) level.bars.shift();
  level.at = performance.now();
  level.warning = msg.warning;
}

function drawWave() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const live = state.recording && !state.paused;
  const silent = live && level.at && performance.now() - level.at > 3000;
  if (live) {
    const w = canvas.width / BARS;
    ctx.fillStyle = level.warning || silent ? "#ffb454" : "#5ea8ff";
    level.bars.forEach((v, i) => {
      const h = v * canvas.height;
      ctx.fillRect((BARS - level.bars.length + i) * w, (canvas.height - h) / 2, w - 2, h);
    });
  }
  const text = !live ? ""
    : silent ? "マイクの音が届いていません。接続を確認してください"
    : level.warning === "low" ? "音が小さいか、無音です。マイクを近づけてください"
    : level.warning === "loud" ? "音が大きすぎて割れています。マイクを離してください" : "";
  const box = $("level-warning");
  if (box.textContent !== text) box.textContent = text;
  box.classList.toggle("hidden", !text);
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
    if (msg.type === "level") return onLevel(msg);
    if (state.viewing && ["segment", "partial", "jp", "jp_failed", "summary"].includes(msg.type)) return;
    if (msg.type.startsWith("jp") && msg.dirname !== state.current?.dirname) return;
    if (msg.type === "jp_failed") { const p = pendingJp(msg.ts); if (p) p.textContent = "訳せませんでした(英語は保存済み)"; }
    if (msg.type === "segment") { clearPartial(); addCaption(msg.ts, msg.text, msg.translating); }
    if (msg.type === "partial") showPartial(msg.text);
    if (msg.type === "jp") addJapanese(msg.ts, msg.text);
    if (msg.type === "stt_fallback") { clearPartial(); addSummaryNote("通信が切れたため、ローカル字幕に切り替えました(復旧すると自動で戻ります)"); refreshStatus(); }
    if (msg.type === "stt_restored") { clearPartial(); addSummaryNote("オンライン字幕に戻りました"); refreshStatus(); }
    if (msg.type === "summary" && (!msg.dirname || msg.dirname === state.current?.dirname)) addSummary(msg.text);
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

/* Follow new captions only while the reader is at the bottom */
const follow = { on: true, last: 0, unseen: false };
const capBox = $("captions");
capBox.addEventListener("scroll", () => {
  const gap = capBox.scrollHeight - capBox.clientHeight - capBox.scrollTop;
  if (gap < 24) follow.on = true;
  else if (capBox.scrollTop < follow.last) follow.on = false;
  follow.last = capBox.scrollTop;
  showLatestBtn();
});
function showLatestBtn() {
  $("btn-latest").classList.toggle("hidden", follow.on || state.viewing);
}
function scrollToLatest(force) {
  if (force) follow.on = true;
  if (follow.on) capBox.scrollTop = capBox.scrollHeight;
  showLatestBtn();
}
// a warning bar or window resize must not push the newest line out of view
new ResizeObserver(() => scrollToLatest()).observe(capBox);
$("btn-latest").addEventListener("click", () => scrollToLatest(true));

function addCaption(ts, text, translating) {
  const box = $("captions");
  box.querySelector(".captions-empty")?.remove();
  const div = document.createElement("div");
  div.className = "caption";
  div.dataset.ts = String(ts);
  div.tabIndex = 0;
  div.innerHTML = `<span class="ts">${fmtTs(ts)}</span>${escapeHtml(text)}`;
  if (translating) {
    const p = document.createElement("div");
    p.className = "jp-pending";
    p.textContent = "翻訳中…";
    div.appendChild(p);
  }
  box.appendChild(div);
  scrollToLatest();
}

function pendingJp(ts) {
  return capBox.querySelector(`.caption[data-ts="${ts}"] .jp-pending`);
}

function addJapanese(ts, text) {
  pendingJp(ts)?.remove();
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
  scrollToLatest();
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
  const where = state.viewing ? state.selected : state.current;
  summary.text = text;
  summary.where = where && { ...where };
  showSummaryTools();
}

/* ---------- copy / mail the summary on screen ---------- */
const summary = { text: "", where: null };
function showSummaryTools() {
  for (const id of ["btn-sum-copy", "btn-sum-mail"]) $(id).classList.toggle("hidden", !summary.text);
}
function clearSummary(text) {
  summary.text = "";
  summary.where = null;
  setPlaceholder("summaries", text);
  showSummaryTools();
}

$("btn-sum-copy").addEventListener("click", async () => {
  const b = $("btn-sum-copy");
  try {
    await navigator.clipboard.writeText(summary.text);
    b.textContent = "コピーしました ✓";
  } catch {
    b.textContent = "コピーできませんでした";
  }
  setTimeout(() => (b.textContent = "コピー"), 1500);
});

$("btn-sum-mail").addEventListener("click", async () => {
  const where = summary.where && { ...summary.where };
  if (!where) return;
  const text = summary.text; // what the user saw, even if a newer one arrives
  const url = `/api/sessions/${where.date}/${encodeURIComponent(where.dirname)}`;
  let d;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error();
    d = await r.json();
  } catch {
    alert("録音の情報を読めませんでした");
    return;
  }
  try { $("s-to").value = localStorage.getItem("mailTo") || ""; } catch {}
  $("s-subject").value = `[${d.meta.id}] ${d.meta.title} 要約`;
  $("s-body").value = `# [${d.meta.id}] ${d.meta.title}\n\n${d.meta.venue ? `会場: ${d.meta.venue}\n\n` : ""}${text}`;
  $("s-attach").checked = false;
  const grid = $("s-photos");
  grid.replaceChildren();
  for (const p of d.photos) {
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox"><img loading="lazy" alt="スライド写真" src="${photoUrl(where.date, where.dirname, p.id)}">`;
    l.firstChild.dataset.id = p.id;
    grid.appendChild(l);
  }
  const send = $("btn-s-send");
  send.disabled = !state.mailOk;
  $("s-note").textContent = state.mailOk ? "" : ".env に Gmail の設定が必要です(README参照)";
  send.onclick = async () => {
    const to = $("s-to").value.trim();
    if (!to) { $("s-note").textContent = "宛先を入力してください"; return; }
    const photos = [...grid.querySelectorAll("input:checked")].map((i) => ({ ...where, id: i.dataset.id }));
    if (!confirm(`${to} に要約を送信しますか?${$("s-attach").checked ? "\n(文字起こしを添付)" : ""}${photos.length ? `\n(写真${photos.length}枚を添付。共有してよい写真か確認してください)` : ""}`)) return;
    try { localStorage.setItem("mailTo", to); } catch {}
    send.disabled = true;
    $("s-note").textContent = "送信中…";
    try {
      const r = await fetch(`${url}/send-summary`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to, subject: $("s-subject").value, body: $("s-body").value,
          attach_transcript: $("s-attach").checked, photos }),
      });
      const x = await r.json();
      $("s-note").textContent = r.ok ? `送信しました ✓${x.attachments ? `(添付 ${x.attachments}件)` : ""}` : x.error || "送信に失敗しました";
    } catch {
      $("s-note").textContent = "送信できたか確認できません。Gmail の送信済みを確認してください";
    }
    send.disabled = false;
  };
  $("sum-modal").showModal();
});
$("btn-s-close").addEventListener("click", () => $("sum-modal").close());

/* ---------- enlarge captions ---------- */
// The live panel moves into a dialog, so recording controls, warnings and
// captions keep working there and nothing else on the page changes size.
const livePanel = document.querySelector(".main-panel");
const panelHome = document.createComment("live panel");
livePanel.before(panelHome);
function setFocusView(on) {
  const top = capBox.scrollTop; // moving a node resets its scroll
  const btn = $("btn-focus");
  if (on) {
    $("focus-modal").appendChild(livePanel);
    $("focus-modal").showModal();
  } else {
    panelHome.after(livePanel);
  }
  const label = on ? "拡大を閉じる" : "字幕だけを拡大";
  btn.setAttribute("aria-label", label);
  btn.title = label;
  const wasFollowing = follow.on;
  const place = () => {
    capBox.style.scrollBehavior = "auto";
    capBox.scrollTop = wasFollowing ? capBox.scrollHeight : top;
    capBox.style.scrollBehavior = "";
    follow.on = wasFollowing; // the jump is not the reader scrolling back
    follow.last = capBox.scrollTop;
    showLatestBtn();
  };
  place();
  requestAnimationFrame(() => requestAnimationFrame(place)); // after layout
  btn.focus();
}
$("btn-focus").addEventListener("click", () => {
  if ($("focus-modal").open) $("focus-modal").close();
  else setFocusView(true);
});
$("focus-modal").addEventListener("close", () => setFocusView(false)); // also Esc
$("focus-modal").addEventListener("click", (e) => {
  if (e.target === $("focus-modal")) $("focus-modal").close();
});

let openCard = null;
const cards = () => [...capBox.querySelectorAll(".caption:not(.partial)")];
function showCard(card) {
  openCard = card;
  const copy = card.cloneNode(true);
  copy.className = `caption-large${card.classList.contains("photo") ? " photo" : ""}`;
  const btn = copy.querySelector(".photo-btn");
  if (btn) btn.replaceWith(btn.querySelector("img"));
  for (const x of copy.querySelectorAll("details")) x.open = true;
  const parts = [copy];
  if (btn) { // a slide makes sense next to what was being said
    const all = cards(), i = all.indexOf(card);
    const before = all.slice(0, i).reverse().find((c) => !c.classList.contains("photo"));
    const after = all.slice(i + 1).find((c) => !c.classList.contains("photo"));
    for (const c of [before, after]) if (c) {
      const near = c.cloneNode(true);
      near.className = "caption-large near";
      parts.push(near);
    }
  }
  $("card-body").replaceChildren(...parts);
  $("card-body").scrollTop = 0;
  $("btn-card-photo").classList.toggle("hidden", !card.openPhoto);
  const all = cards(), i = all.indexOf(card);
  $("btn-card-prev").disabled = i <= 0;
  $("btn-card-next").disabled = i < 0 || i >= all.length - 1;
  if (!$("card-modal").open) $("card-modal").showModal();
}
function stepCard(d) {
  const all = cards(), next = all[all.indexOf(openCard) + d];
  if (next) showCard(next);
}
capBox.addEventListener("click", (e) => {
  const card = e.target.closest(".caption:not(.partial)");
  if (!card || e.target.closest("details") || String(getSelection())) return;
  showCard(card);
});
capBox.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("caption")) showCard(e.target);
});
$("btn-card-prev").addEventListener("click", () => stepCard(-1));
$("btn-card-next").addEventListener("click", () => stepCard(1));
$("btn-card-close").addEventListener("click", () => $("card-modal").close());
$("btn-card-photo").addEventListener("click", () => { $("card-modal").close(); openCard.openPhoto(); });
// on the document: a disabled prev/next button drops focus out of the dialog
document.addEventListener("keydown", (e) => {
  if (!$("card-modal").open) return;
  if (e.key === "ArrowLeft") stepCard(-1);
  if (e.key === "ArrowRight") stepCard(1);
});

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
  scrollToLatest();
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
}

/* ---------- clear screen (files on disk are untouched) ---------- */
function setPlaceholder(id, text) {
  const div = document.createElement("div");
  div.className = "captions-empty";
  div.textContent = text;
  $(id).replaceChildren(div);
}

function clearScreen() {
  state.viewId++;
  setPlaceholder("captions", "録音を開始すると、ここに英語字幕が流れます");
  clearSummary("オンライン時、約3分ごとに要約が更新されます");
  $("chat-log").replaceChildren();
  $("chat-input").value = "";
  $("chat-input").placeholder = "文字起こしについて質問する…";
  $("rec-timer").textContent = "00:00:00";
  state.current = null;
  state.selected = null;
  state.viewing = false;
  $("rec-title").textContent = "待機中";
  $("chat-target").textContent = "";
  scrollToLatest(true);
  markSelected();
}

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
    level.at = performance.now();
    state.paused = false;
    state.startedAt += Date.now() - state.pausedAt;
    state.timerId = setInterval(tick, 1000);
    setRecorderUI("recording");
    return;
  }
  const open = sched.base === undefined ? [] : candidates().filter((i) =>
    i.date === sched.today && evMs(i.date, i.start) - 15 * 60000 <= nowMs());
  if (open.length === 1) startItem(open[0]);
  else openUnscheduled();
});

function openUnscheduled() {
  $("start-form").reset();
  $("start-modal").showModal();
}

$("btn-cancel").addEventListener("click", () => $("start-modal").close());

$("start-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("start-modal").close();
  startRecording({ session_id: $("f-id").value.trim(), title: $("f-title").value.trim(), memo: $("f-memo").value.trim() });
});

async function startRecording(payload) {
  if (state.busy || state.recording) return;
  setBusy(true);
  try {
    await doStart(payload);
  } catch {
    alert("サーバに接続できませんでした");
  } finally {
    setBusy(false);
    refreshSchedule();
  }
}

function setBusy(on) {
  state.busy = on;
  renderHero();
  renderDay();
}

async function doStart(payload) {
  const r = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert(err.error || "開始に失敗しました");
    refreshStatus();
    return;
  }
  const data = await r.json();
  level.bars = [];
  level.warning = null;
  level.at = performance.now();
  clearScreen();
  state.recording = true;
  state.paused = false;
  state.current = { dirname: data.dirname, date: data.date };
  state.selected = state.current;
  state.startedAt = Date.now();
  state.timerId = setInterval(tick, 1000);
  setRecorderUI("recording");
  const label = `[${data.meta.id}] ${data.meta.title}`;
  state.currentMeta = data.meta;
  $("rec-title").textContent = label;
  $("chat-target").textContent = label;
  setDate(data.meta.schedule_date || data.date);
  loadSessions();
  refreshStatus();
}

$("btn-stop").addEventListener("click", async () => {
  if (state.busy) return;
  setBusy(true);
  $("btn-stop").disabled = true;
  $("rec-title").textContent = "停止中…";
  try {
    await stopRecording();
  } catch {
    alert("停止できたか確認できません。画面を再読み込みしてください");
  } finally {
    $("btn-stop").disabled = false;
    setBusy(false);
  }
});

async function stopRecording() {
  const r = await fetch("/api/sessions/stop", { method: "POST" });
  if (!r.ok && r.status !== 409) {
    alert("停止に失敗しました");
    return false;
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
  loadSessions();
  refreshSchedule();
  return true;
}

/* ---------- sessions ---------- */
let listSeq = 0;
const cal = { recs: [], days: {}, scrolled: null };

async function loadSessions() {
  const date = $("list-date").value || today();
  const seq = ++listSeq;
  let r, d;
  try {
    [r, d] = await Promise.all([
      fetch(`/api/sessions?date=${date}`).then((x) => x.json()),
      fetch("/api/recording-days").then((x) => x.json()),
    ]);
  } catch { return; }
  if (seq !== listSeq) return; // a newer request superseded this one
  cal.recs = r.sessions;
  cal.days = d.days;
  renderChips();
  renderDay();
}

const recsOf = (key) => cal.recs.filter((s) => s.meta.schedule_key === key);

function renderDay() {
  // a row menu the user has open must not vanish under them
  if (document.querySelector(".cal-panel .row-menu[open]")) return;
  const date = $("list-date").value || today();
  const plans = sched.items.filter((i) => i.date === date);
  drawGrid($("cal-day"), [date], 1.5);
  if (cal.scrolled !== date && plans.length) {
    cal.scrolled = date;
    const now = $("cal-day").querySelector(".cal-now");
    const first = now || $("cal-day").querySelector(".cal-event");
    if (first) $("cal-day").scrollTop = Math.max(0, first.offsetTop - 40);
  }
  const keys = new Set(plans.map((p) => p.key));
  const rest = cal.recs.filter((s) => !keys.has(s.meta.schedule_key));
  const box = $("other-recs");
  box.replaceChildren();
  if (rest.length) {
    const h = document.createElement("h3");
    h.textContent = `${plans.length ? "予定以外の録音" : "録音"} ${rest.length}件`;
    box.append(h, ...rest.map(recordingRow));
  }
  markSelected();
}

const toMin = (hm) => Number(hm.slice(0, 2)) * 60 + Number(hm.slice(3));

// Clashing sessions sit side by side, like any calendar
function lanes(items) {
  const out = [];
  let group = [], end = -1;
  const flush = () => {
    const cols = [];
    for (const g of group) {
      let c = cols.findIndex((e) => e <= g.from);
      if (c < 0) c = cols.push(0) - 1;
      cols[c] = g.to;
      g.col = c;
    }
    for (const g of group) g.cols = cols.length;
    out.push(...group);
    group = [];
  };
  for (const i of [...items].sort((x, y) => x.start.localeCompare(y.start))) {
    const from = toMin(i.start), to = Math.max(toMin(i.end), from + 30);
    if (from >= end) flush();
    group.push({ i, from, to });
    end = Math.max(end, to);
  }
  flush();
  return out;
}

function drawGrid(box, days, scale) {
  const items = sched.items.filter((i) => days.includes(i.date));
  const keep = box.scrollTop;
  box.replaceChildren();
  if (!items.length) {
    const p = document.createElement("div");
    p.className = "captions-empty";
    p.textContent = "この日の予定はありません";
    box.appendChild(p);
    return;
  }
  let lo = Math.floor(Math.min(...items.map((i) => toMin(i.start))) / 60) * 60;
  if (days.includes(sched.today)) { // show "now" when the day is about to begin
    const at = (nowMs() - evMs(sched.today, "00:00")) / 60000;
    if (at < lo && at > lo - 120) lo = Math.floor(at / 60) * 60;
  }
  const hi = Math.ceil(Math.max(...items.map((i) => Math.max(toMin(i.end), toMin(i.start) + 30))) / 60) * 60;
  const grid = document.createElement("div");
  grid.className = "cal-grid";
  grid.style.gridTemplateColumns = `44px repeat(${days.length}, minmax(0, 1fr))`;
  const rail = document.createElement("div");
  rail.className = "cal-rail";
  const height = (hi - lo) * scale;
  for (let m = lo; m < hi; m += 60) {
    const l = document.createElement("span");
    l.style.top = `${(m - lo) * scale}px`;
    l.textContent = `${Math.floor(m / 60)}:00`;
    rail.appendChild(l);
  }
  if (days.length > 1) {
    grid.appendChild(document.createElement("div"));
    for (const d of days) {
      const h = document.createElement("div");
      h.className = `cal-dayname${d === sched.today ? " today" : ""}`;
      h.textContent = dayLabel(d);
      grid.appendChild(h);
    }
  }
  grid.appendChild(rail);
  for (const d of days) {
    const col = document.createElement("div");
    col.className = "cal-col";
    col.style.height = `${height}px`;
    col.style.setProperty("--hour", `${60 * scale}px`);
    for (const e of lanes(items.filter((i) => i.date === d))) col.appendChild(eventBlock(e, lo, scale));
    if (d === sched.today && sched.base !== undefined) {
      const at = (nowMs() - evMs(d, "00:00")) / 60000;
      if (at >= lo && at <= hi) {
        const line = document.createElement("div");
        line.className = "cal-now";
        line.style.top = `${(at - lo) * scale}px`;
        col.appendChild(line);
      }
    }
    grid.appendChild(col);
  }
  box.appendChild(grid);
  box.scrollTop = keep;
}

function eventBlock({ i, from, to, col, cols }, lo, scale) {
  const recs = recsOf(i.key);
  const live = state.recording && state.currentMeta?.schedule_key === i.key;
  const past = sched.base !== undefined && evMs(i.date, i.end) < nowMs();
  const el = document.createElement("div");
  el.className = `cal-event ${["reserved", "favorite"].includes(i.kind) ? i.kind : "personal"}${past ? " past" : ""}${live ? " live" : ""}`;
  el.style.top = `${(from - lo) * scale}px`;
  el.style.height = `${(to - from) * scale - 3}px`;
  el.style.left = `${(col / cols) * 100}%`;
  el.style.width = `calc(${100 / cols}% - 4px)`;
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.title = `${i.start}–${i.end} ${i.title}`;
  const status = live ? '<span class="ev-live">● 録音中</span>'
    : recs.length || i.recorded ? '<span class="ev-done">✓ 録音済み</span>' : "";
  el.innerHTML =
    `<div class="ev-top"><span class="ev-time">${escapeHtml(i.start)}–${escapeHtml(i.end)}</span>` +
    (KIND[i.kind] ? `<span class="tag">${KIND[i.kind]}</span>` : "") + status + "</div>" +
    `<div class="ev-title">${escapeHtml(i.title)}</div>` +
    (i.venue ? `<div class="ev-venue">${escapeHtml(i.venue)}</div>` : "");
  // one tap to record, only on the day itself
  if (i.date === sched.today && !past && !live && !state.recording) {
    const b = document.createElement("button");
    b.className = "row-rec";
    b.textContent = "● 録音";
    b.disabled = !!state.busy;
    b.onclick = (e) => { e.stopPropagation(); closeCalendars(); startItem(i); };
    el.querySelector(".ev-top").appendChild(b);
  }
  el.onclick = () => openPlan(i);
  el.onkeydown = (e) => {
    if (e.target === el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openPlan(i); }
  };
  return el;
}

function closeCalendars() {
  for (const id of ["plan-modal", "week-modal"]) if ($(id).open) $(id).close();
}

async function openPlan(i) {
  let recs = recsOf(i.key);
  if (i.date !== $("list-date").value) { // from the week view
    try {
      recs = (await (await fetch(`/api/sessions?date=${i.date}`)).json()).sessions
        .filter((s) => s.meta.schedule_key === i.key);
    } catch { recs = []; }
  }
  $("plan-title").textContent = i.title;
  $("plan-info").textContent = [`${dayLabel(i.date)} ${i.start}–${i.end}(現地)`, i.venue, KIND[i.kind] || "予約済み"]
    .filter(Boolean).join("\n");
  const box = $("plan-recs");
  box.replaceChildren();
  if (recs.length) {
    const h = document.createElement("h3");
    h.textContent = `録音 ${recs.length}件`;
    box.append(h, ...recs.map(recordingRow));
  }
  const isToday = i.date === sched.today;
  const go = $("plan-go");
  go.textContent = isToday ? "● 録音開始" : "試し録り";
  go.className = isToday ? "primary-btn" : "ghost-btn";
  go.disabled = state.recording || !!state.busy;
  go.onclick = () => { closeCalendars(); startItem(i); };
  $("plan-note").textContent = state.recording ? "録音中です" : isToday ? ""
    : "当日(現地)になると「録音開始」に変わります";
  $("plan-modal").showModal();
}

for (const b of document.querySelectorAll("[data-close]"))
  b.addEventListener("click", () => b.closest("dialog").close());
$("btn-week").addEventListener("click", () => {
  const days = [...new Set(sched.items.map((i) => i.date))];
  drawGrid($("cal-week"), days, 1.35);
  $("week-modal").showModal();
});
$("btn-unscheduled").addEventListener("click", () => openUnscheduled());

function recordingRow(s) {
  const day = s.meta.started_at.slice(0, 10);
  const url = `/api/sessions/${day}/${encodeURIComponent(s.dirname)}`;
  const el = document.createElement("div");
  el.className = "rec-row";
  el.dataset.dirname = s.dirname;
  const active = state.recording && state.current?.dirname === s.dirname;
  const status = active ? "録音中" : !s.meta.ended_at ? "中断" : s.hq ? "" : "高精度版を作成中…";
  const len = s.meta.ended_at
    ? Math.max(1, Math.round((new Date(s.meta.ended_at) - new Date(s.meta.started_at)) / 60000)) : null;
  const sub = [s.meta.started_at.slice(11, 16), len && `${len}分`, s.photos && `写真${s.photos}枚`, status]
    .filter(Boolean).join(" · ");
  el.innerHTML =
    `<button class="rec-open"><span class="rec-name">${escapeHtml(s.meta.title)}` +
    (s.meta.test ? ' <span class="tag">試し録り</span>' : "") +
    `</span><span class="rec-sub">${escapeHtml(sub)}</span></button>` +
    `<details class="row-menu"><summary aria-label="この録音の操作" title="この録音の操作">⋯</summary><div class="menu">` +
    (active ? "" :
      `<a download href="${url}/audio" title="音声をダウンロード">音声をダウンロード</a>` +
      `<a download href="${url}/transcript.txt" title="文字起こしをダウンロード">文字起こしをダウンロード</a>` +
      (s.summary ? `<a download href="${url}/summary.txt" title="日本語要約をダウンロード">日本語要約をダウンロード</a>` : "") +
      `<a download href="${url}/article.zip" title="記事用(文字起こし+写真)をダウンロード">記事用データをダウンロード</a>`) +
    `<button class="m-memo">メモを編集</button>` +
    (active ? "" : '<button class="m-del">ゴミ箱に移動</button>') + "</div></details>";
  const menu = el.querySelector(".row-menu");
  menu.querySelector(".menu").addEventListener("click", () => { menu.open = false; });
  el.querySelector(".rec-open").onclick = () => { closeCalendars(); openSession(day, s.dirname); };
  el.querySelector(".m-memo").onclick = () => openMemo(s);
  const del = el.querySelector(".m-del");
  if (del) del.onclick = async () => {
    const r = await fetch(url, { method: "DELETE" });
    if (!r.ok) { alert((await r.json().catch(() => ({}))).error || "移動できませんでした"); return; }
    if (!state.recording && state.selected?.dirname === s.dirname) clearScreen();
    el.remove();
    refreshSchedule();
    loadSessions();
    syncNote("ゴミ箱に移動しました(復元できます)", true);
  };
  return el;
}
document.addEventListener("click", (e) => {
  for (const m of document.querySelectorAll(".row-menu[open]")) if (!m.contains(e.target)) m.open = false;
});

/* ---------- trash ---------- */
async function showTrash() {
  const { items } = await (await fetch("/api/trash")).json();
  const box = $("trash-list");
  box.replaceChildren();
  if (!items.length) box.textContent = "ゴミ箱は空です";
  for (const s of items) {
    const url = `/api/trash/${s.date}/${encodeURIComponent(s.dirname)}`;
    const el = document.createElement("div");
    el.className = "rec-row";
    el.innerHTML = `<div class="rec-open"><span class="rec-name">${escapeHtml(s.meta.title)}</span>` +
      `<span class="rec-sub">${escapeHtml(s.meta.started_at.slice(0, 16).replace("T", " "))}</span></div>` +
      `<button class="ghost-btn t-back">元に戻す</button><button class="link-btn t-del">完全に削除</button>`;
    el.querySelector(".t-back").onclick = async () => {
      if (!(await fetch(`${url}/restore`, { method: "POST" })).ok) alert("元に戻せませんでした");
      await showTrash();
      refreshSchedule();
      loadSessions();
    };
    el.querySelector(".t-del").onclick = async () => {
      if (!confirm(`「${s.meta.title}」の音声・文字起こし・写真を完全に削除しますか?元に戻せません。`)) return;
      if (!(await fetch(url, { method: "DELETE" })).ok) alert("削除できませんでした");
      showTrash();
    };
    box.appendChild(el);
  }
}
$("btn-trash").addEventListener("click", () => { $("trash-modal").showModal(); showTrash(); });

function markSelected() {
  for (const el of document.querySelectorAll(".rec-row[data-dirname]"))
    el.classList.toggle("selected", el.dataset.dirname === state.selected?.dirname);
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
  let asked = "";
  for (const [i, m] of d.chat.entries()) {
    const el = addChat(m.role, m.content);
    if (m.role === "user") asked = m.content;
    if (m.role === "assistant" && d.chat[i + 1]?.role !== "web") offerWeb(el, asked, { date, dirname });
  }
  if (state.recording) return;
  state.viewing = true;
  $("rec-title").textContent = `[${d.meta.id}] ${d.meta.title}(保存済み)`;
  if (d.segments.length || d.photos.length) $("captions").replaceChildren();
  else setPlaceholder("captions", "この録音の文字起こしはありません");
  const shots = d.photos.slice();
  for (const s of d.segments) {
    while (shots.length && shots[0].pos < s.ts) addPhoto(shots.shift(), date, dirname);
    addCaption(s.ts, s.text);
    if (s.ja) addJapanese(s.ts, s.ja);
  }
  for (const p of shots) addPhoto(p, date, dirname);
  if (d.photos.length) photoBar(d, date, dirname);
  if (d.summary) addSummary(d.summary, "保存済みの要約");
  else clearSummary("この録音の要約はありません");
  $("captions").scrollTop = 0;
  showLatestBtn();
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
  $("btn-mail").textContent = `${tab === "slack" ? "Slack投稿文" : "日報"}をGmailで送信`;
  $("btn-mail").disabled = !report[tab] || !state.mailOk;
  $("mail-note").textContent = state.mailOk ? "" : ".env に Gmail の設定が必要です(README参照)";
}
$("tab-slack").addEventListener("click", () => showReport("slack"));
$("tab-md").addEventListener("click", () => showReport("md"));

function reportState(message = "") {
  const done = !!report.md;
  const b = $("btn-regen");
  $("report-result").classList.toggle("hidden", !done);
  b.disabled = false;
  b.textContent = done ? "作り直す" : "日報を作成";
  // one main action at a time: make it first, then copy it
  b.className = done ? "ghost-btn" : "primary-btn";
  $("report-status").textContent = message;
  if (done) showReport();
}

$("btn-report").addEventListener("click", () => {
  const date = $("list-date").value || today();
  // reopening the same day keeps what was made
  if (report.date !== date && !report.busy) report.slack = report.md = "";
  $("report-title").textContent = `出張日報 ${date.replaceAll("-", "/")}`;
  if (!report.busy) reportState();
  $("report-modal").showModal();
});
$("btn-regen").addEventListener("click", async () => {
  const mention = $("f-mention").value.trim();
  try { localStorage.setItem("mention", mention); } catch {}
  const date = $("list-date").value || today();
  const b = $("btn-regen");
  report.busy = true;
  b.disabled = true;
  b.textContent = "作成中…";
  for (const id of ["btn-copy", "btn-mail"]) $(id).disabled = true;
  $("report-status").textContent = "録音を読んで日報を作っています。録音が多いと1分ほどかかります";
  let message = "";
  try {
    const r = await fetch(`/api/daily-report?date=${date}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mention }),
    });
    if (!r.ok) throw new Error();
    const data = await r.json();
    report.slack = data.slack;
    report.md = data.markdown;
    report.date = date;
    loadMailPhotos(date);
  } catch {
    message = report.md ? "作り直せませんでした。前の内容を表示しています" : "作成できませんでした。もう一度お試しください";
  }
  report.busy = false;
  $("mail-note").textContent = "";
  reportState(message);
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
try { $("f-to").value = localStorage.getItem("mailTo") || ""; } catch {}
$("btn-mail").addEventListener("click", async () => {
  const to = $("f-to").value.trim();
  const note = $("mail-note");
  if (!to) { note.textContent = "宛先を入力してください"; return; }
  const kind = report.tab === "slack" ? "Slack投稿文" : "日報";
  // a leading Slack mention means nothing in an email
  const body = report.tab === "slack" ? report.slack.replace(/^@[^\n]*\n+/, "") : report.md;
  const attach = $("f-attach").checked;
  if (!confirm(`${to} に ${report.date} の「${kind}」を送信しますか?${attach ? "\n(文字起こしを添付)" : ""}${mailPhotos().length ? `\n(写真${mailPhotos().length}枚を添付。共有してよい写真か確認してください)` : ""}`)) return;
  try { localStorage.setItem("mailTo", to); } catch {}
  $("btn-mail").disabled = true;
  note.textContent = "送信中…";
  try {
    const r = await fetch(`/api/daily-report/send?date=${report.date}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to, subject: `re:Invent 出張日報 ${report.date}`, body, attach_transcript: attach, photos: mailPhotos() }),
    });
    const d = await r.json();
    note.textContent = r.ok ? `送信しました ✓${d.attachments ? `(添付 ${d.attachments}件)` : ""}` : d.error || "送信に失敗しました";
  } catch {
    note.textContent = "送信できたか確認できません。Gmail の送信済みを確認してください";
  }
  $("btn-mail").disabled = false;
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
  let r = { ok: false }, data = {};
  try {
    r = await fetch(`/api/sessions/${target.date}/${target.dirname}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    data = await r.json();
  } catch {}
  typing.remove();
  if (view !== state.viewId) return; // screen was cleared; reply is in chat.md
  if (!r.ok && !$("chat-input").value) $("chat-input").value = q; // ready to resend
  const reply = addChat("assistant", r.ok ? data.answer : (data.error || "エラーが発生しました"));
  if (r.ok) offerWeb(reply, q, target);
});

// Searching is opt-in: it costs money and leaves the transcript's facts
function offerWeb(el, question, target) {
  if (!state.webOk || !question) return;
  const b = document.createElement("button");
  b.className = "ghost-btn web-btn";
  b.textContent = "🌐 Webで調べる";
  el.appendChild(document.createElement("br"));
  el.appendChild(b);
  b.onclick = async () => {
    const view = state.viewId;
    b.disabled = true;
    b.textContent = "検索中…";
    let r = { ok: false }, d = {};
    try {
      r = await fetch(`/api/sessions/${target.date}/${target.dirname}/web-search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      d = await r.json();
    } catch {}
    if (view !== state.viewId) return;
    if (!r.ok) { b.disabled = false; b.textContent = d.error || "失敗しました。もう一度"; return; }
    b.remove();
    const w = addChat("web", d.answer);
    el.after(w); // keep it next to the answer it supplements
    w.scrollIntoView({ block: "nearest" });
  };
}

function addChat(cls, text) {
  const log = $("chat-log");
  const div = document.createElement("div");
  div.className = `chat-msg ${cls}`;
  if (cls === "web") {
    const head = document.createElement("div");
    head.className = "web-head";
    head.textContent = "🌐 Webの情報(発言内容ではありません)";
    div.appendChild(head);
    for (const line of text.split("\n")) {
      const row = document.createElement(/^https?:\/\/\S+$/.test(line) ? "a" : "div");
      row.textContent = line;
      if (row.tagName === "A") { row.href = line; row.target = "_blank"; row.rel = "noopener noreferrer"; }
      div.appendChild(row);
    }
  } else div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

/* ---------- photos ---------- */
const photoUrl = (date, dirname, id) =>
  `/api/sessions/${date}/${encodeURIComponent(dirname)}/photos/${id}.jpg`;

function addPhoto(p, date, dirname) {
  const div = document.createElement("div");
  div.className = "caption photo";
  div.innerHTML = `<span class="ts">${p.taken ? fmtTs(p.pos) : "時刻なし"}</span>` +
    `<button class="photo-btn" title="拡大"><img loading="lazy" alt="スライド写真" src="${photoUrl(date, dirname, p.id)}"></button>`;
  if (p.text) {
    const t = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "スライドの読み取り";
    const body = document.createElement("div");
    body.className = "view-text";
    body.textContent = p.text;
    t.append(sum, body);
    div.appendChild(t);
  }
  div.openPhoto = () => viewPhoto(p, date, dirname);
  div.tabIndex = 0;
  capBox.appendChild(div);
}

// Tools for one recording's photos, shown above its captions
function photoBar(d, date, dirname) {
  const bar = document.createElement("div");
  bar.className = "photo-bar";
  const unread = d.photos.filter((p) => !p.text);
  bar.innerHTML = `<span>写真 ${d.photos.length}枚・時刻補正</span>` +
    `<button class="ghost-btn" data-s="-60">−1分</button><span class="shift"></span><button class="ghost-btn" data-s="60">+1分</button>` +
    (unread.length ? `<button class="ghost-btn read-all">未読の${unread.length}枚をAIで読み取る</button>` : "") +
    (d.photos.some((p) => p.text) ? `<button class="ghost-btn redo">写真を反映して要約を作り直す</button>` : "");
  const redo = bar.querySelector(".redo");
  if (redo) redo.onclick = async () => {
    redo.disabled = true;
    redo.textContent = "要約を作成中…";
    let r = { ok: false }, x = {};
    try {
      r = await fetch(`/api/sessions/${date}/${encodeURIComponent(dirname)}/resummarize`, { method: "POST" });
      x = await r.json();
    } catch {}
    redo.disabled = false;
    redo.textContent = r.ok ? "写真を反映しました ✓" : x.error || "失敗しました";
    if (r.ok && state.viewing && state.selected?.dirname === dirname) addSummary(x.summary, "写真を反映した要約");
  };
  const now = d.meta.photo_shift_sec || 0;
  bar.querySelector(".shift").textContent = `${now > 0 ? "+" : ""}${now / 60}分`;
  for (const b of bar.querySelectorAll("[data-s]")) b.onclick = async () => {
    await fetch(`/api/sessions/${date}/${encodeURIComponent(dirname)}/photo-shift`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds: now + Number(b.dataset.s) }),
    });
    openSession(date, dirname);
  };
  const all = bar.querySelector(".read-all");
  if (all) all.onclick = async () => {
    if (!confirm(`${unread.length}枚の写真をAIサービスに送って読み取ります。送ってよい写真だけですか?`)) return;
    all.disabled = true;
    let failed = 0;
    for (const [i, p] of unread.entries()) {
      all.textContent = `読み取り中 ${i + 1}/${unread.length}`;
      if (!(await readPhoto(p, date, dirname))) failed++;
    }
    if (failed) alert(`${failed}枚は読み取れませんでした`);
    if (state.viewing && state.selected?.dirname === dirname) openSession(date, dirname);
  };
  capBox.prepend(bar);
}

async function readPhoto(p, date, dirname) {
  try {
    const r = await fetch(`/api/sessions/${date}/${encodeURIComponent(dirname)}/photos/${p.id}/read`, { method: "POST" });
    if (!r.ok) return false;
    p.text = (await r.json()).text;
    return true;
  } catch { return false; }
}

const recOptions = (recs) => {
  const f = document.createDocumentFragment();
  for (const r of recs) f.append(new Option(r.label, `${r.date}/${r.dirname}`));
  return f;
};
const recValue = (v) => ({ date: v.slice(0, 10), dirname: v.slice(11) });

async function viewPhoto(p, date, dirname) {
  $("view-img").src = photoUrl(date, dirname, p.id);
  $("view-text").textContent = p.text || "";
  $("btn-view-read").disabled = false;
  $("btn-view-read").textContent = p.text ? "もう一度AIで読み取る" : "AIで読み取る";
  const box = await (await fetch("/api/photos/inbox")).json();
  const sel = $("view-rec");
  sel.replaceChildren(recOptions(box.recordings));
  sel.value = `${date}/${dirname}`;
  const refresh = () => { $("view-modal").close(); openSession(date, dirname); loadSessions(); };
  sel.onchange = async () => {
    const r = await fetch(`/api/sessions/${date}/${encodeURIComponent(dirname)}/photos/${p.id}/move`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(recValue(sel.value)),
    });
    if (!r.ok) alert("移動できませんでした");
    refresh();
  };
  $("btn-view-del").onclick = async () => {
    if (!confirm("この写真をアプリから削除しますか?(スマホの写真は消えません)")) return;
    await fetch(photoUrl(date, dirname, p.id).replace(/\.jpg$/, ""), { method: "DELETE" });
    refresh();
  };
  $("btn-view-read").onclick = async () => {
    const b = $("btn-view-read");
    b.disabled = true;
    b.textContent = "読み取り中…";
    const ok = await readPhoto(p, date, dirname);
    b.disabled = false;
    b.textContent = ok ? "もう一度AIで読み取る" : "失敗しました。もう一度";
    $("view-text").textContent = p.text || "";
  };
  $("btn-view-close").onclick = refresh;
  $("view-modal").showModal();
}

/* import: upload to the inbox, then confirm where each photo belongs */
let uploading = false;
const choice = {}; // id -> {on, rec}: survives re-rendering
$("btn-photos").addEventListener("click", () => { $("photo-modal").showModal(); showInbox(); });
$("btn-photo-close").addEventListener("click", () => $("photo-modal").close());
$("photo-files").addEventListener("change", (e) => { uploadPhotos([...e.target.files]); e.target.value = ""; });
// a stray drop must not navigate away from a running recording
for (const ev of ["dragover", "drop"]) window.addEventListener(ev, (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  const files = [...(e.dataTransfer?.files || [])];
  if (!files.length) return;
  if (!$("photo-modal").open) $("photo-modal").showModal();
  uploadPhotos(files);
});

async function uploadPhotos(files) {
  if (uploading || !files.length) return;
  uploading = true;
  $("btn-photo-save").disabled = true;
  let dup = 0;
  const bad = [];
  for (const [i, f] of files.entries()) {
    $("photo-note").textContent = `読み込み中 ${i + 1}/${files.length}`;
    try {
      const r = await fetch(`/api/photos/upload?name=${encodeURIComponent(f.name)}`, { method: "POST", body: f });
      const d = await r.json();
      if (!r.ok) bad.push(f.name);
      else if (d.duplicate) dup++;
    } catch { bad.push(f.name); }
  }
  uploading = false;
  await showInbox();
  if (dup) $("photo-note").textContent += ` ・取り込み済み${dup}枚を省略`;
  if (bad.length) $("photo-note").textContent += ` ・読めないファイル: ${bad.slice(0, 5).join("、")}${bad.length > 5 ? ` ほか${bad.length - 5}件` : ""}`;
}

async function showInbox() {
  const box = await (await fetch("/api/photos/inbox")).json();
  const grid = $("inbox-grid");
  grid.replaceChildren();
  const groups = [
    ["録音中に撮影", (p) => p.suggest?.inside],
    ["録音の前後10分(候補)", (p) => p.suggest && !p.suggest.inside],
    ["未分類(選ばなければ保存しません)", (p) => !p.suggest],
  ];
  for (const [title, test] of groups) {
    const list = box.items.filter(test);
    if (!list.length) continue;
    const h = document.createElement("h3");
    h.textContent = `${title} ${list.length}枚`;
    grid.appendChild(h);
    for (const p of list) {
      choice[p.id] ??= { on: !!p.suggest?.inside, rec: p.suggest ? `${p.suggest.date}/${p.suggest.dirname}` : "" };
      const c = choice[p.id];
      const card = document.createElement("div");
      card.className = "inbox-card";
      card.innerHTML = `<label><input type="checkbox"><img loading="lazy" alt="" src="/api/photos/inbox/${p.id}.jpg"></label>` +
        `<div class="inbox-when">${p.when ? escapeHtml(p.when) + (p.zone_known ? "" : "(時差不明)") : "撮影時刻なし"}</div><select></select>`;
      const ck = card.querySelector("input"), sel = card.querySelector("select");
      sel.append(new Option("録音を選ぶ", ""), recOptions(box.recordings));
      sel.value = c.rec;
      ck.checked = c.on;
      ck.onchange = () => { c.on = ck.checked; updateSave(box.items); };
      sel.onchange = () => { c.rec = sel.value; c.on = ck.checked = !!sel.value; updateSave(box.items); };
      grid.appendChild(card);
    }
  }
  $("photo-note").textContent = box.items.length ? `確認待ち ${box.items.length}枚(時刻は ${box.timezone})` : "";
  updateSave(box.items);
}

function updateSave(items) {
  const n = items.filter((p) => choice[p.id].on && choice[p.id].rec).length;
  const b = $("btn-photo-save");
  b.disabled = uploading || !n;
  b.textContent = `選択した${n}枚を保存`;
  b.onclick = async () => {
    const rest = items.length - n;
    const picked = items.filter((p) => choice[p.id].on && choice[p.id].rec);
    b.disabled = true;
    const r = await fetch("/api/photos/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: picked.map((p) => ({ id: p.id, ...recValue(choice[p.id].rec) })) }),
    });
    if (!r.ok) { alert("保存できませんでした"); return showInbox(); }
    if (rest && confirm(`残りの${rest}枚を確認待ちから取り消しますか?(スマホの写真は消えません)`))
      await fetch("/api/photos/inbox", { method: "DELETE" });
    await showInbox();
    $("photo-note").textContent = `${picked.length}枚を保存しました。録音を開くと字幕の間に表示されます`;
    loadSessions();
    const sel = state.selected;
    if (!state.recording && sel) openSession(sel.date, sel.dirname);
  };
}

$("btn-photo-clear").addEventListener("click", async () => {
  if (uploading || !confirm("確認待ちの写真をすべて取り消しますか?(スマホの写真は消えません)")) return;
  await fetch("/api/photos/inbox", { method: "DELETE" });
  showInbox();
});

/* photos in the report mail: nothing is attached unless picked */
async function loadMailPhotos(date) {
  const grid = $("mail-photo-grid");
  grid.replaceChildren();
  $("mail-photo-count").textContent = "0";
  const { sessions } = await (await fetch(`/api/sessions?date=${date}`)).json();
  for (const s of sessions.filter((x) => x.photos)) {
    const day = s.meta.started_at.slice(0, 10);
    const d = await (await fetch(`/api/sessions/${day}/${encodeURIComponent(s.dirname)}`)).json();
    for (const p of d.photos) {
      const l = document.createElement("label");
      l.innerHTML = `<input type="checkbox"><img loading="lazy" alt="スライド写真" src="${photoUrl(day, s.dirname, p.id)}">`;
      Object.assign(l.firstChild.dataset, { date: day, dirname: s.dirname, id: p.id });
      l.firstChild.onchange = () => ($("mail-photo-count").textContent = mailPhotos().length);
      grid.appendChild(l);
    }
  }
}
function mailPhotos() {
  return [...document.querySelectorAll("#mail-photo-grid input:checked")].map((i) => ({ ...i.dataset }));
}

/* ---------- schedule ---------- */
const sched = { items: [], now: null, today: null, initialized: false };
const KIND = { favorite: "お気に入り", personal: "自分の予定" };

function setDate(date) {
  $("list-date").value = date;
  renderChips();
  loadSessions();
}

// Event-local timestamps compared as UTC so the Mac's own timezone never matters
const evMs = (date, time) => Date.parse(`${date}T${time}Z`);
const nowMs = () => sched.base + (performance.now() - sched.at);

async function refreshSchedule() {
  try {
    const d = await (await fetch("/api/schedule")).json();
    sched.items = d.items;
    sched.today = d.today;
    sched.base = Date.parse(d.now + "Z");
    sched.at = performance.now();
  } catch {
    return;
  }
  if (!sched.initialized) {
    sched.initialized = true;
    if (!state.recording && sched.items.length)
      setDate(sched.items.find((i) => i.date >= sched.today)?.date || today());
  }
  renderChips();
  renderDay();
  renderHero();
}

/* Refresh from the re:Invent calendar through the bundled Chrome extension */
let syncTimer = null;
function syncNote(text, done) {
  $("sync-note").textContent = text;
  $("btn-sync").disabled = !done;
  clearTimeout(syncTimer);
}
$("btn-sync").addEventListener("click", () => {
  syncNote("予定を読み込み中…");
  syncTimer = setTimeout(() => syncNote("Chrome拡張が必要です(README参照。導入後はこの画面を再読み込み)", true), 1500);
  window.postMessage({ type: "scribe-sync" }, location.origin);
});
window.addEventListener("message", async (ev) => {
  if (ev.source !== window) return;
  if (ev.data?.type === "scribe-sync-ack") {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => syncNote("時間切れです。もう一度お試しください", true), 120000);
  }
  if (ev.data?.type !== "scribe-sync-result") return;
  if (ev.data.error) return syncNote(ev.data.error, true);
  const send = (force) => fetch("/api/schedule/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: ev.data.items, force }),
  });
  try {
    let r = await send(false), d = await r.json();
    if (d.confirm && confirm(`${d.error}更新しますか?`)) { r = await send(true); d = await r.json(); }
    if (!r.ok) return syncNote(d.error || "更新できませんでした", true);
    syncNote(`${d.total}件・追加${d.added.length} 変更${d.changed.length} 削除${d.removed.length}`, true);
    await refreshSchedule();
    loadSessions();
  } catch {
    syncNote("更新できませんでした", true);
  }
});

function dayLabel(date) {
  const d = new Date(date + "T00:00Z");
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()}(${"日月火水木金土"[d.getUTCDay()]})`;
}

function renderChips() {
  const box = $("day-chips");
  const chosen = $("list-date").value;
  const plans = {};
  for (const i of sched.items) plans[i.date] = (plans[i.date] || 0) + 1;
  const days = [...new Set([...Object.keys(plans), ...Object.keys(cal.days), today(), chosen])].filter(Boolean).sort();
  box.replaceChildren(...days.map((date) => {
    const b = document.createElement("button");
    b.className = "day-tab";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(chosen === date));
    const n = plans[date] ? `予定${plans[date]}` : "", m = cal.days[date] ? `録音${cal.days[date]}` : "";
    b.innerHTML = `<b>${dayLabel(date)}</b><small>${[n, m].filter(Boolean).join("・") || "予定なし"}</small>`;
    b.onclick = () => setDate(date);
    return b;
  }));
}

// Sessions to offer: those open now (from 15 min before start), else the next slot
function candidates() {
  const now = nowMs();
  const left = sched.items.filter((i) => !i.recorded && evMs(i.date, i.end) > now);
  const open = left.filter((i) => evMs(i.date, i.start) - 15 * 60000 <= now);
  if (open.length) return open;
  return left.filter((i) => i.date === left[0].date && i.start === left[0].start);
}

function until(ms) {
  if (ms <= 0) return "開催中";
  const min = Math.ceil(ms / 60000);
  if (min < 60) return `あと${min}分`;
  if (min < 1440) return `あと${Math.floor(min / 60)}時間${min % 60}分`;
  return `あと${Math.floor(min / 1440)}日`;
}

function renderHero() {
  const box = $("hero");
  if (sched.base === undefined) return;
  box.replaceChildren();
  for (const i of candidates()) {
    const wait = evMs(i.date, i.start) - nowMs();
    const isToday = i.date === sched.today;
    // while recording, only offer a switch once the next session is at hand
    if (state.recording && (wait > 10 * 60000 || !isToday)) continue;
    const card = document.createElement("div");
    card.className = `panel glass hero-card${wait <= 0 ? " now" : ""}`;
    card.innerHTML =
      `<div class="hero-body"><div class="hero-when">${escapeHtml(i.date.slice(5).replace("-", "/"))} ` +
      `${escapeHtml(i.start)}–${escapeHtml(i.end)}(現地)· ${until(wait)}` +
      `${KIND[i.kind] ? ` · ${KIND[i.kind]}` : ""}</div>` +
      `<div class="hero-title">${escapeHtml(i.title)}</div>` +
      `<div class="hero-venue">${escapeHtml(i.venue)}</div></div>` +
      `<button class="primary-btn"></button>`;
    const b = card.querySelector("button");
    b.textContent = state.busy ? "処理中…"
      : state.recording ? "停止して次を開始" : isToday ? "● 録音開始" : "試し録り";
    if (!state.recording && !isToday) b.className = "ghost-btn";
    b.disabled = !!state.busy;
    b.onclick = () => startItem(i);
    box.appendChild(card);
  }
}

async function startItem(item) {
  if (state.busy) return;
  if (state.recording) {
    setBusy(true);
    const ok = await stopRecording().catch(() => false);
    setBusy(false);
    if (!ok) return;
  }
  await startRecording({ schedule_key: item.key });
}

/* ---------- init ---------- */
$("list-date").value = today();
refreshSchedule();
setInterval(refreshSchedule, 30000);
refreshStatus();
setInterval(refreshStatus, 4000);
loadSessions();
setInterval(loadSessions, 15000); // fallback when WS is unavailable
