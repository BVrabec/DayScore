"use strict";
/* Settings → Backups: schedule, password, locations, "Back up now", and restore.
   Uses helpers from app.js ($, esc, api, pill, bindForm, fmt). */

const BACKUP_FIELDS = {
  folder: {
    hint: `Any folder mapped into the container: a NAS share mounted on the host, a USB disk…
      Map it first in <code>docker-compose.override.yml</code>, e.g.
      <code>volumes: ["/mnt/nas/dayscore:/backups"]</code>, then enter <code>/backups</code> here.`,
    fields: [["path", "Folder inside the container", "/backups"]],
  },
  smb: {
    hint: "A Windows or NAS share, reached directly with a username and password.",
    fields: [["server", "Server", "nas.local or 192.168.1.20"], ["share", "Share", "backups"],
             ["folder", "Folder (optional)", "DayScore"], ["username", "Username", ""], ["password", "Password", "", "password"]],
  },
  webdav: {
    hint: `For Nextcloud use <code>https://your-cloud/remote.php/dav/files/USERNAME/DayScore</code> and an
      <b>app password</b> (Nextcloud → Settings → Security). Copies are always encrypted.`,
    fields: [["url", "WebDAV address", "https://cloud.example.com/remote.php/dav/files/me/DayScore"],
             ["username", "Username", ""], ["password", "Password (app password)", "", "password"]],
  },
  s3: {
    hint: "Backblaze B2, Cloudflare R2, AWS S3, Wasabi, MinIO… Create a bucket and an access key first. Copies are always encrypted.",
    fields: [["endpoint", "Endpoint", "https://s3.eu-central-003.backblazeb2.com"], ["bucket", "Bucket", "my-dayscore-backups"],
             ["region", "Region (optional)", "eu-central-003"], ["access_key", "Access key ID", ""],
             ["secret_key", "Secret key", "", "password"], ["prefix", "Folder in the bucket (optional)", "dayscore"]],
  },
  rclone: {
    hint: `Uses <a href="https://rclone.org" target="_blank" rel="noopener">rclone</a>. On your computer:
      <ol class="steps"><li><a href="https://rclone.org/install/" target="_blank" rel="noopener">Install rclone</a>.</li>
      <li>Run <code>rclone config</code> and add a remote for
        <a href="https://rclone.org/drive/" target="_blank" rel="noopener">Google Drive</a>,
        <a href="https://rclone.org/dropbox/" target="_blank" rel="noopener">Dropbox</a> or
        <a href="https://rclone.org/onedrive/" target="_blank" rel="noopener">OneDrive</a> (it opens a browser to log in).</li>
      <li>Run <code>rclone config show NAME</code> and paste everything it prints below.</li></ol>
      Only Google Drive, Dropbox and OneDrive remotes are accepted. Copies are always encrypted.`,
    fields: [["config", "rclone config", "[gdrive]\ntype = drive\nscope = drive.file\ntoken = {…}", "textarea"],
             ["folder", "Folder", "DayScore"]],
  },
};

let backupData = null;

const fmtSize = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const fmtWhen = (isoString) => new Date(isoString).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function backupCard() {
  return `<section class="card" id="bk-card"><div class="card-head"><h2>Backups</h2></div>
    <p class="hint"><span class="spinner dark"></span> Loading…</p></section>`;
}

async function loadBackups() {
  try {
    backupData = await api("/api/backups");
  } catch (err) {
    const card = $("#bk-card");
    if (card) card.innerHTML = `<h2>Backups</h2><p class="error">${esc(err.message)}</p>`;
    return;
  }
  renderBackups();
}

function backupStatus(runs) {
  const last = runs.find((r) => !r.skipped);
  if (!last) return pill("off", "No backups yet");
  const ok = last.results.every((r) => r.ok);
  return ok ? pill("ok", `Last backup ${fmtWhen(last.at)}`) : pill("wait", "Last backup had problems");
}

