"""Graph view of the memory graph.

Writes a small static page (viewer/index.html + viewer.js + the vendored, unmodified
vis-network bundle) next to the vault. It opens straight from disk, makes no network request
and loads no code other than those two scripts (enforced by a Content-Security-Policy).

Besides knowledge nodes and their typed links, the graph shows where knowledge came from and
where it was used: project -> goal, node -> goal it was recorded in, node -> goal it was
recalled in.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import memory, vault
from .kernel import Kernel
from .util import LupusError, atomic_write


def export(k: Kernel) -> dict:
    nodes, edges, scopes = [], [], []
    seen: set[str] = set()
    for project in [None] + [dict(r) for r in k.q("SELECT project_id, name FROM project ORDER BY created_at")]:
        pid = project["project_id"] if project else None
        scope = pid or ""
        if project:
            scopes.append([pid, project["name"]])
            nodes.append({"id": pid, "kind": "project", "scope": scope, "label": project["name"][:30],
                          "title": project["name"], "page": f"projects/{pid}/map.md"})
            for goal in k.q("SELECT goal_id, objective, status FROM goal WHERE project_id = ? ORDER BY created_at", pid):
                nodes.append({"id": goal["goal_id"], "kind": "goal", "scope": scope, "label": goal["objective"][:24],
                              "title": goal["objective"], "state": goal["status"],
                              "page": vault.goal_page(pid, goal["goal_id"])})
                edges.append({"from": pid, "to": goal["goal_id"], "type": "has_goal"})
        for node in memory.nodes(k, pid):
            seen.add(node["node_id"])
            nodes.append({
                "id": node["node_id"], "kind": node["kind"], "scope": scope, "label": node["title"][:24],
                "title": node["title"], "body": node["body"], "status": node["status"], "origin": node["origin"],
                "recalled": node["recalled"], "helped": node["helped"], "unhelped": node["unhelped"],
                "page": vault.node_page(node),
            })
            if node["source_goal_id"]:
                edges.append({"from": node["node_id"], "to": node["source_goal_id"], "type": "recorded_in"})
    for edge in memory.edges(k, sorted(seen)):
        edges.append({"from": edge["src"], "to": edge["dst"], "type": edge["type"]})
    for row in k.q("SELECT DISTINCT node_id, goal_id FROM recall"):
        edges.append({"from": row["node_id"], "to": row["goal_id"], "type": "recalled_in"})
    return {"nodes": nodes, "edges": edges, "scopes": scopes,
            "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")}


def write(k: Kernel, out_dir: str | Path | None = None, vault_root: str | Path | None = None) -> Path:
    out = Path(out_dir) if out_dir is not None else k.home / "viewer"
    if out.is_symlink():
        raise LupusError("VIEWER_PATH_ESCAPE", str(out))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets = resources.files("lupus").joinpath("viewer")
    data = export(k)
    # The pages of the vault as they would be written now, so a note can be read beside the graph
    # without the page ever fetching a file.
    data["pages"] = vault.render(k)
    data["vault_rel"] = os.path.relpath(os.path.realpath(vault_root or vault.default_root(k)), os.path.realpath(out))
    # Embedded as inert JSON. '<', '>' and '&' are escaped so node text can never close the
    # script element or be parsed as markup.
    payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = assets.joinpath("index.template.html").read_text(encoding="utf-8").replace("__DATA__", payload)
    atomic_write(out / "index.html", html.encode("utf-8"), mode=0o644)
    # Written to a temp file and renamed into place: an existing symlink at the destination is
    # replaced itself, never followed.
    for name in ("viewer.js", "vendor/vis-network.min.js"):
        atomic_write(out / Path(name).name, assets.joinpath(name).read_bytes(), mode=0o644)
    return out / "index.html"
