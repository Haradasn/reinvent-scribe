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
    if ((state.cleared || state.viewing) && ["segment", "partial", "jp", "jp_failed", "summary"].includes(msg.type)) return;
    if (msg.type.startsWith("jp") && msg.dirname !== state.current?.dirname) return;
    if (msg.type === "jp_failed") { const p = pendingJp(msg.ts); if (p) p.textContent = "訳せませんでした(英語は保存済み)"; }
    if (msg.type === "segment") { clearPartial(); addCaption(msg.ts, msg.text, msg.translating); }
    if (msg.type === "partial") showPartial(msg.text);
    if (msg.type === "jp") addJapanese(msg.ts, msg.text);
    if (msg.type === "stt_fallback") { clearPartial(); addSummaryNote("ストリーミング接続が切れたため、ローカル文字起こしに切り替えました"); }
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
$("btn-latest").addEventListener("click", () => scrollToLatest(true));

function addCaption(ts, text, translating) {
  const box = $("captions");
  box.querySelector(".captions-empty")?.remove();
  const div = document.createElement("div");
  div.className = "caption";
  div.dataset.ts = String(ts);
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
  setPlaceholder("summaries", "オンライン時、約3分ごとに要約が更新されます");
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
  for (const b of document.querySelectorAll(".row-rec")) b.disabled = on || state.recording;
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
  state.cleared = false;
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
async function loadSessions() {
  const date = $("list-date").value || today();
  const seq = ++listSeq;
  const r = await (await fetch(`/api/sessions?date=${date}`)).json();
  if (seq !== listSeq) return; // a newer request superseded this one
  const list = $("session-list");
  list.replaceChildren();
  const plans = sched.items.filter((i) => i.date === date);
  const rest = new Set(r.sessions);
  for (const p of plans) {
    list.appendChild(planRow(p));
    for (const s of r.sessions) {
      if (s.meta.schedule_key === p.key) {
        rest.delete(s);
        list.appendChild(recordingRow(s));
      }
    }
  }
  for (const s of rest) list.appendChild(recordingRow(s));
  const add = document.createElement("button");
  add.className = "ghost-btn row-rec-other";
  add.textContent = "＋ 予定外の録音";
  add.disabled = state.recording;
  add.onclick = openUnscheduled;
  const li = document.createElement("li");
  li.className = "plan";
  li.appendChild(add);
  list.appendChild(li);
  markSelected();
}

function planRow(p) {
  const li = document.createElement("li");
  li.className = "plan";
  li.innerHTML =
    `<span class="row-time">${escapeHtml(p.start)}–${escapeHtml(p.end)}</span>` +
    `<span class="row-main">${escapeHtml(p.title)}<br><span class="row-venue">${escapeHtml(p.venue)}</span></span>` +
    (KIND[p.kind] ? `<span class="tag">${KIND[p.kind]}</span>` : "") +
    `<button class="row-rec">${p.date === sched.today ? "● 録音" : "● テスト"}</button>`;
  const b = li.querySelector(".row-rec");
  b.disabled = state.recording || !!state.busy;
  b.onclick = () => startItem(p);
  return li;
}

function recordingRow(s) {
  const li = document.createElement("li");
  li.className = s.meta.schedule_key ? "rec" : "";
  const active = state.recording && state.current?.dirname === s.dirname;
  const status = active ? '<span class="hq-wait">録音中</span>'
    : !s.meta.ended_at ? '<span class="hq-wait">中断</span>'
    : s.hq ? '<span class="hq-tag">HQ✓</span>' : '<span class="hq-wait">HQ処理中…</span>';
  li.innerHTML =
    `<span class="sid">${escapeHtml(s.meta.id)}</span>` +
    `<span class="row-main">${s.meta.schedule_key ? `録音 ${escapeHtml(s.meta.started_at.slice(11, 16))}` : escapeHtml(s.meta.title)}</span>` +
    (s.meta.test ? '<span class="tag">テスト</span>' : "") + status +
    (active ? "" :
      `<a class="memo-btn" title="音声をダウンロード" download href="/api/sessions/${s.meta.started_at.slice(0, 10)}/${encodeURIComponent(s.dirname)}/audio">♪⤓</a>` +
      `<a class="memo-btn" title="文字起こしをダウンロード" download href="/api/sessions/${s.meta.started_at.slice(0, 10)}/${encodeURIComponent(s.dirname)}/transcript.txt">文字⤓</a>` +
      (s.summary ? `<a class="memo-btn" title="日本語要約をダウンロード" download href="/api/sessions/${s.meta.started_at.slice(0, 10)}/${encodeURIComponent(s.dirname)}/summary.txt">要約⤓</a>` : "") +
      `<a class="memo-btn" title="記事用(文字起こし+写真)をダウンロード" download href="/api/sessions/${s.meta.started_at.slice(0, 10)}/${encodeURIComponent(s.dirname)}/article.zip">記事⤓</a>`) +
    (s.photos ? `<span class="tag">📷${s.photos}</span>` : "") +
    `<button class="memo-btn memo-edit" title="メモ編集">✎</button>` +
    `<button class="memo-btn del-btn" title="削除">🗑</button>`;
  li.querySelector(".memo-edit").onclick = () => openMemo(s);
  li.querySelector(".del-btn").onclick = async () => {
    if (!confirm(`[${s.meta.id}] ${s.meta.title} を削除しますか?(録音・文字起こし・写真も消えます)`)) return;
    const date = s.meta.started_at.slice(0, 10);
    const r = await fetch(`/api/sessions/${date}/${s.dirname}`, { method: "DELETE" });
    if (!r.ok) alert((await r.json().catch(() => ({}))).error || "削除に失敗しました");
    if (r.ok && !state.recording && state.selected?.dirname === s.dirname) clearScreen();
    refreshSchedule();
    loadSessions();
  };
  li.onclick = (e) => {
    if (e.target.closest(".memo-btn")) return;
    openSession(s.meta.started_at.slice(0, 10), s.dirname);
  };
  li.dataset.dirname = s.dirname;
  return li;
}

function markSelected() {
  for (const li of $("session-list").children)
    li.classList.toggle("selected", !!li.dataset.dirname && li.dataset.dirname === state.selected?.dirname);
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
  else setPlaceholder("summaries", "この録音の要約はありません");
  $("captions").scrollTop = 0;
  showLatestBtn();
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
  $("btn-mail").textContent = `${tab === "slack" ? "Slack投稿文" : "日報"}をGmailで送信`;
  $("btn-mail").disabled = !report[tab] || !state.mailOk;
  $("mail-note").textContent = state.mailOk ? "" : ".env に Gmail の設定が必要です(README参照)";
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
  $("btn-mail").disabled = true;
  report.slack = report.md = "";
  const date = $("list-date").value || today();
  $("report-body").textContent = "生成中…";
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
  if (!state.recording) $("btn-clear").classList.remove("hidden");
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
  div.querySelector("button").onclick = () => viewPhoto(p, date, dirname);
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
    if (!state.recording && sched.items.some((i) => i.date === sched.today)) setDate(sched.today);
  }
  renderChips();
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

function renderChips() {
  const box = $("day-chips");
  box.replaceChildren();
  for (const date of new Set(sched.items.map((i) => i.date))) {
    const b = document.createElement("button");
    b.className = "ghost-btn";
    const d = new Date(date + "T00:00Z");
    b.textContent = `${d.getUTCMonth() + 1}/${d.getUTCDate()}(${"日月火水木金土"[d.getUTCDay()]})`;
    b.setAttribute("aria-pressed", String($("list-date").value === date));
    b.onclick = () => setDate(date);
    box.appendChild(b);
  }
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
      : state.recording ? "停止して次を開始" : isToday ? "● 録音開始" : "● テスト録音";
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
$("list-date").addEventListener("change", () => setDate($("list-date").value));
refreshSchedule();
setInterval(refreshSchedule, 30000);
refreshStatus();
setInterval(refreshStatus, 4000);
loadSessions();
setInterval(loadSessions, 15000); // fallback when WS is unavailable
