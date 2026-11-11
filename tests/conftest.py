"""Offline tests must never read a developer's real credentials or live configuration."""
import socket
import shutil
import threading
import pytest


@pytest.fixture(autouse=True)
def isolate_environment_file(monkeypatch, tmp_path):
    monkeypatch.setenv('INVOICEDESK_ENV_FILE', str(tmp_path/'unused.env'))
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'offline-test-key')
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv('LLM_MODEL', '')
    monkeypatch.setenv('LLM_MODE', 'off')
    monkeypatch.setenv('INVOICEDESK_PARSER', 'poppler' if shutil.which('pdftotext') else 'pdfplumber')
    monkeypatch.setenv('INVOICEDESK_MATCHER', 'development')
    original_connect, original_socketpair = socket.socket.connect, socket.socketpair
    internal = threading.local()
    def socketpair(*args, **kwargs):
        # CPython on Windows builds socketpair using one local TCP connection.
        # Scope the exemption to this implementation and this thread only; test
        # requests and providers still cannot resolve or connect to any host.
        previous = getattr(internal, 'socketpair', False)
        internal.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal.socketpair = previous
    def deny_network(*args, **kwargs):
        raise AssertionError('Network access is disabled in offline tests.')
    def connect(sock, address):
        if getattr(internal, 'socketpair', False) and isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return deny_network()
    monkeypatch.setattr(socket, 'getaddrinfo', deny_network)
    monkeypatch.setattr(socket, 'socketpair', socketpair)
    monkeypatch.setattr(socket.socket, 'connect', connect)
