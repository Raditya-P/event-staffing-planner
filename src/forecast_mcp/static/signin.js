/* Sign-in page: one card with several views (sign in, create an account, email code, password reset, Claude
   connector). Each form posts JSON to /auth/*; the server passes it on to the sign-in provider. */
(function () {
  const views = document.querySelectorAll("[data-view]");
  const msg = document.getElementById("msg");
  const params = new URLSearchParams(location.search);
  const resetToken = location.pathname === "/reset-password" ? params.get("token") : null;
  const titles = {
    signin: "Sign in", signup: "Create an account", verify: "Check your email", forgot: "Reset your password",
    "forgot-sent": "Check your email", reset: "Set a new password", "reset-done": "Password updated",
    connect: "Connect Claude", "connect-failed": "Claude was not connected",
  };
  const errors = {
    expired: "The sign-in took too long. Try again.",
    cancelled: "Google sign-in was cancelled.",
    failed: "Google sign-in did not complete. Try again or use your email and password.",
  };
  let email = "";

  function note(text, kind) {
    msg.hidden = !text;
    msg.textContent = text || "";
    msg.className = "alert alert-soft text-sm " + (kind === "info" ? "alert-info" : "alert-error");
  }

  function show(name, text, kind) {
    for (const v of views) v.hidden = v.dataset.view !== name;
    for (const el of document.querySelectorAll("[data-email]")) el.textContent = email;
    for (const input of document.querySelectorAll('input[name="email"]')) if (email && !input.value) input.value = email;
    document.title = (titles[name] || "Sign in") + " · Event Staffing Planner";
    note(text, kind);
    const first = [...document.querySelectorAll(`[data-view="${name}"] input`)].find((el) => !el.value);
    if (first && !("ontouchstart" in window)) first.focus();
  }

  async function post(path, body) {
    const resp = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await resp.json(); } catch (_) { /* empty answer */ }
    if (!resp.ok) throw new Error(data.error || "Something went wrong. Try again.");
    return data;
  }

  function next(data) {
    if (data.redirect) { location.assign(data.redirect); return; }
    if (data.step === "verify") { email = data.email || email; show("verify"); }
  }

  async function busy(button, work) {
    button.disabled = true;
    const label = button.textContent;
    button.innerHTML = '<span class="loading loading-spinner loading-sm"></span>';
    try { await work(); } catch (err) { note(err.message); } finally { button.disabled = false; button.textContent = label; }
  }

  const handlers = {
    password: async (f) => { email = f.email; next(await post("/auth/password", f)); },
    signup: async (f) => { email = f.email; next(await post("/auth/signup", f)); },
    verify: async (f) => next(await post("/auth/verify", f)),
    forgot: async (f) => { email = f.email; await post("/auth/forgot", f); show("forgot-sent"); },
    reset: async (f) => {
      const data = await post("/auth/reset", { token: resetToken, password: f.password });
      email = data.email || email;
      history.replaceState(null, "", "/login");
      show("reset-done");
    },
  };

  for (const form of document.querySelectorAll("[data-form]")) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const fields = Object.fromEntries(new FormData(form));
      const missing = [...form.querySelectorAll("[required]")].find((el) => !el.value.trim());
      if (missing) { note("Fill in " + missing.closest("label").querySelector("span").textContent.toLowerCase() + "."); missing.focus(); return; }
      busy(form.querySelector('[type="submit"]'), () => handlers[form.dataset.form](fields));
    });
  }

  for (const el of document.querySelectorAll("[data-go]")) {
    el.addEventListener("click", () => show(el.dataset.go));
  }

  document.querySelector('[data-action="resend"]').addEventListener("click", (e) => {
    busy(e.currentTarget, async () => { await post("/auth/resend"); note("A new code is on its way.", "info"); });
  });
  document.querySelector('[data-action="connect"]').addEventListener("click", (e) => {
    busy(e.currentTarget, async () => next(await post("/auth/connect")));
  });
  document.querySelector('[data-action="switch"]').addEventListener("click", (e) => {
    busy(e.currentTarget, async () => { await post("/auth/switch"); email = ""; show("signin"); });
  });

  if (resetToken) { show("reset"); return; }
  if (params.get("error") === "connect") { show("connect-failed"); return; }
  fetch("/auth/status", { credentials: "same-origin" }).then((r) => r.json()).then((st) => {
    if (st.signed_in && st.connect) { email = st.email || ""; show("connect"); return; }
    if (params.get("view") === "verify" && st.verify_email) { email = st.verify_email; show("verify"); return; }
    show(params.get("view") === "signup" ? "signup" : "signin", errors[params.get("error")]);
  }).catch(() => show("signin"));
})();
