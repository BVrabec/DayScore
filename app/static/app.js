"use strict";

/* ---------- helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const tooltip = $("#tooltip");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

function parseDay(s) { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); }
function iso(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
const fmt = (d, opts) => (typeof d === "string" ? parseDay(d) : d).toLocaleDateString("en-GB", opts);
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
const round1 = (v) => Math.round(v * 10) / 10;

function band(score) {
  if (score >= 90) return "Exceptional";
  if (score >= 75) return "Very productive";
  if (score >= 60) return "Solid day";
  if (score >= 40) return "Mixed day";
  if (score >= 20) return "Low day";
  return "Off day";
}

const ICON = {
  chevron: '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
};

/* ---------- state ---------- */
let S = null;
let todayRange = store.get("todayRange") || "7";
let insightsRange = store.get("insightsRange") || "90";
let selectedDay = null;
let query = "";
const openDays = new Set();

async function api(path, opts = {}, retried = false) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) { location.href = "/login"; throw new Error("Logged out"); }
  const data = await res.json().catch(() => ({}));
  // Sensitive settings need the password again (then stay unlocked for 10 minutes).
  if (res.status === 403 && data.detail === "unlock" && !retried) {
    const password = await askPassword("Confirm it's you");
    if (password === null) throw new Error("Cancelled.");
    await api("/api/unlock", { method: "POST", body: JSON.stringify({ password }) }, true);
    return api(path, opts, true);
  }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// Days where scoring switched to another AI model, so jumps in the chart can be explained.
function modelChanges(entries) {
  const out = {};
  let prev = null;
  for (const e of entries) {
    if (prev !== null && e.model !== prev) out[e.day] = e.model.replace(/^(openrouter|local):/, "");
    prev = e.model;
  }
  return out;
}

async function load() {
  S = await api("/api/state");
  S.byDay = Object.fromEntries(S.entries.map((e) => [e.day, e]));
  S.modelChanges = modelChanges(S.entries);
  $("#app-name").textContent = S.app_name;
  document.title = S.app_name;
}

const route = () => location.hash.replace(/^#\/?/, "").split("/")[0];

function render() {
  if (!S) return;
  clearInterval(tgPoll);
  const r = route();
  document.querySelectorAll(".tabs a, #settings-btn").forEach((a) => a.classList.toggle("active", a.dataset.route === r));
  if (r === "history") renderHistory();
  else if (r === "insights") renderInsights();
  else if (r === "settings") renderSettings();
  else renderToday();
  // Restart the fade-in so every page change animates.
  view.classList.remove("fade-in");
  void view.offsetWidth;
  view.classList.add("fade-in");
}

/* ---------- shared pieces ---------- */
let ringId = 0;
function ring(score, animate = false) {
  const r = 58, c = 2 * Math.PI * r;
  const offset = c * (1 - score / 100);
  const id = `ring-grad-${++ringId}`;
  return `<div class="ring" role="img" aria-label="Score ${score} out of 100">
    <svg viewBox="0 0 132 132">
      <defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" style="stop-color:var(--a1)"/><stop offset="1" style="stop-color:var(--a2)"/>
      </linearGradient></defs>
      <circle cx="66" cy="66" r="${r}" fill="none" stroke="var(--accent-soft)" stroke-width="10"/>
      <circle class="ring-val" cx="66" cy="66" r="${r}" fill="none" stroke="url(#${id})" stroke-width="10"
        stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${animate ? c : offset}"
        data-offset="${offset}" style="transition: stroke-dashoffset 1.2s cubic-bezier(.2,.8,.2,1)"/>
    </svg>
    <div class="num"><b ${animate ? `data-count="${score}"` : ""}>${animate ? 0 : score}</b><span>of 100</span></div>
  </div>`;
}

function activityList(entry) {
  if (!entry.activities.length) return "";
  return `<div class="acts-label">What you did</div>
    <ul class="acts">${entry.activities.map((a) => `
      <li><span class="dot" style="background:var(--cat-${esc(a.category)})"></span>
        <span class="t">${esc(a.text)}</span><span class="cat">${esc(a.category)}</span></li>`).join("")}
    </ul>`;
}

const MOODS = ["😞", "😕", "😐", "🙂", "😄"];

function moodRow(entry) {
  return `<div class="mood-row" data-mood-day="${esc(entry.day)}">
    <span class="hint">${entry.mood ? "Mood" : "How did you feel?"}</span>
    ${MOODS.map((m, i) => `<button type="button" data-mood="${i + 1}" class="${entry.mood === i + 1 ? "on" : ""}"
      aria-label="Mood ${i + 1} of 5" aria-pressed="${entry.mood === i + 1}">${m}</button>`).join("")}
  </div>`;
}

function whyBlock(entry) {
  if (!entry.reason && !entry.tip) return "";
  return `<details class="why"><summary>Why ${entry.score}?</summary>
    ${entry.reason ? `<p>${esc(entry.reason)}</p>` : ""}
    ${entry.tip ? `<p class="tip"><b>Tip:</b> ${esc(entry.tip)}</p>` : ""}
  </details>`;
}

function entryResult(entry, animate = false) {
  return `<div class="result">
      ${ring(entry.score, animate)}
      <div>
        <div class="band">${band(entry.score)}</div>
        <h3>${esc(entry.title)}</h3>
        <p>${esc(entry.summary)}</p>
      </div>
    </div>
    ${whyBlock(entry)}
    ${activityList(entry)}
    ${moodRow(entry)}`;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-mood]");
  const row = btn?.closest("[data-mood-day]");
  if (!row) return;
  const day = row.dataset.moodDay;
  const mood = Number(btn.dataset.mood);
  const next = S.byDay[day]?.mood === mood ? null : mood;   // tap again to clear
  try {
    await api(`/api/entries/${day}/mood`, { method: "PUT", body: JSON.stringify({ mood: next }) });
    S.byDay[day].mood = next;
    row.querySelectorAll("[data-mood]").forEach((b) => {
      const on = Number(b.dataset.mood) === next;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on);
    });
    row.querySelector(".hint").textContent = next ? "Mood" : "How did you feel?";
  } catch (err) { alert(err.message); }
});

function original(entry) {
  return `<div class="original-label">What you wrote</div><div class="original">${esc(entry.raw_text)}</div>`;
}

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function animateRings(root) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    root.querySelectorAll(".ring-val").forEach((c) => { c.style.strokeDashoffset = c.dataset.offset; });
    // Count the score up in step with the ring filling.
    const nums = [...root.querySelectorAll("[data-count]")];
    if (reducedMotion()) { nums.forEach((n) => { n.textContent = n.dataset.count; }); return; }
    const start = performance.now();
    const tick = (t) => {
      const k = Math.min(1, (t - start) / 1200);
      const eased = 1 - Math.pow(1 - k, 3);
      nums.forEach((n) => { n.textContent = Math.round(eased * n.dataset.count); });
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }));
}

function openDay(day) {
  const e = S.byDay[day];
  if (!e) return;
  $("#dialog-body").innerHTML = `
    <div class="eyebrow">${fmt(day, { weekday: "long" })}</div>
    <h2 style="margin:0 0 18px;font-size:20px">${fmt(day, { day: "numeric", month: "long", year: "numeric" })}</h2>
    ${entryResult(e, true)}
    ${suggestionBox(day)}${todoistList(day)}${createdList(day)}
    <div id="day-note">${original(e)}</div>
    <div class="row-btns day-actions">
      <button class="btn ghost small" type="button" id="day-edit">Edit note</button>
      <button class="btn ghost small danger" type="button" id="day-delete">Delete day</button>
    </div>`;
  const dlg = $("#day-dialog");
  if (!dlg.open) dlg.showModal();
  animateRings(dlg);

  $("#day-edit").onclick = () => {
    $(".day-actions").hidden = true;
    $("#day-note").innerHTML = `<form class="note-form" id="edit-form">
        <div class="original-label">Edit what you wrote</div>
        <textarea name="text" required>${esc(e.raw_text)}</textarea>
        <div class="form-foot"><span class="hint">The day is scored again. Todoist isn't changed.</span>
          <span class="row-btns"><button class="btn ghost small" type="button" id="edit-cancel">Cancel</button>
          <button class="btn small" type="submit">Save &amp; re-score</button></span></div>
        <div class="error" hidden></div>
      </form>`;
    const form = $("#edit-form");
    form.text.focus();
    $("#edit-cancel").onclick = () => openDay(day);
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      const err = form.querySelector(".error");
      btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Scoring…';
      try {
        await api(`/api/entries/${day}`, { method: "PUT", body: JSON.stringify({ text: form.text.value }) });
        await load(); render(); openDay(day);
      } catch (x) {
        err.textContent = x.message; err.hidden = false;
        btn.disabled = false; btn.textContent = "Save & re-score";
      }
    };
  };
  $("#day-delete").onclick = async () => {
    if (!confirm(`Delete ${fmt(day, { weekday: "long", day: "numeric", month: "long" })}? Its note and score are removed for good (Todoist isn't changed).`)) return;
    try {
      await api(`/api/entries/${day}`, { method: "DELETE" });
      dlg.close(); await load(); render();
    } catch (x) { alert(x.message); }
  };
}
$("#dialog-close").onclick = () => $("#day-dialog").close();
$("#day-dialog").addEventListener("click", (e) => { if (e.target.id === "day-dialog") e.target.close(); });

