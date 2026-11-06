"""Polling health is local evidence of a real SDK poll, not a network probe."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import ApplicationBuilder, TypeHandler

from wuwaterm_telegram import bot, poll_health

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="poll health needs Linux /proc")


def _sentinel():
    read, write = os.pipe()
    os.close(write)
    return read


def test_ready_requires_live_sentinel_and_does_not_touch_state(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep", encoding="utf-8")
    sentinel = _sentinel()
    try:
        poll_health.record_success(sentinel)
        assert poll_health.health()["status"] == "ready"
        record = json.loads((tmp_path / "poll.json").read_text())
        assert record["pid"] == os.getpid() and record["schema"] == 1
        assert unrelated.read_text() == "keep"
        os.close(sentinel)
        sentinel = None
        with pytest.raises(ValueError):
            poll_health.health()
    finally:
        if sentinel is not None:
            os.close(sentinel)


@pytest.mark.parametrize("kind", ["old-boot", "dead-pid", "reused-pid", "stale", "future", "fd-reused", "unknown-field"])
def test_bad_or_stale_process_identity_is_not_ready(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    sentinel = _sentinel()
    try:
        poll_health.record_success(sentinel)
        record_path = tmp_path / "poll.json"
        record = json.loads(record_path.read_text())
        if kind == "old-boot":
            record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        elif kind == "dead-pid":
            record["pid"] = 99999999
        elif kind == "reused-pid":
            record["start_ticks"] += 1
        elif kind == "stale":
            record["success_monotonic_ns"] = time.monotonic_ns() - 91_000_000_000
        elif kind == "future":
            record["success_monotonic_ns"] = time.monotonic_ns() + 10_000_000_000
        elif kind == "fd-reused":
            record["sentinel_inode"] += 1
        else:
            record["unexpected"] = True
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises((OSError, ValueError)):
            poll_health.health()
    finally:
        os.close(sentinel)


def test_invalid_record_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    path = tmp_path / "poll.json"
    path.write_bytes(b"user-owned invalid file")
    path.chmod(0o600)
    before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    sentinel = _sentinel()
    try:
        with pytest.raises(ValueError):
            poll_health.record_success(sentinel)
        with pytest.raises(ValueError):
            poll_health.health()
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
    finally:
        os.close(sentinel)


def test_unwritable_directory_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    sentinel = _sentinel()
    try:
        poll_health.record_success(sentinel)
        path = tmp_path / "poll.json"
        before = path.read_bytes()
        tmp_path.chmod(0o500)
        with pytest.raises(ValueError):
            poll_health.record_success(sentinel)
        with pytest.raises(ValueError):
            poll_health.health()
        assert path.read_bytes() == before
    finally:
        tmp_path.chmod(0o700)
        os.close(sentinel)


def test_existing_live_poller_record_not_stolen(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    first, second = _sentinel(), _sentinel()
    try:
        poll_health.record_success(first)
        path = tmp_path / "poll.json"
        before = path.read_bytes()
        with pytest.raises(ValueError):
            poll_health.record_success(second)
        assert path.read_bytes() == before
    finally:
        os.close(first)
        os.close(second)


def test_dead_process_with_prior_success_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    program = """import os,sys,time
from pathlib import Path
from wuwaterm_telegram import poll_health
poll_health.HEALTH_DIR=Path(sys.argv[1])
read,write=os.pipe(); os.close(write)
poll_health.record_success(read)
print('ready', flush=True)
time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", program, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert poll_health.health()["pid"] == process.pid
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(ValueError):
            poll_health.health()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_health_cli_is_read_only_and_does_not_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(poll_health, "HEALTH_DIR", tmp_path)
    sentinel = _sentinel()
    try:
        poll_health.record_success(sentinel)
        before = (tmp_path / "poll.json").read_bytes()
        program = """import socket,sys
from pathlib import Path
socket.socket.connect = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('network'))
from wuwaterm_telegram import poll_health
from wuwaterm_telegram.operations import main
poll_health.HEALTH_DIR=Path(sys.argv[1])
raise SystemExit(main(sys.argv[2:]))
"""
        def run(*args):
            return subprocess.run([sys.executable, "-B", "-c", program, str(tmp_path), *args],
                                  capture_output=True, text=True, check=False)
        result = run("health")
        assert result.returncode == 0 and json.loads(result.stdout)["status"] == "ready"
        assert run("health", "--max-age", "0").returncode == 2
        assert run("health", "--max-age", "not-an-int").returncode == 2
        assert (tmp_path / "poll.json").read_bytes() == before
    finally:
        os.close(sentinel)


