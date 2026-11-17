// /admin page. Reads live from the database through /api/admin (src/web/admin.py).
// Chat text comes from the public site, so everything is rendered with
// textContent via h(), never innerHTML.
(() => {
  const $ = (id) => document.getElementById(id);
  const POLL_LIST_MS = 4000;
  const POLL_DETAIL_MS = 2500;

  // ---------- helpers ----------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  // replaceChildren() would print a skipped (null) section as the text "null".
  const fill = (el, ...children) => el.replaceChildren(...children.flat(Infinity).filter(Boolean));

  async function api(path, opts = {}) {
    const r = await fetch(`/api/admin${path}`, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    const body = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
    if (!r.ok) {
      const d = body && body.detail;
      const err = new Error(typeof d === "string" ? d : `Request failed (${r.status})`);
      // {errors: [...]} from our checks, or FastAPI's list of field problems
      err.errors = Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(-1)[0]}: ${x.msg}`) : d && d.errors;
      throw err;
    }
    return body;
  }

  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return new Date(iso).toLocaleDateString();
  }
  const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "");
  const short = (t, n = 90) => (!t ? "" : t.length > n ? t.slice(0, n - 1) + "…" : t);
  const clone = (o) => JSON.parse(JSON.stringify(o));

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch { /* storage blocked: per-viewer convenience only */ }
    return null;
  }

  // Shared reference data: policy rules and the interaction tree (from /api/admin/flow).
  const REF = { rules: {}, wording: {}, flags: [], catalogue: { hooks: {}, checks: {}, actions: {}, hard_rules: {} }, doc: { hooks: {}, nodes: {} } };
  async function loadRef() {
    const f = await api("/flow");
    Object.assign(REF, { rules: f.rules, wording: f.wording, flags: f.screen_flags, catalogue: f.catalogue, doc: f.doc, errors: f.errors });
    return f;
  }
  const hookLabel = (hook) => REF.catalogue.hooks[hook] || hook;

  function ruleChip(id, extra = "") {
    if (!id) return null;
    const r = REF.rules[id];
    return h("span", {
      class: `chip rule ${r && r.hard ? "hard" : ""} ${extra}`,
      title: r ? `${id}${r.hard ? " [hard]" : ""}: ${r.text}` : id,
    }, id);
  }

  function errorBox(title, errors) {
    if (!errors || !errors.length) return null;
    return h("div", { class: "errors", role: "alert" }, h("strong", {}, title),
      h("ul", {}, errors.map((e) => h("li", {}, typeof e === "string" ? e : JSON.stringify(e)))));
  }

  function poller(fn, ms) {
    let timer = null;
    const tick = async () => {
      if (!document.hidden) {
        try { await fn(); } catch (e) { console.warn(e); }
      }
      timer = setTimeout(tick, ms);
    };
    return {
      start() { if (!timer) tick(); },
      stop() { clearTimeout(timer); timer = null; },
      now() { fn().catch(console.warn); },
    };
  }

  // =====================================================================
  // Tree rendering, shared by the Flow editor and the walkthrough.
  // The main line follows "no" / "next"; "yes" branches hang off to the side.
  // =====================================================================
  function nodeSummary(n) {
    const p = n.params || {};
    if (n.check) {
      const c = REF.catalogue.checks[n.check];
      const params = Object.entries(p).map(([k, v]) => `${k}: ${v}`).join(", ");
      return `${c ? c.label : n.check}${params ? ` (${params})` : ""}`;
    }
    const a = REF.catalogue.actions[n.action];
    const label = a ? a.label : n.action;
    if (n.action === "reply") return `${label}: "${short(REF.wording[p.wording] || p.wording, 110)}"`;
    if (n.action === "handoff") return `${label}${p.prefix ? ` after "${short(REF.wording[p.prefix] || p.prefix, 60)}"` : ""}${p.reason ? ` · ${p.reason}` : ""}`;
    if (n.action === "guide") return `${label}: "${short(p.say, 140)}"`;
    if (n.action === "block") return `${label}: "${short(p.message, 110)}"`;
    return label;
  }

  const endsHook = (n) => !!(n && n.action && REF.catalogue.actions[n.action]?.ends);

  function chainIds(doc, start, stop) {
    const ids = [];
    let id = start;
    while (id && doc.nodes[id] && !ids.includes(id) && !stop.has(id)) {
      ids.push(id);
      const n = doc.nodes[id];
      id = n.check ? n.no : endsHook(n) ? null : n.next;
    }
    return { ids, rejoin: id && stop.has(id) ? id : null, missing: id && !doc.nodes[id] ? id : null };
  }

  // opts: { trace: Map(id -> {branch, observed, current}), selected, onSelect(id), onAdd(fromId, slot) }
  function renderChain(doc, start, opts, outer = new Set(), depth = 0) {
    const wrap = h("div", { class: "chain" });
    if (!start) {
      wrap.append(endMarker("Nothing runs here yet", opts.onAdd ? () => opts.onAdd(null, "hook") : null));
      return wrap;
    }
    const { ids, rejoin, missing } = chainIds(doc, start, outer);
    const mine = new Set([...outer, ...ids]);
    ids.forEach((id) => {
      const n = doc.nodes[id];
      const t = opts.trace && opts.trace.get(id);
      wrap.append(nodeCard(id, n, opts, t));
      if (n.check) {
        const taken = t && t.branch === "yes";
        const yes = n.yes
          ? (mine.has(n.yes) || depth > 12 ? jumpRef(doc, n.yes) : renderChain(doc, n.yes, opts, mine, depth + 1))
          : endMarker("carry on", opts.onAdd ? () => opts.onAdd(id, "yes") : null);
        wrap.append(h("div", { class: `branch ${taken ? "taken" : ""}` }, h("span", { class: "branch-label yes" }, "yes"), yes));
        wrap.append(h("div", { class: `connector ${t && t.branch === "no" ? "taken" : ""}` }, "no"));
      } else if (!endsHook(n)) {
        wrap.append(h("div", { class: `connector ${t ? "taken" : ""}` }, "then"));
      }
    });
    const last = doc.nodes[ids[ids.length - 1]];
    if (rejoin) wrap.append(jumpRef(doc, rejoin));
    else if (missing) wrap.append(h("div", { class: "end bad" }, `→ missing node "${missing}"`));
    else if (endsHook(last)) wrap.append(h("div", { class: "end" }, "■ turn ends here"));
    else if (last) {
      const slot = last.check ? "no" : "next";
      wrap.append(endMarker("carry on", opts.onAdd ? () => opts.onAdd(ids[ids.length - 1], slot) : null));
    }
    return wrap;
  }

  function jumpRef(doc, id) {
    const n = doc.nodes[id];
    return h("div", { class: "end jump" }, `↪ continues at "${n ? n.title || id : id}"`);
  }

  function endMarker(text, onAdd) {
    return h("div", { class: "end" }, text,
      onAdd ? h("button", { class: "btn small add", onclick: (e) => { e.stopPropagation(); onAdd(); } }, "+ add step") : null);
  }

  function nodeCard(id, n, opts, t) {
    const off = n.enabled === false;
    const kind = n.check ? "decision" : "action";
    const cls = ["node", kind, off ? "off" : "", t ? "visited" : "", t && t.current ? "current" : "",
      opts.selected === id ? "selected" : "", opts.onSelect ? "editable" : ""].join(" ");
    return h("div", {
      class: cls, "data-node": id, tabindex: opts.onSelect ? "0" : null,
      onclick: opts.onSelect ? () => opts.onSelect(id) : null,
      onkeydown: opts.onSelect ? (e) => { if (e.key === "Enter") opts.onSelect(id); } : null,
    },
    h("div", { class: "node-head" },
      h("span", { class: "node-icon", "aria-hidden": "true" }, n.check ? "◆" : endsHook(n) ? "■" : "▸"),
      h("span", { class: "node-title" }, n.title || id),
      ruleChip(n.rule),
      off ? h("span", { class: "chip" }, "off") : null,
      t && t.branch ? h("span", { class: `chip ${t.branch === "yes" ? "warn" : t.branch === "no" ? "ok" : "accent"}` }, t.branch) : null),
    h("div", { class: "node-sub" }, nodeSummary(n)),
    t && t.observed ? h("div", { class: "node-observed" }, "saw: ", t.observed) : null,
    t && t.says ? h("div", { class: "node-observed" }, "→ ", t.says) : null);
  }

  // ---------- tabs ----------
  const tabs = {
    interactions: { start: () => interactions.start(), stop: () => interactions.stop() },
    tickets: { start: () => trackers.ticket.start(), stop: () => trackers.ticket.stop() },
    handoffs: { start: () => trackers.handoff.start(), stop: () => trackers.handoff.stop() },
    flow: { start: () => flowEditor.load(), stop: () => {} },
    files: { start: () => files.load(), stop: () => {} },
    kb: { start: () => kb.start(), stop: () => kb.stop() },
    analytics: { start: () => analytics.start(), stop: () => analytics.stop() },
  };
  let currentTab = "interactions";
  function showTab(name) {
    if (!tabs[name]) name = "interactions";
    tabs[currentTab].stop();
    currentTab = name;
    for (const b of document.querySelectorAll(".tabs button")) {
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    }
    for (const id of Object.keys(tabs)) $(`tab-${id}`).hidden = id !== name;
    tabs[name].start();
    if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  }
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  window.addEventListener("hashchange", () => { if (location.hash.slice(1) !== currentTab) showTab(location.hash.slice(1)); });

  const author = $("author");
  author.value = store("admin-author") || "";
  author.addEventListener("change", () => store("admin-author", author.value.trim()));
  const who = () => author.value.trim() || null;

  // =====================================================================
  // Interactions: live walkthrough, traced through the tree
  // =====================================================================
  const interactions = (() => {
    let selected = null;
    let detail = null;      // last payload
    let cursor = null;      // index into events; null = show everything
    let signature = "";
    let listSig = "";
    let listDrawn = 0;
    const open = new Set(); // expanded event ids

    const listPoll = poller(loadList, POLL_LIST_MS);
    const detailPoll = poller(() => (selected ? loadDetail(selected) : Promise.resolve()), POLL_DETAIL_MS);

    async function loadList() {
      const { interactions: rows } = await api("/interactions?limit=60");
      $("int-status").textContent = rows.length ? `Live · updated ${new Date().toLocaleTimeString()}` : "No conversations logged yet.";
      if ($("follow").checked && rows[0] && rows[0].interaction_id !== selected) select(rows[0].interaction_id);
      // Redraw only when something changed (or every 30 s for the "ago" times).
      const sig = JSON.stringify([selected, rows]);
      if (sig === listSig && Date.now() - listDrawn < 30000) return;
      listSig = sig;
      listDrawn = Date.now();
      fill($("int-list"), rows.map((r) => {
        const live = Date.now() - new Date(r.last_at).getTime() < 120000;
        return h("button", {
          class: `item ${r.interaction_id === selected ? "selected" : ""}`,
          onclick: () => { $("follow").checked = false; select(r.interaction_id); },
        },
        h("div", { class: "title" }, r.first_message || "(no message)"),
        h("div", { class: "meta" },
          live ? h("span", { class: "dot live", title: "active in the last 2 minutes" }) : null,
          h("span", {}, ago(r.last_at)),
          h("span", {}, `${r.turns} turn${r.turns === 1 ? "" : "s"}`),
          r.tickets ? h("span", { class: "chip ok" }, "ticket") : null,
          r.handoffs ? h("span", { class: "chip warn" }, "handoff") : null,
          (r.rule_ids || []).slice(0, 6).map((id) => ruleChip(id))));
      }));
    }

    function select(id) {
      if (selected !== id) { cursor = null; signature = ""; open.clear(); }
      selected = id;
      detailPoll.stop();
      detailPoll.start();
      listPoll.now();
    }

    async function loadDetail(id) {
      const data = await api(`/interactions/${encodeURIComponent(id)}`);
      if (id !== selected) return;
      const sig = `${data.events.length}|${data.tickets.length}|${data.handoffs.length}`;
      if (sig === signature) return; // nothing new; keep scroll and state
      signature = sig;
      detail = data;
      render();
    }

    const evs = () => (detail ? detail.events : []);
    const isFlow = (e) => e.step === "flow" && e.data;

    // Consecutive flow events of one hook = one run through that hook's tree.
    function runOf(i) {
      const all = evs();
      if (!isFlow(all[i])) return null;
      let a = i;
      let b = i;
      const hook = all[i].data.hook;
      while (a > 0 && isFlow(all[a - 1]) && all[a - 1].data.hook === hook && !startsRun(all, a)) a--;
      while (b < all.length - 1 && isFlow(all[b + 1]) && all[b + 1].data.hook === hook && !startsRun(all, b + 1)) b++;
      return { hook, from: a, to: b };
    }
    // A new run of the same hook starts at the hook's root node.
    const startsRun = (all, i) => REF.doc.hooks[all[i].data.hook] === all[i].data.node && i > 0 && isFlow(all[i - 1]) && all[i - 1].data.hook === all[i].data.hook;

    function render() {
      const all = evs();
      if (cursor != null && cursor >= all.length) cursor = all.length - 1;
      const models = [...new Set(all.map((e) => e.model).filter(Boolean))];
      const tokens = all.reduce((n, e) => n + (e.tokens_in || 0) + (e.tokens_out || 0), 0);
      const cost = all.reduce((n, e) => n + Number(e.cost_usd || 0), 0);
      const decisions = all.filter((e) => isFlow(e) && e.data.check).length;

      const head = h("div", { class: "int-head" },
        h("h2", {}, "Conversation"),
        h("span", { class: "meta mono" }, detail.interaction_id),
        h("span", { class: "meta" }, `${clock(all[0] && all[0].created_at)} · ${all.length} steps · ${decisions} decisions · ${tokens} tokens${cost ? ` · $${cost.toFixed(4)}` : ""}`),
        h("span", { class: "meta" }, models.join(", ")));

      const controls = h("div", { class: "controls" },
        h("button", { class: "btn", onclick: () => step(-1), disabled: cursor === 0 || !all.length, title: "Previous step (←)" }, "← Prev"),
        h("span", { class: "count" }, cursor == null ? `${all.length} steps` : `Step ${cursor + 1} of ${all.length}`),
        h("button", { class: "btn", onclick: () => step(1), disabled: cursor === all.length - 1 || !all.length, title: "Next step (→)" }, "Next →"),
        h("button", { class: "btn small", onclick: () => jump(1), disabled: !all.length, title: "Next decision that fired a rule (Shift+→)" }, "Next rule ⇥"),
        h("span", { class: "spacer" }),
        cursor == null
          ? h("button", { class: "btn primary", onclick: () => { cursor = 0; render(); focusCurrent(); }, disabled: !all.length }, "Walk through")
          : h("button", { class: "btn", onclick: () => { cursor = null; render(); } }, "Show all"));

      const timeline = h("div", { class: "timeline" });
      let turn = 0;
      for (let i = 0; i < all.length; i++) {
        const e = all[i];
        if (e.step === "input_screen") timeline.append(h("div", { class: "turn-label" }, `Turn ${++turn}`));
        const run = runOf(i);
        if (run) {
          timeline.append(runCard(run));
          i = run.to;
        } else {
          timeline.append(eventCard(e, i));
        }
      }

      const records = h("div", { class: "records" },
        detail.tickets.length || detail.handoffs.length ? h("h3", {}, "Records in the database") : null,
        detail.tickets.map(ticketCard),
        detail.handoffs.map(handoffCard));

      fill($("int-detail"), head, controls,
        h("div", { class: "walk" }, h("div", { class: "walk-main" }, timeline, records), logicPanel()));
    }

    // The right-hand panel: the tree of the hook at the cursor, with the path taken.
    function logicPanel() {
      const all = evs();
      let i = cursor == null ? -1 : cursor;
      if (i >= 0 && !isFlow(all[i])) {   // show the most recent tree run before this step
        while (i >= 0 && !isFlow(all[i])) i--;
      }
      if (i < 0) {
        return h("aside", { class: "logic" }, h("p", { class: "hint" },
          cursor == null ? "Press Walk through (or click a step) to follow the agent's logic through the interaction tree." : "No tree has run yet at this step."));
      }
      const run = runOf(i);
      const trace = new Map();
      for (let k = run.from; k <= Math.min(run.to, cursor ?? run.to); k++) {
        const d = all[k].data;
        trace.set(d.node, { branch: d.branch, observed: d.observed, says: d.says, current: k === cursor });
      }
      const doc = REF.doc;
      const known = [...trace.keys()].every((id) => doc.nodes[id]);
      return h("aside", { class: "logic" },
        h("div", { class: "logic-head" }, h("strong", {}, "Logic: "), hookLabel(run.hook)),
        known && doc.hooks[run.hook]
          ? renderChain(doc, doc.hooks[run.hook], { trace })
          : h("div", {}, h("p", { class: "hint" }, "The tree has changed since this conversation; showing the path it took."),
            [...trace.entries()].map(([id, t]) => h("div", { class: `node visited ${t.current ? "current" : ""}` },
              h("div", { class: "node-head" }, h("span", { class: "node-title" }, id), h("span", { class: "chip" }, t.branch)),
              t.observed ? h("div", { class: "node-observed" }, "saw: ", t.observed) : null))),
        h("p", { class: "hint" }, "Tree as it is now in Supabase; highlighted nodes are the path this conversation took."));
    }

    function runCard(run) {
      const all = evs();
      const rows = [];
      for (let k = run.from; k <= run.to; k++) {
        const e = all[k];
        const d = e.data;
        const cls = cursor == null ? "" : k === cursor ? "current" : k > cursor ? "future" : "";
        rows.push(h("div", { class: `path-step ${cls} ${e.rule_id ? "fired" : ""}`, "data-i": k, onclick: () => { cursor = k; render(); } },
          h("span", { class: "node-icon" }, d.check ? "◆" : "▸"),
          h("span", { class: "path-title" }, d.title || d.node),
          d.rule ? ruleChip(d.rule, e.rule_id ? "fired" : "") : null,
          h("span", { class: `chip ${d.branch === "yes" ? "warn" : d.branch === "no" ? "ok" : "accent"}` }, d.branch),
          h("span", { class: "path-observed" }, d.says || d.observed || "")));
      }
      return h("div", { class: "ev run" },
        h("div", { class: "run-head" }, h("span", { class: "ev-stage" }, "Logic"), h("span", { class: "ev-step" }, hookLabel(run.hook))),
        rows);
    }

    function summary(e) {
      const d = e.data || {};
      const res = d.result || {};
      switch (e.step) {
        case "model": return e.outcome === "tool_calls" ? "decided to call tools" : e.outcome === "error" ? (e.text || "error") : "wrote a reply";
        case "tool:classifier": return res.code ? `${res.code} at ${Math.round((res.confidence || 0) * 100)}%` : d.error || "no code";
        case "tool:policy_lookup": return res.code ? `${res.code} → ${res.unit}; needs ${(res.required_fields || []).join(", ")}` : d.error || "";
        case "tool:search_tickets": return res.tickets ? `${res.tickets.length} open ticket(s) nearby` : d.error || "";
        case "tool:create_ticket": return res.ticket_id ? `created ${res.ticket_id}` : res.missing ? `missing: ${res.missing.join(", ")}` : d.error || e.text || "";
        case "tool:get_status": return res.status ? `status: ${res.status}` : d.error || e.text || "";
        case "tool:kb_search": return res.passages ? `${res.passages.length} passage(s): ${res.passages.map((p) => p.title).join(", ")}` : d.error || "";
        case "tool:handoff": return res.ref_id ? `${res.ref_id}: ${d.reason || ""}` : d.reason || e.text || "";
        default: return short(e.text || e.outcome || "");
      }
    }

    function eventCard(e, i) {
      const cls = cursor == null ? "" : i === cursor ? "current" : i > cursor ? "future" : "";
      const id = e.event_id || String(i);
      const pick = () => { cursor = i; render(); };

      if (e.step === "input_screen" || e.step === "reply") {
        const resident = e.step === "input_screen";
        return h("div", { class: `ev ${resident ? "resident" : "reply"} ${cls}`, "data-i": i, onclick: pick },
          h("div", { class: "who-label" },
            resident ? "Resident (masked)" : "Agent reply", " · ", clock(e.created_at),
            resident && e.outcome !== "clean" ? [" ", h("span", { class: "chip warn" }, e.outcome)] : null,
            e.rule_id ? [" ", ruleChip(e.rule_id)] : null),
          h("div", { class: "bubble" }, e.text || ""));
      }

      const outcomeClass = e.outcome === "ok" || e.outcome === "reply" || e.outcome === "tool_calls" ? "ok"
        : e.outcome === "error" || e.outcome === "blocked" ? "err" : "warn";
      const card = h("div", { class: `ev ${cls} ${open.has(id) ? "open" : ""}`, "data-i": i },
        h("div", {
          class: "ev-head",
          onclick: () => { open.has(id) ? open.delete(id) : open.add(id); card.classList.toggle("open"); if (cursor != null) pick(); },
        },
        h("span", { class: "ev-stage" }, e.step === "model" ? "Model" : "Tool"),
        h("span", { class: "ev-step mono" }, e.step.replace("tool:", "")),
        e.outcome ? h("span", { class: `chip ${outcomeClass}` }, e.outcome) : null,
        e.rule_id ? ruleChip(e.rule_id) : null,
        h("span", { class: "ev-sum" }, summary(e)),
        h("span", { class: "ev-time" },
          [e.latency_ms != null ? `${e.latency_ms} ms` : null, e.tokens_in || e.tokens_out ? `${e.tokens_in}→${e.tokens_out} tok` : null]
            .filter(Boolean).join(" · "))),
        h("div", { class: "ev-body" },
          e.model ? h("p", { class: "hint" }, `Model: ${e.model}`) : null,
          h("pre", {}, e.data ? JSON.stringify(e.data, null, 2) : e.text || "(nothing logged)")));
      return card;
    }

    function kv(obj) {
      return h("dl", { class: "kv" }, Object.entries(obj).flatMap(([k, v]) => [
        h("dt", {}, k), h("dd", {}, typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? "")),
      ]));
    }

    function ticketCard(t) {
      return h("div", { class: "record" },
        h("div", { class: "row" }, h("button", { class: "linkish mono", onclick: () => trackers.ticket.open(t.ticket_id) }, t.ticket_id),
          h("span", { class: "chip ok" }, t.status), h("span", {}, `${t.code} → ${t.unit || ""}`),
          h("span", { class: "hint" }, clock(t.opened_at))),
        kv({ location: t.location, postal_code: t.postal_code, ...(t.fields || {}) }));
    }

    function handoffCard(o) {
      return h("div", { class: "record" },
        h("div", { class: "row" }, h("button", { class: "linkish mono", onclick: () => trackers.handoff.open(o.ref_id) }, o.ref_id), ruleChip(o.rule_id),
          h("span", { class: "chip warn" }, o.status), h("span", {}, o.reason || ""),
          h("span", { class: "hint" }, clock(o.created_at))),
        kv({ code: o.code, confidence: o.confidence, ...(o.fields || {}),
          possible_duplicates: (o.possible_duplicates || []).map((d) => d.ticket_id).join(", ") || "none" }));
    }

    function step(delta) {
      const n = evs().length;
      if (!n) return;
      cursor = cursor == null ? 0 : Math.min(n - 1, Math.max(0, cursor + delta));
      render();
      focusCurrent();
    }

    function jump(dir) {   // next/previous step where a rule fired
      const all = evs();
      let i = cursor == null ? -1 : cursor;
      do { i += dir; } while (i >= 0 && i < all.length && !all[i].rule_id);
      if (i >= 0 && i < all.length) { cursor = i; render(); focusCurrent(); }
    }

    function focusCurrent() {
      const el = document.querySelector(`#int-detail .walk-main [data-i="${cursor}"]`);
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
      const node = document.querySelector("#int-detail .logic .node.current");
      if (node) node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    document.addEventListener("keydown", (ev) => {
      if (currentTab !== "interactions" || !detail) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
      if (ev.key === "ArrowRight") { ev.shiftKey ? jump(1) : step(1); ev.preventDefault(); }
      if (ev.key === "ArrowLeft") { ev.shiftKey ? jump(-1) : step(-1); ev.preventDefault(); }
      if (ev.key === "Escape" && cursor != null) { cursor = null; render(); }
    });
    $("follow").addEventListener("change", () => listPoll.now());

    return {
      start() { loadRef().catch(console.warn); listPoll.start(); if (selected) detailPoll.start(); },
      stop() { listPoll.stop(); detailPoll.stop(); },
      open(id) {
        $("follow").checked = false;
        if (currentTab !== "interactions") showTab("interactions");
        select(id);
      },
    };
  })();

  // =====================================================================
  // Tickets and handoffs: live lists, detail, status tracking
  // =====================================================================
  const statusLabel = (s) => (s || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const statusClass = (s) => ({ open: "accent", in_progress: "warn", closed: "ok", pending: "warn", in_review: "accent", resolved: "ok" }[s] || "");

  function tracker(kind) {
    const isTicket = kind === "ticket";
    const base = isTicket ? "/tickets" : "/handoffs";
    const filter = { status: "", extra: "", q: "" };
    let selected = null;
    let listSig = "";
    let detailSig = "";
    let filtersBuilt = false;
    const countEls = {};
    const poll = poller(load, 5000);

    function buildFilters(data) {
      const chips = h("div", { class: "status-chips", role: "group", "aria-label": "Status" },
        ["", ...data.statuses].map((s) => {
          const count = h("span", { class: "count" });
          countEls[s] = count;
          return h("button", {
            class: `chip-btn ${filter.status === s ? "on" : ""}`, "data-status": s,
            onclick: (e) => {
              filter.status = s;
              e.currentTarget.parentElement.querySelectorAll(".chip-btn").forEach((b) => b.classList.toggle("on", b.dataset.status === s));
              listSig = "";
              poll.now();
            },
          }, s ? statusLabel(s) : "All", " ", count);
        }));
      const extra = h("select", { "aria-label": isTicket ? "Service code" : "Rule", onchange: (e) => { filter.extra = e.target.value; listSig = ""; poll.now(); } },
        h("option", { value: "" }, isTicket ? "All codes" : "All rules"),
        (isTicket ? data.codes : Object.keys(REF.rules).filter((r) => /^[AEMF]/.test(r))).map((c) => h("option", { value: c }, c)));
      let timer = null;
      const search = h("input", {
        type: "search", placeholder: isTicket ? "Ticket number or location" : "Reference, reason or code",
        "aria-label": "Search",
        oninput: (e) => { clearTimeout(timer); timer = setTimeout(() => { filter.q = e.target.value.trim(); listSig = ""; poll.now(); }, 300); },
      });
      fill($(`${kind}-filters`), chips, h("div", { class: "filter-row" }, extra, search));
      filtersBuilt = true;
    }

    async function load() {
      const params = new URLSearchParams();
      if (filter.status) params.set("status", filter.status);
      if (filter.extra) params.set(isTicket ? "code" : "rule_id", filter.extra);
      if (filter.q) params.set("q", filter.q);
      const data = await api(`${base}?${params}`);
      if (!filtersBuilt) buildFilters(data);
      const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
      for (const [s, el] of Object.entries(countEls)) el.textContent = s ? (data.counts[s] || 0) : total;

      const rows = data[isTicket ? "tickets" : "handoffs"];
      const sig = JSON.stringify([rows, selected]);
      if (sig !== listSig) {
        listSig = sig;
        fill($(`${kind}-list`), rows.length ? rows.map(isTicket ? ticketItem : handoffItem)
          : [h("p", { class: "hint pad" }, filter.status || filter.extra || filter.q ? "Nothing matches these filters." : `No ${kind}s yet.`)]);
      }
      if (selected) await loadDetail();
    }

    function ticketItem(t) {
      return h("button", { class: `item ${t.ticket_id === selected ? "selected" : ""}`, onclick: () => open(t.ticket_id) },
        h("div", { class: "title" }, h("span", { class: "mono" }, t.ticket_id), " · ", t.location || "(no location)"),
        h("div", { class: "meta" }, h("span", { class: `chip ${statusClass(t.status)}` }, statusLabel(t.status)),
          h("span", { class: "mono" }, t.code), h("span", {}, t.unit || ""), h("span", {}, ago(t.opened_at))));
    }

    function handoffItem(o) {
      return h("button", { class: `item ${o.ref_id === selected ? "selected" : ""}`, onclick: () => open(o.ref_id) },
        h("div", { class: "title" }, h("span", { class: "mono" }, o.ref_id), " · ", o.reason || "(no reason)"),
        h("div", { class: "meta" }, h("span", { class: `chip ${statusClass(o.status)}` }, statusLabel(o.status)),
          ruleChip(o.rule_id), o.code ? h("span", { class: "mono" }, o.code) : null, h("span", {}, ago(o.created_at))));
    }

    function open(id) {
      selected = id;
      detailSig = "";
      listSig = "";
      if (currentTab !== `${kind}s`) showTab(`${kind}s`);
      else poll.now();
    }

    async function loadDetail() {
      const id = selected;
      let data;
      try {
        data = await api(`${base}/${encodeURIComponent(id)}`);
      } catch (e) {
        fill($(`${kind}-detail`), h("div", { class: "empty-state" }, e.message));
        return;
      }
      if (id !== selected) return;
      const sig = JSON.stringify(data);
      // Don't redraw under someone typing a note.
      const note = document.querySelector(`#${kind}-detail textarea`);
      if (sig === detailSig || (note && note.value.trim())) return;
      detailSig = sig;
      render(data);
    }

    function kvList(obj) {
      const entries = Object.entries(obj).filter(([, v]) => v !== null && v !== undefined && v !== "");
      if (!entries.length) return h("p", { class: "hint" }, "Nothing recorded.");
      return h("dl", { class: "kv" }, entries.flatMap(([k, v]) => [
        h("dt", {}, k.replace(/_/g, " ")), h("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v)),
      ]));
    }

    function history(createdAt, createdText, updates) {
      return h("ol", { class: "history-list" },
        h("li", {}, h("span", { class: "when" }, new Date(createdAt).toLocaleString()), h("span", {}, createdText)),
        updates.map((u) => h("li", {},
          h("span", { class: "when" }, new Date(u.created_at).toLocaleString()),
          h("span", {},
            h("strong", {}, u.author || "someone"),
            u.new_status ? [" changed status ", h("span", { class: `chip ${statusClass(u.old_status)}` }, statusLabel(u.old_status)), " → ",
              h("span", { class: `chip ${statusClass(u.new_status)}` }, statusLabel(u.new_status))] : " added a note",
            u.note ? h("div", { class: "note-text" }, u.note) : null))));
    }

    function updateForm(id, status, statuses) {
      const sel = h("select", { "aria-label": "Status" }, statuses.map((s) => h("option", { value: s, selected: s === status }, statusLabel(s))));
      const note = h("textarea", { rows: "2", placeholder: "Note for the team (optional when changing status)", maxlength: "1000" });
      const msg = h("span", { class: "hint" });
      const save = h("button", {
        class: "btn primary",
        onclick: async () => {
          save.disabled = true;
          try {
            await api(`${base}/${encodeURIComponent(id)}/updates`, {
              method: "POST", body: JSON.stringify({ status: sel.value, note: note.value.trim() || null, author: who() }),
            });
            note.value = "";
            detailSig = "";
            listSig = "";
            await load();
          } catch (e) {
            msg.className = "hint error-text";
            msg.textContent = (e.errors || [e.message]).join(" ");
            save.disabled = false;
          }
        },
      }, "Save update");
      return h("div", { class: "update-form" }, h("h3", {}, "Update"),
        h("div", { class: "ed-actions" }, h("label", { class: "ne-field inline" }, h("span", {}, "Status"), sel), save, msg), note,
        who() ? null : h("p", { class: "hint" }, "Tip: fill in Your name at the top so the history shows who made the change."));
    }

    const convoLink = (iid) => iid
      ? h("button", { class: "btn small", onclick: () => interactions.open(iid) }, "Open the conversation ↗")
      : h("span", { class: "hint" }, "No linked conversation (created before tracking, or seeded).");

    function render(data) {
      if (isTicket) {
        const t = data.ticket;
        fill($("ticket-detail"),
          h("div", { class: "rec-head" },
            h("h2", { class: "mono" }, t.ticket_id),
            h("span", { class: `chip ${statusClass(t.status)}` }, statusLabel(t.status)),
            h("span", {}, `${t.code}: ${data.category || ""} → ${t.unit || ""}`),
            h("span", { class: "hint" }, `opened ${new Date(t.opened_at).toLocaleString()} (${ago(t.opened_at)})`)),
          h("div", { class: "rec-grid" },
            h("section", {}, h("h3", {}, "Where"), kvList({ location: t.location, postal_code: t.postal_code })),
            h("section", {}, h("h3", {}, "What was collected"),
              kvList(Object.fromEntries([...data.required_fields.map((f) => [f, (t.fields || {})[f] ?? "(missing)"]),
                ...Object.entries(t.fields || {}).filter(([k]) => !data.required_fields.includes(k))])))),
          h("div", { class: "ed-actions" }, convoLink(t.interaction_id)),
          updateForm(t.ticket_id, t.status, data.statuses),
          h("h3", {}, "History"),
          history(t.opened_at, "Created by the agent", data.updates),
          h("p", { class: "hint" }, "Shown masked (D6) because this page has no login yet. The agent itself can never edit, close or reprioritize a ticket (A7)."));
        return;
      }
      const o = data.handoff;
      const rule = REF.rules[o.rule_id];
      fill($("handoff-detail"),
        h("div", { class: "rec-head" },
          h("h2", { class: "mono" }, o.ref_id),
          h("span", { class: `chip ${statusClass(o.status)}` }, statusLabel(o.status)),
          ruleChip(o.rule_id),
          h("span", { class: "hint" }, `${new Date(o.created_at).toLocaleString()} (${ago(o.created_at)})`)),
        h("p", { class: "rec-reason" }, h("strong", {}, "Why: "), o.reason || "(no reason given)",
          rule ? h("span", { class: "hint" }, ` · ${o.rule_id}: ${rule.text}`) : null),
        h("div", { class: "rec-grid" },
          h("section", {}, h("h3", {}, "Routing"), kvList({ code: o.code, confidence: o.confidence })),
          h("section", {}, h("h3", {}, "What was collected"), kvList(o.fields || {}))),
        (o.possible_duplicates || []).length ? h("section", {}, h("h3", {}, "Possible duplicates"),
          h("div", { class: "ed-actions" }, o.possible_duplicates.map((d) =>
            h("button", { class: "btn small mono", onclick: () => trackers.ticket.open(d.ticket_id) }, `${d.ticket_id} (${d.status})`)))) : null,
        h("div", { class: "ed-actions" }, convoLink(o.interaction_id)),
        updateForm(o.ref_id, o.status, data.statuses),
        h("h3", {}, "Transcript (masked)"),
        h("div", { class: "transcript" }, (o.transcript || []).map((m) =>
          h("div", { class: `bubble ${m.role === "resident" ? "resident-b" : "agent-b"}` }, m.text))),
        h("h3", {}, "History"),
        history(o.created_at, `Handed off by the agent under ${o.rule_id}`, data.updates));
    }

    return {
      start() { poll.start(); },
      stop() { poll.stop(); },
      open,
    };
  }
  const trackers = { ticket: tracker("ticket"), handoff: tracker("handoff") };

  // =====================================================================
  // Flow: edit the interaction tree
  // =====================================================================
  const flowEditor = (() => {
    let draft = null;       // working copy of {hooks, nodes}
    let saved = null;       // JSON of the last saved tree
    let hook = "message";
    let selected = null;
    let errors = null;
    const dirty = () => draft && JSON.stringify(draft) !== saved;

    async function load(force) {
      if (draft && dirty() && !force) { render(); return; }
      await loadRef();
      draft = clone(REF.doc);
      saved = JSON.stringify(draft);
      errors = REF.errors && REF.errors.length ? REF.errors : null;
      if (!(hook in REF.catalogue.hooks)) hook = "message";
      render();
    }

    function hooksInOrder() {
      const used = Object.keys(draft.hooks || {});
      return [...used, ...Object.keys(REF.catalogue.hooks).filter((k) => !used.includes(k))];
    }

    function reachableFrom(start) {
      const seen = new Set();
      const stack = start ? [start] : [];
      while (stack.length) {
        const id = stack.pop();
        if (seen.has(id) || !draft.nodes[id]) continue;
        seen.add(id);
        const n = draft.nodes[id];
        [n.yes, n.no, n.next].forEach((t) => t && stack.push(t));
      }
      return seen;
    }

    function render() {
      const all = new Set();
      Object.values(draft.hooks || {}).forEach((s) => reachableFrom(s).forEach((id) => all.add(id)));
      const orphans = Object.keys(draft.nodes).filter((id) => !all.has(id));

      fill($("hook-list"), hooksInOrder().map((k) => {
        const n = reachableFrom(draft.hooks[k]).size;
        return h("button", { class: `item ${k === hook ? "selected" : ""}`, onclick: () => { hook = k; selected = null; render(); } },
          h("div", { class: "title" }, hookLabel(k)),
          h("div", { class: "meta" }, h("span", { class: "mono" }, k), n ? h("span", {}, `${n} step${n === 1 ? "" : "s"}`) : h("span", {}, "empty"),
            Object.entries(REF.catalogue.hard_rules).filter(([, hk]) => hk === k).map(([r]) => ruleChip(r))));
      }), orphans.length ? h("button", { class: `item ${hook === "__orphans" ? "selected" : ""}`, onclick: () => { hook = "__orphans"; selected = null; render(); } },
        h("div", { class: "title" }, "Not connected"), h("div", { class: "meta" }, `${orphans.length} step(s) no hook reaches`)) : null);

      $("hook-title").textContent = hook === "__orphans" ? "Not connected" : hookLabel(hook);
      $("hook-desc").textContent = hook === "__orphans"
        ? "Steps no hook reaches. Connect them from a branch, or delete them."
        : `Hook "${hook}". The agent runs this tree here on every turn. Click a step to edit it.`;
      const isDirty = dirty();
      $("flow-save").disabled = !isDirty;
      $("flow-discard").disabled = !isDirty;
      $("flow-status").textContent = isDirty ? "Unsaved changes" : "Saved";
      $("flow-status").className = isDirty ? "dirty" : "hint";
      fill($("flow-errors"), errorBox("Not saved: this tree would break the agent.", errors));

      const opts = { selected, onSelect: (id) => { selected = id; render(); }, onAdd: addStep };
      fill($("flow-tree"), hook === "__orphans"
        ? orphans.map((id) => renderChain(draft, id, opts, new Set(Object.keys(draft.nodes).filter((x) => x !== id && !orphans.includes(x)))))
        : renderChain(draft, draft.hooks[hook], opts));
      renderEditor();
    }

    function uniqueId(title) {
      const base = (title || "step").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "step";
      let id = base;
      for (let i = 2; draft.nodes[id]; i++) id = `${base}_${i}`;
      return id;
    }

    function addStep(fromId, slot) {
      const id = uniqueId("new_step");
      draft.nodes[id] = { title: "New step", action: "flag" };
      if (slot === "hook") draft.hooks[hook] = id;
      else draft.nodes[fromId][slot] = id;
      selected = id;
      render();
    }

    function targetSelect(value, onChange, exclude) {
      const sel = h("select", { onchange: (e) => onChange(e.target.value || null) },
        h("option", { value: "" }, "(carry on / end)"),
        Object.entries(draft.nodes).filter(([id]) => id !== exclude)
          .map(([id, n]) => h("option", { value: id, selected: id === value }, `${n.title || id} (${id})`)));
      sel.value = value || "";
      return sel;
    }

    function field(label, control, hint) {
      return h("label", { class: "ne-field" }, h("span", {}, label), control, hint ? h("span", { class: "hint" }, hint) : null);
    }

    function paramControl(n, key, desc) {
      const p = n.params || (n.params = {});
      const set = (v) => { if (v === "" || v == null) delete p[key]; else p[key] = v; render(); };
      if (key === "flag") {
        return h("select", { onchange: (e) => set(e.target.value) }, REF.flags.map((f) => h("option", { value: f, selected: p.flag === f }, f)));
      }
      if (key === "wording" || key === "prefix") {
        const sel = h("select", { onchange: (e) => set(e.target.value) },
          key === "prefix" ? h("option", { value: "" }, "(none)") : null,
          Object.keys(REF.wording).map((w) => h("option", { value: w, selected: p[key] === w }, w)));
        return h("div", {}, sel, p[key] ? h("p", { class: "hint quote" }, `"${REF.wording[p[key]] || ""}"`) : null);
      }
      if (key === "handoff_rule") {
        return h("select", { onchange: (e) => set(e.target.value) }, h("option", { value: "" }, "(the step's rule)"),
          Object.keys(REF.rules).map((r) => h("option", { value: r, selected: p[key] === r }, r)));
      }
      if (/number/.test(desc)) {
        return h("input", { type: "number", min: "0", value: p[key] ?? "", onchange: (e) => set(e.target.value === "" ? null : Number(e.target.value)) });
      }
      return h("textarea", { rows: "3", onchange: (e) => set(e.target.value) }, p[key] || "");
    }

    function renderEditor() {
      const box = $("node-editor");
      const n = selected && draft.nodes[selected];
      if (!n) { box.hidden = true; return; }
      box.hidden = false;
      const isDecision = !!n.check;
      const kindSel = h("select", {
        onchange: (e) => {
          if (e.target.value === "decision") { delete n.action; delete n.next; n.check = "always"; }
          else { delete n.check; delete n.yes; delete n.no; n.action = "flag"; }
          n.params = {};
          render();
        },
      }, h("option", { value: "decision", selected: isDecision }, "Decision (yes / no)"), h("option", { value: "action", selected: !isDecision }, "Action"));

      const cat = isDecision ? REF.catalogue.checks : REF.catalogue.actions;
      const typeSel = h("select", {
        onchange: (e) => { n[isDecision ? "check" : "action"] = e.target.value; n.params = {}; if (!isDecision && cat[e.target.value].ends) delete n.next; render(); },
      }, Object.entries(cat).map(([k, v]) => h("option", { value: k, selected: (isDecision ? n.check : n.action) === k },
        `${v.label}${isDecision && v.hooks && !v.hooks.includes(hook) && hook !== "__orphans" ? " (not for this hook)" : ""}`)));
      const params = (cat[isDecision ? n.check : n.action] || {}).params || {};

      const ruleSel = h("select", { onchange: (e) => { if (e.target.value) n.rule = e.target.value; else delete n.rule; render(); } },
        h("option", { value: "" }, "(none)"),
        Object.entries(REF.rules).map(([r, v]) => h("option", { value: r, selected: n.rule === r }, `${r}${v.hard ? " [hard]" : ""}: ${short(v.text, 60)}`)));

      fill(box,
        h("div", { class: "ne-head" }, h("strong", {}, "Edit step"), h("span", { class: "mono hint" }, selected),
          h("button", { class: "icon-x", "aria-label": "Close", onclick: () => { selected = null; render(); } }, "×")),
        field("Title", h("input", { value: n.title || "", onchange: (e) => { n.title = e.target.value; render(); } }),
          isDecision ? "Phrase it as a yes/no question." : null),
        field("Notes", h("textarea", { rows: "2", onchange: (e) => { if (e.target.value) n.description = e.target.value; else delete n.description; render(); } }, n.description || "")),
        field("Policy rule", ruleSel),
        h("label", { class: "ne-check" }, h("input", { type: "checkbox", checked: n.enabled !== false, onchange: (e) => { if (e.target.checked) delete n.enabled; else n.enabled = false; render(); } }),
          " Switched on", h("span", { class: "hint" }, isDecision ? " (off: takes the no branch)" : " (off: skipped)")),
        field("Kind", kindSel),
        field(isDecision ? "Check" : "Action", typeSel),
        Object.entries(params).map(([k, desc]) => field(k, paramControl(n, k, desc), desc)),
        isDecision
          ? [field("If yes, go to", targetSelect(n.yes, (v) => { if (v) n.yes = v; else delete n.yes; render(); }, selected)),
            field("If no, go to", targetSelect(n.no, (v) => { if (v) n.no = v; else delete n.no; render(); }, selected))]
          : (REF.catalogue.actions[n.action]?.ends
            ? h("p", { class: "hint" }, "This action ends the hook (the turn ends, or the tool call is refused).")
            : field("Then go to", targetSelect(n.next, (v) => { if (v) n.next = v; else delete n.next; render(); }, selected))),
        h("div", { class: "ne-actions" },
          Object.keys(REF.catalogue.hooks).some((k) => k === hook) && draft.hooks[hook] !== selected
            ? h("button", { class: "btn small", onclick: () => { draft.hooks[hook] = selected; render(); } }, "Start this hook here") : null,
          h("button", {
            class: "btn small danger",
            onclick: () => {
              if (!confirm(`Delete "${n.title || selected}"? Branches that pointed to it will carry on instead.`)) return;
              delete draft.nodes[selected];
              for (const m of Object.values(draft.nodes)) for (const k of ["yes", "no", "next"]) if (m[k] === selected) delete m[k];
              for (const k of Object.keys(draft.hooks)) if (draft.hooks[k] === selected) delete draft.hooks[k];
              selected = null;
              render();
            },
          }, "Delete step")));
    }

    $("flow-save").addEventListener("click", async () => {
      $("flow-save").disabled = true;
      try {
        await api("/flow", { method: "PUT", body: JSON.stringify({ doc: draft, author: who(), note: $("flow-note").value.trim() || null }) });
        $("flow-note").value = "";
        errors = null;
        await load(true);
        $("flow-status").textContent = "Saved. Live for the next message.";
        $("flow-status").className = "saved";
      } catch (e) {
        errors = e.errors || [e.message];
        render();
      }
    });
    $("flow-discard").addEventListener("click", () => { if (confirm("Discard your changes to the tree?")) { errors = null; selected = null; load(true); } });
    window.addEventListener("beforeunload", (ev) => { if (dirty()) { ev.preventDefault(); ev.returnValue = ""; } });

    return { load };
  })();

  // =====================================================================
  // Files: every config file, in Supabase
  // =====================================================================
  const files = (() => {
    let current = null; // {path, content, in_use, updated_at, updated_by, isNew}
    let dirty = false;

    async function load() {
      const { files: rows } = await api("/files");
      const groups = [["Config", rows.filter((f) => !f.path.startsWith("prompts/"))],
        ["Prompts", rows.filter((f) => f.path.startsWith("prompts/"))]];
      fill($("file-list"), groups.flatMap(([name, list]) => [
        h("div", { class: "pane-head" }, h("span", { class: "hint" }, name)),
        ...list.map((f) => h("button", {
          class: `item ${current && current.path === f.path ? "selected" : ""}`,
          onclick: () => openFile(f.path),
        },
        h("div", { class: "title mono" }, f.path),
        h("div", { class: "meta" },
          f.in_use ? h("span", { class: "chip ok" }, "in use") : h("span", { class: "chip" }, "not in use"),
          h("span", {}, `${f.updated_by || "someone"} · ${ago(f.updated_at)}`)))),
      ]));
    }

    async function openFile(path) {
      if (dirty && !confirm("Discard your unsaved changes?")) return;
      current = await api(`/file?path=${encodeURIComponent(path)}`);
      dirty = false;
      renderEditor();
      load();
    }

    function renderEditor(errors, message) {
      const f = current;
      const text = h("textarea", { class: "code", spellcheck: "false", "aria-label": `Contents of ${f.path}` });
      text.value = f.content;
      const status = h("span", { class: message ? "saved" : "dirty" }, message || (dirty ? "Unsaved changes" : ""));
      const note = h("input", { class: "note", placeholder: "What did you change, and why? (kept in the history)", maxlength: "500" });
      const save = h("button", { class: "btn primary", title: "Save (Ctrl+S)" }, f.isNew ? "Create" : "Save");
      const history = h("div", { class: "history", hidden: true });

      text.addEventListener("input", () => { dirty = true; status.className = "dirty"; status.textContent = "Unsaved changes"; });
      text.addEventListener("keydown", (ev) => {
        if (ev.key === "Tab") {
          ev.preventDefault();
          text.setRangeText("  ", text.selectionStart, text.selectionEnd, "end");
          text.dispatchEvent(new Event("input"));
        }
        if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save.click(); }
      });

      save.addEventListener("click", async () => {
        save.disabled = true;
        try {
          await api(`/file?path=${encodeURIComponent(f.path)}`, {
            method: "PUT", body: JSON.stringify({ content: text.value, author: who(), note: note.value.trim() || null }),
          });
          current = await api(`/file?path=${encodeURIComponent(f.path)}`);
          dirty = false;
          renderEditor(null, "Saved. Live for the next message.");
          load();
          if (f.path === "flow.yaml" || f.path === "policy.yaml") loadRef().catch(console.warn);
        } catch (e) {
          current = { ...f, content: text.value };
          dirty = true;
          renderEditor(e.errors || [e.message]);
        }
      });

      const del = f.isNew ? null : h("button", {
        class: "btn danger", disabled: f.in_use, title: f.in_use ? "The live agent reads this file" : "Delete this file (it stays in the history)",
        onclick: async () => {
          if (!confirm(`Delete ${f.path}? Its versions stay in the history.`)) return;
          try {
            await api(`/file?path=${encodeURIComponent(f.path)}${who() ? `&author=${encodeURIComponent(who())}` : ""}`, { method: "DELETE" });
            dirty = false;
            current = null;
            fill($("file-detail"), h("div", { class: "empty-state" }, "File deleted."));
            load();
          } catch (e) { renderEditor(e.errors || [e.message]); }
        },
      }, "Delete");

      const download = h("button", {
        class: "btn", title: "Save a copy to your computer",
        onclick: () => {
          const a = h("a", { href: URL.createObjectURL(new Blob([text.value], { type: "text/plain" })), download: f.path.split("/").pop() });
          a.click();
          setTimeout(() => URL.revokeObjectURL(a.href), 1000);
        },
      }, "Download");

      const histBtn = f.isNew ? null : h("button", {
        class: "btn",
        onclick: async () => {
          if (!history.hidden) { history.hidden = true; return; }
          const { revisions } = await api(`/revisions?path=${encodeURIComponent(f.path)}`);
          fill(history, revisions.length ? revisions.map((r) => h("button", {
            class: "item", disabled: r.content == null,
            title: r.content == null ? "Deleted" : "Load this version into the editor (then Save to restore it)",
            onclick: () => { text.value = r.content; text.dispatchEvent(new Event("input")); },
          },
          h("div", {}, r.note || (r.content == null ? "deleted" : "(no note)")),
          h("div", { class: "meta" }, h("span", {}, r.author || "someone"), h("span", {}, new Date(r.saved_at).toLocaleString())))) : [h("p", { class: "hint pad" }, "No saved versions yet.")]);
          history.hidden = false;
        },
      }, "History");

      fill($("file-detail"),
        h("div", { class: "ed-head" },
          h("h2", {}, f.path),
          h("span", { class: "meta" }, f.isNew ? "New file" : `Saved by ${f.updated_by || "someone"} · ${ago(f.updated_at)}`),
          f.in_use ? h("span", { class: "chip ok", title: "The live agent reads this file" }, "in use") : h("span", { class: "chip", title: "Not read by the agent right now" }, "not in use"),
          f.path === "flow.yaml" ? h("a", { class: "link", href: "#flow" }, "Edit as a tree ↗") : null),
        errorBox("Not saved: this would break the agent.", errors),
        h("div", { class: "ed-actions" }, save, note, status),
        text,
        h("div", { class: "ed-actions" }, del, download, histBtn,
          h("span", { class: "hint" }, "Stored only in Supabase. Saves go live for the next message; every version is kept.")),
        history);
      if (!errors) text.focus();
    }

    $("new-prompt").addEventListener("click", async () => {
      const name = prompt("New prompt path, e.g. prompts/agent/system_v2.md", "prompts/agent/system_v2.md");
      if (!name) return;
      const path = name.trim();
      if (!/^prompts\/[a-z0-9_]+\/[A-Za-z0-9_.-]+\.(md|txt)$/.test(path)) {
        alert("Use prompts/<folder>/<name>.md (lowercase folder; letters, numbers, _ . - in the name).");
        return;
      }
      let base = "";
      try { base = (await api(`/file?path=${encodeURIComponent("prompts/agent/system_v1.md")}`)).content; } catch { /* start empty */ }
      current = { path, content: base, in_use: false, isNew: true };
      dirty = true;
      renderEditor(null, "");
    });

    window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
    return { load };
  })();

  // =====================================================================
  // Knowledge base
  // =====================================================================
  const kb = (() => {
    let current = null;
    let docs = [];
    const poll = poller(load, 8000);

    async function load() {
      ({ documents: docs } = await api("/kb"));
      fill($("kb-list"), docs.length ? docs.map((d) => {
        const pending = d.active && (!d.chunked_at || new Date(d.updated_at) > new Date(d.chunked_at));
        return h("button", {
          class: `item ${current && current.id === d.id ? "selected" : ""}`,
          onclick: () => openDoc(d.id),
        },
        h("div", { class: "title" }, d.title),
        h("div", { class: "meta" },
          !d.active ? h("span", { class: "chip" }, "hidden") : pending ? h("span", { class: "chip warn" }, "indexing…") : h("span", { class: "chip ok" }, "searchable"),
          h("span", { class: "mono" }, d.source),
          h("span", {}, ago(d.updated_at))));
      }) : [h("p", { class: "hint pad" }, "No documents yet.")]);
    }

    function openDoc(id) {
      current = docs.find((d) => d.id === id) || null;
      if (current) render();
      load();
    }

    function render(errors, message) {
      const d = current;
      const isNew = !d.id;
      const title = h("input", { value: d.title || "", maxlength: "300", "aria-label": "Title" });
      const source = h("input", { class: "mono", value: d.source || "", maxlength: "200", placeholder: "unique label (optional)", disabled: !isNew, "aria-label": "Source label" });
      const active = h("input", { type: "checkbox", checked: d.active !== false });
      const content = h("textarea", { class: "prose", "aria-label": "Content (Markdown)" });
      content.value = d.content || "";

      const save = h("button", {
        class: "btn primary",
        onclick: async () => {
          const payload = { title: title.value.trim(), content: content.value, active: active.checked, source: isNew ? (source.value.trim() || null) : d.source };
          try {
            const { document } = isNew
              ? await api("/kb", { method: "POST", body: JSON.stringify(payload) })
              : await api(`/kb/${encodeURIComponent(d.id)}`, { method: "PUT", body: JSON.stringify(payload) });
            current = document;
            render(null, active.checked ? "Saved. The agent can find it now." : "Saved. Hidden from the agent.");
            load();
          } catch (e) {
            current = { ...d, ...payload };
            render(e.errors || [e.message]);
          }
        },
      }, isNew ? "Create" : "Save");

      const del = isNew ? null : h("button", {
        class: "btn danger",
        onclick: async () => {
          if (!confirm(`Delete "${d.title}" for good? To hide it instead, untick "Searchable by the agent".`)) return;
          await api(`/kb/${encodeURIComponent(d.id)}`, { method: "DELETE" });
          current = null;
          fill($("kb-detail"), h("div", { class: "empty-state" }, "Document deleted."));
          load();
        },
      }, "Delete");

      fill($("kb-detail"),
        h("div", { class: "ed-head" }, h("h2", {}, isNew ? "New document" : d.title)),
        errorBox("Not saved.", errors),
        h("div", { class: "fields-row" },
          h("label", {}, "Title", title),
          h("label", {}, "Source", source),
          h("label", {}, active, "Searchable by the agent")),
        content,
        h("div", { class: "ed-actions" }, save, del, h("span", { class: "saved" }, message || ""),
          h("span", { class: "hint" }, "One topic per document; blank lines separate chunks. Synthetic content only.")));
    }

    $("new-doc").addEventListener("click", () => {
      current = { title: "", content: "# Title\n\n", active: true };
      render();
    });

    return { start: () => poll.start(), stop: () => poll.stop() };
  })();

  // =====================================================================
  // Analytics: requests per hour / day / month, latency, running cost
  // =====================================================================
  const analytics = (() => {
    const RANGES = {
      hour: [[24, "Last 24 hours"], [48, "Last 48 hours"], [168, "Last 7 days"], [744, "Last 31 days"]],
      day: [[7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"], [365, "Last 12 months"]],
      month: [[6, "Last 6 months"], [12, "Last 12 months"], [24, "Last 24 months"], [36, "Last 36 months"]],
    };
    const OUTCOMES = [
      { key: "ticket", label: "Filed a ticket", color: "var(--series-1)" },
      { key: "handoff", label: "Handed to staff", color: "var(--series-2)" },
      { key: "answered", label: "Answered or sent to 911", color: "var(--series-3)" },
    ];
    const COST_LABELS = {
      currency: "Currency",
      labour_rate_per_hour: "Staff cost per hour",
      manual_handle_time_s: "Manual handle time (s)",
      review_time_s: "Handoff review time (s)",
      hosting_per_month: "Hosting per month",
      ollama_per_million_tokens: "Ollama per million tokens",
    };
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const view = { bucket: store("analytics-bucket") || "day", periods: Number(store("analytics-periods")) || 30 };
    if (!RANGES[view.bucket]) view.bucket = "day";
    let data = null;
    let sig = "";
    let costsShown = "";
    let costsSaved = "";   // survives the form redrawing with the saved values
    const poll = poller(load, 30000);

    // ---------- formatting ----------
    const num = (v) => (v == null ? "–" : Number(v).toLocaleString());
    const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
    const secs = (ms) => (ms == null ? "–" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
    function money(v, currency) {
      if (v == null) return "–";
      const small = v !== 0 && Math.abs(v) < 0.01;
      const opts = small ? { maximumSignificantDigits: 2 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
      try {
        return new Intl.NumberFormat(undefined, { style: "currency", currency, ...opts }).format(v);
      } catch {
        return `${new Intl.NumberFormat(undefined, opts).format(v)} ${currency}`;
      }
    }
    const cur = (v) => money(v, data.costs.currency);
    function periodLabel(iso, long = false) {
      const d = new Date(iso);
      if (data.bucket === "hour") {
        const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
        return long || d.getHours() === 0 ? `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${time}` : time;
      }
      if (data.bucket === "day") return d.toLocaleDateString(undefined, long ? { weekday: "short", month: "short", day: "numeric", year: "numeric" } : { month: "short", day: "numeric" });
      return d.toLocaleDateString(undefined, { month: "short", year: long ? "numeric" : "2-digit" });
    }

    // ---------- toolbar ----------
    function toolbar() {
      const group = h("div", { class: "seg", role: "group", "aria-label": "Group by" },
        ["hour", "day", "month"].map((b) => h("button", {
          class: view.bucket === b ? "on" : "", "aria-pressed": String(view.bucket === b),
          onclick: () => {
            if (view.bucket === b) return;
            view.bucket = b;
            view.periods = RANGES[b][1][0];
            remember();
          },
        }, b[0].toUpperCase() + b.slice(1))));
      const range = h("select", { "aria-label": "Period", onchange: (e) => { view.periods = Number(e.target.value); remember(); } },
        RANGES[view.bucket].map(([n, label]) => h("option", { value: String(n), selected: n === view.periods }, label)));
      fill($("an-toolbar"),
        h("div", { class: "an-title" }, h("h2", {}, "Analytics"),
          h("p", { class: "hint" }, `Live from the event log in Supabase · times in ${tz}`)),
        h("label", { class: "an-ctl" }, h("span", {}, "Group by"), group),
        h("label", { class: "an-ctl" }, h("span", {}, "Show"), range),
        h("span", { id: "an-updated", class: "hint live-note" }));
    }
    function remember() {
      store("analytics-bucket", view.bucket);
      store("analytics-periods", String(view.periods));
      toolbar();
      sig = "";
      poll.now();
    }

    async function load() {
      const q = new URLSearchParams({ bucket: view.bucket, periods: String(view.periods), tz });
      const next = await api(`/analytics?${q}`);
      $("an-updated").textContent = `updated ${clock(next.generated_at)}`;
      const s = JSON.stringify({ ...next, generated_at: null });
      if (s === sig) return;
      sig = s;
      data = next;
      render();
    }

    // ---------- tiles ----------
    function tile(label, value, sub, status) {
      return h("div", { class: "tile" },
        h("div", { class: "tile-label" }, label),
        h("div", { class: "tile-value" }, value),
        sub ? h("div", { class: "tile-sub" }, status ? h("span", { class: `chip ${status}` }, status === "ok" ? "✓ on target" : "✕ over target") : null, sub) : null);
    }
    function tiles() {
      const t = data.totals;
      const lat = data.latency;
      const status = lat.p95_ms == null ? null : lat.p95_ms < lat.target_ms ? "ok" : "err";
      return h("div", { class: "tiles" },
        tile("Requests", num(t.requests), `${num(t.turns)} messages from residents`),
        tile("Tickets filed", num(t.tickets), `${pct(t.ticket_rate)} of requests`),
        tile("Handed to staff", num(t.handoffs), `${pct(t.handoff_rate)} of requests`),
        tile("Reply time p95", secs(lat.p95_ms), ` median ${secs(lat.p50_ms)} · target under ${secs(lat.target_ms)} (G10)`, status),
        tile("Running cost", cur(t.total_cost), `${cur(t.cost_per_request)} per request`),
        tile("By hand it would cost", cur(t.manual_cost), `${cur(t.manual_per_request)} per request`),
        tile(t.savings >= 0 ? "Saved" : "Extra cost", cur(Math.abs(t.savings)), t.requests ? `${pct(t.manual_cost ? t.savings / t.manual_cost : null)} of the manual cost` : "no requests yet"));
    }

    // ---------- column chart (SVG) ----------
    const SVG = "http://www.w3.org/2000/svg";
    function s(tag, attrs, ...children) {
      const el = document.createElementNS(SVG, tag);
      for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
      for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
      return el;
    }
    function niceMax(v) {
      if (v <= 0) return 1;
      const p = 10 ** Math.floor(Math.log10(v));
      return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
    }
    // Column with a 4px rounded top and a square base.
    function column(x, y, w, hgt, rounded) {
      const r = rounded ? Math.min(4, w / 2, hgt) : 0;
      return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
    }

    function chart({ series, value, fmt, tip, whole = false }) {
      const wrap = h("div", { class: "chart" });
      const tipEl = h("div", { class: "chart-tip", role: "status", hidden: true });
      wrap.append(tipEl);
      const draw = () => {
        const W = Math.max(280, wrap.clientWidth || 640);
        const H = 220, L = 52, R = 8, T = 10, B = 28;
        const rows = data.rows;
        const pw = W - L - R, ph = H - T - B;
        const band = pw / rows.length;
        const bw = Math.max(2, Math.min(24, band * 0.7));
        const top = Math.max(...rows.map((r) => series.reduce((a, k) => a + value(r, k.key), 0)));
        // Counts get whole-number ticks: the top is a multiple of the 4 grid steps.
        const max = whole ? Math.max(4, Math.ceil(top / 4) * 4, niceMax(top) % 4 ? 0 : niceMax(top)) : niceMax(top);
        const y = (v) => T + ph - (v / max) * ph;
        const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-hidden": "true" });
        for (let i = 0; i <= 4; i++) {
          const v = (max / 4) * i;
          svg.append(s("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }),
            s("text", { x: L - 6, y: y(v) + 4, class: "tick", "text-anchor": "end" }, fmt(v, true)));
        }
        const every = Math.max(1, Math.ceil(64 / band));
        const hover = s("rect", { class: "band-hover", y: T, height: ph, width: band, visibility: "hidden" });
        svg.append(hover);
        rows.forEach((r, i) => {
          const x = L + i * band + (band - bw) / 2;
          let base = 0;
          const live = series.filter((k) => value(r, k.key) > 0);
          live.forEach((k, j) => {
            const v = value(r, k.key);
            const top = y(base + v), bottom = y(base);
            const gap = j > 0 ? 2 : 0;   // surface gap between stacked segments
            const hgt = Math.max(0, bottom - top - gap);
            if (hgt > 0) svg.append(s("path", { d: column(x, top, bw, hgt, j === live.length - 1), fill: k.color }));
            base += v;
          });
          if (i % every === 0 || i === rows.length - 1 && rows.length <= 12) {
            svg.append(s("text", { x: L + i * band + band / 2, y: H - 8, class: "tick", "text-anchor": "middle" }, periodLabel(r.start)));
          }
          const hit = s("rect", { x: L + i * band, y: T, width: band, height: ph, fill: "transparent" });
          hit.addEventListener("mouseenter", () => {
            hover.setAttribute("x", L + i * band);
            hover.setAttribute("visibility", "visible");
            fill(tipEl, h("strong", {}, periodLabel(r.start, true)), tip(r));
            tipEl.hidden = false;
            const left = Math.min(Math.max(0, L + i * band + band / 2 - tipEl.offsetWidth / 2), W - tipEl.offsetWidth);
            tipEl.style.left = `${left}px`;
          });
          hit.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); tipEl.hidden = true; });
          svg.append(hit);
        });
        svg.append(s("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), class: "axis" }));
        wrap.querySelector("svg")?.remove();
        wrap.prepend(svg);
      };
      requestAnimationFrame(draw);
      wrap._draw = draw;
      return wrap;
    }
    window.addEventListener("resize", () => {
      if (currentTab !== "analytics") return;
      clearTimeout(window._anResize);
      window._anResize = setTimeout(() => document.querySelectorAll("#tab-analytics .chart").forEach((c) => c._draw && c._draw()), 150);
    });

    function legend(series) {
      if (series.length < 2) return null;
      return h("div", { class: "legend" }, series.map((k) =>
        h("span", { class: "legend-item" }, h("span", { class: "swatch", style: `background:${k.color}` }), k.label)));
    }
    function tableView(head, rows) {
      return h("details", { class: "table-view" }, h("summary", {}, "Show as a table"),
        h("div", { class: "table-scroll" }, h("table", { class: "data" },
          h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
          h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c))))))));
    }
    const per = () => ({ hour: "hour", day: "day", month: "month" }[data.bucket]);

    function requestsCard() {
      const rows = data.rows;
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, `Requests per ${per()}`),
          h("span", { class: "hint" }, "A request is one conversation, counted when it started.")),
        legend(OUTCOMES),
        chart({
          series: OUTCOMES, value: (r, k) => r[k], whole: true,
          fmt: (v) => num(v),
          tip: (r) => [h("div", {}, `${num(r.requests)} requests · ${num(r.turns)} messages${r.emergencies ? ` · ${num(r.emergencies)} sent to 911` : ""}`),
            OUTCOMES.map((k) => h("div", { class: "tip-row" }, h("span", { class: "swatch", style: `background:${k.color}` }), `${k.label}: ${num(r[k.key])}`))],
        }),
        tableView([per()[0].toUpperCase() + per().slice(1), "Requests", ...OUTCOMES.map((k) => k.label), "Sent to 911", "Messages"],
          rows.map((r) => [periodLabel(r.start, true), num(r.requests), ...OUTCOMES.map((k) => num(r[k.key])), num(r.emergencies), num(r.turns)])));
    }

    function costCard() {
      const series = [{ key: "total_cost", label: "Running cost", color: "var(--series-7)" }];
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, `Running cost per ${per()}`),
          h("span", { class: "hint" }, "Model spend + hosting + staff time on handoffs.")),
        chart({
          series, value: (r) => r.total_cost,
          fmt: (v) => cur(v),
          tip: (r) => [h("div", {}, `Total ${cur(r.total_cost)} · by hand ${cur(r.manual_cost)}`),
            h("div", { class: "tip-row" }, `Model ${cur(r.model_cost)} · hosting ${cur(r.hosting_cost)} · staff review ${cur(r.review_cost)}`)],
        }),
        tableView([per()[0].toUpperCase() + per().slice(1), "Model", "Hosting", "Staff review", "Total", "By hand"],
          data.rows.map((r) => [periodLabel(r.start, true), cur(r.model_cost), cur(r.hosting_cost), cur(r.review_cost), cur(r.total_cost), cur(r.manual_cost)])));
    }

    function simpleTable(head, rows, empty) {
      if (!rows.length) return h("p", { class: "hint" }, empty);
      return h("div", { class: "table-scroll" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
        h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c)))))));
    }

    function breakdown() {
      const t = data.totals, c = data.costs;
      const rate = c.labour_rate_per_hour;
      const p = data.problems;
      return h("div", { class: "an-grid" },
        h("section", { class: "card" }, h("h3", {}, "Where the cost goes"),
          simpleTable(["", "Amount", "Per request"], [
            ["Model calls", cur(t.model_cost), cur(t.requests ? t.model_cost / t.requests : null)],
            ["Hosting", cur(t.hosting_cost), cur(t.requests ? t.hosting_cost / t.requests : null)],
            [`Staff review (${num(t.handoffs)} handoff${t.handoffs === 1 ? "" : "s"} × ${num(c.review_time_s)} s)`, cur(t.review_cost), cur(t.requests ? t.review_cost / t.requests : null)],
            [h("strong", {}, "Running cost"), h("strong", {}, cur(t.total_cost)), h("strong", {}, cur(t.cost_per_request))],
            [`By hand (${num(c.manual_handle_time_s)} s at ${cur(rate)}/h)`, cur(t.manual_cost), cur(t.manual_per_request)],
          ], ""),
          h("p", { class: "hint" }, `Cost per ticket filed: ${cur(t.cost_per_ticket)}. G9 in the metrics doc also divides by the intake success rate, which only the eval harness measures.`)),
        h("section", { class: "card" }, h("h3", {}, "Models"),
          simpleTable(["Model", "Calls", "Errors", "Tokens in", "Tokens out", "Cost"],
            data.models.map((m) => [h("span", { class: "mono" }, m.model), num(m.calls), num(m.errors), num(m.tokens_in), num(m.tokens_out), money(m.cost, "USD")]),
            "No model calls in this period."),
          p.unpriced_calls ? h("p", { class: "hint" }, `${num(p.unpriced_calls)} calls came back without a price and count as free.`) : null),
        h("section", { class: "card" }, h("h3", {}, "Why requests were handed off"),
          simpleTable(["Rule", "Handoffs", ""],
            data.handoff_rules.map((r) => [ruleChip(r.rule_id), num(r.count), short(r.text || "", 90)]),
            "No handoffs in this period.")),
        h("section", { class: "card" }, h("h3", {}, "Tickets by service"),
          simpleTable(["Code", "Service", "Tickets"],
            data.codes.map((r) => [h("span", { class: "mono" }, r.code), r.category || "", num(r.count)]),
            "No tickets in this period.")),
        h("section", { class: "card" }, h("h3", {}, "Safety and problems"),
          simpleTable(["", "Count"], [
            ["Emergencies sent to 911 (E1)", num(data.totals.emergencies)],
            ["Model errors (F1)", num(p.model_errors)],
            ["Tool failures (F5)", num(p.tool_failures)],
            ["Slowest reply", secs(data.latency.max_ms)],
          ], "")));
    }

    // ---------- cost assumptions (saved to app.yaml in Supabase) ----------
    function assumptions() {
      const c = data.costs;
      const inputs = {};
      const msg = h("span", { class: "hint" }, costsSaved);
      const save = h("button", { class: "btn primary" }, "Save assumptions");
      const fields = Object.keys(COST_LABELS).map((k) => {
        inputs[k] = h("input", k === "currency"
          ? { value: c[k], maxlength: "8", "aria-label": COST_LABELS[k] }
          : { type: "number", min: "0", step: "any", value: String(c[k]), "aria-label": COST_LABELS[k] });
        return h("label", { class: "cost-field" },
          h("span", { class: "cost-label" }, COST_LABELS[k], data.cost_defaults.includes(k) ? h("span", { class: "chip" }, "default") : null),
          inputs[k],
          h("span", { class: "hint" }, data.cost_notes[k] || ""));
      });
      save.addEventListener("click", async () => {
        const costs = {};
        for (const [k, el] of Object.entries(inputs)) costs[k] = k === "currency" ? el.value.trim() : Number(el.value);
        save.disabled = true;
        msg.className = "hint";
        msg.textContent = "Saving…";
        try {
          await api("/costs", { method: "PUT", body: JSON.stringify({ costs, author: who() }) });
          costsSaved = `Saved to app.yaml at ${clock(new Date().toISOString())}.`;
          msg.textContent = costsSaved;
          costsShown = "";
          sig = "";
          await load();
        } catch (e) {
          msg.className = "hint error-text";
          msg.textContent = (e.errors || [e.message]).join(" ");
        } finally {
          save.disabled = false;
        }
      });
      return h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, "Cost assumptions"),
          h("span", { class: "hint" }, "Stored in app.yaml (costs) in Supabase, with history in the Files tab.")),
        h("div", { class: "cost-fields" }, fields),
        h("div", { class: "ed-actions" }, save, msg));
    }

    function render() {
      if (!data.totals.requests && !data.models.length) {
        fill($("an-body"), tiles(), h("div", { class: "empty-state" }, "No conversations in this period yet. Try a longer range, or chat on the site to see it here."));
      } else {
        fill($("an-body"), tiles(), h("div", { class: "an-charts" }, requestsCard(), costCard()), breakdown());
      }
      // Don't redraw the form under someone typing; refresh it only when the saved values change.
      const shown = JSON.stringify([data.costs, data.cost_defaults]);
      if (shown !== costsShown) {
        costsShown = shown;
        fill($("an-costs"), assumptions());
      }
    }

    return {
      start() { toolbar(); sig = ""; poll.start(); },
      stop() { poll.stop(); },
    };
  })();

  // ---------- boot ----------
  (async () => {
    try {
      const cfg = await fetch("/api/config").then((r) => r.json());
      $("site-title").textContent = cfg.site.title;
      document.title = `${cfg.site.title} · Admin`;
      await loadRef();
    } catch (e) { console.warn(e); }
    showTab(location.hash.slice(1));
  })();
})();