function showTip(html, evt) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let x, y;
  if (evt.clientX != null && evt.type.startsWith("mouse")) {
    x = evt.clientX + pad; y = evt.clientY - rect.height - pad;
  } else {
    const r = evt.target.getBoundingClientRect();
    x = r.left + r.width / 2; y = r.top - rect.height - 8;
  }
  x = Math.min(Math.max(8, x), window.innerWidth - rect.width - 8);
  if (y < 8) y = (evt.clientY ?? 0) + pad * 1.5;
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}
const hideTip = () => { tooltip.hidden = true; };

function dayTip(day) {
  const e = S.byDay[day];
  const date = fmt(day, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const changed = S.modelChanges[day] ? `<div class="t-date">Scored by a different AI from here: ${esc(S.modelChanges[day])}</div>` : "";
  return e
    ? `<div class="t-date">${date}</div><b>${e.score}</b>${e.mood ? ` ${MOODS[e.mood - 1]}` : ""} · ${esc(e.title)}${changed}`
    : `<div class="t-date">${date}</div>Not logged`;
}

/* ---------- today ---------- */
const segButtons = (id, options, current) =>
  `<div class="seg" id="${id}">${options.map(([v, l]) =>
    `<button data-range="${v}" class="${current === v ? "active" : ""}">${l}</button>`).join("")}</div>`;

function onSeg(id, fn) {
  $(`#${id}`).onclick = (e) => { const v = e.target.dataset?.range; if (v) fn(v); };
}

function renderToday() {
  view.innerHTML = `
    <div class="narrow">
      <section class="card" id="today-card"></section>
      <section class="card">
        <div class="card-head">
          <h2>Daily score</h2>
          ${segButtons("today-range", [["7", "Week"], ["30", "Month"]], todayRange)}
        </div>
        <div class="chart" id="score-chart"></div>
      </section>
    </div>`;

  renderTodayCard();
  onSeg("today-range", (v) => { todayRange = v; store.set("todayRange", v); renderToday(); });
  drawCharts();
}

function tiles(s) {
  let delta = '<div class="delta">last 7 days</div>';
  if (s.avg_7 != null && s.avg_7_prev != null) {
    const d = round1(s.avg_7 - s.avg_7_prev);
    const cls = d > 0 ? "up" : d < 0 ? "down" : "";
    const arrow = d > 0 ? "▲" : d < 0 ? "▼" : "■";
    delta = `<div class="delta ${cls}" title="Compared with the 7 days before">${arrow} ${Math.abs(d)} vs last week</div>`;
  }
  const best = s.best;
  return `
    <div class="card tile"><div class="label">Average</div>
      <div class="value">${s.avg_7 == null ? "–" : Math.round(s.avg_7)}</div>${delta}</div>
    <div class="card tile"><div class="label">Streak</div>
      <div class="value">${s.streak}<small>day${s.streak === 1 ? "" : "s"}</small></div>
      <div class="delta">${S.byDay[S.today] ? "today logged ✓" : "log today to continue"}</div></div>
    <div class="card tile" ${best ? `data-open="${best.day}" style="cursor:pointer"` : ""}><div class="label">Best day</div>
      <div class="value">${best ? best.score : "–"}</div>
      <div class="delta">${best ? fmt(best.day, { day: "numeric", month: "short" }) : "no days yet"}</div></div>`;
}

/* ---------- insights ---------- */
function renderInsights() {
  view.innerHTML = `
    <div class="page-head"><h1>Insights</h1>
      ${segButtons("ins-range", [["30", "Month"], ["90", "3 months"], ["365", "Year"]], insightsRange)}
    </div>

    <div class="tiles">${tiles(S.stats)}</div>

    <section class="card section-gap">
      <h2>Your year</h2>
      <p class="sub">Each square is one day, colored by its score. Click a day to open it.</p>
      <div class="chart" id="heatmap"></div>
      <div class="heat-legend">
        <span>Not logged</span><i style="background:var(--heat-0)"></i><span class="gap"></span>
        <span>0</span><i style="background:var(--heat-1)"></i><i style="background:var(--heat-2)"></i><i style="background:var(--heat-3)"></i><i style="background:var(--heat-4)"></i><i style="background:var(--heat-5)"></i><span>100</span>
      </div>
    </section>

    <section class="card section-gap">
      <h2>${insightsRange === "365" ? "Weekly average score" : "Score trend"}</h2>
      <p class="sub">${insightsRange === "365" ? "Average of the logged days in each week." : "Daily scores with the 7-day average."}</p>
      <div class="legend" id="ins-legend"></div>
      <div class="chart" id="ins-chart"></div>
    </section>

    <div class="grid grid-2 section-gap">
      <section class="card">
        <h2>Where your effort went</h2>
        <p class="sub">Number of activities per category</p>
        <div id="cat-chart"></div>
      </section>
      <section class="card">
        <h2>Your best weekdays</h2>
        <p class="sub">Average score by day of the week</p>
        <div id="wd-chart"></div>
      </section>
    </div>`;
  onSeg("ins-range", (v) => { insightsRange = v; store.set("insightsRange", v); renderInsights(); });
  $(".tiles").onclick = (e) => { const d = e.target.closest("[data-open]")?.dataset.open; if (d) openDay(d); };
  drawCharts();
}

function drawCharts() {
  const empty = '<div class="empty">Charts appear after your first logged day.</div>';
  const has = S.entries.length > 0;
  const r = route();
  if (r === "") {
    const el = $("#score-chart");
    has ? renderScoreChart(el, todayRange) : (el.innerHTML = empty);
  } else if (r === "insights") {
    if (!has) { ["#heatmap", "#ins-chart", "#cat-chart", "#wd-chart"].forEach((s) => { $(s).innerHTML = empty; }); return; }
    renderHeatmap($("#heatmap"));
    renderScoreChart($("#ins-chart"), insightsRange, $("#ins-legend"));
    renderCategories($("#cat-chart"), insightsRange);
    renderWeekdays($("#wd-chart"), insightsRange);
  }
}

function rescoreNote(entry) {
  const r = lastRescore;
  if (!entry || !r || r.day !== entry.day) return "";
  const text = r.to === r.from ? `Re-scored with your addition: still ${r.to}`
    : `Re-scored with your addition: ${r.from} → ${r.to}`;
  return `<div class="rescore">${r.to > r.from ? "▲" : r.to < r.from ? "▼" : "↻"} ${text}</div>`;
}

function renderTodayCard() {
  const el = $("#today-card");
  const days = S.loggable_days;
  if (!selectedDay || !days.includes(selectedDay)) selectedDay = S.default_day;
  const entry = S.byDay[selectedDay];
  const isToday = selectedDay === S.today;
  const label = (d) => (d === S.today ? "Today" : "Yesterday");
  const seg = days.length > 1
    ? `<div class="seg" id="day-seg">${[...days].reverse().map((d) =>
        `<button data-day="${d}" class="${d === selectedDay ? "active" : ""}">${label(d)}</button>`).join("")}</div>`
    : "";

  const warn = S.config.ai.configured || entry ? "" :
    `<a class="callout" href="#/settings/ai">
       <b>Connect the AI to start scoring your days</b>
       <span>It takes a minute: paste your API key in Settings →</span>
     </a>`;

  const placeholder = isToday
    ? "What did you get done today? e.g. Fixed the backup job on the home server, 2 hours of studying, cleaned the kitchen, 45 min run…"
    : "What did you get done yesterday?";
  const lateHint = isToday ? "" : ` · Yesterday can be logged until ${S.late_entry_until}`;

  const form = (addMore) => `
    <form class="note-form${addMore ? " add-more" : ""}" ${addMore ? "hidden" : ""}>
      <textarea name="text" placeholder="${esc(addMore ? "Anything you forgot? It gets added and the day is re-scored." : placeholder)}" required></textarea>
      ${addMore ? "" : `<div class="mood-pick" role="radiogroup" aria-label="Mood (optional)">
        <span class="hint">Mood <span class="opt">(optional)</span></span>
        ${MOODS.map((m, i) => `<button type="button" role="radio" aria-checked="false" data-pick="${i + 1}" aria-label="Mood ${i + 1} of 5">${m}</button>`).join("")}
      </div>`}
      <div class="form-foot">
        <span class="hint">Slovenian or English${lateHint}</span>
        <button class="btn" type="submit">${addMore ? "Add & re-score" : "Score my day"}</button>
      </div>
      <div class="error" hidden></div>
    </form>`;

  el.innerHTML = `
    <div class="today-head">
      <div><div class="eyebrow">${label(selectedDay)}</div><h2>${fmt(selectedDay, { weekday: "long", day: "numeric", month: "long" })}</h2></div>
      ${seg}
    </div>
    ${warn}
    ${rescoreNote(entry)}
    ${entry
      ? `${entryResult(entry, true)}
         ${suggestionBox(entry.day)}${createdList(entry.day)}
         <div class="add-more"><button class="btn ghost small" id="add-more-btn" type="button">+ Add something</button></div>
         ${form(true)}`
      : form(false)}`;

  animateRings(el);
  const seg$ = $("#day-seg", el);
  if (seg$) seg$.onclick = (e) => { const d = e.target.dataset?.day; if (d) { selectedDay = d; renderTodayCard(); } };
  const addBtn = $("#add-more-btn", el);
  if (addBtn) addBtn.onclick = () => { addBtn.parentElement.hidden = true; const f = $(".note-form", el); f.hidden = false; f.text.focus(); };

  const f = $(".note-form", el);
  const pick = $(".mood-pick", el);
  if (pick) pick.onclick = (e) => {
    const b = e.target.closest("[data-pick]");
    if (!b) return;
    const was = f.dataset.mood === b.dataset.pick;
    f.dataset.mood = was ? "" : b.dataset.pick;
    pick.querySelectorAll("[data-pick]").forEach((x) => {
      const on = x.dataset.pick === f.dataset.mood;
      x.classList.toggle("on", on); x.setAttribute("aria-checked", on);
    });
  };
  f.onsubmit = (e) => { e.preventDefault(); submitNote(f); };
  f.text.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitNote(f); } };
}