function locationBlock(kind, loc, info) {
  const def = BACKUP_FIELDS[kind];
  const field = ([name, label, placeholder, type]) => {
    const saved = loc[`${name}_set`];
    const ph = saved ? "Saved. Type to replace" : placeholder;
    const value = type === "password" || type === "textarea" ? "" : esc(loc[name] || "");
    return type === "textarea"
      ? `<label class="field">${label}<textarea name="${kind}.${name}" rows="5" spellcheck="false" placeholder="${esc(ph)}"></textarea></label>`
      : `<label class="field">${label}<input name="${kind}.${name}" type="${type || "text"}" value="${value}"
           placeholder="${esc(ph)}" autocomplete="off" spellcheck="false"></label>`;
  };
  return `<div class="bk-loc ${loc.enabled ? "on" : ""}" data-kind="${kind}">
    <label class="check-row bk-toggle"><input type="checkbox" name="${kind}.enabled" ${loc.enabled ? "checked" : ""}>
      <span><b>${esc(info.label)}</b>${info.offsite ? ' <span class="hint">· off-site, encrypted</span>' : ""}</span></label>
    <div class="bk-fields">
      <p class="hint">${def.hint}</p>
      ${def.fields.map(field).join("")}
      <div class="row-btns"><button class="btn ghost small" type="button" data-bk-test="${kind}">Test connection</button>
        <span class="msg" data-bk-msg="${kind}"></span></div>
    </div>
  </div>`;
}

function runsList(runs) {
  const items = runs.filter((r) => !r.skipped).slice(0, 5);
  if (!items.length) return '<p class="hint">No backups yet. Press <b>Back up now</b> to make the first one.</p>';
  const label = (k) => (k === "local" ? "This server" : backupData.kinds[k]?.label || k);
  return `<ul class="bk-runs">${items.map((r) => `<li>
      <span class="bk-when">${fmtWhen(r.at)}${r.trigger === "manual" ? ' <span class="hint">· manual</span>' : ""}</span>
      <span class="bk-results">${r.results.map((x) => x.ok
        ? `<span class="bk-chip ok" title="${esc(x.name)}">✓ ${esc(label(x.location))} · ${fmtSize(x.size)}</span>`
        : `<span class="bk-chip bad" title="${esc(x.error)}">✗ ${esc(label(x.location))}</span>`).join("")}</span>
      ${r.results.filter((x) => !x.ok).map((x) => `<span class="error">${esc(label(x.location))}: ${esc(x.error)}</span>`).join("")}
    </li>`).join("")}</ul>`;
}

function renderBackups() {
  const card = $("#bk-card");
  if (!card) return;
  const { config: c, runs, kinds, keep } = backupData;
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const sourceOptions = [["local", "This server"], ...Object.entries(kinds).filter(([k]) => c.locations[k].enabled).map(([k, v]) => [k, v.label])];
  card.innerHTML = `
    <div class="card-head"><h2>Backups</h2>${backupStatus(runs)}</div>
    <p class="sub">Compressed, encrypted copies of everything: your days, settings and keys. Old copies are cleaned up
      automatically: the last ${keep.daily} daily, ${keep.weekly} weekly and ${keep.monthly} monthly are kept.</p>
    ${c.password_set ? "" : `<p class="callout-inline">${c.server_key
      ? "Set a backup password to keep copies anywhere else. Until then only this server keeps a copy, and it can only be restored here."
      : "Set a backup password to encrypt your backups and keep copies anywhere else."}</p>`}

    <form id="bk-form" class="fields">
      <div class="field">Schedule
        <div class="bk-row">
          <select name="schedule">${[["off", "Off"], ["daily", "Every day"], ["weekly", "Every week"]].map(([v, l]) =>
            `<option value="${v}" ${c.schedule === v ? "selected" : ""}>${l}</option>`).join("")}</select>
          <select name="weekday" ${c.schedule === "weekly" ? "" : "hidden"}>${days.map((d, i) =>
            `<option value="${i}" ${Number(c.weekday) === i ? "selected" : ""}>${d}</option>`).join("")}</select>
          <input type="time" name="time" value="${esc(c.time)}" required>
        </div>
      </div>
      <label class="field">Backup password
        <input type="password" name="password" autocomplete="new-password" minlength="8"
          placeholder="${c.password_set ? "Saved. Type to replace" : "At least 8 characters"}">
        <span class="hint">Every backup is encrypted with it. Keep it somewhere safe (a password manager):
          without it a backup can't be restored, also not on a new server.</span>
      </label>

      <div class="field">Where to keep copies
        <div class="bk-loc on"><label class="check-row bk-toggle"><input type="checkbox" checked disabled>
          <span><b>This server</b> <span class="hint">· always on · same disk as your data, so also add a place below</span></span></label></div>
        ${Object.entries(kinds).map(([k, info]) => locationBlock(k, c.locations[k], info)).join("")}
      </div>
      <div class="form-foot"><span class="msg"></span><button class="btn" type="submit">Save</button></div>
    </form>

    <div class="bk-section">
      <div class="row-btns"><button class="btn small" type="button" id="bk-now">Back up now</button><span class="msg" id="bk-now-msg"></span></div>
      <h3 class="bk-h">Recent backups</h3>
      ${runsList(runs)}
    </div>

    <div class="bk-section">
      <h3 class="bk-h">Restore</h3>
      <p class="hint">Restoring replaces everything with the backup. A safety copy of your current data is made
        first, so it can be undone.</p>
      <div class="bk-row">
        <select id="bk-source">${sourceOptions.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select>
        <button class="btn ghost small" type="button" id="bk-show">Show backups</button>
        <label class="btn ghost small bk-file">Restore from a file…<input type="file" id="bk-file" hidden></label>
      </div>
      <div id="bk-list"></div>
    </div>`;
  bindBackups();
  paintSettingsDots();   // the menu's Backups dot follows the latest state
}

