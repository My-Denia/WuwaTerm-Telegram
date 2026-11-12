"""The delivery builder seals a real OCI archive, and refuses what Core would refuse.

The Docker daemon is the only replaced part: a fake ``docker`` executable returns a
real-format OCI archive for ``image save``. Archive parsing, layer reconstruction,
source archiving, the context builder, sealing and the self-check all run for real.

The synthetic image carries a self-consistent installed filesystem: the wheels'
members, the ``.dist-info`` records and the probe report all describe the same
bytes, so a passing self-check proves the reconstruction, not just the fixture.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_delivery.py"
sys.path.insert(0, str(ROOT / "scripts"))
import build_delivery as bd  # noqa: E402

CORE_COMMIT = "c" * 40
SITE = "/usr/local/lib/python3.11/site-packages"
INTERPRETER = "/usr/local/bin/python"
BIN = posixpath.dirname(INTERPRETER)
DATA_ROOT = posixpath.normpath(posixpath.join(SITE, "../../.."))
PROGRAM_PATH = "/app/scripts/install_probe.py"
REPORT_DIRS = ("usr", "usr/local", "usr/local/share", "usr/local/share/wuwaterm")
REPORT_NAME = bd.REPORT_PATH.lstrip("/")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------ real-format OCI archive

def tar_bytes(entries: list[tuple[str, str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.mode, info.mtime = 0o644, 0
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = data.decode()
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def stub_report(product: str = "telegram", names: tuple[str, ...] = ("wuwaterm", "wuwaterm-telegram")) -> bytes:
    """A well-formed probe report that describes nothing; structural tests only."""
    packages = [{"name": n, "version": "1", "location": "/site", "direct_url": None, "record_entries": 1,
                 "files": [{"path": "m.py", "sha256": "0" * 64, "size": 1}]} for n in names]
    return bd.canonical({"schema_version": 1, "kind": "install-probe", "product": product,
                         "interpreter": "/usr/local/bin/python", "python_version": "3.11.0",
                         "site_packages": "/site", "packages": packages, "extra_files": []})


def report_layer(extra: list[tuple[str, str, bytes]] = (), report: bytes | None = None) -> list[tuple[str, str, bytes]]:
    entries = [(d, "dir", b"") for d in REPORT_DIRS]
    return entries + [(REPORT_NAME, "file", stub_report() if report is None else report)] + list(extra)


def oci_archive(layers: list[list[tuple[str, str, bytes]]], *, annotations: dict | None = None, repo_tags=None,
                corrupt_layer: bool = False, stray: bool = False,
                extra_blobs: dict[str, bytes] | None = None, compat_extra: dict | None = None,
                compat_layers: list[str] | None = None) -> bytes:
    blobs: dict[str, bytes] = {}
    descriptors, diff_ids = [], []
    for number, entries in enumerate(layers):
        plain = tar_bytes(entries)
        compressed = gzip.compress(plain, mtime=0)
        digest = sha(compressed)
        if corrupt_layer and number == len(layers) - 1:
            compressed = compressed[:-8] + bytes(8)
        blobs[digest] = compressed
        descriptors.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                            "digest": "sha256:" + digest, "size": len(compressed)})
        diff_ids.append("sha256:" + sha(plain))
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {},
                         "rootfs": {"type": "layers", "diff_ids": diff_ids}}).encode()
    blobs[sha(config)] = config
    manifest = json.dumps({"schemaVersion": 2, "mediaType": bd.MANIFEST_MEDIA,
                           "config": {"mediaType": bd.CONFIG_MEDIA, "digest": "sha256:" + sha(config), "size": len(config)},
                           "layers": descriptors}).encode()
    blobs[sha(manifest)] = manifest
    index = {"schemaVersion": 2, "mediaType": bd.INDEX_MEDIA,
             "manifests": [{"mediaType": bd.MANIFEST_MEDIA, "digest": "sha256:" + sha(manifest), "size": len(manifest),
                            "annotations": dict({"org.opencontainers.image.created": "2026-11-07T00:00:00Z"},
                                                **(annotations or {}))}]}
    docker = [{"Config": "blobs/sha256/" + sha(config), "RepoTags": repo_tags,
               "Layers": compat_layers if compat_layers is not None
               else ["blobs/sha256/" + d["digest"][7:] for d in descriptors]}]
    docker[0].update(compat_extra or {})
    entries = [("blobs", "dir", b""), ("blobs/sha256", "dir", b"")]
    entries += [("blobs/sha256/" + k, "file", v) for k, v in sorted(blobs.items())]
    entries += [("index.json", "file", json.dumps(index).encode()), ("manifest.json", "file", json.dumps(docker).encode()),
                ("oci-layout", "file", b'{"imageLayoutVersion":"1.0.0"}')]
    if stray:
        entries.append(("repositories", "file", b"{}"))
    for data in (extra_blobs or {}).values():
        entries.append(("blobs/sha256/" + sha(data), "file", data))
    return tar_bytes(entries)


def image(**options) -> bytes:
    return oci_archive([[("app", "dir", b""), ("app/x", "file", b"x")], report_layer()], **options)


def read(tmp_path: Path, raw: bytes) -> bd.OciArchive:
    path = tmp_path / "image.oci.tar"
    path.write_bytes(raw)
    return bd.read_oci(path)


def test_read_oci_returns_facts_and_the_final_layer_report(tmp_path):
    archive = read(tmp_path, image())
    assert archive.report == stub_report()
    assert archive.facts["platform"] == {"os": "linux", "architecture": "amd64", "variant": None}
    assert len(archive.facts["layers"]) == 2
    assert all(bd.DIGEST.fullmatch(layer["diff_id"]) for layer in archive.facts["layers"])
    assert archive.facts["archive_sha256"] == sha((tmp_path / "image.oci.tar").read_bytes())


def test_a_config_digest_annotation_must_match_the_manifest(tmp_path):
    # Docker's containerd image store writes this annotation on the index
    # descriptor. It is harmless when it agrees with the manifest config.
    facts = read(tmp_path, image()).facts
    accepted = read(tmp_path, image(annotations={"config.digest": facts["config_digest"]}))
    assert accepted.facts["config_digest"] == facts["config_digest"]
    with pytest.raises(bd.BuildError, match="config digest annotation differs"):
        read(tmp_path, image(annotations={"config.digest": "sha256:" + "0" * 64}))


def test_the_reconstructed_filesystem_honours_whiteouts(tmp_path):
    archive = read(tmp_path, oci_archive([
        [("app", "dir", b""), ("app/gone", "file", b"x"), ("app/kept", "file", b"y")],
        [("app/.wh.gone", "file", b""), ("app/.wh..wh..opq", "file", b"")],
        report_layer(),
    ]))
    assert "app" in archive.rootfs.nodes
    assert "app/gone" not in archive.rootfs.nodes and "app/kept" not in archive.rootfs.nodes
    assert REPORT_NAME in archive.rootfs.nodes


def test_a_hard_link_is_resolved_to_the_file_it_names(tmp_path):
    archive = read(tmp_path, oci_archive([
        [("bin", "dir", b""), ("bin/real", "file", b"payload")],
        [("bin/link", "hardlink", b"bin/real")],
        report_layer(),
    ]))
    assert archive.rootfs.nodes["bin/link"].sha256 == sha(b"payload")


@pytest.mark.parametrize("raw, reason", [
    (image(annotations={"io.containerd.image.name": "x:latest"}), "name annotation"),
    (image(annotations={"org.opencontainers.image.ref.name": "x"}), "name annotation"),
    (image(repo_tags=["x:latest"]), "RepoTags"),
    (image(corrupt_layer=True), "invalid layer|differs"),
    (image(stray=True), "unexpected OCI archive entry"),
    (oci_archive([[("app/x", "file", b"x")], report_layer(extra=[("etc/passwd", "file", b"x")])]), "more than the probe"),
    (oci_archive([[("app/x", "file", b"x")], report_layer(extra=[(".wh.app", "file", b"")])]), "more than the probe"),
    (oci_archive([[("app/x", "file", b"x")], [(d, "dir", b"") for d in REPORT_DIRS]]), "does not add the probe report"),
])
def test_read_oci_refuses_a_tampered_or_named_archive(tmp_path, raw, reason):
    with pytest.raises(bd.BuildError, match=reason):
        read(tmp_path, raw)


@pytest.mark.parametrize("raw", [stub_report("core"), stub_report(names=("wuwaterm",)), b'{"kind":"install-probe"}\n'])
def test_probe_report_must_be_the_canonical_telegram_record(raw):
    with pytest.raises(bd.BuildError):
        bd.check_report(raw)


# ------------------------------------------------------------ image store layer records

def layer_record() -> bytes:
    """What the classic image store writes beside an image for a metadata-only layer."""
    return json.dumps({"id": "sha256:" + "1" * 64, "parent": "", "created": "2026-11-08T00:00:00Z"}).encode()


def test_store_layer_records_are_dropped_before_the_archive_is_checked(tmp_path):
    record = layer_record()
    path = tmp_path / "image.oci.tar"
    path.write_bytes(image(extra_blobs={"record": record}))
    assert bd.normalize_saved_archive(path) == ["dropped 1 image-store layer records"]
    with tarfile.open(path) as tar:
        assert "blobs/sha256/" + sha(record) not in [member.name for member in tar]
    archive = bd.read_oci(path)
    assert archive.facts["archive_sha256"] == sha(path.read_bytes())
    assert archive.report == stub_report()
    # The daemon may export no records at all, and normalising never repeats itself.
    assert bd.normalize_saved_archive(path) == []


@pytest.mark.parametrize("blob", [b"payload", b'{"id": 3}', json.dumps({"id": "x" * 70000}).encode()])
def test_an_unreferenced_blob_that_is_not_a_layer_record_is_still_refused(tmp_path, blob):
    path = tmp_path / "image.oci.tar"
    path.write_bytes(image(extra_blobs={"stray": blob}))
    assert bd.normalize_saved_archive(path) == []
    with pytest.raises(bd.BuildError, match="unreferenced OCI archive content"):
        bd.read_oci(path)


def test_an_archive_without_records_is_left_byte_identical(tmp_path):
    path = tmp_path / "image.oci.tar"
    path.write_bytes(image())
    before = path.read_bytes()
    assert bd.normalize_saved_archive(path) == []
    assert path.read_bytes() == before


def test_unsupported_compat_manifest_keys_are_dropped(tmp_path):
    path = tmp_path / "image.oci.tar"
    path.write_bytes(image(compat_extra={"LayerSources": {"sha256:" + "1" * 64: {"mediaType": "x", "size": 1}}}))
    with pytest.raises(bd.BuildError, match="invalid docker archive manifest"):
        bd.read_oci(path)
    assert bd.normalize_saved_archive(path) == ["dropped unsupported keys from the compat manifest"]
    with tarfile.open(path) as tar:
        compat = json.loads(tar.extractfile("manifest.json").read())
    assert list(compat[0]) == ["Config", "RepoTags", "Layers"]
    assert bd.read_oci(path).report == stub_report()


@pytest.mark.parametrize("options", [{"compat_layers": ["blobs/sha256/" + "0" * 64]},
                                     {"compat_layers": [], "compat_extra": {"LayerSources": {}}}])
def test_a_compat_manifest_that_disagrees_with_the_image_is_refused(tmp_path, options):
    path = tmp_path / "image.oci.tar"
    path.write_bytes(image(**options))
    assert bd.normalize_saved_archive(path) == []
    with pytest.raises(bd.BuildError, match="docker archive manifest|invalid docker archive manifest"):
        bd.read_oci(path)


# ------------------------------------------------------------ the whole command, with a fake daemon

FAKE_DOCKER = """\
#!{python}
import json, os, shutil, sys
home = os.environ["FAKE_DOCKER_HOME"]
args = sys.argv[1:]
if args[:2] == ["buildx", "version"]:
    sys.exit(0 if os.environ.get("FAKE_BUILDKIT") == "1" else 1)