let lastRescore = null;

async function submitNote(form) {
  const text = form.text.value.trim();
  if (!text) return;
  const btn = form.querySelector("button[type=submit]");
  const err = form.querySelector(".error");
  const label = btn.innerHTML;
  btn.disabled = true; form.text.disabled = true; err.hidden = true;
  btn.innerHTML = '<span class="spinner"></span> Scoring…';
  try {
    const mood = Number(form.dataset.mood) || null;
    const res = await api("/api/entries", { method: "POST", body: JSON.stringify({ day: selectedDay, text, mood }) });
    // After adding to an already scored day, show how the score moved.
    lastRescore = res.previous_score == null ? null
      : { day: res.entry.day, from: res.previous_score, to: res.entry.score };
    await load();
    render();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
    btn.disabled = false; form.text.disabled = false; btn.innerHTML = label;
  }
}

/* ---------- charts ---------- */
function heatLevel(score) {
  if (score == null) return 0;
  if (score < 20) return 1;
  if (score < 40) return 2;
  if (score < 60) return 3;
  if (score < 80) return 4;
  return 5;
}

function bindHover(el, tipFor, onClick) {
  const svg = el.querySelector("svg");
  const enter = (e) => {
    const t = e.target.closest("[data-key]");
    if (!t) return;
    el.classList.add("hovering");
    el.querySelectorAll(".hot").forEach((n) => n.classList.remove("hot"));
    el.querySelector(`.bar[data-for="${t.dataset.key}"]`)?.classList.add("hot");
    showTip(tipFor(t.dataset.key), e);
  };
  const leave = () => { el.classList.remove("hovering"); hideTip(); };
  svg.addEventListener("mousemove", enter);
  svg.addEventListener("mouseleave", leave);
  svg.addEventListener("focusin", enter);
  svg.addEventListener("focusout", leave);
  svg.addEventListener("click", (e) => { const t = e.target.closest("[data-key]"); if (t) onClick(t.dataset.key); });
  svg.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const t = e.target.closest("[data-key]"); if (t) onClick(t.dataset.key); }
  });
}

function renderHeatmap(el) {
  const W = el.clientWidth;
  const labelW = 30, topH = 18, gap = 3;
  const weeks = Math.max(8, Math.min(53, Math.floor((W - labelW) / 13)));
  const step = Math.min(20, (W - labelW) / weeks);
  const cell = step - gap;
  const today = parseDay(S.today);
  const dow = (today.getDay() + 6) % 7; // Monday = 0
  const start = addDays(today, -dow - (weeks - 1) * 7);

  let cells = "", months = "", lastMonth = null, lastLabelW = -99;
  for (let w = 0; w < weeks; w++) {
    const colFirst = addDays(start, w * 7);
    const m = colFirst.getMonth();
    if (m !== lastMonth) {
      if (w - lastLabelW >= 3 && !(w === 0 && colFirst.getDate() > 21)) {
        months += `<text class="axis-label" x="${labelW + w * step}" y="11">${fmt(colFirst, { month: "short" })}</text>`;
        lastLabelW = w;
      }
      lastMonth = m;
    }
    for (let d = 0; d < 7; d++) {
      const day = addDays(start, w * 7 + d);
      if (day > today) continue;
      const key = iso(day);
      const e = S.byDay[key];
      cells += `<rect class="heat-cell${key === S.today ? " today" : ""}" data-key="${key}"
        x="${labelW + w * step}" y="${topH + d * step}" width="${cell}" height="${cell}" rx="3"
        fill="var(--heat-${heatLevel(e?.score)})"/>`;
    }
  }
  const dayLabels = [["Mon", 0], ["Wed", 2], ["Fri", 4]].map(([t, d]) =>
    `<text class="axis-label" x="0" y="${topH + d * step + cell - 2}">${t}</text>`).join("");
  const H = topH + 7 * step;
  el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Calendar of daily scores">${months}${dayLabels}${cells}</svg>`;
  bindHover(el, dayTip, openDay);
}

function rangeSeries(range) {
  const today = parseDay(S.today);
  const n = Number(range);
  if (n <= 90) {
    return Array.from({ length: n }, (_, i) => {
      const d = addDays(today, i - n + 1);
      const key = iso(d);
      const window = [];
      for (let k = 0; k < 7; k++) { const e = S.byDay[iso(addDays(d, -k))]; if (e) window.push(e.score); }
      return { key, date: d, score: S.byDay[key]?.score ?? null, avg: mean(window) };
    });
  }
  const dow = (today.getDay() + 6) % 7;
  const lastMonday = addDays(today, -dow);
  return Array.from({ length: 52 }, (_, i) => {
    const d = addDays(lastMonday, (i - 51) * 7);
    const scores = [];
    for (let k = 0; k < 7; k++) { const e = S.byDay[iso(addDays(d, k))]; if (e) scores.push(e.score); }
    const avg = mean(scores);
    return { key: iso(d), date: d, score: avg == null ? null : round1(avg), count: scores.length, weekly: true };
  });
}

function barPath(x, top, w, base) {
  const h = base - top;
  const r = Math.min(4, w / 2, h);
  return `M${x},${base}V${top + r}Q${x},${top} ${x + r},${top}H${x + w - r}Q${x + w},${top} ${x + w},${top + r}V${base}Z`;
}

