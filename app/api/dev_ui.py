"""A stand-in login screen.

Served only when ``app/ui/`` holds no React build, so that a clean checkout
runs with nothing but Python installed. Phase 3 replaces it with the real
register. It is intentionally plain: its job is to prove the shell, the health
gate, the session token and the login path work end to end.
"""

from __future__ import annotations

DEV_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Register — sign in</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f5f7; --fg:#16181d; --card:#fff;
          --line:#d8dbe0; --accent:#1b6ef3; --bad:#c0392b; --muted:#6b7280; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111318; --fg:#e8eaed; --card:#191c22; --line:#2b2f38;
            --muted:#9aa1ad; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         background:var(--bg); color:var(--fg);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:32px; width:min(420px, 92vw); }
  h1 { margin:0 0 4px; font-size:20px; }
  p.sub { margin:0 0 24px; color:var(--muted); font-size:14px; }
  label { display:block; font-size:13px; font-weight:600; margin:16px 0 6px; }
  input { width:100%; padding:12px 14px; font-size:18px; border-radius:9px;
          border:1px solid var(--line); background:var(--bg); color:var(--fg); }
  input:focus { outline:2px solid var(--accent); outline-offset:1px; }
  button { width:100%; margin-top:24px; padding:13px; font-size:16px;
           font-weight:600; border:0; border-radius:9px; background:var(--accent);
           color:#fff; cursor:pointer; }
  button[disabled] { opacity:.55; cursor:progress; }
  .msg { margin-top:18px; padding:12px 14px; border-radius:9px; font-size:14px;
         display:none; white-space:pre-wrap; }
  .msg.error { display:block; background:#c0392b1a; color:var(--bad);
               border:1px solid #c0392b55; }
  .msg.ok { display:block; background:#1b6ef31a; border:1px solid #1b6ef355; }
  code { font-family: ui-monospace, Consolas, monospace; font-size:12px; }
  .foot { margin-top:20px; font-size:12px; color:var(--muted); }
</style>
</head>
<body>
  <main class="card">
    <h1>Sign in</h1>
    <p class="sub" id="terminal">&nbsp;</p>
    <form id="form" autocomplete="off">
      <label for="code">Employee code</label>
      <input id="code" name="code" inputmode="text" autocapitalize="characters"
             autofocus required>
      <label for="pin">PIN</label>
      <input id="pin" name="pin" type="password" inputmode="numeric"
             pattern="[0-9]*" required>
      <button id="submit" type="submit">Sign in</button>
    </form>
    <div class="msg" id="msg"></div>
    <p class="foot">Development login screen. The register arrives in phase 3.</p>
  </main>
<script>
  // The session token is handed to the webview in its URL and kept in memory
  // only — never localStorage (architecture §5).
  const TOKEN = new URLSearchParams(location.search).get("t") || "";
  history.replaceState(null, "", location.pathname);

  const msg = document.getElementById("msg");
  const show = (text, kind) => { msg.textContent = text; msg.className = "msg " + kind; };

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json",
                 "Authorization": "Bearer " + TOKEN, ...(options.headers || {}) },
    });
    const body = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.detail) || ("HTTP " + res.status));
    return body;
  }

  api("/health").then(h => {
    document.getElementById("terminal").textContent =
      `${h.store_code} · ${h.terminal_code} · schema v${h.schema_version}` +
      (h.cloud_configured ? "" : " · offline mode");
  }).catch(() => {});

  document.getElementById("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = document.getElementById("submit");
    button.disabled = true;
    show("", "");
    try {
      const s = await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          employee_code: document.getElementById("code").value.trim(),
          pin: document.getElementById("pin").value,
        }),
      });
      show(`Signed in as ${s.full_name} (${s.employee_code})`
           + `\\nStore: ${s.store_id}`
           + `\\nRoles: ${s.roles.join(", ") || "none"}`
           + `\\nPermissions: ${s.permissions.length}`
           + (s.offline ? "\\nAuthenticated offline from the local cache." : ""), "ok");
      document.getElementById("pin").value = "";
    } catch (err) {
      show(err.message, "error");
      document.getElementById("pin").value = "";
      document.getElementById("pin").focus();
    } finally {
      button.disabled = false;
    }
  });
</script>
</body>
</html>
"""

SPLASH_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Starting</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; height:100vh; display:grid; place-items:center;
         background:#111318; color:#e8eaed;
         font:16px system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { text-align:center; }
  .spin { width:34px; height:34px; margin:0 auto 18px; border-radius:50%;
          border:3px solid #2b2f38; border-top-color:#1b6ef3;
          animation:spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }
  p { margin:0; color:#9aa1ad; font-size:14px; }
  #detail { margin-top:8px; font-size:12px; color:#6b7280; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="spin"></div>
    <p>Opening the till&hellip;</p>
    <p id="detail">Applying database migrations</p>
  </div>
</body>
</html>
"""
