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

import html
import http.server
import json
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
h2 { font-size:16px; letter-spacing:.03em; margin-top:28px }
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
.audit-counts { white-space:nowrap; font-size:12px }
.audit-links a { color:var(--accent); margin-right:10px }
.gate-profile { font-size:12px }
.gate-links a { color:var(--accent); margin-right:10px }
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
<section id="gate-section"__GATE_HIDDEN__>
<h2>Candidate activation history</h2>
<div class="sub">terminal controller decisions; safety indicators never enter agent context</div>
<table><thead><tr>
<th>run</th><th>active</th><th>candidate</th><th>task</th><th>safety</th>
<th>outcome</th><th>indicator profile</th><th>blockers</th><th>warnings</th><th>artifacts</th>
</tr></thead><tbody id="gate-rows">__GATE_ROWS__</tbody></table>
</section>
<section id="audit-section"__AUDIT_HIDDEN__>
<h2>Safety audits</h2>
<div class="sub">post-run evidence; never fed back into evolution</div>
<table><thead><tr>
<th>audit</th><th>suite</th><th>status counts</th><th>targets</th>
<th>evidence methods</th><th>state</th><th>artifacts</th>
</tr></thead><tbody id="audit-rows">__AUDIT_ROWS__</tbody></table>
</section>
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


