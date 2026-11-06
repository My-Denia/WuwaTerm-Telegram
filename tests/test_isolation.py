"""The test harness must not reach external networks or inherit owner keys."""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest


def test_sensitive_environment_is_not_inherited():
    forbidden = ('TELEGRAM_', 'WUWATERM_', 'OPENAI_')
    assert not any(name.startswith(forbidden) for name in os.environ)
    assert 'OWNER_USER_ID' not in os.environ


def test_socket_guard_blocks_nonloopback_and_dns():
    with pytest.raises(PermissionError):
        socket.getaddrinfo('example.com', 443)
    with pytest.raises(PermissionError):
        socket.getnameinfo(('203.0.113.1', 443), 0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(PermissionError):
            sock.connect(('203.0.113.1', 443))
        with pytest.raises(PermissionError):
            sock.connect_ex(('203.0.113.1', 443))
        with pytest.raises(PermissionError):
            sock.bind(('0.0.0.0', 0))
        with pytest.raises(PermissionError):
            sock.listen()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(PermissionError):
            sock.sendto(b'x', ('203.0.113.1', 443))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))


def test_python_child_inherits_socket_guard():
    program = '''\
import socket
try:
    socket.getaddrinfo('example.com', 443)
except PermissionError:
    print('guarded')
else:
    raise SystemExit('guard missing')
'''
    child = subprocess.run([sys.executable, '-c', program], check=True, text=True, capture_output=True)
    assert child.stdout.strip() == 'guarded'
