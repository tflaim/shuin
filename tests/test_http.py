import http.client
import threading

from shuin.httpapi import serve


def test_http_rejects_foreign_host(env):
    server = serve(env.db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health", headers={"Host": "evil.example"})
        foreign = conn.getresponse()
        foreign.read()
        assert foreign.status == 421
        conn.request("GET", "/health", headers={"Host": f"127.0.0.1.evil.example:{port}"})
        dotted = conn.getresponse()
        dotted.read()
        assert dotted.status == 421
        conn.request("GET", "/health", headers={"Host": f"127.0.0.1:{port + 1}"})
        wrong_port = conn.getresponse()
        wrong_port.read()
        assert wrong_port.status == 421
        conn.request("GET", "/health", headers={"Host": f"127.0.0.1:{port}"})
        local = conn.getresponse()
        body = local.read()
        assert local.status == 200
        assert b'"ok":true' in body
        conn.request("GET", "/health", headers={"Host": f"localhost:{port}"})
        named = conn.getresponse()
        named.read()
        assert named.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_rejects_bad_content_length(env):
    server = serve(env.db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    host = f"127.0.0.1:{port}"
    try:
        for raw in ("-1", "nope", "+5"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("POST", "/health", body=b"{}", headers={"Host": host, "Content-Length": raw})
            response = conn.getresponse()
            body = response.read()
            assert response.status == 400
            assert b"bad_length" in body
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