function collectBackupForm(form) {
  const f = new FormData(form);
  const locations = {};
  for (const kind of Object.keys(BACKUP_FIELDS)) {
    locations[kind] = { enabled: form.elements[`${kind}.enabled`].checked };
    for (const [name] of BACKUP_FIELDS[kind].fields) locations[kind][name] = f.get(`${kind}.${name}`) ?? "";
  }
  return {
    schedule: f.get("schedule"), time: f.get("time"), weekday: Number(f.get("weekday") || 6),
    password: f.get("password") || "", locations,
  };
}

function bindBackups() {
  const form = $("#bk-form");
  form.schedule.onchange = () => { form.weekday.hidden = form.schedule.value !== "weekly"; };
  form.addEventListener("change", (e) => {
    const kind = e.target.name?.endsWith(".enabled") && e.target.closest(".bk-loc")?.dataset.kind;
    if (kind) e.target.closest(".bk-loc").classList.toggle("on", e.target.checked);
  });
  bindForm(form, async () => {
    backupData = await api("/api/backups/config", { method: "PUT", body: JSON.stringify(collectBackupForm(form)) });
    renderBackups();
    const msg = $("#bk-form > .form-foot .msg"); msg.className = "msg saved"; msg.textContent = "Saved ✓";
    return false;
  });

  form.addEventListener("click", async (e) => {
    const kind = e.target.dataset?.bkTest;
    if (!kind) return;
    const msg = form.querySelector(`[data-bk-msg="${kind}"]`);
    const cfg = collectBackupForm(form).locations[kind];
    msg.className = "msg"; msg.innerHTML = '<span class="spinner dark"></span> Testing…';
    try {
      await api(`/api/backups/test/${kind}`, { method: "POST", body: JSON.stringify(cfg) });
      msg.className = "msg saved"; msg.textContent = "Works ✓";
    } catch (err) {
      msg.className = "msg error"; msg.textContent = err.message;
    }
  });

  $("#bk-now").onclick = async (e) => {
    const msg = $("#bk-now-msg");
    e.target.disabled = true; msg.className = "msg"; msg.innerHTML = '<span class="spinner dark"></span> Backing up…';
    try {
      backupData = await api("/api/backups/run", { method: "POST" });
      renderBackups();
    } catch (err) {
      msg.className = "msg error"; msg.textContent = err.message; e.target.disabled = false;
    }
  };

  $("#bk-show").onclick = () => showBackupList($("#bk-source").value);
  $("#bk-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (file) await startRestore((password) => file.arrayBuffer().then((buf) => uploadForRestore(buf, password)), file.name.endsWith(".enc"));
    e.target.value = "";
  };
}

