// Loop view: connects to bin/loop-view.py and draws the state of the loop as the files on disk say it.
//
// The server sends a `state` message every time the state on disk changes, and `log` messages with
// the rows of registry/loop.jsonl. The page draws the org chart from the positions of the loop's
// status, as a tree from the root down: for every position its switch (switches.json), its figures,
// its live sessions (sessions.json), the messages still in its mailbox (loop/mailboxes/<position>/),
// and its closed sessions folded. On the side: the interpellations, read only; the requests the
// loop has not decided; the detail of what is clicked; the log, newest first.
//
// The click on a switch is the only write: a POST /switch that the server deposits as a request of
// the human. The page never changes a switch by itself: the switch turns when switches.json turns.
// Every text that comes from a session goes into the DOM as text, never as markup.

"use strict";

const MAX_LOG_ENTRIES = 500;
const RECONNECT_MS = 2000;
const TEXT_PREVIEW = 90;

const view = {
  state: null,
  structure: null,
  records: new Map(),       // "kind:id" → record shown in the detail
  selected: null,           // "kind:id" of the selected element
  pendingSwitches: new Set(),
  logCounter: 0,
};

function element(tag, attributes = {}, text) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value !== undefined && value !== null) node.setAttribute(name, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function clock(ts) {
  return ts ? ts.slice(11, 19) : "—";
}

function preview(text) {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > TEXT_PREVIEW ? `${flat.slice(0, TEXT_PREVIEW - 1)}…` : flat;
}

// <editor-fold desc="Detail and selection">
function remember(kind, id, record, node) {
  const key = `${kind}:${id}`;
  view.records.set(key, record);
  node.dataset.key = key;
  if (view.selected === key) node.classList.add("selected");
}

function show(node) {
  const key = node.dataset.key;
  const record = node.record || view.records.get(key);
  if (!record) return;
  document.querySelectorAll(".selected").forEach((selected) => selected.classList.remove("selected"));
  node.classList.add("selected");
  view.selected = key;
  setText("detail-title", key.replace(":", " · "));
  setText("detail-json", JSON.stringify(record, null, 2));
}
// </editor-fold>

// <editor-fold desc="Header">
function loopStatus(state) {
  if (!state.loop) return "absent";
  return state.loop.alive ? "running" : "down";
}

function renderHeader(state) {
  const status = loopStatus(state);
  document.body.dataset.loop = status;
  if (status === "absent") setText("loop", "absent · no status file");
  else if (status === "running") setText("loop", `running · pid ${state.loop.pid} · since ${clock(state.loop.started)}`);
  else setText("loop", `down · last pid ${state.loop.pid}`);
  const alive = state.sessions.filter((session) => session.status === "alive").length;
  setText("positions-count", String(Object.keys(state.positions).length));
  setText("sessions-count", String(alive));
  setText("state-root", state.state_root);
  renderHeartbeat();
}

function renderHeartbeat() {
  const heartbeat = view.state?.loop?.heartbeat;
  if (!heartbeat) {
    setText("heartbeat", "—");
    return;
  }
  const age = Math.max(0, (Date.now() - Date.parse(heartbeat)) / 1000);
  setText("heartbeat", `${clock(heartbeat)} · ${age < 10 ? age.toFixed(1) : Math.round(age)} s ago`);
}
// </editor-fold>

// <editor-fold desc="Org chart">
function orderPositions(positions) {
  const ids = Object.keys(positions).sort();
  const children = new Map();
  for (const id of ids) {
    const parent = positions[id].parent;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(id);
  }
  const ordered = [];
  const seen = new Set();
  const visit = (id, depth) => {
    if (seen.has(id)) return;
    seen.add(id);
    ordered.push([id, depth]);
    for (const child of children.get(id) || []) visit(child, depth + 1);
  };
  for (const root of children.get("human") || []) visit(root, 0);
  for (const id of ids) visit(id, 0);
  return ordered;
}

function figure(label, value, extra) {
  const wrapper = element("div");
  wrapper.append(element("dt", {}, label), element("dd", {}, value));
  if (extra) wrapper.append(extra);
  return wrapper;
}

function switchButton(id, on, loopRunning) {
  const button = element("button", {
    type: "button", class: "switch", role: "switch", "data-switch": id,
    "aria-checked": String(on), "aria-label": `switch of ${id}`,
  }, on ? "on" : "off");
  if (view.pendingSwitches.has(id)) button.setAttribute("aria-busy", "true");
  if (!loopRunning) {
    button.disabled = true;
    button.title = "the loop is not running: it starts in neutral and rejects the switches deposited before";
  }
  return button;
}

function sessionItem(session) {
  const by = session.launched_by?.session || session.launched_by?.kind || "?";
  const parts = session.status === "alive"
    ? [session.id, session.type, `pid ${session.pid}`, `by ${by}`, session.reason]
    : [session.id, session.close_reason, `closed ${clock(session.closed)}`];
  if (session.status === "alive" && session.wakes) parts.push(`wakes ${session.wakes}`);
  if (session.process === "gone") parts.push("process gone");
  const item = element("li", {
    "data-session": session.id, "data-status": session.status, "data-type": session.type,
    "data-process": session.process,
  }, parts.filter(Boolean).join(" · "));
  remember("session", session.id, session, item);
  return item;
}

function messageItem(message) {
  const item = element("li", {
    "data-message": message.id, "data-to": message.to_session || "", "data-kind": message.kind || "message",
  }, `${message.kind || "message"} · ${message.from} → ${message.to_session || "unassigned"} · ${preview(message.text)}`);
  remember("message", message.id, message, item);
  return item;
}

function list(items, emptyText) {
  if (!items.length) return element("p", { class: "empty" }, emptyText);
  const container = element("ul", { class: "items" });
  container.append(...items);
  return container;
}

function positionCard(state, id, depth) {
  const figures = state.positions[id];
  const switchEntry = state.switches[id] || {};
  const on = switchEntry.on === true;
  const sessions = state.sessions.filter((session) => session.position === id)
    .sort((a, b) => (a.launched || "").localeCompare(b.launched || ""));
  const alive = sessions.filter((session) => session.status === "alive");
  const closed = sessions.filter((session) => session.status !== "alive");
  const mailbox = state.mailboxes[id] || [];
  const queued = state.requests.filter((request) => request.position === id && request.queued).length;
  const tokens = figures.tokens;
  const budget = figures.budget_tokens;

  const card = element("section", { class: "position", "data-position": id, "data-on": String(on), "data-depth": String(depth) });
  card.style.setProperty("--depth", depth);
  if (typeof tokens === "number" && tokens > budget) card.classList.add("over-budget");

  const header = element("header");
  header.append(switchButton(id, on, loopStatus(state) === "running"), element("h3", {}, id),
    element("span", { class: "parent" }, `parent ${figures.parent}`));
  if (switchEntry.since) {
    header.append(element("span", { class: "since", "data-switch-reason": switchEntry.reason },
      `${on ? "on" : "off"} · ${switchEntry.reason} · ${clock(switchEntry.since)}`));
  }
  card.append(header);
  if (figures.description) card.append(element("p", { class: "description" }, figures.description));

  const numbers = element("dl", { class: "figures" });
  const meter = typeof tokens === "number" ? element("meter", { min: "0", max: String(budget), value: String(tokens), high: String(budget * 0.8) }) : null;
  numbers.append(
    figure("sessions", `${alive.length}/${figures.max_sessions}`),
    figure("tokens", `${typeof tokens === "number" ? tokens : "—"}/${budget}`, meter),
    figure("queued", String(queued)),
    figure("mailbox", String(mailbox.length)),
    figure("read", String(state.received[id] || 0)),
  );
  if (figures.launch_type) numbers.append(figure("launch", figures.launch_type));
  card.append(numbers);

  card.append(element("h4", {}, "live sessions"), list(alive.map(sessionItem), "none"));
  card.append(element("h4", {}, "mailbox"), list(mailbox.map(messageItem), "empty"));
  if (closed.length) {
    const folded = element("details");
    folded.append(element("summary", {}, `closed sessions (${closed.length})`), list(closed.map(sessionItem), ""));
    card.append(folded);
  }
  return card;
}

function renderPositions(state) {
  const container = document.getElementById("positions");
  const open = new Set([...container.querySelectorAll("details[open]")].map((details) => details.closest("[data-position]")?.dataset.position));
  const cards = orderPositions(state.positions).map(([id, depth]) => positionCard(state, id, depth));
  container.replaceChildren(...cards);
  for (const card of cards) {
    if (open.has(card.dataset.position)) card.querySelector("details")?.setAttribute("open", "");
  }
  let note = "";
  if (state.positions_source === "grants") note = "The loop has not written its status yet: positions from the grant files, not validated.";
  else if (loopStatus(state) === "down") note = "The loop is down: figures are its last status; at its next start every switch is off.";
  setText("positions-note", note);
}
// </editor-fold>

// <editor-fold desc="Interpellations and requests">
function interpellationCard(entry) {
  const card = element("div", { class: "interpellation", "data-interpellation": entry.id, "data-status": entry.status });
  card.append(element("div", { class: "meta" }, `${entry.position} · ${entry.from} · ${clock(entry.ts)} · ${entry.status}`));
  if (entry.text) card.append(element("p", { class: "text" }, entry.text));
  const fields = element("dl");
  const add = (label, field, value) => {
    if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return;
    const definition = element("dd", { "data-field": field });
    if (Array.isArray(value)) {
      const items = element("ol");
      items.append(...value.map((alternative) => element("li", { "data-alternative": "" }, alternative)));
      definition.append(items);
    } else {
      definition.textContent = value;
    }
    fields.append(element("dt", {}, label), definition);
  };
  add("blocked", "blocked", entry.blocked);
  add("alternatives", "alternatives", entry.alternatives);
  add("evidence", "evidence", entry.evidence);
  add("recommendation", "recommendation", entry.recommendation);
  add("waiting cost", "waiting_cost", entry.waiting_cost);
  add("default", "default", entry.default);
  add("deadline", "deadline", entry.deadline);
  card.append(fields);
  remember("interpellation", entry.id, entry, card);
  return card;
}

function renderInterpellations(state) {
  const container = document.getElementById("interpellations");
  const entries = [...state.interpellations].sort((a, b) =>
    (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || (b.ts || "").localeCompare(a.ts || ""));
  if (!entries.length) {
    container.replaceChildren(element("p", { class: "empty" }, "none"));
    return;
  }
  container.replaceChildren(...entries.map(interpellationCard));
}

function renderRequests(state) {
  const items = state.requests.map((request) => {
    const outcome = request.queued ? `queued · ${request.queued.reason}` : "not yet decided";
    const item = element("li", { "data-request": request.id, "data-kind": request.kind, "data-queued": request.queued ? request.queued.reason : "" },
      `${request.kind} ${request.position ?? ""} · from ${request.from} · ${outcome}`);
    remember("request", request.id, request, item);
    return item;
  });
  const container = document.getElementById("requests");
  if (!items.length) container.replaceChildren(element("li", { class: "empty" }, "none"));
  else container.replaceChildren(...items);
}
// </editor-fold>

// <editor-fold desc="Log">
function logItem(row) {
  view.logCounter += 1;
  const words = [row.event, row.position, row.session, row.from && row.from !== row.session ? `from ${row.from}` : null,
    row.reason, row.detail].filter(Boolean);
  const item = element("li", { "data-log-row": String(view.logCounter), "data-event": row.event });
  item.append(element("span", { class: "time" }, clock(row.ts)), document.createTextNode(words.join(" · ")));
  item.record = row;
  item.dataset.key = `log:${view.logCounter}`;
  return item;
}

function applyLog(message) {
  const container = document.getElementById("log");
  if (message.reset) {
    container.replaceChildren();
    view.logCounter = 0;
  }
  for (const row of message.rows) container.prepend(logItem(row));
  while (container.children.length > MAX_LOG_ENTRIES) container.lastElementChild.remove();
}
// </editor-fold>

function applyState(state) {
  view.state = state;
  const structure = JSON.stringify({ ...state, loop: state.loop && { ...state.loop, heartbeat: null } });
  renderHeader(state);
  if (structure === view.structure) return;
  view.structure = structure;
  view.records.clear();
  renderPositions(state);
  renderInterpellations(state);
  renderRequests(state);
  document.body.dataset.rendered = String(Number(document.body.dataset.rendered || 0) + 1);
}

// <editor-fold desc="The click on a switch">
async function toggle(button) {
  const position = button.dataset.switch;
  if (view.pendingSwitches.has(position) || button.disabled) return;
  const on = button.getAttribute("aria-checked") !== "true";
  view.pendingSwitches.add(position);
  button.setAttribute("aria-busy", "true");
  const line = document.getElementById("last-action");
  line.className = "";
  line.textContent = `${position} ${on ? "on" : "off"}: sent`;
  try {
    const response = await fetch(`/switch?position=${encodeURIComponent(position)}&on=${on}`, { method: "POST" });
    const answer = await response.json();
    if (!response.ok) throw new Error(answer.error || response.status);
    line.dataset.lastRequest = answer.request;
    line.textContent = `${position} ${on ? "on" : "off"} · ${answer.request}: ${answer.text}`;
    if (answer.decision?.outcome === "rejected") line.className = "rejected";
  } catch (error) {
    line.className = "rejected";
    line.textContent = `${position} ${on ? "on" : "off"}: not sent — ${error.message}`;
  } finally {
    view.pendingSwitches.delete(position);
    document.querySelector(`[data-switch="${CSS.escape(position)}"]`)?.removeAttribute("aria-busy");
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-switch]");
  if (button) {
    toggle(button);
    return;
  }
  const target = event.target.closest("[data-key]");
  if (target) show(target);
});
// </editor-fold>

function handle(message) {
  if (message.type === "state") applyState(message.state);
  else if (message.type === "log") applyLog(message);
}

function connect() {
  const socket = new WebSocket(`ws://${location.host}/events`);
  socket.addEventListener("open", () => {
    document.body.dataset.connection = "open";
    setText("connection", "open");
  });
  socket.addEventListener("message", (message) => handle(JSON.parse(message.data)));
  socket.addEventListener("close", () => {
    document.body.dataset.connection = "closed";
    setText("connection", "closed · retrying");
    setTimeout(connect, RECONNECT_MS);
  });
}

setInterval(renderHeartbeat, 1000);
connect();
