"use strict";
/* Knowledge base console. Every edit goes to a single draft that is checked
   with the runtime's own loader; publishing makes an immutable version that
   new chat turns read immediately. Checks prove verbatim quotes and source
   binding only, never that a rule is correct or currently in force. */
(() => {
  const { h, append, icon, store, createApi, toast, clock, publicUrl, highlighted, openDrawer, openModal,
    closePanel, confirmDialog, modeBadge } = window.SA;
  /* replaceChildren() would render a literal "null" for optional children; skip them instead. */
  const fill = (el, ...children) => { el.replaceChildren(); return append(el, children); };
  const ADMIN_KEY = "sa.admin.v1";
  const root = document.getElementById("root");
  const api = createApi(() => state.auth?.token);
  const state = {
    auth: null, overview: null, view: "cards", scope: "draft", cards: [], filter: { q: "", domain: "" },
    selected: null, detail: null, editing: null, check: null, documents: [], picked: new Set(),
    sectionView: null, versions: [], history: [], version: null, diff: null, modelView: null,
  };
  const els = {};
  const ORIGIN = { baseline: ["基线", ""], edited: ["已修改", "info"], created: ["新增", "success"], ai_draft: ["AI 起草", "violet"] };
  const KIND = { official_snapshot: ["官网快照", "brand"], merchant_upload: ["上传文档", "violet"], authored_rule: ["规则摘录", "info"] };
  let domains = {};
  let sourceKinds = {};

  /* ---------------- auth ---------------- */
  async function boot() {
    state.auth = store.get(ADMIN_KEY);
    if (!state.auth?.token || !(await tryOverview())) {
      state.auth = null;
      try {
        const demo = await api("/demo/identities", { token: "" });
        if (demo.identities.some(item => item.role === "admin")) {
          state.auth = await api("/demo/login", { body: { identity: "kb-admin" }, token: "" });
          store.set(ADMIN_KEY, state.auth);
        }
      } catch (_) { /* no demo sign-in */ }
      if (!state.auth || !(await tryOverview())) { renderDenied(); return; }
    }
    renderShell();
    await loadCards();
    selectFirstCard();
  }

  async function tryOverview() {
    try {
      state.overview = await api("/kb/overview");
      domains = state.overview.effect_domains; sourceKinds = state.overview.source_kinds || {};
      return true;
    }
    catch (_) { return false; }
  }

  function renderDenied() {
    root.className = "boot";
    root.replaceChildren(h("div.empty-detail", icon("book"),
      h("h2", { text: "需要知识库管理员身份" }),
      h("p", { text: "用 app 命令启动的演示会自动登录管理员；普通 service 服务未启用知识库管理。" }),
      h("a.btn.secondary", { href: "/" , text: "返回对话" })));
  }

  /* ---------------- shell ---------------- */
  function renderShell() {
    els.status = h("div.grow");
    els.actions = h("div.actions");
    els.nav = h("nav.kb-nav", { "aria-label": "知识库导航" });
    els.list = h("section.kb-list");
    els.detail = h("section.kb-detail");
    root.className = "";
    root.replaceChildren(h("div.kb-app",
      h("header.kb-top",
        h("div.brand", h("div.brand-mark", icon("book")), h("h1", { text: "知识库管理" })),
        els.status, els.actions),
      h("div.kb-body", els.nav, els.list, els.detail)));
    renderTop(); renderNav();
  }

  function draftChanges() {
    const diff = state.overview?.draft?.diff || {};
    return (diff.added?.length || 0) + (diff.removed?.length || 0) + (diff.changed?.length || 0)
      + (diff.sources_added?.length || 0) + (diff.sources_removed?.length || 0) + (diff.publishers ? 1 : 0);
  }

  function renderTop() {
    const ov = state.overview;
    const version = ov.active_version;
    fill(els.status,
      h("span.badge.brand", { title: version?.label || "", text: `已发布 v${version?.number ?? "?"}` }),
      version?.label ? h("span.small.muted", { text: version.label }) : null,
      ov.draft.dirty ? h("span.badge.accent", { text: `草稿有 ${draftChanges()} 处改动` }) : h("span.badge", { text: "草稿与发布一致" }),
      ov.draft.stale ? h("span.badge.danger", { text: "草稿基于旧版本" }) : null,
      ov.publisher_warning ? h("span.badge.danger", { text: ov.publisher_warning }) : null,
      modeBadge(ov.mode));
    fill(els.actions,
      h("button.btn.ghost.small", { type: "button", onclick: editPublishers, text: "来源发布方" }),
      h("button.btn.secondary.small", { type: "button", onclick: validateDraft }, icon("check"), "校验草稿"),
      h("button.btn.small", { type: "button", disabled: !ov.draft.dirty, onclick: publishDraft }, icon("upload"), "发布"),
      h("button.btn.ghost.small", { type: "button", disabled: !ov.draft.dirty, onclick: discardDraft, text: "放弃草稿" }),
      h("a.btn.ghost.small", { href: "/" }, icon("chevron"), "去对话测试"));
  }

  function renderNav() {
    const items = [["cards", "政策卡片", "book", state.overview?.draft_card_count ?? (state.cards.length || null)], ["sources", "来源文档", "doc", null],
      ["versions", "版本历史", "history", state.overview?.version_count], ["view", "模型视图", "search", null]];
    els.nav.replaceChildren(...items.map(([key, label, iconName, count]) => h("button.nav-item" + (state.view === key ? ".active" : ""),
      { type: "button", onclick: () => switchView(key), title: label }, icon(iconName), h("span", { text: label }),
      count ? h("span.count", { text: count }) : null)),
      h("p.nav-note", { text: state.overview?.scope_notice || "" }));
  }

  async function refreshOverview() { await tryOverview(); renderTop(); renderNav(); }

  async function switchView(view) {
    if (state.editing && !(await leaveEditor())) return;
    state.view = view; renderNav();
    if (view === "cards") { await loadCards(); renderDetail(); }
    if (view === "sources") { await loadSources(); renderSourceDetail(); }
    if (view === "versions") { await loadVersions(); }
    if (view === "view") await loadModelView();
  }

  async function leaveEditor() {
    const ok = await confirmDialog({ title: "放弃未保存的编辑？", message: "编辑器中的改动还没有保存到草稿。", confirmText: "放弃编辑", danger: true });
    if (ok) { state.editing = null; state.check = null; }
    return !!ok;
  }

  /* ---------------- cards ---------------- */
  async function loadCards() {
    try { state.cards = (await api("/kb/cards?scope=" + state.scope)).cards; } catch (error) { toast(error.message, "error"); }
    renderCardList(); renderNav();
  }

  function selectFirstCard() { if (!state.selected && state.cards[0]) openCard(state.cards[0].id); else renderDetail(); }

  function renderCardList() {
    const search = h("input.input", { type: "search", placeholder: "搜索标题、ID 或范围", value: state.filter.q,
      oninput: event => { state.filter.q = event.target.value.trim(); renderCardItems(); } });
    const domain = h("select.select", { onchange: event => { state.filter.domain = event.target.value; renderCardItems(); } },
      h("option", { value: "", text: "全部规则类型" }),
      Object.entries(domains).map(([key, label]) => h("option", { value: key, text: label, selected: state.filter.domain === key })));
    const seg = h("div.seg", ["draft", "active"].map(scope => h("button" + (state.scope === scope ? ".on" : ""), { type: "button",
      text: scope === "draft" ? "草稿" : "已发布", onclick: async () => { state.scope = scope; await loadCards(); if (state.selected) openCard(state.selected); } })));
    els.cardItems = h("div.list-body");
    els.list.replaceChildren(h("div.list-head",
      h("div.row", h("h2", { text: "政策卡片" }), seg,
        h("button.btn.small", { type: "button", onclick: () => startEditor(null) }, icon("plus"), "新建")),
      search, domain), els.cardItems);
    renderCardItems();
  }

  function renderCardItems() {
    const q = state.filter.q.toLowerCase();
    const rows = state.cards.filter(card => (!state.filter.domain || card.effect_domain === state.filter.domain)
      && (!q || [card.title, card.id, card.scope].some(value => value.toLowerCase().includes(q))));
    els.cardItems.replaceChildren(...rows.map(card => {
      const [origin, tone] = ORIGIN[card.origin] || [card.origin, ""];
      return h("button.item" + (state.selected === card.id ? ".active" : ""), { type: "button", onclick: () => openCard(card.id) },
        h("strong", { text: card.title }),
        h("div.tags", h("span.badge.brand", { text: domains[card.effect_domain] || card.effect_domain }),
          card.origin !== "baseline" ? h("span.badge." + (tone || "info"), { text: origin }) : null,
          card.uses_upload ? h("span.badge.violet", { text: "上传来源" }) : null,
          h("code", { text: card.id })));
    }));
    if (!rows.length) els.cardItems.append(h("p.empty-detail", { text: "没有匹配的卡片" }));
    els.cardItems.querySelector(".item.active")?.scrollIntoView({ block: "nearest" });
  }

  async function openCard(id) {
    if (state.editing && !(await leaveEditor())) return;
    state.selected = id;
    renderCardItems();
    try { state.detail = (await api(`/kb/cards/${encodeURIComponent(id)}?scope=${state.scope}`)).card; }
    catch (error) { state.detail = null; if (error.status !== 404) toast(error.message, "error"); }
    renderDetail();
  }

  function renderDetail() {
    if (state.view !== "cards") return;
    const wrap = h("div.detail-inner");
    els.detail.replaceChildren(wrap);
    if (state.editing) { wrap.append(editorView()); return; }
    const card = state.detail;
    if (!card) {
      wrap.append(h("div.empty-detail", icon("book"), h("p", { text: state.scope === "draft" ? "选择左侧卡片查看，或新建一张卡片。" : "这张卡片不在已发布版本中。" })));
      return;
    }
    const [origin, tone] = ORIGIN[card.kb?.origin || "baseline"] || ["", ""];
    wrap.append(h("div.detail-head",
      h("div.grow", h("h2", { text: card.title }),
        h("div.sub", h("span.badge.brand", { text: domains[card.effect_domain] || card.effect_domain }),
          h("span.badge", { text: sourceKinds[card.source_kind || "platform"] || card.source_kind }),
          h("span.badge." + (tone || "info"), { text: origin }), h("code.mono.muted", { text: card.id }),
          card.kb?.updated_at ? h("span.small.muted", { text: "更新于 " + clock(card.kb.updated_at) }) : null)),
      state.scope === "draft" ? h("div.chip-row",
        h("button.btn.secondary.small", { type: "button", onclick: () => startEditor(card) }, icon("edit"), "编辑"),
        h("button.btn.ghost.small", { type: "button", onclick: () => deleteCard(card) }, icon("trash"), "删除")) : null));
    if ((card.aliases || []).length) wrap.append(h("div.section", h("h3", { text: "别名" }),
      h("div.chip-row", card.aliases.map(alias => h("span.badge", { text: alias })))));
    wrap.append(h("div.section", h("h3", { text: "适用范围" }), h("p", { text: card.scope })));
    wrap.append(h("div.section", h("h3", { text: "条件" }), h("ul", card.conditions.map(item => h("li", { text: item })))));
    wrap.append(h("div.section", h("h3", { text: "结论与各自前提" }),
      card.outcomes.map((outcome, index) => h("div.outcome", h("p", { text: outcome }),
        h("div.premises", h("b", { text: card.premises_authored ? "前提：" : "前提（由范围与条件推出，保存后固定）：" }),
          (card.outcome_premises[index] || []).join("；") || "未填写")))));
    if (card.exceptions.length) wrap.append(h("div.section", h("h3", { text: "例外与注意" }), h("ul", card.exceptions.map(item => h("li", { text: item })))));
    wrap.append(h("div.section", h("h3", { text: `原文引用（${card.references.length}）` }), card.references.map(referenceView)));
    if (card.kb?.review_note) wrap.append(h("div.callout.info", { text: "备注：" + card.kb.review_note }));
  }

  function referenceView(ref) {
    const source = ref.source;
    const [kind, tone] = KIND[source?.kind] || ["来源", ""];
    const href = publicUrl(source?.url || "");
    return h("div.ref",
      h("div.ref-head", h("span.badge." + tone, { text: kind }), h("strong", { text: source?.title || ref.evidence_id }),
        href ? h("a.small", { href, target: "_blank", rel: "noopener noreferrer" }, "原网页 ", icon("external")) : null),
      ref.quotes.map((quote, index) => h("div.quote" + (ref.quotes_found?.[index] === false ? ".missing" : ""), { text: quote })),
      ref.text ? h("details", h("summary", { text: "展开整段原文（高亮为引用）" }), highlighted(ref.text, ref.quotes)) : null);
  }

  async function deleteCard(card) {
    const ok = await confirmDialog({ title: "从草稿删除这张卡片？", message: `「${card.title}」会在下次发布后从知识库移除；已发布版本不受影响，可随时回滚。`, confirmText: "删除", danger: true });
    if (!ok) return;
    try {
      await api(`/kb/draft/cards/${card.id}/delete`, { body: {} });
      toast("已从草稿删除");
      state.selected = null; state.detail = null;
      await Promise.all([loadCards(), refreshOverview()]);
      renderDetail();
    } catch (error) { toast(error.message, "error"); }
  }

  /* ---------------- editor ---------------- */
  function startEditor(card, preset = null) {
    const source = preset || card;
    state.scope = "draft";
    state.editing = {
      mode: card ? "edit" : "new", original: card?.id || null,
      id: source?.id || "", title: source?.title || "", scope: source?.scope || "",
      effect_domain: source?.effect_domain || "general_guidance", source_kind: source?.source_kind || "platform",
      aliases: [...(source?.aliases || [])],
      conditions: [...(source?.conditions || [""])],
      outcomes: (source?.outcomes || [""]).map((text, index) => ({ text, premises: [...((source?.outcome_premises || [])[index] || [""])] })),
      exceptions: [...(source?.exceptions || [])],
      references: (source?.references || []).map(ref => ({ evidence_id: ref.evidence_id, title: ref.source?.title || ref.title || ref.evidence_id,
        kind: ref.source?.kind || ref.kind || "", quotes: [...ref.quotes] })),
      review_note: source?.review_note ?? source?.kb?.review_note ?? "", origin: preset?.origin || null,
    };
    state.check = null;
    if (state.view !== "cards") { state.view = "cards"; renderNav(); renderCardList(); }
    renderDetail();
    scheduleCheck();
  }

  function proposal() {
    const e = state.editing;
    const clean = list => list.map(item => item.trim()).filter(Boolean);
    const body = { id: e.id.trim(), title: e.title.trim(), scope: e.scope.trim(), effect_domain: e.effect_domain,
      source_kind: e.source_kind, aliases: clean(e.aliases),
      conditions: clean(e.conditions), outcomes: e.outcomes.map(item => item.text.trim()).filter(Boolean),
      outcome_premises: e.outcomes.filter(item => item.text.trim()).map(item => clean(item.premises)),
      exceptions: clean(e.exceptions), review_note: e.review_note.trim(),
      references: e.references.map(ref => ({ evidence_id: ref.evidence_id, quotes: ref.quotes.map(q => q).filter(q => q.trim()) })) };
    if (e.mode === "edit") body.replaces = e.original;
    if (e.origin) body.origin = e.origin;
    return body;
  }

  let checkTimer = null;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    if (state.check) { state.check = { ...state.check, pending: true }; renderCheck(); }
    checkTimer = setTimeout(runCheck, 450);
  }

  async function runCheck() {
    if (!state.editing) return;
    try { await api("/kb/draft/cards/check", { body: { card: proposal() } }); state.check = { ok: true, errors: [] }; }
    catch (error) { state.check = { ok: false, errors: error.details || [{ field: "card", message: error.message }] }; }
    renderCheck();
  }

  const FIELD_LABELS = { id: "ID", title: "标题", scope: "适用范围", effect_domain: "规则类型", source_kind: "来源类型",
    aliases: "别名", conditions: "条件",
    outcomes: "结论", outcome_premises: "前提", exceptions: "例外", references: "原文引用", review_note: "备注", catalog: "整体", card: "卡片" };
  function fieldLabel(path) {
    return path.replace(/^([a-z_]+)/, (_, key) => FIELD_LABELS[key] || key)
      .replace(/\[(\d+)\]/g, (_, index) => " " + (Number(index) + 1)).replace(".quotes", " · 原文");
  }

  function renderCheck() {
    const box = document.querySelector(".check-box");
    if (!box) return;
    box.className = "check-box " + (!state.check || state.check.pending ? "wait" : state.check.ok ? "ok" : "bad");
    if (!state.check) { box.replaceChildren(h("span", { text: "正在校验…" })); return; }
    if (state.check.ok) {
      box.replaceChildren(h("strong", { text: state.check.pending ? "正在重新校验…" : "✓ 结构完整，所有引用与原文逐字一致" }),
        h("span.small", { text: "校验不判断规则是否正确或现行有效，请人工核对原文后再发布。" }));
    } else {
      box.replaceChildren(h("strong", { text: `还有 ${state.check.errors.length} 个问题` }),
        h("ul", state.check.errors.map(error => h("li", { text: `${fieldLabel(error.field)}：${error.message}` }))));
    }
    document.querySelectorAll("[data-field]").forEach(el => {
      const bad = state.check && !state.check.ok && state.check.errors.some(error => error.field === el.dataset.field
        || error.field.startsWith(el.dataset.field + "["));
      el.classList.toggle("invalid", !!bad);
    });
  }

  function textField(label, key, { area = false, hint = "", readonly = false } = {}) {
    const e = state.editing;
    const input = h(area ? "textarea.textarea" : "input.input", { value: e[key], readOnly: readonly,
      oninput: event => { e[key] = event.target.value; scheduleCheck(); } });
    if (area) input.value = e[key];
    return h("label.field", { dataset: { field: key } }, h("span", label, hint ? h("span.hint", { text: " · " + hint }) : null), input);
  }

  function listEditor(label, items, field, { placeholder = "", minimum = 0 } = {}) {
    const wrap = h("div.field", { dataset: { field } }, h("span", { text: label }));
    const list = h("div.list-editor");
    const draw = () => {
      list.replaceChildren(...items.map((value, index) => h("div.list-row",
        h("textarea.textarea", { rows: 2, placeholder, value, oninput: event => { items[index] = event.target.value; scheduleCheck(); } }),
        h("button.icon-btn", { type: "button", "aria-label": "删除这一项", disabled: items.length <= minimum,
          onclick: () => { items.splice(index, 1); draw(); scheduleCheck(); } }, icon("trash")))));
      list.querySelectorAll("textarea").forEach((area, index) => { area.value = items[index]; });
    };
    draw();
    wrap.append(list, h("button.btn.ghost.small.add-row", { type: "button", onclick: () => { items.push(""); draw(); } }, icon("plus"), "添加一项"));
    return wrap;
  }

  function editorView() {
    const e = state.editing;
    const outcomesWrap = h("div.field", { dataset: { field: "outcomes" } }, h("span", { text: "结论（每条结论配一组适用前提）" }));
    const drawOutcomes = () => {
      outcomesWrap.querySelectorAll(".outcome-editor, .add-outcome").forEach(el => el.remove());
      e.outcomes.forEach((outcome, index) => {
        const text = h("textarea.textarea", { rows: 2, placeholder: "在什么前提下，适用什么结果", oninput: event => { outcome.text = event.target.value; scheduleCheck(); } });
        text.value = outcome.text;
        outcomesWrap.append(h("div.outcome-editor", { dataset: { field: `outcome_premises[${index}]` } },
          h("div.row", h("strong", { text: "结论 " + (index + 1) }),
            h("button.icon-btn", { type: "button", "aria-label": "删除结论", disabled: e.outcomes.length <= 1,
              onclick: () => { e.outcomes.splice(index, 1); drawOutcomes(); scheduleCheck(); } }, icon("trash"))),
          text, listEditor("适用前提", outcome.premises, `outcome_premises[${index}]`, { placeholder: "例如：京东自营商品；签收后七天内", minimum: 1 })));
      });
      outcomesWrap.append(h("button.btn.ghost.small.add-row.add-outcome", { type: "button",
        onclick: () => { e.outcomes.push({ text: "", premises: [""] }); drawOutcomes(); } }, icon("plus"), "添加结论"));
    };
    drawOutcomes();
    const refsWrap = h("div.field", { dataset: { field: "references" } },
      h("span", "原文引用", h("span.hint", { text: " · 引用必须与来源逐字一致，可在原文中直接选取" })));
    const drawRefs = () => {
      refsWrap.querySelectorAll(".ref-editor, .add-ref").forEach(el => el.remove());
      e.references.forEach((ref, index) => {
        const [kind, tone] = KIND[ref.kind] || ["来源", ""];
        refsWrap.append(h("div.ref-editor", { dataset: { field: `references[${index}]` } },
          h("div.row", h("span.badge." + tone, { text: kind }), h("strong", { text: ref.title }),
            h("button.btn.secondary.small", { type: "button", onclick: () => openPassage(ref.evidence_id, ref) }, icon("doc"), "在原文中选取"),
            h("button.icon-btn", { type: "button", "aria-label": "删除引用", onclick: () => { e.references.splice(index, 1); drawRefs(); scheduleCheck(); } }, icon("trash"))),
          listEditor("引用原文", ref.quotes, `references[${index}].quotes`, { placeholder: "从原文复制的文字", minimum: 1 })));
      });
      refsWrap.append(h("button.btn.ghost.small.add-row.add-ref", { type: "button", onclick: () => pickSource(null) }, icon("plus"), "添加来源段落"));
    };
    drawRefs();
    state.editing.redrawRefs = drawRefs;
    const domainSelect = h("select.select", { onchange: event => { e.effect_domain = event.target.value; scheduleCheck(); } },
      Object.entries(domains).map(([key, label]) => h("option", { value: key, text: label, selected: e.effect_domain === key })));
    const kindSelect = h("select.select", { onchange: event => { e.source_kind = event.target.value; scheduleCheck(); } },
      Object.entries(sourceKinds).map(([key, label]) => h("option", { value: key, text: label, selected: e.source_kind === key })));
    return h("div.editor",
      h("div.detail-head", h("div.grow", h("h2", { text: e.mode === "new" ? "新建政策卡" : "编辑：" + (e.title || e.id) }),
        h("div.sub", e.origin === "ai_draft" ? h("span.badge.violet", { text: "AI 起草，待人工核对" }) : null,
          h("span.small.muted", { text: "保存只写入草稿；发布后新对话才会使用。" })))),
      h("div.check-box.wait", h("span", { text: "正在校验…" })),
      h("div.section", h("div.grid2", textField("ID", "id", { hint: e.mode === "edit" ? "已有卡片不可改" : "小写字母、数字、下划线", readonly: e.mode === "edit" }),
        h("label.field", { dataset: { field: "effect_domain" } }, h("span", { text: "规则类型" }), domainSelect)),
        textField("标题", "title"),
        h("label.field", { dataset: { field: "source_kind" } }, h("span", { text: "来源类型" }), kindSelect),
        textField("适用范围", "scope", { area: true })),
      h("div.section", listEditor("别名（本卡文字或引用原文里逐字出现的简称，可选）", e.aliases, "aliases", { placeholder: "例如某项服务的名称" })),
      h("div.section", listEditor("条件", e.conditions, "conditions", { placeholder: "适用本卡的条件", minimum: 1 })),
      h("div.section", outcomesWrap),
      h("div.section", listEditor("例外与注意", e.exceptions, "exceptions", { placeholder: "可选" })),
      h("div.section", refsWrap),
      h("div.section", textField("审核备注", "review_note", { area: true, hint: "可选，例如核对人与核对点" })),
      h("div.editor-actions",
        h("button.btn.secondary", { type: "button", text: "取消", onclick: () => { state.editing = null; state.check = null; renderDetail(); } }),
        h("button.btn", { type: "button", onclick: saveCard }, icon("check"), "保存到草稿")));
  }

  async function saveCard() {
    try {
      const result = await api("/kb/draft/cards", { body: { card: proposal() } });
      toast("已保存到草稿。发布后对话才会使用新内容。");
      state.editing = null; state.check = null; state.selected = result.card.id; state.scope = "draft";
      await Promise.all([loadCards(), refreshOverview()]);
      await openCard(result.card.id);
    } catch (error) {
      state.check = { ok: false, errors: error.details || [{ field: "card", message: error.message }] };
      renderCheck();
      toast(error.message, "error");
    }
  }

  /* Source picker and passage viewer (text selection → quote). */
  async function ensureSources() {
    if (!state.documents.length) {
      try { state.documents = (await api("/kb/sources?scope=draft")).documents; } catch (error) { toast(error.message, "error"); }
    }
    return state.documents;
  }

  async function pickSource() {
    const documents = await ensureSources();
    const list = h("div.picker-list", documents.map(doc => doc.sections.map(section => h("button.pick", { type: "button",
      onclick: () => { closePanel(); setTimeout(() => openPassage(section.evidence_id, null), 260); } },
      h("strong", { text: section.title }), h("small", { text: `${(KIND[section.kind] || [""])[0]} · ${section.chars} 字 · ${doc.document_id}` })))));
    openModal({ title: "选择来源段落", body: [h("p.small.muted", { text: "选中后可在原文中划选要引用的文字。" }), list],
      actions: [h("button.btn.secondary", { type: "button", text: "关闭", onclick: closePanel })] });
  }

  async function openPassage(evidenceId, ref) {
    let data;
    try { data = await api(`/kb/sources/${encodeURIComponent(evidenceId)}?scope=draft`); }
    catch (error) { toast(error.message, "error"); return; }
    const source = data.source;
    const text = highlighted(source.text, ref ? ref.quotes : []);
    text.classList.add("selectable");
    const addSelection = () => {
      const selection = window.getSelection();
      const chosen = selection ? selection.toString() : "";
      if (!chosen.trim() || !selection.anchorNode || !text.contains(selection.anchorNode)) { toast("请先在原文中划选要引用的文字", "error"); return; }
      if (!source.text.includes(chosen)) { toast("所选文字与原文不完全一致，请缩小选区后重试", "error"); return; }
      addQuote(source, chosen);
    };
    const actions = state.editing ? [
      h("button.btn.small", { type: "button", onclick: addSelection }, icon("plus"), "引用选中文字"),
      h("button.btn.secondary.small", { type: "button", onclick: () => addQuote(source, source.text) , text: "引用整段" })] : [];
    const [kind, tone] = KIND[source.kind] || ["来源", ""];
    const href = publicUrl(source.url || "");
    openDrawer({ title: source.title, subtitle: state.editing ? "在下方划选文字，然后点击“引用选中文字”" : "来源原文", actions,
      body: [h("dl.meta-grid", h("dt", { text: "类型" }), h("dd", h("span.badge." + tone, { text: kind })),
        href ? [h("dt", { text: "原网页" }), h("dd", h("a", { href, target: "_blank", rel: "noopener noreferrer" }, href))] : null,
        h("dt", { text: "被引用" }), h("dd", { text: data.cited_by.length ? data.cited_by.join("、") : "暂无卡片引用" }),
        h("dt", { text: "SHA-256" }), h("dd.mono", { text: source.sha256.slice(0, 20) + "…" })),
        source.scope_note ? h("div.callout.info", { text: source.scope_note }) : null, h("div.divider"), text] });
  }

  function addQuote(source, quote) {
    const e = state.editing;
    if (!e) return;
    let ref = e.references.find(item => item.evidence_id === source.evidence_id);
    if (!ref) {
      ref = { evidence_id: source.evidence_id, title: source.title, kind: source.kind, quotes: [] };
      e.references.push(ref);
    }
    const empty = ref.quotes.findIndex(item => !item.trim());
    if (ref.quotes.includes(quote)) { toast("这段原文已经在引用里了"); return; }
    if (empty >= 0) ref.quotes[empty] = quote; else ref.quotes.push(quote);
    toast("已加入引用");
    closePanel();
    e.redrawRefs?.();
    scheduleCheck();
  }

  /* ---------------- sources view ---------------- */
  async function loadSources() {
    try { state.documents = (await api("/kb/sources?scope=draft")).documents; } catch (error) { toast(error.message, "error"); }
    renderSourceList();
  }

  function renderSourceList() {
    const body = h("div.list-body");
    for (const doc of state.documents) {
      const [kind, tone] = KIND[doc.kind] || ["来源", ""];
      const details = h("details.doc", { open: doc.kind === "merchant_upload" },
        h("summary", h("span.badge." + tone, { text: kind }), h("strong", { text: doc.title }),
          h("span.small.muted", { text: doc.sections.length + " 段" })),
        h("div.sections", doc.sections.map((section, index) => {
          const repeated = doc.sections.filter(other => other.title === section.title).length > 1;
          const label = repeated ? `${section.title} · 第 ${index + 1} 段` : section.title;
          const box = h("input", { type: "checkbox", "aria-label": "选择用于 AI 起草", checked: state.picked.has(section.evidence_id),
            onchange: event => { event.target.checked ? state.picked.add(section.evidence_id) : state.picked.delete(section.evidence_id); renderDraftBar(); } });
          return h("div.section-row", box,
            h("button.link", { type: "button", text: label, onclick: () => { state.sectionView = section.evidence_id; renderSourceDetail(); } }),
            h("small", { text: section.chars + " 字" }));
        }),
        doc.kind === "merchant_upload" ? h("button.btn.ghost.small", { type: "button", onclick: () => deleteUpload(doc) }, icon("trash"), "删除文档") : null));
      body.append(details);
    }
    els.draftBar = h("div.row");
    els.list.replaceChildren(h("div.list-head",
      h("div.row", h("h2", { text: "来源文档" }),
        h("button.btn.small", { type: "button", onclick: uploadDialog }, icon("upload"), "上传")),
      h("p.small.muted", { text: "勾选 1–3 个段落，可让 AI 起草卡片；草稿仍需人工核对。" }), els.draftBar), body);
    renderDraftBar();
  }

  function renderDraftBar() {
    const count = state.picked.size;
    fill(els.draftBar, h("button.btn.secondary.small", { type: "button", disabled: count < 1 || count > 3, onclick: aiDraft },
      icon("spark"), count ? `用所选 ${count} 段起草卡片` : "AI 起草卡片"),
      count ? h("button.btn.ghost.small", { type: "button", text: "清空", onclick: () => { state.picked.clear(); renderSourceList(); } }) : null);
  }

  async function renderSourceDetail() {
    if (state.view !== "sources") return;
    const wrap = h("div.detail-inner");
    els.detail.replaceChildren(wrap);
    if (!state.sectionView) {
      wrap.append(h("div.empty-detail", icon("doc"), h("p", { text: "选择一个段落查看原文与引用它的卡片，或上传新的政策文档。" })));
      return;
    }
    try {
      const data = await api(`/kb/sources/${encodeURIComponent(state.sectionView)}?scope=draft`);
      const source = data.source;
      const [kind, tone] = KIND[source.kind] || ["来源", ""];
      const href = publicUrl(source.url || "");
      wrap.append(h("div.detail-head", h("div.grow", h("h2", { text: source.title }),
        h("div.sub", h("span.badge." + tone, { text: kind }), h("code.mono.muted", { text: source.evidence_id }))),
        h("button.btn.secondary.small", { type: "button", onclick: () => { state.picked = new Set([source.evidence_id]); aiDraft(); } }, icon("spark"), "用此段起草卡片")));
      wrap.append(h("div.section", h("h3", { text: "被这些卡片引用" }),
        data.cited_by.length ? h("div.chip-row", data.cited_by.map(id => h("button.chip", { type: "button", text: id,
          onclick: async () => { state.view = "cards"; renderNav(); renderCardList(); await openCard(id); } }))) : h("p.muted", { text: "暂无" })));
      if (source.scope_note) wrap.append(h("div.callout.info", { text: source.scope_note }));
      if (href) wrap.append(h("a.small", { href, target: "_blank", rel: "noopener noreferrer" }, "打开原网页 ", icon("external")));
      wrap.append(h("div.section", h("h3", { text: `原文（${source.chars} 字）` }), highlighted(source.text, [])));
    } catch (error) { wrap.append(h("div.callout.danger", { text: error.message })); }
  }

  function uploadDialog() {
    const title = h("input.input", { placeholder: "例如：星河数码店铺售后补充说明", maxlength: 200 });
    const url = h("input.input", { placeholder: "https://…（可选，仅用于标注来源）" });
    const text = h("textarea.textarea", { rows: 10, placeholder: "粘贴政策正文，或选择 .txt / .md 文件。按空行和“一、”“第X条”等标题自动分段。" });
    const file = h("input", { type: "file", accept: ".txt,.md,text/plain,text/markdown", onchange: async event => {
      const chosen = event.target.files[0];
      if (!chosen) return;
      if (chosen.size > 400000) { toast("文件过大，请控制在 400KB 以内", "error"); return; }
      text.value = await chosen.text();
      if (!title.value.trim()) title.value = chosen.name.replace(/\.(txt|md)$/i, "");
    } });
    const submit = h("button.btn", { type: "button", text: "上传到草稿", onclick: async () => {
      if (!title.value.trim() || !text.value.trim()) { toast("请填写标题和正文", "error"); return; }
      submit.disabled = true;
      try {
        const result = await api("/kb/uploads", { body: { title: title.value.trim(), text: text.value, url: url.value.trim() } });
        closePanel();
        toast(`已上传，自动切分为 ${result.document.sections.length} 段；它会随下一次发布进入知识库。`);
        state.documents = [];
        await Promise.all([loadSources(), refreshOverview()]);
        state.sectionView = `upload:${result.document.document_id}#${result.document.sections[0].section_id}`;
        renderSourceDetail();
      } catch (error) {
        toast(error.details?.map(item => item.message).join("；") || error.message, "error");
        submit.disabled = false;
      }
    } });
    openModal({ title: "上传政策文档", body: [
      h("p.small.muted", { text: "上传文本只保存在本地知识库草稿中，被标记为“上传文档”，不会与官网快照混淆。" }),
      h("label.field", h("span", { text: "标题" }), title), h("label.field", h("span", { text: "来源链接" }), url),
      h("label.field", h("span", { text: "正文" }), text), h("label.field", h("span", { text: "或选择文件" }), file)],
      actions: [h("button.btn.secondary", { type: "button", text: "取消", onclick: closePanel }), submit] });
    title.focus();
  }

  async function deleteUpload(doc) {
    const ok = await confirmDialog({ title: "删除上传文档？", message: `「${doc.title}」将从草稿移除。仍被卡片引用时不能删除。`, confirmText: "删除", danger: true });
    if (!ok) return;
    try {
      await api(`/kb/uploads/${doc.document_id}/delete`, { body: {} });
      toast("已从草稿删除文档");
      state.documents = []; state.sectionView = null;
      await Promise.all([loadSources(), refreshOverview()]);
      renderSourceDetail();
    } catch (error) { toast(error.details?.map(item => item.message).join("；") || error.message, "error"); }
  }

  async function aiDraft() {
    const ids = [...state.picked];
    if (!ids.length || ids.length > 3) return;
    if (state.overview.ai_drafting === "model") toast("正在请大模型起草，通常需要十几秒…");
    try {
      const result = await api("/kb/ai-draft", { body: { evidence_ids: ids, instruction: "" } });
      const docs = await ensureSources();
      const lookup = new Map(docs.flatMap(doc => doc.sections.map(section => [section.evidence_id, section])));
      const card = { ...result.card, references: result.card.references.map(ref => ({ ...ref,
        title: lookup.get(ref.evidence_id)?.title || ref.evidence_id, kind: lookup.get(ref.evidence_id)?.kind || "" })) };
      state.picked.clear();
      startEditor(null, card);
      toast(result.notice + (result.usage?.total_tokens ? `（本次 ${result.usage.total_tokens} tokens）` : ""));
    } catch (error) { toast(error.details?.map(item => item.message).join("；") || error.message, "error"); }
  }

  /* ---------------- versions ---------------- */
  async function loadVersions() {
    try { const data = await api("/kb/versions"); state.versions = data.versions; state.history = data.history; }
    catch (error) { toast(error.message, "error"); }
    if (!state.version || !state.versions.some(row => row.version_id === state.version)) state.version = state.versions[0]?.version_id;
    renderVersionList();
    await showVersion(state.version);
  }

  function renderVersionList() {
    els.list.replaceChildren(h("div.list-head", h("div.row", h("h2", { text: "版本历史" }))),
      h("div.list-body", state.versions.map(row => h("button.item" + (row.version_id === state.version ? ".active" : ""),
        { type: "button", onclick: () => showVersion(row.version_id) },
        h("div.version", h("div.top", h("strong", { text: `v${row.number} · ${row.label}` }), row.active ? h("span.badge.success", { text: "生效中" }) : null),
          h("span.small.muted", { text: `${clock(row.created_at)} · ${row.author} · ${row.card_count} 张卡片` +
            (row.supplementary_source_count ? ` · ${row.supplementary_source_count} 份上传文档` : "") }))))));
  }

  async function showVersion(versionId) {
    if (!versionId) return;
    state.version = versionId;
    renderVersionList();
    const row = state.versions.find(item => item.version_id === versionId);
    const wrap = h("div.detail-inner");
    els.detail.replaceChildren(wrap);
    wrap.append(h("div.detail-head", h("div.grow", h("h2", { text: `v${row.number} · ${row.label}` }),
      h("div.sub", row.active ? h("span.badge.success", { text: "当前生效" }) : h("span.badge", { text: "历史版本" }),
        h("code.mono.muted", { text: row.version_id.slice(0, 16) + "…" }))),
      row.active ? null : h("button.btn.warn.small", { type: "button", onclick: () => activate(row) }, icon("history"), "切换到此版本")));
    try {
      const data = await api(`/kb/versions/${versionId}/diff`);
      wrap.append(data.against ? diffSection(data.diff) : h("div.section", h("h3", { text: "首个版本" }),
        h("p.muted", { text: `导入 ${row.card_count} 张卡片` + (row.supplementary_source_count ? `、${row.supplementary_source_count} 份上传文档` : "")
          + "，作为之后发布的比较基线。" })));
    } catch (error) { wrap.append(h("div.callout.danger", { text: error.message })); }
    wrap.append(h("div.section", h("h3", { text: "发布与切换记录" }),
      state.history.map(event => h("div.history-row", h("span.badge" + (event.event === "rollback" ? ".accent" : ".brand"),
        { text: { seed: "初始导入", publish: "发布", rollback: "回滚", reactivate: "重新启用" }[event.event] || event.event }),
        h("span", { text: `v${state.versions.find(item => item.version_id === event.version_id)?.number ?? "?"}` }),
        h("span.muted", { text: clock(event.activated_at) }), h("span", { text: event.reason }), h("span.muted", { text: event.author })))));
  }

  const FIELD_NAMES = { title: "标题", scope: "范围", conditions: "条件", outcomes: "结论", outcome_premises: "前提",
    exceptions: "例外", references: "引用", effect_domain: "类型", review_note: "备注" };
  function diffSection(diff) {
    const rows = [
      ...diff.added.map(item => h("div.diff-row", h("span.badge.success", { text: "新增卡片" }), item.title, h("code.mono.muted", { text: item.id }))),
      ...diff.removed.map(item => h("div.diff-row", h("span.badge.danger", { text: "删除卡片" }), item.title)),
      ...diff.changed.map(item => h("div.diff-row", h("span.badge.info", { text: "修改" }), item.title,
        h("span.small.muted", { text: "字段：" + item.fields.map(field => FIELD_NAMES[field] || field).join("、") }))),
      ...diff.sources_added.map(item => h("div.diff-row", h("span.badge.violet", { text: "新增文档" }), item.title)),
      ...diff.sources_removed.map(item => h("div.diff-row", h("span.badge.danger", { text: "删除文档" }), item.title)),
      ...(diff.publishers ? [h("div.diff-row", h("span.badge.info", { text: "来源发布方" }),
        (diff.publishers.after || []).join("、") || "（不再登记）")] : []),
    ];
    return h("div.section", h("h3", { text: "相对上一版本的变化" }),
      rows.length ? h("div.diff-list", rows) : h("p.muted", { text: "没有卡片变化" }),
      diff.unchanged_count ? h("p.small.muted", { text: `未变化的卡片 ${diff.unchanged_count} 张` }) : null);
  }

  async function activate(row) {
    const reason = await confirmDialog({ title: `切换到 v${row.number}？`, message: "切换后新的对话轮次立即使用该版本；正在进行中的回答不受影响。当前草稿不会被删除。",
      confirmText: "切换版本", input: { label: "切换原因", placeholder: "例如：新规则有误，先回滚", maxlength: 200 } });
    if (!reason) return;
    try {
      await api(`/kb/versions/${row.version_id}/activate`, { body: { reason } });
      toast(`已切换到 v${row.number}`);
      await refreshOverview(); await loadVersions();
    } catch (error) { toast(error.message, "error"); }
  }

  /* ---------------- draft-level actions ---------------- */
  async function validateDraft() {
    try {
      const report = await api("/kb/draft/validate", { body: {} });
      openModal({ title: report.ok ? "草稿校验通过" : "草稿还有问题", body: [
        h("div.check-box." + (report.ok ? "ok" : "bad"),
          h("strong", { text: report.ok ? `✓ ${report.card_count} 张卡片结构完整，引用与原文逐字一致` : `发现 ${report.card_errors.length} 个问题` }),
          report.card_errors.length ? h("ul", report.card_errors.map(error => h("li", { text: `${error.card_id} · ${fieldLabel(error.field)}：${error.message}` }))) : null,
          report.runtime_check !== "passed" ? h("p", { text: "运行时加载：" + report.runtime_check }) : null),
        h("p.small.muted", { text: "校验范围：来源绑定、逐字引用与运行时加载；不判断规则是否正确或仍然有效。" })],
        actions: [h("button.btn", { type: "button", text: "知道了", onclick: closePanel })] });
    } catch (error) { toast(error.message, "error"); }
  }

  async function publishDraft() {
    const diff = state.overview.draft.diff;
    const label = h("input.input", { placeholder: "例如：新增店铺配件退换规则", maxlength: 80 });
    const summary = [
      diff.added.length ? `新增 ${diff.added.length} 张` : null, diff.changed.length ? `修改 ${diff.changed.length} 张` : null,
      diff.removed.length ? `删除 ${diff.removed.length} 张` : null, diff.sources_added.length ? `新增文档 ${diff.sources_added.length} 份` : null,
      diff.sources_removed.length ? `删除文档 ${diff.sources_removed.length} 份` : null,
      diff.publishers ? "更改来源发布方" : null].filter(Boolean).join("，");
    const go = h("button.btn", { type: "button", text: "发布并立即生效", onclick: async () => {
      if (!label.value.trim()) { label.focus(); return; }
      go.disabled = true;
      try {
        const result = await api("/kb/draft/publish", { body: { label: label.value.trim() } });
        closePanel();
        toast(`已发布 v${result.version.number}，新的对话轮次立即使用。`);
        state.documents = [];
        await refreshOverview();
        if (state.view === "versions") await loadVersions(); else { await loadCards(); if (state.selected) openCard(state.selected); }
      } catch (error) {
        go.disabled = false;
        toast(error.details?.map(item => item.message).join("；") || error.message, "error");
      }
    } });
    openModal({ title: "发布草稿", body: [
      h("p", { text: `本次改动：${summary || "无"}。` }),
      state.overview.draft.stale ? h("div.callout.warn", { text: "草稿基于旧版本创建，发布会以草稿内容为准。" }) : null,
      h("label.field", h("span", { text: "版本说明" }), label),
      h("p.small.muted", { text: "发布前会用运行时加载器重新校验全部卡片；已发布的版本不可修改，可随时回滚。" })],
      actions: [h("button.btn.secondary", { type: "button", text: "取消", onclick: closePanel }), go] });
    label.focus();
  }

  /* B5g (D8): the names the catalogue's sources are published under (G10 keeps them out of the answer's words). */
  function editPublishers() {
    const names = h("textarea.textarea", { rows: 5, placeholder: "每行一个名称，须在卡片引用原文里逐字出现" });
    names.value = (state.overview.source_publishers?.draft || []).join("\n");
    const save = h("button.btn", { type: "button", text: "保存到草稿", onclick: async () => {
      save.disabled = true;
      try {
        const list = names.value.split("\n").map(line => line.trim()).filter(Boolean);
        await api("/kb/draft/publishers", { body: { source_publishers: list } });
        closePanel();
        toast("已保存到草稿，发布后新的对话轮次才会使用。");
        await refreshOverview();
      } catch (error) {
        save.disabled = false;
        toast(error.details?.map(item => item.message).join("；") || error.message, "error");
      }
    } });
    openModal({ title: "来源发布方", body: [
      h("p.small.muted", { text: "答复里模型写的文字不能出现这些名称；卡片引用的原文照常展示。留空则不检查。" }),
      h("label.field", h("span", { text: "名称" }), names)],
      actions: [h("button.btn.secondary", { type: "button", text: "取消", onclick: closePanel }), save] });
    names.focus();
  }

  async function discardDraft() {
    const ok = await confirmDialog({ title: "放弃全部草稿改动？", message: "草稿会恢复为当前生效版本，未发布的卡片编辑和上传文档将丢失。", confirmText: "放弃草稿", danger: true });
    if (!ok) return;
    try {
      await api("/kb/draft/discard", { body: {} });
      toast("草稿已恢复为生效版本");
      state.documents = []; state.editing = null;
      await refreshOverview();
      if (state.view === "cards") { await loadCards(); if (state.selected) openCard(state.selected); }
      if (state.view === "sources") await loadSources();
    } catch (error) { toast(error.message, "error"); }
  }

  /* ---------------- model view (B6) ---------------- */
  // The answer model reads the whole catalogue every turn (no search); this view shows that text and its size
  // against the publication cap, for the draft and the published version.
  async function loadModelView() {
    try {
      const [draft, active] = await Promise.all([api("/kb/model-view?scope=draft"), api("/kb/model-view?scope=active")]);
      state.modelView = { draft, active };
    } catch (error) { toast(error.message, "error"); state.modelView = null; }
    renderModelView();
  }

  function renderModelView() {
    const view = state.modelView;
    const size = (title, result) => h("div.item", h("strong", { text: title }),
      h("span.small" + (result.over_cap ? ".over-cap" : ".muted"), { text: `${result.card_count} / ${result.limits.cards} 张卡 · `
        + `${result.chars} / ${result.limits.chars} 字` }),
      result.over_cap ? h("span.small.over-cap", { text: "超过上限，不能发布：" + result.over_cap }) : null);
    els.list.replaceChildren(h("div.list-head", h("div.row", h("h2", { text: "模型视图" })),
      h("p.small.muted", { text: view ? view.draft.note : "" })),
      h("div.list-body", view ? [size("草稿", view.draft), size("已发布", view.active)] : []));
    const wrap = h("div.detail-inner");
    els.detail.replaceChildren(wrap);
    if (!view) { wrap.append(h("div.empty-detail", icon("search"), h("p", { text: "模型视图暂时读不到，请稍后重试。" }))); return; }
    wrap.append(h("div.detail-head", h("div.grow", h("h2", { text: "草稿的模型视图" }),
      h("p.small.muted", { text: "下面是答复模型读到的卡片文字：标题、范围、条件、结论、例外和每条结论的适用前提；不含来源原文和哈希。" }))),
      h("div.section", h("pre.source-text", { text: JSON.stringify(view.draft.view, null, 1) })));
  }

  boot();
})();