function renderScoreChart(el, range, legendEl = null) {
  const series = rangeSeries(range);
  const weekly = range === "365";
  // The 7-day average line only shows where a legend explains it (Insights).
  const showAvg = !!legendEl && !weekly && Number(range) >= 30;
  if (legendEl) {
    legendEl.innerHTML = showAvg
      ? '<span><i class="key-bar"></i>Daily score</span><span><i class="key-line"></i>7-day average</span>'
      : "";
    legendEl.hidden = !showAvg;
  }

  const W = el.clientWidth, H = 230;
  const m = { l: 30, r: 4, t: 10, b: 26 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const n = series.length;
  const y = (v) => m.t + ph - (v / 100) * ph;
  const bandW = pw / n;
  const bw = Math.max(2, Math.min(24, bandW - 2));
  const base = m.t + ph;

  let grid = "";
  for (const v of [0, 25, 50, 75, 100]) {
    grid += `<line class="${v === 0 ? "baseline" : "gridline"}" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
      <text class="axis-label" x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }

  let bars = "", hits = "", labels = "", marks = "";
  const tickEvery = n <= 7 ? 1 : Math.ceil(n / (W < 500 ? 4 : 7));
  series.forEach((p, i) => {
    const x0 = m.l + i * bandW;
    if (!weekly && S.modelChanges[p.key]) {
      marks += `<line class="model-mark" x1="${x0}" x2="${x0}" y1="${m.t}" y2="${base}"/>`;
    }
    if (p.score != null) {
      bars += `<path class="bar" data-for="${p.key}" d="${barPath(x0 + (bandW - bw) / 2, y(p.score), bw, base)}"/>`;
      if (n <= 7) bars += `<text class="axis-label" x="${x0 + bandW / 2}" y="${y(p.score) - 6}" text-anchor="middle">${p.score}</text>`;
    }
    hits += `<rect class="hit" data-key="${p.key}" x="${x0}" y="${m.t}" width="${bandW}" height="${ph}" ${p.score != null ? 'tabindex="0"' : ""}/>`;
    if ((n - 1 - i) % tickEvery === 0) {
      const text = n <= 7 ? fmt(p.date, { weekday: "short" }) : fmt(p.date, { day: "numeric", month: "short" });
      // The newest label hugs the right edge so it never gets clipped.
      const last = i === n - 1 && n > 7;
      labels += `<text class="axis-label" x="${last ? x0 + bandW : x0 + bandW / 2}" y="${H - 6}" text-anchor="${last ? "end" : "middle"}">${text}</text>`;
    }
  });

  let avgPath = "";
  if (showAvg) {
    let d = "", pen = false;
    series.forEach((p, i) => {
      if (p.avg == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${(m.l + i * bandW + bandW / 2).toFixed(1)},${y(p.avg).toFixed(1)}`;
      pen = true;
    });
    avgPath = `<path class="avg" d="${d}"/>`;
  }

  el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Score chart">${grid}${marks}${bars}${avgPath}${labels}${hits}</svg>`;

  const byKey = Object.fromEntries(series.map((p) => [p.key, p]));
  const tipFor = (key) => {
    const p = byKey[key];
    if (!weekly) {
      const avg = showAvg && p.avg != null ? `<div class="t-date">7-day average ${round1(p.avg)}</div>` : "";
      return dayTip(key) + avg;
    }
    const end = addDays(p.date, 6);
    const dates = `${fmt(p.date, { day: "numeric", month: "short" })} – ${fmt(end, { day: "numeric", month: "short" })}`;
    return p.score == null
      ? `<div class="t-date">${dates}</div>Nothing logged`
      : `<div class="t-date">${dates}</div><b>${p.score}</b> average · ${p.count} day${p.count === 1 ? "" : "s"} logged`;
  };
  bindHover(el, tipFor, (key) => { if (!weekly) openDay(key); });
}

function entriesInRange(range) {
  const from = iso(addDays(parseDay(S.today), -Number(range) + 1));
  return S.entries.filter((e) => e.day >= from);
}

function renderCategories(el, range) {
  const counts = Object.fromEntries(S.categories.map((c) => [c, 0]));
  let total = 0;
  for (const e of entriesInRange(range)) for (const a of e.activities) { counts[a.category] = (counts[a.category] || 0) + 1; total++; }
  if (!total) { el.innerHTML = '<div class="empty">No activities in this period.</div>'; return; }
  const rows = Object.entries(counts).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const max = rows[0][1];
  el.innerHTML = `<div class="hbars">${rows.map(([c, n]) => `
    <div class="hbar">
      <span class="name"><span class="dot" style="background:var(--cat-${c})"></span>${c}</span>
      <span class="track"><span class="fill" style="width:calc((100% - 70px) * ${n / max})"></span>
        <span class="val">${n} · ${Math.round((n / total) * 100)}%</span></span>
    </div>`).join("")}</div>`;
}

function renderWeekdays(el, range) {
  const buckets = Array.from({ length: 7 }, () => []);
  for (const e of entriesInRange(range)) buckets[(parseDay(e.day).getDay() + 6) % 7].push(e.score);
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  el.innerHTML = `<div class="cols">${buckets.map((b, i) => {
    const avg = mean(b);
    return `<div class="col" title="${names[i]}: ${avg == null ? "no data" : `${round1(avg)} average over ${b.length} day${b.length === 1 ? "" : "s"}`}">
      <span class="v">${avg == null ? "–" : Math.round(avg)}</span>
      ${avg == null ? "" : `<span class="b" style="height:calc((100% - 44px) * ${avg / 100})"></span>`}
      <span class="d">${names[i]}</span>
    </div>`;
  }).join("")}</div>`;
}

/* ---------- history ---------- */
function renderHistory() {
  view.innerHTML = `
    <div class="narrow">
      <div class="page-head">
        <h1>History</h1>
        <input class="search" type="search" placeholder="Search…" value="${esc(query)}" aria-label="Search activities and notes">
      </div>
      <div id="history-list"></div>
    </div>`;
  const input = $(".search");
  input.oninput = () => { query = input.value; renderHistoryList(); };
  renderHistoryList();
}

function renderHistoryList() {
  const list = $("#history-list");
  if (!S.entries.length) {
    list.innerHTML = '<div class="card empty">Nothing here yet. Log your first day on the <a href="#/">Today</a> page or in Telegram.</div>';
    return;
  }
  const q = query.trim().toLowerCase();
  const matches = (e) => !q || [e.title, e.summary, e.raw_text, e.reason, ...e.activities.map((a) => a.text)]
    .some((t) => t.toLowerCase().includes(q));
  const entries = S.entries.filter(matches).slice().reverse();
  if (!entries.length) { list.innerHTML = '<div class="card empty">No days match your search.</div>'; return; }

  const months = [];
  for (const e of entries) {
    const key = e.day.slice(0, 7);
    if (!months.length || months.at(-1).key !== key) months.push({ key, items: [] });
    months.at(-1).items.push(e);
  }

  list.innerHTML = months.map((mo) => {
    const avg = mean(mo.items.map((e) => e.score));
    let prev = null;
    const rows = mo.items.map((e) => {
      let gapRow = "";
      if (!q && prev) {
        const missing = Math.round((parseDay(prev.day) - parseDay(e.day)) / 864e5) - 1;
        if (missing > 0) gapRow = `<div class="gap-row">${missing} day${missing === 1 ? "" : "s"} not logged</div>`;
      }
      prev = e;
      const open = openDays.has(e.day);
      return `${gapRow}<div class="day-row${open ? " open" : ""}" data-day="${e.day}">
        <button class="day-sum" aria-expanded="${open}">
          <span class="date-block"><b>${parseDay(e.day).getDate()}</b><span>${fmt(e.day, { weekday: "short" })}</span></span>
          <span class="score-pill"><b>${e.score}</b><span class="meter"><i style="width:${e.score}%"></i></span></span>
          <span class="day-text"><h3>${esc(e.title)}</h3><p>${esc(e.summary)}</p></span>
          ${ICON.chevron}
        </button>
        ${open ? `<div class="day-detail">
          ${whyBlock(e)}${activityList(e)}${suggestionBox(e.day)}${todoistList(e.day)}${createdList(e.day)}${original(e)}
          <div class="row-btns"><button class="btn ghost small" type="button" data-open-day="${e.day}">Edit or delete</button></div>
        </div>` : ""}
      </div>`;
    }).join("");
    return `<section class="month">
      <div class="month-head"><h2>${fmt(mo.key + "-01", { month: "long", year: "numeric" })}</h2>
        <span>average ${round1(avg)} · ${mo.items.length} day${mo.items.length === 1 ? "" : "s"}</span></div>
      <div class="card days">${rows}</div>
    </section>`;
  }).join("");

  list.onclick = (e) => {
    const openBtn = e.target.closest("[data-open-day]");
    if (openBtn) { openDay(openBtn.dataset.openDay); return; }
    const row = e.target.closest(".day-sum")?.parentElement;
    if (!row) return;
    const d = row.dataset.day;
    openDays.has(d) ? openDays.delete(d) : openDays.add(d);
    renderHistoryList();
  };
}

/* ---------- settings ---------- */
let tgPoll = null;

function pill(kind, text) {
  return `<span class="pill ${kind}">${kind === "ok" ? "✓ " : kind === "wait" ? "● " : ""}${esc(text)}</span>`;
}

// Submit a settings form: disables the button, shows success or the server's error.
function bindForm(form, action, okText = "Saved ✓") {
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    const msg = form.querySelector(":scope > .form-foot .msg") || form.querySelector(".msg");
    const label = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Saving…';
    msg.className = "msg"; msg.textContent = "";
    try {
      const done = await action(new FormData(form));
      if (done === false) return;
      msg.className = "msg saved"; msg.textContent = okText;
    } catch (err) {
      msg.className = "msg error"; msg.textContent = err.message;
    } finally {
      btn.disabled = false; btn.innerHTML = label;
    }
  };
}

