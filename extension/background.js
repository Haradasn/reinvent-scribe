const CALENDAR = "https://aws.amazon.com/jp/events/reinvent/calendar/";
let running = false;

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (msg?.type !== "sync") return;
  if (running) { respond({ error: "更新中です" }); return; }
  running = true;
  sync(sender.tab, msg.known || []).then(respond, (e) => respond({ error: String(e.message || e) }))
    .finally(() => { running = false; });
  return true;
});

async function sync(appTab, known) {
  // The calendar only lays out reliably in a visible tab
  const tab = await chrome.tabs.create({ url: CALENDAR, active: true });
  let keep = false;
  try {
    await loaded(tab.id);
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: scrape, args: [known] });
    if (!result) throw new Error("カレンダーを読めませんでした");
    keep = !!result.login; // leave the page open so the user can sign in
    return result;
  } finally {
    if (!keep) {
      await chrome.tabs.remove(tab.id).catch(() => {});
      await chrome.tabs.update(appTab.id, { active: true }).catch(() => {});
    }
  }
}

function loaded(id) {
  return new Promise((resolve, reject) => {
    const done = (f, v) => { clearInterval(poll); clearTimeout(limit); f(v); };
    const poll = setInterval(async () => {
      try { if ((await chrome.tabs.get(id)).status === "complete") done(resolve); }
      catch { done(reject, new Error("カレンダーのタブが閉じられました")); }
    }, 300);
    const limit = setTimeout(() => done(reject, new Error("カレンダーの読み込みが終わりません")), 40000);
  });
}

// Runs in the calendar page and only reads. The only things it ever clicks are
// the day buttons and the "View session: ..." cards; the details dialog, which
// also holds "Release seat" and favorites, is closed with the Escape key.
async function scrape(known) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const dayRe = /^\w+day, (\w+) (\d+)$/;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];
  const days = () => [...document.querySelectorAll("button[aria-label]")]
    .filter((b) => dayRe.test(b.getAttribute("aria-label")));
  const cards = () => [...document.querySelectorAll("[role=button]")]
    .filter((c) => /\d\d:\d\d (PST|–|PDT)/.test(c.innerText));
  for (let i = 0; i < 40 && !days().length; i++) await wait(500);
  if (!days().length) return { login: true, error: "re:Invent にログインしてから、もう一度押してください" };
  for (let i = 0; i < 20 && !cards().length; i++) await wait(500);

  const dialog = () => [...document.querySelectorAll("[role=dialog]")]
    .find((d) => /^Session details$/m.test(d.innerText) && d.querySelector('[aria-label="Back to Calendar"]'));
  const until = async (test, ms) => {
    for (let t = 0; t < ms; t += 100) { if (test()) return true; await wait(100); }
    return false;
  };
  let readDetails = true;
  async function details(label, nth, title) {
    const card = [...document.querySelectorAll("[role=button][aria-label]")]
      .filter((c) => c.getAttribute("aria-label") === label)[nth];
    if (!card) return null;
    card.click();
    let out = null;
    if (await until(() => dialog()?.innerText.includes(title), 5000)) {
      const lines = dialog().innerText.split(/\n+/).map((x) => x.trim()).filter(Boolean);
      const end = lines.indexOf("Session details");
      const field = (n) => lines.slice(end + 1).find((l) => l.startsWith(n + ": "))?.slice(n.length + 2) || "";
      const from = lines.findIndex((l) => /^\d+ min$/.test(l));
      const sp = lines.indexOf("Speakers");
      const body = lines.slice((sp >= 0 ? sp : from + 1) + 1, end)
        .filter((l) => !/seat|walk-up|waitlist/i.test(l) || l.length > 120);
      const cut = body.findIndex((l) => l.length > 70);
      out = {
        code: field("ID"), level: field("Level"), type: field("Type"), topic: field("Topic"),
        speakers: sp >= 0 ? body.slice(0, cut < 0 ? body.length : cut) : [],
        abstract: cut < 0 ? "" : body.slice(cut).join("\n"),
      };
    }
    document.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
    if (!(await until(() => !dialog(), 3000))) readDetails = false; // leave it; never click around
    await wait(150);
    return out;
  }

  const leaf = (re) => [...document.querySelectorAll("span,div,p,button")]
    .filter((e) => !e.children.length && re.test(e.textContent.trim()));
  const items = [];
  for (const label of days().map((b) => b.getAttribute("aria-label"))) {
    days().find((b) => b.getAttribute("aria-label") === label).click();
    let sig = null;
    for (let i = 0, same = 0; i < 14 && same < 3; i++) { // until the day stops changing
      await wait(400);
      const s = cards().map((c) => c.innerText).join("\u0000");
      same = s === sig ? same + 1 : 0;
      sig = s;
    }
    const ticks = leaf(/^\d\d:(00|30)$/).slice(0, 2).map((e) => e.getBoundingClientRect().top);
    const halfHour = ticks[1] - ticks[0];
    const fav = leaf(/^(Hide |Show )?Favorites$/)[0]?.getBoundingClientRect().left;
    if (!(halfHour > 20) || fav === undefined) return { error: "カレンダーの画面構成が変わったため読み取れません" };
    const [, month, day] = label.match(dayRe);
    const fresh = [];
    for (const c of cards()) {
      const lines = c.innerText.split(/\n+/).map((x) => x.trim()).filter(Boolean);
      const at = lines.findIndex((x) => /^\d\d:\d\d/.test(x));
      if (at < 1) continue;
      const [start, until] = lines[at].match(/\d\d:\d\d/g);
      const box = c.getBoundingClientRect();
      const viewed = (c.getAttribute("aria-label") || "").match(/^View session: (.*)$/s);
      let end = until;
      if (!end) {
        const [h, m] = start.split(":").map(Number);
        const t = h * 60 + m + Math.round((box.height + 8) / halfHour * 6) * 5;
        if (t >= 1440) continue;
        end = `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
      }
      const item = {
        month: MONTHS.indexOf(month) + 1, day: Number(day), start, end,
        title: viewed ? viewed[1].trim() : lines[at - 1],
        venue: viewed ? lines.slice(at + 1, at + 3).join(" | ") : "",
        kind: !viewed ? "personal" : box.left >= fav - 10 ? "favorite" : "reserved",
      };
      items.push(item);
      if (viewed) fresh.push([item, c.getAttribute("aria-label")]);
    }
    const seen = {};
    for (const [item, label] of fresh) {
      const nth = seen[label] = (seen[label] ?? -1) + 1;
      const id = `${item.month}/${item.day} ${item.start} ${item.title}`;
      if (readDetails && !known.includes(id)) item.details = await details(label, nth, item.title);
    }
  }
  if (!items.length) return { login: true, error: "予定が見つかりません。re:Invent にログインしてから、もう一度押してください" };
  return { items };
}
