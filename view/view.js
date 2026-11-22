// Match view: connects to the harness channel and draws what it receives.
//
// The graph arrives inside the match-opened event (row.graph) and is drawn from there: nodes
// on a circle in file order, edges as oriented curves. A delivered message is a dot that runs
// along the edge from → to and stays still, lit, on the recipient node; the receipt dims it
// but does not remove it. A write to a source is the same dot, green, that runs toward the
// source and carries data-outcome="written": nobody reads it, so it stays lit. A rejection is
// a still mark on the sender node: it has no edge to run on. Every dot and every mark carries
// the raw event in the data-event attribute, and the click shows it as it arrived. A spawn
// and a graph-changed apply their delta and recompute the circle. The page writes nothing to
// the harness.

"use strict";

const NS = "http://www.w3.org/2000/svg";
const CENTER = { x: 500, y: 350 };
const CIRCLE_RADIUS = 260;
const NODE_RADIUS = 34;
const CURVATURE = 0.16;
const RUN_DURATION_S = 0.9;
const REJECTION_MARK_STEP = 26;
const MAX_LOG_ENTRIES = 400;

const state = {
  match: null,
  graph: null,
  nodes: new Map(),      // id → { entry, element, x, y, events, rejections }
  edges: new Map(),      // "from→to" → { entry, element, path, events }
  dots: new Map(),       // message id → element of the delivered dot
  selected: null,
  edgeCounter: 0,
};

const layers = {
  edges: document.getElementById("layer-edges"),
  nodes: document.getElementById("layer-nodes"),
  dots: document.getElementById("layer-dots"),
};

function svgElement(tag, attributes = {}) {
  const element = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  return element;
}

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function edgeKey(from, to) {
  return `${from}→${to}`;
}

// <editor-fold desc="Detail and log">
function show(title, payload, element) {
  if (state.selected) state.selected.classList.remove("selected");
  state.selected = element || null;
  if (element) element.classList.add("selected");
  setText("detail-title", title);
  setText("detail-json", JSON.stringify(payload, null, 2));
}

function describe(event) {
  const row = event.row || {};
  if (event.class === "message") {
    const reason = row.reason ? ` · ${row.reason}` : "";
    return `${event.type} ${row.from ?? "?"} → ${row.to ?? "?"}${reason}`;
  }
  if (event.class === "spawn") return `spawn ${event.parent} → ${event.child.id} · ${event.template}`;
  if (event.type === "graph-changed") {
    const { version, nodes_added: nodes, edges_added: added, edges_removed: removed } = event.delta;
    return `graph-changed v${version} · +${nodes.length} nodes · +${added.length} −${removed.length} edges`;
  }
  if (event.node) return `${event.type} ${event.node}`;
  return `${event.type} ${event.match}`;
}

function logEvent(event) {
  const list = document.getElementById("event-log");
  const entry = document.createElement("li");
  if (event.type === "rejected") entry.classList.add("rejected");
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = (event.ts.split("T")[1] || "").slice(0, 12);
  entry.append(time, describe(event));
  entry.addEventListener("click", () => show(`event · ${event.class}/${event.type}`, event));
  list.prepend(entry);
  while (list.children.length > MAX_LOG_ENTRIES) list.lastElementChild.remove();
}
// </editor-fold>

// <editor-fold desc="Graph drawing">
function positions() {
  const nodes = state.graph.nodes;
  nodes.forEach((entry, index) => {
    const node = state.nodes.get(entry.id);
    if (nodes.length === 1) {
      node.x = CENTER.x;
      node.y = CENTER.y;
      return;
    }
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / nodes.length;
    node.x = CENTER.x + CIRCLE_RADIUS * Math.cos(angle);
    node.y = CENTER.y + CIRCLE_RADIUS * Math.sin(angle);
  });
}

function unitVector(dx, dy) {
  const length = Math.hypot(dx, dy) || 1;
  return { x: dx / length, y: dy / length };
}