let aiProvider = null;  // provider tab shown in Settings → AI (null = the active one)

const PROVIDERS = {
  anthropic: {
    name: "Anthropic",
    placeholder: "sk-ant-…",
    steps: `<li>Go to <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a>, create an account and add a little credit under Billing.</li>
      <li>Create an API key, copy it and paste it below.</li>`,
  },
  openrouter: {
    name: "OpenRouter",
    placeholder: "sk-or-…",
    steps: `<li>Sign in at <a href="https://openrouter.ai" target="_blank" rel="noopener">openrouter.ai</a> and add a little credit.</li>
      <li>Create a key at <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">openrouter.ai/settings/keys</a> and paste it below.</li>`,
  },
  local: {
    name: "Your own server",
    placeholder: "Usually empty",
    steps: `<li>Install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a> (or LM Studio, llama.cpp…) on a computer in your network
        and get a model, e.g. <code>ollama pull qwen3:8b</code>.</li>
      <li>Let other devices reach it (for Ollama: <code>OLLAMA_HOST=0.0.0.0</code>) and enter its address below.
        Your notes then never leave your network.</li>`,
  },
};

function aiSection(ai) {
  const prov = aiProvider || ai.provider;
  const p = ai.providers[prov];
  const info = PROVIDERS[prov];
  const known = p.model in p.models;
  const options = Object.entries(p.models).map(([id, label]) =>
    `<option value="${esc(id)}" ${id === p.model ? "selected" : ""}>${esc(label)}</option>`).join("")
    + (prov === "openrouter" ? `<option value="__other" ${known ? "" : "selected"}>Other model…</option>` : "");
  const switching = ai.configured && prov !== ai.provider;
  const local = prov === "local";
  const localSaved = ai.provider === "local" && ai.configured;
  return `<section class="card" id="ai-card">
    <div class="card-head"><h2>AI scoring</h2>
      ${ai.configured ? pill("ok", `Connected · ${PROVIDERS[ai.provider].name}`) : pill("off", "Not set up")}</div>
    <p class="sub">An AI reads your notes and scores each day. Use your own API key (a few dollars of credit lasts for years),
      or your own AI server so nothing leaves your network.</p>
    <div class="seg" id="ai-prov" style="margin-bottom:16px">
      ${Object.entries(PROVIDERS).map(([id, x]) =>
        `<button type="button" data-prov="${id}" class="${id === prov ? "active" : ""}">${x.name}</button>`).join("")}
    </div>
    ${(local ? localSaved : p.key_hint) ? "" : `<ol class="steps">${info.steps}</ol>`}
    <form id="ai-form" class="fields">
      ${local ? `<label class="field">Server address
          <input name="url" value="${esc(ai.local_url || "")}" placeholder="http://192.168.1.20:11434" spellcheck="false" required>
        </label>
        <label class="field">Model
          <input name="model" value="${esc(p.model || "")}" placeholder="qwen3:8b" spellcheck="false" required>
          <span class="hint">A model with good instruction following works best (8B or bigger).</span>
        </label>` : ""}
      <label class="field">${local ? "API key (optional)" : `${info.name} API key`}
        <input type="password" name="api_key" autocomplete="off" spellcheck="false"
          placeholder="${p.key_hint ? `Saved (${esc(p.key_hint)}). Paste a new key to replace it` : info.placeholder}">
      </label>
      ${local ? "" : `<label class="field">Model <select name="model">${options}</select></label>`}
      ${prov === "openrouter" ? `<label class="field" id="custom-model" ${known ? "hidden" : ""}>Model ID
        <input name="custom_model" value="${known ? "" : esc(p.model)}" placeholder="vendor/model, e.g. mistralai/mistral-small-3.2-24b-instruct" spellcheck="false">
        <span class="hint">It must support structured outputs; see <a href="https://openrouter.ai/models?supported_parameters=structured_outputs" target="_blank" rel="noopener">the model list</a>.</span>
      </label>` : ""}
      <div class="form-foot">
        <span class="msg">${switching ? `Saving switches scoring to ${info.name}.` : ""}</span>
        <button class="btn" type="submit">${(local ? localSaved : p.key_hint) ? "Save" : "Connect"}</button>
      </div>
    </form>
  </section>`;
}

function telegramSection(tg) {
  let body;
  if (!tg.token_set) {
    body = `<ol class="steps">
        <li>In Telegram, open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, send <code>/newbot</code> and pick a name.</li>
        <li>Copy the token it gives you and paste it here.</li>
      </ol>
      <form id="tg-form" class="fields">
        <label class="field">Bot token
          <input type="password" name="token" autocomplete="off" spellcheck="false" placeholder="123456789:AA…" required>
        </label>
        <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Connect bot</button></div>
      </form>`;
  } else if (!tg.linked) {
    const code = tg.link_code || "";
    body = `<div class="link-step">
        <p>Your bot <b>@${esc(tg.bot_username)}</b> is ready. Now link it to your Telegram account:</p>
        <a class="btn" href="${esc(tg.link_url)}" target="_blank" rel="noopener">Open @${esc(tg.bot_username)} and press Start</a>
        <div class="code-box">
          <span>If that doesn't connect it, send the bot this code:</span>
          <b class="link-code" aria-label="Code ${esc(code.split("").join(" "))}">${esc(code.slice(0, 3))}<i></i>${esc(code.slice(3))}</b>
        </div>
        <p class="hint"><span class="spinner dark"></span> Waiting for you in Telegram. This page updates by itself.</p>
      </div>
      <div class="row-btns"><button class="btn ghost small" data-tg="remove" type="button">Use a different bot</button></div>`;
  } else {
    body = `<p class="connected">Messages to <b>@${esc(tg.bot_username)}</b> from ${tg.user_name ? `<b>@${esc(tg.user_name)}</b>` : "your account"} are logged as your day.
        Everyone else is ignored.</p>
      <div class="row-btns">
        <a class="btn small" href="https://t.me/${esc(tg.bot_username)}" target="_blank" rel="noopener">Open bot</a>
        <button class="btn ghost small" data-tg="unlink" type="button">Link a different account</button>
        <button class="btn ghost small" data-tg="remove" type="button">Remove bot</button>
      </div>`;
  }
  const trouble = tg.token_set && ["retrying", "error"].includes(tg.status);
  const status = trouble ? pill("wait", tg.status === "error" ? "Not working" : "Reconnecting…")
    : tg.linked ? pill("ok", "Connected") : tg.token_set ? pill("wait", "Almost done") : pill("off", "Optional");
  return `<section class="card" id="tg-card">
    <div class="card-head"><h2>Telegram</h2>${status}</div>
    <p class="sub">Write about your day by messaging your own bot, and get a reminder when you forget.</p>
    ${trouble && tg.status_detail ? `<p class="msg error">${esc(tg.status_detail)}</p>` : ""}
    ${body}
  </section>`;
}

function todoistSection(t) {
  const body = !t.connected
    ? `<ol class="steps">
        <li>In Todoist, open <a href="https://app.todoist.com/app/settings/integrations/developer" target="_blank" rel="noopener">Settings → Integrations → Developer</a>.</li>
        <li>Copy your API token and paste it here.</li>
      </ol>
      <form id="td-form" class="fields">
        <label class="field">Todoist API token
          <input type="password" name="token" autocomplete="off" spellcheck="false" placeholder="0123abcd…" required>
        </label>
        <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Connect Todoist</button></div>
      </form>`
    : `<form id="td-projects-form" class="fields">
        <div class="field">Projects to watch
          <div id="td-projects" class="checklist"><span class="hint"><span class="spinner dark"></span> Loading your projects…</span></div>
        </div>
        <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Save projects</button></div>
      </form>
      <form id="td-options" class="fields td-options">
        <label class="check-row"><input type="checkbox" name="confirm" ${t.confirm ? "checked" : ""}>
          <span><b>Ask me before ticking tasks off</b><br>
          <span class="hint">DayScore suggests the tasks your note finished; you confirm them on the Today page or in Telegram.</span></span></label>
        <label class="check-row"><input type="checkbox" name="create" ${t.create ? "checked" : ""}>
          <span><b>Add tasks I mention for later to my Inbox</b><br>
          <span class="hint">Only when you clearly say you need to do something, like "tomorrow I have to…".</span></span></label>
        <div class="form-foot"><span class="msg"></span><button class="btn small" type="submit">Save</button></div>
      </form>
      <div class="row-btns" style="margin-top:12px">
        <button class="btn ghost small" id="td-remove" type="button">Disconnect Todoist</button>
      </div>`;
  const status = !t.connected ? pill("off", "Optional")
    : t.project_ids.length ? pill("ok", `Watching ${t.project_ids.length} project${t.project_ids.length === 1 ? "" : "s"}`)
    : pill("wait", "Pick projects");
  return `<section class="card" id="td-card">
    <div class="card-head"><h2>Todoist</h2>${status}</div>
    <p class="sub">When your note says you finished a task from these projects, DayScore can tick it off in Todoist,
      and tasks you mention for later can go to your Inbox. Everything shows up in History.</p>
    ${body}
  </section>`;
}

