"""The Docker context is built from either the reviewed Core lock or one explicit local Core."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import core_lock  # noqa: E402
import install_verified  # noqa: E402
import prepare_docker_context  # noqa: E402


def make_wheel(path: Path, name: str, version: str) -> str:
    with ZipFile(path, "w") as z:
        z.writestr(f"{name}-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def wheels(tmp_path):
    core_version = core_lock.product_core_version()
    telegram_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    core = tmp_path / "in" / f"wuwaterm-{core_version}-py3-none-any.whl"
    telegram = tmp_path / "in" / f"wuwaterm_telegram-{telegram_version}-py3-none-any.whl"
    core.parent.mkdir()
    return core, make_wheel(core, "wuwaterm", core_version), telegram, make_wheel(telegram, "wuwaterm-telegram", telegram_version)


def prepare(monkeypatch, tmp_path, *argv) -> Path:
    out = tmp_path / "context"
    monkeypatch.setattr(sys, "argv", ["prepare_docker_context", *argv, "--output-dir", str(out)])
    prepare_docker_context.main()
    return out


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_default_context_uses_the_given_lock_and_stages_the_probe(tmp_path, monkeypatch, capsys, wheels):
    core, core_sha, telegram, telegram_sha = wheels
    lock = json.loads((ROOT / "core-lock.json").read_text())
    lock.update(sha256=core_sha, expires_at="2999-01-01T00:00:00Z")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    committed = (ROOT / "core-lock.json").read_bytes()
    context = prepare(monkeypatch, tmp_path, "--lock", str(lock_path), "--core", str(core),
                      "--telegram", str(telegram), "--telegram-sha256", telegram_sha)
    assert (context / "core-lock.json").read_bytes() == lock_path.read_bytes()
    assert (context / "scripts/install_probe.py").read_bytes() == (ROOT / "scripts/install_probe.py").read_bytes()
    assert (ROOT / "core-lock.json").read_bytes() == committed
    assert "CORE_WHEEL_SHA256" not in capsys.readouterr().out


def test_local_core_context_has_no_lock_and_names_the_build_arg(tmp_path, monkeypatch, capsys, wheels):
    core, core_sha, telegram, telegram_sha = wheels
    committed = (ROOT / "core-lock.json").read_bytes()
    context = prepare(monkeypatch, tmp_path, "--local-core-wheel", str(core), "--local-core-sha256", core_sha,
                      "--telegram", str(telegram), "--telegram-sha256", telegram_sha)
    assert "core-lock.json" not in files(context)
    assert {"scripts/install_probe.py", "scripts/install_verified.py", f"wheels/{core.name}"} <= files(context)
    assert (context / "wheels/core.filename").read_text() == core.name + "\n"
    assert f"--build-arg CORE_WHEEL_SHA256={core_sha}" in capsys.readouterr().out
    assert (ROOT / "core-lock.json").read_bytes() == committed


@pytest.mark.parametrize("change", ["hash", "version", "name"])
def test_unverified_local_core_creates_no_context(tmp_path, monkeypatch, wheels, change):
    core, core_sha, telegram, telegram_sha = wheels
    if change == "hash":
        core_sha = "0" * 64
    else:
        core_sha = make_wheel(core, "wuwaterm" if change == "version" else "other",
                              "9.9" if change == "version" else core_lock.product_core_version())
    with pytest.raises(SystemExit, match="SHA-256 mismatch|wrong version|wrong package name"):
        prepare(monkeypatch, tmp_path, "--local-core-wheel", str(core), "--local-core-sha256", core_sha,
                "--telegram", str(telegram), "--telegram-sha256", telegram_sha)
    assert not (tmp_path / "context").exists()


@pytest.mark.parametrize("mixture", [
    ["--lock", "L", "--local-core-sha256", "H", "--core", "C"],
    ["--lock", "L", "--local-core-wheel", "C"],
    ["--local-core-wheel", "C"],
    ["--lock", "L", "--core", "C", "--local-core-wheel", "C"],
    ["--core", "C"],
])
def test_official_and_local_inputs_cannot_be_mixed(tmp_path, monkeypatch, wheels, mixture):
    core, core_sha, telegram, telegram_sha = wheels
    values = {"L": str(ROOT / "core-lock.json"), "H": core_sha, "C": str(core)}
    with pytest.raises(SystemExit) as error:
        prepare(monkeypatch, tmp_path, *(values.get(a, a) for a in mixture),
                "--telegram", str(telegram), "--telegram-sha256", telegram_sha)
    assert error.value.code == 2 and not (tmp_path / "context").exists()


def test_install_verified_local_core_reaches_pip_only_after_a_match(tmp_path, monkeypatch, wheels):
    core, core_sha, telegram, telegram_sha = wheels
    calls = []
    monkeypatch.setattr(install_verified.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    argv = ["install_verified", "--local-core-wheel", str(core), "--telegram", str(telegram),
            "--telegram-sha256", telegram_sha, "--local-core-sha256"]
    monkeypatch.setattr(sys, "argv", [*argv, "1" * 64])
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        install_verified.main()
    assert calls == []
    monkeypatch.setattr(sys, "argv", [*argv, core_sha])
    install_verified.main()
    assert [Path(a).name for a in calls[0][-2:]] == [core.name, telegram.name]


DOCKERFILE = (ROOT / "deploy/Dockerfile").read_text()
INSTRUCTIONS = [" ".join(m.group(0).replace("\\\n", " ").split()) for m in re.finditer(r"^[A-Z]+ (?:.*\\\n)*.*", DOCKERFILE, re.M)]


def test_probe_is_the_last_filesystem_change_and_runs_in_its_own_layer():
    runs = [i for i in INSTRUCTIONS if i.startswith("RUN ")]
    assert "install_probe.py" in runs[-1] and "install_verified.py" not in runs[-1]
    assert "--out /usr/local/share/wuwaterm/install-probe.json" in runs[-1]
    assert "--dist wuwaterm --dist wuwaterm-telegram" in runs[-1]
    after = INSTRUCTIONS[INSTRUCTIONS.index(runs[-1]) + 1:]
    assert [i.split()[0] for i in after] == ["ENTRYPOINT", "CMD"]
    assert [i for i in INSTRUCTIONS if i.startswith("COPY scripts ")] and \
        INSTRUCTIONS.index("COPY scripts ./scripts") < INSTRUCTIONS.index(runs[-1])


def test_core_source_is_selected_by_one_build_argument():
    assert 'ARG CORE_WHEEL_SHA256=""' in INSTRUCTIONS
    install = next(i for i in INSTRUCTIONS if i.startswith("RUN ") and "install_verified.py" in i)
    local, official = re.search(r'then (.*?); else (.*?); fi', install).groups()
    assert '[ -n "$CORE_WHEEL_SHA256" ]' in install and "test ! -e core-lock.json" in local and "exit 1" in local
    assert '--local-core-sha256 "$CORE_WHEEL_SHA256"' in local and "--lock" not in local
    assert "--lock core-lock.json" in official and "CORE_WHEEL_SHA256" not in official


def test_every_script_the_dockerfile_runs_is_staged_in_the_context(tmp_path, monkeypatch, wheels):
    core, core_sha, telegram, telegram_sha = wheels
    context = prepare(monkeypatch, tmp_path, "--local-core-wheel", str(core), "--local-core-sha256", core_sha,
                      "--telegram", str(telegram), "--telegram-sha256", telegram_sha)
    called = set(re.findall(r"scripts/[a-z_]+\.py", "\n".join(INSTRUCTIONS)))
    assert called == {"scripts/install_verified.py", "scripts/install_probe.py"}
    assert called <= files(context)