// Quadratic curve from border to border, bent to the left of the direction: A → B and B → A do not overlap.
// null if an endpoint is missing or if the two nodes coincide.
function edgePoints(from, to) {
  const origin = state.nodes.get(from);
  const destination = state.nodes.get(to);
  if (!origin || !destination) return null;
  const dx = destination.x - origin.x;
  const dy = destination.y - origin.y;
  const length = Math.hypot(dx, dy);
  const control = {
    x: (origin.x + destination.x) / 2 - dy * CURVATURE,
    y: (origin.y + destination.y) / 2 + dx * CURVATURE,
  };
  if (length === 0) return null;
  const outward = unitVector(control.x - origin.x, control.y - origin.y);
  const inward = unitVector(control.x - destination.x, control.y - destination.y);
  const start = { x: origin.x + outward.x * NODE_RADIUS, y: origin.y + outward.y * NODE_RADIUS };
  const end = { x: destination.x + inward.x * (NODE_RADIUS + 3), y: destination.y + inward.y * (NODE_RADIUS + 3) };
  return { start, control, end };
}

function edgeGeometry(from, to) {
  const points = edgePoints(from, to);
  if (!points) return "";
  const { start, control, end } = points;
  return `M ${start.x.toFixed(1)} ${start.y.toFixed(1)} Q ${control.x.toFixed(1)} ${control.y.toFixed(1)} ${end.x.toFixed(1)} ${end.y.toFixed(1)}`;
}

function rejectionPosition(node, index) {
  const direction = unitVector(node.x - CENTER.x, node.y - CENTER.y);
  const outside = direction.x === 0 && direction.y === 0 ? { x: 0, y: -1 } : direction;
  const distance = NODE_RADIUS + 18 + index * REJECTION_MARK_STEP;
  return { x: node.x + outside.x * distance, y: node.y + outside.y * distance };
}

function updateGeometry() {
  positions();
  for (const node of state.nodes.values()) {
    node.element.setAttribute("transform", `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`);
    node.rejections.forEach((mark, index) => {
      const point = rejectionPosition(node, index);
      mark.setAttribute("transform", `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`);
    });
  }
  for (const edge of state.edges.values()) {
    const shape = edgeGeometry(edge.entry.from, edge.entry.to);
    for (const path of edge.element.querySelectorAll("path")) path.setAttribute("d", shape);
  }
}

function drawNode(entry) {
  const group = svgElement("g", { class: `node type-${entry.type}`, "data-node": entry.id });
  group.append(svgElement("circle", { r: NODE_RADIUS }));
  const label = svgElement("text", { class: "label" });
  label.textContent = entry.id;
  // A long id stays inside the circle: the font size shrinks with the length.
  label.style.fontSize = `${entry.id.length <= 4 ? 18 : entry.id.length <= 6 ? 15 : entry.id.length <= 8 ? 13 : 11}px`;
  const role = svgElement("text", { class: "role", y: NODE_RADIUS + 18 });
  role.textContent = entry.role || entry.type;
  group.append(label, role);
  const node = { entry, element: group, x: 0, y: 0, events: [], rejections: [] };
  group.addEventListener("click", () => show(`node · ${entry.id}`, { node: entry, events: node.events }, group));
  layers.nodes.append(group);
  state.nodes.set(entry.id, node);
}

function drawEdge(entry) {
  const key = edgeKey(entry.from, entry.to);
  const group = svgElement("g", { "data-edge": key });
  const path = svgElement("path", { class: "edge", id: `edge-${++state.edgeCounter}`, "marker-end": "url(#arrow)" });
  const hitArea = svgElement("path", { class: "edge-hit" });
  group.append(path, hitArea);
  const edge = { entry, element: group, path, events: [], dots: [] };
  group.addEventListener("click", () => show(`edge · ${key}`, { edge: entry, messages: edge.events }, group));
  layers.edges.append(group);
  state.edges.set(key, edge);
}

function versionText() {
  setText("version", `v${state.graph.version} · ${state.graph.nodes.length} nodes · ${state.graph.edges.length} edges`);
}

