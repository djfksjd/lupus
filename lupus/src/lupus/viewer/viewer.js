// Lupus graph viewer. Renders the exported memory graph with vis-network (vendored, unmodified).
// The layout is a live force simulation: nodes can be dragged freely and their neighbours follow.
// All node text is untrusted data: it is only ever assigned through textContent or drawn on the
// canvas by vis-network, never interpreted as HTML.
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("data").textContent);
  var root = document.documentElement;
  var SHAPE = { project: "diamond", goal: "square" };
  // ---- every word the page shows, per language. Kinds, statuses and link names are data, not prose:
  // link = [drawn on the line, as read from the source node, as read from the target node]
  var KINDS = ["project", "goal", "lesson", "decision", "fact", "procedure", "preference"];
  var WORDS = {
    ko: {
      title: "지식 그래프", language: "언어", theme: "밝게 / 어둡게", zoom_in: "확대", zoom_out: "축소", fit: "화면에 맞추기",
      hint_move: "끌어서 이동 · 휠로 확대 · 더블클릭하면 문서 열기", hint_search: "검색", hint_esc: "선택 해제",
      search: "제목과 내용에서 찾기", kinds: "종류", all_projects: "모든 프로젝트",
      show_recalls: "회상된 곳으로 가는 연결 표시", show_retired: "은퇴한 노드 표시", selected: "선택한 노드",
      idle: "노드를 누르면 내용과 연결이 여기에 표시됩니다.", recalled: "회상", helped: "도움 됨", unhelped: "도움 안 됨",
      origin: "기록 주체", state: "상태", open_page: "문서 읽기", doc_index: "Vault 색인", back: "뒤로", close: "닫기", no_page: "이 문서는 아직 만들어지지 않았습니다.", layout: "배치 조정",
      f_repel: "반발력", f_link: "연결 거리", f_center: "중심 인력", f_size: "노드 크기",
      pin: "끌어다 놓은 위치에 고정", unpin: "고정 모두 해제", legend: "표기",
      k_verified: "사용자가 확인한 지식", k_confirmed: "반복해서 쓰인 지식", k_candidate: "아직 검증되지 않은 지식",
      k_relation: "지식 사이의 관계", k_contradicts: "서로 상충", k_recorded: "기록된 목표", k_recalled: "회상된 목표",
      read_only: "읽기 전용", generated: "{t} 생성", vendor: "vis-network (Apache-2.0 / MIT)를 수정 없이 포함",
      count: "노드 {n} · 연결 {e}", hits: "{n}개 일치", no_hits: "일치 없음",
      none_title: "아직 기록된 지식이 없습니다",
      none_text: "작업이 끝날 때 배운 것이 기록되고, lupus note-add 로 직접 적을 수도 있습니다. 그 뒤 lupus graph 를 다시 실행하세요.",
      filtered_title: "이 조건에 맞는 노드가 없습니다", filtered_text: "위의 종류 단추나 범위를 바꿔 보세요.",
      kind: { project: "프로젝트", goal: "목표", lesson: "교훈", decision: "결정", fact: "사실", procedure: "절차", preference: "선호" },
      status: { verified: "사용자 확인", confirmed: "반복 사용됨", candidate: "미검증", retired: "은퇴" },
      link: { supersedes: ["대체", "대체함", "대체됨"], derived_from: ["파생", "여기서 파생", "파생시킴"],
              contradicts: ["상충", "상충", "상충"], part_of: ["부분", "여기에 속함", "포함"], relates: ["관련", "관련", "관련"],
              has_goal: ["", "목표", "프로젝트"], recorded_in: ["기록", "기록된 목표", "여기서 기록됨"],
              recalled_in: ["회상", "회상된 목표", "여기서 회상됨"] }
    },
    en: {
      title: "Knowledge graph", language: "Language", theme: "Light / dark", zoom_in: "Zoom in", zoom_out: "Zoom out", fit: "Fit to screen",
      hint_move: "Drag to move · scroll to zoom · double-click opens the document", hint_search: "search", hint_esc: "clear selection",
      search: "Search titles and text", kinds: "Kinds", all_projects: "All projects",
      show_recalls: "Show links to where a note was recalled", show_retired: "Show retired nodes", selected: "Selected node",
      idle: "Click a node to see its text and connections here.", recalled: "Recalled", helped: "Helped", unhelped: "Did not help",
      origin: "Recorded by", state: "Status", open_page: "Read the document", doc_index: "Vault index", back: "Back", close: "Close", no_page: "This document has not been generated yet.", layout: "Layout",
      f_repel: "Repulsion", f_link: "Link length", f_center: "Centre pull", f_size: "Node size",
      pin: "Keep nodes where I drop them", unpin: "Unpin all", legend: "Key",
      k_verified: "Confirmed by the user", k_confirmed: "Used repeatedly", k_candidate: "Not yet verified",
      k_relation: "Relation between notes", k_contradicts: "Contradiction", k_recorded: "Goal it was recorded in",
      k_recalled: "Goal it was recalled in",
      read_only: "read-only", generated: "generated {t}", vendor: "vis-network (Apache-2.0 / MIT), included unmodified",
      count: "{n} nodes · {e} links", hits: "{n} found", no_hits: "No match",
      none_title: "Nothing has been recorded yet",
      none_text: "Lessons are recorded when work finishes, and you can write one yourself with lupus note-add. Then run lupus graph again.",
      filtered_title: "No node matches these filters", filtered_text: "Change the kind buttons or the project above.",
      kind: { project: "Project", goal: "Goal", lesson: "Lesson", decision: "Decision", fact: "Fact", procedure: "Procedure", preference: "Preference" },
      status: { verified: "confirmed by user", confirmed: "used repeatedly", candidate: "unverified", retired: "retired" },
      link: { supersedes: ["replaces", "replaces", "replaced by"], derived_from: ["from", "derived from", "source of"],
              contradicts: ["contradicts", "contradicts", "contradicts"], part_of: ["part of", "part of", "contains"],
              relates: ["related", "related", "related"], has_goal: ["", "goal", "project"],
              recorded_in: ["recorded", "recorded in", "recorded here"], recalled_in: ["recalled", "recalled in", "recalled here"] }
    },
    ja: {
      title: "ナレッジグラフ", language: "言語", theme: "ライト / ダーク", zoom_in: "拡大", zoom_out: "縮小", fit: "画面に合わせる",
      hint_move: "ドラッグで移動 · ホイールで拡大 · ダブルクリックで文書を開く", hint_search: "検索", hint_esc: "選択解除",
      search: "タイトルと本文を検索", kinds: "種類", all_projects: "すべてのプロジェクト",
      show_recalls: "想起された目標へのリンクを表示", show_retired: "引退したノードを表示", selected: "選択したノード",
      idle: "ノードをクリックすると、内容とつながりがここに表示されます。", recalled: "想起", helped: "役立った", unhelped: "役立たなかった",
      origin: "記録者", state: "状態", open_page: "文書を読む", doc_index: "Vault の索引", back: "戻る", close: "閉じる", no_page: "この文書はまだ生成されていません。", layout: "レイアウト調整",
      f_repel: "反発力", f_link: "リンク距離", f_center: "中心引力", f_size: "ノードサイズ",
      pin: "ドロップした位置に固定", unpin: "固定をすべて解除", legend: "凡例",
      k_verified: "ユーザーが確認した知識", k_confirmed: "繰り返し使われた知識", k_candidate: "まだ検証されていない知識",
      k_relation: "知識どうしの関係", k_contradicts: "互いに矛盾", k_recorded: "記録された目標", k_recalled: "想起された目標",
      read_only: "読み取り専用", generated: "{t} 生成", vendor: "vis-network (Apache-2.0 / MIT) を無改変で同梱",
      count: "ノード {n} · リンク {e}", hits: "{n} 件", no_hits: "一致なし",
      none_title: "まだ記録された知識がありません",
      none_text: "作業が終わると学んだことが記録され、lupus note-add で直接書くこともできます。その後 lupus graph をもう一度実行してください。",
      filtered_title: "この条件に合うノードがありません", filtered_text: "上の種類ボタンか範囲を変えてみてください。",
      kind: { project: "プロジェクト", goal: "目標", lesson: "教訓", decision: "決定", fact: "事実", procedure: "手順", preference: "好み" },
      status: { verified: "ユーザー確認済み", confirmed: "繰り返し使用", candidate: "未検証", retired: "引退" },
      link: { supersedes: ["置換", "置き換える", "置き換えられた"], derived_from: ["派生", "ここから派生", "派生元"],
              contradicts: ["矛盾", "矛盾", "矛盾"], part_of: ["部分", "ここに属する", "含む"], relates: ["関連", "関連", "関連"],
              has_goal: ["", "目標", "プロジェクト"], recorded_in: ["記録", "記録された目標", "ここで記録"],
              recalled_in: ["想起", "想起された目標", "ここで想起"] }
    },
    zh: {
      title: "知识图谱", language: "语言", theme: "浅色 / 深色", zoom_in: "放大", zoom_out: "缩小", fit: "适应屏幕",
      hint_move: "拖动移动 · 滚轮缩放 · 双击打开文档", hint_search: "搜索", hint_esc: "取消选择",
      search: "搜索标题和内容", kinds: "类型", all_projects: "所有项目",
      show_recalls: "显示指向回忆位置的连接", show_retired: "显示已停用的节点", selected: "选中的节点",
      idle: "点击节点后，这里会显示其内容和连接。", recalled: "回忆", helped: "有帮助", unhelped: "没帮助",
      origin: "记录者", state: "状态", open_page: "阅读文档", doc_index: "Vault 索引", back: "返回", close: "关闭", no_page: "该文档尚未生成。", layout: "布局调整",
      f_repel: "斥力", f_link: "连接距离", f_center: "向心力", f_size: "节点大小",
      pin: "固定在放下的位置", unpin: "全部取消固定", legend: "图例",
      k_verified: "用户确认的知识", k_confirmed: "多次使用的知识", k_candidate: "尚未验证的知识",
      k_relation: "知识之间的关系", k_contradicts: "相互矛盾", k_recorded: "记录它的目标", k_recalled: "回忆它的目标",
      read_only: "只读", generated: "生成于 {t}", vendor: "内含未经修改的 vis-network (Apache-2.0 / MIT)",
      count: "节点 {n} · 连接 {e}", hits: "{n} 个匹配", no_hits: "无匹配",
      none_title: "还没有记录任何知识",
      none_text: "工作结束时会记录学到的内容，也可以用 lupus note-add 直接写入。然后再次运行 lupus graph。",
      filtered_title: "没有符合这些条件的节点", filtered_text: "请更改上方的类型按钮或范围。",
      kind: { project: "项目", goal: "目标", lesson: "教训", decision: "决定", fact: "事实", procedure: "步骤", preference: "偏好" },
      status: { verified: "用户已确认", confirmed: "多次使用", candidate: "未验证", retired: "已停用" },
      link: { supersedes: ["替代", "替代了", "被替代"], derived_from: ["派生", "派生自", "派生出"],
              contradicts: ["矛盾", "矛盾", "矛盾"], part_of: ["部分", "属于", "包含"], relates: ["相关", "相关", "相关"],
              has_goal: ["", "目标", "项目"], recorded_in: ["记录", "记录于", "在此记录"],
              recalled_in: ["回忆", "回忆于", "在此回忆"] }
    }
  };
  var W = WORDS.ko, KIND = W.kind, STATUS = W.status, LINK = W.link;
  function say(key, values) {
    return String(W[key]).replace(/\{(\w)\}/g, function (_, name) { return values[name]; });
  }
  var FACE = '"Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", "Noto Sans KR", "Segoe UI", sans-serif';
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };
  var byId = {};
  data.nodes.forEach(function (n) { byId[n.id] = n; });

  // ---- persisted view state (pinned positions, sliders, hidden kinds, theme, language); best effort only
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
    KINDS.forEach(function (k) { C.kind[k] = get("--k-" + k); });
    // which icon the theme button shows follows what is actually on screen
    var dark = saved.theme ? saved.theme === "dark" : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (dark) { root.setAttribute("data-dark", ""); } else { root.removeAttribute("data-dark"); }
  }
  readTheme();

  [["", ""]].concat(data.scopes).forEach(function (s) {
    var o = document.createElement("option");
    o.value = s[0]; o.textContent = s[1]; $("scope").appendChild(o);
  });
  var chips = {};
  KINDS.forEach(function (k) {
    var b = document.createElement("button"), dot = document.createElement("i"), name = document.createElement("span"),
        count = document.createElement("small");
    b.type = "button"; dot.className = k; dot.style.background = "var(--k-" + k + ")";
    b.appendChild(dot); b.appendChild(name); b.appendChild(count);
    b.setAttribute("aria-pressed", String(!saved.hidden[k]));
    b.addEventListener("click", function () {
      saved.hidden[k] = !saved.hidden[k]; persist();
      b.setAttribute("aria-pressed", String(!saved.hidden[k]));
      rebuild();
    });
    chips[k] = [b, count, name]; $("kinds").appendChild(b);
  });
  // The language follows the browser until the user picks one. Only the page's own words change:
  // titles and bodies of nodes are the user's data and stay as written.
  function speak(lang) {
    W = WORDS[lang] || WORDS.en; KIND = W.kind; STATUS = W.status; LINK = W.link;
    root.setAttribute("lang", lang === "zh" ? "zh-CN" : lang);
    $("lang").value = lang;
    document.title = "Lupus " + W.title;
    [["data-t", "textContent"], ["data-t-title", "title"], ["data-t-placeholder", "placeholder"], ["data-t-aria", "aria-label"]]
      .forEach(function (rule) {
        Array.prototype.forEach.call(document.querySelectorAll("[" + rule[0] + "]"), function (el) {
          var text = W[el.getAttribute(rule[0])];
          if (rule[1] === "textContent") { el.textContent = text; } else { el.setAttribute(rule[1], text); }
          if (rule[1] === "title" || rule[1] === "placeholder") { el.setAttribute("aria-label", text); }
        });
      });
    $("scope").options[0].textContent = W.all_projects;
    KINDS.forEach(function (k) { chips[k][2].textContent = KIND[k]; });
    $("generated").textContent = say("generated", { t: data.generated_at });
  }
  var tongue = (window.navigator.language || "en").toLowerCase().slice(0, 2);
  speak(WORDS[saved.lang] ? saved.lang : WORDS[tongue] ? tongue : "en");
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
    out.textContent = !query ? "" : hits ? say("hits", { n: hits }) : W.no_hits;
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
    $("count").textContent = say("count", { n: nodes.length, e: edges.length });
    $("empty").hidden = nodes.length > 0;
    if (!nodes.length) {
      var none = !data.nodes.length;
      $("empty-title").textContent = none ? W.none_title : W.filtered_title;
      $("empty-text").textContent = none ? W.none_text : W.filtered_text;
    }
    show(selected); tally();
  }

  function show(id) {
    var n = byId[id], links = $("d-links");
    $("detail").className = n ? "" : "idle";
    $("d-title").textContent = n ? n.title : W.idle;
    while (links.firstChild) { links.removeChild(links.firstChild); }
    ["d-kind", "d-body", "d-use", "d-meta", "d-links", "d-page"].forEach(function (part) { $(part).hidden = true; });
    if (!n) { return; }
    $("d-dot").style.background = "var(--k-" + n.kind + ")";
    $("d-kind-text").textContent = KIND[n.kind] + (n.status ? " · " + (STATUS[n.status] || n.status) : "");
    $("d-kind").hidden = false;
    var body = n.state ? W.state + ": " + n.state : n.body;
    if (body) { $("d-body").textContent = body; $("d-body").hidden = false; }
    if (n.recalled !== undefined) {
      $("d-recalled").textContent = n.recalled; $("d-helped").textContent = n.helped; $("d-unhelped").textContent = n.unhelped;
      $("d-use").hidden = false;
    }
    if (n.origin) { $("d-meta").textContent = W.origin + ": " + n.origin; $("d-meta").hidden = false; }
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
    $("d-page").hidden = !(n.page && pages[n.page]);
  }

  // ---- the reading pane. Vault pages are Markdown that Lupus generated; this reads the few constructs
  // those pages use and builds elements with textContent only, so nothing in a page can become markup.
  var pages = data.pages || {}, pageOf = {}, trail = [];
  data.nodes.forEach(function (n) { if (n.page) { pageOf[n.page] = n.id; } });
  $("docs").hidden = !pages["index.md"];      // nothing to browse when the export carries no pages
  function resolve(from, rel) {
    var parts = from.split("/").slice(0, -1);
    rel.split("/").forEach(function (p) { if (p === "..") { parts.pop(); } else if (p && p !== ".") { parts.push(p); } });
    return parts.join("/");
  }
  function inline(into, text, page) {
    var plain = "", i = 0, flush = function () { if (plain) { into.appendChild(document.createTextNode(plain)); plain = ""; } };
    var until = function (mark, from) {            // index of the next unescaped `mark`
      for (var at = from; at < text.length; at += 1) {
        if (text[at] === "\\") { at += 1; } else if (text.substr(at, mark.length) === mark) { return at; }
      }
      return -1;
    };
    while (i < text.length) {
      var ch = text[i], end, el;
      if (ch === "\\" && i + 1 < text.length) { plain += text[i + 1]; i += 2; continue; }
      if (text.substr(i, 4) === "&lt;") { plain += "<"; i += 4; continue; }
      if (text.substr(i, 2) === "**" && (end = until("**", i + 2)) > 0) {
        flush(); el = document.createElement("strong"); inline(el, text.slice(i + 2, end), page); into.appendChild(el); i = end + 2; continue;
      }
      if (ch === "`" && (end = text.indexOf("`", i + 1)) > 0) {
        flush(); el = document.createElement("code"); el.textContent = text.slice(i + 1, end); into.appendChild(el); i = end + 1; continue;
      }
      if (ch === "[" && (end = until("](", i + 1)) > 0) {
        var stop = until(")", end + 2);
        if (stop > 0) {
          var target = resolve(page, text.slice(end + 2, stop));
          flush();
          if (pages[target]) {
            el = document.createElement("a"); el.setAttribute("role", "link"); el.tabIndex = 0;
            (function (to) {
              el.addEventListener("click", function () { read(to); });
              el.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { read(to); } });
            })(target);
          } else { el = document.createElement("span"); }      // a link out of the vault is shown as its words only
          inline(el, text.slice(i + 1, end), page); into.appendChild(el); i = stop + 1; continue;
        }
      }
      plain += ch; i += 1;
    }
    flush();
  }
  function cells(line) {
    var out = [], cell = "", i;
    for (i = line.indexOf("|") + 1; i < line.length; i += 1) {
      if (line[i] === "\\") { cell += line.substr(i, 2); i += 1; } else if (line[i] === "|") { out.push(cell.trim()); cell = ""; } else { cell += line[i]; }
    }
    return out;
  }
  function typeset(into, markdown, page) {
    var lines = markdown.split("\n"), list = null, table = null, i, line, el, first = true;
    while (into.firstChild) { into.removeChild(into.firstChild); }
    for (i = 0; i < lines.length; i += 1) {
      line = lines[i];
      if (!/^- /.test(line)) { list = null; }
      if (line[0] !== "|") { table = null; }
      if (!line.trim()) { continue; }
      if (/^#{1,2} /.test(line)) {
        el = document.createElement(line[1] === "#" ? "h2" : "h1"); inline(el, line.replace(/^#+ /, ""), page); into.appendChild(el);
      } else if (line[0] === ">") {
        el = document.createElement("blockquote"); inline(el, line.replace(/^> ?/, ""), page);
        if (first) { el.className = "note"; first = false; }      // every page opens with the same read-only notice
        into.appendChild(el);
      } else if (/^- /.test(line)) {
        if (!list) { list = document.createElement("ul"); into.appendChild(list); }
        el = document.createElement("li"); inline(el, line.slice(2), page); list.appendChild(el);
      } else if (line[0] === "|") {
        if (/^\|[-| ]+\|$/.test(line)) { continue; }
        var row = document.createElement("tr"), head = !table;
        if (!table) {
          var wrap = document.createElement("div"); wrap.className = "table";
          table = document.createElement("table"); wrap.appendChild(table); into.appendChild(wrap);
        }
        cells(line).forEach(function (text) {
          var cell = document.createElement(head ? "th" : "td"); inline(cell, text, page); row.appendChild(cell);
        });
        table.appendChild(row);
      } else {
        el = document.createElement("p"); inline(el, line, page); into.appendChild(el);
      }
    }
  }
  function read(page, back) {
    if (!back && trail[trail.length - 1] !== page) { trail.push(page); }
    $("r-path").textContent = page.replace(/\.md$/, "");
    if (pages[page]) { typeset($("r-body"), pages[page], page); } else { $("r-body").textContent = W.no_page; }
    $("r-back").disabled = trail.length < 2;
    $("reader").hidden = false;
    $("r-body").scrollTop = 0;
    if (pageOf[page] && pageOf[page] !== selected && nodes.get(pageOf[page])) { select(pageOf[page], false); }
  }
  function shut() { $("reader").hidden = true; trail = []; }
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
    if (n && n.page) { read(n.page); }
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
      if (!$("reader").hidden) { shut(); }
      else if (document.activeElement === $("search") && query) { $("search").value = ""; query = ""; repaint(); } else { select(null, false); }
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
  $("d-page").addEventListener("click", function () { if (selected && byId[selected].page) { read(byId[selected].page); } });
  $("docs").addEventListener("click", function () { if ($("reader").hidden) { read("index.md"); } else { shut(); } });
  $("r-close").addEventListener("click", shut);
  $("r-back").addEventListener("click", function () { if (trail.length > 1) { trail.pop(); read(trail[trail.length - 1], true); } });
  $("lang").addEventListener("change", function () {
    saved.lang = this.value; persist();
    speak(saved.lang); rebuild();
  });
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
