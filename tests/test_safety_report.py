from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest

from proteus.report import write_report


class AuditTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_audit_body = False
        self.cells: list[str] = []
        self.links: list[str] = []
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        if tag == "tbody" and values.get("id") == "audit-rows":
            self.in_audit_body = True
        elif self.in_audit_body and tag == "td":
            self._cell_parts = []
        elif self.in_audit_body and tag == "a" and "href" in values:
            self.links.append(values["href"])

    def handle_endtag(self, tag) -> None:
        if tag == "tbody" and self.in_audit_body:
            self.in_audit_body = False
        elif tag == "td" and self._cell_parts is not None:
            self.cells.append("".join(self._cell_parts).strip())
            self._cell_parts = None

    def handle_data(self, data) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)


class GateTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_gate_body = False
        self.cells: list[str] = []
        self.links: list[str] = []
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        if tag == "tbody" and values.get("id") == "gate-rows":
            self.in_gate_body = True
        elif self.in_gate_body and tag == "td":
            self._cell_parts = []
        elif self.in_gate_body and tag == "a" and "href" in values:
            self.links.append(values["href"])

    def handle_endtag(self, tag) -> None:
        if tag == "tbody" and self.in_gate_body:
            self.in_gate_body = False
        elif tag == "td" and self._cell_parts is not None:
            self.cells.append("".join(self._cell_parts).strip())
            self._cell_parts = None

    def handle_data(self, data) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)


def test_report_has_hidden_optional_audit_section(tmp_path) -> None:
    html = write_report(tmp_path).read_text()

    assert 'id="audit-section" hidden' in html
    assert "post-run evidence; never fed back into evolution" in html
    assert "composite safety score" not in html.lower()


def test_report_uses_a_separate_audit_table(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "episodes": 1,
                "arms": [],
                "seeds": 0,
                "runs": [],
            }
        )
    )
    audit = tmp_path / "audits/integrity"
    audit.mkdir(parents=True)
    (audit / "summary.json").write_text(
        json.dumps(
            {
                "status_counts": {"pass": 3, "not_evaluated": 1},
                "target_counts": {"trace": 4},
                "evidence_method_counts": {"artifact": 4},
            }
        )
    )
    (tmp_path / "audits/index.json").write_text(
        json.dumps(
            {
                "audits": [
                    {
                        "id": "integrity",
                        "suite": "instrument-integrity",
                        "version": "1",
                        "summary": "integrity/summary.json",
                        "results": "integrity/results.jsonl",
                    }
                ]
            }
        )
    )

    html = write_report(tmp_path).read_text()
    parser = AuditTableParser()
    parser.feed(html)

    assert "Safety audits" in html
    assert 'id="audit-rows"' in html
    assert 'id="tbl"' in html
    assert "last score" in html
    assert 'id="audit-section" hidden' not in html
    assert parser.cells[:5] == [
        "integrity",
        "instrument-integrity 1",
        "not_evaluated:1  pass:3",
        "trace:4",
        "artifact:4",
    ]
    assert parser.cells[5] == "completed"
    assert parser.links == [
        "audits/integrity/summary.json",
        "audits/integrity/results.jsonl",
    ]


def test_report_skips_malformed_entries_and_escapes_text(tmp_path) -> None:
    audit = tmp_path / "audits/good"
    audit.mkdir(parents=True)
    (audit / "summary.json").write_text(
        json.dumps(
            {
                "status_counts": {"pass": 1},
                "target_counts": {"trace": 1},
                "evidence_method_counts": {"artifact": 1},
            }
        )
    )
    (tmp_path / "audits/index.json").write_text(
        json.dumps(
            {
                "audits": [
                    None,
                    {
                        "id": "good",
                        "suite": "<script>alert(1)</script>",
                        "version": "1",
                        "summary": "good/summary.json",
                        "results": "good/results&details.jsonl",
                    },
                ]
            }
        )
    )

    html = write_report(tmp_path).read_text()
    parser = AuditTableParser()
    parser.feed(html)

    assert parser.cells[0] == "good"
    assert parser.cells[1] == "<script>alert(1)</script> 1"
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "results&amp;details.jsonl" in html
    assert parser.links[-1] == "audits/good/results&details.jsonl"


@pytest.mark.parametrize("index_text", ["not-json", "[]", '{"audits": "bad"}'])
def test_report_hides_audits_for_malformed_index(tmp_path, index_text: str) -> None:
    audits = tmp_path / "audits"
    audits.mkdir()
    (audits / "index.json").write_text(index_text)

    html = write_report(tmp_path).read_text()

    assert 'id="audit-section" hidden' in html
    parser = AuditTableParser()
    parser.feed(html)
    assert parser.cells == []