if args[:2] == ["buildx", "inspect"]:
    print("Driver: docker")
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    print("sha256:" + "b" * 64)
elif args[0] == "build":
    try:
        os.fstat(9)
        fd9 = True
    except OSError:
        fd9 = False
    with open(os.path.join(home, "log"), "a") as log:
        log.write(json.dumps({{"args": args, "fd9": fd9, "dockerfile": open(args[args.index("--file") + 1]).read()}}) + "\\n")
    open(args[args.index("--iidfile") + 1], "w").write(open(os.path.join(home, "iid")).read())
elif args[:2] == ["image", "save"]:
    shutil.copyfile(os.path.join(home, "archive.tar"), args[args.index("-o") + 1])
else:
    sys.exit(3)
"""


def dist_info(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}.dist-info"


def wheel(path: Path, name: str, version: str, files: dict[str, bytes] | None = None,
          scripts: dict[str, str] | None = None, data_files: dict[str, bytes] | None = None) -> Path:
    info = dist_info(name, version)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        for member, data in (files or {}).items():
            z.writestr(member, data)
        if scripts:
            z.writestr(f"{info}/entry_points.txt", "[console_scripts]\n" + "".join(
                f"{script} = {target}\n" for script, target in scripts.items()))
        data_dir = info.removesuffix(".dist-info") + ".data"
        for member, data in (data_files or {}).items():
            z.writestr(f"{data_dir}/data/{member}", data)
        z.writestr(f"{info}/RECORD", f"{info}/METADATA,,\n{info}/RECORD,,\n")
    return path


def wheel_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def file_entries(files: dict[str, bytes]) -> list[tuple[str, str, bytes]]:
    """Directory entries implied by an absolute file layout, then the files."""
    relative = {name.lstrip("/"): data for name, data in files.items()}
    directories = set()
    for name in relative:
        parts = name.split("/")
        directories.update("/".join(parts[:number]) for number in range(1, len(parts)))
    return [(name, "dir", b"") for name in sorted(directories)] + [
        (name, "file", data) for name, data in sorted(relative.items())]


class Layout:
    """The installed filesystem and the probe report a correct image has to agree on."""

    def __init__(self, replace: dict[str, bytes] | None = None,
                 extra: dict[str, dict[str, bytes]] | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.packages: list[dict] = []
        self.replace = replace or {}
        self.extra = extra or {}

    def add(self, name: str, version: str, path: Path, bin_scripts: dict[str, bytes] | None = None) -> None:
        members = wheel_members(path)
        info = next(top for top in {n.split("/", 1)[0] for n in members} if top.endswith(".dist-info"))
        data_dir = info.removesuffix(".dist-info") + ".data"
        installed: dict[str, bytes] = {}
        for member, data in members.items():
            if member == f"{info}/RECORD":
                continue
            top, _, rest = member.partition("/")
            if top == data_dir and rest.startswith("data/"):
                # An installer moves ``*.data/data`` content to the installation prefix.
                installed[posixpath.relpath(posixpath.join(DATA_ROOT, rest[len("data/"):]), SITE)] = data
            elif top == data_dir and rest.startswith("scripts/"):
                installed[posixpath.relpath(posixpath.join(BIN, rest[len("scripts/"):]), SITE)] = data
            else:
                installed[member] = data
        installed.update({n: d for n, d in self.replace.items() if n in installed})
        installed.update(self.extra.get(name, {}))
        installed[f"{info}/direct_url.json"] = bd.canonical({
            "archive_info": {"hashes": {"sha256": sha(path.read_bytes())}}, "url": "file:///wheels/" + path.name})
        for script, data in (bin_scripts or {}).items():
            installed[posixpath.relpath(posixpath.join(BIN, script), SITE)] = data
        rows = [f"{n},sha256={sha(d)},{len(d)}" for n, d in sorted(installed.items())] + [f"{info}/RECORD,,"]
        installed[f"{info}/RECORD"] = ("\n".join(rows) + "\n").encode()
        for member, data in installed.items():
            # RECORD keeps the stored path; the image holds the bytes where the installer put them.
            self.files[posixpath.normpath(posixpath.join(SITE, member))] = data
        self.packages.append({
            "name": name, "version": version, "location": SITE,
            "direct_url": json.loads(installed[f"{info}/direct_url.json"]), "record_entries": len(rows),
            "files": [{"path": n, "sha256": sha(installed[n]), "size": len(installed[n])} for n in sorted(installed)],
        })
        self.packages.sort(key=lambda package: package["name"])

    def report(self) -> bytes:
        return bd.canonical({"schema_version": 1, "kind": "install-probe", "product": "telegram",
                             "interpreter": INTERPRETER, "python_version": "3.11.0",
                             "site_packages": SITE, "packages": self.packages, "extra_files": []})

    def layers(self, tamper: str | None = None) -> list[list[tuple[str, str, bytes]]]:
        installed = dict(self.files)
        installed[PROGRAM_PATH] = (ROOT / "scripts/install_probe.py").read_bytes()
        if tamper == "missing-program":
            installed.pop(PROGRAM_PATH)
        elif tamper == "changed-file":
            first = sorted(n for n in installed if n.startswith(SITE + "/wuwaterm_telegram/") and n.endswith(".py"))
            installed[first[0]] = b"# changed after the probe ran\n"
        elif tamper == "unrecorded-file":
            installed[SITE + "/wuwaterm_telegram/unrecorded.py"] = b"# never recorded\n"
        return [file_entries(installed), report_layer(report=self.report())]


class Delivery:
    """A throwaway repository holding this tree's build inputs, a fake daemon and synthetic wheels."""

    def __init__(self, tmp_path: Path, *, archive: bytes | None = None, tamper: str | None = None,
                 replace: dict[str, bytes] | None = None, extra: dict[str, dict[str, bytes]] | None = None,
                 iid: str | None = None) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        for name in ("pyproject.toml", "deploy/Dockerfile", ".github/workflows/release.yml"):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.repo / name)
        shutil.copytree(ROOT / "scripts", self.repo / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "src", self.repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
        import core_lock
        self.core_version = core_lock.product_core_version()
        self.core = wheel(tmp_path / f"wuwaterm-{self.core_version}-py3-none-any.whl", "wuwaterm", self.core_version,
                          {"wuwaterm/__init__.py": b'"""Core fixture."""\n',
                           "wuwaterm/cli.py": b"def main():\n    return 0\n",
                           "wuwaterm_api/__init__.py": b'"""Core API fixture."""\n',
                           "wuwaterm_api/cli.py": b"def main():\n    return 0\n"},
                          scripts={"wuwaterm": "wuwaterm.cli:main", "wuwaterm-api": "wuwaterm_api.cli:main"})
        self.core_sha = sha(self.core.read_bytes())
        lock = json.loads((ROOT / "core-lock.json").read_text())
        lock.update(sha256=self.core_sha, expires_at="2999-01-01T00:00:00Z")
        (self.repo / "core-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        self.lock = lock
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)
        self.commit = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        version = tomllib.loads((self.repo / "pyproject.toml").read_text())["project"]["version"]
        package = {f"wuwaterm_telegram/{p.relative_to(self.repo / 'src/wuwaterm_telegram').as_posix()}": p.read_bytes()
                   for p in (self.repo / "src/wuwaterm_telegram").rglob("*") if p.is_file()}
        self.telegram = wheel(tmp_path / f"wuwaterm_telegram-{version}-py3-none-any.whl", "wuwaterm-telegram", version,
                              package, scripts={"wuwaterm-telegram": "wuwaterm_telegram.cli:main"},
                              data_files={"share/doc/wuwaterm-telegram/PROVENANCE.md": b"# fixture provenance\n"})
        self.layout = Layout(replace, extra)
        self.layout.add("wuwaterm", self.core_version, self.core, bin_scripts={
            "wuwaterm": f"#!{INTERPRETER}\nfrom wuwaterm.cli import main\n".encode(),
            "wuwaterm-api": f"#!{INTERPRETER}\nfrom wuwaterm_api.cli import main\n".encode()})
        self.layout.add("wuwaterm-telegram", version, self.telegram, bin_scripts={
            "wuwaterm-telegram": f"#!{INTERPRETER}\nfrom wuwaterm_telegram.cli import main\n".encode()})
        self.home = tmp_path / "daemon"
        self.home.mkdir()
        raw = archive if archive is not None else oci_archive(self.layout.layers(tamper))
        (self.home / "archive.tar").write_bytes(raw)
        (self.home / "iid").write_text(iid or read(tmp_path, raw).facts["config_digest"])
        fake = tmp_path / "bin" / "docker"
        fake.parent.mkdir()
        fake.write_text(FAKE_DOCKER.format(python=sys.executable))
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{fake.parent}:{os.environ['PATH']}", FAKE_DOCKER_HOME=str(self.home))
        self.out = tmp_path / "delivery"

    def run(self, *extra: str, local: bool = True, shell_prefix: str = "9>&-", out: str | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        core = (["--local-core-wheel", str(self.core), "--local-core-sha256", self.core_sha,
                 "--local-core-source-commit", CORE_COMMIT] if local else ["--core-wheel", str(self.core)])
        command = [sys.executable, str(SCRIPT), "--source", str(self.repo), "--out", out or str(self.out),
                   "--telegram-wheel", str(self.telegram), *core, *extra]
        return subprocess.run(["sh", "-c", f'exec "$@" {shell_prefix}', "sh", *command], env=self.env,
                              capture_output=True, text=True, check=False, cwd=cwd)

    def attempt(self) -> Path:
        (only,) = (self.out / "attempts").iterdir()
        return only

    def builds(self) -> list[dict]:
        log = self.home / "log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture()
def delivery(tmp_path) -> Delivery:
    return Delivery(tmp_path)


def test_local_core_build_seals_a_bundle_in_the_core_layout(delivery):
    result = delivery.run()
    assert result.returncode == 0, result.stderr
    attempt = delivery.attempt()
    assert sorted(p.name for p in (attempt / "bundle").iterdir()) == ["bundle.json", "image.oci.tar", "install-probe.json"]
    bundle = json.loads((attempt / "bundle" / "bundle.json").read_text())
    assert (attempt / "bundle" / "bundle.json").read_bytes() == bd.canonical(bundle)
    assert bundle["kind"] == "completed-component-bundle" and bundle["product"] == "wuwaterm-telegram"
    assert bundle["repository"] == "My-Denia/WuwaTerm-Telegram" and bundle["tag"] is None
    assert bundle["source"]["commit"] == delivery.commit and bundle["recipe"]["target"] is None
    assert bundle["recipe"]["build_args"] == [{"name": "WUWATERM_SOURCE_REVISION", "value": delivery.commit}]
    assert bundle["core_binding"] == {"core_version": delivery.core_version, "core_wheel_sha256": delivery.core_sha,
                                      "core_source_commit": CORE_COMMIT, "compatibility": "exact"}
    assert [(w["role"], w["package"]) for w in bundle["wheels"]] == [("core", "wuwaterm"), ("telegram", "wuwaterm-telegram")]
    assert bundle["build_result"] == {"authority": "local-source", "ci": None, "foreground_exit": 0,
                                      "iid": (delivery.home / "iid").read_text()}
    assert [p["path"] for p in bundle["payload"]] == ["image.oci.tar", "install-probe.json"]
    assert (attempt / "state" / "sealed.json").is_file() and json.loads(result.stdout)["state"] == "sealed"
    assert (attempt / "bundle" / "install-probe.json").read_bytes() == delivery.layout.report()
    assert bundle["install_probe"]["program_sha256"] == sha((delivery.repo / "scripts/install_probe.py").read_bytes())
    # Verified from the sealed files alone, and bound to the archived source bytes.
    assert bd.check_sealed(attempt)["version"] == bundle["version"]
    source = tarfile.open(attempt / "inputs" / "source.tar")
    assert source.pax_headers["comment"] == delivery.commit


def test_the_build_seals_away_what_the_daemon_exported_beside_the_image(delivery):
    archive = delivery.home / "archive.tar"
    # What Docker 28 on the runner writes: an unreferenced layer record blob and
    # a compat manifest key Core's verifier does not accept.
    rewritten = io.BytesIO()
    with tarfile.open(fileobj=rewritten, mode="w") as out, tarfile.open(archive) as tar:
        for member in tar:
            if member.name == "manifest.json":
                entry = json.load(tar.extractfile(member))
                entry[0]["LayerSources"] = {"sha256:" + "1" * 64: {"mediaType": "x", "size": 1}}
                raw = json.dumps(entry).encode()
                member.size = len(raw)
                out.addfile(member, io.BytesIO(raw))
            else:
                out.addfile(member, tar.extractfile(member) if member.isfile() else None)
    record = layer_record()
    staged = delivery.home / "staged.tar"
    staged.write_bytes(rewritten.getvalue())
    with tarfile.open(staged, mode="a") as tar:
        info = tarfile.TarInfo("blobs/sha256/" + sha(record))
        info.size = len(record)
        tar.addfile(info, io.BytesIO(record))
    archive.write_bytes(staged.read_bytes())
    staged.unlink()
    result = delivery.run()
    assert result.returncode == 0, result.stderr
    assert "dropped 1 image-store layer records" in result.stderr
    assert "dropped unsupported keys from the compat manifest" in result.stderr
    sealed = delivery.attempt() / "bundle" / "image.oci.tar"
    with tarfile.open(sealed) as tar:
        assert "blobs/sha256/" + sha(record) not in [member.name for member in tar]
        compat = json.loads(tar.extractfile("manifest.json").read())
    assert list(compat[0]) == ["Config", "RepoTags", "Layers"]
    assert bd.check_sealed(delivery.attempt())["version"]


def test_the_self_check_rebuilds_the_installed_filesystem(delivery):
    delivery.run()
    rootfs = bd.read_oci(delivery.attempt() / "bundle" / "image.oci.tar").rootfs
    assert rootfs.nodes[PROGRAM_PATH.lstrip("/")].sha256 == sha((delivery.repo / "scripts/install_probe.py").read_bytes())
    installed = sorted(n for n in rootfs.nodes if n.startswith(SITE.lstrip("/") + "/wuwaterm_telegram/"))
    assert len(installed) > 1 and all(n.endswith((".py", "/METADATA", "/WHEEL")) or "dist-info" in n for n in installed)


@pytest.mark.parametrize("options, reason", [
    ({"tamper": "changed-file"}, "installed file differs from the probe report"),
    ({"tamper": "missing-program"}, "not a regular file: " + PROGRAM_PATH),
    ({"tamper": "unrecorded-file"}, "unrecorded file in the installed package"),
    ({"replace": {"wuwaterm_telegram/__init__.py": b"# not the wheel\n"}}, "installed file differs from the verified wheel"),
    ({"extra": {"wuwaterm-telegram": {"wuwaterm_telegram/injected.py": b"# in RECORD and the report, in no wheel\n"}}},
     "installed file is not in the verified wheel: wuwaterm-telegram wuwaterm_telegram/injected.py"),
    ({"extra": {"wuwaterm-telegram": {"../site-packages/evil.pth": b"# import-time hook beside the package\n"}}},
     "installed file is not in the verified wheel: wuwaterm-telegram ../site-packages/evil.pth"),
    ({"extra": {"wuwaterm-telegram": {"../sitecustomize.py": b"# import-time hook outside the package tree\n"}}},
     "installed file is not in the verified wheel: wuwaterm-telegram ../sitecustomize.py"),
    ({"extra": {"wuwaterm-telegram": {"zz.data/../evil.pth": b"# unnormalized path that resolves inside site-packages\n"}}},
     "installed file path is not normalized: wuwaterm-telegram zz.data/../evil.pth"),
    ({"extra": {"wuwaterm-telegram": {"x.data/evil.py": b"# wheel data member that was never relocated\n"}}},
     "installed file is not in the verified wheel: wuwaterm-telegram x.data/evil.py"),
])
def test_the_self_check_refuses_a_tampered_installed_state(tmp_path, options, reason):
    delivery = Delivery(tmp_path, **options)
    result = delivery.run()
    assert result.returncode == 2, result.stderr
    assert reason in result.stderr
    assert (delivery.attempt() / "bundle" / "install-probe.json").is_file()
    assert not (delivery.attempt() / "state" / "sealed.json").exists()


def test_declared_console_scripts_and_relocated_data_are_installed_content(delivery):
    result = delivery.run()
    assert result.returncode == 0, result.stderr
    report = json.loads((delivery.attempt() / "bundle" / "install-probe.json").read_text())
    paths = {item["path"] for package in report["packages"] for item in package["files"]}
    assert {"../../../bin/wuwaterm", "../../../bin/wuwaterm-api", "../../../bin/wuwaterm-telegram",
            "../../../share/doc/wuwaterm-telegram/PROVENANCE.md"} <= paths


def test_a_relative_output_directory_is_resolved_before_archiving_another_source(delivery):
    result = delivery.run(out="relative-delivery", cwd=delivery.tmp)
    assert result.returncode == 0, result.stderr
    (attempt,) = (delivery.tmp / "relative-delivery" / "attempts").iterdir()
    assert (attempt / "inputs" / "source.tar").is_file() and (attempt / "bundle" / "bundle.json").is_file()


def test_the_build_is_a_plain_untagged_build_without_inherited_descriptors(delivery):
    delivery.run()
    (build,) = delivery.builds()
    args = build["args"]
    assert "-t" not in args and "--tag" not in args and "--target" not in args
    assert {"--pull=false", "--force-rm", "--iidfile"} <= set(args)
    assert "--provenance=false" not in args and "--sbom=false" not in args
    assert args[args.index("--platform") + 1] == "linux/amd64"
    assert f"CORE_WHEEL_SHA256={delivery.core_sha}" in args and "--network" not in args
    assert build["fd9"] is False
    assert build["dockerfile"] == (delivery.repo / "deploy/Dockerfile").read_text()


def test_buildkit_attestations_are_disabled(delivery):
    # The docker driver is BuildKit; its default provenance/SBOM manifests
    # turn the saved archive into a multi-manifest index that read_oci refuses.
    delivery.env["FAKE_BUILDKIT"] = "1"
    result = delivery.run()
    assert result.returncode == 0, result.stderr
    (build,) = delivery.builds()
    assert "--provenance=false" in build["args"]
    assert "--sbom=false" in build["args"]


def test_official_core_uses_the_committed_lock_and_passes_no_local_argument(delivery):
    result = delivery.run(local=False)
    assert result.returncode == 0, result.stderr
    (build,) = delivery.builds()
    assert not any(a.startswith("CORE_WHEEL_SHA256") for a in build["args"])
    bundle = json.loads((delivery.attempt() / "bundle" / "bundle.json").read_text())
    assert bundle["core_binding"]["core_source_commit"] == delivery.lock["source_commit"]
    assert bundle["recipe"]["lock_sha256"] == sha((delivery.repo / "core-lock.json").read_bytes())


def test_a_wheelhouse_builds_offline_and_records_dependency_wheels(delivery):
    house = delivery.tmp / "house"
    house.mkdir()
    dep = wheel(house / "idna-3.20-py3-none-any.whl", "idna", "3.20")
    shutil.copyfile(delivery.core, house / delivery.core.name)
    assert delivery.run("--wheelhouse", str(house)).returncode == 0
    (build,) = delivery.builds()
    assert build["args"][build["args"].index("--network") + 1] == "none"
    bundle = json.loads((delivery.attempt() / "bundle" / "bundle.json").read_text())
    assert [a["name"] for a in bundle["recipe"]["build_args"]] == ["PIP_FIND_LINKS", "PIP_NO_INDEX", "WUWATERM_SOURCE_REVISION"]
    assert [w["role"] for w in bundle["wheels"]] == ["core", "telegram", "dependency"]
    assert bundle["wheels"][2]["sha256"] == sha(dep.read_bytes())
    assert (delivery.attempt() / "inputs/wheels" / dep.name).is_file()


def test_inherited_descriptor_nine_is_refused_before_any_work(delivery):
    result = delivery.run(shell_prefix="9</dev/null")
    assert result.returncode == 2 and "file descriptor 9 is open" in result.stderr
    assert not delivery.out.exists() and delivery.builds() == []


def test_a_wheel_that_is_not_the_selected_commit_never_reaches_docker(delivery):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheel(delivery.telegram, "wuwaterm-telegram", version, {"wuwaterm_telegram/__init__.py": b"# other\n"})
    result = delivery.run()
    assert result.returncode == 2 and "differs from the selected source commit" in result.stderr
    assert delivery.builds() == [] and not (delivery.attempt() / "state" / "sealed.json").exists()


def test_a_wrong_local_core_digest_never_reaches_docker(delivery):
    delivery.core_sha = "0" * 64
    result = delivery.run()
    assert result.returncode == 2 and delivery.builds() == []


def test_official_ci_authority_needs_a_github_run(delivery):
    delivery.env.pop("GITHUB_ACTIONS", None)
    result = delivery.run("--authority", "official-ci")
    assert result.returncode == 2 and "only available inside GitHub Actions" in result.stderr
    assert delivery.builds() == []


def test_official_ci_records_run_evidence_for_the_built_commit(delivery):
    delivery.env.update(GITHUB_ACTIONS="true", GITHUB_RUN_ID="7", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="image-bundle",
                        GITHUB_SHA=delivery.commit)
    assert delivery.run("--authority", "official-ci").returncode == 0
    ci = json.loads((delivery.attempt() / "bundle" / "bundle.json").read_text())["build_result"]["ci"]
    assert ci == {"run_id": 7, "run_attempt": 2, "job": "image-bundle", "control_commit": delivery.commit,
                  "workflow_sha256": sha((delivery.repo / ".github/workflows/release.yml").read_bytes())}


@pytest.mark.parametrize("target", ["image.oci.tar", "install-probe.json", "bundle.json"])
def test_self_check_rejects_a_changed_sealed_file(delivery, target):
    delivery.run()
    attempt = delivery.attempt()
    path = attempt / "bundle" / target
    path.chmod(0o644)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 1
    path.write_bytes(bytes(data))
    with pytest.raises(bd.BuildError):
        bd.check_sealed(attempt)


def test_self_check_rejects_a_changed_input_wheel(delivery):
    delivery.run()
    attempt = delivery.attempt()
    (attempt / "inputs/wheels" / delivery.core.name).write_bytes(b"changed")
    with pytest.raises(bd.BuildError, match="wheel bytes differ"):
        bd.check_sealed(attempt)


def test_an_archive_with_a_name_is_not_sealed(tmp_path):
    delivery = Delivery(tmp_path, archive=image(annotations={"io.containerd.image.name": "x:1"}), iid="sha256:" + "d" * 64)
    result = delivery.run()
    assert result.returncode == 2 and "image name annotation" in result.stderr
    assert not (delivery.attempt() / "bundle" / "bundle.json").exists()


def test_core_tool_rejection_leaves_the_attempt_unsealed(delivery):
    tool = delivery.tmp / "tool.py"
    tool.write_text(textwrap.dedent("import sys; sys.exit(2)\n"))
    result = delivery.run("--core-tool", str(tool))
    assert result.returncode == 2
    assert (delivery.attempt() / "bundle" / "bundle.json").is_file()
    assert not (delivery.attempt() / "state" / "sealed.json").exists()
