"use strict";
/* Customer chat app. Server state (sessions, runs, confirmations) stays
   authoritative: the page renders saved history and retries requests with the
   same request_id, so a lost response never creates a second action. */
(() => {
  const { h, icon, store, createApi, toast, requestId, money, relativeTime, clock, publicUrl,
    highlighted, openDrawer, modeBadge } = window.SA;
  const AUTH_KEY = "sa.customer.v1";
  const ADMIN_KEY = "sa.admin.v1";
  const PENDING_KEY = "sa.pending.v1";
  const root = document.getElementById("root");
  const state = {
    auth: null, info: null, demo: null, sessions: [], orders: [], ordersMeta: null,
    current: null, snapshot: null, entries: [], busy: false, pending: null, tab: "orders",
    progress: null, draftText: "", unfolded: null, ticketBusy: false, ticketRetry: null,
  };
  const api = createApi(() => state.auth?.token);
  const els = {};

  // B3a: a session's state is its return cards (the task and its draft are gone); B3c: one open card per
  // order, and the session goes on after a submission.
  const CARD_STATUS = { pending: ["待确认", "accent"], consumed: ["已提交申请", "success"] };
  const DOMAIN_LABELS = {
    return_eligibility: "退换资格", eligibility_deadline: "申请时限", return_items: "退回物品与赠品",
    return_deduction: "退款扣减", return_shipping_responsibility: "运费承担", return_shipping_compensation: "运费补偿",
    return_shipping_benefit: "运费权益", return_pickup_fee: "取件费用", return_pickup_procedure: "取件流程",
    repair_procedure: "维修流程", repair_shipping_responsibility: "维修运费", warranty_coverage: "质保范围",
    exchange_procedure: "换货流程", general_guidance: "一般说明",
  };

  /* ---------------- boot & sign-in ---------------- */
  async function boot() {
    state.auth = store.get(AUTH_KEY);
    if (state.auth?.token) {
      try {
        state.info = await api("/client-info");
        if (state.info.role === "customer") { await enterApp(); return; }
      } catch (_) { /* stale token: sign in again */ }
      store.remove(AUTH_KEY);
      state.auth = null;
    }
    await renderLogin();
  }

  async function renderLogin() {
    try { state.demo = await api("/demo/identities", { token: "" }); } catch (_) { state.demo = null; }
    const values = [
      ["book", "回答附带原文依据", "政策结论只从知识库的规则卡中选择，每条都能展开保存的官网原文与适用前提。"],
      ["box", "查单、退货一条龙", "查自己的订单、补齐信息、出确认卡；你点了确认，申请才会提交。"],
      ["flow", "处理过程可追溯", "每轮都能查看需求理解、业务操作、引用的规则卡与答复选择，便于排查与复盘。"],
    ];
    const hero = h("section.login-hero",
      h("div.brand-row", h("div.brand-mark", icon("spark")), h("span.brand-name", { text: "售后助手" })),
      h("h1", "可以追溯依据的", h("em", { text: "售后客服 Agent" })),
      h("p.lede", { text: "多轮售后客服：理解需求 → 查询订单 → 对照整份政策规则卡 → 由程序组织有依据的答复。写操作必须经过你本人确认。" }),
      h("div.value-list", values.map(([name, title, text]) => h("div.value",
        h("div.value-icon", icon(name)), h("div", h("strong", { text: title }), h("span", { text: text }))))));
    const panel = h("section.login-panel");
    if (state.demo) {
      const customers = state.demo.identities.filter(item => item.role === "customer");
      const admin = state.demo.identities.find(item => item.role === "admin");
      panel.append(h("div", h("p.eyebrow", { text: "选择身份" }), h("h2", { text: "选择一个买家身份进入" })),
        h("div.identity-list", customers.map(item => identityButton(item)),
          admin ? identityButton(admin, true) : null),
        h("div.login-foot", h("span", { text: state.demo.notice })));
    } else {
      const input = h("input.input", { type: "password", placeholder: "粘贴本地 Bearer 凭据", autocomplete: "off", spellcheck: "false" });
      const form = h("form.token-form", { onsubmit: async event => {
        event.preventDefault();
        const token = input.value.trim();
        if (!token) return;
        try {
          const info = await api("/client-info", { token });
          if (info.role !== "customer") throw new Error("请使用买家凭据。");
          signIn({ token, identity: info.customer_id, role: "customer", display_name: info.customer_id });
        } catch (error) { toast(error.message, "error"); }
      } }, h("label.field", h("span", { text: "本地凭据" }), input), h("button.btn", { type: "submit", text: "进入" }));
      panel.append(h("div", h("p.eyebrow", { text: "连接本地服务" }), h("h2", { text: "当前服务未开启演示登录" })),
        h("p.muted", { text: "请用 app 命令启动演示，或粘贴 service init 生成的本地凭据。" }), form);
    }
    root.className = "";
    root.replaceChildren(h("main.login", hero, panel));
  }

  function identityButton(item, admin = false) {
    const avatar = h("div.avatar.lg", { text: item.display_name.slice(0, 1).toUpperCase() });
    avatar.style.background = item.color || "";
    return h("button.identity" + (admin ? ".admin" : ""), { type: "button", onclick: () => demoLogin(item.identity) },
      avatar, h("div.grow", h("strong", { text: item.display_name }), h("small", { text: item.subtitle })),
      h("span.chev", icon("chevron")));
  }

  async function demoLogin(identity) {
    try {
      const auth = await api("/demo/login", { body: { identity }, token: "" });
      if (auth.role === "admin") { store.set(ADMIN_KEY, auth); location.href = "/kb"; return; }
      signIn(auth);
    } catch (error) { toast(error.message, "error"); }
  }

  async function signIn(auth) {
    state.auth = auth;
    store.set(AUTH_KEY, auth);
    try { state.info = await api("/client-info"); await enterApp(); } catch (error) { toast(error.message, "error"); }
  }

  function signOut() {
    if (state.busy) return;
    store.remove(AUTH_KEY);
    store.remove(PENDING_KEY);
    Object.assign(state, { auth: null, info: null, current: null, snapshot: null, entries: [], sessions: [], orders: [] });
    renderLogin();
  }

  /* ---------------- shell ---------------- */
  async function enterApp() {
    renderShell();
    await Promise.all([loadSessions(), loadOrders()]);
    state.pending = store.get(PENDING_KEY);
    if (state.pending && state.pending.identity !== state.auth.identity) state.pending = null;
    const remembered = state.pending?.sid || store.get("sa.current." + state.auth.identity);
    if (remembered && state.sessions.some(item => item.session_id === remembered)) await openSession(remembered);
    else startNew();
    renderRetryBar();
  }

  function renderShell() {
    const avatar = h("div.avatar", { text: (state.auth.display_name || state.auth.identity).slice(0, 1).toUpperCase() });
    if (state.auth.identity === "bob") avatar.style.background = "#6b4fa0";
    els.convList = h("nav.conv-list", { "aria-label": "历史咨询" });
    els.sidebar = h("aside.sidebar",
      h("div.side-head",
        h("div.brand-row", h("div.brand-mark", icon("spark")), h("span.brand-name", { text: "售后助手" })),
        h("div.me", avatar, h("div.grow", h("strong", { text: state.auth.display_name || state.auth.identity }),
          h("small", { text: "当前账号" })),
          h("button.icon-btn", { title: "切换身份", "aria-label": "切换身份", onclick: signOut }, icon("logout"))),
        h("button.btn.block", { onclick: () => { startNew(); closeOverlays(); } }, icon("plus"), "新建咨询")),
      h("div.conv-title.eyebrow", { text: "历史咨询" }),
      els.convList,
      h("div.side-foot",
        h("div.side-links",
          state.info.knowledge_base ? h("a", { href: "/kb", onclick: event => { event.preventDefault(); demoAdmin(); } }, icon("book"), "知识库管理") : null)));
    els.title = h("h1", { text: "新的咨询" });
    els.sub = h("div.sub");
    els.threadInner = h("div.thread-inner");
    els.thread = h("section.thread", { "aria-live": "polite" }, els.threadInner);
    els.textarea = h("textarea", { rows: 1, placeholder: "描述你的问题，例如：赠品需要一起退回吗？", "aria-label": "输入消息" });
    els.send = h("button.btn", { type: "submit", "aria-label": "发送" }, icon("send"));
    els.count = h("span.composer-count");
    els.quick = h("div.quick");
    els.notice = h("div");
    // The one persistent statement of the environment (B1): the server's notice, shown on every screen.
    els.envNotice = h("p.env-notice", { role: "note", text: state.ordersMeta?.notice || "" });
    els.composerWrap = h("footer.composer-wrap", h("div.composer-inner", els.notice, els.quick,
      h("form.composer", { onsubmit: event => { event.preventDefault(); submitComposer(); } }, els.textarea, els.send),
      h("div.composer-hint", h("span", { text: "Enter 发送 · Shift+Enter 换行" }), els.count), els.envNotice));
    // B4a: the persistent "找人工" button, usable in every state of the page and of the session.
    els.handoff = h("button.btn.small.secondary.handoff-btn", { type: "button", onclick: () => ticketAction("handoff") },
      icon("user"), "找人工");
    els.main = h("main.main",
      h("header.topbar",
        h("button.icon-btn.mobile-only", { "aria-label": "打开会话列表", onclick: () => toggle(els.sidebar) }, icon("menu")),
        h("div.title", els.title, els.sub),
        els.handoff,
        h("button.icon-btn.panel-toggle", { "aria-label": "打开订单与详情", onclick: () => toggle(els.panel) }, icon("panel"))),
      els.thread, els.composerWrap);
    els.tabs = h("div.tabs", { role: "tablist" });
    els.panelBody = h("div.panel-body");
    els.panel = h("aside.panel", els.tabs, els.panelBody);
    root.className = "";
    root.replaceChildren(h("div.app", els.sidebar, els.main, els.panel));
    els.textarea.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitComposer(); }
    });
    els.textarea.addEventListener("input", () => { autosize(); renderCount(); });
    renderPanel();
  }

  function toggle(el) { el.classList.toggle("open"); }
  function closeOverlays() { els.sidebar?.classList.remove("open"); els.panel?.classList.remove("open"); }
  function autosize() { els.textarea.style.height = "auto"; els.textarea.style.height = Math.min(180, els.textarea.scrollHeight) + "px"; }

  async function demoAdmin() {
    try {
      const auth = await api("/demo/login", { body: { identity: "kb-admin" }, token: "" });
      store.set(ADMIN_KEY, auth);
      window.open("/kb", "_blank", "noopener");
    } catch (_) { location.href = "/kb"; }
  }

  /* ---------------- data ---------------- */
  async function loadSessions() {
    try { state.sessions = (await api("/me/sessions")).sessions; } catch (error) { toast(error.message, "error"); }
    renderSessions();
  }
  async function loadOrders() {
    try { const data = await api("/me/orders"); state.orders = data.orders; state.ordersMeta = data; }
    catch (error) { toast(error.message, "error"); }
    if (els.envNotice) els.envNotice.textContent = state.ordersMeta?.notice || "";
    renderPanel();
    // Inline order cards in the thread read their status from state.orders; refresh them
    // without disturbing the reader's scroll position.
    if (!state.busy && els.thread) {
      const top = els.thread.scrollTop;
      renderThread();
      els.thread.scrollTop = top;
    }
  }

  function renderSessions() {
    els.convList.replaceChildren();
    if (!state.sessions.length) { els.convList.append(h("p.empty-state", { text: "还没有咨询记录" })); return; }
    for (const item of state.sessions) {
      const [label, tone] = CARD_STATUS[item.card_status] || [null, ""];
      const showBadge = !!label || item.busy;
      els.convList.append(h("button.conv" + (item.session_id === state.current ? ".active" : ""),
        { type: "button", onclick: () => { openSession(item.session_id); closeOverlays(); } },
        h("div.conv-top", h("strong", { text: item.title || "新的咨询" }),
          h("time", { text: relativeTime(item.updated_at) })),
        item.preview ? h("p", { text: item.preview }) : null,
        showBadge ? h("span.badge" + (tone ? "." + tone : ""), { text: item.busy ? "处理中" : label }) : null));
    }
  }

  function startNew() {
    state.current = null; state.snapshot = null; state.entries = [];
    store.remove("sa.current." + state.auth.identity);
    renderSessions(); renderThread(); renderHeader(); renderComposer(); renderPanel();
    els.textarea.focus();
  }

  async function openSession(sid) {
    if (state.busy) { toast("请等待当前回复完成后再切换会话。"); return; }
    state.current = sid;
    store.set("sa.current." + state.auth.identity, sid);
    renderSessions();
    try {
      const data = await api("/sessions/" + encodeURIComponent(sid) + "/history");
      state.snapshot = data.session; state.entries = data.entries;
    } catch (error) {
      toast(error.message, "error");
      if (error.status === 404) { startNew(); await loadSessions(); }
      return;
    }
    renderHeader(); renderThread(); renderComposer(); renderPanel();
    scrollToEnd();
  }

  async function refreshCurrent() {
    if (!state.current) return;
    const data = await api("/sessions/" + encodeURIComponent(state.current) + "/history");
    state.snapshot = data.session; state.entries = data.entries;
    renderHeader(); renderThread(); renderComposer(); renderPanel();
    scrollToEnd();
  }

  /* ---------------- header & composer ---------------- */
  function sessionTitle() {
    const first = state.entries.find(entry => entry.entry === "turn" && entry.user_message);
    return first ? first.user_message.replace(/\s+/g, " ").slice(0, 40) : "新的咨询";
  }

  function renderHeader() {
    els.title.textContent = sessionTitle();
    els.sub.replaceChildren();
    const snapshot = state.snapshot;
    if (snapshot) {
      const { status: [label, tone], orders } = sessionStatus(snapshot);
      els.sub.append(h("span.badge" + (tone ? "." + tone : ""), { text: label }));
      for (const orderId of orders) els.sub.append(h("span.badge", { text: "订单 " + orderId }));
    }
    const kb = state.info?.knowledge_base_version;
    if (kb?.number) els.sub.append(h("span.badge.brand", { title: kb.label || "", text: "知识库 v" + kb.number }));
  }

  // B3c (decision D4): the session's open cards, one per order and oldest first; a submission ends nothing.
  function openCards() { return state.snapshot?.open_cards || []; }

  /* What the header and the task panel say the session is at, read in this one place (B3c). Each status keeps
     its own orders: the open cards; else the request a click in this session submitted last, with that
     request's order (the latest card may be another order's, voided or expired); else the latest card's order. */
  function sessionStatus(snapshot) {
    const open = openCards();
    if (open.length) return { status: CARD_STATUS.pending, orders: open.map(card => card.order_id) };
    if (snapshot.return_request) return { status: CARD_STATUS.consumed, orders: [snapshot.return_request.order_id] };
    return { status: ["咨询中", ""], orders: snapshot.card?.order_id ? [snapshot.card.order_id] : [] };
  }

  /* B3b (plan §4.4): a failed, cancelled or interrupted turn leaves the card valid on the server; the page folds
     it until the user expands it, so a message that did not get through cannot leave a card one click from
     submission. Read from the saved entries: the latest turn did not finish, and this card was not asked for
     since (B3c: another order's "申请退货" leaves it folded). */
  function cardFolded(card) {
    if (state.unfolded === card.action_id) return false;
    for (let index = state.entries.length - 1; index >= 0; index -= 1) {
      const entry = state.entries[index];
      if (entry.entry === "card_prepare" && entry.status === "completed"
          && entry.response?.card?.action_id === card.action_id) return false;
      if (entry.entry === "turn") return ["failed", "cancelled", "interrupted"].includes(entry.status);
    }
    return false;
  }

  function renderComposer() {
    els.textarea.disabled = state.busy || !!state.snapshot?.busy;
    renderCount();
    els.notice.replaceChildren();
    if (state.snapshot?.busy && !state.busy) {
      els.notice.append(h("div.retry-bar", h("span", { text: "上一轮仍在处理中（可能来自另一个页面或中断的请求）。" }),
        h("span.chip-row",
          h("button.btn.small.secondary", { text: "刷新", onclick: () => refreshCurrent().catch(e => toast(e.message, "error")) }),
          h("button.btn.small.warn", { text: "恢复中断", onclick: recoverRun }))));
    }
    renderQuick();
  }

  // B4c: the server's message limit (client-info message_limit), counted as the server counts it: characters
  // (code points) of the trimmed text that is sent. Over the limit the counter says by how much and nothing is sent.
  function renderCount() {
    const limit = state.info.message_limit, length = Array.from(els.textarea.value.trim()).length;
    els.count.textContent = length > limit ? `${length}/${limit}，超出 ${length - limit} 字` : `${length}/${limit}`;
    els.count.classList.toggle("over", length > limit);
    els.send.disabled = els.textarea.disabled || length > limit;
  }

  function orderPick(eligible = true) {
    const pool = state.orders.filter(order => !order.return_request && (!eligible || order.return_eligible));
    return (pool[0] || state.orders[0])?.order_id || "O1001";
  }

  function renderQuick() {
    els.quick.replaceChildren();
    if (openCards().length || state.busy) return;
    const chips = state.entries.length ? [
      ["查订单 " + orderPick(false), "帮我查一下订单 " + orderPick(false)],
      ["赠品要退回吗？", "京东自营的赠品需要和主商品一起退回吗？"],
      ["寄回运费问题", "因为个人原因退回，寄回去的快递费由谁出？"],
      ["七天无理由怎么算", "七天无理由退货有哪些前提？"],
    ] : [];
    for (const [label, text] of chips) els.quick.append(h("button.chip", { type: "button", text: label, onclick: () => send(text) }));
  }

  function submitComposer() {
    const text = els.textarea.value.trim();
    if (!text || els.send.disabled) return;
    els.textarea.value = ""; autosize(); renderCount();
    send(text);
  }

  /* ---------------- sending ---------------- */
  async function ensureSession() {
    if (state.current) return true;
    try {
      const created = await api("/sessions", { body: {} });
      state.current = created.session_id; state.snapshot = created; state.entries = [];
      store.set("sa.current." + state.auth.identity, state.current);
      return true;
    } catch (error) { toast(error.message, "error"); return false; }
  }

  async function send(text) {
    if (state.busy) return;
    if (!await ensureSession()) return;
    state.unfolded = null;   // a new message folds the card again if this turn does not finish either
    state.pending = { kind: "message", identity: state.auth.identity, sid: state.current, text,
      path: "/sessions/" + encodeURIComponent(state.current) + "/messages", body: { message: text, request_id: requestId() } };
    store.set(PENDING_KEY, state.pending);
    await runPending();
  }

  async function runPending() {
    const pending = state.pending;
    if (!pending || state.busy) return;
    state.busy = true;
    renderRetryBar(); renderComposer();
    if (pending.kind === "message") {
      state.progress = { started: Date.now(), steps: {}, text: pending.text };
      renderThread(); scrollToEnd();
      pollProgress(pending.sid);
    }
    try {
      const result = await api(pending.path, { body: pending.body });
      state.pending = null; store.remove(PENDING_KEY);
      if (pending.kind === "confirm") toast("退货申请已提交；同一订单只会保留一条申请。");
      if (pending.kind === "card") toast(cardNote(result) + "，请在卡上选择原因后确认。");
      if (pending.kind === "void") toast("这张确认卡已作废，退货申请没有提交。");
    } catch (error) {
      if (!error.network) { state.pending = null; store.remove(PENDING_KEY); }
      toast(error.network ? error.message + " 可点击重试，沿用原请求 ID，不会重复执行。" : error.message, "error");
    } finally {
      state.busy = false; state.progress = null;
      try { await refreshCurrent(); } catch (_) { renderThread(); renderComposer(); }
      renderRetryBar();
      loadSessions();
      loadOrders();
    }
  }

  function renderRetryBar() {
    if (!els.notice) return;
    const existing = els.notice.querySelector(".retry-pending");
    if (existing) existing.remove();
    if (!state.pending || state.busy) return;
    els.notice.prepend(h("div.retry-bar.retry-pending",
      h("span", { text: "有一个请求尚未确认结果（" + (state.pending.kind === "message" ? "消息" : "操作") + "）。重试会沿用原请求 ID，不会重复执行。" }),
      h("span.chip-row", h("button.btn.small.warn", { text: "重试", onclick: runPending }),
        h("button.btn.small.ghost", { text: "放弃", onclick: () => { state.pending = null; store.remove(PENDING_KEY); renderRetryBar(); } }))));
  }

  async function pollProgress(sid) {
    let runId = null;
    while (state.busy && state.progress) {
      await new Promise(resolve => setTimeout(resolve, 650));
      if (!state.busy || !state.progress) break;
      try {
        if (!runId) {
          const snapshot = await api("/sessions/" + encodeURIComponent(sid));
          runId = snapshot.active_run_id;
        }
        if (runId) {
          const run = await api("/sessions/" + encodeURIComponent(sid) + "/runs/" + encodeURIComponent(runId));
          if (state.progress) { state.progress.steps = stepsFromEvents(run.events); updateProgress(); }
        } else updateProgress();
      } catch (_) { /* polling is best effort */ }
    }
  }

  function stepsFromEvents(events) {
    const steps = { understand: "active" };
    for (const event of events) {
      const detail = event.details || {};
      if (event.kind === "model_attempt" && detail.step === "understand") steps.understand = "active";
      if (event.kind === "model_response" && detail.step === "understand") steps.understand = "done";
      if (event.kind === "tool_reserved") steps.business = "active";
      if (event.kind === "model_attempt" && detail.step === "answer") {
        steps.understand = "done";
        if (steps.business) steps.business = "done";
        steps.answer = "active";
      }
    }
    return steps;
  }

  function progressView() {
    const steps = state.progress?.steps || { understand: "active" };
    const rows = [["understand", "理解你的问题"], ["business", "查询订单 / 处理申请"], ["answer", "组织有依据的答复"]];
    const box = h("div.progress", { role: "status" });
    for (const [key, label] of rows) {
      const status = steps[key];
      if (!status && key === "business") continue;
      const mark = status === "done" ? icon("check") : status === "active" ? h("span.spinner") : h("span.idle-dot");
      box.append(h("div.step" + (status ? "." + status : ""), h("span.mark", mark), h("span", { text: label })));
    }
    const seconds = ((Date.now() - (state.progress?.started || Date.now())) / 1000).toFixed(1);
    box.append(h("div.elapsed", { text: `已用时 ${seconds} 秒` }));
    return box;
  }

  function updateProgress() {
    const current = els.threadInner.querySelector(".progress");
    if (current) current.replaceWith(progressView());
  }

  /* ---------------- thread ---------------- */
  function scrollToEnd() { requestAnimationFrame(() => { els.thread.scrollTop = els.thread.scrollHeight; }); }

  function renderThread() {
    const inner = els.threadInner;
    inner.replaceChildren();
    const inflight = state.busy && state.pending?.kind === "message" ? state.pending : null;
    if (!state.entries.length && !inflight) { inner.append(welcome()); return; }
    for (const entry of state.entries) {
      if (entry.entry === "turn") {
        if (inflight && entry.request_id === inflight.body.request_id) continue;
        inner.append(userMessage(entry.user_message));
        if (entry.status === "running") inner.append(assistantShell(h("div.answer-card", h("div.answer-text", { text: "这一轮仍在处理中，完成后刷新即可看到结果。" }))));
        else inner.append(assistantMessage(entry.response || {}, entry));
      } else if (entry.entry === "card_confirm") {
        const ok = entry.status === "completed";
        inner.append(h("div.system-note", icon(ok ? "check" : "alert"),
          ok ? (entry.response?.reply || "你点了确认，退货申请已提交") : "确认未完成：" + (entry.response?.error?.message || "")));
      } else if (entry.entry === "card_prepare") {
        const ok = entry.status === "completed";
        inner.append(h("div.system-note", icon(ok ? "check" : "alert"),
          ok ? cardNote(entry.response) : "没有生成确认卡：" + (entry.response?.error?.message || "")));
      } else if (entry.entry === "card_void") {
        const ok = entry.status === "completed";
        inner.append(h("div.system-note", icon(ok ? "info" : "alert"),
          ok ? "你点了暂不提交，这张确认卡已作废" : "没有作废确认卡：" + (entry.response?.error?.message || "")));
      } else if (entry.entry === "ticket_handoff" || entry.entry === "ticket_withdraw") {
        inner.append(ticketEntry(entry));
      } else if (entry.entry === "cancel") {
        inner.append(h("div.system-note", icon("info"), "已停止处理中的一轮，确认卡不受影响"));
      } else if (entry.entry === "recover") {
        inner.append(h("div.system-note", icon("refresh"), "已结束中断的处理轮次"));
      }
    }
    if (inflight) { inner.append(userMessage(inflight.text)); inner.append(assistantShell(progressView())); }
    if (!inflight) for (const card of openCards()) inner.append(confirmCard(card));
  }

  function welcome() {
    const name = state.auth.display_name || state.auth.identity;
    const eligible = orderPick(true);
    const product = orderInfo(eligible)?.product_name;
    const items = [
      ["我的订单", "帮我看看我最近几单哪些能退"],
      product ? ["按商品名", `${product}那单还能退吗？`] : ["查询订单", "帮我查一下订单 " + eligible],
      ["办理退货", `我要退货 ${eligible}`],
      ["赠品规则", "赠品需要和主商品一起退回吗？"],
      ["七天无理由", "七天无理由退货有哪些前提？"],
      ["运费补偿", "我从广西寄回北京，退货运费补偿表列金额是多少？"],
    ];
    return h("section.welcome",
      h("div", h("h2", { text: `你好，${name}` }),
        h("p", { text: "我可以查询你名下的订单、解释售后规则并附上原文，也能帮你准备退货申请。试试下面的问题：" })),
      h("div.suggestions", items.map(([label, text]) => h("button.suggestion", { type: "button", onclick: () => send(text) },
        h("small", { text: label }), h("span", { text: text })))));
  }

  function userMessage(text) { return h("div.msg.user", h("div.bubble", { text })); }
  function assistantShell(...children) {
    return h("div.msg.assistant", h("div.avatar", icon("spark")), h("div.body", children));
  }

  function assistantMessage(response, entry) {
    const blocks = response.presentation?.blocks || [];
    const sources = response.presentation?.source_cards || [];
    const index = new Map(sources.map(source => [source.citation_id, source]));
    const failed = response.status && response.status !== "completed";
    const card = h("div.answer-card" + (failed ? ".error" : ""));
    if (failed || !blocks.length) {
      card.append(h("div.answer-text", { text: response.reply || response.error?.message || "本轮没有形成回答。" }));
      const step = failed ? failureStep(response, entry) : null;
      if (step) card.append(step);
    } else {
      for (const block of blocks) {
        const view = renderBlock(block, index);
        if (view) card.append(view);
      }
      // B8: a completed turn that opened or appended to the session's ticket declares its card, drawn at its latest
      // declaration only, like a ticket click's (I13).
      const ticket = response.ticket_card;
      if (ticket?.type === "ticket" && sessionTickets().get(ticket.ticket_id) === ticket) card.append(ticketCard(ticket));
    }
    const meta = h("div.msg-meta");
    if (response.usage?.total_tokens) meta.append(h("span", { text: response.usage.total_tokens + " tokens" }));
    if (sources.length) meta.append(h("button", { type: "button", onclick: () => openSources(sources, blocks) }, icon("doc"), `参考来源 ${uniqueTitles(sources).length}`));
    if (response.graph || response.run_id) meta.append(h("button", { type: "button", onclick: () => openTrace(response, entry) }, icon("flow"), "处理过程"));
    return assistantShell(card, meta);
  }

  /* B4b (plan §6.1; standard HO4, HR5, W6): a failed turn shows the one next step its response declares, never two.
     "重试" (next_step retry) sends the same message again as a new request, on the session's latest turn only; the
     failed request keeps its saved result. After two failed turns in a row the turn declares the ticket it opened or
     appended to instead (ticket_card), drawn at its latest declaration like a ticket click's, and offers no retry.
     B4d (HO4): beside the retry, the "找人工" the step declares (offer_handoff), the same click as the persistent
     button, until a click follows this turn. */
  function failureStep(response, entry) {
    const block = response.ticket_card;
    if (block?.type === "ticket") return sessionTickets().get(block.ticket_id) === block ? ticketCard(block) : null;
    const step = response.next_step;
    const latest = state.entries.filter(item => item.entry === "turn").pop();
    if (step?.action !== "retry" || !entry || entry !== latest) return null;
    const offer = step.offer_handoff === true && entry === state.entries[state.entries.length - 1];
    return h("div.confirm-actions", h("button.btn.small", { type: "button", disabled: state.busy, text: "重试",
      onclick: () => send(entry.user_message) }),
      offer ? h("button.btn.small.secondary", { type: "button", disabled: state.ticketBusy, text: "找人工",
        onclick: () => ticketAction("handoff") }) : null);
  }

  function uniqueTitles(sources) { return [...new Set(sources.map(source => source.title))]; }

  /* B5d2 (plan §5.3): the reply's blocks come in the server's fixed order, each with its author. Only the words the
     model wrote (B5f: an item's note, the next step and the one follow-up question) carry the author mark; everything
     else, card text included, is rendered by code from records and the card's own words. */
  function renderBlock(block, sourceIndex) {
    const view = blockView(block, sourceIndex);
    if (!view) return null;
    view.dataset.author = block.author === "model" ? "model" : "code";
    if (block.author === "model") view.prepend(h("span.author-mark", { text: "模型撰写" }));
    return view;
  }

  function blockView(block, sourceIndex) {
    switch (block.type) {
      case "business": return businessBlock(block);
      case "order_facts": return orderFactsBlock(block);
      case "policy_card": return cardBlock(block, sourceIndex);
      case "route": return routeBlock(block);
      case "path": return h("div.callout.info", h("strong", { text: "办理路径：" }), block.text);
      case "ask": return askBlock(block);
      // The open card is drawn once, live, at the end of the thread (confirmCard); the block only declares it.
      case "return_card": return null;
      // B8: a safety line is a code-written warning, drawn first and marked as one.
      case "text": return block.safety ? h("div.callout.warn", { text: block.text })
        : h("div.answer-text" + (block.fallback ? ".muted" : ""), { text: block.text });
      default: return null;
    }
  }

  function businessBlock(block) {
    const wrap = h("div.biz");
    const tone = !block.ok || block.status === "ineligible" || block.status === "blocked" ? "warn" : null;
    if (block.status !== "order_found") {
      wrap.append(tone ? h("div.callout.warn", { text: block.text }) : h("div.answer-text", { text: block.text }));
    } else if (block.lead) {
      wrap.append(h("div.answer-text", { text: block.lead }));
    }
    if (block.error_code === "UNSOURCED_FIELD") {
      const order = orderPick(true);
      wrap.append(h("p.small.muted", { text: `办理信息需要能在你这句话里逐字核对到。可以把订单号或你名下订单的商品名写进同一句话再发一次，例如“退货 ${order}”。` }));
    }
    if (block.orders?.length) wrap.append(h("div.orders-inline", block.orders.map(order => orderCard(order, { compact: true, notes: block.status === "order_found", apply: true }))));
    if (block.status === "order_found" && block.note) wrap.append(h("p.small.muted", { text: block.note }));
    if (block.candidate_orders?.length) {
      // Only the backend's candidates (I13): an eligible order opens a card with one click (no model call);
      // the others stay visible, greyed, with the rule's reason.
      wrap.append(h("div.pick-group", h("span.pick-label", { text: "选择订单" }),
        h("div.chip-row", block.candidate_orders.map(order => {
          const open = order.return_eligible === true && !order.return_request;
          return h("button.chip", { type: "button", disabled: !open || state.busy,
            title: open ? "" : (order.eligibility_text || orderInfo(order.order_id)?.eligibility_text || ""),
            text: `${order.order_id} · ${order.product_name}` + (open ? "" : "（不可申请）"),
            onclick: () => requestCard(order.order_id) });
        }))));
    }
    return wrap;
  }

  /* B5b: the own orders the answer chose to show, as the server declared them (read-only facts; an order that can be
     returned keeps its "申请退货" button, D3). */
  function orderFactsBlock(block) {
    return h("div.biz",
      h("div.orders-inline", (block.orders || []).map(order => orderCard(order, { compact: true, notes: true, apply: true }))),
      block.note ? h("p.small.muted", { text: block.note }) : null);
  }

  /* D7: the card the answer cited, whole. B5f: the selected outcomes come first, as the direct answer, open with their
     conditions; the card's title follows as their basis; its other outcomes stay folded, and its exceptions and sources
     are one click away. Nothing says the card applies to this order: the conditions are shown, not judged. */
  function cardBlock(block, sourceIndex) {
    const refs = new Map((block.references || []).map(ref => [ref.evidence_id, ref.quotes || []]));
    const chips = h("div.source-chips");
    const seen = new Set();
    for (const identifier of block.evidence_ids || []) {
      const source = sourceIndex.get(identifier);
      if (!source || seen.has(source.title)) continue;
      seen.add(source.title);
      const upload = identifier.startsWith("upload:");
      chips.append(h("button.source-chip" + (upload ? ".upload" : ""), { type: "button", title: source.title,
        onclick: () => openSource(source, refs.get(identifier) || []) }, icon(upload ? "upload" : "doc"), h("span", { text: source.title })));
    }
    const outcome = row => h("div.rule-branch",
      h("p.rule-outcome", { text: row.outcome }),
      row.premises?.length ? h("div.rule-conditions", h("strong", { text: "适用条件：" }),
        h("ul", row.premises.map(item => h("li", { text: item })))) : null);
    const others = block.other_outcomes || [];
    return h("div.rules", h("article.rule",
      (block.selected || []).map(outcome),
      h("div.rule-head", h("span.small.muted", { text: "依据" }),
        h("span.badge.brand", { text: DOMAIN_LABELS[block.effect_domain] || "规则" }), h("strong", { text: block.card_title })),
      others.length ? h("details", h("summary", icon("chevron"), `这张卡的其他情形（${others.length}）`),
        others.map(outcome)) : null,
      block.exceptions?.length ? h("details", h("summary", icon("chevron"), `例外与注意（${block.exceptions.length}）`),
        h("ul", block.exceptions.map(item => h("li", { text: item })))) : null,
      seen.size ? chips : null));
  }

  /* The one follow-up question: the model's words, the code's options (a click sends the option as the next message). */
  function askBlock(block) {
    return h("div.followup",
      h("p", { text: block.text }),
      block.options?.length ? h("div.chip-row", block.options.map(choice => h("button.chip", { type: "button", text: choice,
        disabled: state.busy, onclick: () => send(choice) }))) : null);
  }

  function routeBlock(block) {
    const card = h("div.route-card");
    if (block.origin && block.destination) {
      card.append(h("div.route-top",
        h("span.route-dir", icon("route"), `${block.origin} → ${block.destination}`),
        block.amount_text ? h("span.route-amount", "¥" + block.amount_text, h("small", { text: "首重表列参考" })) : null));
    }
    card.append(h("p", { text: block.text }));
    return card;
  }

  function orderInfo(orderId) { return state.orders.find(order => order.order_id === orderId); }

  function orderCard(order, { compact = false, notes = false, apply = false } = {}) {
    const full = orderInfo(order.order_id) || order;
    // B3e: an order whose card is open in this session shows that card (at the end of the thread), no second button.
    const canApply = !full.return_request && full.return_eligible === true
      && !openCards().some(card => card.order_id === full.order_id);
    const status = full.return_request ? ["已申请退货", "success"]
      : full.return_eligible === true ? ["可申请退货", "brand"]
      : full.return_eligible === false ? [full.eligibility_code === "outside_window" ? "已超出 7 天" : "不可退货", "danger"] : null;
    const thumb = h("div.order-thumb" + (full.category === "food" ? ".food" : ""), icon("box"));
    const card = h("div.order-card", thumb,
      h("div", h("strong", { text: full.product_name }),
        h("small", { text: `订单 ${full.order_id} · 签收于 ${full.delivered_at}` + (full.opened ? " · 已拆封" : "") })),
      h("div.order-price", h("b", { text: money(full.paid_amount_cents) }),
        status ? h("span.badge." + status[1], { text: status[0] }) : null));
    if (compact && notes && !full.return_request) {
      // The server words the verdict (eligibility_text); the order's own note is model-facing text.
      const note = full.return_eligible === false || full.last_return_day ? full.eligibility_text || "" : "";
      if (note) card.append(h("small.muted.order-note", { text: note }));
    }
    if (compact && apply && canApply) {
      // D3: an order that can be returned offers the button; the card comes from POST /cards, not chat.
      card.append(h("div.order-actions", h("button.btn.small.secondary", { type: "button", text: "申请退货",
        disabled: state.busy, onclick: () => requestCard(full.order_id) })));
    }
    if (!compact) {
      const actions = h("div.order-actions",
        h("button.btn.small.secondary", { type: "button", text: "查询这单", disabled: state.busy,
          onclick: () => send("帮我查一下订单 " + full.order_id) }),
        canApply ? h("button.btn.small.secondary", { type: "button", text: "申请退货", disabled: state.busy,
          onclick: () => requestCard(full.order_id) }) : null);
      card.append(actions);
      if (full.eligibility_text && full.return_eligible === false && !full.return_request) {
        card.append(h("small.muted.order-note", { text: full.eligibility_text }));
      }
    }
    return card;
  }

  /* B3e: a "申请退货" click names a new card or the one still open for that order (card_outcome). */
  function cardNote(response) {
    return response?.card_outcome === "reshown" ? "这一单的确认卡仍然有效，尚未提交" : "确认卡已生成";
  }

  /* I13 (B3e): a card is drawn from the server's declaration of it, the return_card block of the latest turn or
     "申请退货" click that showed it: the order facts and the reason options come from there, nothing else. */
  function declaredCard(card) {
    for (let index = state.entries.length - 1; index >= 0; index -= 1) {
      const response = state.entries[index].response || {};
      const block = [...(response.presentation?.blocks || []), response.return_card]
        .find(item => item?.type === "return_card" && item.action_id === card.action_id);
      if (block) return block;
    }
    return null;
  }

  /* Confirmation protocol v3: the card binds the order; the reason is picked here, from the catalogue the card
     was declared with, and travels only in the click. Confirm stays disabled until a valid reason is chosen.
     "暂不提交" voids the card through its own endpoint (B3b), not through a chat message. */
  function confirmCard(card) {
    const decline = h("button.btn.secondary", { type: "button", disabled: state.busy, onclick: () => voidAction(card),
      text: "暂不提交" });
    if (cardFolded(card)) {
      return h("section.confirm-card.folded", { "aria-label": "退货确认卡（已收起）" },
        h("div.confirm-head", icon("alert"), h("strong", { text: "退货确认卡" }), h("span.badge.accent", { text: "尚未提交" })),
        h("p.fold-note", { text: `上一条消息没有处理完。请先核对这张卡：订单 ${card.order_id}，确认仍要退这一单，再展开选择原因并提交。` }),
        h("div.confirm-actions",
          h("button.btn", { type: "button", disabled: state.busy, text: "展开这张卡",
            onclick: () => { state.unfolded = card.action_id; renderThread(); scrollToEnd(); } }),
          decline));
    }
    const declared = declaredCard(card);
    const order = declared?.order;
    const catalogue = declared?.reasons || { options: [], other_text_limit: 0 };
    const limit = catalogue.other_text_limit;
    const expires = card.expires_at ? clock(card.expires_at, { timeOnly: true }) : "";
    const choice = { code: null };
    const other = h("textarea.reason-other", { rows: 2, maxlength: limit, placeholder: `请写明原因（不超过 ${limit} 字）`,
      "aria-label": "其他原因说明" });
    other.hidden = true;
    const submit = h("button.btn.warn", { type: "button", disabled: true,
      onclick: () => confirmAction(card, choice.code, choice.code === "other" ? other.value.trim() : "") },
      icon("check"), "确认提交");
    const update = () => {
      const text = other.value.trim();
      other.hidden = choice.code !== "other";
      submit.disabled = state.busy || !choice.code || (choice.code === "other" && (!text || text.length > limit));
    };
    other.addEventListener("input", update);
    const options = h("div.reason-options", { role: "radiogroup", "aria-label": "退货原因" },
      catalogue.options.map(option => h("label.reason-option",
        h("input", { type: "radio", name: "return-reason-" + card.action_id, value: option.code,
          onchange: () => { choice.code = option.code; update(); } }),
        h("span", { text: option.label }))));
    const days = order && typeof order.days_since_delivery === "number"
      ? (order.days_since_delivery === 0 ? "签收当天" : `签收后第 ${order.days_since_delivery} 天`) : "";
    const rule = state.ordersMeta?.return_shipping_rule;
    return h("section.confirm-card", { "aria-label": "退货确认卡" },
      h("div.confirm-head", icon("alert"), h("strong", { text: "退货确认卡" }), h("span.badge.accent", { text: "尚未提交" })),
      h("dl.confirm-grid",
        h("dt", { text: "订单" }), h("dd", { text: card.order_id }),
        order ? [h("dt", { text: "商品" }), h("dd", { text: `${order.product_name}（${money(order.paid_amount_cents)}）` })] : null,
        order ? [h("dt", { text: "签收日" }), h("dd", { text: order.delivered_at + (days ? `（${days}）` : "") })] : null,
        order?.last_return_date ? [h("dt", { text: "最后可退日" }), h("dd", { text: order.last_return_date })] : null,
        order?.condition_label ? [h("dt", { text: "商品状态" }), h("dd", { text: order.condition_label })] : null,
        expires ? [h("dt", { text: "有效期" }), h("dd", { text: `10 分钟内有效（至 ${expires}）` })] : null),
      h("div.pick-group", h("span.pick-label", { text: "退货原因（必选）" }), options, other),
      rule ? h("details.shipping-rule", h("summary", icon("chevron"), "退回运费的规则"),
        h("p.small.muted", { text: rule.title }),
        h("ul", rule.branches.map(branch => h("li", h("span", { text: branch.outcome }),
          branch.premises.length ? h("small.muted", { text: " 适用条件：" + branch.premises.join("；") }) : null))),
        rule.exceptions.length ? h("ul", rule.exceptions.map(item => h("li.small.muted", { text: item }))) : null) : null,
      h("div.confirm-actions", submit, decline),
      h("p.fine", { text: "提交后生成一条退货申请；重复点击不会重复提交。" }));
  }

  async function confirmAction(card, reasonCode, reasonText) {
    if (state.busy || !reasonCode) return;
    state.pending = { kind: "confirm", identity: state.auth.identity, sid: state.current,
      path: "/sessions/" + encodeURIComponent(state.current) + "/cards/" + encodeURIComponent(card.action_id) + "/confirm",
      body: { version: card.version, reason_code: reasonCode, reason_text: reasonText, request_id: requestId() } };
    store.set(PENDING_KEY, state.pending);
    await runPending();
  }

  async function voidAction(card) {
    if (state.busy) return;
    state.pending = { kind: "void", identity: state.auth.identity, sid: state.current,
      path: "/sessions/" + encodeURIComponent(state.current) + "/cards/" + encodeURIComponent(card.action_id) + "/void",
      body: { request_id: requestId() } };
    store.set(PENDING_KEY, state.pending);
    await runPending();
  }

  async function requestCard(orderId) {
    if (state.busy) return;
    if (!await ensureSession()) return;
    state.pending = { kind: "card", identity: state.auth.identity, sid: state.current,
      path: "/sessions/" + encodeURIComponent(state.current) + "/cards",
      body: { order_id: orderId, request_id: requestId() } };
    store.set(PENDING_KEY, state.pending);
    closeOverlays();
    await runPending();
  }

  /* B4a (plan §6.3): "找人工" opens this session's ticket or appends to its open one, and the ticket card's "撤回工单"
     withdraws it; neither calls a model and the server accepts both while a turn runs. So they do not wait for the
     one pending slot of messages and card clicks: a ticket click has its own, and after a network failure the next
     press of the same button reuses its request id, so a lost response never adds a second event. */
  async function ticketAction(kind, ticketId = null) {
    if (state.ticketBusy) return;
    if (!await ensureSession()) return;
    const sid = state.current;
    const retry = state.ticketRetry;
    const request = retry && retry.kind === kind && retry.sid === sid && retry.ticketId === ticketId ? retry
      : { kind, sid, ticketId, requestId: requestId() };
    const path = "/sessions/" + encodeURIComponent(sid)
      + (kind === "handoff" ? "/handoff" : "/tickets/" + encodeURIComponent(ticketId) + "/withdraw");
    state.ticketBusy = true;
    els.handoff.disabled = true;
    try {
      const result = await api(path, { body: { request_id: request.requestId } });
      state.ticketRetry = null;
      if (state.busy) {
        // A turn is still running: show the click now; the refresh after the turn reads it back from history.
        state.entries.push({ entry: kind === "handoff" ? "ticket_handoff" : "ticket_withdraw",
          request_id: request.requestId, user_message: "", status: "completed",
          response: { reply: result.last_reply, ticket_outcome: result.ticket_outcome, ticket_card: result.ticket_card } });
      }
    } catch (error) {
      state.ticketRetry = error.network ? request : null;
      toast(error.network ? error.message + " 再点一次会沿用原请求 ID，不会重复提交。" : error.message, "error");
    } finally {
      state.ticketBusy = false;
      els.handoff.disabled = false;
      if (!state.busy && state.current === sid) {
        try { await refreshCurrent(); } catch (_) { renderThread(); }
        loadSessions();
      } else if (state.current === sid) { renderThread(); renderPanel(); scrollToEnd(); }
    }
  }

  /* The latest declaration of each ticket in this session's saved entries (ticket_card), by ticket id. */
  function sessionTickets() {
    const latest = new Map();
    for (const entry of state.entries) {
      const block = entry.response?.ticket_card;
      if (block?.type === "ticket") latest.set(block.ticket_id, block);
    }
    return latest;
  }

  /* A ticket click's code reply with the ticket card it declared (I13). A ticket's card is drawn live only at its
     latest declaration, so the page shows the recorded status and a withdrawn ticket offers no second withdrawal. */
  function ticketEntry(entry) {
    const response = entry.response || {};
    if (entry.status !== "completed") {
      return h("div.system-note", icon("alert"),
        (entry.entry === "ticket_handoff" ? "没有提交人工工单：" : "没有撤回人工工单：") + (response.error?.message || ""));
    }
    const card = h("div.answer-card", h("div.answer-text", { text: response.reply || "" }));
    const block = response.ticket_card;
    if (block?.type === "ticket" && sessionTickets().get(block.ticket_id) === block) card.append(ticketCard(block));
    return assistantShell(card);
  }

  function ticketCard(block) {
    const open = (block.actions || []).includes("withdraw");
    return h("section.ticket-card", { "aria-label": "人工工单" },
      h("div.confirm-head", icon("user"), h("strong", { text: "人工工单 " + block.short_no }),
        h("span.badge" + (open ? ".accent" : ""), { text: "当前状态：" + block.status_label })),
      open ? h("div.confirm-actions", h("button.btn.small.secondary", { type: "button", disabled: state.ticketBusy,
        text: "撤回工单", onclick: () => ticketAction("withdraw", block.ticket_id) })) : null);
  }

  async function recoverRun() {
    try {
      await api("/sessions/" + encodeURIComponent(state.current) + "/recover", { body: { request_id: requestId() } });
      toast("已结束中断的处理轮次，可以继续对话。");
      await refreshCurrent();
    } catch (error) { toast(error.message, "error"); }
  }

  /* ---------------- panel ---------------- */
  function renderPanel() {
    if (!els.panel) return;
    const sources = collectSources();
    const tabs = [["orders", "我的订单", state.orders.length], ["task", "当前事项", null], ["sources", "引用来源", sources.length]];
    els.tabs.replaceChildren(...tabs.map(([key, label, count]) => h("button.tab" + (state.tab === key ? ".active" : ""),
      { type: "button", role: "tab", "aria-selected": String(state.tab === key), onclick: () => { state.tab = key; renderPanel(); } },
      label, count != null ? h("span.count", { text: count }) : null)));
    const body = els.panelBody;
    body.replaceChildren();
    if (state.tab === "orders") {
      if (!state.orders.length) body.append(h("p.empty-state", { text: "这个账号下没有订单" }));
      state.orders.forEach(order => body.append(orderCard(order)));
    } else if (state.tab === "task") {
      body.append(taskPanel());
    } else {
      if (!sources.length) body.append(h("p.empty-state", { text: "本次对话还没有引用政策原文" }));
      for (const source of sources) body.append(sourceRow(source, []));
    }
  }

  function taskPanel() {
    const snapshot = state.snapshot;
    if (!snapshot) return h("p.empty-state", { text: "发送第一条消息后，这里会显示办理进度。" });
    const { status: [label], orders } = sessionStatus(snapshot);
    const wrap = h("div.status-card",
      h("dl.kv",
        h("dt", { text: "状态" }), h("dd", { text: label || "—" }),
        h("dt", { text: "订单" }), h("dd", { text: orders.join("、") || "未指定" }),
        snapshot.return_request ? [h("dt", { text: "最近提交" }), h("dd", { text: snapshot.return_request.id + " · " + snapshot.return_request.status })] : null,
        h("dt", { text: "处理轮次" }), h("dd", { text: `${snapshot.execution_totals?.runs ?? 0} 轮 · 模型调用 ${snapshot.execution_totals?.model_attempts ?? 0} 次` })));
    for (const card of openCards()) wrap.append(h("div.callout.warn", { text: `待确认：${card.order_id} 的退货确认卡。请在对话下方的卡上选择原因后确认。` }));
    // B4a: each ticket of this session with its recorded status (only the system changes it).
    for (const block of sessionTickets().values()) {
      wrap.append(h("div.callout.info", { text: `人工工单 ${block.short_no}，当前状态：${block.status_label}` }));
    }
    // The card's own "暂不提交" is the one way to drop it (B3b); this panel only offers recovery of a stuck turn.
    if (snapshot.busy) wrap.append(h("div.chip-row",
      h("button.btn.small.warn", { type: "button", text: "恢复中断", onclick: recoverRun })));
    wrap.append(h("p.panel-note", { text: "会话 ID：" + snapshot.session_id }));
    return wrap;
  }

  function collectSources() {
    const map = new Map();
    for (const entry of state.entries) {
      for (const source of entry.response?.presentation?.source_cards || []) {
        if (!map.has(source.citation_id)) map.set(source.citation_id, source);
      }
    }
    return [...map.values()];
  }

  function sourceKind(source) {
    const id = source.citation_id || "";
    if (id.startsWith("upload:")) return ["商家上传", "violet"];
    if (id.startsWith("route:")) return ["补偿表", "accent"];
    if (id.startsWith("rule:")) return ["规则摘录", "info"];
    return ["官网快照", "brand"];
  }

  function sourceRow(source, quotes) {
    const [kind, tone] = sourceKind(source);
    return h("button.source-row", { type: "button", onclick: () => openSource(source, quotes) },
      h("strong", { text: source.title }), h("small", h("span.badge." + tone, { text: kind }), " ", (source.content || "").slice(0, 60) + "…"));
  }

  function openSource(source, quotes) {
    const [kind, tone] = sourceKind(source);
    const href = publicUrl(source.url || "");
    const body = [
      h("dl.meta-grid",
        h("dt", { text: "类型" }), h("dd", h("span.badge." + tone, { text: kind })),
        href ? [h("dt", { text: "原网页" }), h("dd", h("a", { href, target: "_blank", rel: "noopener noreferrer" }, href, " ", icon("external")))] : null,
        h("dt", { text: "段落" }), h("dd.mono", { text: source.citation_id || "" }),
        source.sha256 ? [h("dt", { text: "SHA-256" }), h("dd.mono", { text: source.sha256.slice(0, 16) + "…" })] : null),
      source.scope_note ? h("div.callout.info", { text: source.scope_note }) : null,
      quotes.length ? h("p.small.muted", { text: `黄色高亮是规则卡引用的 ${quotes.length} 段原文。` }) : null,
      h("div.divider"),
      highlighted(source.content || "", quotes),
    ];
    const upload = (source.citation_id || "").startsWith("upload:");
    openDrawer({ title: source.title, subtitle: upload ? "上传文档原文（未与官方来源核对）" : "政策原文（保存的快照，生效日期未核实）", body });
  }

  function openSources(sources, blocks) {
    const quotes = new Map();
    for (const block of blocks) for (const ref of block.type === "policy_card" ? block.references || [] : []) {
      quotes.set(ref.evidence_id, [...(quotes.get(ref.evidence_id) || []), ...ref.quotes]);
    }
    const seen = new Set();
    const rows = [];
    for (const source of sources) {
      if (seen.has(source.citation_id)) continue;
      seen.add(source.citation_id);
      rows.push(sourceRow(source, quotes.get(source.citation_id) || []));
    }
    openDrawer({ title: "参考来源", subtitle: "本轮答复展示的全部原文段落", body: h("div.panel-body", rows) });
  }

  /* ---------------- trace ---------------- */
  async function openTrace(response, entry) {
    const graph = response.graph || {};
    const plan = graph.plan || {};
    const blocks = response.presentation?.blocks || [];
    const business = blocks.find(block => block.type === "business");
    const cards = blocks.filter(block => block.type === "policy_card");
    const items = response.answer_validation?.items || [];
    const rejected = (response.trace || []).filter(entry => entry.type === "item_rejected");
    const nodes = graph.nodes || [];
    const names = { understand: "理解需求", business: "订单与申请", policy: "政策规则卡", answer: "组织答复" };
    const stats = h("div.stat-row",
      h("div.stat", h("b.run-seconds", { text: "…" }), h("span", { text: "本轮耗时" })),
      h("div.stat", h("b.run-models", { text: "…" }), h("span", { text: "模型调用" })),
      h("div.stat", h("b", { text: response.usage?.total_tokens ? String(response.usage.total_tokens) : "—" }), h("span", { text: "tokens" })));
    // The run mode lives here only (B1): the chat itself carries no mode tag.
    const mode = response.mode || state.info?.mode;
    const modeRow = h("div.kvline", "运行方式：", modeBadge(mode));
    const offlineNote = mode === "live_agent" ? null
      : h("p.muted.small", { text: "离线演示：「模型调用」由本地关键词脚本模拟，不调用 API，也不计 tokens。" });
    const timeline = h("div.timeline");
    const detail = {
      understand: [
        plan.goal ? h("div", "目标：", plan.goal) : null,
        (plan.items || []).length ? h("div", { text: "事项：" + plan.items.map(item => `${item.index}. ${item.kind}「${item.quote}」`).join("；") }) : h("div", { text: "没有列出事项" }),
        rejected.length ? h("div.callout.warn", { text: "没有通过核对、未办理的事项：" + rejected.map(row => `${row.index}. ${row.kind}`).join("；") }) : null,
      ],
      business: [business ? h("div.kvline", "结果：", h("code", { text: business.status }), business.text ? " " + business.text.slice(0, 60) : "") : h("div", { text: "本轮没有业务操作" })],
      policy: [h("div", { text: `整份规则卡目录供答复选择，本轮引用 ${cards.length} 张` })],
      answer: [
        cards.length ? h("div", "规则卡：" + cards.map(card => card.card_title).join("、")) : h("div", { text: "没有引用规则卡" }),
        items.length ? h("div", { text: "事项：" + items.map(item => `${item.index}. ${item.kind} → ${item.disposition || "程序处理"}`).join("；") }) : null,
        response.answer_validation?.fallback ? h("div.callout.warn", { text: "模型写的短文没有通过检查，已换成固定的兜底句。" }) : null,
      ],
    };
    ["understand", "business", "policy", "answer"].forEach((key, position) => {
      const ran = nodes.includes(key) || (key === "business");
      timeline.append(h("div.tl-item", h("div.tl-dot" + (ran ? "" : ".skip"), { text: String(position + 1) }),
        h("div", h("h3", { text: names[key] + (ran ? "" : "（本轮跳过）") }), h("div.detail", detail[key]))));
    });
    const events = h("div.small.muted", { text: "正在读取运行事件…" });
    openDrawer({ title: "处理过程", subtitle: "四个步骤；模型只提交结构化提案，结论由程序渲染", body: [
      modeRow, stats, offlineNote, timeline,
      h("details", h("summary", { text: "保存的原始回复文本" }), h("div.raw-reply", { text: response.reply || "" })),
      h("div.divider"), h("p.eyebrow", { text: "运行事件" }), events,
    ] });
    if (!response.run_id || !state.current) { events.textContent = "没有运行记录。"; return; }
    try {
      const run = await api("/sessions/" + encodeURIComponent(state.current) + "/runs/" + encodeURIComponent(response.run_id));
      const start = run.events[0]?.created_at || run.started_at;
      const end = run.finished_at || run.events[run.events.length - 1]?.created_at || start;
      document.querySelector(".run-seconds").textContent = ((end - run.started_at) || 0).toFixed(1) + " 秒";
      document.querySelector(".run-models").textContent = run.model_attempts + " 次";
      events.replaceChildren(...run.events.map(event => h("div.kvline",
        h("code", { text: "+" + ((event.created_at - start) * 1000).toFixed(0) + "ms" }), " ", event.kind,
        event.details?.step ? " · " + event.details.step : "", event.details?.tool_name ? " · " + event.details.tool_name : "")));
    } catch (error) { events.textContent = error.message; }
  }

  boot();
})();
