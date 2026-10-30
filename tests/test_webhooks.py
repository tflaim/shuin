import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card

_BLOCKED = (
    "http://169.254.169.254/latest/meta-data",
    "http://127.0.0.1/hook",
    "http://127.0.0.1:9/hook",
    "http://localhost/hook",
    "http://localhost:8741/hook",
    "http://10.1.2.3/hook",
    "http://192.168.0.8/hook",
    "http://172.16.0.4/hook",
    "http://[::1]/hook",
    "http://2130706433/hook",
    "http://[64:ff9b::808:808]/hook",
    "http://224.0.0.1/hook",
    "http://[ff02::1]/hook",
    "file:///etc/passwd",
    "gopher://169.254.169.254/",
)


def test_webhook_rejects_private_loopback_and_link_local(env):
    open_card(env, head=SHA_A)
    for url in _BLOCKED:
        with pytest.raises(LedgerError) as raised:
            env.ledger.notify_me(
                env.actors["worker-1"],
                "review.requested",
                "webhook",
                channel_target=url,
            )
        assert raised.value.code == "bad_url"
    stored = env.ledger.conn.execute("SELECT COUNT(*) AS n FROM subscriptions").fetchone()["n"]
    assert stored == 0


def test_localhost_webhooks_are_opt_in(env):
    open_card(env, head=SHA_A)
    with pytest.raises(LedgerError):
        env.ledger.notify_me(
            env.actors["worker-1"],
            "review.requested",
            "webhook",
            channel_target="http://127.0.0.1:9/hook",
        )
    env.ledger.set_setting("allow_localhost_webhooks", "1")
    body = env.ledger.notify_me(
        env.actors["worker-1"],
        "review.requested",
        "webhook",
        channel_target="http://127.0.0.1:9/hook",
    )
    assert body["subscription_id"]
    with pytest.raises(LedgerError) as raised:
        env.ledger.notify_me(
            env.actors["worker-1"],
            "review.requested",
            "webhook",
            channel_target="http://169.254.169.254/latest/meta-data",
        )
    assert raised.value.code == "bad_url"
    with pytest.raises(LedgerError):
        env.ledger.notify_me(
            env.actors["worker-1"],
            "review.requested",
            "webhook",
            channel_target="http://10.0.0.8/hook",
        )


def test_webhook_resolve_rejects_link_local(env):
    from shuin.ledger import post_webhook

    def resolver(host, port):
        assert host == "example.com"
        return ["169.254.169.254"]

    with pytest.raises(LedgerError) as raised:
        post_webhook(
            "http://example.com/hook",
            b"{}",
            {},
            resolver=resolver,
        )
    assert raised.value.code == "bad_url"


def test_webhook_does_not_follow_redirects():
    from shuin.ledger import post_webhook

    hits = {"n": 0, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            hits["paths"].append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with pytest.raises(OSError):
            post_webhook(
                f"http://127.0.0.1:{port}/hook",
                b"{}",
                {"Content-Type": "application/json"},
                allow_localhost=True,
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert hits["n"] == 1
    assert hits["paths"] == ["/hook"]


def test_webhook_post_does_not_follow_a_rebind(monkeypatch):
    from shuin import ledger as ledger_mod
    from shuin.ledger import post_webhook

    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    calls = []

    def resolver(host, _port):
        calls.append(host)
        if len(calls) == 1:
            return ["1.1.1.1"]
        return ["127.0.0.1"]

    real = ledger_mod.socket.create_connection

    def create_connection(address, timeout=None, source_address=None):
        host = address[0]
        if host in ("127.0.0.1", "::1"):
            return real((host, address[1]), timeout, source_address)
        raise OSError("refused")

    monkeypatch.setattr(ledger_mod.socket, "create_connection", create_connection)
    try:
        with pytest.raises((LedgerError, OSError)):
            post_webhook(
                f"http://rebind.example:{port}/hook",
                b"{}",
                {"Content-Type": "application/json"},
                resolver=resolver,
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert hits["n"] == 0
    assert calls == ["rebind.example"]


def test_webhook_post_rechecks_the_connected_peer(monkeypatch):
    from shuin import ledger as ledger_mod
    from shuin.ledger import post_webhook

    hits = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["n"] += 1
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    real = ledger_mod.socket.create_connection

    def create_connection(address, timeout=None, source_address=None):
        return real(("127.0.0.1", port), timeout, source_address)

    monkeypatch.setattr(ledger_mod.socket, "create_connection", create_connection)
    try:
        with pytest.raises(LedgerError) as raised:
            post_webhook(
                "http://rebind.example/hook",
                b"{}",
                {},
                resolver=lambda host, _port: ["1.1.1.1"],
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert raised.value.code == "bad_url"
    assert hits["n"] == 0


def test_webhook_registration_rejects_a_loopback_resolution(env, monkeypatch):
    import shuin.ledger as ledger_mod

    open_card(env, head=SHA_A)

    def resolve(host, _port):
        assert host == "rebind.example"
        return ["127.0.0.1"]

    monkeypatch.setattr(ledger_mod, "_dns_resolve", resolve)
    with pytest.raises(LedgerError) as raised:
        env.ledger.notify_me(
            env.actors["worker-1"],
            "review.requested",
            "webhook",
            channel_target="http://rebind.example/hook",
        )
    assert raised.value.code == "bad_url"
    stored = env.ledger.conn.execute("SELECT COUNT(*) AS n FROM subscriptions").fetchone()["n"]
    assert stored == 0