async function bindTodoist(t) {
  const put = (url, body) => api(url, { method: "PUT", body: JSON.stringify(body) });
  const form = $("#td-form");
  if (form) {
    bindForm(form, async (f) => { S.config = await put("/api/config/todoist", { token: f.get("token") }); renderSettings(); return false; });
    return;
  }
  $("#td-remove").onclick = async () => {
    if (!confirm("Disconnect Todoist? Tasks already listed in History stay there.")) return;
    S.config = await api("/api/config/todoist", { method: "DELETE" });
    renderSettings();
  };
  bindForm($("#td-options"), async (f) => {
    S.config = await put("/api/config/todoist/options", { confirm: f.get("confirm") === "on", create: f.get("create") === "on" });
  });
  const box = $("#td-projects");
  const projectsForm = $("#td-projects-form");
  bindForm(projectsForm, async (f) => {
    S.config = await put("/api/config/todoist/projects", { project_ids: f.getAll("project") });
    await load();
    renderSettings();
    return false;
  });
  try {
    const projects = await api("/api/todoist/projects");
    if (!box.isConnected) return;  // page changed meanwhile
    box.innerHTML = projects.map((p) => `
      <label class="check-row"><input type="checkbox" name="project" value="${esc(p.id)}" ${t.project_ids.includes(p.id) ? "checked" : ""}>
        ${esc(p.name)}${p.is_inbox && p.name !== "Inbox" ? ' <span class="hint">(Inbox)</span>' : ""}</label>`).join("")
      || '<span class="hint">No projects found in your Todoist.</span>';
  } catch (err) {
    box.innerHTML = `<span class="error">${esc(err.message)}</span>`;
  }
}

/* Tasks the note seems to finish, waiting for the owner's OK before they're ticked off. */
function suggestionBox(day) {
  const items = (S.todoist_suggestions || {})[day] || [];
  if (!items.length) return "";
  return `<div class="suggest-box" data-day="${esc(day)}">
    <b>Tick these off in Todoist?</b>
    <p class="hint">Your note seems to finish them. Uncheck any that aren't done.</p>
    ${items.map((t) => `<label class="check-row"><input type="checkbox" value="${t.suggestion_id}" checked>
      <span>${esc(t.content)}${t.project ? ` <span class="hint">· ${esc(t.project)}</span>` : ""}</span></label>`).join("")}
    <div class="row-btns">
      <button class="btn small" type="button" data-sg="ok">Tick off selected</button>
      <button class="btn ghost small" type="button" data-sg="no">None of these</button>
    </div>
    <p class="msg error" hidden></p>
  </div>`;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-sg]");
  if (!btn) return;
  const box = btn.closest(".suggest-box");
  const boxes = [...box.querySelectorAll("input[type=checkbox]")];
  const accept = btn.dataset.sg === "ok" ? boxes.filter((b) => b.checked).map((b) => Number(b.value)) : [];
  const reject = boxes.map((b) => Number(b.value)).filter((id) => !accept.includes(id));
  box.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const res = await api("/api/todoist/suggestions", { method: "POST", body: JSON.stringify({ day: box.dataset.day, accept, reject }) });
    await load();
    render();
    if (res.failed.length) alert(`Todoist didn't accept these, please tick them off yourself:\n• ${res.failed.join("\n• ")}`);
  } catch (err) {
    const msg = box.querySelector(".msg");
    msg.textContent = err.message; msg.hidden = false;
    box.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  }
});

/* Tasks DayScore added to the Todoist Inbox from a day's note. */
function createdList(day) {
  const items = (S.todoist_created || {})[day] || [];
  if (!items.length) return "";
  return `<details class="todo-done">
    <summary>Added to Todoist <span class="count">${items.length}</span></summary>
    <ul class="tasks">${items.map((c) => `<li class="task">
      <span class="flag">+</span>
      <div class="t-body"><span class="t-title">${esc(c.content)}</span>
        <div class="t-meta"><span>Inbox</span>${c.due ? `<span>Due ${fmt(c.due, { weekday: "short", day: "numeric", month: "short" })}</span>` : ""}</div>
      </div></li>`).join("")}</ul>
  </details>`;
}

