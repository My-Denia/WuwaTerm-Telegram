"""Keep offline Telegram tests on local sockets, including Python children.

The test runner puts this directory first on a sanitized PYTHONPATH. This is a
Python-level guard, not an operating-system network namespace.
"""
from __future__ import annotations

import ipaddress
import socket


def _host_allowed(host: object) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


def _check_address(sock: socket.socket, address: object) -> None:
    if sock.family == socket.AF_UNIX:
        return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        raise PermissionError('test socket family is not allowed')
    if not isinstance(address, tuple) or not address or not _host_allowed(address[0]):
        raise PermissionError('test socket address must be loopback')


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_bind = socket.socket.bind
_listen = socket.socket.listen
_sendto = socket.socket.sendto
_sendmsg = getattr(socket.socket, 'sendmsg', None)
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname
_gethostbyname_ex = socket.gethostbyname_ex
_gethostbyaddr = socket.gethostbyaddr
_getnameinfo = socket.getnameinfo


def _safe_connect(self, address):
    _check_address(self, address)
    return _connect(self, address)


def _safe_connect_ex(self, address):
    _check_address(self, address)
    return _connect_ex(self, address)


def _safe_bind(self, address):
    _check_address(self, address)
    return _bind(self, address)


def _safe_listen(self, *args):
    _check_address(self, self.getsockname())
    return _listen(self, *args)


def _safe_sendto(self, data, *args):
    if not args:
        raise TypeError('sendto needs an address')
    _check_address(self, args[-1])
    return _sendto(self, data, *args)


def _safe_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if address is not None:
        _check_address(self, address)
        return _sendmsg(self, buffers, ancdata, flags, address)
    return _sendmsg(self, buffers, ancdata, flags)


def _safe_getaddrinfo(host, *args, **kwargs):
    if not _host_allowed(host):
        raise PermissionError('test DNS lookup must be loopback')
    # Avoid consulting external DNS for localhost when /etc/hosts differs.
    if host == 'localhost' or host == b'localhost':
        host = '127.0.0.1'
    return _getaddrinfo(host, *args, **kwargs)


def _safe_gethostbyname(host):
    if not _host_allowed(host):
        raise PermissionError('test DNS lookup must be loopback')
    return _gethostbyname('127.0.0.1' if host == 'localhost' else host)


def _safe_gethostbyname_ex(host):
    if not _host_allowed(host):
        raise PermissionError('test DNS lookup must be loopback')
    return _gethostbyname_ex('127.0.0.1' if host == 'localhost' else host)


def _safe_gethostbyaddr(host):
    if not _host_allowed(host):
        raise PermissionError('test DNS lookup must be loopback')
    # No reverse lookup is required by the tests.
    return (str(host), [], [str(host)])


def _safe_getnameinfo(address, flags):
    if not isinstance(address, tuple) or not address or not _host_allowed(address[0]):
        raise PermissionError('test DNS lookup must be loopback')
    return _getnameinfo(address, flags | socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)


socket.socket.connect = _safe_connect
socket.socket.connect_ex = _safe_connect_ex
socket.socket.bind = _safe_bind
socket.socket.listen = _safe_listen
socket.socket.sendto = _safe_sendto
if _sendmsg is not None:
    socket.socket.sendmsg = _safe_sendmsg
socket.getaddrinfo = _safe_getaddrinfo
socket.gethostbyname = _safe_gethostbyname
socket.gethostbyname_ex = _safe_gethostbyname_ex
socket.gethostbyaddr = _safe_gethostbyaddr
socket.getnameinfo = _safe_getnameinfo
