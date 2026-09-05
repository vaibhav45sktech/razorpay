/* CampusHood frontend. Deliberately dumb (HLD s4.2): renders what the API
   returns — the monthly plan, the pool timeline + recommendation, the offers,
   the ledger state — and turns the user's taps into structured API calls.
   Computes nothing and decides nothing: every rupee figure, percentage,
   status and reason on screen is a value the server returned. The only
   arithmetic below is SVG/CSS geometry that draws API numbers. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // ---- formatting: paise -> "₹1,500" is presentation of an API integer, not a computed figure
  const rupees = (paise) => paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rupees0 = (paise) => paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  const timeOf = (iso) => { try { return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch { return iso; } };
  const dayOf = (iso) => { try { return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").toLocaleDateString("en-IN", { day: "numeric", month: "short" }); } catch { return iso; } };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = { userId: null, history: [], lastSeq: 0, busy: false, spending: null, pool: null, plan: null };

  // ---- HTTP
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch { /* no body */ }
    if (!res.ok) { const err = new Error((body && (body.detail?.[0]?.msg || body.detail)) || res.statusText); err.status = res.status; err.body = body; throw err; }
    return body;
  }
  const post = (path, body) => api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

  // ---- toasts
  const toastWrap = document.createElement("div"); toastWrap.className = "toast-wrap"; document.body.appendChild(toastWrap);
  function toast(text, kind = "ok") { const t = document.createElement("div"); t.className = `toast ${kind}`; t.textContent = text; toastWrap.appendChild(t); setTimeout(() => t.remove(), 4800); }

  // ---- reveal on scroll + nav shadow
  const io = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: 0.08 });
  $$(".reveal").forEach((el) => io.observe(el));
  const nav = $("#nav");
  addEventListener("scroll", () => nav.classList.toggle("scrolled", scrollY > 8), { passive: true });
  $("#year").textContent = new Date().getFullYear();

  // ---- tabs
  function showTab(name) {
    $$(".tab").forEach((t) => { const on = t.dataset.tab === name; t.classList.toggle("active", on); t.setAttribute("aria-selected", String(on)); });
    $$(".panel").forEach((p) => { p.hidden = p.dataset.panel !== name; });
    $$(".panel:not([hidden]) .reveal").forEach((el) => el.classList.add("in"));
    try { localStorage.setItem("cp.tab", name); } catch {}
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  try { const saved = localStorage.getItem("cp.tab"); if (saved && $(`.tab[data-tab="${saved}"]`)) showTab(saved); } catch {}

  // ---- drawer: Ask the agent
  const drawer = $("#drawer"), backdrop = $("#drawerBackdrop");
  function openDrawer() { drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); backdrop.hidden = false; document.body.classList.add("drawer-open"); setTimeout(() => $("#chatInput").focus(), 300); }
  function closeDrawer() { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); backdrop.hidden = true; document.body.classList.remove("drawer-open"); }
  $$("[data-open-drawer]").forEach((b) => b.addEventListener("click", openDrawer));
  $$("[data-close-drawer]").forEach((b) => b.addEventListener("click", closeDrawer));
  backdrop.addEventListener("click", closeDrawer);
  addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

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

  // =====================================================================
  // PLAN
  // =====================================================================
  function renderPlan(p) {
    state.plan = p;
    $("#planMonth").textContent = p.month_label || p.month;
    const st = $("#planStatus");
    st.textContent = { due: "action needed", pending: "waiting for payment", done: "done for the month" }[p.status] || p.status;
    st.className = "pill " + ({ due: "pill-warn", pending: "pill-warn", done: "" }[p.status] || "pill-muted");
    $("#planHeadline").textContent = p.headline;
    $("#planAmount").textContent = rupees0(p.status === "pending" && p.pending_intent ? p.pending_intent.amount_paise : p.recommended_paise);
    $("#planBand").textContent = `allowed band ${rupees0(p.band.min_paise)}–${rupees0(p.band.max_paise)}`;

    const cta = $("#planCta"); cta.innerHTML = "";
    if (p.status === "done") {
      cta.innerHTML = `<span class="done">Contribution in the ledger</span>`;
    } else if (p.status === "pending" && p.pending_intent) {
      const i = p.pending_intent;
      cta.innerHTML = i.status === "AWAITING_APPROVAL"
        ? `<span class="pill pill-warn">approve it in “Needs your decision”</span>`
        : `<button class="btn btn-neon" id="planPay">${i.status === "EXECUTING" ? "Resume payment" : `Pay ${rupees0(i.amount_paise)} with Razorpay (test)`}</button>`;
      const b = $("#planPay"); if (b) b.onclick = () => guard(b, () => payIntent(i.intent_id));
    } else {
      cta.innerHTML = `<button class="btn btn-neon" id="planAgree">Agree &amp; pay ${rupees0(p.recommended_paise)}</button>`;
      $("#planAgree").onclick = () => guard($("#planAgree"), agreeAndPay);
    }

    const pol = p.policy_preview || {};
    $("#planPolicy").innerHTML = `<span class="status ${esc(pol.decision === "ALLOW" ? "ALLOWED" : pol.decision || "")}">policy · ${esc(pol.decision || "—")}</span><span>${esc(pol.reason || "")}</span>`;
    const ul = $("#planReasons"); ul.innerHTML = "";
    (p.reasons || []).forEach((r) => { const li = document.createElement("li"); li.textContent = r; ul.appendChild(li); });

    const g = p.goal, body = $("#goalBody"), eta = $("#goalEta");
    if (!g) { body.innerHTML = `<p class="empty">No active goal — the plan keeps the habit at the minimum contribution.</p>`; eta.textContent = "—"; return; }
    eta.textContent = g.months_to_goal == null ? "reached" : `${g.months_to_goal} month${g.months_to_goal === 1 ? "" : "s"} to go · ${g.goal_month_label}`;
    body.innerHTML = `<div class="goal-big">
        <div class="nums"><span><strong>${rupees0(g.saved_paise)}</strong> <span class="muted">saved of ${rupees0(g.target_paise)}</span></span><span class="muted">${g.pct_complete}%</span></div>
        <div class="meter"><span class="meter-fill" style="width:${Math.min(100, g.pct_complete)}%"></span></div>
        <div class="goal-foot"><span>${esc(g.label)}</span><span>${rupees0(g.remaining_paise)} remaining</span></div>
      </div>`;
  }

  async function agreeAndPay() {
    const r = await post(`/api/plan/${encodeURIComponent(state.userId)}/agree`);
    toast(r.reused ? "Resuming this month's contribution" : `Agreed — ${rupees0(r.amount_paise)} contribution proposed (${r.status})`);
    if (["ALLOWED", "APPROVED", "EXECUTING"].includes(r.status)) await payIntent(r.intent_id);
    else if (r.status === "AWAITING_APPROVAL") toast("This one needs your approval first — see “Needs your decision”.");
    else if (r.policy && r.policy.decision === "DENY") toast(`Policy refused it: ${r.policy.reason}`, "bad");
  }

  // =====================================================================
  // POOL
  // =====================================================================
  function renderPool(v) {
    state.pool = v;
    const tl = $("#timeline"), rec = $("#recBody"), pill = $("#recPill");
    if (!v.in_pool) {
      $("#poolMeta").textContent = "not in a cycle"; tl.innerHTML = `<p class="empty">${esc(v.message)}</p>`; rec.innerHTML = `<p class="empty">Join a cycle to get a draw recommendation.</p>`; pill.textContent = "—"; renderNeeds(v.needs || []); return;
    }
    const c = v.cycle;
    $("#poolMeta").textContent = `${c.member_count} members · ${rupees0(c.contribution_amount_paise)} a round · ${rupees0(v.round_amount_paise)} per draw`;
    $("#poolTitle").firstChild.textContent = c.label + " ";

    tl.innerHTML = "";
    v.rounds.forEach((r) => {
      const el = document.createElement(r.status === "open" || r.status === "requested" ? "button" : "div");
      if (el.tagName === "BUTTON") { el.type = "button"; el.dataset.month = r.month; el.title = "Request this round"; }
      const mine = r.requested_by_you;
      el.className = "round " + (r.past ? "past" : "open") + (r.current ? " current" : "") + (r.recommended && !mine ? " rec" : "") + (mine ? " mine" : "") + (mine && v.my_draw && v.my_draw.status === "paid" ? " paid" : "");
      const status = r.past ? `drawn · ${r.drawer}` : mine ? (v.my_draw && v.my_draw.status === "paid" ? "paid out to you (simulated)" : "your requested round") : r.recommended ? "recommended for you" : "open";
      el.innerHTML = `<span class="r-i">Round ${r.index}</span><span class="r-m">${esc(r.label)}</span><span class="r-s">${esc(status)}</span>`;
      tl.appendChild(el);
    });

    const R = v.recommendation, mine = v.my_draw;
    if (!R) { pill.textContent = "—"; rec.innerHTML = `<p class="empty">Every round of this cycle has been drawn.</p>`; }
    else {
      const following = mine && mine.round_month === R.month;
      pill.textContent = mine ? (following ? "you're on it" : "you chose differently") : "not requested yet";
      pill.className = "pill " + (mine ? (following ? "" : "pill-warn") : "pill-muted");
      const openRounds = v.rounds.filter((r) => !r.past && r.month !== R.month);
      rec.innerHTML = `
        <div class="rec-month">${esc(R.label)}</div>
        <p class="rec-sub">Draw the ${rupees0(R.amount_paise)} round then. Based on ${rupees0(v.saved_now_paise)} saved now and ${rupees0(v.assumed_monthly_contribution_paise)} a month going forward.</p>
        <div class="why"><p class="why-h">Why</p><ul>${R.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>
        <div class="rec-actions">
          ${mine && mine.status === "paid" ? "" : `<button class="btn btn-neon btn-sm" id="reqRec" ${following ? "disabled" : ""}>${following ? "Requested ✓" : "Request this round"}</button>`}
          ${mine && mine.status === "paid" ? "" : `<select id="otherRound" aria-label="Or pick another open round"><option value="">Or pick another round…</option>${openRounds.map((r) => `<option value="${esc(r.month)}">${esc(r.label)}</option>`).join("")}</select>`}
        </div>
        ${mine ? `<div class="mine-box">
            <div class="intent-top"><strong>Your draw · ${esc(mine.round_month ? (v.rounds.find((r) => r.month === mine.round_month) || {}).label || mine.round_month : "—")}</strong><span class="status ${mine.status === "paid" ? "LEDGER_UPDATED" : "ALLOWED"}">${esc(mine.status)}</span></div>
            <div class="reason">${esc(mine.reason)}</div>
            ${mine.status !== "paid" && v.can_simulate_draw ? `<div><button class="btn btn-ghost btn-sm" id="simDraw">Simulate the payout (demo)</button></div>` : ""}
            ${mine.status === "paid" ? `<div class="muted" style="font-size:.82rem">Settled through the same policy gate a real payout would use. It shows under “Rewards &amp; payouts”.</div>` : ""}
          </div>` : ""}`;
      const rb = $("#reqRec"); if (rb) rb.onclick = () => guard(rb, () => requestRound(R.month));
      const sel = $("#otherRound"); if (sel) sel.onchange = () => { if (sel.value) requestRound(sel.value).catch((e) => toast(e.message, "bad")); };
      const sd = $("#simDraw"); if (sd) sd.onclick = () => guard(sd, simulateDraw);
    }

    const ben = (v.benefits || []);
    if (ben.length) {
      const box = document.createElement("div"); box.className = "benefits";
      box.innerHTML = `<p class="why-h">Your benefits this cycle</p>` + ben.map((b) => `<div class="benefit"><strong>${rupees0(b.amount_paise)}</strong> · ${esc(b.status)}<span class="reason">${esc(b.reason)}</span></div>`).join("");
      rec.appendChild(box);
    }
    renderNeeds(v.needs || []);
  }

  $("#timeline").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-month]"); if (!b || b.classList.contains("mine")) return;
    guard(b, () => requestRound(b.dataset.month));
  });

  async function requestRound(month) {
    const r = await post(`/api/pool/${encodeURIComponent(state.userId)}/request-round`, { month });
    toast(r.followed_recommendation ? `Requested the agent's pick — allocation ${r.status}` : `Requested a different round — recorded with the agent's assessment`);
    await refreshAutopilot();
  }
  async function simulateDraw() {
    const r = await post(`/api/pool/${encodeURIComponent(state.userId)}/simulate-draw`);
    if (r.executed) toast(`Simulated payout settled: ${rupees0(r.amount_paise)} (intent ${r.status})`);
    else toast(`Policy engine did not execute it: ${r.policy && r.policy.reason || r.status}`, "bad");
    await refreshAll();
  }

  // ---- needs
  function renderNeeds(list) {
    $("#needsCount").textContent = String(list.length);
    const ul = $("#needsList"); ul.innerHTML = "";
    if (!list.length) { ul.innerHTML = `<li class="empty" style="border:0;padding:4px 0">Nothing listed yet — so the agent assumes you don't need early access.</li>`; return; }
    list.forEach((n) => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${esc(n.label)}${n.category ? `<span class="cat">${esc(n.category)}</span>` : ""}<span class="m"> · ${esc(monthLabel(n.month))}</span></span><span class="a">${rupees0(n.amount_paise)}</span><button class="icon-btn" type="button" data-del="${esc(n.need_id)}" aria-label="Remove">×</button>`;
      ul.appendChild(li);
    });
  }
  const monthLabel = (ym) => { const r = (state.pool && state.pool.rounds || []).find((x) => x.month === ym); if (r) return r.label; try { const [y, m] = ym.split("-"); return new Date(+y, +m - 1, 1).toLocaleDateString("en-IN", { month: "short", year: "numeric" }); } catch { return ym; } };
  $("#needsList").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-del]"); if (!b) return;
    try { await api(`/api/needs/${encodeURIComponent(state.userId)}/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast("Removed"); await refreshAutopilot(); }
    catch (err) { toast(err.message, "bad"); }
  });
  $("#needForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, fd = new FormData(f);
    const body = { label: fd.get("label"), month: fd.get("month"), amount_rupees: Number(fd.get("amount")), category: fd.get("category") || null };
    const btn = f.querySelector("button");
    await guard(btn, async () => {
      await post(`/api/needs/${encodeURIComponent(state.userId)}`, body);
      toast("Added — the agent has re-planned around it");
      f.reset(); await refreshAutopilot();
    });
  });
  function fillCategories(cats) {
    const sel = $("#needForm select[name=category]"); if (sel.options.length > 1) return;
    cats.forEach((c) => { const o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o); });
  }

  // =====================================================================
  // SPEND
  // =====================================================================
  function renderSpend(v) {
    $("#spendRule").textContent = `limit ${rupees0(v.monthly_limit_paise)} · approval above ${rupees0(v.approval_threshold_paise)}` + (v.paused ? " · PAUSED" : "");
    $("#spendText2").textContent = `${rupees0(v.spent_this_month_paise)} of ${rupees0(v.monthly_limit_paise)}`;
    $("#spendHeadroom").textContent = `${rupees0(v.headroom_paise)} headroom`;
    $("#spendThreshold").textContent = state.spending ? `${state.spending.pct_used}% used` : "";
    const fill = $("#spendFill2"); if (state.spending) { fill.style.width = `${Math.min(100, state.spending.pct_used)}%`; fill.classList.toggle("hot", state.spending.pct_used >= 80); }
    fillCategories(v.categories || []);

    const box = $("#offers"); box.innerHTML = "";
    if (!v.offers.length) { box.innerHTML = `<p class="empty">No eligible offers right now.</p>`; return; }
    v.offers.forEach((o) => {
      const el = document.createElement("article"); el.className = "offer" + (o.matched_needs.length ? " matched" : "");
      const pv = o.policy_preview;
      const verdict = !pv ? `<span class="status">no fixed price</span><span>ask the agent about it</span>`
        : pv.decision === "ALLOW" ? `<span class="status ALLOWED">within your rule</span><span>${esc(pv.reason)}</span>`
        : pv.decision === "REQUIRE_APPROVAL" ? `<span class="status AWAITING_APPROVAL">needs your approval</span><span>${esc(pv.reason)}</span>`
        : `<span class="status DENIED">over your limit</span><span>${esc(pv.reason)}</span>`;
      el.innerHTML = `
        <div class="offer-top"><div><div class="merchant">${esc(o.merchant)}</div><div class="title">${esc(o.title)}</div></div><span class="cat">${esc(o.category || "")}</span></div>
        <div class="price">${o.effective_price_paise != null ? `<strong>${rupees0(o.effective_price_paise)}</strong>` : `<strong>—</strong>`}${o.list_price_paise != null && o.effective_price_paise != null && o.list_price_paise !== o.effective_price_paise ? `<s>${rupees0(o.list_price_paise)}</s>` : ""}${o.effective_discount_paise ? `<span class="save">save ${rupees0(o.effective_discount_paise)}</span>` : ""}</div>
        ${o.match_note ? `<div class="match">◆ ${esc(o.match_note)}</div>` : ""}
        <div class="verdict">${verdict}</div>
        <div class="offer-actions">${o.effective_price_paise != null && pv && pv.decision !== "DENY" ? `<button class="btn btn-primary btn-sm" data-offer="${esc(o.offer_id)}">${pv.decision === "ALLOW" ? "Propose & pay" : "Propose for approval"}</button>` : `<span class="muted" style="font-size:.8rem">${o.effective_price_paise == null ? "" : "The agent won't propose this — it would break your rule."}</span>`}</div>`;
      box.appendChild(el);
    });
  }
  $("#offers").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-offer]"); if (!b) return;
    guard(b, async () => {
      const r = await post(`/api/spend/${encodeURIComponent(state.userId)}/propose`, { offer_id: b.dataset.offer });
      const d = r.policy && r.policy.decision;
      if (d === "DENY") toast(`Refused by policy: ${r.policy.reason}`, "bad");
      else if (r.status === "AWAITING_APPROVAL") toast(`Proposed ${rupees0(r.amount_paise)} for “${r.title}” — approve it in “Needs your decision”.`);
      else { toast(`Proposed ${rupees0(r.amount_paise)} for “${r.title}” — ${r.status}`); if (["ALLOWED", "APPROVED"].includes(r.status)) await payIntent(r.intent_id); }
      await refreshAll();
    });
  });

  // =====================================================================
  // STATE (sidebar) + pending + recent
  // =====================================================================
  function renderState(s) {
    $("#stateStamp").textContent = "verified " + new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    const b = s.balances_paise || {};
    $("#balEmergency").textContent = rupees(b.emergency_savings);
    $("#balRewards").textContent = rupees(b.rewards);
    $("[data-stat=emergency]").textContent = rupees0(b.emergency_savings);

    const sp = s.spending_this_month; state.spending = sp;
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
    renderRecent(s.recent_events || []);
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
    const id = btn.dataset.id, act = btn.dataset.act;
    await guard(btn, async () => {
      if (act === "approve" || act === "deny") {
        const r = await post(`/api/intents/${id}/${act}`, { user_id: state.userId });
        toast(`${act === "approve" ? "Approved" : "Denied"} — status ${r.status}`);
      } else if (act === "pay") {
        await payIntent(id);
      }
      await refreshAll();
    });
  });

  const EVENT_LABEL = { CONTRIBUTION: "Contribution", PURCHASE: "Purchase", REWARD: "Reward", POOL_PAYOUT: "Pool payout", REVERSAL: "Reversal" };
  function renderRecent(events) {
    const ul = $("#recent"); ul.innerHTML = "";
    if (!events.length) { ul.innerHTML = `<li class="empty">No ledger activity yet.</li>`; return; }
    events.slice(0, 6).forEach((e) => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${esc(EVENT_LABEL[e.type] || e.type)}<span class="k">${esc(e.bucket.replace(/_/g, " "))} · ${dayOf(e.at)}${/simulated/.test(e.source) ? " · simulated" : ""}</span></span><span class="amt ${e.amount_paise < 0 ? "neg" : ""}">${e.amount_paise < 0 ? "−" : "+"}${rupees0(Math.abs(e.amount_paise))}</span>`;
      ul.appendChild(li);
    });
  }

  // ---- checkout (HLD s6.4): execute -> Razorpay Checkout -> /api/checkout/verify
  async function payIntent(intentId) {
    const ex = await post(`/api/intents/${intentId}/execute`, { user_id: state.userId });
    toast(ex.reused_existing_order ? `Resuming order ${ex.order_id}` : `Order ${ex.order_id} created (test mode)`);
    if (typeof Razorpay === "undefined") { toast("Razorpay Checkout script did not load (CSP/offline?)", "bad"); return; }
    await new Promise((resolve) => {
      const rzp = new Razorpay({
        key: ex.key_id, order_id: ex.order_id, amount: ex.amount_paise, currency: ex.currency,
        name: "CampusHood (DEMO — Test Mode)", description: "Synthetic demo · no real money",
        theme: { color: "#0a0a0a" },
        handler: async (resp) => {
          try {
            const v = await post("/api/checkout/verify", resp);
            toast(v.ok ? `Payment verified — intent ${v.intent_status}` : `Payment ${v.payment_status}; the webhook or reconciliation will settle it`, v.ok ? "ok" : "bad");
          } catch (err) { toast("Verify failed: " + err.message, "bad"); }
          resolve();
        },
        modal: { ondismiss: () => { toast("Checkout closed. A completed payment still settles via webhook."); resolve(); } },
      });
      rzp.on("payment.failed", () => { toast("Razorpay reported a failed attempt (browser event). The server waits for Razorpay's confirmation.", "bad"); });
      rzp.open();
    });
    await refreshAll();
  }

  // ---- donut: SVG geometry over API integers; no number is displayed that the API didn't return
  const DONUT_SERIES = [
    { key: "emergency_savings", label: "Emergency savings", note: "protected · never spendable by the agent", color: "#17b26a" },
    { key: "rewards", label: "Rewards & payouts", note: "partner-funded rewards and simulated pool payouts", color: "#0e7a4c" },
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
    $("#donutTotal").textContent = rupees0(balances.emergency_savings);   // API value verbatim
  }

  // =====================================================================
  // CHAT (drawer)
  // =====================================================================
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
      const r = await post("/api/chat", { user_id: state.userId, message: text, history: state.history });
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
    } finally { state.busy = false; $("#chatSend").disabled = false; refreshTrust(); refreshAutopilot(); }
  }

  // =====================================================================
  // AUDIT + EXCEPTIONS
  // =====================================================================
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
    try { await post(`/api/exceptions/${f.dataset.id}/resolve`, { note }); toast("Exception resolved (recorded in the audit trail)"); }
    catch (err) { toast(err.message, "bad"); }
    refreshTrust();
  });

  // =====================================================================
  // refresh + boot
  // =====================================================================
  async function guard(btn, fn) {
    if (btn) btn.disabled = true;
    try { await fn(); }
    catch (err) { toast(err.status === 503 ? (err.message || "Service busy — nothing was executed") : (err.message || "Request failed"), "bad"); }
    finally { if (btn && btn.isConnected) btn.disabled = false; }
  }
  async function refreshTrust() {
    try { renderAudit(await api(`/api/audit?user_id=${encodeURIComponent(state.userId)}&limit=80`)); } catch { /* keep last */ }
    try { renderExceptions(await api("/api/exceptions")); } catch { /* keep last */ }
  }
  async function refreshState() {
    try { renderState(await api(`/api/state/${encodeURIComponent(state.userId)}`)); } catch (err) { toast("Could not load state: " + err.message, "bad"); }
  }
  async function refreshAutopilot() {
    const u = encodeURIComponent(state.userId);
    const [plan, pool, spend] = await Promise.allSettled([api(`/api/plan/${u}`), api(`/api/pool/${u}`), api(`/api/spend/${u}`)]);
    if (plan.status === "fulfilled") renderPlan(plan.value); else toast("Plan unavailable: " + plan.reason.message, "bad");
    if (pool.status === "fulfilled") renderPool(pool.value); else toast("Pool unavailable: " + pool.reason.message, "bad");
    if (spend.status === "fulfilled") renderSpend(spend.value); else toast("Offers unavailable: " + spend.reason.message, "bad");
  }
  async function refreshAll() { await refreshState(); await Promise.all([refreshAutopilot(), refreshTrust()]); }

  (async () => {
    try { await loadUsers(); await refreshAll(); }
    catch (err) { toast("Backend not reachable: " + err.message, "bad"); }
    setInterval(refreshTrust, 6000);
    setInterval(async () => { await refreshState(); await refreshAutopilot(); }, 15000);
    try { const h = await api("/health"); $("#modelPill").textContent = h.config.ollama_model + (h.config.razorpay_mode === "test" ? " · razorpay test" : ""); } catch {}
  })();
})();