/* Tasks DayScore ticked off in Todoist on a day (History and the day popup). */
function todoistList(day) {
  const tasks = (S.todoist_done || {})[day] || [];
  if (!tasks.length) return "";
  const short = (d) => fmt(d, { day: "numeric", month: "short" });
  const item = (t) => {
    const p = 5 - t.priority;  // Todoist API 4 = the app's P1
    const time = new Date(t.completed_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    const how = t.status === "failed" ? "⚠ Todoist didn't accept it" : "✓ ticked off by DayScore";
    const meta = [
      t.project && `<span>${esc(t.project)}</span>`,
      t.due && `<span>Due ${short(t.due)}${t.recurring ? " ↻" : ""}</span>`,
      t.deadline && `<span>Deadline ${short(t.deadline)}</span>`,
      ...(t.labels || []).map((l) => `<span class="t-label">@${esc(l)}</span>`),
      `<span>${how} · ${time}</span>`,
    ].filter(Boolean).join("");
    return `<li class="task">
      <span class="flag p${p}" title="Priority ${p}">P${p}</span>
      <div class="t-body">
        <a class="t-title" href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.content)}</a>
        <div class="t-meta">${meta}</div>
        ${t.description ? `<p class="t-desc">${esc(t.description)}</p>` : ""}
      </div>
    </li>`;
  };
  return `<details class="todo-done">
    <summary>Ticked off in Todoist <span class="count">${tasks.length}</span></summary>
    <ul class="tasks">${tasks.map(item).join("")}</ul>
  </details>`;
}

function generalSection(g) {
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { /* old browser */ }
  if (!zones.includes(g.timezone)) zones.unshift(g.timezone);
  const reminder = (name, label, value) => `
    <label class="field check">
      <input type="checkbox" data-toggle="${name}" ${value ? "checked" : ""}> ${label}
      <input type="time" name="${name}" value="${esc(value || (name === "morning_reminder" ? "09:00" : "21:30"))}" ${value ? "" : "disabled"}>
    </label>`;
  return `<section class="card">
    <h2>General</h2>
    <form id="gen-form" class="fields" style="margin-top:14px">
      <label class="field">Time zone
        <select name="timezone">${zones.map((z) => `<option ${z === g.timezone ? "selected" : ""}>${esc(z)}</option>`).join("")}</select>
      </label>
      <div class="field">Your workdays
        <div class="day-picks">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((name, i) => `
          <label class="day-pick"><input type="checkbox" name="workday" value="${i}" ${g.workdays.includes(i) ? "checked" : ""}><span>${name}</span></label>`).join("")}
        </div>
        <span class="hint">Things you get done after work on these days earn extra credit.</span>
      </div>
      <label class="field">Yesterday can still be logged until
        <input type="time" name="late_entry_until" value="${esc(g.late_entry_until)}" required>
      </label>
      <div class="field">Telegram reminders
        ${reminder("morning_reminder", "Morning, if yesterday wasn't logged", g.morning_reminder)}
        ${reminder("evening_reminder", "Evening, if today isn't logged yet", g.evening_reminder)}
        <label class="field check"><input type="checkbox" name="weekly_summary" ${g.weekly_summary ? "checked" : ""}>
          Sunday evening, a short summary of your week</label>
      </div>
      <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Save</button></div>
    </form>
  </section>`;
}

/* ---------- security (authenticator app, recovery codes) ---------- */

// A small password prompt in the shared dialog. Resolves to the password, or null if cancelled.
function askPassword(title) {
  return new Promise((resolve) => {
    const dlg = $("#day-dialog");
    $("#dialog-body").innerHTML = `
      <form id="confirm-pw" class="fields">
        <h2 style="margin:0">${esc(title)}</h2>
        <p class="sub" style="margin:0">Confirm it's you with your password.</p>
        <input type="password" name="password" class="field-input" autocomplete="current-password" required>
        <div class="row-btns" style="justify-content:flex-end">
          <button class="btn ghost small" type="button" id="confirm-cancel">Cancel</button>
          <button class="btn small" type="submit">Continue</button>
        </div>
      </form>`;
    let answered = false;
    const done = (value) => { answered = true; dlg.close(); resolve(value); };
    $("#confirm-pw").onsubmit = (e) => { e.preventDefault(); done(e.target.password.value); };
    $("#confirm-cancel").onclick = () => done(null);
    dlg.addEventListener("close", () => { if (!answered) resolve(null); }, { once: true });
    dlg.showModal();
    $("#confirm-pw input").focus();
  });
}

function showRecoveryCodes(codes) {
  const text = `${S.app_name} recovery codes (each works once)\n\n${codes.join("\n")}\n`;
  $("#dialog-body").innerHTML = `
    <h2 style="margin:0 0 6px">Save your recovery codes</h2>
    <p class="sub">If you lose your phone, sign in with one of these instead. Each code works once.
      <b>They won't be shown again.</b></p>
    <div class="codes">${codes.map((c) => `<code>${esc(c)}</code>`).join("")}</div>
    <div class="row-btns" style="margin-top:16px">
      <button class="btn small" type="button" id="codes-copy">Copy</button>
      <a class="btn ghost small" download="dayscore-recovery-codes.txt"
         href="data:text/plain;charset=utf-8,${encodeURIComponent(text)}">Download .txt</a>
    </div>`;
  $("#codes-copy").onclick = async (e) => {
    try { await navigator.clipboard.writeText(text); e.target.textContent = "Copied ✓"; }
    catch { e.target.textContent = "Select and copy them manually"; }
  };
  $("#day-dialog").showModal();
}

function securitySection(sec) {
  const on = sec.totp;
  return `<section class="card" id="sec-card">
    <div class="card-head"><h2>Security</h2>${on ? pill("ok", "Two-factor on") : pill("off", "Password only")}</div>
    <p class="sub">Add a second step after your password: a 6-digit code from an app on your phone.</p>

    <div class="sec-row">
      <div><b>Authenticator app</b><span>6-digit codes from Google Authenticator, Aegis, 1Password, Bitwarden…</span></div>
      ${sec.totp
        ? `<div class="row-btns">${pill("ok", "On")}<button class="btn ghost small" type="button" id="totp-off">Turn off</button></div>`
        : sec.totp_pending
          ? `<div class="row-btns">${pill("wait", "Not finished")}<button class="btn small" type="button" id="totp-on">Finish setup</button></div>`
          : '<button class="btn small" type="button" id="totp-on">Set up</button>'}
    </div>
    <div id="totp-setup"></div>

    ${on ? `<div class="sec-row">
      <div><b>Recovery codes</b><span>${sec.recovery_left} unused. For when you lose your phone.</span></div>
      <button class="btn ghost small" type="button" id="codes-new">Get new codes</button>
    </div>` : ""}
    <div class="sec-row">
      <div><b>Signed-in devices</b><span>Lost a phone or used a shared computer? Sign out everywhere, including here.</span></div>
      <button class="btn ghost small" type="button" id="logout-all">Sign out everywhere</button>
    </div>
    <div class="sec-row">
      <div><b>Encryption on disk</b><span>${sec.encrypted
        ? "Your journal, settings and backups are stored encrypted. Copying the server's disk reveals nothing."
        : "Off. Anyone who can read the server's disk can read your journal. Set DAYSCORE_KEY to turn it on (see the README)."}</span></div>
      ${sec.encrypted ? pill("ok", "On") : pill("wait", "Off")}
    </div>
    <p class="msg" id="sec-msg"></p>
  </section>`;
}

function bindSecurity() {
  const msg = (text, ok = false) => { const m = $("#sec-msg"); m.className = `msg ${ok ? "saved" : "error"}`; m.textContent = text; };
  const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body) });
  const update = (sec) => {
    S.config.security = sec;
    renderSettings();
    if (sec.recovery_codes) showRecoveryCodes(sec.recovery_codes);
  };
  const guarded = (fn) => async (e) => {
    try { await fn(e); } catch (err) { msg(err.message); }
  };

  const on = $("#totp-on");
  const resuming = S.config.security.totp_pending;
  const showSetup = (setup, password, resumed) => {
    on.hidden = true;
    $("#totp-setup").innerHTML = `<div class="totp-box">
        <div class="qr">${setup.qr_svg}</div>
        <form id="totp-form" class="fields">
          <p style="margin:0"><b>1.</b> Scan this with your authenticator app, or type the key:</p>
          <code class="secret">${esc(setup.secret)}</code>
          ${resumed ? `<p class="hint" style="margin:0">Same key as before. If you already scanned it, just enter the code.
            <button class="linkish" type="button" id="totp-new">Use a new key instead</button></p>` : ""}
          <label class="field"><span><b>2.</b> Enter the 6-digit code it shows and press <b>Turn on</b>.
            Two-factor isn't active until you do.</span>
            <input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required>
          </label>
          <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Turn on</button></div>
        </form>
      </div>`;
    const form = $("#totp-form");
    form.code.focus();
    bindForm(form, async (f) => {
      update(await post("/api/security/totp/confirm", { code: f.get("code") }));
      msg("Authenticator app is on. Other devices were signed out and will need a code next time.", true);
      return false;
    });
    const fresh = $("#totp-new");
    if (fresh) fresh.onclick = guarded(async () => {
      showSetup(await post("/api/security/totp/start", { password, new_key: true }), password, false);
    });
  };
  if (on) on.onclick = guarded(async () => {
    const password = await askPassword(resuming ? "Finish setting up the authenticator app" : "Set up an authenticator app");
    if (password === null) return;
    showSetup(await post("/api/security/totp/start", { password }), password, resuming);
    S.config.security.totp_pending = true;  // so leaving and coming back shows "Finish setup"
  });

  const off = $("#totp-off");
  if (off) off.onclick = guarded(async () => {
    const password = await askPassword("Turn off the authenticator app");
    if (password !== null) update(await post("/api/security/totp/disable", { password }));
  });

  $("#logout-all").onclick = guarded(async () => {
    if (!confirm("Sign out on every device, including this one?")) return;
    await post("/api/logout/all", {});
    location.href = "/login";
  });

  const codes = $("#codes-new");
  if (codes) codes.onclick = guarded(async () => {
    const password = await askPassword("Get new recovery codes");
    if (password !== null) update(await post("/api/security/recovery", { password }));
  });
}

/* Settings: a side menu (a row of tabs on phones) and one section at a time. */
const SICON = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const SETTINGS_PAGES = [
  { id: "ai", label: "AI scoring", icon: SICON('<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z"/>') },
  { id: "telegram", label: "Telegram", icon: SICON('<path d="M22 3L2 11l7 2 2 7 4-5 5 4z"/><path d="M9 13l13-10"/>') },
  { id: "todoist", label: "Todoist", icon: SICON('<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l3 3 5-6"/>') },
  { id: "general", label: "General", icon: SICON('<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>') },
  { id: "backups", label: "Backups", icon: SICON('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>') },
  { id: "security", label: "Security", icon: SICON('<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>') },
  { id: "data", label: "Your data", icon: SICON('<path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>') },
];

