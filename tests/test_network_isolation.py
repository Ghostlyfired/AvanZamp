"""Asyncio wakeups work offline without permitting application network access."""
import socket

import pytest


def test_socketpair_works_but_direct_network_connections_remain_blocked():
    left, right = socket.socketpair()
    try:
        left.sendall(b'wakeup')
        assert right.recv(6) == b'wakeup'
    finally:
        left.close()
        right.close()
    with socket.socket() as sock:
        for address in [('127.0.0.1', 1), ('203.0.113.1', 443)]:
            with pytest.raises(AssertionError, match='Network access is disabled'):
                sock.connect(address)
    with pytest.raises(AssertionError, match='Network access is disabled'):
        socket.getaddrinfo('example.com', 443)
