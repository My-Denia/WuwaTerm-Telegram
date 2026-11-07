"""The build-time probe reports installed bytes, and refuses to report what it cannot hash."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import install_probe  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_dist(site: Path, name: str, files: dict[str, bytes], *, direct_url: dict | None = None) -> Path:
    """Install a synthetic distribution the way pip lays it out, with a RECORD."""
    info = site / f"{name.replace('-', '_')}-1.0.dist-info"
    info.mkdir(parents=True)
    files = {f"{info.name}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n".encode(), **files}
    if direct_url is not None:
        files[f"{info.name}/direct_url.json"] = json.dumps(direct_url).encode()
    rows = []
    for relative, data in files.items():
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows.append(f"{relative},{_record_hash(data)},{len(data)}")
    rows.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return info


@pytest.fixture()
def site(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "lib" / "python3.11" / "site-packages"
    path.mkdir(parents=True)
    monkeypatch.syspath_prepend(str(path))
    return path


def run(tmp_path, *argv) -> dict:
    out = tmp_path / "share" / "install-probe.json"
    assert install_probe.main(["--product", "telegram", *argv, "--out", str(out)]) == 0
    return json.loads(out.read_text())


def test_report_describes_installed_bytes(site, tmp_path):
    direct = {"url": "file:///wheels/probe_alpha.whl", "archive_info": {"hashes": {"sha256": "ab" * 32}}}
    make_dist(site, "probe-alpha", {
        "probe_alpha/__init__.py": b"VALUE = 1\n",
        "probe_alpha/__pycache__/__init__.cpython-311.pyc": b"compiled",
        "probe_alpha/old.pyc": b"compiled",
    }, direct_url=direct)
    make_dist(site, "probe-beta", {"probe_beta.py": b"B = 2\n"})
    report = run(tmp_path, "--dist", "probe-beta", "--distributions", "probe-alpha")

    assert (report["schema_version"], report["kind"], report["product"]) == (1, "install-probe", "telegram")
    assert report["interpreter"] == sys.executable
    assert [p["name"] for p in report["packages"]] == ["probe-alpha", "probe-beta"]
    alpha, beta = report["packages"]
    assert alpha["location"] == str(site) and alpha["version"] == "1.0"
    assert alpha["direct_url"] == direct and beta["direct_url"] is None
    paths = [f["path"] for f in alpha["files"]]
    assert paths == sorted(paths)
    assert not any(p.endswith(".pyc") or "__pycache__" in p for p in paths)
    # Every RECORD row counts, including the compiled files the report leaves out.
    assert alpha["record_entries"] == len(paths) + 2
    init = next(f for f in alpha["files"] if f["path"] == "probe_alpha/__init__.py")
    assert init == {"path": "probe_alpha/__init__.py", "sha256": _sha(b"VALUE = 1\n"), "size": 10}


def test_report_hashes_the_installed_bytes_not_the_record(site, tmp_path):
    make_dist(site, "probe-alpha", {"probe_alpha.py": b"A = 1\n"})
    (site / "probe_alpha.py").write_bytes(b"A = 2\n")  # RECORD still claims the old hash
    report = run(tmp_path, "--dist", "probe-alpha")
    entry = next(f for f in report["packages"][0]["files"] if f["path"] == "probe_alpha.py")
    assert entry["sha256"] == _sha(b"A = 2\n")


def test_output_is_canonical_and_deterministic(site, tmp_path):
    make_dist(site, "probe-alpha", {"probe_alpha.py": b"A = 1\n"})
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    for out in (first, second):
        assert install_probe.main(["--product", "telegram", "--dist", "probe-alpha", "--out", str(out)]) == 0
    data = first.read_bytes()
    assert data == second.read_bytes() and data.endswith(b"}\n")
    assert data == install_probe._canonical(json.loads(data))


def test_extra_file_records_hash_and_mode(site, tmp_path):
    make_dist(site, "probe-alpha", {"probe_alpha.py": b"A = 1\n"})
    entry = tmp_path / "entrypoint.sh"
    entry.write_bytes(b"#!/bin/sh\n")
    entry.chmod(0o755)
    report = run(tmp_path, "--dist", "probe-alpha", "--extra-file", str(entry))
    assert report["extra_files"] == [{"path": str(entry), "sha256": _sha(b"#!/bin/sh\n"), "mode": 0o755}]


@pytest.mark.parametrize("damage", ["delete", "symlink"])
def test_unreadable_installed_file_writes_nothing(site, tmp_path, damage):
    make_dist(site, "probe-alpha", {"probe_alpha.py": b"A = 1\n"})
    target = site / "probe_alpha.py"
    target.unlink()
    if damage == "symlink":
        target.symlink_to(site / "probe_alpha-1.0.dist-info" / "METADATA")
    out = tmp_path / "out" / "install-probe.json"
    assert install_probe.main(["--product", "telegram", "--dist", "probe-alpha", "--out", str(out)]) == 1
    assert not out.exists()


def test_missing_distribution_is_refused(site, tmp_path):
    out = tmp_path / "out.json"
    assert install_probe.main(["--product", "telegram", "--dist", "probe-absent", "--out", str(out)]) == 1
    assert not out.exists()


def test_existing_report_is_never_replaced(site, tmp_path):
    make_dist(site, "probe-alpha", {"a.py": b"1\n"})
    out = tmp_path / "install-probe.json"
    out.write_text("earlier\n")
    assert install_probe.main(["--product", "telegram", "--dist", "probe-alpha", "--out", str(out)]) == 1
    assert out.read_text() == "earlier\n"


@pytest.mark.parametrize("argv", [
    ["--dist", "probe-alpha", "--dist", "probe-alpha"],
    ["--dist", "probe-alpha", "--extra-file", "relative.sh"],
])
def test_invalid_arguments_are_refused_without_a_report(site, tmp_path, argv):
    make_dist(site, "probe-alpha", {"a.py": b"1\n"})
    out = tmp_path / "o.json"
    assert install_probe.main(["--product", "telegram", *argv, "--out", str(out)]) == 1
    assert not out.exists()


def test_unknown_product_is_a_usage_error(site, tmp_path):
    with pytest.raises(SystemExit):
        install_probe.main(["--product", "other", "--dist", "probe-alpha", "--out", str(tmp_path / "o.json")])


def test_command_line_runs_isolated_against_an_installed_distribution(tmp_path):
    out = tmp_path / "pytest.json"
    subprocess.run([sys.executable, "-I", "-B", str(SCRIPTS / "install_probe.py"), "--product", "telegram",
                    "--dist", "pytest", "--out", str(out)], check=True, cwd=tmp_path)
    package = json.loads(out.read_text())["packages"][0]
    root = Path(package["location"])
    assert package["name"] == "pytest" and package["files"]
    for item in package["files"]:
        assert _sha((root / item["path"]).read_bytes()) == item["sha256"]