function newMatch(event) {
  for (const layer of Object.values(layers)) layer.replaceChildren();
  state.match = event.match;
  state.graph = structuredClone(event.row.graph);
  state.nodes.clear();
  state.edges.clear();
  state.dots.clear();
  state.selected = null;
  state.graph.nodes.forEach(drawNode);
  state.graph.edges.forEach(drawEdge);
  updateGeometry();
  const meta = event.row.meta;
  setText("match", event.match);
  setText("game", [meta.game, meta.rule].filter(Boolean).join(" · "));
  versionText();
  setText("match-status", "open");
  document.body.dataset.match = "open";
}

// The spawn carries the graph delta: the child and all the edges the template gives it by
// role, not only parent → child. The child joins the circle and the layout is recomputed.
function applySpawn(event) {
  for (const entry of event.delta.nodes) {
    if (state.nodes.has(entry.id)) continue;
    state.graph.nodes.push(entry);
    drawNode(entry);
  }
  for (const entry of event.delta.edges) {
    if (state.edges.has(edgeKey(entry.from, entry.to))) continue;
    state.graph.edges.push(entry);
    drawEdge(entry);
  }
  state.graph.version = event.delta.version;
  updateGeometry();
  versionText();
  state.nodes.get(event.parent)?.events.push(event);
  const child = state.nodes.get(event.child.id);
  if (child) {
    child.events.push(event);
    child.element.classList.add("spawned");
  }
}

// A graph change written by the director while the match is open: the view applies the event delta.
// The dots that already ran on a removed edge stay lit on their recipient: the animation follows the
// edge path, so it goes away with the edge, and the dot stops at the arrival point.
function applyGraphChanged(event) {
  const delta = event.delta;
  const orphans = [];
  for (const entry of delta.edges_removed) {
    const key = edgeKey(entry.from, entry.to);
    const edge = state.edges.get(key);
    if (!edge) continue;
    const arrival = edgePoints(entry.from, entry.to)?.end;
    orphans.push(...edge.dots.map((dot) => ({ dot, entry, arrival })));
    edge.element.remove();
    state.edges.delete(key);
  }
  for (const entry of delta.nodes_removed) {
    state.nodes.get(entry.id)?.element.remove();
    state.nodes.delete(entry.id);
  }
  const removedEdges = new Set(delta.edges_removed.map((entry) => edgeKey(entry.from, entry.to)));
  const removedNodes = new Set(delta.nodes_removed.map((entry) => entry.id));
  state.graph.edges = state.graph.edges.filter((entry) => !removedEdges.has(edgeKey(entry.from, entry.to)));
  state.graph.nodes = state.graph.nodes.filter((entry) => !removedNodes.has(entry.id));
  for (const entry of delta.nodes_added) {
    state.graph.nodes.push(entry);
    drawNode(entry);
    const node = state.nodes.get(entry.id);
    node.events.push(event);
    node.element.classList.add("joined");
  }
  for (const entry of delta.edges_added) {
    state.graph.edges.push(entry);
    drawEdge(entry);
  }
  state.graph.version = delta.version;
  updateGeometry();
  versionText();
  for (const { dot, entry, arrival } of orphans) {
    const point = edgePoints(entry.from, entry.to)?.end ?? arrival;
    dot.querySelector("animateMotion")?.remove();
    if (point) dot.setAttribute("transform", `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`);
    dot.setAttribute("data-arrived", "");
  }
}
// </editor-fold>

