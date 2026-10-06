// Drives the graph viewer in headless Chrome over the DevTools protocol and checks that a node
// can really be dragged with the mouse: it must follow the pointer, a neighbour must move too
// (live physics), and with "pin" enabled it must stay where it was dropped.
// Usage: node evaluations/drag_check.mjs <path to viewer/index.html>
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const page = "file://" + resolve(process.argv[2]);
const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  "--headless=new", "--disable-gpu", "--remote-debugging-port=9333", "--window-size=1300,720",
  "--user-data-dir=" + mkdtempSync(join(tmpdir(), "lupus-chrome-")), page,
], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, seq = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((ok) => { const id = ++seq; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result.result.value;
const mouse = (type, x, y, extra = {}) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1, ...extra });

try {
  let target;
  for (let i = 0; i < 50 && !target; i++) {
    await sleep(200);
    try { target = (await (await fetch("http://127.0.0.1:9333/json")).json()).find((t) => t.type === "page"); } catch {}
  }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); } };
  await sleep(2500);                                              // let the layout settle a little
  const info = await evaluate(`(() => { const g = window.lupusGraph, ids = g.nodes.getIds();
    const id = ids.find((i) => g.network.getConnectedNodes(i).length > 0), nb = g.network.getConnectedNodes(id)[0];
    const r = document.getElementById("graph").getBoundingClientRect(), d = g.network.canvasToDOM(g.network.getPositions([id])[id]);
    return { id, nb, x: r.left + d.x, y: r.top + d.y, before: g.network.getPositions([id, nb]) }; })()`);
  const drag = async (dx, dy) => {
    const at = await evaluate(`(() => { const g = window.lupusGraph, r = document.getElementById("graph").getBoundingClientRect(),
      d = g.network.canvasToDOM(g.network.getPositions(["${info.id}"])["${info.id}"]); return { x: r.left + d.x, y: r.top + d.y }; })()`);
    await mouse("mouseMoved", at.x, at.y, { buttons: 0 });
    await mouse("mousePressed", at.x, at.y);
    for (let i = 1; i <= 12; i++) { await mouse("mouseMoved", at.x + (dx * i) / 12, at.y + (dy * i) / 12); await sleep(30); }
    const during = await evaluate(`window.lupusGraph.network.getPositions(["${info.id}", "${info.nb}"])`);
    await mouse("mouseReleased", at.x + dx, at.y + dy);
    return during;
  };
  const during = await drag(220, -140);
  const moved = Math.hypot(during[info.id].x - info.before[info.id].x, during[info.id].y - info.before[info.id].y);
  const neighbour = Math.hypot(during[info.nb].x - info.before[info.nb].x, during[info.nb].y - info.before[info.nb].y);
  await sleep(1500);
  await evaluate(`document.getElementById("pin").click()`);
  const dropped = await drag(-180, 120);
  await sleep(2500);                                              // physics keeps running; a pinned node must not drift
  const after = await evaluate(`window.lupusGraph.network.getPositions(["${info.id}"])`);
  const drift = Math.hypot(after[info.id].x - dropped[info.id].x, after[info.id].y - dropped[info.id].y);
  const result = { node_followed_pointer_px: Math.round(moved), neighbour_moved_px: Math.round(neighbour), pinned_drift_px: Math.round(drift),
                   ok: moved > 80 && neighbour > 3 && drift < 12 };
  console.log(JSON.stringify(result));
  process.exitCode = result.ok ? 0 : 1;
} finally {
  if (ws) ws.close();
  chrome.kill();
}