def test_standard_sdk_transport_defaults_are_preserved():
    standard = ApplicationBuilder().token("123456:LOCAL_FIXTURE").build().bot
    observed = bot.PollHealthBot(token="123456:LOCAL_FIXTURE")
    assert type(standard).__name__ == "ExtBot"
    for left, right in zip(standard._request, observed._request, strict=True):
        for name in ("read", "write", "connect", "pool"):
            assert getattr(left._client_kwargs["timeout"], name) == getattr(right._client_kwargs["timeout"], name)
        assert left._client_kwargs["limits"].max_connections == right._client_kwargs["limits"].max_connections
        assert left._client_kwargs["http2"] == right._client_kwargs["http2"]


def test_opt_in_preserves_default_builder(monkeypatch, sample_db):
    monkeypatch.delenv("WUWATERM_POLL_HEALTH", raising=False)
    app = bot.create_application("123456:LOCAL_FIXTURE", sample_db)
    assert type(app.bot).__name__ == "ExtBot"
    monkeypatch.setenv("WUWATERM_POLL_HEALTH", "1")
    app = bot.create_application("123456:LOCAL_FIXTURE", sample_db)
    assert isinstance(app.bot, bot.PollHealthBot)
    assert isinstance(app.updater, bot.PollHealthUpdater)


def test_invalid_opt_in_fails_before_state_migration(monkeypatch, sample_db, tmp_path):
    state = tmp_path / "new-state"
    legacy = sample_db.parent / "chat_settings.json"
    legacy.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    monkeypatch.setenv("WUWATERM_POLL_HEALTH", "typo")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    with pytest.raises(ValueError):
        bot.create_application("123456:LOCAL_FIXTURE", sample_db)
    assert not state.exists() and legacy.exists()


