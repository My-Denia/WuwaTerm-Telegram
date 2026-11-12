"""Prove the advisory gate fails on high, critical and unreadable, and only those."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-audit.yml"

spec = importlib.util.spec_from_file_location(
    "check_dependency_advisories", ROOT / "scripts/check_dependency_advisories.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

CRITICAL = {"GHSA-6q48-6c7w-9gw2": {"database_specific": {"severity": "CRITICAL"}}}
HIGH = {"GHSA-h5c8-rqwp-cp95": {"database_specific": {"severity": "HIGH"}}}
MODERATE = {"GHSA-9wx4-h78v-vm56": {"database_specific": {"severity": "MODERATE"}}}
LOW = {"GHSA-aaaa-bbbb-cccc": {"database_specific": {"severity": "LOW"}}}


class Osv:
    """Stand-in for the OSV lookup that records what was asked."""

    def __init__(self, records=None, error=None):
        self.records = records or {}
        self.error = error
        self.asked = []

    def __call__(self, vuln_id):
        self.asked.append(vuln_id)
        if self.error is not None:
            raise gate.AuditError(self.error)
        return self.records.get(vuln_id)


def _vuln(advisory_id, fix_versions=(), aliases=()):
    return {
        "id": advisory_id,
        "fix_versions": list(fix_versions),
        "aliases": list(aliases),
        "description": "fixture advisory",
    }


def _dependency(name, version="1.0", vulns=(), skip_reason=None):
    entry = {"name": name, "version": version, "vulns": list(vulns)}
    if skip_reason is not None:
        entry["skip_reason"] = skip_reason
    return entry


def _report(tmp_path, dependencies, name="pip-audit.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"dependencies": dependencies, "fixes": []}))
    return path


def test_clean_report_passes_without_asking_osv(tmp_path, capsys):
    osv = Osv()
    report = _report(tmp_path, [_dependency("httpx", "0.28.4")])

    assert gate.main([str(report)], fetch=osv) == 0
    assert osv.asked == []
    output = capsys.readouterr().out
    assert "1 package audited" in output
    assert "no advisories" in output
    assert "PASSED" in output


def test_critical_advisory_blocks_and_names_the_fix(tmp_path, capsys):
    osv = Osv(CRITICAL)
    report = _report(
        tmp_path,
        [
            _dependency(
                "certifi",
                "2026.6.17",
                [_vuln("GHSA-6q48-6c7w-9gw2", ["2026.10.19"])],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    captured = capsys.readouterr()
    assert "critical" in captured.out
    assert "certifi" in captured.out
    assert "2026.6.17" in captured.out
    assert "GHSA-6q48-6c7w-9gw2" in captured.out
    assert "fix: 2026.10.19" in captured.out
    assert "::error title=certifi GHSA-6q48-6c7w-9gw2::" in captured.out
    assert "FAILED: 1 blocking advisory" in captured.err


def test_high_advisory_blocks(tmp_path):
    osv = Osv(HIGH)
    report = _report(
        tmp_path, [_dependency("jinja2", "3.1.4", [_vuln("GHSA-h5c8-rqwp-cp95", ["3.1.5"])])]
    )

    assert gate.main([str(report)], fetch=osv) == 1


def test_moderate_and_low_are_reported_but_do_not_block(tmp_path, capsys):
    osv = Osv({**MODERATE, **LOW})
    report = _report(
        tmp_path,
        [
            _dependency(
                "requests",
                "2.31.0",
                [
                    _vuln("GHSA-9wx4-h78v-vm56", ["2.32.0"]),
                    _vuln("GHSA-aaaa-bbbb-cccc"),
                ],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 0
    captured = capsys.readouterr()
    assert "moderate" in captured.out
    assert "low" in captured.out
    assert "GHSA-9wx4-h78v-vm56" in captured.out
    assert "no fixed version published" in captured.out
    assert "::error" not in captured.out
    assert "PASSED" in captured.out


def test_an_advisory_reported_twice_is_one_finding(tmp_path, capsys):
    osv = Osv(HIGH)
    duplicate = _vuln("GHSA-h5c8-rqwp-cp95", ["3.1.5"])
    report = _report(
        tmp_path, [_dependency("jinja2", "3.1.4", [duplicate, dict(duplicate)])]
    )

    assert gate.main([str(report)], fetch=osv) == 1
    assert osv.asked == ["GHSA-h5c8-rqwp-cp95"]
    output = capsys.readouterr().out
    assert "totals: 1 advisory (1 high)" in output


def test_severity_comes_from_the_ghsa_alias_when_the_primary_has_none(tmp_path):
    """A PYSEC record carries a CVSS vector, not GitHub's label."""
    osv = Osv(
        {
            "PYSEC-2026-4320": {"severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/x"}]},
            **CRITICAL,
        }
    )
    report = _report(
        tmp_path,
        [
            _dependency(
                "certifi",
                "2026.6.17",
                [_vuln("PYSEC-2026-4320", ["2026.10.18"], ["GHSA-6q48-6c7w-9gw2"])],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    # The GHSA is asked for first precisely so the PYSEC record - which has no
    # label to read - is not needed.
    assert osv.asked == ["GHSA-6q48-6c7w-9gw2"]


def test_records_sharing_a_cve_are_one_vulnerability(tmp_path, capsys):
    osv = Osv({"PYSEC-2024-38": {"database_specific": {"severity": "HIGH"}}})
    report = _report(
        tmp_path,
        [
            _dependency(
                "python-multipart",
                "0.0.6",
                [
                    _vuln("PYSEC-2024-38", ["0.0.7"], ["CVE-2024-24762", "PYSEC-2026-1850"]),
                    _vuln("PYSEC-2026-1850", ["0.0.7"], ["CVE-2024-24762"]),
                ],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    output = capsys.readouterr().out
    assert "totals: 1 advisory (1 high)" in output
    # The second record survives as an identifier on the first row, not as a
    # row of its own.
    assert output.count("python-multipart  0.0.6") == 1
    assert "PYSEC-2026-1850" in output


def test_grouping_keeps_other_packages_apart():
    shared = ("PYSEC-1", ("CVE-9",), ("2.0",))

    grouped = gate.group_advisories(
        [gate.Advisory("a", "1.0", *shared), gate.Advisory("b", "1.0", *shared)]
    )

    assert [advisory.package for advisory in grouped] == ["a", "b"]


def test_unreadable_severity_blocks(tmp_path, capsys):
    osv = Osv({})
    report = _report(tmp_path, [_dependency("certifi", "2026.6.17", [_vuln("PYSEC-1")])])

    assert gate.main([str(report)], fetch=osv) == 1
    captured = capsys.readouterr()
    assert "severity could not be read from OSV" in captured.out
    assert "unknown" in captured.out


def test_lookup_order_prefers_the_ghsa_identifier():
    advisory = gate.Advisory(
        "pkg", "1.0", "PYSEC-1", ("CVE-2", "GHSA-abc-def-ghi"), ()
    )

    assert gate.lookup_order(advisory) == ("GHSA-abc-def-ghi", "PYSEC-1", "CVE-2")


@pytest.mark.parametrize(
    "audit_exit, dependencies, fragment",
    [
        (0, [], "lists no audited dependencies"),
        (2, [_dependency("httpx")], "the audit did not complete"),
        (1, [_dependency("httpx")], "report lists no advisories"),
        (0, [_dependency("httpx", skip_reason="unresolvable")], "could not audit"),
    ],
)
def test_untrustworthy_audits_are_red_not_green(tmp_path, capsys, audit_exit, dependencies, fragment):
    report = _report(tmp_path, dependencies)

    assert gate.main([str(report), "--audit-exit", str(audit_exit)], fetch=Osv()) == 2
    assert fragment in capsys.readouterr().err


def test_missing_report_is_red(tmp_path, capsys):
    assert gate.main([str(tmp_path / "absent.json")], fetch=Osv()) == 2
    assert "could not be read" in capsys.readouterr().err


def test_truncated_report_is_red(tmp_path, capsys):
    """A killed audit can leave valid-looking output on disk."""
    path = tmp_path / "pip-audit.json"
    path.write_text('{"dependencies": [{"name": "httpx", "vers')
    assert gate.main([str(path)], fetch=Osv()) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_malformed_report_is_red(tmp_path, capsys):
    path = tmp_path / "pip-audit.json"
    path.write_text(json.dumps({"dependencies": [{"name": "httpx"}]}))

    assert gate.main([str(path)], fetch=Osv()) == 2
    assert "without name or version" in capsys.readouterr().err


def test_osv_lookup_failure_is_red(tmp_path, capsys):
    osv = Osv(error="OSV lookup for PYSEC-1 failed with HTTP 503")
    report = _report(tmp_path, [_dependency("certifi", "1.0", [_vuln("PYSEC-1")])])

    assert gate.main([str(report)], fetch=osv) == 2
    assert "HTTP 503" in capsys.readouterr().err


def test_the_audited_input_is_the_resolved_set_not_a_lock():
    """The workflow has no lock to read; it resolves, freezes, then audits."""
    text = WORKFLOW.read_text()

    assert "no lock file here to scan" in text
    assert "scripts/install_verified.py" in text
    assert "pip freeze" in text
    assert "--no-deps" in text
    assert "pip-audit==2.10.1" in text
    for trigger in ("pyproject.toml", "core-lock.json", "dependency-audit.yml"):
        assert trigger in text
    assert "cron:" in text
    assert "workflow_dispatch:" in text
    # The report is evidence for a red run too, and a re-run must not collide
    # with the artifact name of the attempt it replaces.
    assert "if: always()" in text
    assert "run_attempt" in text
    assert "permissions:\n  contents: read" in text
