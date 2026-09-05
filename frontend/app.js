/* CampusPool frontend. Deliberately dumb (HLD s4.2): renders /api/state, sends
   chat messages, opens Razorpay Checkout for an EXECUTING intent, posts the
   checkout response to /verify. Computes nothing and decides nothing — every
   number on screen is a value the API returned. The only arithmetic below is
   SVG geometry for the donut, which draws API numbers; it displays none of
   its own. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // ---- formatting: paise -> "₹1,500.00" is presentation of an API integer, not a computed figure
  const rupees = (paise) => paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rupees0 = (paise) => paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  const timeOf = (iso) => { try { return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch { return iso; } };

  const state = { userId: null, history: [], lastSeq: 0, keyId: null, busy: false };

  // ---- HTTP
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch { /* no body */ }
    if (!res.ok) { const err = new Error((body && (body.detail?.[0]?.msg || body.detail)) || res.statusText); err.status = res.status; err.body = body; throw err; }
    return body;
  }

  // ---- toasts
  const toastWrap = document.createElement("div"); toastWrap.className = "toast-wrap"; document.body.appendChild(toastWrap);
  function toast(text, kind = "ok") { const t = document.createElement("div"); t.className = `toast ${kind}`; t.textContent = text; toastWrap.appendChild(t); setTimeout(() => t.remove(), 4200); }

  // ---- reveal on scroll + nav shadow
  const io = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: 0.12 });
  $$(".reveal").forEach((el) => io.observe(el));
  const nav = $("#nav");
  addEventListener("scroll", () => nav.classList.toggle("scrolled", scrollY > 8), { passive: true });
  $("#year").textContent = new Date().getFullYear();

  // ---- users
  async function loadUsers() {
    const { users } = await api("/api/users");
    const sel = $("#userSelect");
    sel.innerHTML = "";
    users.forEach((u) => { const o = document.createElement("option"); o.value = u.user_id; o.textContent = `${u.name}`; sel.appendChild(o); });
    const saved = (() => { try { return localStorage.getItem("cp.user"); } catch { return null; } })();
    state.userId = users.some((u) => u.user_id === saved) ? saved : (users[0] && users[0].user_id);
    sel.value = state.userId;
    sel.onchange = () => { state.userId = sel.value; state.history = []; state.lastSeq = 0; try { localStorage.setItem("cp.user", state.userId); } catch {} $("#chatLog").innerHTML = ""; addMsg("assistant", "Switched user. Ask me anything about this account."); refreshAll(); };
  }

  // ---- state panel
  function renderState(s) {
    $("#stateStamp").textContent = "verified " + new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    const b = s.balances_paise || {};
    $("#balEmergency").textContent = rupees(b.emergency_savings);
    $("#balRewards").textContent = rupees(b.rewards);
    $("[data-stat=emergency]").textContent = rupees0(b.emergency_savings);

    const sp = s.spending_this_month;
    if (sp) {
      $("#spendText").textContent = `${rupees0(sp.used_paise)} of ${rupees0(sp.limit_paise)}`;
      $("#spendRemaining").textContent = `${rupees0(sp.remaining_paise)} remaining`;
      $("#spendPct").textContent = `${sp.pct_used}% used`;               // pct comes from the API
      const fill = $("#spendFill"); fill.style.width = `${Math.min(100, sp.pct_used)}%`; fill.classList.toggle("hot", sp.pct_used >= 80);
      $("[data-stat=spent]").textContent = `${rupees0(sp.used_paise)} / ${rupees0(sp.limit_paise)}`;
    }

    const goals = $("#goals"); goals.innerHTML = "";
    (s.goals || []).forEach((g) => {
      const el = document.createElement("div"); el.className = "goal" + (g.status === "paused" ? " paused" : "");
      el.innerHTML = `<div class="goal-top"><strong>${esc(g.label)}</strong><span class="status ${g.status === "paused" ? "EXCEPTION" : "ALLOWED"}">${esc(g.status)}</span></div>
        <div class="meter"><span class="meter-fill" style="width:${Math.min(100, g.pct_complete)}%"></span></div>
        <div class="goal-foot"><span>${rupees0(g.current_paise)} of ${rupees0(g.target_paise)}</span><span>${g.pct_complete}%</span></div>`;
      goals.appendChild(el);
    });

    renderPending(s.pending_actions || []);
    renderDonut(b, sp);
  }

  function renderPending(list) {
    $("#pendingCount").textContent = String(list.length);
    const box = $("#pendingList");
    if (!list.length) { box.innerHTML = `<p class="empty">Nothing waiting. When the agent proposes something above your approval threshold, it appears here — and only your tap can approve it.</p>`; return; }
    box.innerHTML = "";
    list.forEach((i) => {
      const el = document.createElement("div"); el.className = "intent";
      const payable = ["ALLOWED", "APPROVED", "EXECUTING"].includes(i.status);
      el.innerHTML = `<div class="intent-top"><span class="intent-amt">${rupees(i.amount_paise)}</span><span class="status ${esc(i.status)}">${esc(i.status.replace(/_/g, " "))}</span></div>
        <div class="intent-purpose">${esc(i.type)} · ${esc(i.purpose)}</div>
        <div class="intent-actions">
          ${i.needs_your_approval ? `<button class="btn btn-accent btn-sm" data-act="approve" data-id="${esc(i.intent_id)}">Approve</button><button class="btn btn-ghost btn-sm" data-act="deny" data-id="${esc(i.intent_id)}">Deny</button>` : ""}
          ${payable ? `<button class="btn btn-primary btn-sm" data-act="pay" data-id="${esc(i.intent_id)}">${i.status === "EXECUTING" ? "Resume payment" : "Pay with Razorpay (test)"}</button>` : ""}
        </div>`;
      box.appendChild(el);
    });
  }

  $("#pendingList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]"); if (!btn) return;
    const id = btn.dataset.id, act = btn.dataset.act; btn.disabled = true;
    try {
      if (act === "approve" || act === "deny") {
        const r = await api(`/api/intents/${id}/${act}`, { method: "POST", body: JSON.stringify({ user_id: state.userId }) });
        toast(`${act === "approve" ? "Approved" : "Denied"} — status ${r.status}`);
      } else if (act === "pay") {
        await payIntent(id);
      }
    } catch (err) { toast(err.message || "Request failed", "bad"); }
    finally { refreshAll(); }
  });

  // ---- checkout (HLD s6.4): execute -> Razorpay Checkout -> /api/checkout/verify
  async function payIntent(intentId) {
    const ex = await api(`/api/intents/${intentId}/execute`, { method: "POST", body: JSON.stringify({ user_id: state.userId }) });
    toast(ex.reused_existing_order ? `Resuming order ${ex.order_id}` : `Order ${ex.order_id} created (test mode)`);
    if (typeof Razorpay === "undefined") { toast("Razorpay Checkout script did not load (CSP/offline?)", "bad"); return; }
    await new Promise((resolve) => {
      const rzp = new Razorpay({
        key: ex.key_id, order_id: ex.order_id, amount: ex.amount_paise, currency: ex.currency,
        name: "CampusPool (DEMO — Test Mode)", description: "Synthetic demo · no real money",
        theme: { color: "#17b26a" },
        handler: async (resp) => {
          try {
            const v = await api("/api/checkout/verify", { method: "POST", body: JSON.stringify(resp) });
            toast(v.ok ? `Payment verified — intent ${v.intent_status}` : `Payment ${v.payment_status}; the webhook or reconciliation will settle it`, v.ok ? "ok" : "bad");
          } catch (err) { toast("Verify failed: " + err.message, "bad"); }
          resolve();
        },
        modal: { ondismiss: () => { toast("Checkout closed. A completed payment still settles via webhook."); resolve(); } },
      });
      rzp.on("payment.failed", () => { toast("Razorpay reported a failed attempt (browser event). The server waits for Razorpay's confirmation.", "bad"); });
      rzp.open();
    });
  }

  // ---- donut: SVG geometry over API integers; no number is displayed that the API didn't return
  const DONUT_SERIES = [
    { key: "emergency_savings", label: "Emergency savings", note: "protected · never spendable by the agent", color: "#17b26a" },
    { key: "rewards", label: "Rewards", note: "partner-funded · promotions, not advice", color: "#0e7a4c" },
    { key: "spent_this_month", label: "Spent this month", note: "discretionary is a spend tracker, not a balance (decision D2.1)", color: "#9fd7bd" },
  ];
  function renderDonut(balances, spending) {
    const segs = $("#donutSegs"); segs.innerHTML = "";
    const legend = $("#legend"); legend.innerHTML = "";
    const figures = { ...balances, spent_this_month: spending ? spending.used_paise : null };
    const vals = DONUT_SERIES.map((s) => Math.max(0, figures[s.key] || 0));
    const total = vals.reduce((a, b) => a + b, 0);
    const C = 2 * Math.PI * 78; let offset = 0;
    DONUT_SERIES.forEach((s, i) => {
      const frac = total ? vals[i] / total : 0;          // geometry only
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("class", "donut-seg"); c.setAttribute("cx", 100); c.setAttribute("cy", 100); c.setAttribute("r", 78);
      c.setAttribute("stroke", s.color); c.setAttribute("stroke-dasharray", `${frac * C} ${C}`); c.setAttribute("stroke-dashoffset", String(-offset));
      segs.appendChild(c); offset += frac * C;
      const li = document.createElement("li");
      li.innerHTML = `<span class="sw" style="background:${s.color}"></span><span>${s.label}<small>${s.note}</small></span><span class="v">${rupees(figures[s.key])}</span>`;
      legend.appendChild(li);
    });
    // Centre figure: an API value verbatim (plan Phase 6 item 4 - no client-side
    // math on anything displayed). `total` above is ring geometry only.
    $("#donutTotal").textContent = rupees0(balances.emergency_savings);
  }

  // ---- chat
  function addMsg(role, text, meta) {
    const log = $("#chatLog"); const el = document.createElement("div"); el.className = `msg ${role}`;
    el.innerHTML = `<p>${esc(text)}</p>${meta ? `<span class="meta">${esc(meta)}</span>` : ""}`;
    log.appendChild(el); log.scrollTop = log.scrollHeight; return el;
  }
  $("#chatForm").addEventListener("submit", async (e) => {
    e.preventDefault(); if (state.busy) return;
    const input = $("#chatInput"); const text = input.value.trim(); if (!text) return;
    input.value = ""; await sendChat(text);
  });
  $("#chips").addEventListener("click", (e) => { const b = e.target.closest("button[data-q]"); if (b && !state.busy) sendChat(b.dataset.q); });

  async function sendChat(text) {
    state.busy = true; $("#chatSend").disabled = true;
    addMsg("user", text);
    const pending = addMsg("assistant", ""); pending.classList.add("pending"); pending.innerHTML = `<span class="dots"><span></span><span></span><span></span></span>`;
    const t0 = performance.now();
    try {
      const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ user_id: state.userId, message: text, history: state.history }) });
      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      pending.remove();
      const meta = `${r.degraded ? "assistant unavailable · verified numbers shown" : `${r.steps} step${r.steps === 1 ? "" : "s"}`} · ${secs}s`;
      addMsg(r.degraded ? "system" : "assistant", r.reply, meta);
      state.history.push({ role: "user", content: text }, { role: "assistant", content: r.reply });
      if (state.history.length > 20) state.history = state.history.slice(-20);
      if (r.state) renderState(r.state);
    } catch (err) {
      pending.remove();
      addMsg("system", err.status === 503 ? "The database was busy for a moment — nothing was executed. Please try again." : `Request failed: ${err.message}`);
    } finally { state.busy = false; $("#chatSend").disabled = false; refreshTrust(); }
  }

  // ---- audit feed + chain + exceptions
  const MONEY = /^(intent:|ledger_append|tool:create_payment_intent|forced_policy_check)/;
  const BLOCKED = /^(blocked_|invalid_|repeated_|untrusted_|exception_opened|tool_error|parrot_retry|unkept_promise)/;
  function renderAudit(data) {
    const pill = $("#chainPill");
    pill.textContent = data.chain.ok ? `chain intact · ${data.chain.checked} entries` : `CHAIN BROKEN at ${data.chain.reason}`;
    pill.className = "pill " + (data.chain.ok ? "" : "pill-danger");
    $("[data-stat=chain]").textContent = data.chain.ok ? `intact · ${data.chain.checked}` : "broken";
    const feed = $("#auditFeed"); feed.innerHTML = "";
    const newest = data.events[0] ? data.events[0].seq : 0;
    data.events.forEach((ev) => {
      const el = document.createElement("div"); el.className = "ev" + (ev.seq > state.lastSeq && state.lastSeq ? " new" : "");
      const cls = BLOCKED.test(ev.action) ? "blocked" : MONEY.test(ev.action) ? "money" : "";
      const pr = ev.policy_result;
      const detail = pr && pr.decision ? `${pr.decision}${pr.rule ? " · " + pr.rule : ""}${pr.reason ? " — " + pr.reason : ""}` : "";
      el.innerHTML = `<span class="seq">#${ev.seq}</span><span class="actor ${esc(ev.actor)}">${esc(ev.actor)}</span><span class="action ${cls}">${esc(ev.action)}</span><span class="time">${timeOf(ev.created_at)}</span>${detail ? `<span class="detail">${esc(detail)}</span>` : ""}`;
      feed.appendChild(el);
    });
    state.lastSeq = newest;
  }
  function renderExceptions(data) {
    const list = data.exceptions; $("#excCount").textContent = `${list.length} open`;
    $("#excCount").className = "pill " + (list.length ? "pill-warn" : "pill-muted");
    const box = $("#excList");
    if (!list.length) { box.innerHTML = `<p class="empty">No open exceptions. Unknown orders, bad signatures, amount mismatches and reconciliation timeouts land here for a human — nothing auto-corrects.</p>`; return; }
    box.innerHTML = "";
    list.forEach((x) => {
      const el = document.createElement("div"); el.className = "exc";
      el.innerHTML = `<div class="intent-top"><span class="kind">${esc(x.kind)}</span><span class="time muted">${timeOf(x.created_at)}</span></div>
        ${x.intent_id ? `<div class="intent-purpose">intent ${esc(x.intent_id)}</div>` : ""}
        <pre>${esc(JSON.stringify(x.detail, null, 1))}</pre>
        <form data-id="${esc(x.exception_id)}"><input placeholder="Resolution note (who checked, what was found)" required minlength="3"><button class="btn btn-ghost btn-sm" type="submit">Resolve</button></form>`;
      box.appendChild(el);
    });
  }
  $("#excList").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; const note = f.querySelector("input").value.trim();
    try { await api(`/api/exceptions/${f.dataset.id}/resolve`, { method: "POST", body: JSON.stringify({ note }) }); toast("Exception resolved (recorded in the audit trail)"); }
    catch (err) { toast(err.message, "bad"); }
    refreshTrust();
  });

  async function refreshTrust() {
    try { renderAudit(await api(`/api/audit?user_id=${encodeURIComponent(state.userId)}&limit=80`)); } catch { /* keep last */ }
    try { renderExceptions(await api("/api/exceptions")); } catch { /* keep last */ }
  }
  async function refreshState() {
    try { renderState(await api(`/api/state/${encodeURIComponent(state.userId)}`)); } catch (err) { toast("Could not load state: " + err.message, "bad"); }
  }
  async function refreshAll() { await Promise.all([refreshState(), refreshTrust()]); }

  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // ---- boot
  (async () => {
    try { await loadUsers(); await refreshAll(); }
    catch (err) { toast("Backend not reachable: " + err.message, "bad"); }
    setInterval(refreshTrust, 6000);
    setInterval(refreshState, 15000);
    try { const h = await api("/health"); $("#modelPill").textContent = h.config.ollama_model + (h.config.razorpay_mode === "test" ? " · razorpay test" : ""); } catch {}
  })();
})();
