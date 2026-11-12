#!/usr/bin/env python3
"""Build and seal a Telegram ``completed-component-bundle`` (schema 1).

One selected Git commit becomes a source archive, a verified Docker context and
a plain ``docker build`` (no tag, ``--iidfile``, the daemon's default builder),
then ``docker image save`` of the full image ID, then a sealed ``bundle.json``.
Nothing is registered or deployed here.

The delivery layout and every field follow the Core consumer
(``scripts/component_artifact.py``), which alone verifies a bundle for
acquisition; this producer deliberately imports none of Core. ``--core-tool``
runs that verifier as a separate process before the attempt is marked sealed.

Sealing and the self-check are pure file parsing. The image filesystem is
rebuilt from the layer tars, whiteouts included, so the build-time probe report
is never trusted: every installed file it names is hashed again and compared
with the sealed source archive and the verified wheels.

The tool never holds or inherits the deployment lock FD 9, never starts a
container itself, never calls BuildKit, and starts every child process with
``close_fds=True`` and no inherited descriptors.
"""
from __future__ import annotations

import argparse
import configparser
import csv
import dataclasses
from email.parser import BytesParser
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from typing import Any, Callable
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = "wuwaterm-telegram"
REPOSITORY = "My-Denia/WuwaTerm-Telegram"
BUNDLE_KIND = "completed-component-bundle"
STATE_KIND = "completed-artifact-delivery-state"
REPORT_PATH = "/usr/local/share/wuwaterm/install-probe.json"
BUNDLE_FILE, ARCHIVE_FILE, REPORT_FILE = "bundle.json", "image.oci.tar", "install-probe.json"
# Core's non-secret build-arg allowlist; CORE_WHEEL_SHA256 is passed to the build
# but not recorded because that list rejects it (its value is core_binding's).
RECORDED_ARGS = ("WUWATERM_SOURCE_REVISION", "PIP_NO_INDEX", "PIP_FIND_LINKS")
WHEELHOUSE_DIR = "wheels/deps"
INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST_MEDIA = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_MEDIA_TYPES = {MANIFEST_MEDIA, DOCKER_MANIFEST_MEDIA}
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
DOCKER_CONFIG_MEDIA = "application/vnd.docker.container.image.v1+json"
CONFIG_MEDIA_TYPES = {CONFIG_MEDIA, DOCKER_CONFIG_MEDIA}
LAYER_GZIP = {"application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"}
LAYER_TAR = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.docker.image.rootfs.diff.tar"}
NAME_ANNOTATIONS = ("org.opencontainers.image.ref.name", "io.containerd.image.name")
INDEX_ANNOTATIONS = {"org.opencontainers.image.created", "config.digest"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
GIT = re.compile(r"[0-9a-f]{40}\Z")
CHUNK = 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
# A layer record the classic image store exports beside an image is a few
# hundred bytes of JSON; anything larger is not a record and is never dropped.
RECORD_MAX = 64 * 1024
MAX_CAPTURE = 16 * 1024 * 1024
PROGRAM_PATH = "/app/scripts/install_probe.py"
FINAL_LAYER_DIRS = {"usr", "usr/local", "usr/local/share", "usr/local/share/wuwaterm"}
# Regular files sealing and the self-check have to read out of the source archive.
SOURCE_FILES = ("pyproject.toml", "deploy/Dockerfile", "scripts/install_probe.py", "core-lock.json")
# Files an installer writes itself; they are not wheel content.
INSTALLER_FILES = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json", "uv_build.json"}
# Extra image files the product contract declares, mapped to their source path.
# A Telegram image installs none; the Core image's /entrypoint.sh is a Core field.
EXTRA_FILES: dict[str, str] = {}
REPORT_KEYS = {"schema_version", "kind", "product", "interpreter", "python_version", "site_packages",
               "packages", "extra_files"}
PACKAGE_KEYS = {"name", "version", "location", "direct_url", "record_entries", "files"}


class BuildError(RuntimeError):
    """The delivery cannot be built or sealed."""


def fail(message: str) -> None:
    raise BuildError(message)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise BuildError(f"cannot read {path.name}") from exc
    return digest.hexdigest(), size


def refuse_inherited_fd9() -> None:
    try:
        os.fstat(9)
    except OSError:
        return
    fail("file descriptor 9 is open; the delivery builder must not inherit the deployment lock (run with 9>&-)")


def new_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(16)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Exclusive, durable create: temp file, fsync, link, fsync parent."""
    temporary = path.parent / (".tmp-" + path.name + "-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise BuildError("record already exists: " + path.name) from None
    finally:
        os.unlink(temporary)
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(command: list[str], *, capture: bool = False, cwd: Path | None = None) -> str:
    """Run a child with no inherited descriptors; its stdout goes to our stderr unless captured."""
    try:
        result = subprocess.run(command, check=False, close_fds=True, pass_fds=(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else 2, cwd=cwd)
    except OSError as exc:
        raise BuildError(f"cannot run {command[0]}") from exc
    if result.returncode != 0:
        fail(f"{' '.join(command[:3])} exited with status {result.returncode}")
    return result.stdout.decode("utf-8", "replace").strip() if capture else ""


# --------------------------------------------------------------------------
# sealed OCI archive: facts and the probe report, from file parsing only
# --------------------------------------------------------------------------

def member_name(raw: str) -> str:
    name = (raw[2:] if raw.startswith("./") else raw).rstrip("/")
    if not name or name.startswith("/") or "\\" in name or "\x00" in name \
            or any(part in ("", ".", "..") for part in name.split("/")):
        fail("unsafe archive member name")
    return name


class HashingReader:
    def __init__(self, stream: Any) -> None:
        self.stream, self.digest, self.size = stream, hashlib.sha256(), 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.digest.update(data)
        self.size += len(data)
        return data

    def drain(self) -> None:
        while self.read(CHUNK):
            pass


@dataclasses.dataclass(frozen=True)
class Node:
    kind: str            # "file" | "dir" | "symlink" | "other"
    mode: int
    size: int
    sha256: str | None
    link: str | None
    layer: int


def captures(name: str) -> bool:
    """Only the files whose bytes verification reads are kept in memory."""
    return (name.endswith((".dist-info/RECORD", ".dist-info/METADATA", ".dist-info/direct_url.json"))
            or name in (REPORT_PATH.lstrip("/"), PROGRAM_PATH.lstrip("/")))


class RootFS:
    """The image filesystem rebuilt from its layer tars, whiteouts honoured."""

    def __init__(self, capture: Callable[[str], bool] = captures) -> None:
        self.nodes: dict[str, Node] = {}
        self.content: dict[str, bytes] = {}
        self.layer_entries: list[list[tuple[str, str]]] = []
        self._capture = capture

    def _drop(self, path: str, *, keep_self: bool = False) -> None:
        prefix = path + "/"
        for key in [k for k in self.nodes if k.startswith(prefix)]:
            del self.nodes[key]
            self.content.pop(key, None)
        if not keep_self:
            self.nodes.pop(path, None)
            self.content.pop(path, None)

    def apply_layer(self, index: int, stream: Any) -> None:
        additions: list[tuple[str, Node, bytes | None, str | None]] = []
        whiteouts: list[str] = []
        opaque: list[str] = []
        listing: list[tuple[str, str]] = []
        seen: set[str] = set()
        try:
            with tarfile.open(fileobj=stream, mode="r|") as layer:
                for member in layer:
                    if member.name in (".", "./"):
                        # Root directory metadata only; it cannot hide or change a probed path.
                        if not member.isdir():
                            fail("layer root entry is not a directory")
                        continue
                    name = member_name(member.name)
                    if name in seen:
                        fail("duplicate layer entry: " + name)
                    seen.add(name)
                    base, parent = posixpath.basename(name), posixpath.dirname(name)
                    if base == ".wh..wh..opq":
                        opaque.append(parent)
                        listing.append((name, "opaque"))
                        continue
                    if base.startswith(".wh..wh."):
                        fail("unsupported whiteout entry")
                    if base.startswith(".wh."):
                        whiteouts.append(posixpath.join(parent, base[4:]) if parent else base[4:])
                        listing.append((name, "whiteout"))
                        continue
                    mode = member.mode & 0o7777
                    if member.isreg():
                        data = layer.extractfile(member)
                        if data is None:
                            fail("layer file unavailable")
                        digest, size, kept = hashlib.sha256(), 0, []
                        keep = self._capture(name) and member.size <= MAX_CAPTURE
                        while chunk := data.read(CHUNK):
                            digest.update(chunk)
                            size += len(chunk)
                            if keep:
                                kept.append(chunk)
                        if size != member.size:
                            fail("layer file truncated: " + name)
                        additions.append((name, Node("file", mode, size, digest.hexdigest(), None, index),
                                          b"".join(kept) if keep else None, None))
                        listing.append((name, "file"))
                    elif member.isdir():
                        additions.append((name, Node("dir", mode, 0, None, None, index), None, None))
                        listing.append((name, "dir"))
                    elif member.issym():
                        additions.append((name, Node("symlink", mode, 0, None, member.linkname, index), None, None))
                        listing.append((name, "symlink"))
                    elif member.islnk():
                        additions.append((name, Node("file", mode, 0, None, None, index), None,
                                          member_name(member.linkname)))
                        listing.append((name, "hardlink"))
                    else:
                        additions.append((name, Node("other", mode, 0, None, None, index), None, None))
                        listing.append((name, "other"))
        except tarfile.TarError as exc:
            raise BuildError("invalid layer tar") from exc
        # Whiteouts remove lower-layer content; this layer's own additions win.
        for path in opaque:
            self._drop(path, keep_self=True)
        for path in whiteouts:
            self._drop(path)
        for name, node, data, hardlink in additions:
            parent = posixpath.dirname(name)
            while parent:
                existing = self.nodes.get(parent)
                if existing is not None and existing.kind != "dir":
                    fail("layer entry is placed below a non-directory")
                parent = posixpath.dirname(parent)
            previous = self.nodes.get(name)
            if previous is not None and (previous.kind == "dir") != (node.kind == "dir"):
                self._drop(name)
            if hardlink is not None:
                target = self.nodes.get(hardlink)
                if target is None or target.kind != "file":
                    fail("hard link target is not a regular file")
                node = dataclasses.replace(target, mode=node.mode, layer=index)
                data = self.content.get(hardlink)
            self.nodes[name] = node
            if data is not None:
                self.content[name] = data
            else:
                self.content.pop(name, None)
        self.layer_entries.append(listing)

    def resolve(self, path: str) -> str:
        """Resolve directory symlinks in the parents; the last component is not followed."""
        parts = [p for p in path.strip("/").split("/") if p]
        resolved: list[str] = []
        hops = 0
        while parts:
            part = parts.pop(0)
            if part == ".":
                continue
            if part == "..":
                if resolved:
                    resolved.pop()
                continue
            candidate = "/".join(resolved + [part])
            node = self.nodes.get(candidate)
            if parts and node is not None and node.kind == "symlink":
                hops += 1
                if hops > 40:
                    fail("symlink loop in image filesystem")
                link = node.link or ""
                if link.startswith("/"):
                    resolved = []
                parts = [p for p in link.split("/") if p] + parts
                continue
            resolved.append(part)
        return "/".join(resolved)

    def regular(self, path: str) -> tuple[str, Node]:
        name = self.resolve(path)
        node = self.nodes.get(name)
        if node is None or node.kind != "file":
            fail("image path is not a regular file: " + path)
        return name, node

    def read(self, path: str) -> bytes:
        name, node = self.regular(path)
        data = self.content.get(name)
        if data is None or sha(data) != node.sha256:
            fail("image file content was not captured: " + path)
        return data


def parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BuildError("invalid JSON") from exc


COMPAT_KEYS = ("Config", "RepoTags", "Layers")


def normalize_saved_archive(path: Path) -> list[str]:
    """Make a saved archive exactly what the image says it is.

    Two pieces of image-store bookkeeping are dropped: the JSON record the
    classic store keeps for each metadata-only layer, which ``docker image save``
    writes as an unreferenced blob, and any compat ``manifest.json`` key beside
    Config, RepoTags and Layers, which Core's verifier refuses. Neither is image
    content, so the sealed archive keeps the reachable closure and stays readable
    by the consumer. Anything that diverges — an unreferenced member that is not
    a layer record, a compat file that disagrees with the OCI manifest — is left
    in place for :func:`read_oci` to refuse. A malformed archive is untouched, so
    the reader reports the reason itself.
    """
    try:
        with tarfile.open(path, mode="r:") as tar:
            infos = [(member_name(member.name), member) for member in tar]
            by_name = dict(infos)
            manifests = parse_json(extract_member(tar, by_name.get("index.json")))["manifests"]
            if type(manifests) is not list or len(manifests) != 1:
                return []
            referenced = {"oci-layout", "index.json", "blobs", "blobs/sha256", "manifest.json"}
            manifest_name = "blobs/sha256/" + blob_digest(manifests[0]["digest"])
            referenced.add(manifest_name)
            manifest = parse_json(extract_member(tar, by_name.get(manifest_name)))
            config_name = "blobs/sha256/" + blob_digest(manifest["config"]["digest"])
            layer_names = ["blobs/sha256/" + blob_digest(layer["digest"]) for layer in manifest["layers"]]
            referenced.add(config_name)
            referenced.update(layer_names)
            dropped = [name for name, member in infos
                       if member.isfile() and name.startswith("blobs/sha256/") and name not in referenced]
            for name in dropped:
                record = parse_json(extract_member(tar, by_name.get(name), cap=RECORD_MAX))
                if type(record) is not dict or type(record.get("id")) is not str:
                    return []
            compat_raw = None
            compat = by_name.get("manifest.json")
            if compat is not None and compat.isfile():
                entry = parse_json(extract_member(tar, compat))
                if type(entry) is list and len(entry) == 1 and type(entry[0]) is dict \
                        and entry[0].get("Config") == config_name and entry[0].get("Layers") == layer_names:
                    extra = set(entry[0]) - set(COMPAT_KEYS)
                    if extra:
                        compat_raw = json.dumps([{key: entry[0][key] for key in COMPAT_KEYS if key in entry[0]}]).encode()
        if not dropped and compat_raw is None:
            return []
        temporary = path.parent / ("." + path.name + ".normalized-" + secrets.token_hex(8))
        try:
            with tarfile.open(path, mode="r:") as tar, tarfile.open(temporary, mode="w") as out:
                for member in tar:
                    name = member_name(member.name)
                    if name in dropped:
                        continue
                    if name == "manifest.json" and compat_raw is not None:
                        member.size = len(compat_raw)
                        out.addfile(member, io.BytesIO(compat_raw))
                    else:
                        out.addfile(member, tar.extractfile(member) if member.isfile() else None)
            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        fsync_dir(path.parent)
        changes = []
        if dropped:
            changes.append(f"dropped {len(dropped)} image-store layer records")
        if compat_raw is not None:
            changes.append("dropped unsupported keys from the compat manifest")
        return changes
    except (OSError, EOFError, tarfile.TarError, KeyError, IndexError, TypeError, ValueError, AttributeError,
            BuildError):
        return []


def extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo | None, *, cap: int = MAX_JSON) -> bytes:
    """Read one regular member; the archive's own validator reports what is wrong."""
    handle = tar.extractfile(member) if member is not None and member.isfile() else None
    if handle is None or member.size > cap:
        raise BuildError("OCI entry missing or oversized")
    data = handle.read()
    if len(data) != member.size:
        raise BuildError("OCI entry truncated")
    return data


def blob_digest(digest: Any) -> str:
    if type(digest) is not str or not DIGEST.fullmatch(digest):
        raise BuildError("invalid OCI descriptor")
    return digest[7:]


def read_oci(path: Path) -> OciArchive:
    """Return the archive facts and the filesystem rebuilt from its layers.

    Fails closed on an image name, a repo tag, an unreferenced entry, a digest
    or size mismatch, and any final-layer change besides the report.
    """
    archive_sha, archive_size = hash_file(path)
    try:
        with tarfile.open(path, mode="r:") as tar:
            entries: dict[str, tarfile.TarInfo] = {}
            for member in tar:
                name = member_name(member.name)
                if name in entries:
                    fail("duplicate OCI archive entry")
                if member.isdir():
                    if name not in ("blobs", "blobs/sha256"):
                        fail("unexpected OCI archive directory")
                elif not member.isfile():
                    fail("unsafe OCI archive entry")
                elif name not in ("oci-layout", "index.json", "manifest.json") and not (
                        name.startswith("blobs/sha256/") and HEX64.fullmatch(name[13:])):
                    fail("unexpected OCI archive entry")
                entries[name] = member
            referenced = {"oci-layout", "index.json", "blobs", "blobs/sha256"}

            def read(name: str) -> bytes:
                member = entries.get(name)
                handle = tar.extractfile(member) if member is not None and member.isfile() else None
                if handle is None or member.size > MAX_JSON:
                    fail("OCI entry missing or oversized: " + name)
                data = handle.read()
                if len(data) != member.size:
                    fail("OCI entry truncated")
                return data

            def descriptor_blob(desc: Any, media: set[str], *, data: bool = True) -> tuple[str, int, bytes]:
                if type(desc) is not dict or set(desc) - {"mediaType", "digest", "size"} or desc.get("mediaType") not in media:
                    fail("invalid or annotated OCI descriptor")
                digest, size = desc.get("digest"), desc.get("size")
                if type(digest) is not str or not DIGEST.fullmatch(digest) or type(size) is not int or size < 0:
                    fail("invalid OCI descriptor")
                name = "blobs/sha256/" + digest[7:]
                if name not in entries or entries[name].size != size:
                    fail("OCI blob missing or size differs")
                referenced.add(name)
                raw = read(name) if data else b""
                if data and "sha256:" + sha(raw) != digest:
                    fail("OCI blob digest differs")
                return digest, size, raw

            if parse_json(read("oci-layout")) != {"imageLayoutVersion": "1.0.0"}:
                fail("unsupported OCI layout")
            index_raw = read("index.json")
            index = parse_json(index_raw)
            if type(index) is not dict or set(index) - {"schemaVersion", "mediaType", "manifests", "annotations"} \
                    or index.get("annotations") or index.get("schemaVersion") != 2 \
                    or index.get("mediaType") != INDEX_MEDIA or type(index.get("manifests")) is not list \
                    or len(index["manifests"]) != 1:
                fail("OCI index must reference exactly one unannotated image")
            descriptor = index["manifests"][0]
            if type(descriptor) is not dict or set(descriptor) - {"mediaType", "digest", "size", "annotations", "platform"}:
                fail("invalid OCI index descriptor")
            annotations = descriptor.get("annotations", {})
            if type(annotations) is not dict:
                fail("invalid OCI annotations")
            for key in NAME_ANNOTATIONS:
                if key in annotations:
                    fail("OCI archive carries an image name annotation (" + key + ")")
            if set(annotations) - INDEX_ANNOTATIONS:
                fail("unexpected OCI index annotation")
            config_annotation = annotations.get("config.digest")
            if config_annotation is not None and (type(config_annotation) is not str or not DIGEST.fullmatch(config_annotation)):
                fail("invalid OCI config digest annotation")
            manifest_digest, _, manifest_raw = descriptor_blob(
                {k: descriptor.get(k) for k in ("mediaType", "digest", "size")}, MANIFEST_MEDIA_TYPES)
            manifest = parse_json(manifest_raw)
            if type(manifest) is not dict or set(manifest) != {"schemaVersion", "mediaType", "config", "layers"} \
                    or manifest["schemaVersion"] != 2 or manifest["mediaType"] not in MANIFEST_MEDIA_TYPES:
                fail("invalid OCI manifest")
            config_digest, _, config_raw = descriptor_blob(manifest["config"], CONFIG_MEDIA_TYPES)
            if config_annotation is not None and config_annotation != config_digest:
                fail("OCI config digest annotation differs")
            config = parse_json(config_raw)
            if type(config) is not dict or type(config.get("rootfs")) is not dict \
                    or type(config["rootfs"].get("diff_ids")) is not list:
                fail("invalid OCI config")
            platform = {"os": config.get("os"), "architecture": config.get("architecture"),
                        "variant": config.get("variant")}
            if type(platform["os"]) is not str or type(platform["architecture"]) is not str:
                fail("invalid OCI config platform")
            layer_descs, diff_ids = manifest["layers"], config["rootfs"]["diff_ids"]
            if type(layer_descs) is not list or not layer_descs or len(layer_descs) != len(diff_ids):
                fail("OCI layer count differs from config diff IDs")
            if "manifest.json" in entries:
                referenced.add("manifest.json")
                compat = parse_json(read("manifest.json"))
                if type(compat) is not list or len(compat) != 1 or type(compat[0]) is not dict \
                        or set(compat[0]) - set(COMPAT_KEYS):
                    fail("invalid docker archive manifest")
                if compat[0].get("RepoTags") not in (None, []):
                    fail("OCI archive carries RepoTags")
                # The compat file is optional, but when present it has to describe
                # the same image Core's verifier will read.
                if compat[0].get("Config") != "blobs/sha256/" + config_digest[7:] \
                        or compat[0].get("Layers") != ["blobs/sha256/" + d["digest"][7:] for d in manifest["layers"]]:
                    fail("docker archive manifest differs from the OCI manifest")
            layers, rootfs = [], RootFS()
            for number, (desc, diff_id) in enumerate(zip(layer_descs, diff_ids, strict=True)):
                digest, size, _ = descriptor_blob(desc, LAYER_GZIP | LAYER_TAR, data=False)
                if type(diff_id) is not str or not DIGEST.fullmatch(diff_id):
                    fail("invalid layer diff ID")
                handle = tar.extractfile(entries["blobs/sha256/" + digest[7:]])
                if handle is None:
                    fail("OCI layer unavailable")
                compressed = HashingReader(handle)
                plain = HashingReader(gzip.GzipFile(fileobj=compressed, mode="rb")
                                      if desc["mediaType"] in LAYER_GZIP else compressed)
                try:
                    rootfs.apply_layer(number, plain)
                    plain.drain()
                    compressed.drain()
                except BuildError:
                    raise
                except (OSError, EOFError, tarfile.TarError) as exc:
                    raise BuildError("invalid layer") from exc
                if compressed.size != size or "sha256:" + compressed.digest.hexdigest() != digest:
                    fail("OCI layer digest differs")
                if "sha256:" + plain.digest.hexdigest() != diff_id:
                    fail("OCI layer diff ID differs")
                layers.append({"digest": digest, "size": size, "diff_id": diff_id, "media_type": desc["mediaType"]})
            check_final_layer(rootfs)
            if set(entries) != referenced:
                fail("unreferenced OCI archive content")
    except BuildError:
        raise
    except (OSError, EOFError, tarfile.TarError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise BuildError("invalid OCI archive") from exc
    facts = {"archive_sha256": archive_sha, "archive_size": archive_size, "index_digest": "sha256:" + sha(index_raw),
             "manifest_digest": manifest_digest, "config_digest": config_digest, "layers": layers, "platform": platform}
    return OciArchive(facts, rootfs)


@dataclasses.dataclass
class OciArchive:
    facts: dict[str, Any]
    rootfs: RootFS

    @property
    def report(self) -> bytes:
        return self.rootfs.read(REPORT_PATH)


def check_final_layer(rootfs: RootFS) -> None:
    """The probe RUN layer may add only the report and its parent directories."""
    report = REPORT_PATH.lstrip("/")
    final = rootfs.layer_entries[-1]
    if dict(final).get(report) != "file":
        fail("final layer does not add the probe report as a regular file")
    for name, kind in final:
        if name == report:
            continue
        if name in FINAL_LAYER_DIRS and kind == "dir":
            continue
        fail("final layer changes more than the probe report: " + name)


def check_report(raw: bytes) -> dict[str, Any]:
    """The canonical Telegram probe report, with every field verification reads."""
    report = parse_json(raw)
    if type(report) is not dict or canonical(report) != raw or set(report) != REPORT_KEYS \
            or report.get("kind") != "install-probe" or report.get("schema_version") != 1 \
            or report.get("product") != "telegram":
        fail("probe report is not a canonical Telegram install-probe record")
    if type(report["interpreter"]) is not str or type(report["site_packages"]) is not str:
        fail("probe report has an invalid interpreter or site-packages path")
    packages = report["packages"]
    if type(packages) is not list \
            or [p.get("name") if type(p) is dict else None for p in packages] != ["wuwaterm", "wuwaterm-telegram"]:
        fail("probe report must describe exactly wuwaterm and wuwaterm-telegram")
    for package in packages:
        if set(package) != PACKAGE_KEYS or type(package["version"]) is not str \
                or type(package["record_entries"]) is not int or package["record_entries"] < 1:
            fail("probe report has an invalid package record")
        files = package["files"]
        if type(files) is not list or not files:
            fail("probe report lists no files for " + package["name"])
        paths = [f.get("path") if type(f) is dict else None for f in files]
        if paths != sorted({p for p in paths if type(p) is str}):
            fail("probe report files must be unique and sorted")
        for item in files:
            if set(item) != {"path", "sha256", "size"} or type(item["sha256"]) is not str \
                    or not HEX64.fullmatch(item["sha256"]) or type(item["size"]) is not int or item["size"] < 0:
                fail("probe report has an invalid file record")
    if type(report["extra_files"]) is not list:
        fail("probe report has invalid extra files")
    for item in report["extra_files"]:
        if set(item) != {"path", "sha256", "mode"} or type(item["sha256"]) is not str \
                or not HEX64.fullmatch(item["sha256"]) or type(item["mode"]) is not int:
            fail("probe report has an invalid extra file record")
    return report


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def record_rows(raw: bytes) -> list[str]:
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    except (UnicodeError, csv.Error) as exc:
        raise BuildError("invalid RECORD") from exc
    paths = []
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            fail("invalid RECORD row")
        paths.append(row[0])
    return paths


def compiled_cache(path: str) -> bool:
    return path.endswith((".pyc", ".pyo")) or "__pycache__" in path.split("/")


def installer_path(path: str) -> bool:
    """A wheel member an installer does not install verbatim.

    ``*.data`` content is relocated to the installation prefix, and ``RECORD`` and
    its neighbours are written by the installer itself rather than shipped.
    """
    top, _, rest = path.partition("/")
    return ".data/" in path or (top.endswith(".dist-info") and rest in INSTALLER_FILES)


def relocated_data(members: dict[str, str], digest: str, target: str, data_root: str) -> bool:
    """Whether ``target`` is a wheel's ``*.data/data`` member, bound by the member's bytes."""
    for member, member_digest in members.items():
        top, _, rest = member.partition("/")
        if not top.endswith(".data") or not rest.startswith("data/") or member_digest != digest:
            continue
        relative = rest[len("data/"):]
        if relative and not relative.startswith("bin/") \
                and target == posixpath.normpath(posixpath.join(data_root, relative)):
            return True
    return False


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as wheel:
            meta = [n for n in wheel.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            if len(meta) != 1:
                fail(f"{path.name}: wheel metadata is invalid")
            fields = BytesParser().parsebytes(wheel.read(meta[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildError(f"{path.name}: unreadable wheel") from exc
    name, version = fields.get("Name", ""), fields.get("Version", "")
    if not name or not version:
        fail(f"{path.name}: wheel has no name or version")
    return name, version


def wheel_members(path: Path, package: str, version: str) -> dict[str, str]:
    """Member digests of a wheel whose metadata names the expected package and version."""
    try:
        with zipfile.ZipFile(path) as wheel:
            names = wheel.namelist()
            if len(names) != len(set(names)):
                fail(path.name + ": duplicate wheel member")
            for name in names:
                if not name.endswith("/"):
                    member_name(name)
            meta = [n for n in names if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(meta) != 1:
                fail(path.name + ": wheel metadata is invalid")
            fields = BytesParser().parsebytes(wheel.read(meta[0]))
            if normalize(fields.get("Name", "")) != normalize(package) or fields.get("Version") != version:
                fail(path.name + ": wheel package or version differs")
            return {n: sha(wheel.read(n)) for n in names if not n.endswith("/")}
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BuildError(path.name + ": unreadable wheel") from exc


def wheel_script_names(path: Path) -> frozenset[str]:
    """Script names an installer creates from entry points and ``*.data/scripts`` members."""
    names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as wheel:
            members = wheel.namelist()
            for member in members:
                if member.endswith("/"):
                    continue
                top, _, rest = member.partition("/")
                if top.endswith(".data") and rest.startswith("scripts/"):
                    script = rest[len("scripts/"):]
                    if "/" in script:
                        fail(path.name + ": wheel script member is not a plain file")
                    names.add(script)
            meta = [n for n in members if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(meta) != 1:
                fail(path.name + ": wheel metadata is invalid")
            entry = meta[0].rsplit("/", 1)[0] + "/entry_points.txt"
            if entry in members:
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
                try:
                    parser.read_string(wheel.read(entry).decode("utf-8"))
                except (UnicodeError, configparser.Error) as exc:
                    raise BuildError(path.name + ": invalid wheel entry points") from exc
                for section in ("console_scripts", "gui_scripts"):
                    if parser.has_section(section):
                        names.update(name.strip() for name, _ in parser.items(section))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise BuildError(path.name + ": unreadable wheel") from exc
    for name in names:
        if not name or "/" in name or name in (".", "..") or "\x00" in name:
            fail(path.name + ": wheel script name is not a plain file")
    return frozenset(names)


def check_wheel_matches_source(wheel_path: Path, source_dir: Path) -> None:
    """The Telegram wheel must carry exactly the selected commit's package files."""
    expected = {p.relative_to(source_dir / "src").as_posix(): p.read_bytes()
                for p in (source_dir / "src/wuwaterm_telegram").rglob("*") if p.is_file()}
    with zipfile.ZipFile(wheel_path) as wheel:
        found = {n: wheel.read(n) for n in wheel.namelist()
                 if n.startswith("wuwaterm_telegram/") and not n.endswith("/")}
    if found != expected:
        fail("Telegram wheel differs from the selected source commit")


def dockerfile_bases(text: str) -> list[str]:
    stages: set[str] = set()
    refs: list[str] = []
    for line in text.splitlines():
        words = line.strip().split()
        if not words or words[0].upper() != "FROM":
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        if not args:
            fail("invalid FROM instruction")
        if args[0].lower() not in stages and args[0] not in refs:
            refs.append(args[0])
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return refs


def core_source_commit(lock: dict[str, Any]) -> str:
    value = lock.get("checkout_sha") if lock.get("kind") == "github-actions-artifact" else lock.get("source_commit")
    if type(value) is not str or not GIT.fullmatch(value):
        fail("the Core lock names no source commit")
    return value


def ci_evidence(source_dir: Path, commit: str, workflow_file: str) -> dict[str, Any]:
    env = os.environ
    if env.get("GITHUB_ACTIONS") != "true":
        fail("official-ci authority is only available inside GitHub Actions")
    try:
        run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
        job, control = env["GITHUB_JOB"], env["GITHUB_SHA"]
    except (KeyError, ValueError) as exc:
        raise BuildError("incomplete GitHub Actions run evidence") from exc
    workflow = source_dir / workflow_file
    if control != commit or not GIT.fullmatch(control) or not workflow.is_file():
        fail("the run does not build the selected commit or its workflow file is missing")
    return {"run_id": run_id, "run_attempt": attempt, "job": job, "workflow_sha256": sha(workflow.read_bytes()),
            "control_commit": control}


# --------------------------------------------------------------------------
# build and seal
# --------------------------------------------------------------------------

def begin(delivery: Path, attempt_id: str) -> Path:
    attempt = delivery / "attempts" / attempt_id
    attempt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        attempt.mkdir(mode=0o700)
    except FileExistsError:
        raise BuildError("delivery attempt already exists; attempt IDs are never reused") from None
    for name in ("state", "inputs", "context", "bundle"):
        (attempt / name).mkdir(mode=0o700)
    fsync_dir(attempt.parent)
    write_state(attempt, "building")
    return attempt


def write_state(attempt: Path, state: str, **extra: str) -> None:
    write_new(attempt / "state" / f"{state}.json",
              canonical({"schema_version": 1, "kind": STATE_KIND, "attempt_id": attempt.name, "state": state,
                         "product": PRODUCT, "utc": utc_now(), **extra}))


def read_source_archive(path: Path) -> tuple[dict[str, tuple[int, str, int]], dict[str, bytes]]:
    """Regular-file members of the git archive, and the files sealing has to read."""
    members: dict[str, tuple[int, str, int]] = {}
    contents: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:") as tar:
        for member in tar:
            if member.isreg():
                data = tar.extractfile(member).read()  # type: ignore[union-attr]
                name = member_name(member.name)
                members[name] = (0o100755 if member.mode & 0o111 else 0o100644, sha(data), len(data))
                if name in SOURCE_FILES:
                    contents[name] = data
    for needed in SOURCE_FILES:
        if needed not in contents:
            fail("source archive lacks " + needed)
    return members, contents


def context_manifest(members: dict[str, tuple[int, str, int]]) -> str:
    """Core's digest of the archived context: every regular file, its mode and bytes."""
    return sha(canonical([{"path": p, "mode": oct(m)[2:], "sha256": s, "size": n}
                          for p, (m, s, n) in sorted(members.items())]))


def probe_claims(report: dict[str, Any], program_sha256: str, report_sha256: str, layer: str) -> dict[str, Any]:
    """The bundle's probe claims, derived from the report Core recomputes anyway."""
    return {
        "program_sha256": program_sha256, "report_sha256": report_sha256, "report_layer_digest": layer,
        "report_path": REPORT_PATH, "interpreter": report["interpreter"], "site_packages": report["site_packages"],
        "packages": [{k: p[k] for k in ("name", "version", "location", "files")} for p in report["packages"]],
        "extra_files": report["extra_files"],
    }


def seal(attempt: Path, *, commit: str, tree: str, tag: dict[str, str] | None, iid: str, facts: dict[str, Any],
         report_raw: bytes, base_images: list[dict[str, str]], build_args: list[dict[str, str]],
         wheels: list[dict[str, str]], core_binding: dict[str, Any], compatibility: dict[str, Any],
         authority: str, ci: dict[str, Any] | None, script_sha256: str) -> bytes:
    source = attempt / "inputs" / "source.tar"
    members, contents = read_source_archive(source)
    report = check_report(report_raw)
    write_new(attempt / "bundle" / REPORT_FILE, report_raw, 0o644)
    telegram = next(w for w in wheels if w["role"] == "telegram")
    bundle = {
        "schema_version": 1, "kind": BUNDLE_KIND, "attempt_id": attempt.name, "product": PRODUCT,
        "repository": REPOSITORY, "version": telegram["version"],
        "source": {"commit": commit, "tree": tree, "archive_sha256": hash_file(source)[0]}, "tag": tag,
        "recipe": {
            "dockerfile_sha256": sha(contents["deploy/Dockerfile"]), "build_script_sha256": script_sha256,
            "context_manifest_sha256": context_manifest(members),
            "target": None, "platform": "linux/amd64", "build_args": build_args, "base_images": base_images,
            "lock_sha256": sha(contents["core-lock.json"]),
        },
        "wheels": wheels, "core_binding": core_binding, "oci": facts,
        "install_probe": probe_claims(report, sha(contents["scripts/install_probe.py"]), sha(report_raw),
                                       facts["layers"][-1]["digest"]),
        "build_result": {"authority": authority, "foreground_exit": 0, "iid": iid, "ci": ci},
        "compatibility": compatibility,
        "payload": [{"path": ARCHIVE_FILE, "sha256": facts["archive_sha256"], "size": facts["archive_size"]},
                    {"path": REPORT_FILE, "sha256": sha(report_raw), "size": len(report_raw)}],
    }
    raw = canonical(bundle)
    write_new(attempt / "bundle" / BUNDLE_FILE, raw, 0o644)
    return raw


def check_sealed(attempt: Path) -> dict[str, Any]:
    """Offline self-check of the written bundle against its own files.

    The image filesystem is rebuilt from the layer tars, so the probe report is
    checked against the bytes it describes and the wheels they came from. This
    is deliberately a subset of Core's verifier; ``--core-tool`` remains the
    authority for a bundle Core will acquire.
    """
    directory = attempt / "bundle"
    if sorted(os.listdir(directory)) != sorted((BUNDLE_FILE, ARCHIVE_FILE, REPORT_FILE)):
        fail("bundle directory must hold exactly bundle.json, image.oci.tar and install-probe.json")
    raw = (directory / BUNDLE_FILE).read_bytes()
    bundle = parse_json(raw)
    if canonical(bundle) != raw:
        fail("bundle.json is not canonical")
    for item in bundle["payload"]:
        if hash_file(directory / item["path"]) != (item["sha256"], item["size"]):
            fail("bundle payload file differs: " + item["path"])
    source_path = attempt / "inputs" / "source.tar"
    if hash_file(source_path)[0] != bundle["source"]["archive_sha256"]:
        fail("source archive differs from the bundle")
    members, contents = read_source_archive(source_path)
    recipe = bundle["recipe"]
    if sha(contents["deploy/Dockerfile"]) != recipe["dockerfile_sha256"]:
        fail("recipe Dockerfile differs from the sealed source")
    if sha(contents["core-lock.json"]) != recipe["lock_sha256"]:
        fail("recipe lock differs from the sealed source")
    if context_manifest(members) != recipe["context_manifest_sha256"]:
        fail("recipe context manifest differs from the sealed source")
    if sha(contents["scripts/install_probe.py"]) != bundle["install_probe"]["program_sha256"]:
        fail("probe program differs from the sealed source")
    image = read_oci(directory / ARCHIVE_FILE)
    if image.facts != bundle["oci"]:
        fail("sealed OCI archive differs from the bundle")
    report = image.report
    if report != (directory / REPORT_FILE).read_bytes() or sha(report) != bundle["install_probe"]["report_sha256"]:
        fail("sealed OCI archive differs from the bundle")
    if bundle["build_result"]["iid"] not in (image.facts["manifest_digest"], image.facts["config_digest"]):
        fail("build image ID differs from the sealed archive")
    wheels: dict[str, dict[str, str]] = {}
    wheel_scripts: dict[str, frozenset[str]] = {}
    wheel_hashes: dict[str, str] = {}
    for wheel in bundle["wheels"]:
        path = attempt / "inputs" / "wheels" / wheel["filename"]
        if hash_file(path)[0] != wheel["sha256"]:
            fail("wheel bytes differ from the bundle: " + wheel["filename"])
        if wheel["role"] in ("core", "telegram"):
            wheels[wheel["package"]] = wheel_members(path, wheel["package"], wheel["version"])
            wheel_scripts[wheel["package"]] = wheel_script_names(path)
            wheel_hashes[wheel["package"]] = wheel["sha256"]
    check_image(image, bundle, report, members, wheels, wheel_scripts, wheel_hashes)
    return bundle


def check_image(image: OciArchive, bundle: dict[str, Any], report_raw: bytes,
                source_members: dict[str, tuple[int, str, int]], wheels: dict[str, dict[str, str]],
                wheel_scripts: dict[str, frozenset[str]], wheel_hashes: dict[str, str]) -> None:
    """Bind the sealed claims to the reconstructed filesystem, not to the report's word."""
    probe = bundle["install_probe"]
    if probe["report_path"] != REPORT_PATH:
        fail("unexpected probe report path")
    if probe["report_layer_digest"] != image.facts["layers"][-1]["digest"]:
        fail("probe report is not in the final layer")
    _, program = image.rootfs.regular(PROGRAM_PATH)
    if program.sha256 != probe["program_sha256"] or program.layer >= len(image.facts["layers"]) - 1:
        fail("probe program in the image differs from the sealed source")
    report = check_report(report_raw)
    if probe_claims(report, probe["program_sha256"], sha(report_raw), probe["report_layer_digest"]) != probe:
        fail("bundle probe claims differ from the probe report")
    versions = {p["name"]: p["version"] for p in report["packages"]}
    if versions.get("wuwaterm") != bundle["core_binding"]["core_version"]:
        fail("installed Core version differs from the Core binding")
    if versions.get("wuwaterm-telegram") != bundle["version"]:
        fail("installed Telegram version differs from the bundle")
    check_installed(image.rootfs, report, wheels, wheel_scripts, wheel_hashes, source_members)


def check_installed(rootfs: RootFS, report: dict[str, Any], wheels: dict[str, dict[str, str]],
                    wheel_scripts: dict[str, frozenset[str]], wheel_hashes: dict[str, str],
                    source_members: dict[str, tuple[int, str, int]]) -> None:
    """Every reported file must be the image's bytes and a verified wheel's bytes."""
    for package in report["packages"]:
        name, location = package["name"], package["location"]
        if not location.startswith("/") or posixpath.normpath(location) != location:
            fail("package location is not normalized")
        site = rootfs.resolve(location)
        site_node = rootfs.nodes.get(site)
        if site_node is None or site_node.kind != "dir":
            fail("package location is missing in the image")
        dist_infos = []
        for path, node in rootfs.nodes.items():
            if node.kind == "dir" and posixpath.dirname(path) == site and path.endswith(".dist-info"):
                meta = path + "/METADATA"
                if meta in rootfs.nodes:
                    parsed = BytesParser().parsebytes(rootfs.read(meta))
                    if normalize(parsed.get("Name", "")) == normalize(name):
                        dist_infos.append((path, parsed))
        if len(dist_infos) != 1:
            fail("installed distribution metadata is missing or ambiguous: " + name)
        dist_info, metadata = dist_infos[0]
        if metadata.get("Version") != package["version"]:
            fail("installed version differs from the probe report: " + name)
        record_paths = record_rows(rootfs.read(dist_info + "/RECORD"))
        if len(record_paths) != package["record_entries"]:
            fail("probe RECORD entry count differs: " + name)
        if [item["path"] for item in package["files"]] != sorted({p for p in record_paths if not compiled_cache(p)}):
            fail("probe file list differs from the installed RECORD: " + name)
        installed: dict[str, str] = {}
        for item in package["files"]:
            target = posixpath.normpath(posixpath.join(location, item["path"]))
            _, node = rootfs.regular(target)
            if node.sha256 != item["sha256"] or node.size != item["size"]:
                fail("installed file differs from the probe report: " + item["path"])
            installed[item["path"]] = item["sha256"]
        # A file placed in the distribution's package directories but left out of
        # RECORD would still be importable.
        tops = {p.split("/", 1)[0] for p in installed if not p.startswith("../") and "/" in p}
        for top in tops:
            prefix = site + "/" + top + "/"
            for path, node in rootfs.nodes.items():
                if path.startswith(prefix) and node.kind != "dir":
                    relative = path[len(site) + 1:]
                    if not compiled_cache(relative) and relative not in installed:
                        fail("unrecorded file in the installed package: " + relative)
        if name not in wheels:
            fail("no verified wheel for the probed package: " + name)
        for path, digest in wheels[name].items():
            if installer_path(path):
                continue
            if installed.get(path) != digest:
                fail("installed file differs from the verified wheel: " + path)
        # The wheel inventory also bounds what may be installed. A reported file is
        # either a wheel member bytes-for-bytes, an installer-written dist-info file,
        # a launcher the wheel's entry points (or ``*.data/scripts``) declare, or a
        # ``*.data/data`` member the installer relocated to the prefix. A path is never
        # normalized after the fact: ``zz.data/../evil.pth`` and a ``*.data`` member
        # left where no installer puts it are not installed content.
        dist_info_name = posixpath.basename(dist_info)
        bin_dir = posixpath.dirname(report["interpreter"])
        data_root = posixpath.normpath(posixpath.join(location, "../../.."))
        for path, digest in installed.items():
            if posixpath.normpath(path) != path:
                fail("installed file path is not normalized: " + name + " " + path)
            if path in wheels[name]:
                if ".data/" in path:
                    fail("installed file is an unrelocated wheel data member: " + name + " " + path)
                continue
            if posixpath.dirname(path) == dist_info_name and posixpath.basename(path) in INSTALLER_FILES:
                continue
            if path.startswith("../"):
                target = posixpath.normpath(posixpath.join(location, path))
                if posixpath.dirname(target) == bin_dir and posixpath.basename(target) in wheel_scripts[name]:
                    continue
                if relocated_data(wheels[name], digest, target, data_root):
                    continue
            fail("installed file is not in the verified wheel: " + name + " " + path)
        direct = package["direct_url"]
        hashes = direct.get("archive_info", {}).get("hashes", {}) if type(direct) is dict else {}
        if type(hashes) is dict and "sha256" in hashes and hashes["sha256"] != wheel_hashes[name]:
            fail("installed distribution came from a different wheel: " + name)
    if [item["path"] for item in report["extra_files"]] != sorted(EXTRA_FILES):
        fail("probe extra files differ from the product contract")
    for item in report["extra_files"]:
        _, node = rootfs.regular(item["path"])
        if node.sha256 != item["sha256"] or node.mode != item["mode"]:
            fail("extra file differs from the probe report: " + item["path"])
        origin = source_members.get(EXTRA_FILES[item["path"]])
        if origin is None or origin[1] != item["sha256"]:
            fail("extra file differs from the sealed source archive: " + item["path"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True, help="delivery root; the attempt is created beneath it")
    p.add_argument("--source", type=Path, default=ROOT, help="Git repository holding the commit (default: this one)")
    p.add_argument("--commit", default="HEAD")
    p.add_argument("--tag", help="annotated or lightweight tag that must peel to the commit")
    official = p.add_argument_group("official Core", "the committed core-lock.json decides the Core wheel")
    official.add_argument("--core-wheel", type=Path)
    local = p.add_argument_group("local Core", "an explicit Core wheel; the committed lock is untouched")
    local.add_argument("--local-core-wheel", type=Path)
    local.add_argument("--local-core-sha256")
    local.add_argument("--local-core-source-commit")
    p.add_argument("--telegram-wheel", type=Path, required=True)
    p.add_argument("--wheelhouse", type=Path, help="dependency wheels; builds offline with --network none")
    p.add_argument("--authority", choices=("local-source", "official-ci"), default="local-source")
    p.add_argument("--workflow-file", default=".github/workflows/release.yml")
    p.add_argument("--telegram-state-protocol", type=int, default=1)
    p.add_argument("--core-tool", type=Path, help="Core component_artifact.py; its verify-bundle must accept the bundle")
    args = p.parse_args(argv)
    try:
        refuse_inherited_fd9()
        is_local = args.local_core_wheel is not None
        if is_local == (args.core_wheel is not None) or (is_local and not (
                args.local_core_sha256 and args.local_core_source_commit)):
            p.error("use --core-wheel, or --local-core-wheel with --local-core-sha256 and --local-core-source-commit")
        if is_local and (not HEX64.fullmatch(args.local_core_sha256) or not GIT.fullmatch(args.local_core_source_commit)):
            p.error("invalid --local-core-sha256 or --local-core-source-commit")
        return build(args)
    except (BuildError, OSError) as exc:
        print("build_delivery: " + str(exc), file=sys.stderr)
        return 2


def build(args: argparse.Namespace) -> int:
    # A container-driver builder would start a BuildKit container; refuse it.
    buildkit = False
    if subprocess.run(["docker", "buildx", "version"], close_fds=True, pass_fds=(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
        driver = re.search(r"^Driver:\s*(\S+)", run(["docker", "buildx", "inspect"], capture=True), re.M)
        if not driver or driver.group(1) != "docker":
            fail("the current builder driver is not the daemon's docker driver")
        buildkit = True
    git = ["git", "-C", str(args.source)]
    commit = run([*git, "rev-parse", "--verify", args.commit + "^{commit}"], capture=True)
    tree = run([*git, "rev-parse", "--verify", commit + "^{tree}"], capture=True)
    tag = None
    if args.tag:
        tag_object = run([*git, "rev-parse", "--verify", "refs/tags/" + args.tag], capture=True)
        if run([*git, "rev-parse", "--verify", "refs/tags/" + args.tag + "^{commit}"], capture=True) != commit:
            fail(f"tag {args.tag} does not peel to {commit}")
        tag = {"object": tag_object, "peeled_commit": commit}
    # Absolutized: git -C resolves a relative archive output beneath --source, not here.
    attempt = begin(args.out, new_id()).resolve()
    print("build_delivery: attempt " + attempt.name, file=sys.stderr)

    source_tar, source_dir = attempt / "inputs" / "source.tar", attempt / "context" / "source"
    run([*git, "archive", "--format=tar", "-o", str(source_tar), commit])
    source_dir.mkdir()
    with tarfile.open(source_tar, mode="r:") as tar:
        tar.extractall(source_dir, filter="data")
    # Everything below runs the archive's own bytes, not the working tree.
    ci = ci_evidence(source_dir, commit, args.workflow_file) if args.authority == "official-ci" else None
    pins = [d.removeprefix("wuwaterm==") for d in tomllib.loads(
        (source_dir / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        if d.startswith("wuwaterm==")]
    if len(pins) != 1:
        fail("the archive's project must pin exactly one Core version")
    core_version = pins[0]

    wheels_dir = attempt / "inputs" / "wheels"
    wheels_dir.mkdir()
    core_path = args.local_core_wheel or args.core_wheel
    telegram_path = args.telegram_wheel
    context = attempt / "context" / "build"
    telegram_sha = hash_file(telegram_path)[0]
    prepare = [sys.executable, "-B", str(source_dir / "scripts/prepare_docker_context.py"),
               "--telegram", str(telegram_path), "--telegram-sha256", telegram_sha, "--output-dir", str(context)]
    if args.local_core_wheel:
        core_sha, source_commit = args.local_core_sha256, args.local_core_source_commit
        prepare += ["--local-core-wheel", str(core_path), "--local-core-sha256", core_sha]
    else:
        lock = source_dir / "core-lock.json"
        record = json.loads(lock.read_text(encoding="utf-8"))
        core_sha, source_commit = record["sha256"], core_source_commit(record)
        prepare += ["--lock", str(lock), "--core", str(core_path)]
    # The archive's own context builder checks both wheels' hashes, names and versions.
    run(prepare)
    check_wheel_matches_source(telegram_path, source_dir)

    wheels = []
    for role, package, path in (("core", "wuwaterm", core_path), ("telegram", "wuwaterm-telegram", telegram_path)):
        shutil.copyfile(path, wheels_dir / path.name)
        wheels.append({"filename": path.name, "package": package, "version": wheel_identity(path)[1],
                       "sha256": hash_file(path)[0], "role": role})
    build_args = [("WUWATERM_SOURCE_REVISION", commit)]
    network: list[str] = []
    if args.wheelhouse:
        deps = context / WHEELHOUSE_DIR
        deps.mkdir()
        for path in sorted(args.wheelhouse.glob("*.whl")):
            if path.name in (core_path.name, telegram_path.name):
                if hash_file(path)[0] != hash_file(wheels_dir / path.name)[0]:
                    fail("wheelhouse holds a different " + path.name)
                continue
            name, version = wheel_identity(path)
            shutil.copyfile(path, deps / path.name)
            shutil.copyfile(path, wheels_dir / path.name)
            wheels.append({"filename": path.name, "package": name, "version": version,
                           "sha256": hash_file(path)[0], "role": "dependency"})
        build_args += [("PIP_NO_INDEX", "1"), ("PIP_FIND_LINKS", "/app/" + WHEELHOUSE_DIR)]
        network = ["--network", "none"]
    wheels.sort(key=lambda w: ({"core": 0, "telegram": 1, "dependency": 2}[w["role"]], w["filename"]))
    command_args = build_args + ([("CORE_WHEEL_SHA256", core_sha)] if args.local_core_wheel else [])

    dockerfile = source_dir / "deploy/Dockerfile"
    base_images = [{"ref": ref, "digest": run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], capture=True)}
                   for ref in dockerfile_bases(dockerfile.read_text(encoding="utf-8"))]
    if not base_images or not all(DIGEST.fullmatch(b["digest"]) for b in base_images):
        fail("base images must be present locally with a full image ID (the build never pulls)")
    refuse_inherited_fd9()
    iid_file = attempt / "state" / "iid"
    # BuildKit now adds provenance/SBOM attestations by default. The saved
    # archive would then be an image index with extra annotated manifests,
    # which read_oci deliberately refuses. Disable them where BuildKit is in
    # use; the classic builder has no such flags and emits one manifest.
    attestations = ["--provenance=false", "--sbom=false"] if buildkit else []
    run(["docker", "build", *attestations, "--pull=false", "--force-rm", "--platform", "linux/amd64", *network,
         *[a for name, value in command_args for a in ("--build-arg", f"{name}={value}")],
         "--iidfile", str(iid_file), "--file", str(dockerfile), str(context)])
    iid = iid_file.read_text().strip()
    if not DIGEST.fullmatch(iid):
        fail("docker build wrote no full image ID")
    archive = attempt / "bundle" / ARCHIVE_FILE
    run(["docker", "image", "save", "-o", str(archive), iid])
    dropped = normalize_saved_archive(archive)
    for change in dropped:
        print("build_delivery: " + change, file=sys.stderr)

    image = read_oci(archive)
    raw = seal(
        attempt, commit=commit, tree=tree, tag=tag, iid=iid, facts=image.facts, report_raw=image.report,
        base_images=base_images, wheels=wheels, authority=args.authority,
        build_args=sorted(({"name": n, "value": v} for n, v in build_args if n in RECORDED_ARGS),
                          key=lambda a: a["name"]),
        core_binding={"core_version": core_version, "core_wheel_sha256": core_sha,
                      "core_source_commit": source_commit, "compatibility": "exact"},
        compatibility={"api": "v1", "review": "v2", "manuscript": "v1",
                       "db_schema": int(_compat_schema(source_dir)),
                       "telegram_state_protocol": args.telegram_state_protocol},
        ci=ci,
        script_sha256=sha(Path(__file__).read_bytes()))
    check_sealed(attempt)
    if args.core_tool:
        run([sys.executable, "-I", "-B", str(args.core_tool), "verify-bundle", "--bundle",
             str(attempt / "bundle" / BUNDLE_FILE)])
    write_state(attempt, "sealed", bundle_sha256=sha(raw))
    sys.stdout.write(canonical({"state": "sealed", "attempt_id": attempt.name, "bundle": str(attempt / "bundle" / BUNDLE_FILE),
                                "bundle_sha256": sha(raw), "iid": iid}).decode("utf-8"))
    return 0


def _compat_schema(source_dir: Path) -> str:
    match = re.search(r'^DATABASE_SCHEMA_VERSION = "([0-9]+)"$',
                      (source_dir / "src/wuwaterm_telegram/core_compat.py").read_text(encoding="utf-8"), re.M)
    if match is None:
        fail("cannot read the archive's database schema version")
    return match.group(1)


if __name__ == "__main__":
    raise SystemExit(main())
