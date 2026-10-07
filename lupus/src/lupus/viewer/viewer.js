// Lupus graph viewer. Renders the exported memory graph with vis-network (vendored, unmodified).
// The layout is a live force simulation: nodes can be dragged freely and their neighbours follow.
// All node text is untrusted data: it is only ever assigned through textContent or drawn on the
// canvas by vis-network, never interpreted as HTML.
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("data").textContent);
  var root = document.documentElement;
  var KIND = {
    project: "프로젝트", goal: "목표", lesson: "교훈", decision: "결정", fact: "사실", procedure: "절차", preference: "선호"
  };
  var STATUS = { verified: "사용자 확인", confirmed: "반복 사용됨", candidate: "미검증", retired: "은퇴" };
  var SHAPE = { project: "diamond", goal: "square" };
  // [label drawn on the line, as read from the source node, as read from the target node]
  var LINK = {
    supersedes: ["대체", "대체함", "대체됨"], derived_from: ["파생", "여기서 파생", "파생시킴"],
    contradicts: ["상충", "상충", "상충"], part_of: ["부분", "여기에 속함", "포함"], relates: ["관련", "관련", "관련"],
    has_goal: ["", "목표", "프로젝트"], recorded_in: ["기록", "기록된 목표", "여기서 기록됨"],
    recalled_in: ["회상", "회상된 목표", "여기서 회상됨"]
  };
  var FACE = '"Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", "Segoe UI", sans-serif';
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };
  var byId = {};
  data.nodes.forEach(function (n) { byId[n.id] = n; });

  // ---- persisted view state (pinned positions, sliders, hidden kinds, theme); best effort only
  var KEY = "lupus-graph-v1", saved = {};
  try { saved = JSON.parse(window.localStorage.getItem(KEY) || "{}") || {}; } catch (e) { saved = {}; }
  saved.pins = saved.pins || {};
  saved.hidden = saved.hidden || {};
  function persist() { try { window.localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* file:// may refuse */ } }

  // ---- colours come from the stylesheet, so the canvas and the panel can never disagree
  var C = {};
  function readTheme() {
    if (saved.theme === "light" || saved.theme === "dark") { root.setAttribute("data-theme", saved.theme); }
    var css = window.getComputedStyle(root), get = function (name) { return css.getPropertyValue(name).trim(); };
    C = { ground: get("--ground"), ink: get("--ink"), muted: get("--muted"), faint: get("--faint"), edge: get("--edge"),
          accent: get("--accent"), danger: get("--danger"), kind: {} };
    Object.keys(KIND).forEach(function (k) { C.kind[k] = get("--k-" + k); });
    // which icon the theme button shows follows what is actually on screen
    var dark = saved.theme ? saved.theme === "dark" : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (dark) { root.setAttribute("data-dark", ""); } else { root.removeAttribute("data-dark"); }
  }
  readTheme();

  [["", "모든 프로젝트"]].concat(data.scopes).forEach(function (s) {
    var o = document.createElement("option");
    o.value = s[0]; o.textContent = s[1]; $("scope").appendChild(o);
  });
  var chips = {};
  Object.keys(KIND).forEach(function (k) {
    var b = document.createElement("button"), dot = document.createElement("i"), count = document.createElement("small");
    b.type = "button"; dot.className = k; dot.style.background = "var(--k-" + k + ")";
    b.appendChild(dot); b.appendChild(document.createTextNode(KIND[k])); b.appendChild(count);
    b.setAttribute("aria-pressed", String(!saved.hidden[k]));
    b.addEventListener("click", function () {
      saved.hidden[k] = !saved.hidden[k]; persist();
      b.setAttribute("aria-pressed", String(!saved.hidden[k]));
      rebuild();
    });
    chips[k] = [b, count]; $("kinds").appendChild(b);
  });
  $("generated").textContent = data.generated_at;
  $("kinds-title").hidden = !data.nodes.length;
  ["f-repel", "f-link", "f-center", "f-size"].forEach(function (id) { if (saved[id]) { $(id).value = saved[id]; } });
  $("pin").checked = !!saved.pinOnDrag;

  var nodes = new vis.DataSet(), edges = new vis.DataSet(), neighbours = {}, shown = [];
  var focus = null, selected = null, query = "", labelPx = 12, edgePx = 10, arrived = 0;

  function size(n) {
    var base = Number($("f-size").value);
    if (n.kind === "project") { return base * 1.9; }
    if (n.kind === "goal") { return base * 1.3; }
    return base + Math.min(base, 1.5 * (n.recalled || 0));
  }
  function matches(n) { return !query || (n.title + " " + (n.body || "")).toLowerCase().indexOf(query) >= 0; }
  function lit(n) {
    if (!matches(n)) { return false; }
    if (focus && n.id !== focus && !(neighbours[focus] && neighbours[focus][n.id])) { return false; }
    return true;
  }
  function visNode(n) {
    var on = lit(n) && n.status !== "retired", hue = C.kind[n.kind], hollow = n.status === "candidate";
    var fill = hollow ? C.ground : on ? hue : C.faint, rim = !on ? C.faint : n.status === "verified" ? C.ink : hue;
    var node = {
      id: n.id, label: n.label, shape: SHAPE[n.kind] || "dot", size: size(n),
      color: { background: fill, border: rim, highlight: { background: hollow ? C.ground : hue, border: C.ink },
               hover: { background: hollow ? C.ground : hue, border: C.ink } },
      borderWidth: n.status === "verified" ? 2.5 : hollow ? 1.5 : 1, borderWidthSelected: 2.5,
      shapeProperties: { borderDashes: hollow ? [3, 3] : false },
      // the label carries a halo in the ground colour, so a line passing behind it never strikes it through
      font: { color: on ? C.ink : C.faint, size: labelPx, face: FACE, strokeWidth: 4, strokeColor: C.ground, vadjust: 3 }
    };
    var pin = saved.pins[n.id];
    if (pin) { node.x = pin[0]; node.y = pin[1]; node.fixed = true; node.borderWidth = 3.5; }
    return node;
  }
  function visEdge(e, i) {
    var soft = e.type === "recalled_in" || e.type === "recorded_in" || e.type === "has_goal";
    var on = lit(byId[e.from]) && lit(byId[e.to]);
    var near = focus && (e.from === focus || e.to === focus);      // names appear only around the node being read
    return {
      id: i, from: e.from, to: e.to, label: near ? LINK[e.type][0] : "", dashes: e.type === "recalled_in" ? [4, 4] : false,
      arrows: { to: { enabled: !soft || e.type === "recorded_in", scaleFactor: 0.5 } },
      color: { color: !on ? C.faint : e.type === "contradicts" ? C.danger : near ? C.muted : C.edge, highlight: C.accent, hover: C.accent },
      font: { size: edgePx, color: C.muted, face: FACE, strokeWidth: 4, strokeColor: C.ground, align: "horizontal" },
      width: soft ? 1 : 1.7, smooth: false
    };
  }
  function physics() {
    return {
      enabled: true, solver: "barnesHut", stabilization: false, minVelocity: 0.05, timestep: 0.45,
      barnesHut: {
        gravitationalConstant: -40 * Number($("f-repel").value), springLength: Number($("f-link").value),
        springConstant: 0.035, centralGravity: Number($("f-center").value) / 100, damping: 0.32, avoidOverlap: 0.35
      }
    };
  }

  var network = new vis.Network($("graph"), { nodes: nodes, edges: edges }, {
    physics: physics(),
    interaction: { hover: true, dragNodes: true, dragView: true, zoomView: true, hideEdgesOnDrag: false, tooltipDelay: 99999 },
    nodes: { chosen: true }, edges: { chosen: true }
  });

  function visible() {
    var showRetired = $("retired").checked, want = $("scope").value, keep = {}, counts = {};
    data.nodes.forEach(function (n) {
      if (want && n.scope !== want && n.scope !== "") { return; }
      if (n.status === "retired" && !showRetired) { return; }
      counts[n.kind] = (counts[n.kind] || 0) + 1;
      if (!saved.hidden[n.kind]) { keep[n.id] = true; }
    });
    Object.keys(chips).forEach(function (k) {
      chips[k][1].textContent = String(counts[k] || 0);
      chips[k][0].hidden = !counts[k] && !saved.hidden[k];      // a kind this scope does not have is not offered
    });
    return keep;
  }
  function repaint() {            // restyle in place: positions and motion are kept
    nodes.update(nodes.getIds().map(function (id) {
      var v = visNode(byId[id]);
      delete v.x; delete v.y;
      return v;
    }));
    edges.update(shown.map(function (i) { return visEdge(data.edges[i], i); }));
    tally();
  }
  function tally() {
    var out = $("s-count"), hits = 0;
    if (query) { nodes.getIds().forEach(function (id) { if (matches(byId[id])) { hits += 1; } }); }
    out.textContent = !query ? "" : hits ? hits + "개 일치" : "일치 없음";
    out.className = query && !hits ? "none" : "";
  }
  // edge ids are indexes into data.edges, so restyling can find its source record
  function rebuild() {
    var keep = visible(), showRecalls = $("recalls").checked;
    neighbours = {}; shown = [];
    nodes.clear(); edges.clear();
    if (selected && !keep[selected]) { selected = null; focus = null; }
    nodes.add(data.nodes.filter(function (n) { return keep[n.id]; }).map(visNode));
    data.edges.forEach(function (e, i) {
      if (!(keep[e.from] && keep[e.to] && (showRecalls || e.type !== "recalled_in"))) { return; }
      (neighbours[e.from] = neighbours[e.from] || {})[e.to] = true;
      (neighbours[e.to] = neighbours[e.to] || {})[e.from] = true;
      shown.push(i);
    });
    edges.add(shown.map(function (i) { return visEdge(data.edges[i], i); }));
    $("count").textContent = "노드 " + nodes.length + " · 연결 " + edges.length;
    $("empty").hidden = nodes.length > 0;
    if (!nodes.length) {
      var none = !data.nodes.length;
      $("empty-title").textContent = none ? "아직 기록된 지식이 없습니다" : "이 조건에 맞는 노드가 없습니다";
      $("empty-text").textContent = none
        ? "작업이 끝날 때 배운 것이 기록되고, lupus note-add 로 직접 적을 수도 있습니다. 그 뒤 lupus graph 를 다시 실행하세요."
        : "위의 종류 단추나 범위를 바꿔 보세요.";
    }
    show(selected); tally();
  }

  function show(id) {
    var n = byId[id], links = $("d-links");
    $("detail").className = n ? "" : "idle";
    $("d-title").textContent = n ? n.title : "노드를 누르면 내용과 연결이 여기에 표시됩니다.";
    while (links.firstChild) { links.removeChild(links.firstChild); }
    ["d-kind", "d-body", "d-use", "d-meta", "d-links", "d-page"].forEach(function (part) { $(part).hidden = true; });
    if (!n) { return; }
    $("d-dot").style.background = "var(--k-" + n.kind + ")";
    $("d-kind-text").textContent = KIND[n.kind] + (n.status ? " · " + (STATUS[n.status] || n.status) : "");
    $("d-kind").hidden = false;
    if (n.body) { $("d-body").textContent = n.body; $("d-body").hidden = false; }
    if (n.recalled !== undefined) {
      $("d-recalled").textContent = n.recalled; $("d-helped").textContent = n.helped; $("d-unhelped").textContent = n.unhelped;
      $("d-use").hidden = false;
    }
    if (n.origin) { $("d-meta").textContent = "기록 주체: " + n.origin; $("d-meta").hidden = false; }
    shown.forEach(function (i) {
      var e = data.edges[i], out = e.from === id;
      if (!out && e.to !== id) { return; }
      var other = byId[out ? e.to : e.from], li = document.createElement("li"), b = document.createElement("button");
      var dot = document.createElement("i"), name = document.createElement("span"), how = document.createElement("small");
      dot.className = "dot"; dot.style.background = "var(--k-" + other.kind + ")";
      name.textContent = other.title; how.textContent = LINK[e.type][out ? 1 : 2];
      b.type = "button"; b.title = other.title;
      b.appendChild(dot); b.appendChild(name); b.appendChild(how);
      b.addEventListener("click", function () { select(other.id, true); });
      li.appendChild(b); links.appendChild(li);
    });
    links.hidden = !links.firstChild;
    if (n.page && data.vault_rel) { $("d-page").setAttribute("href", data.vault_rel + "/" + n.page); $("d-page").hidden = false; }
  }
  function select(id, travel) {
    selected = id && nodes.get(id) ? id : null; focus = selected;
    arrived = window.performance.now();
    if (selected) { network.selectNodes([selected]); } else { network.unselectAll(); }
    show(selected); repaint();
    if (selected && travel) {
      network.focus(selected, { scale: Math.max(network.getScale(), 0.9),
                                animation: calm ? false : { duration: 480, easingFunction: "easeOutQuart" } });
    }
  }

  // ---- the selected node wears the crescent of the Lupus mark: it swings in and settles
  network.on("afterDrawing", function (ctx) {
    if (!selected || !nodes.get(selected)) { return; }
    var at = network.getPositions([selected])[selected], t = calm ? 1 : Math.min(1, (window.performance.now() - arrived) / 620);
    var ease = 1 - Math.pow(1 - t, 4), r = size(byId[selected]) * (SHAPE[byId[selected].kind] ? 1.25 : 1) + 7 + 10 * (1 - ease);
    var turn = -2.2 + 1.5 * ease, thick = 3.2, lean = 2.7;
    ctx.save();
    ctx.globalAlpha = ease;
    ctx.fillStyle = C.accent;
    ctx.beginPath();
    ctx.arc(at.x, at.y, r, 0, 2 * Math.PI);
    ctx.arc(at.x + lean * Math.cos(turn), at.y + lean * Math.sin(turn), r - thick, 0, 2 * Math.PI, true);
    ctx.fill("evenodd");
    ctx.restore();
    if (t < 1) { window.requestAnimationFrame(function () { network.redraw(); }); }
  });

  // ---- interaction
  network.on("hoverNode", function (p) { if (!selected) { focus = p.node; repaint(); } });
  network.on("blurNode", function () { if (!selected) { focus = null; repaint(); } });
  network.on("click", function (p) { select(p.nodes[0] || null, false); });
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
      nodes.update({ id: id, fixed: true, borderWidth: 3.5 });
    });
    persist();
  });
  // labels fade out when zoomed far out, like a map; restyle only when a threshold is crossed
  network.on("zoom", function (p) {
    var a = p.scale < 0.45 ? 0 : 12, b = p.scale < 0.8 ? 0 : 10;
    if (a !== labelPx || b !== edgePx) { labelPx = a; edgePx = b; repaint(); }
  });

  function zoom(by) {
    network.moveTo({ scale: Math.min(4, Math.max(0.15, network.getScale() * by)),
                     animation: calm ? false : { duration: 220, easingFunction: "easeOutQuart" } });
  }
  function fit() { network.fit({ animation: calm ? false : { duration: 520, easingFunction: "easeOutQuart" } }); }

  $("search").addEventListener("input", function () { query = this.value.trim().toLowerCase(); repaint(); });
  $("search").addEventListener("keydown", function (ev) {      // Enter travels to the first match
    if (ev.key !== "Enter" || !query) { return; }
    var hit = nodes.getIds().filter(function (id) { return matches(byId[id]); })[0];
    if (hit) { select(hit, true); }
  });
  document.addEventListener("keydown", function (ev) {
    var typing = /^(INPUT|SELECT|TEXTAREA)$/.test((ev.target && ev.target.tagName) || "");
    if (ev.key === "/" && !typing) { ev.preventDefault(); $("search").focus(); $("search").select(); }
    if (ev.key === "Escape") {
      if (document.activeElement === $("search") && query) { $("search").value = ""; query = ""; repaint(); } else { select(null, false); }
    }
  });
  ["scope", "retired", "recalls"].forEach(function (id) { $(id).addEventListener("change", rebuild); });
  $("pin").addEventListener("change", function () { saved.pinOnDrag = this.checked; persist(); });
  $("unpin").addEventListener("click", function () {
    saved.pins = {}; persist();
    nodes.update(nodes.getIds().map(function (id) { return { id: id, fixed: false }; }));
    repaint();
  });
  $("fit").addEventListener("click", fit);
  $("zoom-in").addEventListener("click", function () { zoom(1.35); });
  $("zoom-out").addEventListener("click", function () { zoom(1 / 1.35); });
  $("theme").addEventListener("click", function () {
    saved.theme = root.hasAttribute("data-dark") ? "light" : "dark"; persist();
    readTheme(); repaint();
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { readTheme(); repaint(); });
  }
  ["f-repel", "f-link", "f-center"].forEach(function (id) {
    $(id).addEventListener("input", function () { saved[id] = this.value; persist(); network.setOptions({ physics: physics() }); });
  });
  $("f-size").addEventListener("input", function () { saved["f-size"] = this.value; persist(); repaint(); });

  rebuild();
  // frame the graph once it has spread out, unless the user has already taken the camera
  var touched = false;
  ["dragStart", "zoom", "click"].forEach(function (what) { network.on(what, function () { touched = true; }); });
  window.setTimeout(function () { if (!touched) { network.fit(); } }, 900);
  network.once("stabilized", function () { if (!touched) { fit(); } });
  window.lupusGraph = { network: network, nodes: nodes, select: select };   // handle for automated UI checks
})();
