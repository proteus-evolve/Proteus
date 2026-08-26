"""The live tracking page and the run-repo export.

`write_report(sweep_root)` drops a self-contained `report.html` into the sweep root. The
page fetches `manifest.json` and `progress/<run>.jsonl` every few seconds and renders
per-run progress, per-surface growth curves, and evaluator scores — so a sweep can be
watched while it runs. No external assets; serve the sweep root over HTTP (`proteus
watch`) because browsers do not allow `fetch` from `file://`.

`export_repo` / `push_repo` turn a run's snapshot chain (one commit per episode) into a
normal git repository the user can browse or push — the evolution history *is* the
artifact.
"""

from __future__ import annotations

import http.server
import subprocess
from pathlib import Path

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proteus run tracker</title>
<style>
:root { --bg:#f6f5f1; --card:#fff; --ink:#20241f; --sub:#6b6f68; --line:#e2e1da;
        --accent:#2e5d43; --bad:#a33b2e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#181a17; --card:#20231f; --ink:#e8e7e0; --sub:#9a9e96; --line:#33362f;
          --accent:#7fb598; --bad:#d98577; } }
* { box-sizing:border-box; margin:0 }
body { background:var(--bg); color:var(--ink);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif; padding:24px }
h1 { font-size:18px; letter-spacing:.04em }
.sub { color:var(--sub); margin:4px 0 20px }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:8px; overflow:hidden }
th,td { text-align:left; padding:8px 12px; border-top:1px solid var(--line);
        font-variant-numeric:tabular-nums }
th { border-top:none; color:var(--sub); font-weight:600; font-size:12px;
     text-transform:uppercase; letter-spacing:.06em }
.bar { height:6px; background:var(--line); border-radius:3px; min-width:120px }
.bar i { display:block; height:6px; background:var(--accent); border-radius:3px }
.err i { background:var(--bad) }
.spark { width:150px; height:28px }
.tag { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
       border:1px solid var(--line); color:var(--sub) }
.done { color:var(--accent); border-color:var(--accent) }
.bad  { color:var(--bad); border-color:var(--bad) }
footer { color:var(--sub); margin-top:16px; font-size:12px }
</style>
</head>
<body>
<h1>PROTEUS · run tracker</h1>
<div class="sub" id="meta">loading…</div>
<table id="tbl"><thead><tr>
<th>arm</th><th>seed</th><th>progress</th><th>episodes</th><th>tool calls</th>
<th>units by surface</th><th>growth</th><th>last score</th><th>status</th>
</tr></thead><tbody id="rows"></tbody></table>
<footer id="foot"></footer>
<script>
async function jl(u){ const r = await fetch(u,{cache:"no-store"});
  if(!r.ok) return []; const t = await r.text();
  return t.split("\\n").filter(Boolean).map(JSON.parse); }
function spark(recs, key){
  if(!recs.length) return "";
  const names = Object.keys(recs[recs.length-1][key]||{});
  const W=150,H=28,n=recs.length;
  let max=1; recs.forEach(r=>names.forEach(s=>max=Math.max(max,(r[key]||{})[s]||0)));
  const line=(s,i)=>{
    const pts=recs.map((r,j)=>`${(j/(Math.max(n-1,1)))*W},${H-2-((r[key]||{})[s]||0)/max*(H-6)}`);
    const dash=i%2?"3,2":"";
    return `<polyline fill="none" stroke="var(--accent)" stroke-opacity="${1-i*0.35}"
      stroke-width="1.5" stroke-dasharray="${dash}" points="${pts.join(" ")}"/>`; };
  return `<svg class="spark" viewBox="0 0 ${W} ${H}">${names.map(line).join("")}</svg>`;
}
async function tick(){
  const m = await (await fetch("manifest.json",{cache:"no-store"})).json();
  document.getElementById("meta").textContent =
    `${m.name} — ${m.arms.length} arms x ${m.seeds} seeds x ${m.episodes} episodes`;
  const rows = [];
  for(const r of m.runs){
    const recs = await jl(`progress/${r.id}.jsonl`);
    const last = recs[recs.length-1];
    const ep = last ? last.episode : 0, tgt = m.episodes;
    const pct = Math.round(100*ep/tgt);
    const errored = last && !last.ok;
    const status = errored ? `<span class="tag bad">error</span>`
      : ep>=tgt ? `<span class="tag done">complete</span>`
      : ep>0 ? `<span class="tag">running</span>` : `<span class="tag">pending</span>`;
    const units = last ? Object.entries(last.units)
        .map(([k,v])=>`${k}:${v}`).join("  ") : "—";
    const score = last && Object.keys(last.scores||{}).length
      ? Object.entries(last.scores).map(([k,v])=>`${k}=${(+v).toFixed(2)}`).join(" ") : "—";
    const calls = recs.reduce((a,r)=>a+(r.tool_calls||0),0);
    rows.push(`<tr><td>${r.arm}</td><td>${r.seed}</td>
      <td><div class="bar ${errored?"err":""}"><i style="width:${pct}%"></i></div></td>
      <td>${ep}/${tgt}</td><td>${calls||"—"}</td><td>${units}</td>
      <td>${spark(recs,"units")}</td><td>${score}</td><td>${status}</td></tr>`);
  }
  document.getElementById("rows").innerHTML = rows.join("");
  document.getElementById("foot").textContent =
    `refreshed ${new Date().toLocaleTimeString()} — polls every 5s while the sweep runs`;
}
tick(); setInterval(tick, 5000);
</script>
</body>
</html>
"""


def write_report(sweep_root: Path) -> Path:
    out = Path(sweep_root) / "report.html"
    out.write_text(_PAGE, encoding="utf-8")
    return out


def serve(sweep_root: Path, port: int = 8300) -> None:
    """Serve the sweep root (report.html + manifest + progress) over local HTTP."""
    root = str(Path(sweep_root).resolve())

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, *a):  # quiet
            pass

    print(f"tracking page: http://localhost:{port}/report.html   (Ctrl-C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def export_repo(run_root: Path, dest: Path, branch: str = "main") -> Path:
    """Clone a run's snapshot chain into a normal repository at `dest`.

    One commit per episode; `git log` reads as the evolution history. The bare snapshot
    repo stays untouched under the run root.
    """
    run_root, dest = Path(run_root), Path(dest)
    git_dir = run_root / ".snapshot.git"
    if not git_dir.exists():
        raise FileNotFoundError(f"{run_root} has no .snapshot.git")
    subprocess.run(["git", "clone", "-q", str(git_dir), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "branch", "-q", "-M", branch], check=True)
    return dest


def push_repo(run_root: Path, remote: str, branch: str = "main") -> None:
    """Push a run's snapshot chain to a remote the user provides. Never automatic."""
    git_dir = Path(run_root) / ".snapshot.git"
    if not git_dir.exists():
        raise FileNotFoundError(f"{run_root} has no .snapshot.git")
    subprocess.run(["git", "--git-dir", str(git_dir), "push", remote,
                    f"HEAD:refs/heads/{branch}"], check=True)
