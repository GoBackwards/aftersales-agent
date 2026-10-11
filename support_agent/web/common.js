"use strict";
/* Shared helpers for the chat app and knowledge console. No framework, no
   innerHTML with data: every dynamic string is set through textContent. */
(() => {
  const ICONS = {
    plus: "M12 5v14M5 12h14",
    send: "M4 12l16-8-6 16-2.5-6.5L4 12z",
    menu: "M4 7h16M4 12h16M4 17h16",
    panel: "M4 5h16v14H4zM15 5v14",
    close: "M6 6l12 12M18 6L6 18",
    book: "M5 4h9a4 4 0 014 4v12H9a4 4 0 01-4-4V4zM5 16a4 4 0 014-4h9",
    box: "M4 8l8-4 8 4-8 4-8-4zm0 0v8l8 4 8-4V8M12 12v8",
    check: "M5 12l4 4 10-10",
    alert: "M12 8v5m0 3.5v.5M10.3 4.2L2.6 18a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 4.2a2 2 0 00-3.4 0z",
    info: "M12 11v6m0-9.5V8M3 12a9 9 0 1018 0 9 9 0 00-18 0z",
    route: "M6 18a2 2 0 100-4 2 2 0 000 4zM18 10a2 2 0 100-4 2 2 0 000 4zM8 16h6a3 3 0 000-6h-4a3 3 0 010-6h6",
    link: "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1",
    flow: "M5 6h4v4H5zM15 6h4v4h-4zM10 16h4v4h-4zM7 10v3h10v-3M12 13v3",
    doc: "M7 3h7l5 5v13H7zM14 3v5h5M9.5 13h7M9.5 17h7",
    upload: "M12 16V4m0 0l-5 5m5-5l5 5M5 20h14",
    history: "M4 12a8 8 0 108-8 8 8 0 00-6.3 3M4 4v4h4M12 8v5l3 2",
    search: "M11 18a7 7 0 110-14 7 7 0 010 14zm9 3l-4.5-4.5",
    edit: "M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4",
    trash: "M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
    spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z",
    user: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 20a8 8 0 0116 0",
    logout: "M15 12H4m0 0l4-4m-4 4l4 4M13 4h6v16h-6",
    chevron: "M9 6l6 6-6 6",
    refresh: "M20 11a8 8 0 10-2.3 5.7M20 5v6h-6",
    external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  };

  function icon(name, className = "") {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    if (className) svg.setAttribute("class", className);
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", ICONS[name] || ICONS.info);
    svg.append(path);
    return svg;
  }

  /* h("div.card.big", {onclick, title, dataset}, children...) */
  function h(spec, props, ...children) {
    const [tag, ...classes] = spec.split(".");
    const el = document.createElement(tag || "div");
    if (classes.length) el.className = classes.join(" ");
    if (props && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) {
      children.unshift(props);
      props = null;
    }
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "text") el.textContent = String(value);
      else if (key === "className") el.className = value;
      else if (key in el && typeof value !== "string") el[key] = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const store = {
    get(key) { try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch (_) { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ } },
    remove(key) { try { sessionStorage.removeItem(key); } catch (_) { /* ignore */ } },
  };

  class ApiError extends Error {
    constructor(message, { status = 0, code = "", details = null, network = false } = {}) {
      super(message);
      Object.assign(this, { status, code, details, network });
    }
  }

  function createApi(getToken) {
    return async function api(path, { method, body, token } = {}) {
      const auth = token || getToken();
      const headers = {};
      if (auth) headers.Authorization = "Bearer " + auth;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      let response;
      try {
        response = await fetch(path, { method: method || (body === undefined ? "GET" : "POST"), cache: "no-store",
          credentials: "omit", headers, body: body === undefined ? undefined : JSON.stringify(body) });
      } catch (_) {
        throw new ApiError("网络连接中断，请确认本地服务仍在运行。", { network: true });
      }
      let data = null;
      try { data = await response.json(); } catch (_) { /* non-JSON */ }
      if (!response.ok) {
        throw new ApiError(data?.error?.message || `请求未完成（HTTP ${response.status}）`,
          { status: response.status, code: data?.error?.code || "", details: data?.error?.details || null });
      }
      return data;
    };
  }

  function toast(message, kind = "") {
    let box = document.querySelector(".toasts");
    if (!box) { box = h("div.toasts", { role: "status", "aria-live": "polite" }); document.body.append(box); }
    const item = h("div.toast" + (kind ? "." + kind : ""), { text: message });
    box.append(item);
    setTimeout(() => item.remove(), kind === "error" ? 6000 : 3200);
  }

  function requestId(prefix = "WEB") { return prefix + "-" + crypto.randomUUID(); }

  function money(cents) { return "¥" + (cents / 100).toFixed(2); }

  function relativeTime(epochSeconds) {
    if (!epochSeconds) return "";
    const diff = Date.now() / 1000 - epochSeconds;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    const date = new Date(epochSeconds * 1000);
    return (date.getMonth() + 1) + "月" + date.getDate() + "日";
  }

  // timeOnly drops the wall-clock date where the page also states the demo business date (B1, D13).
  function clock(value, { timeOnly = false } = {}) {
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const day = timeOnly ? {} : { month: "numeric", day: "numeric" };
    return date.toLocaleString("zh-CN", { hour12: false, ...day, hour: "2-digit", minute: "2-digit" });
  }

  /* Only plain public http(s) links are rendered for sources. */
  function publicUrl(value) {
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase();
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !host.includes(".")
        || host.endsWith(".localhost") || host.endsWith(".local") || /^\d+\.\d+\.\d+\.\d+$/.test(host)) return null;
      return url.href;
    } catch (_) { return null; }
  }

  /* Render text with every exact quote occurrence wrapped in <mark>. */
  function highlighted(text, quotes = []) {
    const container = h("div.source-text");
    const ranges = [];
    for (const quote of quotes.filter(Boolean)) {
      let index = text.indexOf(quote);
      while (index !== -1) { ranges.push([index, index + quote.length]); index = text.indexOf(quote, index + quote.length); }
    }
    ranges.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const range of ranges) {
      const last = merged[merged.length - 1];
      if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]); else merged.push([...range]);
    }
    let cursor = 0;
    for (const [start, end] of merged) {
      if (start > cursor) container.append(text.slice(cursor, start));
      container.append(h("mark", { text: text.slice(start, end) }));
      cursor = end;
    }
    if (cursor < text.length) container.append(text.slice(cursor));
    return container;
  }

  /* Drawer / modal controller shared by both pages. */
  function overlay() {
    let scrim = document.querySelector(".scrim");
    if (!scrim) { scrim = h("div.scrim"); document.body.append(scrim); }
    return scrim;
  }
  let openPanel = null;
  function openDrawer({ title, subtitle, body, actions }) {
    closePanel();
    const scrim = overlay();
    const drawer = h("aside.drawer", { role: "dialog", "aria-modal": "true", "aria-label": title },
      h("div.drawer-head",
        h("div.grow", h("h2", { text: title }), subtitle ? h("p.small.muted", { text: subtitle }) : null),
        h("div.drawer-actions", actions || []),
        h("button.icon-btn", { "aria-label": "关闭", onclick: closePanel }, icon("close"))),
      h("div.drawer-body", body));
    document.body.append(drawer);
    requestAnimationFrame(() => { scrim.classList.add("open"); drawer.classList.add("open"); });
    scrim.onclick = closePanel;
    openPanel = drawer;
    drawer.querySelector(".icon-btn").focus();
    return drawer;
  }
  function openModal({ title, body, actions }) {
    closePanel();
    const scrim = overlay();
    const modal = h("div.modal", { role: "dialog", "aria-modal": "true", "aria-label": title },
      h("div.modal-head", h("h2", { text: title })), h("div.modal-body", body), h("div.modal-foot", actions || []));
    document.body.append(modal);
    requestAnimationFrame(() => { scrim.classList.add("open"); modal.classList.add("open"); });
    scrim.onclick = closePanel;
    openPanel = modal;
    return modal;
  }
  function closePanel() {
    const scrim = document.querySelector(".scrim");
    if (scrim) scrim.classList.remove("open");
    if (openPanel) {
      const panel = openPanel;
      panel.classList.remove("open");
      setTimeout(() => panel.remove(), 240);
      openPanel = null;
    }
  }
  document.addEventListener("keydown", event => { if (event.key === "Escape") closePanel(); });

  function confirmDialog({ title, message, confirmText = "确定", danger = false, input = null }) {
    return new Promise(resolve => {
      let field = null;
      if (input) field = h("input.input", { placeholder: input.placeholder || "", value: input.value || "", maxlength: input.maxlength || 200 });
      const ok = h("button.btn" + (danger ? ".danger" : ""), { text: confirmText, onclick: () => {
        if (field && !field.value.trim()) { field.focus(); return; }
        closePanel(); resolve(field ? field.value.trim() : true);
      } });
      const cancel = h("button.btn.secondary", { text: "取消", onclick: () => { closePanel(); resolve(null); } });
      openModal({ title, body: [h("p.muted", { text: message }), input ? h("label.field", h("span", { text: input.label }), field) : null], actions: [cancel, ok] });
      (field || ok).focus();
    });
  }

  const MODE_LABELS = {
    offline_keyword_demo: ["离线演示", "关键词脚本代替大模型，不调用 API"],
    offline_scripted_demo: ["离线脚本", "固定脚本代替大模型，不调用 API"],
    live_agent: ["真实模型", "由 .env 配置的大模型处理，会产生 API 费用"],
  };
  function modeBadge(mode) {
    const [label, detail] = MODE_LABELS[mode] || [mode || "未知模式", ""];
    return h("span.badge." + (mode === "live_agent" ? "success" : "info"), { title: detail }, h("span.dot"), label);
  }

  window.SA = { h, append, icon, store, createApi, ApiError, toast, requestId, money, relativeTime, clock,
    publicUrl, highlighted, openDrawer, openModal, closePanel, confirmDialog, modeBadge, MODE_LABELS };
})();