// <editor-fold desc="Messages: dots and rejections">
function deliveredDot(event) {
  const row = event.row;
  const edge = state.edges.get(edgeKey(row.from, row.to));
  const dot = svgElement("circle", {
    class: event.type === "written" ? "dot written" : "dot",
    r: 7,
    "data-dot": `${event.match}:${event.index}`,
    "data-outcome": event.type,
    "data-event": JSON.stringify(event),
  });
  const kind = event.type === "written" ? "write" : "message";
  dot.addEventListener("click", () => show(`${kind} · ${row.from} → ${row.to}`, JSON.parse(dot.dataset.event), dot));
  layers.dots.append(dot);
  if (row.id) state.dots.set(row.id, dot);
  if (!edge) {
    // The bus does not deliver outside the graph: a delivered message without an edge means a graph different from the drawn one.
    const destination = state.nodes.get(row.to);
    dot.classList.add("off-edge");
    if (destination) dot.setAttribute("transform", `translate(${destination.x} ${destination.y})`);
    dot.setAttribute("data-arrived", "");
    return;
  }
  edge.events.push(event);
  edge.dots.push(dot);
  const run = svgElement("animateMotion", { dur: `${RUN_DURATION_S}s`, fill: "freeze", begin: "indefinite", calcMode: "paced" });
  const track = svgElement("mpath", { href: `#${edge.path.id}` });
  run.append(track);
  run.addEventListener("endEvent", () => dot.setAttribute("data-arrived", ""));
  dot.append(run);
  run.beginElement();
}

function rejectionMark(event) {
  const row = event.row;
  const sender = state.nodes.get(row.from);
  const mark = svgElement("g", {
    class: "rejection",
    "data-dot": `${event.match}:${event.index}`,
    "data-outcome": "rejected",
    "data-event": JSON.stringify(event),
  });
  mark.append(
    svgElement("circle", { r: 10 }),
    svgElement("line", { x1: -5, y1: -5, x2: 5, y2: 5 }),
    svgElement("line", { x1: -5, y1: 5, x2: 5, y2: -5 }),
  );
  mark.addEventListener("click", () => show(`rejection · ${row.reason}`, JSON.parse(mark.dataset.event), mark));
  layers.dots.append(mark);
  if (sender) {
    sender.rejections.push(mark);
    sender.events.push(event);
    const point = rejectionPosition(sender, sender.rejections.length - 1);
    mark.setAttribute("transform", `translate(${point.x.toFixed(1)} ${point.y.toFixed(1)})`);
  } else {
    mark.setAttribute("transform", "translate(40 40)");
  }
}

function receipt(event) {
  const dot = state.dots.get(event.row.id);
  if (dot) {
    dot.classList.add("dimmed");
    dot.setAttribute("data-received", "");
  }
  const recipient = state.nodes.get(event.row.to);
  if (recipient) recipient.events.push(event);
}
// </editor-fold>

function handle(event) {
  logEvent(event);
  if (event.type === "match-opened") {
    newMatch(event);
    return;
  }
  if (event.match !== state.match) return;
  if (event.class === "spawn") {
    applySpawn(event);
    return;
  }
  if (event.type === "graph-changed") {
    applyGraphChanged(event);
    return;
  }
  if (event.class === "message") {
    if (event.type === "delivered" || event.type === "written") deliveredDot(event);
    else if (event.type === "rejected") rejectionMark(event);
    else if (event.type === "received") receipt(event);
    return;
  }
  if (event.type === "node-launched" || event.type === "node-closed") {
    const node = state.nodes.get(event.node);
    if (!node) return;
    node.events.push(event);
    node.element.classList.add(event.type === "node-launched" ? "launched" : "closed");
    return;
  }
  if (event.type === "match-closed") {
    setText("match-status", `closed · outcome ${event.row.outcome ?? "—"}`);
    document.body.dataset.match = "closed";
  }
}

async function connect() {
  try {
    const channel = await (await fetch("/channel.json")).json();
    const speed = channel.speed === null ? "" : channel.speed === 0 ? " · no waits" : ` · ×${channel.speed}`;
    setText("channel", channel.mode + speed);
  } catch (error) {
    setText("channel", "unknown: open the view from the harness address");
  }
  const socket = new WebSocket(`ws://${location.host}/events`);
  socket.addEventListener("open", () => {
    document.body.dataset.status = "open";
    setText("connection", "open");
  });
  socket.addEventListener("message", (message) => handle(JSON.parse(message.data)));
  socket.addEventListener("close", (closure) => {
    document.body.dataset.status = "finished";
    setText("connection", `closed${closure.reason ? " · " + closure.reason : ""}`);
  });
}

connect();
