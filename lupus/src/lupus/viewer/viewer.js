// Lupus knowledge viewer: an overview of the pack's memory, the live graph (vis-network, vendored,
// unmodified), every note as a list, the vault documents as a tree, and goals by project.
// All node text and page text is untrusted data: it is only ever assigned through textContent or
// drawn on the canvas by vis-network, never interpreted as HTML.
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("data").textContent);
  var root = document.documentElement;
  var SHAPE = { project: "diamond", goal: "square" };
  var KINDS = ["project", "goal", "lesson", "decision", "fact", "procedure", "preference"];
  var NOTES = ["lesson", "decision", "fact", "procedure", "preference"];
  var STATUSES = ["verified", "confirmed", "candidate", "retired"];
  // goal states fall into four phases of the moon: full = done and verified, half = being worked on,
  // crescent = waiting for something, new = stopped
  var PHASE = { DONE: "done", ACTIVE: "active", RECONCILING: "active", CANCELLED: "stop", FAILED: "stop" };
  function phase(state) { return PHASE[state] || "wait"; }

  // ---- every word the page shows, per language. Kinds, statuses and link names are data, not prose:
  // link = [drawn on the line, as read from the source node, as read from the target node]
  var WORDS = {
    en: {
      v_overview: "Overview", v_graph: "Graph", v_knowledge: "Knowledge", v_docs: "Documents", v_goals: "Goals",
      sub_overview: "What the pack has learned, and whether it helped verified work.",
      sub_graph: "How knowledge, goals and projects connect. Drag nodes; double-click opens the document.",
      sub_knowledge: "Every note with its lifecycle. Trust is earned by verified use, never by a model saying so.",
      sub_docs: "The vault as Lupus writes it: read-only Markdown, linked like the graph.",
      sub_goals: "Goals by project. The moon shows each goal's state.",
      find_ph: "Search knowledge, goals, documents", launcher: "Find anything", palette_ph: "Type to find a note, a goal, a document…",
      p_move: "move", p_open: "open", open: "Open", all: "All", language: "Language", theme: "Light / dark",
      c_graph: "Knowledge graph", c_trust: "Trust", c_kinds: "By kind", c_goals: "Recent goals", c_helped: "Most helpful knowledge",
      c_docs: "Vault documents",
      kpi_nodes: "Knowledge", kpi_nodes_sub: "{r} retired", kpi_trusted: "Trusted", kpi_trusted_sub: "verified or confirmed",
      kpi_helped: "Helped verified work", kpi_helped_sub: "{h} of {r} recalls", kpi_goals: "Goals done",
      kpi_goals_sub: "{a} active · {w} waiting", kpi_projects: "Projects", kpi_projects_sub: "{d} documents",
      trust_note: "{p}% trusted", filter_ph: "Filter by title or text", any_status: "Any status", any_kind: "Any kind",
      col_title: "Title", col_kind: "Kind", col_status: "Status", col_project: "Project", shown: "{n} of {t}",
      no_rows: "No note matches these filters.", global: "Global", home: "Home", map: "Map", goals_folder: "Goals",
      nodes_folder: "Knowledge", links: "Connections", show_in_graph: "Show in the graph", nothing_yet: "Nothing yet.",
      no_helped: "No note has been recalled in verified work yet.",
      zoom_in: "Zoom in", zoom_out: "Zoom out", fit: "Fit to screen", unpin: "Unpin all", pin: "Pin where dropped",
      hint_move: "Drag space to orbit · +/− to zoom · click a planet to inspect",
      all_projects: "All projects", show_recalls: "Recall links", show_retired: "Retired",
      recalled: "Recalled", helped: "Helped", unhelped: "Did not help", origin: "Recorded by", state: "State",
      open_page: "Read the document", back: "Back", close: "Close", no_page: "This document has not been generated yet.",
      generated: "Generated {t}", count: "{n} nodes · {e} links",
      none_title: "Nothing has been recorded yet",
      none_text: "Lessons are recorded when work finishes, and you can write one with lupus note-add. Then run lupus graph again.",
      filtered_title: "No node matches these filters", filtered_text: "Change the kinds or the project above.",
      phase: { done: "Done", active: "Working", wait: "Waiting", stop: "Stopped" },
      kind: { project: "Project", goal: "Goal", lesson: "Lesson", decision: "Decision", fact: "Fact", procedure: "Procedure", preference: "Preference" },
      kinds_pl: { lesson: "Lessons", decision: "Decisions", fact: "Facts", procedure: "Procedures", preference: "Preferences" },
      status: { verified: "Verified", confirmed: "Confirmed", candidate: "Candidate", retired: "Retired" },
      status_long: { verified: "confirmed by you", confirmed: "helped repeatedly", candidate: "not yet shown to help", retired: "no longer recalled" },
      link: { supersedes: ["replaces", "replaces", "replaced by"], derived_from: ["from", "derived from", "source of"],
              contradicts: ["contradicts", "contradicts", "contradicts"], part_of: ["part of", "part of", "contains"],
              relates: ["related", "related", "related"], has_goal: ["", "goal", "project"],
              recorded_in: ["recorded", "recorded in", "recorded here"], recalled_in: ["recalled", "recalled in", "recalled here"] }
    },
    ko: {
      v_overview: "개요", v_graph: "그래프", v_knowledge: "지식", v_docs: "문서", v_goals: "목표",
      sub_overview: "무리가 배운 것과, 그것이 검증된 작업에 도움이 되었는지.",
      sub_graph: "지식·목표·프로젝트가 어떻게 이어지는지. 노드를 끌어 보고, 더블클릭하면 문서가 열립니다.",
      sub_knowledge: "모든 지식과 그 수명 주기. 신뢰는 모델의 말이 아니라 검증된 사용으로만 얻습니다.",
      sub_docs: "Lupus가 쓰는 Vault 그대로: 읽기 전용 Markdown, 그래프처럼 서로 연결됩니다.",
      sub_goals: "프로젝트별 목표. 달 모양이 각 목표의 상태입니다.",
      find_ph: "지식, 목표, 문서 찾기", launcher: "무엇이든 찾기", palette_ph: "지식, 목표, 문서 이름을 입력하세요…",
      p_move: "이동", p_open: "열기", open: "열기", all: "전체", language: "언어", theme: "밝게 / 어둡게",
      c_graph: "지식 그래프", c_trust: "신뢰", c_kinds: "종류별", c_goals: "최근 목표", c_helped: "가장 도움이 된 지식",
      c_docs: "Vault 문서",
      kpi_nodes: "지식", kpi_nodes_sub: "은퇴 {r}", kpi_trusted: "신뢰된 지식", kpi_trusted_sub: "사용자 확인 또는 반복 사용",
      kpi_helped: "검증된 작업에 도움", kpi_helped_sub: "회상 {r}번 중 {h}번", kpi_goals: "완료한 목표",
      kpi_goals_sub: "진행 {a} · 대기 {w}", kpi_projects: "프로젝트", kpi_projects_sub: "문서 {d}개",
      trust_note: "{p}% 신뢰됨", filter_ph: "제목이나 내용으로 거르기", any_status: "모든 상태", any_kind: "모든 종류",
      col_title: "제목", col_kind: "종류", col_status: "상태", col_project: "프로젝트", shown: "{t}개 중 {n}개",
      no_rows: "이 조건에 맞는 지식이 없습니다.", global: "전역", home: "처음", map: "지도", goals_folder: "목표",
      nodes_folder: "지식", links: "연결", show_in_graph: "그래프에서 보기", nothing_yet: "아직 없습니다.",
      no_helped: "검증된 작업에서 회상된 지식이 아직 없습니다.",
      zoom_in: "확대", zoom_out: "축소", fit: "화면에 맞추기", unpin: "고정 모두 해제", pin: "놓은 자리에 고정",
      hint_move: "빈 공간을 끌어 회전 · +/−로 확대 · 행성을 클릭해 상세 보기",
      all_projects: "모든 프로젝트", show_recalls: "회상 연결", show_retired: "은퇴한 노드",
      recalled: "회상", helped: "도움 됨", unhelped: "도움 안 됨", origin: "기록 주체", state: "상태",
      open_page: "문서 읽기", back: "뒤로", close: "닫기", no_page: "이 문서는 아직 만들어지지 않았습니다.",
      generated: "{t} 생성", count: "노드 {n} · 연결 {e}",
      none_title: "아직 기록된 지식이 없습니다",
      none_text: "작업이 끝날 때 배운 것이 기록되고, lupus note-add 로 직접 적을 수도 있습니다. 그 뒤 lupus graph 를 다시 실행하세요.",
      filtered_title: "이 조건에 맞는 노드가 없습니다", filtered_text: "위의 종류나 프로젝트를 바꿔 보세요.",
      phase: { done: "완료", active: "진행 중", wait: "대기", stop: "중단" },
      kind: { project: "프로젝트", goal: "목표", lesson: "교훈", decision: "결정", fact: "사실", procedure: "절차", preference: "선호" },
      kinds_pl: { lesson: "교훈", decision: "결정", fact: "사실", procedure: "절차", preference: "선호" },
      status: { verified: "사용자 확인", confirmed: "반복 사용", candidate: "미검증", retired: "은퇴" },
      status_long: { verified: "사용자가 확인함", confirmed: "여러 번 도움이 됨", candidate: "아직 도움이 확인되지 않음", retired: "더 이상 회상하지 않음" },
      link: { supersedes: ["대체", "대체함", "대체됨"], derived_from: ["파생", "여기서 파생", "파생시킴"],
              contradicts: ["상충", "상충", "상충"], part_of: ["부분", "여기에 속함", "포함"], relates: ["관련", "관련", "관련"],
              has_goal: ["", "목표", "프로젝트"], recorded_in: ["기록", "기록된 목표", "여기서 기록됨"],
              recalled_in: ["회상", "회상된 목표", "여기서 회상됨"] }
    },
    ja: {
      v_overview: "概要", v_graph: "グラフ", v_knowledge: "ナレッジ", v_docs: "文書", v_goals: "目標",
      sub_overview: "群れが学んだこと、そしてそれが検証済みの作業に役立ったか。",
      sub_graph: "ナレッジ・目標・プロジェクトのつながり。ノードはドラッグでき、ダブルクリックで文書が開きます。",
      sub_knowledge: "すべてのナレッジとそのライフサイクル。信頼はモデルの言葉ではなく、検証された利用でのみ得られます。",
      sub_docs: "Lupus が書く Vault そのまま: 読み取り専用の Markdown、グラフのように相互リンク。",
      sub_goals: "プロジェクトごとの目標。月の形が各目標の状態です。",
      find_ph: "ナレッジ・目標・文書を検索", launcher: "何でも検索", palette_ph: "ナレッジ、目標、文書の名前を入力…",
      p_move: "移動", p_open: "開く", open: "開く", all: "すべて", language: "言語", theme: "ライト / ダーク",
      c_graph: "ナレッジグラフ", c_trust: "信頼", c_kinds: "種類別", c_goals: "最近の目標", c_helped: "最も役立ったナレッジ",
      c_docs: "Vault の文書",
      kpi_nodes: "ナレッジ", kpi_nodes_sub: "引退 {r}", kpi_trusted: "信頼済み", kpi_trusted_sub: "ユーザー確認または繰り返し使用",
      kpi_helped: "検証済み作業に貢献", kpi_helped_sub: "想起 {r} 回中 {h} 回", kpi_goals: "完了した目標",
      kpi_goals_sub: "進行 {a} · 待機 {w}", kpi_projects: "プロジェクト", kpi_projects_sub: "文書 {d} 件",
      trust_note: "{p}% 信頼済み", filter_ph: "タイトルや本文で絞り込み", any_status: "すべての状態", any_kind: "すべての種類",
      col_title: "タイトル", col_kind: "種類", col_status: "状態", col_project: "プロジェクト", shown: "{t} 件中 {n} 件",
      no_rows: "条件に合うナレッジがありません。", global: "グローバル", home: "ホーム", map: "マップ", goals_folder: "目標",
      nodes_folder: "ナレッジ", links: "つながり", show_in_graph: "グラフで表示", nothing_yet: "まだありません。",
      no_helped: "検証済みの作業で想起されたナレッジはまだありません。",
      zoom_in: "拡大", zoom_out: "縮小", fit: "画面に合わせる", unpin: "固定をすべて解除", pin: "置いた位置に固定",
      hint_move: "空間をドラッグして回転 · +/− で拡大 · クリックで詳細",
      all_projects: "すべてのプロジェクト", show_recalls: "想起リンク", show_retired: "引退ノード",
      recalled: "想起", helped: "役立った", unhelped: "役立たなかった", origin: "記録者", state: "状態",
      open_page: "文書を読む", back: "戻る", close: "閉じる", no_page: "この文書はまだ生成されていません。",
      generated: "{t} 生成", count: "ノード {n} · リンク {e}",
      none_title: "まだ記録されたナレッジがありません",
      none_text: "作業が終わると学んだことが記録され、lupus note-add で直接書くこともできます。その後 lupus graph をもう一度実行してください。",
      filtered_title: "この条件に合うノードがありません", filtered_text: "上の種類かプロジェクトを変えてみてください。",
      phase: { done: "完了", active: "進行中", wait: "待機", stop: "停止" },
      kind: { project: "プロジェクト", goal: "目標", lesson: "教訓", decision: "決定", fact: "事実", procedure: "手順", preference: "好み" },
      kinds_pl: { lesson: "教訓", decision: "決定", fact: "事実", procedure: "手順", preference: "好み" },
      status: { verified: "ユーザー確認", confirmed: "繰り返し使用", candidate: "未検証", retired: "引退" },
      status_long: { verified: "ユーザーが確認", confirmed: "何度も役立った", candidate: "まだ役立つと示されていない", retired: "もう想起されない" },
      link: { supersedes: ["置換", "置き換える", "置き換えられた"], derived_from: ["派生", "ここから派生", "派生元"],
              contradicts: ["矛盾", "矛盾", "矛盾"], part_of: ["部分", "ここに属する", "含む"], relates: ["関連", "関連", "関連"],
              has_goal: ["", "目標", "プロジェクト"], recorded_in: ["記録", "記録された目標", "ここで記録"],
              recalled_in: ["想起", "想起された目標", "ここで想起"] }
    },
    zh: {
      v_overview: "概览", v_graph: "图谱", v_knowledge: "知识", v_docs: "文档", v_goals: "目标",
      sub_overview: "狼群学到了什么，以及它是否帮助了经过验证的工作。",
      sub_graph: "知识、目标与项目如何相连。可拖动节点，双击打开文档。",
      sub_knowledge: "每条知识及其生命周期。信任只来自经过验证的使用，而不是模型的说法。",
      sub_docs: "Lupus 写出的 Vault：只读 Markdown，像图谱一样相互链接。",
      sub_goals: "按项目列出的目标。月相表示每个目标的状态。",
      find_ph: "搜索知识、目标、文档", launcher: "查找任何内容", palette_ph: "输入知识、目标或文档的名称…",
      p_move: "移动", p_open: "打开", open: "打开", all: "全部", language: "语言", theme: "浅色 / 深色",
      c_graph: "知识图谱", c_trust: "信任", c_kinds: "按类型", c_goals: "最近的目标", c_helped: "最有帮助的知识",
      c_docs: "Vault 文档",
      kpi_nodes: "知识", kpi_nodes_sub: "已停用 {r}", kpi_trusted: "受信任", kpi_trusted_sub: "用户确认或多次使用",
      kpi_helped: "帮助了已验证的工作", kpi_helped_sub: "{r} 次回忆中 {h} 次", kpi_goals: "已完成目标",
      kpi_goals_sub: "进行中 {a} · 等待 {w}", kpi_projects: "项目", kpi_projects_sub: "{d} 篇文档",
      trust_note: "{p}% 受信任", filter_ph: "按标题或内容筛选", any_status: "所有状态", any_kind: "所有类型",
      col_title: "标题", col_kind: "类型", col_status: "状态", col_project: "项目", shown: "{t} 条中的 {n} 条",
      no_rows: "没有符合条件的知识。", global: "全局", home: "首页", map: "地图", goals_folder: "目标",
      nodes_folder: "知识", links: "连接", show_in_graph: "在图谱中显示", nothing_yet: "暂无。",
      no_helped: "还没有知识在已验证的工作中被回忆。",
      zoom_in: "放大", zoom_out: "缩小", fit: "适应屏幕", unpin: "全部取消固定", pin: "固定在放下的位置",
      hint_move: "拖动空间旋转 · +/− 缩放 · 点击查看详情",
      all_projects: "所有项目", show_recalls: "回忆连接", show_retired: "已停用节点",
      recalled: "回忆", helped: "有帮助", unhelped: "没帮助", origin: "记录者", state: "状态",
      open_page: "阅读文档", back: "返回", close: "关闭", no_page: "该文档尚未生成。",
      generated: "生成于 {t}", count: "节点 {n} · 连接 {e}",
      none_title: "还没有记录任何知识",
      none_text: "工作结束时会记录学到的内容，也可以用 lupus note-add 直接写入。然后再次运行 lupus graph。",
      filtered_title: "没有符合这些条件的节点", filtered_text: "请更改上方的类型或项目。",
      phase: { done: "完成", active: "进行中", wait: "等待", stop: "已停止" },
      kind: { project: "项目", goal: "目标", lesson: "教训", decision: "决定", fact: "事实", procedure: "步骤", preference: "偏好" },
      kinds_pl: { lesson: "教训", decision: "决定", fact: "事实", procedure: "步骤", preference: "偏好" },
      status: { verified: "用户确认", confirmed: "多次使用", candidate: "未验证", retired: "已停用" },
      status_long: { verified: "已由你确认", confirmed: "多次提供帮助", candidate: "尚未证明有帮助", retired: "不再被回忆" },
      link: { supersedes: ["替代", "替代了", "被替代"], derived_from: ["派生", "派生自", "派生出"],
              contradicts: ["矛盾", "矛盾", "矛盾"], part_of: ["部分", "属于", "包含"], relates: ["相关", "相关", "相关"],
              has_goal: ["", "目标", "项目"], recorded_in: ["记录", "记录于", "在此记录"],
              recalled_in: ["回忆", "回忆于", "在此回忆"] }
    }
  };
  var chatWords = {"en": ["Discuss knowledge", "New conversation", "Ask about this knowledge…", "Send", "This session only", "Select a node or open a document to start a conversation.", "Ask for a summary, explanation, or application of the selected knowledge.", "Thinking…", "Conversation needs the local server. Run: lupus graph --serve --open", "Connection failed. Try sending again."], "ko": ["지식과 대화", "새 대화", "이 지식에 대해 질문하세요…", "보내기", "현재 세션에만 보관", "노드를 선택하거나 문서를 열면 그 내용을 바탕으로 대화할 수 있습니다.", "선택한 지식의 요약, 의미, 활용 방법을 물어보세요.", "답변을 생각하고 있습니다…", "대화 서버가 필요합니다. 실행: lupus graph --serve --open", "연결하지 못했습니다. 다시 보내 주세요."], "ja": ["知識と対話", "新しい会話", "この知識について質問…", "送信", "このセッションのみ", "ノードまたは文書を選んで会話を始めてください。", "選択した知識の要約、意味、活用方法を質問できます。", "回答を考えています…", "会話サーバーが必要です。実行: lupus graph --serve --open", "接続できませんでした。もう一度送信してください。"], "zh": ["与知识对话", "新对话", "询问这条知识…", "发送", "仅当前会话", "选择节点或打开文档即可开始对话。", "询问所选知识的摘要、含义或应用方法。", "正在思考…", "需要本地对话服务器。运行: lupus graph --serve --open", "连接失败，请重试。"]};
  Object.keys(chatWords).forEach(function(lang) { ["chat_title","chat_new","chat_ph","chat_send","chat_private","chat_select","chat_empty","chat_wait","chat_offline","chat_error"].forEach(function(key,i) { WORDS[lang][key] = chatWords[lang][i]; }); });
  var workspaceWords = {
    en:["Filters", "Fullscreen", "Exit fullscreen", "Documents", "Fullscreen is unavailable; the graph fills this window instead."],
    ko:["필터", "전체화면", "전체화면 종료", "문서 목록", "브라우저 전체화면을 사용할 수 없어 창 안에서 그래프를 확장했습니다."],
    ja:["フィルター", "全画面", "全画面を終了", "文書一覧", "全画面表示を利用できないため、ウィンドウ内でグラフを拡大しました。"],
    zh:["筛选", "全屏", "退出全屏", "文档列表", "无法使用浏览器全屏，已在窗口内展开图谱。"]
  };
  Object.keys(workspaceWords).forEach(function(lang) { ["graph_filters", "graph_fullscreen", "graph_fullscreen_exit", "document_list", "graph_fullscreen_fallback"].forEach(function(key,i) { WORDS[lang][key] = workspaceWords[lang][i]; }); });
  [["en","Display settings","Exit"],["ko","화면 설정","나가기"],["ja","表示設定","終了"],["zh","显示设置","退出"]].forEach(function(item) { WORDS[item[0]].view_settings=item[1]; WORDS[item[0]].fullscreen_exit_short=item[2]; });
  var W = WORDS.en;
  function say(key, values) {
    return String(W[key]).replace(/\{(\w)\}/g, function (_, name) { return values[name]; });
  }
  var FACE = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Hiragino Sans", "PingFang SC", "Segoe UI", sans-serif';
  var DISPLAY = 'Sora, ' + FACE;      // the bundled face (Latin only); other scripts fall through to the system
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  var SVG = "http://www.w3.org/2000/svg";
  function moon(ph) {        // the goal-state glyph: full, half, crescent or new moon, drawn in the state's colour
    var s = document.createElementNS(SVG, "svg"), c = document.createElementNS(SVG, "circle");
    s.setAttribute("viewBox", "0 0 16 16"); s.setAttribute("class", "moon-glyph"); s.setAttribute("aria-hidden", "true");
    var colour = ph === "done" ? "var(--ok)" : ph === "active" ? "var(--accent)" : ph === "wait" ? "var(--warn)" : "var(--soft)";
    c.setAttribute("cx", "8"); c.setAttribute("cy", "8"); c.setAttribute("r", "6.2");
    c.setAttribute("fill", ph === "done" ? colour : "none"); c.setAttribute("stroke", colour); c.setAttribute("stroke-width", "1.6");
    s.appendChild(c);
    if (ph === "active" || ph === "wait") {
      var p = document.createElementNS(SVG, "path");
      p.setAttribute("d", ph === "active" ? "M8 1.8a6.2 6.2 0 0 1 0 12.4Z" : "M8 1.8a6.2 6.2 0 0 1 0 12.4a4.2 6.2 0 0 0 0-12.4Z");
      p.setAttribute("fill", colour);
      s.appendChild(p);
    }
    return s;
  }

  var byId = {}, scopeName = { "": null };
  data.nodes.forEach(function (n) { byId[n.id] = n; });
  data.scopes.forEach(function (s) { scopeName[s[0]] = s[1]; });
  function projectOf(n) { return n.scope ? scopeName[n.scope] || n.scope : W.global; }
  var notes = data.nodes.filter(function (n) { return NOTES.indexOf(n.kind) >= 0; });
  var goalNodes = data.nodes.filter(function (n) { return n.kind === "goal"; });
  var pages = data.pages || {}, pageOf = {};
  data.nodes.forEach(function (n) { if (n.page) { pageOf[n.page] = n.id; } });

  // ---- persisted view state (pinned positions, hidden kinds, theme, language, view); best effort only
  var KEY = "lupus-viewer-v2", saved = {};
  try { saved = JSON.parse(window.localStorage.getItem(KEY) || "{}") || {}; } catch (e) { saved = {}; }
  saved.pins = saved.pins || {};
  saved.hidden = saved.hidden || {};
  function persist() { try { window.localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* file:// may refuse */ } }

  // ---- colours come from the stylesheet, so the canvas and the page can never disagree
  var C = {};
  function readTheme() {
    var dark = saved.theme ? saved.theme === "dark" : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (dark) { root.setAttribute("data-dark", ""); } else { root.removeAttribute("data-dark"); }
    var css = window.getComputedStyle(root), get = function (name) { return css.getPropertyValue(name).trim(); };
    C = { ground: get("--ground"), ink: get("--ink"), muted: get("--muted"), soft: get("--soft"), faint: get("--faint"),
          edge: get("--edge"), accent: get("--accent"), danger: get("--danger"), kind: {} };
    KINDS.forEach(function (k) { C.kind[k] = get("--k-" + k); });
  }
  readTheme();
  function dark() { return root.hasAttribute("data-dark"); }
  function tint(hex, alpha) {            // "#2a8fb8" -> rgba(..., alpha); anything else is returned as it is
    var m = /^#([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) { return hex; }
    var v = parseInt(m[1], 16);
    return "rgba(" + (v >> 16) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + alpha + ")";
  }

  // =========================================================== views
  var VIEWS = ["overview", "graph", "knowledge", "docs", "goals"], current = null;
  function go(view, quiet) {
    if (VIEWS.indexOf(view) < 0) { view = "overview"; }
    if (view !== "graph" && graphExpanded) { exitGraphFullscreen(false); }
    var changed = view !== current;
    current = view; document.body.classList.toggle("space-view", view === "graph"); saved.view = view; persist();
    VIEWS.forEach(function (v) {
      var section = $("view-" + v);
      section.hidden = v !== view;
      if (v === view && changed && !quiet && !calm) { section.classList.remove("enter"); void section.offsetWidth; section.classList.add("enter"); }
    });
    Array.prototype.forEach.call(document.querySelectorAll(".rb[data-view]"), function (b) {
      if (b.getAttribute("data-view") === view) { b.setAttribute("aria-current", "page"); } else { b.removeAttribute("aria-current"); }
    });
    document.title = "Lupus · " + W["v_" + view];
    $("v-title").textContent = W["v_" + view];
    $("v-sub").textContent = W["sub_" + view];
    if (changed) { window.scrollTo(0, 0); }
    if (view === "overview" && changed) { window.requestAnimationFrame(mini); }
    if (view === "graph") { ensureGraph(); }
    if (view === "docs" && !reading) { read("index.md"); }
    if (view !== "graph" && view !== "knowledge") { shut(); }
    syncChat();
    if (window.location.hash !== "#" + view) { try { window.history.replaceState(null, "", "#" + view); } catch (e) { /* file: */ } }
  }
  Array.prototype.forEach.call(document.querySelectorAll(".rb[data-view], [data-go]"), function (b) {
    b.addEventListener("click", function () { go(b.getAttribute("data-view") || b.getAttribute("data-go")); });
  });

  // Graph fullscreen keeps the original scene, selection and contextual chat alive.
  var graphExpanded = false, graphScroll = 0;
  function fullscreenLabels() {
    var label = graphExpanded ? W.graph_fullscreen_exit : W.graph_fullscreen;
    $("graph-fullscreen").setAttribute("aria-pressed", String(graphExpanded));
    $("graph-fullscreen").title = label; $("graph-fullscreen").setAttribute("aria-label", label);
    $("fullscreen-label").textContent = graphExpanded && window.innerWidth <= 600 ? W.fullscreen_exit_short : label;
  }
  function setGraphExpanded(value, restoreFocus) {
    graphExpanded = value;
    document.body.classList.toggle("graph-fullscreen", value); root.classList.toggle("graph-fullscreen", value);
    fullscreenLabels();
    if (!value) { window.scrollTo(0, graphScroll); if (restoreFocus) { $("graph-fullscreen").focus({preventScroll:true}); } }
    window.requestAnimationFrame(function() { if (network) { network.redraw(); } });
  }
  function exitGraphFullscreen(restoreFocus) {
    setGraphExpanded(false, restoreFocus);
    if (document.fullscreenElement) { document.exitFullscreen().catch(function() {}); }
  }
  async function toggleGraphFullscreen() {
    if (graphExpanded) { exitGraphFullscreen(true); return; }
    graphScroll = window.scrollY; closePalette();
    setGraphExpanded(true, false); $("fullscreen-status").textContent = "";
    if (root.requestFullscreen) {
      try { await root.requestFullscreen(); }
      catch (error) { $("fullscreen-status").textContent = W.graph_fullscreen_fallback; }
    } else { $("fullscreen-status").textContent = W.graph_fullscreen_fallback; }
    $("graph-fullscreen").focus({preventScroll:true});
  }
  window.addEventListener("resize", fullscreenLabels);
  function updateTopSurface() { $("top").classList.toggle("scrolled", window.scrollY > 8); }
  window.addEventListener("scroll", updateTopSurface, {passive:true});
  updateTopSurface();
  $("settings-toggle").addEventListener("click", function() {
    var expanded = this.getAttribute("aria-expanded") !== "true";
    if (expanded && !$("chat").hidden) { closeChat(); this.focus(); }
    this.setAttribute("aria-expanded", String(expanded)); $("rail-settings").classList.toggle("settings-open", expanded);
  });
  $("graph-fullscreen").addEventListener("click", toggleGraphFullscreen);
  document.addEventListener("fullscreenchange", function() { if (!document.fullscreenElement && graphExpanded) { setGraphExpanded(false, true); } });
  $("filter-toggle").addEventListener("click", function() { var expanded = this.getAttribute("aria-expanded") !== "true"; this.setAttribute("aria-expanded", String(expanded)); $("graph-options").hidden = !expanded; });
  $("tree-toggle").addEventListener("click", function() { var expanded = this.getAttribute("aria-expanded") !== "true"; this.setAttribute("aria-expanded", String(expanded)); $("tree").classList.toggle("tree-open", expanded); });
  new ResizeObserver(function() {
    if (!graphExpanded) { root.style.setProperty("--workspace-top", Math.ceil($("top").offsetTop + $("top").offsetHeight + 16) + "px"); }
  }).observe($("top"));

  // =========================================================== language
  function speak(lang) {
    W = WORDS[lang] || WORDS.en;
    root.setAttribute("lang", lang === "zh" ? "zh-CN" : lang);
    $("lang").value = lang;
    document.title = "Lupus · " + W.v_overview;
    [["data-t", "textContent"], ["data-t-title", "title"], ["data-t-placeholder", "placeholder"], ["data-t-aria", "aria-label"],
     ["data-t-tip", "data-tip"]].forEach(function (rule) {
      Array.prototype.forEach.call(document.querySelectorAll("[" + rule[0] + "]"), function (node) {
        var text = W[node.getAttribute(rule[0])];
        if (rule[1] === "textContent") { node.textContent = text; } else { node.setAttribute(rule[1], text); }
        if (rule[1] !== "textContent") { node.setAttribute("aria-label", text); }
      });
    });
    $("stamp").textContent = say("generated", { t: data.generated_at }); fullscreenLabels();
  }

  // =========================================================== overview
  function sum(list, key) { return list.reduce(function (t, n) { return t + (n[key] || 0); }, 0); }
  function renderOverview() {
    var live = notes.filter(function (n) { return n.status !== "retired"; });
    var count = {}; STATUSES.forEach(function (s) { count[s] = 0; });
    notes.forEach(function (n) { count[n.status] = (count[n.status] || 0) + 1; });
    var trusted = count.verified + count.confirmed, recalls = sum(notes, "recalled"), helped = sum(notes, "helped");
    var phases = { done: 0, active: 0, wait: 0, stop: 0 };
    goalNodes.forEach(function (g) { phases[phase(g.state)] += 1; });
    var pct = function (a, b) { return b ? Math.round(100 * a / b) : 0; };
    var cards = [
      ["kpi_nodes", live.length, "", say("kpi_nodes_sub", { r: count.retired }), "var(--t-a)",
       "M12 3.5 20.5 8 12 12.5 3.5 8ZM3.5 12l8.5 4.5 8.5-4.5M3.5 16l8.5 4.5 8.5-4.5"],
      ["kpi_trusted", trusted, live.length ? " / " + live.length : "", W.kpi_trusted_sub, "var(--t-b)",
       "M12 3.5 19 6.5v5.2c0 4.2-3 7.4-7 8.8-4-1.4-7-4.6-7-8.8V6.5ZM9 12l2.2 2.2L15.5 10"],
      ["kpi_helped", pct(helped, recalls), "%", say("kpi_helped_sub", { h: helped, r: recalls }), "var(--t-c)",
       "M4 15.5 9 10.5l3.5 3.5L20 6.5M15 6.5h5v5"],
      ["kpi_goals", phases.done, goalNodes.length ? " / " + goalNodes.length : "", say("kpi_goals_sub", { a: phases.active, w: phases.wait }),
       "var(--t-d)", "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM8.5 12.2l2.4 2.4 4.6-4.8"],
      ["kpi_projects", data.scopes.length, "", say("kpi_projects_sub", { d: Object.keys(pages).length }), "var(--t-e)",
       "M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"]
    ];
    var box = $("kpis"); clear(box);
    cards.forEach(function (c, i) {
      var card = el("article", "card glass lift kpi"), label = el("div", "label"), badge = el("span", "badge"), value = el("div", "value");
      card.style.setProperty("--i", String(i)); card.style.setProperty("--tint", c[4]);
      var icon = document.createElementNS(SVG, "svg"), path = document.createElementNS(SVG, "path");
      icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("class", "i"); path.setAttribute("d", c[5]); icon.appendChild(path);
      badge.appendChild(icon); label.appendChild(badge); label.appendChild(el("span", "", W[c[0]]));
      value.textContent = String(c[1]);
      if (c[2]) { value.appendChild(el("small", "", c[2])); }
      card.appendChild(label); card.appendChild(value); card.appendChild(el("div", "sub", c[3]));
      box.appendChild(card);
    });

    // trust lifecycle
    var bar = $("trust-bar"), legend = $("trust-legend"); clear(bar); clear(legend);
    var tone = { verified: "var(--ink)", confirmed: "var(--accent)", candidate: "var(--faint)", retired: "var(--line-strong)" };
    STATUSES.forEach(function (s) {
      var seg = el("i"); seg.style.background = tone[s]; seg.style.flexGrow = String(count[s]); seg.title = W.status[s] + ": " + count[s];
      bar.appendChild(seg);
      var cell = el("div"), name = el("span"), dot = el("i", "dot" + (s === "candidate" ? " hollow" : ""));
      dot.style.background = tone[s]; name.appendChild(dot); name.appendChild(document.createTextNode(W.status[s]));
      cell.appendChild(el("b", "", count[s])); cell.appendChild(name); cell.title = W.status_long[s];
      legend.appendChild(cell);
    });
    $("trust-note").textContent = notes.length ? say("trust_note", { p: pct(trusted, live.length) }) : "";

    // by kind
    var meters = $("kind-meters"); clear(meters);
    var most = Math.max.apply(null, NOTES.map(function (k) { return live.filter(function (n) { return n.kind === k; }).length; }).concat([1]));
    NOTES.forEach(function (k) {
      var n = live.filter(function (x) { return x.kind === k; }).length, row = el("div", "meter"), name = el("span"), dot = el("i", "dot");
      var track = el("div"), fill = el("em");
      dot.style.background = "var(--k-" + k + ")"; name.appendChild(dot); name.appendChild(document.createTextNode(W.kinds_pl[k]));
      fill.style.width = (100 * n / most) + "%"; fill.style.color = "var(--k-" + k + ")"; track.appendChild(fill);
      row.appendChild(name); row.appendChild(track); row.appendChild(el("b", "", n));
      meters.appendChild(row);
    });

    // recent goals, most helpful notes, documents
    fill($("goal-rows"), goalNodes.slice(-5).reverse(), function (g) {
      var b = el("button", "row"); b.type = "button";
      b.appendChild(moon(phase(g.state))); b.appendChild(el("span", "", g.title)); b.appendChild(el("small", "", projectOf(g)));
      b.addEventListener("click", function () { if (g.page) { go("docs"); read(g.page); } });
      return b;
    }, W.nothing_yet);
    var useful = notes.filter(function (n) { return n.recalled; }).sort(function (a, b) {
      return (b.helped - a.helped) || (b.recalled - a.recalled);
    }).slice(0, 5);
    fill($("helped-rows"), useful, function (n) {
      var b = el("button", "row"), dot = el("i", "dot"); b.type = "button";
      dot.style.background = "var(--k-" + n.kind + ")";
      b.appendChild(dot); b.appendChild(el("span", "", n.title)); b.appendChild(el("small", "", n.helped + " / " + n.recalled));
      b.title = W.helped + " / " + W.recalled;
      b.addEventListener("click", function () { open(n.id); });
      return b;
    }, W.no_helped);
    var docs = Object.keys(pages).filter(function (p) { return /(^|\/)(index|map)\.md$/.test(p); }).sort(function (a, b) {
      return (a === "index.md" ? -1 : b === "index.md" ? 1 : a < b ? -1 : 1);
    }).slice(0, 5);
    fill($("doc-rows"), docs, function (p) {
      var b = el("button", "row"), icon = document.createElementNS(SVG, "svg"), path = document.createElementNS(SVG, "path");
      b.type = "button"; icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("class", "i");
      path.setAttribute("d", "M6 3.5h8l4 4v13H6ZM14 3.5v4h4"); icon.appendChild(path);
      b.appendChild(icon); b.appendChild(el("span", "", pageTitle(p))); b.appendChild(el("small", "", p.replace(/\.md$/, "")));
      b.addEventListener("click", function () { go("docs"); read(p); });
      return b;
    }, W.nothing_yet);
    mini();
  }
  function fill(list, items, make, empty) {
    clear(list);
    if (!items.length) { list.appendChild(el("li", "empty", empty)); return; }
    items.forEach(function (item) { var li = el("li"); li.appendChild(make(item)); list.appendChild(li); });
  }

  // =========================================================== the graph (shared node and edge styling)
  var nodes = null, edges = null, network = null, miniNet = null, neighbours = {}, shown = [];
  var focus = null, selected = null, labelPx = 12, edgePx = 10, arrived = 0, quiet = {};
  function size(n) {
    if (n.kind === "project") { return 17; }
    if (n.kind === "goal") { return 11.5; }
    return 9 + Math.min(9, 1.5 * (n.recalled || 0));
  }
  function lit(n) { return !focus || n.id === focus || !!(neighbours[focus] && neighbours[focus][n.id]); }
  function visNode(n, small) {
    var on = lit(n) && n.status !== "retired", hue = C.kind[n.kind], hollow = n.status === "candidate";
    var fill = hollow ? C.ground : on ? hue : C.faint, rim = !on ? C.faint : n.status === "verified" ? C.ink : hue;
    var node = {
      id: n.id, label: small ? "" : n.label, shape: SHAPE[n.kind] || "dot", size: small ? size(n) * 1.05 : size(n),
      color: { background: fill, border: rim, highlight: { background: hollow ? C.ground : hue, border: C.ink },
               hover: { background: hollow ? C.ground : hue, border: C.ink } },
      borderWidth: n.status === "verified" ? 2.5 : hollow ? 1.5 : 1, borderWidthSelected: 2.5,
      shapeProperties: { borderDashes: hollow ? [3, 3] : false },
      // the label carries a halo in the ground colour, so a line passing behind it never strikes it through
      font: { color: on ? C.ink : C.faint, size: !small && quiet[n.id] && !(focus && lit(n)) ? 0 : labelPx, face: FACE,
              strokeWidth: 4, strokeColor: C.ground, vadjust: 3 }
    };
    var pin = saved.pins[n.id];
    if (pin && !small) { node.x = pin[0]; node.y = pin[1]; node.z = pin[2] || 0; node.fixed = true; node.borderWidth = 3.5; }
    return node;
  }
  function visEdge(e, i, small) {
    var soft = e.type === "recalled_in" || e.type === "recorded_in" || e.type === "has_goal";
    var on = lit(byId[e.from]) && lit(byId[e.to]);
    var near = !small && focus && (e.from === focus || e.to === focus);      // names appear only around the node being read
    return {
      id: i, from: e.from, to: e.to, type: e.type, label: near ? W.link[e.type][0] : "", dashes: e.type === "recalled_in" ? [4, 4] : false,
      arrows: { to: { enabled: !small && (!soft || e.type === "recorded_in"), scaleFactor: 0.5 } },
      color: { color: !on ? C.faint : e.type === "contradicts" ? C.danger : near ? C.muted : C.edge, highlight: C.accent, hover: C.accent },
      font: { size: edgePx, color: C.muted, face: FACE, strokeWidth: 4, strokeColor: C.ground, align: "horizontal" },
      width: soft ? 1 : 1.6, smooth: false
    };
  }
  function physics() {
    return { enabled: true, solver: "barnesHut", stabilization: false, minVelocity: 0.05, timestep: 0.45,
             barnesHut: { gravitationalConstant: -7200, springLength: 165, springConstant: 0.035, centralGravity: 0.18,
                          damping: 0.32, avoidOverlap: 0.35 } };
  }

  // the overview's small graph: same data, settled once, then still
  function mini() {
    if (current && current !== "overview") { return; }
    var live = data.nodes.filter(function (n) { return n.status !== "retired"; }), keep = {};
    live.forEach(function (n) { keep[n.id] = true; });
    $("mini-empty").hidden = live.length > 0;
    if (!live.length) { return; }
    // Light is earned: only knowledge that verified work confirmed glows. Names go on the projects
    // and the three notes recalled most, so the card reads without hovering.
    var named = {};
    live.filter(function (n) { return n.kind !== "project" && n.kind !== "goal" && n.recalled; })
      .sort(function (a, b) { return b.recalled - a.recalled; }).slice(0, 3)
      .concat(live.filter(function (n) { return n.kind === "project"; }))
      .forEach(function (n) { named[n.id] = true; });
    var mn = new vis.DataSet(live.map(function (n) {
      var node = visNode(n, true), trusted = n.status === "confirmed" || n.status === "verified";
      node.shadow = trusted ? { enabled: true, color: tint(C.kind[n.kind], dark() ? 0.95 : 0.55), size: 16, x: 0, y: 2 }
                            : { enabled: false };
      if (named[n.id]) {
        node.label = n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label;
        node.font = { color: C.ink, size: 11.5, face: DISPLAY, strokeWidth: 4, strokeColor: C.ground, vadjust: 2 };
      }
      return node;
    }));
    var me = new vis.DataSet(data.edges.map(function (e, i) { return [e, i]; }).filter(function (p) {
      return keep[p[0].from] && keep[p[0].to];
    }).map(function (p) { return visEdge(p[0], p[1], true); }));
    if (miniNet) { miniNet.destroy(); }
    miniNet = new vis.Network($("mini"), { nodes: mn, edges: me }, {
      physics: { solver: "barnesHut", stabilization: { iterations: 260, fit: true },
                 barnesHut: { gravitationalConstant: -3800, springLength: 110, springConstant: 0.04, centralGravity: 0.3, avoidOverlap: 0.3 } },
      interaction: { dragNodes: false, dragView: false, zoomView: false, hover: true, tooltipDelay: 99999 }
    });
    miniNet.once("stabilized", function () {
      miniNet.setOptions({ physics: false }); miniNet.fit();
      miniNet.moveTo({ scale: miniNet.getScale() * 0.92 });
      // fitting shrinks everything drawn on the canvas: keep the names and the glow at screen size
      var scale = miniNet.getScale(), boxes = [], pos = miniNet.getPositions(), shown = {};
      // projects first, then the most recalled; a name that would overlap one already placed is left off
      Object.keys(named).sort(function (a, b) {
        return (byId[a].kind === "project" ? -1e6 : -byId[a].recalled) - (byId[b].kind === "project" ? -1e6 : -byId[b].recalled);
      }).forEach(function (id) {
        var at = miniNet.canvasToDOM(pos[id]), w = Math.min(22, byId[id].label.length) * 7, box = [at.x - w / 2, at.y + 8, at.x + w / 2, at.y + 26];
        if (boxes.every(function (o) { return box[2] < o[0] || box[0] > o[2] || box[3] < o[1] || box[1] > o[3]; })) { boxes.push(box); shown[id] = true; }
      });
      mn.update(mn.get().map(function (node) {
        var out = { id: node.id };
        if (named[node.id] && !shown[node.id]) { out.label = ""; }
        if (shown[node.id]) { out.font = Object.assign({}, node.font, { size: 12 / scale, strokeWidth: 4 / scale, vadjust: 3 / scale }); }
        if (node.shadow.enabled) { out.shadow = Object.assign({}, node.shadow, { size: (dark() ? 26 : 18) / scale, y: 2 / scale }); }
        return out;
      }));
    });
    miniNet.on("click", function (p) { if (p.nodes[0]) { go("graph"); select(p.nodes[0], true); } });
  }

  // the main graph is created the first time its view is shown, so it measures a visible box
  var chips = {};
  function ensureGraph() {
    if (network) { network.redraw(); return; }
    nodes = new vis.DataSet(); edges = new vis.DataSet();
    [["", W.all_projects]].concat(data.scopes).forEach(function (s) {
      var o = el("option", "", s[1]); o.value = s[0]; $("scope").appendChild(o);
    });
    KINDS.forEach(function (k) {
      var b = el("button"), dot = el("i", "dot"), name = el("span"), count = el("small");
      b.type = "button"; dot.style.background = "var(--k-" + k + ")";
      b.appendChild(dot); b.appendChild(name); b.appendChild(count);
      b.setAttribute("aria-pressed", String(!saved.hidden[k]));
      b.addEventListener("click", function () {
        saved.hidden[k] = !saved.hidden[k]; persist();
        b.setAttribute("aria-pressed", String(!saved.hidden[k]));
        rebuild();
      });
      chips[k] = [b, count, name]; $("kinds").appendChild(b);
    });
    relabel();
    $("pin").checked = !!saved.pinOnDrag;
    try { if (window.LupusSpace) { network = window.LupusSpace($("graph"), {nodes:nodes, edges:edges}, data.nodes, calm); } }
    catch (error) { $("graph").replaceChildren(); console.warn("3D unavailable; using the 2D graph", error); }
    if (!network) network = new vis.Network($("graph"), { nodes: nodes, edges: edges }, {
      physics: physics(),
      interaction: { hover: true, dragNodes: true, dragView: true, zoomView: true, hideEdgesOnDrag: false, tooltipDelay: 99999 },
      nodes: { chosen: true }, edges: { chosen: true }
    });
    // ---- the selected node wears the crescent of the Lupus mark: it swings in and settles
    network.on("afterDrawing", function (ctx) {
      if (network.isSpace || !selected || !nodes.get(selected)) { return; }
      var at = network.getPositions([selected])[selected], t = calm ? 1 : Math.min(1, (window.performance.now() - arrived) / 620);
      var ease = 1 - Math.pow(1 - t, 4), r = size(byId[selected]) * (SHAPE[byId[selected].kind] ? 1.25 : 1) + 7 + 10 * (1 - ease);
      var turn = -2.2 + 1.5 * ease, thick = 3.2, lean = 2.7;
      ctx.save(); ctx.globalAlpha = ease; ctx.fillStyle = C.accent; ctx.beginPath();
      ctx.arc(at.x, at.y, r, 0, 2 * Math.PI);
      ctx.arc(at.x + lean * Math.cos(turn), at.y + lean * Math.sin(turn), r - thick, 0, 2 * Math.PI, true);
      ctx.fill("evenodd"); ctx.restore();
      if (t < 1) { window.requestAnimationFrame(function () { network.redraw(); }); }
    });
    network.on("hoverNode", function (p) { if (!selected) { focus = p.node; repaint(); } });
    network.on("blurNode", function () { if (!selected) { focus = null; repaint(); } });
    network.on("click", function (p) { select(p.nodes[0] || null, false); });
    network.on("doubleClick", function (p) { var n = byId[p.nodes[0]]; if (n && n.page) { go("docs"); read(n.page); } });
    network.on("dragStart", function (p) {
      if (p.nodes.length) { nodes.update(p.nodes.map(function (id) { return { id: id, fixed: false }; })); }
    });
    network.on("dragEnd", function (p) {
      if (!p.nodes.length || !$("pin").checked) { return; }
      var at = network.getPositions(p.nodes);
      p.nodes.forEach(function (id) {
        saved.pins[id] = [Math.round(at[id].x), Math.round(at[id].y), Math.round(at[id].z || 0)];
        nodes.update({ id: id, x: at[id].x, y: at[id].y, z: at[id].z || 0, fixed: true, borderWidth: 3.5 });
      });
      persist();
    });
    // labels fade out when zoomed far out, like a map; restyle only when a threshold is crossed
    network.on("zoom", relabelSize);
    network.on("animationFinished", relabelSize);
    ["scope", "retired", "recalls"].forEach(function (id) { $(id).addEventListener("change", rebuild); });
    $("pin").addEventListener("change", function () { saved.pinOnDrag = this.checked; persist(); });
    $("unpin").addEventListener("click", function () {
      saved.pins = {}; persist();
      nodes.update(nodes.getIds().map(function (id) { return { id: id, fixed: false }; })); repaint();
    });
    $("fit").addEventListener("click", function () { fitAll(); });
    $("zoom-in").addEventListener("click", function () { zoom(1.35); });
    $("zoom-out").addEventListener("click", function () { zoom(1 / 1.35); });
    rebuild();
    var touched = false;
    ["dragStart", "zoom", "click"].forEach(function (what) { network.on(what, function () { touched = true; }); });
    window.setTimeout(function () { if (!touched) { fitAll(true); } }, 900);
    // frame the graph again once it has spread out, unless the user has already taken the camera
    window.setTimeout(function () { if (!touched) { fitAll(); } }, 2600);
    network.once("stabilized", function () { if (!touched) { fitAll(); } });
    network.on("stabilized", tidySoon); network.on("zoom", tidySoon); network.on("dragEnd", tidySoon);
    window.setTimeout(declutter, 3200);
    window.lupusGraph = { network: network, nodes: nodes, select: select };   // handle for automated UI checks
  }
  function relabel() {
    if (!network && !Object.keys(chips).length) { return; }
    KINDS.forEach(function (k) { if (chips[k]) { chips[k][2].textContent = W.kind[k]; } });
    if ($("scope").options.length) { $("scope").options[0].textContent = W.all_projects; }
  }
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
    if (!network) { return; }
    nodes.update(nodes.getIds().map(function (id) { var v = visNode(byId[id]); delete v.x; delete v.y; delete v.z; return v; }));
    edges.update(shown.map(function (i) { return visEdge(data.edges[i], i); }));
  }
  // edge ids are indexes into data.edges, so restyling can find its source record
  function rebuild() {
    var keep = visible(), showRecalls = $("recalls").checked;
    neighbours = {}; shown = [];
    nodes.clear(); edges.clear();
    if (selected && !keep[selected]) { selected = null; focus = null; network.unselectAll(); shut(); }
    nodes.add(data.nodes.filter(function (n) { return keep[n.id]; }).map(function (n) { return visNode(n); }));
    data.edges.forEach(function (e, i) {
      if (!(keep[e.from] && keep[e.to] && (showRecalls || e.type !== "recalled_in"))) { return; }
      (neighbours[e.from] = neighbours[e.from] || {})[e.to] = true;
      (neighbours[e.to] = neighbours[e.to] || {})[e.from] = true;
      shown.push(i);
    });
    edges.add(shown.map(function (i) { return visEdge(data.edges[i], i); }));
    $("g-empty").hidden = nodes.length > 0;
    if (!nodes.length) {
      var none = !data.nodes.length;
      $("g-empty-title").textContent = none ? W.none_title : W.filtered_title;
      $("g-empty-text").textContent = none ? W.none_text : W.filtered_text;
    }
  }
  // labels that would overlap on screen: the more important node keeps its label (selected, then
  // projects, goals, trusted and often-recalled notes); the other shows it again on hover or selection
  var tidyTimer = null;
  var measure = document.createElement("canvas").getContext("2d");
  function declutter() {
    if (!network || network.isSpace || !labelPx) { return; }
    var ids = nodes.getIds(), at = network.getPositions(ids), scale = network.getScale(), next = {};
    var px = labelPx * scale, kept = [];
    measure.font = px + "px " + FACE;
    var rank = function (n) {
      return (n.id === selected ? 1e6 : 0) + ({ project: 5e4, goal: 4e4 }[n.kind] || 0) +
        ({ verified: 3e3, confirmed: 2e3 }[n.status] || 0) + 10 * (n.recalled || 0);
    };
    // every node's disc is an obstacle a label may not cover; then labels in order of importance
    var discs = ids.map(function (id) {
      var p = network.canvasToDOM(at[id]), r = size(byId[id]) * scale + 2;
      return { id: id, l: p.x - r, r: p.x + r, t: p.y - r, b: p.y + r };
    });
    var hits = function (box, own) {
      return kept.some(function (k) { return box.l < k.r && box.r > k.l && box.t < k.b && box.b > k.t; }) ||
        discs.some(function (d) { return d.id !== own && box.l < d.r && box.r > d.l && box.t < d.b && box.b > d.t; });
    };
    ids.map(function (id) { return byId[id]; }).sort(function (a, b) { return rank(b) - rank(a); }).forEach(function (n) {
      var p = network.canvasToDOM(at[n.id]), w = measure.measureText(n.label).width + 6, top = p.y + size(n) * scale + 2;
      var box = { l: p.x - w / 2, r: p.x + w / 2, t: top, b: top + px + 4 };
      if (hits(box, n.id)) { next[n.id] = true; } else { kept.push(box); }
    });
    var changed = Object.keys(next).length !== Object.keys(quiet).length ||
      Object.keys(next).some(function (id) { return !quiet[id]; });
    quiet = next;
    if (changed) { repaint(); }
  }
  function tidySoon() { window.clearTimeout(tidyTimer); tidyTimer = window.setTimeout(declutter, 180); }

  function relabelSize() {
    if (network.isSpace) { return; }
    var scale = network.getScale(), a = scale < 0.3 ? 0 : Math.round(Math.max(12, 11.5 / scale)), b = scale < 0.6 ? 0 : Math.round(Math.max(10, 10 / scale));
    if (a !== labelPx || b !== edgePx) { labelPx = a; edgePx = b; repaint(); tidySoon(); }
  }

  function zoom(by) {
    network.moveTo({ scale: Math.min(4, Math.max(0.15, network.getScale() * by)),
                     animation: calm ? false : { duration: 220, easingFunction: "easeOutQuart" } });
  }
  // fit into the part of the canvas the filter bar (however many rows it wraps to) and the hint leave free
  function fitAll(still) {
    if (network.isSpace) { network.fit(); return; }
    network.fit({ animation: false });
    var box = $("graph").getBoundingClientRect(), bar = $("filters").getBoundingClientRect();
    var top = Math.max(0, bar.bottom - box.top) + 12, bottom = 44, free = Math.max(120, box.height - top - bottom);
    var scale = network.getScale() * Math.min(1, free / box.height) * 0.97, at = network.getViewPosition();
    var to = { scale: scale, position: { x: at.x, y: at.y - (top - bottom) / 2 / scale } };
    if (still || calm) { network.moveTo(to); relabelSize(); } else { to.animation = { duration: 420, easingFunction: "easeOutQuart" }; network.moveTo(to); }
  }
  function select(id, travel) {
    if (!network) { ensureGraph(); }
    selected = id && nodes.get(id) ? id : null; focus = selected;
    arrived = window.performance.now();
    if (selected) { network.selectNodes([selected]); show(selected); } else { network.unselectAll(); shut(); }
    repaint();
    if (selected && travel) {
      // centred in the part of the canvas the drawer leaves open
      network.focus(selected, { scale: Math.max(network.getScale(), 0.9),
                                animation: calm ? false : { duration: 480, easingFunction: "easeOutQuart" } });
    }
  }

  // =========================================================== the detail drawer
  var drawn = null;
  function show(id) {
    var n = byId[id], links = $("d-links");
    if (!n) { shut(); return; }
    drawn = id;
    $("drawer").classList.remove("shut"); document.body.classList.add("drawn");
    if (network && current === "graph" && nodes.get(id)) {      // the canvas narrowed: re-centre on the node
      window.setTimeout(function () { network.redraw(); network.focus(id, { scale: Math.max(network.getScale(), 0.9), animation: false }); }, 320);
    }
    $("d-dot").style.background = "var(--k-" + n.kind + ")";
    $("d-kind-text").textContent = W.kind[n.kind] + (n.status ? " · " + (W.status[n.status] || n.status) : "") +
      (n.kind !== "project" ? " · " + projectOf(n) : "");
    $("d-title").textContent = n.title;
    var body = n.state ? W.state + ": " + W.phase[phase(n.state)] + " (" + n.state + ")" : n.body;
    $("d-body").textContent = body || ""; $("d-body").hidden = !body;
    $("d-use").hidden = n.recalled === undefined;
    if (n.recalled !== undefined) {
      $("d-recalled").textContent = n.recalled; $("d-helped").textContent = n.helped; $("d-unhelped").textContent = n.unhelped;
    }
    $("d-meta").hidden = !n.origin;
    if (n.origin) { $("d-meta").textContent = W.origin + ": " + n.origin; }
    clear(links);
    data.edges.forEach(function (e) {
      var out = e.from === id;
      if (!out && e.to !== id) { return; }
      var other = byId[out ? e.to : e.from];
      if (!other) { return; }
      var li = el("li"), b = el("button", "row"), dot = el("i", "dot");
      b.type = "button"; b.title = other.title; dot.style.background = "var(--k-" + other.kind + ")";
      b.appendChild(dot); b.appendChild(el("span", "", other.title)); b.appendChild(el("small", "", W.link[e.type][out ? 1 : 2]));
      b.addEventListener("click", function () { open(other.id); });
      li.appendChild(b); links.appendChild(li);
    });
    links.hidden = !links.firstChild; $("d-links-title").hidden = links.hidden;
    $("d-page").hidden = !(n.page && pages[n.page]);
    $("d-scroll").scrollTop = 0;
    markRow(); syncChat();
  }
  function shut() { $("drawer").classList.add("shut"); document.body.classList.remove("drawn"); drawn = null; markRow(); syncChat(); }
  function open(id) {            // from anywhere: the graph when it shows this node, otherwise the knowledge list
    if (current === "graph" && nodes && nodes.get(id)) { select(id, true); return; }
    if (current !== "knowledge" && current !== "graph") { go("graph"); if (nodes.get(id)) { select(id, true); return; } }
    show(id);
  }
  $("d-close").addEventListener("click", function () { if (current === "graph") { select(null, false); } else { shut(); } });
  $("d-page").addEventListener("click", function () { if (drawn && byId[drawn].page) { var p = byId[drawn].page; go("docs"); read(p); } });

  // =========================================================== knowledge list
  var sortBy = "helped", ascending = false;
  function fillSelect(box, first, values, label) {
    clear(box);
    var o = el("option", "", first); o.value = ""; box.appendChild(o);
    values.forEach(function (v) { var opt = el("option", "", label(v)); opt.value = v; box.appendChild(opt); });
  }
  function renderKnowledge() {
    var keepStatus = $("k-status").value, keepKind = $("k-kind").value;
    fillSelect($("k-status"), W.any_status, STATUSES, function (s) { return W.status[s]; });
    fillSelect($("k-kind"), W.any_kind, NOTES, function (k) { return W.kinds_pl[k]; });
    $("k-status").value = keepStatus; $("k-kind").value = keepKind;
    listRows();
  }
  function listRows() {
    var q = $("k-filter").value.trim().toLowerCase(), st = $("k-status").value, kd = $("k-kind").value, body = $("k-rows");
    var rows = notes.filter(function (n) {
      return (!st || n.status === st) && (!kd || n.kind === kd) && (!q || (n.title + " " + n.body).toLowerCase().indexOf(q) >= 0);
    });
    var value = function (n) {
      return sortBy === "kind" ? W.kind[n.kind] : sortBy === "status" ? STATUSES.indexOf(n.status) : sortBy === "scope" ? projectOf(n)
        : sortBy === "title" ? n.title.toLowerCase() : n[sortBy] || 0;
    };
    rows.sort(function (a, b) {
      var x = value(a), y = value(b), d = x < y ? -1 : x > y ? 1 : 0;
      return (ascending ? d : -d) || (a.title < b.title ? -1 : 1);
    });
    clear(body);
    rows.forEach(function (n) {
      var tr = el("tr"), t = el("td"), wrap = el("div", "t"), dot = el("i", "dot");
      if (n.status === "candidate") { dot.style.boxShadow = "inset 0 0 0 1.8px var(--k-" + n.kind + ")"; }
      else { dot.style.background = "var(--k-" + n.kind + ")"; }
      wrap.appendChild(dot); wrap.appendChild(el("span", "", n.title)); t.appendChild(wrap); tr.appendChild(t);
      tr.appendChild(el("td", "", W.kind[n.kind]));
      var sc = el("td"), chip = el("span", "chip " + ({ verified: "done", confirmed: "active", retired: "stop" }[n.status] || ""), W.status[n.status]);
      chip.title = W.status_long[n.status]; sc.appendChild(chip); tr.appendChild(sc);
      tr.appendChild(el("td", "", projectOf(n)));
      tr.appendChild(el("td", "n", n.recalled)); tr.appendChild(el("td", "n", n.helped));
      tr.tabIndex = 0; tr.setAttribute("data-id", n.id);
      tr.addEventListener("click", function () { show(n.id); });
      tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { show(n.id); } });
      body.appendChild(tr);
    });
    $("k-empty").hidden = rows.length > 0;
    $("k-empty").textContent = notes.length ? W.no_rows : W.none_text;
    $("k-count").textContent = say("shown", { n: rows.length, t: notes.length });
    Array.prototype.forEach.call(document.querySelectorAll("#k-table th"), function (th) {
      if (th.getAttribute("data-sort") === sortBy) { th.setAttribute("aria-sort", ascending ? "ascending" : "descending"); }
      else { th.removeAttribute("aria-sort"); }
    });
    markRow();
  }
  function markRow() {
    Array.prototype.forEach.call(document.querySelectorAll("#k-rows tr"), function (tr) {
      tr.setAttribute("aria-selected", String(tr.getAttribute("data-id") === drawn));
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll("#k-table th"), function (th) {
    th.querySelector("button").addEventListener("click", function () {
      var key = th.getAttribute("data-sort");
      if (sortBy === key) { ascending = !ascending; } else { sortBy = key; ascending = key === "title" || key === "kind" || key === "scope"; }
      listRows();
    });
  });
  ["k-filter", "k-status", "k-kind"].forEach(function (id) { $(id).addEventListener(id === "k-filter" ? "input" : "change", listRows); });

  // =========================================================== documents: the vault as a tree, and the reading pane
  function pageTitle(p) {
    if (p === "index.md") { return W.home; }
    if (pageOf[p]) { return byId[pageOf[p]].title; }
    if (/(^|\/)map\.md$/.test(p)) { var pid = p.split("/")[1]; return p.indexOf("global/") === 0 ? W.global : (scopeName[pid] || pid) + " · " + W.map; }
    return p.split("/").pop().replace(/\.md$/, "");
  }
  var leaf = {};
  function renderTree() {
    var tree = $("tree"), top = el("ul", "tree"); clear(tree); leaf = {};
    function item(p) {
      var li = el("li"), b = el("button"), n = pageOf[p] ? byId[pageOf[p]] : null;
      b.type = "button";
      if (n && NOTES.indexOf(n.kind) >= 0) { var dot = el("i", "dot"); dot.style.background = "var(--k-" + n.kind + ")"; b.appendChild(dot); }
      else if (n && n.kind === "goal") { b.appendChild(moon(phase(n.state))); }
      b.appendChild(el("span", "", pageTitle(p)));
      b.addEventListener("click", function () { read(p); });
      leaf[p] = b; li.appendChild(b); return li;
    }
    function folder(name, count, children, openNow) {
      var li = el("li", "folder"), b = el("button"), ul = el("ul"), chev = document.createElementNS(SVG, "svg"), path = document.createElementNS(SVG, "path");
      chev.setAttribute("viewBox", "0 0 24 24"); chev.setAttribute("class", "i chev"); path.setAttribute("d", "m9 6 6 6-6 6"); chev.appendChild(path);
      b.type = "button"; b.appendChild(chev); b.appendChild(el("span", "", name)); b.appendChild(el("small", "", count));
      li.setAttribute("aria-expanded", String(openNow));
      b.addEventListener("click", function () { li.setAttribute("aria-expanded", String(li.getAttribute("aria-expanded") !== "true")); });
      children.forEach(function (c) { ul.appendChild(c); });
      li.appendChild(b); li.appendChild(ul); return li;
    }
    var keys = Object.keys(pages).sort();
    if (pages["index.md"]) { top.appendChild(item("index.md")); }
    var groups = [["global", W.global]].concat(data.scopes.map(function (s) { return ["projects/" + s[0], s[1]]; }));
    groups.forEach(function (g) {
      var mine = keys.filter(function (p) { return p.indexOf(g[0] + "/") === 0; });
      if (!mine.length) { return; }
      var kids = [], map = mine.filter(function (p) { return p === g[0] + "/map.md"; });
      map.forEach(function (p) { var li = item(p); leaf[p].querySelector("span").textContent = W.map; kids.push(li); });
      var goalsHere = mine.filter(function (p) { return p.indexOf(g[0] + "/goals/") === 0; });
      var notesHere = mine.filter(function (p) { return p.indexOf(g[0] + "/nodes/") === 0; });
      if (goalsHere.length) { kids.push(folder(W.goals_folder, goalsHere.length, goalsHere.map(item), true)); }
      if (notesHere.length) { kids.push(folder(W.nodes_folder, notesHere.length, notesHere.map(item), true)); }
      top.appendChild(folder(g[1], mine.length, kids, true));
    });
    if (!keys.length) { var none = el("li", "empty", W.none_text); top.appendChild(none); }
    tree.appendChild(top);
    if (reading) { markLeaf(reading); }
  }
  function markLeaf(p) {
    Object.keys(leaf).forEach(function (k) { if (k === p) { leaf[k].setAttribute("aria-current", "true"); } else { leaf[k].removeAttribute("aria-current"); } });
  }
  function resolve(from, rel) {
    var parts = from.split("/").slice(0, -1);
    rel.split("/").forEach(function (p) { if (p === "..") { parts.pop(); } else if (p && p !== ".") { parts.push(p); } });
    return parts.join("/");
  }
  // Vault pages are Markdown that Lupus generated; this reads the few constructs those pages use and
  // builds elements with textContent only, so nothing in a page can become markup.
  function inline(into, text, page) {
    var plain = "", i = 0, flush = function () { if (plain) { into.appendChild(document.createTextNode(plain)); plain = ""; } };
    var until = function (mark, from) {
      for (var at = from; at < text.length; at += 1) {
        if (text[at] === "\\") { at += 1; } else if (text.substr(at, mark.length) === mark) { return at; }
      }
      return -1;
    };
    while (i < text.length) {
      var ch = text[i], end, node;
      if (ch === "\\" && i + 1 < text.length) { plain += text[i + 1]; i += 2; continue; }
      if (text.substr(i, 4) === "&lt;") { plain += "<"; i += 4; continue; }
      if (text.substr(i, 2) === "**" && (end = until("**", i + 2)) > 0) {
        flush(); node = el("strong"); inline(node, text.slice(i + 2, end), page); into.appendChild(node); i = end + 2; continue;
      }
      if (ch === "`" && (end = text.indexOf("`", i + 1)) > 0) {
        flush(); into.appendChild(el("code", "", text.slice(i + 1, end))); i = end + 1; continue;
      }
      if (ch === "[" && (end = until("](", i + 1)) > 0) {
        var stop = until(")", end + 2);
        if (stop > 0) {
          var target = resolve(page, text.slice(end + 2, stop));
          flush();
          if (pages[target]) {
            node = el("a"); node.setAttribute("role", "link"); node.tabIndex = 0;
            (function (to) {
              node.addEventListener("click", function () { read(to); });
              node.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { read(to); } });
            })(target);
          } else { node = el("span"); }      // a link out of the vault is shown as its words only
          inline(node, text.slice(i + 1, end), page); into.appendChild(node); i = stop + 1; continue;
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
    var lines = markdown.split("\n"), list = null, table = null, i, line, node, first = true;
    clear(into);
    for (i = 0; i < lines.length; i += 1) {
      line = lines[i];
      if (!/^- /.test(line)) { list = null; }
      if (line[0] !== "|") { table = null; }
      if (!line.trim()) { continue; }
      if (/^#{1,2} /.test(line)) {
        node = el(line[1] === "#" ? "h2" : "h1"); inline(node, line.replace(/^#+ /, ""), page); into.appendChild(node);
      } else if (line[0] === ">") {
        node = el("blockquote"); inline(node, line.replace(/^> ?/, ""), page);
        if (first) { node.className = "note"; first = false; }      // every page opens with the same read-only notice
        into.appendChild(node);
      } else if (/^- /.test(line)) {
        if (!list) { list = el("ul"); into.appendChild(list); }
        node = el("li"); inline(node, line.slice(2), page); list.appendChild(node);
      } else if (line[0] === "|") {
        if (/^\|[-| ]+\|$/.test(line)) { continue; }
        var row = el("tr"), head = !table;
        if (!table) { var wrap = el("div", "table"); table = el("table"); wrap.appendChild(table); into.appendChild(wrap); }
        cells(line).forEach(function (text) { var cell = el(head ? "th" : "td"); inline(cell, text, page); row.appendChild(cell); });
        table.appendChild(row);
      } else {
        node = el("p"); inline(node, line, page); into.appendChild(node);
      }
    }
  }
  var reading = null, trail = [];
  function read(page, back) {
    if (current !== "docs") { go("docs", true); }
    if (!back && trail[trail.length - 1] !== page) { trail.push(page); }
    reading = page;
    $("r-path").textContent = page.replace(/\.md$/, "");
    if (pages[page]) { typeset($("r-body"), pages[page], page); } else { $("r-body").textContent = W.no_page; }
    $("r-back").disabled = trail.length < 2;
    $("r-node").hidden = !pageOf[page];
    $("r-body").scrollTop = 0;
    markLeaf(page); syncChat();
  }
  $("r-back").addEventListener("click", function () { if (trail.length > 1) { trail.pop(); read(trail[trail.length - 1], true); } });
  $("r-node").addEventListener("click", function () { if (reading && pageOf[reading]) { go("graph"); select(pageOf[reading], true); } });

  // =========================================================== goals by project
  function renderGoals() {
    var box = $("projects"); clear(box);
    data.scopes.forEach(function (s, i) {
      var mine = goalNodes.filter(function (g) { return g.scope === s[0]; });
      var card = el("article", "card glass lift"), h = el("h2"), list = el("ul", "rows");
      card.style.setProperty("--i", String(i));
      var done = mine.filter(function (g) { return phase(g.state) === "done"; }).length;
      h.appendChild(el("span", "", s[1])); h.appendChild(el("small", "", done + " / " + mine.length + " " + W.phase.done));
      card.appendChild(h);
      if (!mine.length) { list.appendChild(el("li", "empty", W.nothing_yet)); }
      mine.slice().reverse().forEach(function (g) {
        var li = el("li"), b = el("button", "goal"), recorded = 0, recalledHere = 0;
        data.edges.forEach(function (e) {
          if (e.to === g.id && e.type === "recorded_in") { recorded += 1; }
          if (e.to === g.id && e.type === "recalled_in") { recalledHere += 1; }
        });
        b.type = "button";
        b.appendChild(moon(phase(g.state))); b.appendChild(el("span", "", g.title));
        b.appendChild(el("span", "chip " + phase(g.state), W.phase[phase(g.state)]));
        b.appendChild(el("small", "", W.v_knowledge + " +" + recorded + " · " + W.recalled + " " + recalledHere));
        b.title = g.state;
        b.addEventListener("click", function () { if (g.page) { read(g.page); } });
        li.appendChild(b); list.appendChild(li);
      });
      card.appendChild(list); box.appendChild(card);
    });
    if (!data.scopes.length) { box.appendChild(el("p", "empty", W.none_text)); }
  }

  // Context conversations are isolated per node/document and kept in memory only.
  var chatThreads = {}, chatContext = null, chatBusy = false, chatReturn = null;
  $("chat-driver").value = saved.chatDriver === "claude" ? "claude" : "codex";
  $("chat-driver").addEventListener("change", function() { saved.chatDriver = this.value; persist(); });
  $("chat-input").addEventListener("input", function() { if (chatContext) { var thread = chatThreads[chatContext.key] || (chatThreads[chatContext.key] = { turns:[] }); thread.draft = this.value; } });
  var chatToken = document.querySelector('meta[name="lupus-chat-token"]');
  function syncChat() {
    if (!chatThreads) { return; }
    var n = (current === "graph" || current === "knowledge") && byId[drawn];
    chatContext = current === "docs" && pages[reading] ? { key:"page:" + reading, page:reading, title:pageTitle(reading) } :
      n ? { key:"node:" + n.id, node_id:n.id, title:n.title } : null;
    $("chat-context").textContent = chatContext ? chatContext.title : W.chat_select;
    var thread = chatContext && chatThreads[chatContext.key];
    $("chat-input").value = thread && thread.draft || "";
    clear($("chat-thread"));
    if (!thread || !thread.turns.length) { $("chat-thread").appendChild(el("p", "chat-empty", chatContext ? W.chat_empty : W.chat_select)); }
    else { thread.turns.forEach(function(turn) { $("chat-thread").appendChild(el("p", "chat-turn " + turn.role, turn.content)); }); }
    $("chat-input").disabled = !chatContext || chatBusy || !chatToken;
    $("chat-send").disabled = !chatContext || chatBusy || !chatToken;
    $("chat-driver").disabled = chatBusy;
    $("chat-new").disabled = !chatContext || chatBusy;
    $("chat-status").classList.toggle("error", !!(thread && thread.error));
    $("chat-status").textContent = !chatToken ? W.chat_offline : thread && thread.error ? thread.error : chatBusy ? W.chat_wait : "";
    $("chat-thread").scrollTop = $("chat-thread").scrollHeight;
  }
  function openChat(ev) {
    $("rail-settings").classList.remove("settings-open"); $("settings-toggle").setAttribute("aria-expanded", "false");
    chatReturn = ev && ev.currentTarget || $("chat-fab");
    $("chat").hidden = false; document.body.classList.add("chat-open"); syncChat();
    ["chat-fab", "launch"].forEach(function(id) { $(id).setAttribute("aria-expanded", "true"); });
    if (!$("chat-input").disabled) { $("chat-input").focus(); } else { $("chat-close").focus(); }
  }
  function closeChat() {
    $("chat").hidden = true; document.body.classList.remove("chat-open");
    ["chat-fab", "launch"].forEach(function(id) { $(id).setAttribute("aria-expanded", "false"); });
    if (chatReturn) { chatReturn.focus(); }
  }
  $("chat-fab").addEventListener("click", function(ev) { if ($("chat").hidden) { openChat(ev); } else { closeChat(); } });
  $("chat-close").addEventListener("click", closeChat);
  $("chat-new").addEventListener("click", function() { if (chatContext && !chatBusy) { delete chatThreads[chatContext.key]; $("chat-input").value = ""; syncChat(); $("chat-input").focus(); } });
  $("chat-form").addEventListener("submit", async function(ev) {
    ev.preventDefault();
    var question = $("chat-input").value.trim();
    if (!question || !chatContext || chatBusy || !chatToken) { return; }
    var context = chatContext, thread = chatThreads[context.key] || (chatThreads[context.key] = { turns:[] });
    var history = thread.turns.slice(-12);
    thread.error = ""; thread.draft = ""; thread.turns.push({ role:"user", content:question }); chatBusy = true;
    $("chat-input").value = ""; syncChat();
    try {
      var response = await fetch("/api/chat", { method:"POST", headers:{ "Content-Type":"application/json", "X-Lupus-Token":chatToken.content },
        body:JSON.stringify({ node_id:context.node_id, page:context.page, message:question, history:history, driver:$("chat-driver").value }) });
      var result = await response.json();
      if (!response.ok || typeof result.answer !== "string") { throw new Error(result.error || W.chat_error); }
      thread.turns.push({ role:"assistant", content:result.answer });
    } catch(error) {
      thread.turns.pop(); thread.error = error.message || W.chat_error; thread.draft = question;
      if (chatContext && chatContext.key === context.key) { $("chat-input").value = question; }
    } finally { chatBusy = false; syncChat(); }
  });
  $("chat-input").addEventListener("keydown", function(ev) { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); $("chat-form").requestSubmit(); } });

  // =========================================================== search palette
  var hits = [], cursor = 0;
  function openPalette() {
    $("palette").hidden = false; $("p-input").value = ""; search(""); $("p-input").focus();
  }
  function closePalette() { $("palette").hidden = true; }
  function search(q) {
    q = q.trim().toLowerCase(); hits = [];
    VIEWS.forEach(function (v) { if (!q || W["v_" + v].toLowerCase().indexOf(q) >= 0) { hits.push({ type: "view", id: v, title: W["v_" + v] }); } });
    data.nodes.forEach(function (n) {
      var hay = (n.title + " " + (n.body || "")).toLowerCase();
      if (q && hay.indexOf(q) >= 0) { hits.push({ type: "node", id: n.id, title: n.title, kind: n.kind }); }
    });
    Object.keys(pages).forEach(function (p) {
      if (q && !pageOf[p] && (pageTitle(p) + " " + p).toLowerCase().indexOf(q) >= 0) { hits.push({ type: "page", id: p, title: pageTitle(p) }); }
    });
    hits = hits.slice(0, 40); cursor = 0; drawHits();
  }
  function drawHits() {
    var list = $("p-list"), group = null; clear(list);
    hits.forEach(function (h, i) {
      var label = h.type === "view" ? W.all : h.type === "page" ? W.v_docs : W.kind[h.kind];
      var heading = h.type === "view" ? "views" : h.type;
      if (heading !== group) { group = heading; list.appendChild(el("li", "group", h.type === "view" ? W.v_overview + " · " + W.v_graph + " · …" : h.type === "page" ? W.v_docs : W.v_knowledge)); }
      var li = el("li"), b = el("button", "row"), dot = el("i", "dot");
      b.type = "button"; b.setAttribute("role", "option"); b.setAttribute("aria-selected", String(i === cursor));
      dot.style.background = h.kind ? "var(--k-" + h.kind + ")" : "var(--soft)";
      b.appendChild(dot); b.appendChild(el("span", "", h.title)); b.appendChild(el("small", "", label));
      b.addEventListener("click", function () { cursor = i; choose(); });
      li.appendChild(b); list.appendChild(li);
    });
    if (!hits.length) { list.appendChild(el("li", "empty", W.no_rows)); }
  }
  function choose() {
    var h = hits[cursor]; if (!h) { return; }
    closePalette();
    if (h.type === "view") { go(h.id); }
    else if (h.type === "page") { read(h.id); }
    else if (byId[h.id].kind === "goal" && byId[h.id].page) { read(byId[h.id].page); }
    else { go("graph"); select(h.id, true); }
  }
  $("launch").addEventListener("click", openChat);
  $("find").addEventListener("click", openPalette);
  $("p-input").addEventListener("input", function () { search(this.value); });
  $("palette").addEventListener("click", function (ev) { if (ev.target === $("palette")) { closePalette(); } });
  $("p-input").addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault(); cursor = (cursor + (ev.key === "ArrowDown" ? 1 : -1) + hits.length) % Math.max(1, hits.length); drawHits();
      var on = $("p-list").querySelector("[aria-selected=true]"); if (on) { on.scrollIntoView({ block: "nearest" }); }
    } else if (ev.key === "Enter") { ev.preventDefault(); choose(); }
  });
  document.addEventListener("keydown", function (ev) {
    var typing = /^(INPUT|SELECT|TEXTAREA)$/.test((ev.target && ev.target.tagName) || "");
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); if ($("palette").hidden) { openPalette(); } else { closePalette(); } return; }
    if ((ev.code === "KeyF" || ev.key.toLowerCase() === "f") && !typing && !ev.metaKey && !ev.ctrlKey && !ev.altKey && current === "graph" && $("palette").hidden) { ev.preventDefault(); toggleGraphFullscreen(); return; }
    if (ev.key === "/" && !typing) { ev.preventDefault(); openPalette(); return; }
    if (ev.key === "Escape") {
      if (!$("palette").hidden) { closePalette(); }
      else if (!$("chat").hidden) { closeChat(); }
      else if ($("rail-settings").classList.contains("settings-open")) { $("settings-toggle").click(); $("settings-toggle").focus(); }
      else if (graphExpanded) { exitGraphFullscreen(true); }
      else if (!$("drawer").classList.contains("shut")) { if (current === "graph") { select(null, false); } else { shut(); } }
    }
  });

  // =========================================================== settings and start
  $("lang").addEventListener("change", function () {
    saved.lang = this.value; persist(); speak(saved.lang); renderAll();
    if (network) { relabel(); rebuild(); }
    $("v-title").textContent = W["v_" + current]; $("v-sub").textContent = W["sub_" + current];
    if (drawn) { show(drawn); } syncChat();
  });
  $("theme").addEventListener("click", function () {
    saved.theme = root.hasAttribute("data-dark") ? "light" : "dark"; persist();
    readTheme(); repaint(); mini();
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { if (!saved.theme) { readTheme(); repaint(); mini(); } });
  }
  function renderAll() { renderOverview(); renderKnowledge(); renderTree(); renderGoals(); }
  new ResizeObserver(function () {
    if (current === "overview" && miniNet && $("mini").clientWidth) { miniNet.fit({ animation:false }); miniNet.moveTo({ scale:miniNet.getScale() * .92 }); }
  }).observe($("mini"));
  var tongue = (window.navigator.language || "en").toLowerCase().slice(0, 2);
  speak(WORDS[saved.lang] ? saved.lang : WORDS[tongue] ? tongue : "en");
  renderAll();
  // the canvas draws mini-graph labels in the bundled face only once it has loaded
  if (document.fonts && document.fonts.load) { document.fonts.load("600 12px Sora").then(function () { mini(); }, function () {}); }
  go((window.location.hash || "").slice(1) || saved.view || "overview", true);
  window.setTimeout(function () { var r = $("rise"); if (r && r.parentNode) { r.parentNode.removeChild(r); } }, calm ? 0 : 1600);
  // the launcher opens as a pill with the moonrise, then settles to the moon after a few seconds or the first scroll
  document.body.classList.add("launching");
  var settle = function () { $("launch").classList.remove("open"); document.body.classList.remove("launching"); };
  window.setTimeout(settle, 6000);
  window.addEventListener("scroll", settle, { once:true });
  Array.prototype.forEach.call(document.querySelectorAll(".view"), function (v) { v.addEventListener("scroll", settle, { once: true }); });
})();
