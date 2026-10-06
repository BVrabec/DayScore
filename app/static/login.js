"use strict";
/* Sign-in page: password, second factor, first-run setup and restore. */
const $ = (s) => document.querySelector(s);
const err = $("#error");
const pw = $("#pw-step");
let setup = false;

const showError = (text) => { err.textContent = text; err.hidden = !text; };
async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Something went wrong.");
  return data;
}
const done = () => { location.href = setup ? "/#/settings" : "/"; };

// A button that shows progress and errors while `fn` runs.
function busy(el, fn) {
  return async (e) => {
    e.preventDefault();
    const btn = el.tagName === "FORM" ? el.querySelector("button[type=submit]") : el;
    btn.disabled = true; showError("");
    try { await fn(); } catch (x) { showError(x.message); } finally { btn.disabled = false; }
  };
}

fetch("/api/auth").then((r) => r.json()).then((s) => {
  document.querySelectorAll(".app-name").forEach((n) => { n.textContent = s.app_name; });
  setup = s.setup_needed;
  if (setup) {
    $("#intro").textContent = "Welcome! Create a password to keep your journal private.";
    pw.password.placeholder = "New password (at least 8 characters)";
    pw.password.autocomplete = "new-password";
    pw.confirm.hidden = false;
    pw.confirm.required = true;
    pw.querySelector("button[type=submit]").textContent = "Create password";
    pw.querySelector(".setup-code").hidden = false;
    pw.setup_code.required = true;
  }
  pw.hidden = false;
  $("#restore-link").hidden = !setup;
  (setup ? pw.setup_code : pw.password).focus();
});

pw.addEventListener("submit", busy(pw, async () => {
  if (setup && pw.password.value !== pw.confirm.value) throw new Error("The passwords don't match.");
  const data = await post(setup ? "/api/setup" : "/api/login",
    setup ? { password: pw.password.value, setup_code: pw.setup_code.value } : { password: pw.password.value });
  if (!data.second_factor) return done();
  // Password was right: show the code step.
  pw.hidden = true;
  $("#intro").textContent = "Enter the code from your authenticator app.";
  $("#mfa-step").hidden = false;
  $("#totp-step").hidden = false;
  $("#totp-step").code.focus();
}));


const totp = $("#totp-step");
totp.addEventListener("submit", busy(totp, async () => { await post("/api/login/totp", { code: totp.code.value }); done(); }));
const recovery = $("#recovery-step");
recovery.addEventListener("submit", busy(recovery, async () => { await post("/api/login/recovery", { code: recovery.code.value }); done(); }));
$("#restore-link").addEventListener("click", () => {
  pw.hidden = true; $("#restore-link").hidden = true; $("#restore-step").hidden = false;
  restore.setup_code.value = pw.setup_code.value;
  $("#intro").textContent = "Choose a DayScore backup file (.db.gz.enc).";
});
const restore = $("#restore-step");
restore.addEventListener("submit", busy(restore, async () => {
  const buf = await restore.file.files[0].arrayBuffer();
  // Passwords go in headers, never in the URL (URLs end up in logs).
  const res = await fetch("/api/backups/restore/upload", {
    method: "POST", body: buf,
    headers: { "Content-Type": "application/octet-stream", "X-Backup-Password": restore.password.value,
               "X-Setup-Code": restore.setup_code.value },
  });
  const info = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(info.detail || "That backup can't be used.");
  restore.hidden = true;
  $("#intro").textContent = "This backup looks good. Restore it?";
  const box = $("#restore-preview");
  box.hidden = false;
  box.innerHTML = `<div class="bk-preview" style="text-align:left"><b></b><span></span><span>Settings and keys included</span></div>
    <p class="hint">After restoring, sign in with the password that was used when this backup was made.</p>
    <button class="btn" type="button" id="restore-go">Restore this backup</button>`;
  // Values from the file are set as text, never as HTML.
  box.querySelector("b").textContent = `${Number(info.days) || 0} days`;
  box.querySelector(".bk-preview span").textContent = info.days ? `${info.first_day} – ${info.last_day}` : "no days";
  $("#restore-go").addEventListener("click", busy($("#restore-go"), async () => {
    await post("/api/backups/restore/commit", { token: info.token, setup_code: restore.setup_code.value });
    location.href = "/login";
  }));
}));

$("#use-recovery").addEventListener("click", () => {
  recovery.hidden = false; $("#use-recovery").hidden = true; recovery.code.focus();
});
