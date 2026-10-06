// Lupus graph viewer. Renders the exported memory graph with vis-network (vendored, unmodified).
// The layout is a live force simulation: nodes can be dragged freely and their neighbours follow.
// All node text is untrusted data: it is only ever assigned through textContent or drawn on the
// canvas by vis-network, never interpreted as HTML.
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("data").textContent);
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var INK = dark ? "#e6edf3" : "#1f2328", DIM = dark ? "#3a3f47" : "#d8dee4", EDGE = dark ? "#565e69" : "#afb8c1";
  var KIND = {
    project: ["#8b949e", "프로젝트"], goal: ["#58a6ff", "목표"], lesson: ["#d29922", "교훈"],
    decision: ["#a371f7", "결정"], fact: ["#3fb950", "사실"], procedure: ["#f85149", "절차"],
    preference: ["#f0883e", "선호"]
  };
  var STATUS = { verified: "사용자 확인", confirmed: "반복 사용됨", candidate: "미검증", retired: "은퇴" };
  var EDGE_LABEL = {
    supersedes: "대체", derived_from: "파생", contradicts: "상충", part_of: "부분", relates: "관련",
    has_goal: "", recorded_in: "기록", recalled_in: "회상"
  };
  var $ = function (id) { return document.getElementById(id); };
  var byId = {};
  data.nodes.forEach(function (n) { byId[n.id] = n; });

  // ---- persisted view state (positions of pinned nodes, sliders); best effort only
  var KEY = "lupus-graph-v1", saved = {};
  try { saved = JSON.parse(window.localStorage.getItem(KEY) || "{}"); } catch (e) { saved = {}; }
  saved.pins = saved.pins || {};
  function persist() { try { window.localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* file:// may refuse */ } }

  [["", "전체 범위"]].concat(data.scopes).forEach(function (s) {
    var o = document.createElement("option");
    o.value = s[0]; o.textContent = s[1]; $("scope").appendChild(o);
  });
  Object.keys(KIND).forEach(function (k) {
    var s = document.createElement("span"), d = document.createElement("i");
    d.className = "dot"; d.style.background = KIND[k][0];
    s.appendChild(d); s.appendChild(document.createTextNode(KIND[k][1])); $("legend").appendChild(s);
  });
  $("generated").textContent = data.generated_at;
  ["f-repel", "f-link", "f-center", "f-size"].forEach(function (id) { if (saved[id]) { $(id).value = saved[id]; } });
  $("pin").checked = !!saved.pinOnDrag;

  var nodes = new vis.DataSet(), edges = new vis.DataSet(), neighbours = {}, focus = null, query = "";

  function size(n) {
    var base = Number($("f-size").value);
    if (n.kind === "project") { return base * 1.9; }
    if (n.kind === "goal") { return base * 1.4; }
    return base + Math.min(base, 1.5 * (n.recalled || 0));
  }
  function lit(n) {
    if (query && (n.title + " " + (n.body || "")).toLowerCase().indexOf(query) < 0) { return false; }
    if (focus && n.id !== focus && !(neighbours[focus] && neighbours[focus][n.id])) { return false; }
    return true;
  }
  function visNode(n) {
    var on = lit(n) && n.status !== "retired", color = on ? KIND[n.kind][0] : DIM;
    var node = {
      id: n.id, label: n.label, shape: "dot", size: size(n),
      color: { background: color, border: n.status === "verified" && on ? INK : color,
               highlight: { background: KIND[n.kind][0], border: INK }, hover: { background: KIND[n.kind][0], border: INK } },
      borderWidth: n.status === "verified" ? 2 : 1,
      shapeProperties: { borderDashes: n.status === "candidate" ? [3, 3] : false },
      font: { color: on ? INK : DIM, size: 12, strokeWidth: 0, vadjust: 2 }
    };
    var pin = saved.pins[n.id];
    if (pin) { node.x = pin[0]; node.y = pin[1]; node.fixed = true; node.borderWidth = 3; }
    return node;
  }
  function visEdge(e, i) {
    var soft = e.type === "recalled_in" || e.type === "recorded_in" || e.type === "has_goal";
    var on = lit(byId[e.from]) && lit(byId[e.to]);
    return {
      id: i, from: e.from, to: e.to, label: on ? EDGE_LABEL[e.type] : "", dashes: e.type === "recalled_in",
      arrows: { to: { enabled: !soft || e.type === "recorded_in", scaleFactor: 0.5 } },
      color: { color: !on ? DIM : e.type === "contradicts" ? "#f85149" : EDGE, highlight: KIND.goal[0], hover: KIND.goal[0] },
      font: { size: 10, color: dark ? "#8b949e" : "#656d76", strokeWidth: 0, align: "middle" },
      width: soft ? 1 : 1.6, smooth: false
    };
  }
  function physics() {
    return {
      enabled: true, solver: "barnesHut", stabilization: false, minVelocity: 0.05, timestep: 0.45,
      barnesHut: {
        gravitationalConstant: -40 * Number($("f-repel").value), springLength: Number($("f-link").value),
        springConstant: 0.035, centralGravity: Number($("f-center").value) / 100, damping: 0.32, avoidOverlap: 0.2
      }
    };
  }

  var network = new vis.Network($("graph"), { nodes: nodes, edges: edges }, {
    physics: physics(),
    interaction: { hover: true, dragNodes: true, dragView: true, zoomView: true, hideEdgesOnDrag: false, tooltipDelay: 99999 },
    nodes: { chosen: true }, edges: { chosen: true }
  });

  function visible() {
    var showRetired = $("retired").checked, want = $("scope").value, keep = {};
    data.nodes.forEach(function (n) {
      if (want && n.scope !== want && n.scope !== "") { return; }
      if (n.status === "retired" && !showRetired) { return; }
      keep[n.id] = true;
    });
    return keep;
  }
  function repaint() {            // restyle in place: positions and motion are kept
    var ids = nodes.getIds(), shownEdges = edges.get();
    nodes.update(ids.map(function (id) {
      var v = visNode(byId[id]);
      delete v.x; delete v.y;
      return v;
    }));
    edges.update(shownEdges.map(function (e) { return visEdge(data.edges[e.id], e.id); }));
  }
  // edge ids are indexes into data.edges, so restyling can find its source record
  function rebuildStable() {
    var keep = visible(), showRecalls = $("recalls").checked;
    neighbours = {};
    nodes.clear(); edges.clear();
    nodes.add(data.nodes.filter(function (n) { return keep[n.id]; }).map(visNode));
    var list = [];
    data.edges.forEach(function (e, i) {
      if (!(keep[e.from] && keep[e.to] && (showRecalls || e.type !== "recalled_in"))) { return; }
      (neighbours[e.from] = neighbours[e.from] || {})[e.to] = true;
      (neighbours[e.to] = neighbours[e.to] || {})[e.from] = true;
      list.push(visEdge(e, i));
    });
    edges.add(list);
    $("count").textContent = "노드 " + nodes.length + " · 연결 " + edges.length;
  }

  function show(id) {
    var n = byId[id], fields = $("d-fields"), page = $("d-page");
    $("d-title").textContent = n ? n.title : "노드를 선택하세요";
    while (fields.firstChild) { fields.removeChild(fields.firstChild); }
    page.hidden = true;
    if (!n) { return; }
    var rows = [["종류", KIND[n.kind][1]]];
    if (n.status) { rows.push(["상태", STATUS[n.status] || n.status]); }
    if (n.origin) { rows.push(["기록 주체", n.origin]); }
    if (n.body) { rows.push(["내용", n.body]); }
    if (n.recalled !== undefined) { rows.push(["사용", "회상 " + n.recalled + " · 도움 " + n.helped + " · 도움 안 됨 " + n.unhelped]); }
    rows.push(["연결", String(Object.keys(neighbours[id] || {}).length)]);
    rows.forEach(function (r) {
      var dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = r[0]; dd.textContent = r[1]; fields.appendChild(dt); fields.appendChild(dd);
    });
    if (n.page && data.vault_rel) { page.setAttribute("href", data.vault_rel + "/" + n.page); page.hidden = false; }
  }

  // ---- interaction
  var selected = null;
  network.on("hoverNode", function (p) { if (!selected) { focus = p.node; repaint(); } });
  network.on("blurNode", function () { if (!selected) { focus = null; repaint(); } });
  network.on("click", function (p) {
    selected = p.nodes[0] || null; focus = selected; show(selected); repaint();
  });
  network.on("doubleClick", function (p) {
    var n = byId[p.nodes[0]];
    if (n && n.page && data.vault_rel) { window.open(data.vault_rel + "/" + n.page, "_blank", "noopener"); }
  });
  network.on("dragStart", function (p) {
    if (p.nodes.length) { nodes.update(p.nodes.map(function (id) { return { id: id, fixed: false }; })); }
  });
  network.on("dragEnd", function (p) {
    if (!p.nodes.length || !$("pin").checked) { return; }
    var at = network.getPositions(p.nodes);
    p.nodes.forEach(function (id) {
      saved.pins[id] = [Math.round(at[id].x), Math.round(at[id].y)];
      nodes.update({ id: id, fixed: true, borderWidth: 3 });
    });
    persist();
  });
  // labels fade out when zoomed far out, like a map
  network.on("zoom", function (p) {
    var px = p.scale < 0.45 ? 0 : 12;
    network.setOptions({ nodes: { font: { size: px } }, edges: { font: { size: p.scale < 0.8 ? 0 : 10 } } });
  });

  $("search").addEventListener("input", function () { query = this.value.trim().toLowerCase(); repaint(); });
  ["scope", "retired", "recalls"].forEach(function (id) { $(id).addEventListener("change", rebuildStable); });
  $("pin").addEventListener("change", function () { saved.pinOnDrag = this.checked; persist(); });
  $("unpin").addEventListener("click", function () {
    saved.pins = {}; persist();
    nodes.update(nodes.getIds().map(function (id) { return { id: id, fixed: false, borderWidth: byId[id].status === "verified" ? 2 : 1 }; }));
  });
  $("fit").addEventListener("click", function () { network.fit({ animation: { duration: 400, easingFunction: "easeInOutQuad" } }); });
  ["f-repel", "f-link", "f-center"].forEach(function (id) {
    $(id).addEventListener("input", function () { saved[id] = this.value; persist(); network.setOptions({ physics: physics() }); });
  });
  $("f-size").addEventListener("input", function () { saved["f-size"] = this.value; persist(); repaint(); });

  rebuildStable();
  window.setTimeout(function () { network.fit(); }, 900);
  window.lupusGraph = { network: network, nodes: nodes };   // read-only handle for automated UI checks
})();
