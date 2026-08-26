"""Build a prepared environment from a harness repository.

The input for onboarding a harness is a **repository** — a git URL or a local path. This
module turns it into a pinned, runnable environment image:

    proteus env scaffold --from https://github.com/org/harness --name myharness
    proteus env build myharness

`scaffold` writes `environments/<name>/environment.toml` with a `[source]` section;
`build` clones/copies the repo, resolves the ref to a commit sha, and produces the image
`proteus-env-<name>:<shortsha>`, recording the resolved sha and image tag back into the
manifest so the environment is reproducible from the manifest alone.

Where the Dockerfile comes from, in priority order:
  1. `[source] use_local_dockerfile = true` — `environments/<name>/Dockerfile`, built with
     the repo checkout as context (write your wrapper here when the repo has none, or when
     you want to add proteus conventions on top: state mounts, telemetry off, pinned
     runtime);
  2. the repo's own Dockerfile (`[source] dockerfile = "Dockerfile"`, path inside the repo).

The harness binary/library goes INTO the image; the evolving workspace never does — it is
always a mount, which is what lets one image serve every arm and seed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _toml(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(path.read_text(encoding="utf-8"))


def scaffold(source: str, name: str, ref: str = "", env_root: Path = Path("environments"),
             use_local_dockerfile: bool = False) -> Path:
    """Write `environments/<name>/environment.toml` for a repo-sourced harness."""
    d = env_root / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = d / "environment.toml"
    if manifest.exists():
        raise FileExistsError(f"{manifest} already exists")
    lines = [
        "[environment]",
        f'name = "{name}"',
        f'image = ""  # filled in by `proteus env build {name}`',
        'network = "none"  # turn on only if the harness itself must reach an API',
        "",
        "[source]",
        f'repo = "{source}"',
    ]
    if ref:
        lines.append(f'ref = "{ref}"')
    if use_local_dockerfile:
        lines.append("use_local_dockerfile = true")
        stub = d / "Dockerfile"
        if not stub.exists():
            stub.write_text(
                "# Wrapper Dockerfile for a harness repo without its own.\n"
                "# Build context = the repo checkout; COPY what the harness needs.\n"
                "FROM python:3.12-slim\n"
                "COPY . /opt/harness\n"
                'WORKDIR /workspace\n',
                encoding="utf-8",
            )
    lines += ["", "[harness]", 'adapter = ""  # your <module>:<Class> once written',
              'workspace_mount = "/workspace"']
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _checkout(repo: str, ref: str, dest: Path) -> str:
    """Clone/copy the repo into `dest` and return the resolved commit sha ("" if not git)."""
    src = Path(repo).expanduser()
    if src.exists():
        if (src / ".git").exists():
            subprocess.run(["git", "clone", "-q", str(src), str(dest)], check=True)
        else:
            shutil.copytree(src, dest)
    else:
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", repo, str(dest)],
                       check=True)
    if ref and (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True)
    if (dest / ".git").exists():
        out = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    return ""


def build(name_or_manifest: str, env_root: Path = Path("environments")) -> str:
    """Build the environment image from its manifest; returns the image tag."""
    manifest = Path(name_or_manifest)
    if not manifest.is_file():
        manifest = env_root / name_or_manifest / "environment.toml"
    data = _toml(manifest)
    env, source = data["environment"], data.get("source")
    if not source:
        raise ValueError(f"{manifest} has no [source] section; nothing to build from")
    name = env["name"]

    with tempfile.TemporaryDirectory(prefix=f"proteus-env-{name}-") as tmp:
        checkout = Path(tmp) / "src"
        sha = _checkout(source["repo"], source.get("ref", ""), checkout)
        tag = f"proteus-env-{name}:{sha[:12] if sha else 'local'}"
        if source.get("use_local_dockerfile"):
            dockerfile = manifest.parent / "Dockerfile"
        else:
            dockerfile = checkout / source.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            raise FileNotFoundError(
                f"no Dockerfile: looked at {dockerfile}. Either the repo ships one "
                f"([source] dockerfile = ...), or write a wrapper at "
                f"{manifest.parent / 'Dockerfile'} and set use_local_dockerfile = true."
            )
        subprocess.run(["docker", "build", "-q", "-f", str(dockerfile),
                        "-t", tag, str(checkout)], check=True, capture_output=True)

    # record the build back into the manifest: image tag + resolved sha
    text = manifest.read_text(encoding="utf-8")
    out_lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("image ="):
            out_lines.append(f'image = "{tag}"')
        elif s.startswith("resolved_sha ="):
            continue
        else:
            out_lines.append(line)
    if sha:
        idx = next(i for i, ln in enumerate(out_lines) if ln.strip() == "[source]")
        out_lines.insert(idx + 1, f'resolved_sha = "{sha}"')
    manifest.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return tag