@pytest.mark.parametrize("summary_text", [None, "not-json", "[]"])
def test_report_skips_missing_or_malformed_summary(
    tmp_path, summary_text: str | None
) -> None:
    audit = tmp_path / "audits/bad"
    audit.mkdir(parents=True)
    if summary_text is not None:
        (audit / "summary.json").write_text(summary_text)
    (tmp_path / "audits/index.json").write_text(
        json.dumps(
            {
                "audits": [
                    {
                        "id": "bad",
                        "suite": "fixture",
                        "version": "1",
                        "summary": "bad/summary.json",
                        "results": "bad/results.jsonl",
                    }
                ]
            }
        )
    )

    html = write_report(tmp_path).read_text()

    assert 'id="audit-section" hidden' in html
    parser = AuditTableParser()
    parser.feed(html)
    assert parser.cells == []


def _write_terminal_gate(sweep, *, complete: bool = True) -> None:
    gate_root = sweep / "safety-gates/run-1"
    candidate = gate_root / "candidate-0001"
    candidate.mkdir(parents=True)
    decision_ref = "safety-gates/run-1/candidate-0001/decision.json"
    (sweep / "safety-gates/manifest.json").write_text(
        json.dumps({"schema_version": "proteus-evolution-safety-gates/1"})
    )
    (gate_root / "activations.jsonl").write_text(
        json.dumps(
            {
                "episode": 1,
                "candidate": {"run_id": "run-1", "episode": 1, "role": "candidate"},
                "allowed": False,
                "status": "not_evaluated",
                "decision_ref": decision_ref,
            }
        )
        + "\n"
    )
    (candidate / "transition.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "episode": 1,
                "active": {"run_id": "run-1", "episode": 0, "role": "active"},
                "candidate": {"run_id": "run-1", "episode": 1, "role": "candidate"},
            }
        )
    )
    (candidate / "decision.json").write_text(
        json.dumps(
            {
                "status": "not_evaluated",
                "allowed": False,
                "blockers": [
                    {
                        "code": "critical_not_evaluated",
                        "family_id": "memory_bad_admission",
                        "indicator": "recovery_rollback",
                        "component": "verified_restoration",
                    }
                ],
                "warnings": ["archive lineage unavailable"],
            }
        )
    )
    (candidate / "observations.jsonl").write_text("")
    if complete:
        (candidate / "indicators.json").write_text(
            json.dumps(
                {
                    "active": {"run_id": "run-1", "episode": 0, "role": "active"},
                    "candidate": {"run_id": "run-1", "episode": 1, "role": "candidate"},
                    "assessments": {
                        "memory_bad_admission": [
                            {
                                "indicator": "recovery_rollback",
                                "status": "not_evaluated",
                                "direction": "unknown",
                                "components": [
                                    {
                                        "component_id": "verified_restoration",
                                        "planned": 2,
                                        "eligible": 2,
                                        "evaluated": 0,
                                        "unavailable": 2,
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        )
    progress = sweep / "progress"
    progress.mkdir()
    (progress / "run-1.jsonl").write_text(
        json.dumps(
            {
                "episode": 1,
                "task_selected": True,
                "activated": False,
                "decision_ref": decision_ref,
            }
        )
        + "\n"
    )


def test_report_renders_terminal_gate_history_without_rejected_as_active(tmp_path) -> None:
    _write_terminal_gate(tmp_path)

    rendered = write_report(tmp_path).read_text()
    parser = GateTableParser()
    parser.feed(rendered)

    assert 'id="gate-section" hidden' not in rendered
    assert "Candidate activation history" in rendered
    assert parser.cells[:5] == [
        "run-1",
        "episode 0 (active)",
        "episode 1 (candidate)",
        "selected",
        "not_evaluated",
    ]
    assert "rejected" in parser.cells
    assert "recovery_rollback: not_evaluated / unknown (0/2 evaluated)" in rendered
    assert "critical_not_evaluated" in rendered
    assert "archive lineage unavailable" in rendered
    assert parser.links == [
        "safety-gates/run-1/candidate-0001/transition.json",
        "safety-gates/run-1/candidate-0001/indicators.json",
        "safety-gates/run-1/candidate-0001/decision.json",
    ]
    assert "combined score" not in rendered.lower()
    assert "safety score" not in rendered.lower()


def test_report_rejects_activation_index_rows_with_partial_candidate_artifacts(tmp_path) -> None:
    _write_terminal_gate(tmp_path, complete=False)

    rendered = write_report(tmp_path).read_text()
    parser = GateTableParser()
    parser.feed(rendered)

    assert 'id="gate-section" hidden' in rendered
    assert parser.cells == []
