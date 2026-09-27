"""Render a small, secret-redacted failure summary for the upstream canary."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


def _redact(text: str, secret: str = "") -> str:
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    return text


def render(root: Path, *, upstream_sha: str = "", source_outcome: str = "",
           bake_outcome: str = "",
           live_outcome: str = "", smoke_outcome: str = "", secret: str = "") -> str:
    """Summarize seed errors without copying traces, prompts, or candidate files."""
    rows = [
        "Proteus upstream canary diagnostics",
        f"upstream_sha: {upstream_sha or 'unavailable (upstream checkout did not complete)'}",
        f"source_outcome: {source_outcome or 'unknown'}",
        f"bake_outcome: {bake_outcome or 'unknown'}",
        f"live_outcome: {live_outcome or 'unknown'}",
        f"smoke_outcome: {smoke_outcome or 'unknown'}",
    ]
    records_path = root / "seeds.jsonl"
    if not records_path.is_file():
        rows.append("seed_results: unavailable (the sweep did not write seeds.jsonl)")
        return "\n".join(rows) + "\n"

    records = []
    for line_number, line in enumerate(records_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append(f"seed_results: unreadable JSON at line {line_number}")
            continue

    if not records:
        rows.append("seed_results: no seed records")
    for record in records:
        arm = _redact(str(record.get("arm", "unknown")), secret)
        seed = _redact(str(record.get("seed", "unknown")), secret)
        completed = record.get("episodes_complete", "unknown")
        error = _redact(str(record.get("error") or ""), secret).strip()
        if not error and record.get("episodes_complete", 0) < 1:
            error = "no EpisodeResult.error was recorded"
        if not error:
            error = "none"
        rows.append(f"seed: arm={arm} seed={seed} episodes_complete={completed}; error={error[:800]}")
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    parser.add_argument("--output", type=Path,
                        help="write diagnostics here instead of stdout")
    parser.add_argument("--upstream-sha", default=os.environ.get("UPSTREAM_SHA", ""))
    parser.add_argument("--source-outcome", default=os.environ.get("SOURCE_OUTCOME", ""))
    parser.add_argument("--bake-outcome", default=os.environ.get("BAKE_OUTCOME", ""))
    parser.add_argument("--live-outcome", default=os.environ.get("LIVE_OUTCOME", ""))
    parser.add_argument("--smoke-outcome", default=os.environ.get("SMOKE_OUTCOME", ""))
    args = parser.parse_args()
    text = render(
        args.sweep_root,
        upstream_sha=args.upstream_sha,
        source_outcome=args.source_outcome,
        bake_outcome=args.bake_outcome,
        live_outcome=args.live_outcome,
        smoke_outcome=args.smoke_outcome,
        secret=os.environ.get("DEEPSEEK_API_KEY", ""),
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