async function showBackupList(kind) {
  const box = $("#bk-list");
  box.innerHTML = '<p class="hint"><span class="spinner dark"></span> Loading backups…</p>';
  let items;
  try {
    items = await api(`/api/backups/list/${encodeURIComponent(kind)}`);
  } catch (err) {
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  if (!items.length) { box.innerHTML = '<p class="hint">No backups there yet.</p>'; return; }
  box.innerHTML = `<ul class="bk-files">${items.map((i) => `<li>
      <span><b>${fmtWhen(i.modified)}</b> <span class="hint">· ${fmtSize(i.size)}${i.encrypted ? " · 🔒 encrypted" : ""}${i.safety_copy ? " · safety copy" : ""}</span></span>
      <span class="row-btns">
        <a class="btn ghost small" href="/api/backups/download/${encodeURIComponent(kind)}/${encodeURIComponent(i.name)}">Download</a>
        <button class="btn small" type="button" data-restore="${esc(i.name)}" data-enc="${i.encrypted ? 1 : 0}">Restore</button>
      </span></li>`).join("")}</ul>`;
  box.onclick = (e) => {
    const name = e.target.dataset?.restore;
    if (!name) return;
    startRestore((password) => api("/api/backups/restore/prepare", { method: "POST", body: JSON.stringify({ kind, name, password }) }),
      e.target.dataset.enc === "1");
  };
}

function uploadForRestore(buf, password = "") {
  // The password goes in a header, never in the URL (URLs end up in logs).
  return fetch("/api/backups/restore/upload", {
    method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Backup-Password": password }, body: buf,
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Upload failed.");
    return data;
  });
}

/* Ask for the backup password only when needed, show a preview, then confirm with the account password. */
async function startRestore(prepare, encrypted) {
  const dlg = $("#day-dialog");
  const body = $("#dialog-body");
  const needsPassword = encrypted && !backupData.config.password_set && !backupData.config.server_key;
  const run = async (password = "") => {
    body.innerHTML = '<p class="hint"><span class="spinner dark"></span> Checking the backup…</p>';
    if (!dlg.open) dlg.showModal();
    let info;
    try {
      info = await prepare(password);
    } catch (err) {
      if (/password/i.test(err.message)) return askBackupPassword(err.message);
      body.innerHTML = `<h2 style="margin:0 0 8px">Can't restore this backup</h2><p class="error">${esc(err.message)}</p>`;
      return;
    }
    showPreview(info);
  };
  const askBackupPassword = (message = "") => {
    body.innerHTML = `<form id="bk-pw" class="fields"><h2 style="margin:0">Backup password</h2>
      <p class="sub" style="margin:0">This backup is encrypted. Enter the backup password it was made with.</p>
      ${message ? `<p class="error" style="margin:0">${esc(message)}</p>` : ""}
      <input type="password" name="password" class="field-input" required autocomplete="off">
      <div class="row-btns" style="justify-content:flex-end"><button class="btn small" type="submit">Continue</button></div></form>`;
    if (!dlg.open) dlg.showModal();
    $("#bk-pw").onsubmit = (e) => { e.preventDefault(); run(e.target.password.value); };
    $("#bk-pw input").focus();
  };
  const showPreview = (info) => {
    const days = Number(info.days) || 0;
    const range = days ? `${fmt(String(info.first_day), { day: "numeric", month: "short", year: "numeric" })} – ${fmt(String(info.last_day), { day: "numeric", month: "short", year: "numeric" })}` : "no days";
    body.innerHTML = `<form id="bk-confirm" class="fields"><h2 style="margin:0">Restore this backup?</h2>
      <div class="bk-preview"><b>${days} day${days === 1 ? "" : "s"}</b><span>${esc(range)}</span>
        <span>Settings, keys${info.has_2fa ? " and two-factor" : ""} included</span></div>
      <p class="sub" style="margin:0">Everything here is replaced by this backup. A safety copy of your current data is
        made first. <b>Afterwards, sign in with the password that was used when this backup was made.</b></p>
      <label class="field">Your current DayScore password<input type="password" name="account" required autocomplete="current-password"></label>
      <p class="error" hidden></p>
      <div class="row-btns" style="justify-content:flex-end">
        <button class="btn ghost small" type="button" id="bk-cancel">Cancel</button>
        <button class="btn small" type="submit">Restore</button></div></form>`;
    $("#bk-cancel").onclick = () => dlg.close();
    $("#bk-confirm").onsubmit = async (e) => {
      e.preventDefault();
      const err = e.target.querySelector(".error");
      e.target.querySelector("button[type=submit]").disabled = true;
      try {
        await api("/api/backups/restore/commit", { method: "POST", body: JSON.stringify({ token: info.token, account_password: e.target.account.value }) });
        body.innerHTML = '<h2 style="margin:0 0 8px">Restored ✓</h2><p class="sub">Taking you to the sign-in page…</p>';
        setTimeout(() => { location.href = "/login"; }, 1500);
      } catch (x) {
        err.textContent = x.message; err.hidden = false;
        e.target.querySelector("button[type=submit]").disabled = false;
      }
    };
  };
  if (needsPassword) askBackupPassword();
  else run();
}
