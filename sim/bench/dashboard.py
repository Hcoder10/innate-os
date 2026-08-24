#!/usr/bin/env python3
"""Live benchmark dashboard on localhost.

Serves one page that polls its own JSON endpoint, so a run can be watched while
it happens instead of read afterwards. Reads the same files the sweep writes --
no shared state, no coupling: if the sweep dies the page keeps showing the last
good numbers rather than going blank.

    python sim/bench/dashboard.py --port 8110
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RESULTS = Path("/tmp/bench_live.json")
SHIM_LOG = Path("/tmp/shim.log")
BRAIN_LOG = Path("/tmp/brain_tail.txt")  # refreshed by the runner script
PLANNED: list[tuple[str, str]] = [
    ("gallery_tutorial", "observation"),
    ("clarify_one_sock", "observation"),
    ("ground_phrasing_direct", "observation"),
    ("gallery_behind_you", "instruction"),
    ("ground_hall_end_nav", "instruction"),
    ("workshop_sock", "instruction"),
    ("laundry_claim_only", "long-horizon / arm"),
    ("laundry_single_control", "long-horizon / arm"),
    ("laundry_claim_then_delivery", "long-horizon / arm"),
    ("household_two_chores", "long-horizon"),
    ("rounds_tutorial", "long-horizon"),
    ("dropoff_dog_aside_control", "long-horizon"),
]


def snapshot() -> dict:
    rows = []
    if RESULTS.exists():
        try:
            rows = json.loads(RESULTS.read_text())
        except Exception:  # noqa: BLE001 -- mid-write; last good numbers stand
            rows = []
    by = {r["challenge"]: r for r in rows}

    calls = vision = 0
    if SHIM_LOG.exists():
        text = SHIM_LOG.read_text(errors="replace")
        calls = text.count("streamGenerateContent")
        vision = text.count("chat/completions")

    tools = []
    if BRAIN_LOG.exists():
        tail = BRAIN_LOG.read_text(errors="replace")
        tools = re.findall(r"Tool call: ([a-z_]+)", tail)[-14:]

    done = len(by)
    running = next((c for c, _ in PLANNED if c not in by), None) if done < len(PLANNED) else None
    return {
        "planned": [
            {"id": c, "cat": cat,
             "state": ("done" if c in by else "running" if c == running else "queued"),
             **({k: by[c].get(k, "") for k in ("passed", "goals_done", "goals_total", "elapsed_s",
                                               "wall_s", "reason", "error", "blocked")} if c in by else {})}
            for c, cat in PLANNED
        ],
        "done": done, "total": len(PLANNED),
        # Blocked challenges are excluded from every tally, exactly as
        # live_runner excludes them. A blocked Episode carries passed=False
        # with an empty reason and error, so without this it renders as a red
        # failed row with no explanation, inside a denominator that counts it
        # -- the dashboard re-telling the lie the gate exists to remove.
        "passed": sum(1 for r in by.values() if r["passed"] and not r.get("blocked")),
        "blocked": sum(1 for r in by.values() if r.get("blocked")),
        "goals_done": sum(r["goals_done"] for r in by.values() if not r.get("blocked")),
        "goals_total": sum(r["goals_total"] for r in by.values() if not r.get("blocked")),
        "model_calls": calls, "vision_calls": vision,
        "recent_tools": tools,
    }


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MARS benchmark - live</title><style>
:root{--ground:#E9EDF0;--surface:#fff;--sunk:#DFE5EA;--ink:#131A21;--ink2:#3D4C57;--muted:#5C6B78;
--line:#C9D2D9;--accent:#1C6B73;--ok:#2E7D5B;--bad:#A8412C;
--mono:ui-monospace,"Cascadia Mono",Consolas,monospace;--sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif}
@media(prefers-color-scheme:dark){:root{--ground:#0E1419;--surface:#151C24;--sunk:#1B242D;--ink:#DCE4EA;
--ink2:#AEBECA;--muted:#8496A4;--line:#2A3641;--accent:#4FB3B8;--ok:#5DBE8D;--bad:#E0785C}}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);line-height:1.6}
.wrap{max-width:58rem;margin:0 auto;padding:2rem 1.4rem 4rem}
h1{font-size:1.9rem;letter-spacing:-.02em;margin:.3rem 0 0}
.eyebrow{font-family:var(--mono);font-size:.68rem;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}
.sub{color:var(--ink2);margin:.5rem 0 0}
.live{display:inline-flex;align-items:center;gap:.4rem;font-family:var(--mono);font-size:.68rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--accent)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--accent);animation:p 1.6s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
@media(prefers-reduced-motion:reduce){.dot{animation:none}}
.stats{display:grid;gap:1px;background:var(--line);border:1px solid var(--line);
grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr));margin-top:1.3rem}
.stat{background:var(--surface);padding:.8rem .9rem}
.stat b{display:block;font-family:var(--mono);font-size:1.5rem;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.03em}
.stat span{font-family:var(--mono);font-size:.63rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.bar{margin:1.2rem 0 0;height:7px;background:var(--sunk);border:1px solid var(--line)}
.bar i{display:block;height:100%;background:var(--accent);transition:width .4s}
.eps{display:grid;gap:1px;background:var(--line);border:1px solid var(--line);margin-top:1.5rem}
.ep{background:var(--surface);padding:.7rem .85rem;display:grid;grid-template-columns:8rem 1fr auto;
gap:.1rem .85rem;align-items:baseline;border-left:3px solid var(--line)}
.ep.done.y{border-left-color:var(--ok)}.ep.done.n{border-left-color:var(--bad)}
.ep.running{border-left-color:var(--accent);background:var(--sunk)}.ep.queued{opacity:.45}
.cat{font-family:var(--mono);font-size:.62rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.cid{font-family:var(--mono);font-size:.86rem;font-weight:600}
.det{grid-column:2;font-size:.82rem;color:var(--ink2)}
.rt{font-family:var(--mono);font-size:.72rem;color:var(--muted);grid-row:1/3;align-self:center;text-align:right}
.tools{margin-top:1.5rem;border:1px solid var(--line);background:var(--surface);padding:.8rem .9rem}
.tools h2{font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;
color:var(--muted);margin:0 0 .5rem;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:.35rem}
.chip{font-family:var(--mono);font-size:.72rem;border:1px solid var(--line);padding:.15rem .45rem;background:var(--sunk)}
.chip.arm{border-color:var(--ok);color:var(--ok)}
footer{margin-top:2rem;color:var(--muted);font-size:.8rem}
</style></head><body><div class="wrap">
<span class="eyebrow">innate-os brain &middot; gpt-5.6-luna via codex</span>
<h1>Benchmark, live</h1>
<p class="sub"><span class="live"><span class="dot"></span>polling</span> &mdash; the shipping brain,
skills and judge, on a substituted model.</p>
<div class="bar"><i id="bar" style="width:0"></i></div>
<div class="stats" id="stats"></div>
<div class="eps" id="eps"></div>
<div class="tools"><h2>Most recent tool calls</h2><div class="chips" id="tools"></div></div>
<footer id="foot"></footer>
</div><script>
const ARM = new Set(["pick_any_object","open_gripper","close_gripper","arm_rest_position"]);
async function tick(){
  let d; try{ d = await (await fetch("/api")).json(); }catch(e){ return; }
  document.getElementById("bar").style.width = (d.done/d.total*100)+"%";
  document.getElementById("stats").innerHTML = [
    ["episodes", d.done+"/"+d.total], ["passed", d.passed], ["goals", d.goals_done+"/"+d.goals_total],
    ["model calls", d.model_calls], ["vision calls", d.vision_calls],
  ].concat(d.blocked ? [["blocked", d.blocked]] : [])
   .map(([l,v])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
  document.getElementById("eps").innerHTML = d.planned.map(e=>{
    // A blocked challenge was never attempted. Rendering it as a failure is
    // the same mistake as scoring it as one.
    const cls = e.state==="done" ? (e.blocked ? "done blk" : "done "+(e.passed?"y":"n")) : e.state;
    const det = e.state==="done" ? (e.blocked ? "not attempted &mdash; "+e.blocked
                                              : (e.error||e.reason||(e.passed?"passed":""))) :
                e.state==="running" ? "running now" : "queued";
    const rt = e.state==="done" ? (e.blocked ? "&mdash;"
                : `${e.goals_done}/${e.goals_total} &middot; ${Math.round(e.elapsed_s)}s`) : "&mdash;";
    return `<div class="ep ${cls}"><span class="cat">${e.cat}</span><span class="cid">${e.id}</span>
      <span class="det">${det}</span><span class="rt">${rt}</span></div>`;
  }).join("");
  document.getElementById("tools").innerHTML = (d.recent_tools.length?d.recent_tools:["(none yet)"])
    .map(t=>`<span class="chip ${ARM.has(t)?"arm":""}">${t}</span>`).join("");
  document.getElementById("foot").textContent = "updated " + new Date().toLocaleTimeString();
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api"):
            body = json.dumps(snapshot()).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8110)
    a = ap.parse_args()
    print(f"dashboard on http://localhost:{a.port}")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