function settingsPage() {
  const sub = location.hash.replace(/^#\/?/, "").split("/")[1];
  const known = (id) => SETTINGS_PAGES.some((p) => p.id === id);
  const page = known(sub) ? sub : known(store.get("settingsPage")) ? store.get("settingsPage") : "ai";
  store.set("settingsPage", page);
  return page;
}

// A small dot in the menu: "ok" (green) or "warn" (amber, needs a look). Nothing if not used.
function settingsDot(id) {
  const c = S.config;
  if (id === "ai") return c.ai.configured ? "ok" : "warn";
  if (id === "telegram") {
    if (c.telegram.token_set && ["retrying", "error"].includes(c.telegram.status)) return "warn";
    return c.telegram.linked ? "ok" : c.telegram.token_set ? "warn" : "";
  }
  if (id === "todoist") return !c.todoist.connected ? "" : c.todoist.project_ids.length ? "ok" : "warn";
  if (id === "security") return !c.security.encrypted ? "warn" : c.security.totp ? "ok" : "";
  if (id === "backups" && backupData) {
    const last = backupData.runs.find((r) => !r.skipped);
    if (last && !last.results.every((r) => r.ok)) return "warn";
    return backupData.config.password_set ? "ok" : "warn";
  }
  return "";
}

const DOT_TITLE = { ok: "All set", warn: "Needs a look" };

function paintSettingsDots() {
  document.querySelectorAll(".settings-nav [data-page]").forEach((a) => {
    const dot = settingsDot(a.dataset.page);
    const el = a.querySelector(".nav-dot");
    el.className = `nav-dot ${dot}`;
    el.title = DOT_TITLE[dot] || "";
  });
}

function renderSettings() {
  clearInterval(tgPoll);
  const c = S.config;
  const page = settingsPage();
  const sections = {
    ai: () => `${aiSection(c.ai)}
      <section class="card">
        <h2>What matters to you</h2>
        <p class="sub">The AI reads this every time it scores a day, so it knows what "productive" means for you.
          Write it in any language. It only affects new scores; past days keep theirs.</p>
        <form id="prio-form">
          <textarea name="priorities" placeholder="e.g. I'm studying for exams, so study counts a lot. I want to spend less time scrolling on my phone. Working on my home server and fixing things around the house is productive for me.">${esc(S.priorities)}</textarea>
          <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Save</button></div>
        </form>
      </section>`,
    telegram: () => telegramSection(c.telegram),
    todoist: () => todoistSection(c.todoist),
    general: () => generalSection(c.general),
    backups: () => backupCard(),
    security: () => `${securitySection(c.security)}
      <section class="card">
        <h2>Password</h2>
        ${c.password_from_env
          ? '<p class="sub" style="margin:8px 0 0">Your password is set by the server (APP_PASSWORD), so it can\'t be changed here.</p>'
          : `<form id="pw-form" class="fields" style="margin-top:14px">
              <label class="field">Current password<input type="password" name="current" autocomplete="current-password" required></label>
              <label class="field">New password<input type="password" name="new" autocomplete="new-password" minlength="8" required></label>
              <label class="field">Repeat new password<input type="password" name="repeat" autocomplete="new-password" required></label>
              <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Change password</button></div>
            </form>`}
      </section>`,
    data: () => `<section class="card">
        <h2>Your data</h2>
        <p class="sub">Download everything you've logged, as a spreadsheet (CSV) or for other apps (JSON).</p>
        <div class="row-btns">
          <a class="btn ghost small" href="/api/export.csv">Export CSV</a>
          <a class="btn ghost small" href="/api/export.json">Export JSON</a>
        </div>
      </section>
      <section class="card">
        <h2>Sign out</h2>
        <p class="sub">Sign out on this device. To sign out everywhere, go to <a href="#/settings/security">Security</a>.</p>
        <div class="row-btns"><button class="btn ghost small" id="logout" type="button">Log out</button></div>
      </section>
      <p class="version-note">${esc(S.app_name)} ${esc(S.version)} ·
        <a href="https://github.com/BVrabec/DayScore/releases" target="_blank" rel="noopener">What's new</a></p>`,
  };

  view.innerHTML = `
    <div class="settings-layout settings">
      <nav class="settings-nav" aria-label="Settings sections">
        <h1>Settings</h1>
        <div class="nav-list">
          ${SETTINGS_PAGES.map((p) => `<a href="#/settings/${p.id}" data-page="${p.id}" class="${p.id === page ? "active" : ""}"
              ${p.id === page ? 'aria-current="page"' : ""}>${p.icon}<span>${p.label}</span><i class="nav-dot"></i></a>`).join("")}
        </div>
      </nav>
      <div class="settings-body">${sections[page]()}</div>
    </div>`;
  paintSettingsDots();
  // On phones the menu scrolls sideways: bring the active tab to the middle.
  const list = $(".nav-list"), active = $(".nav-list a.active");
  if (list.scrollWidth > list.clientWidth) list.scrollLeft = active.offsetLeft - (list.clientWidth - active.offsetWidth) / 2;
  if (!backupData && page !== "backups") {
    api("/api/backups").then((d) => { backupData = d; paintSettingsDots(); }).catch(() => {});
  }

  const put = (url, body) => api(url, { method: "PUT", body: JSON.stringify(body) });
  const refresh = (config) => { S.config = config; renderSettings(); };
  const binders = {
    ai() {
      const aiForm = $("#ai-form");
      $("#ai-prov").onclick = (e) => {
        const prov = e.target.dataset?.prov;
        if (prov) { aiProvider = prov; renderSettings(); }
      };
      aiForm.model.onchange = () => {
        const custom = $("#custom-model");
        if (custom) custom.hidden = aiForm.model.value !== "__other";
      };
      bindForm(aiForm, async (f) => {
        const model = f.get("model") === "__other" ? (f.get("custom_model") || "").trim() : f.get("model");
        const config = await put("/api/config/ai", { provider: aiProvider || S.config.ai.provider, api_key: f.get("api_key"),
                                                     model, url: f.get("url") || "" });
        aiProvider = null;
        refresh(config);
        return false;
      });
      bindForm($("#prio-form"), async (f) => {
        await put("/api/settings", { priorities: f.get("priorities") });
        S.priorities = f.get("priorities").trim();
      });
    },
    telegram() {
      const tgForm = $("#tg-form");
      if (tgForm) bindForm(tgForm, async (f) => { refresh(await put("/api/config/telegram", { token: f.get("token") })); return false; });
      $("#tg-card").onclick = async (e) => {
        const action = e.target.dataset?.tg;
        if (!action) return;
        if (action === "remove" && !confirm("Remove this bot from DayScore? You can connect it again later.")) return;
        refresh(action === "remove"
          ? await api("/api/config/telegram", { method: "DELETE" })
          : await api("/api/config/telegram/unlink", { method: "POST" }));
      };
      // While waiting for "Start" in Telegram, check every few seconds.
      if (c.telegram.token_set && !c.telegram.linked) {
        tgPoll = setInterval(async () => {
          const t = (await api("/api/state")).config.telegram;
          // Re-render once linked, or if the code was replaced after wrong guesses.
          if (t.linked || t.link_code !== c.telegram.link_code) { await load(); render(); }
        }, 3000);
      }
    },
    todoist() { bindTodoist(c.todoist); },
    general() {
      const gen = $("#gen-form");
      gen.onchange = (e) => {
        const name = e.target.dataset?.toggle;
        if (name) gen.elements[name].disabled = !e.target.checked;
      };
      bindForm(gen, async (f) => {
        await put("/api/config/general", {
          timezone: f.get("timezone"),
          late_entry_until: f.get("late_entry_until"),
          workdays: f.getAll("workday").map(Number),
          morning_reminder: f.get("morning_reminder") ?? "",   // disabled inputs aren't submitted = off
          evening_reminder: f.get("evening_reminder") ?? "",
          weekly_summary: f.get("weekly_summary") === "on",
        });
        await load();
      });
    },
    backups() { loadBackups(); },
    security() {
      bindSecurity();
      const pw = $("#pw-form");
      if (pw) bindForm(pw, async (f) => {
        if (f.get("new") !== f.get("repeat")) throw new Error("The new passwords don't match.");
        await put("/api/password", { current: f.get("current"), new: f.get("new") });
        pw.reset();
      }, "Password changed ✓");
    },
    data() {
      $("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.href = "/login"; };
    },
  };
  binders[page]();
}

/* ---------- theme ---------- */
// Dark is the default; the button switches between dark and light.
const themeBtn = $("#theme-btn");
const currentTheme = () => (store.get("theme") === "light" ? "light" : "dark");
function paintThemeBtn() {
  const t = currentTheme();
  themeBtn.innerHTML = t === "light" ? ICON.sun : ICON.moon;
  themeBtn.title = t === "light" ? "Switch to dark" : "Switch to light";
}
themeBtn.onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  store.set("theme", next);
  document.documentElement.dataset.theme = next;
  paintThemeBtn();
};
paintThemeBtn();

// Spotlight: cards light up where the cursor is.
document.addEventListener("pointermove", (e) => {
  const card = e.target.closest?.(".card");
  if (!card) return;
  const r = card.getBoundingClientRect();
  card.style.setProperty("--mx", `${e.clientX - r.left}px`);
  card.style.setProperty("--my", `${e.clientY - r.top}px`);
}, { passive: true });

/* ---------- boot ---------- */
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (S) drawCharts(); }, 150);
});
window.addEventListener("hashchange", () => { hideTip(); render(); window.scrollTo(0, 0); });
// Pick up days logged from Telegram when you come back to the tab.
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible" || document.querySelector("textarea:focus")) return;
  const sig = () => S.entries.map((e) => e.updated_at).join();
  const before = S && sig();
  await load();
  if (sig() !== before) render();
});

load().then(render).catch((e) => {
  view.innerHTML = `<div class="card empty">Could not load: ${esc(e.message)}</div>`;
});