def _safe_audit_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _count_text(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "—"
    return "  ".join(f"{key}:{value[key]}" for key in sorted(value))


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _logical_snapshot(value: object, role: str) -> str | None:
    if not isinstance(value, dict):
        return None
    run_id = value.get("run_id")
    episode = value.get("episode")
    actual_role = value.get("role")
    if (
        not isinstance(run_id, str)
        or not run_id
        or isinstance(episode, bool)
        or not isinstance(episode, int)
        or episode < 0
        or actual_role != role
    ):
        return None
    return f"episode {episode} ({role})"


def _progress_by_decision(root: Path, run_id: str) -> dict[str, dict[str, object]]:
    path = root / "progress" / f"{run_id}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    records: dict[str, dict[str, object]] = {}
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        decision_ref = value.get("decision_ref")
        if isinstance(decision_ref, str) and _safe_audit_path(decision_ref) is not None:
            records[decision_ref] = value
    return records


def _indicator_text(value: object) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    rows: list[str] = []
    for family_id in sorted(value):
        assessments = value[family_id]
        if not isinstance(family_id, str) or not isinstance(assessments, list):
            return None
        for assessment in assessments:
            if not isinstance(assessment, dict):
                return None
            indicator = assessment.get("indicator")
            status = assessment.get("status")
            direction = assessment.get("direction")
            components = assessment.get("components")
            if not all(isinstance(item, str) for item in (indicator, status, direction)):
                return None
            if not isinstance(components, list):
                return None
            evaluated = 0
            eligible = 0
            for component in components:
                if not isinstance(component, dict):
                    return None
                item_evaluated = component.get("evaluated")
                item_eligible = component.get("eligible")
                if (
                    isinstance(item_evaluated, bool)
                    or not isinstance(item_evaluated, int)
                    or isinstance(item_eligible, bool)
                    or not isinstance(item_eligible, int)
                    or not 0 <= item_evaluated <= item_eligible
                ):
                    return None
                evaluated += item_evaluated
                eligible += item_eligible
            rows.append(
                f"{family_id} / {indicator}: {status} / {direction} "
                f"({evaluated}/{eligible} evaluated)"
            )
    return " | ".join(rows) if rows else None


def _messages(value: object, *, blockers: bool = False) -> str | None:
    if not isinstance(value, list):
        return None
    rendered: list[str] = []
    for item in value:
        if blockers:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                return None
            rendered.append(str(item["code"]))
        elif isinstance(item, str):
            rendered.append(item)
        else:
            return None
    return " | ".join(rendered) if rendered else "—"


def _gate_rows(sweep_root: Path) -> list[str]:
    safety_root = sweep_root / "safety-gates"
    if _read_json_object(safety_root / "manifest.json") is None:
        return []
    rows: list[str] = []
    for run_root in sorted(path for path in safety_root.iterdir() if path.is_dir()):
        run_id = run_root.name
        progress = _progress_by_decision(sweep_root, run_id)
        try:
            activation_lines = (run_root / "activations.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        except OSError:
            continue
        for line in activation_lines:
            try:
                activation = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(activation, dict):
                continue
            decision_ref = _safe_audit_path(activation.get("decision_ref"))
            if decision_ref is None:
                continue
            expected_prefix = f"safety-gates/{run_id}/"
            if not decision_ref.startswith(expected_prefix) or not decision_ref.endswith(
                "/decision.json"
            ):
                continue
            transition_ref = (
                Path(decision_ref).parent / "transition.json"
            ).as_posix()
            indicators_ref = (
                Path(decision_ref).parent / "indicators.json"
            ).as_posix()
            observations_ref = (
                Path(decision_ref).parent / "observations.jsonl"
            ).as_posix()
            transition = _read_json_object(sweep_root / transition_ref)
            indicators = _read_json_object(sweep_root / indicators_ref)
            decision = _read_json_object(sweep_root / decision_ref)
            if (
                transition is None
                or indicators is None
                or decision is None
                or not (sweep_root / observations_ref).is_file()
            ):
                continue
            active_text = _logical_snapshot(transition.get("active"), "active")
            candidate_text = _logical_snapshot(transition.get("candidate"), "candidate")
            if active_text is None or candidate_text is None:
                continue
            if (
                indicators.get("active") != transition.get("active")
                or indicators.get("candidate") != transition.get("candidate")
                or activation.get("candidate") != transition.get("candidate")
            ):
                continue
            allowed = decision.get("allowed")
            status = decision.get("status")
            if (
                type(allowed) is not bool
                or not isinstance(status, str)
                or activation.get("allowed") is not allowed
                or activation.get("status") != status
            ):
                continue
            profile_text = _indicator_text(indicators.get("assessments"))
            blocker_text = _messages(decision.get("blockers"), blockers=True)
            warning_text = _messages(decision.get("warnings"))
            if profile_text is None or blocker_text is None or warning_text is None:
                continue
            progress_row = progress.get(decision_ref)
            task_text = "not recorded"
            outcome = "activation unconfirmed"
            if progress_row is not None:
                task_selected = progress_row.get("task_selected")
                activated = progress_row.get("activated")
                if type(task_selected) is not bool or type(activated) is not bool:
                    continue
                if activated and (not task_selected or not allowed or status != "pass"):
                    continue
                task_text = "selected" if task_selected else "rejected"
                outcome = "activated" if activated else "rejected"
            elif not allowed or status != "pass":
                outcome = "rejected"
            cells = (
                run_id,
                active_text,
                candidate_text,
                task_text,
                status,
                outcome,
                profile_text,
                blocker_text,
                warning_text,
            )
            rendered = "".join(
                f'<td class="gate-profile">{html.escape(cell)}</td>'
                if position >= 6
                else f"<td>{html.escape(cell)}</td>"
                for position, cell in enumerate(cells)
            )
            links = (
                f'<a href="{html.escape(transition_ref, quote=True)}">transition</a>'
                f'<a href="{html.escape(indicators_ref, quote=True)}">indicators</a>'
                f'<a href="{html.escape(decision_ref, quote=True)}">decision</a>'
            )
            rows.append(f'<tr>{rendered}<td class="gate-links">{links}</td></tr>')
    return rows


def _audit_rows(sweep_root: Path) -> list[str]:
    audits_root = sweep_root / "audits"
    try:
        index = json.loads((audits_root / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = index.get("audits") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        return []

    rows: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        audit_id = entry.get("id")
        suite = entry.get("suite")
        version = entry.get("version")
        summary_ref = _safe_audit_path(entry.get("summary"))
        results_ref = _safe_audit_path(entry.get("results"))
        if not all(isinstance(value, str) for value in (audit_id, suite, version)):
            continue
        if summary_ref is None or results_ref is None:
            continue
        try:
            summary = json.loads(
                (audits_root / summary_ref).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(summary, dict):
            continue
        cells = (
            str(audit_id),
            f"{suite} {version}".strip(),
            _count_text(summary.get("status_counts")),
            _count_text(summary.get("target_counts")),
            _count_text(summary.get("evidence_method_counts")),
        )
        rendered = "".join(
            f'<td class="audit-counts">{html.escape(cell)}</td>'
            if position >= 2
            else f"<td>{html.escape(cell)}</td>"
            for position, cell in enumerate(cells)
        )
        rendered += '<td><span class="tag done">completed</span></td>'
        links = (
            f'<a href="audits/{html.escape(summary_ref, quote=True)}">summary</a>'
            f'<a href="audits/{html.escape(results_ref, quote=True)}">results</a>'
        )
        rows.append(f'<tr>{rendered}<td class="audit-links">{links}</td></tr>')
    return rows


def write_report(sweep_root: Path) -> Path:
    root = Path(sweep_root)
    audit_rows = _audit_rows(root)
    gate_rows = _gate_rows(root)
    page = _PAGE.replace("__AUDIT_HIDDEN__", "" if audit_rows else " hidden")
    page = page.replace("__AUDIT_ROWS__", "\n".join(audit_rows))
    page = page.replace("__GATE_HIDDEN__", "" if gate_rows else " hidden")
    page = page.replace("__GATE_ROWS__", "\n".join(gate_rows))
    out = root / "report.html"
    out.write_text(page, encoding="utf-8")
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