@contextmanager
def local_bot_api(mode):
    state = {"calls": [], "polls": 0, "mode": mode, "allow_next": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            method = self.path.rsplit("/", 1)[-1]
            state["calls"].append(method)
            mode = state["mode"]
            code = 200
            if method == "getMe":
                code = 401 if mode == "init-401" else 200
                result = {"id": 123456, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
            elif method == "deleteWebhook":
                result = True
            elif method == "getUpdates":
                state["polls"] += 1
                if mode in ("success-then-401", "success-then-500") and state["polls"] > 1:
                    state["allow_next"].wait(5)
                time.sleep(0.05)
                code = (401 if mode == "poll-401" or (mode == "success-then-401" and state["polls"] > 1)
                        else 500 if mode == "success-then-500" and state["polls"] > 1 else 200)
                result = ([{"bad": "payload"}] if mode == "bad-update" else
                          [{"update_id": 42}] if mode == "invalid-record-update" and state["polls"] == 1 else [])
            else:
                raise AssertionError("unexpected method " + method)
            payload = ({"ok": True, "result": result} if code == 200 else
                       {"ok": False, "error_code": code, "description": "Fixture failure"})
            encoded = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        state["allow_next"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.025)
    raise AssertionError("local SDK condition did not occur")


@pytest.mark.parametrize("mode", ["success", "init-401", "poll-401", "bad-update", "success-then-401", "success-then-500", "unwritable", "invalid-record", "deep-record", "invalid-record-update"])
def test_actual_sdk_only_stamps_success_and_stopped_fails(monkeypatch, sample_db, tmp_path, mode):
    health_dir = tmp_path / "health"
    health_dir.mkdir()
    if mode == "unwritable":
        health_dir.chmod(0o500)
    invalid_record = health_dir / "poll.json"
    if mode in ("invalid-record", "deep-record", "invalid-record-update"):
        invalid_record.write_bytes(b"[" * 1500 + b"]" * 1500 if mode == "deep-record" else b"unrelated content")
        invalid_record.chmod(0o600)
    original_invalid = invalid_record.read_bytes() if invalid_record.exists() else None
    monkeypatch.setattr(poll_health, "HEALTH_DIR", health_dir)
    monkeypatch.setenv("WUWATERM_POLL_HEALTH", "1")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NO_PROXY", "*")
    original_connect = socket.socket.connect
    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("non-loopback network denied")
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    with local_bot_api(mode) as (state, base):
        real_bot = bot.PollHealthBot
        def local_transport(*args, **kwargs):
            return real_bot(*args, base_url=base + "/bot", base_file_url=base + "/file/bot", **kwargs)
        monkeypatch.setattr(bot, "PollHealthBot", local_transport)
        app = bot.create_application("123456:LOCAL_FIXTURE", sample_db)
        delivered = []
        async def capture(update, _context):
            delivered.append(update.update_id)
        app.add_handler(TypeHandler(Update, capture), group=10)
        async def exercise():
            initialized = False
            try:
                if mode == "init-401":
                    with pytest.raises(InvalidToken):
                        await app.initialize()
                    assert not (health_dir / "poll.json").exists()
                    return
                await app.initialize()
                initialized = True
                await app.updater.start_polling()
                await app.start()
                if mode in ("success", "success-then-401", "success-then-500"):
                    await _wait_until(lambda: (health_dir / "poll.json").exists())
                    assert poll_health.health()["status"] == "ready"
                elif mode in ("invalid-record", "deep-record", "invalid-record-update"):
                    await _wait_until(lambda: state["polls"] >= 2)
                    assert invalid_record.read_bytes() == original_invalid
                    with pytest.raises(ValueError):
                        poll_health.health()
                    if mode == "invalid-record-update":
                        await _wait_until(lambda: delivered == [42])
                else:
                    await _wait_until(lambda: state["polls"] >= 1)
                    await asyncio.sleep(0.15)
                    assert not (health_dir / "poll.json").exists()
                    if mode == "unwritable":
                        assert state["polls"] >= 2
                if mode == "success-then-401":
                    state["allow_next"].set()
                    await _wait_until(lambda: state["polls"] >= 2)
                    await _wait_until(lambda: app.updater.health_fd is None)
                    with pytest.raises(ValueError):
                        poll_health.health()
                if mode == "success-then-500":
                    state["allow_next"].set()
                    await _wait_until(lambda: state["polls"] >= 2)
                    await asyncio.sleep(1.1)
                    assert app.updater.health_fd is not None
                    with pytest.raises(ValueError):
                        poll_health.health(max_age=1)
                    state["mode"] = "success"
                if mode == "bad-update":
                    state["mode"] = "success"
                if app.updater.running:
                    with suppress(InvalidToken):
                        await app.updater.stop()
                if mode == "success":
                    with pytest.raises(ValueError):
                        poll_health.health()
                if mode == "bad-update":
                    assert not (health_dir / "poll.json").exists()
            finally:
                if app.updater.running:
                    with suppress(InvalidToken):
                        await app.updater.stop()
                if app.running:
                    await app.stop()
                if initialized:
                    await app.shutdown()
        asyncio.run(exercise())
        assert "getMe" in state["calls"]
        if mode != "init-401":
            assert {"deleteWebhook", "getUpdates"} <= set(state["calls"])
        assert "sendMessage" not in state["calls"]
    if mode == "unwritable":
        health_dir.chmod(0o700)
