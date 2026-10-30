"""Localhost HTTP API. Humans can open the board. Agents send a bearer token."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from shuin.errors import LedgerError
from shuin.github import GhClient
from shuin.ledger import open_ledger
from shuin.render import render_html
from shuin.sanitize import reject_forbidden_fields

_MAX_BODY = 64 * 1024

_ROUTES = [
    ("POST", re.compile(r"^/v1/tasks$"), "create_task", 201),
    ("GET", re.compile(r"^/v1/tasks/ready$"), "ready_tasks", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/depend$"), "depend_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/claim$"), "claim_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/blocked$"), "block_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/unblock$"), "unblock_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/drop$"), "drop_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/done$"), "done_task", 200),
    ("POST", re.compile(r"^/v1/tasks/(T-[0-9]+)/updates$"), "post_update", 201),
    ("GET", re.compile(r"^/v1/tasks/(T-[0-9]+)/claims$"), "claims", 200),
    ("GET", re.compile(r"^/v1/tasks/(T-[0-9]+)/overlap$"), "overlap", 200),
    ("POST", re.compile(r"^/v1/prs$"), "link_pr", 201),
    ("POST", re.compile(r"^/v1/review_requests$"), "review", 201),
    ("POST", re.compile(r"^/v1/evidence$"), "evidence", 201),
    ("POST", re.compile(r"^/v1/verification_queue$"), "enqueue", 201),
    ("POST", re.compile(r"^/v1/verification_queue/claim$"), "claim_slot", 200),
    ("POST", re.compile(r"^/v1/verification_queue/([0-9]+)/heartbeat$"), "heartbeat", 200),
    ("POST", re.compile(r"^/v1/verification_queue/([0-9]+)/release$"), "release", 200),
    ("POST", re.compile(r"^/v1/verification_queue/([0-9]+)/cancel$"), "cancel_queue", 200),
    ("POST", re.compile(r"^/v1/verification_queue/([0-9]+)/drop$"), "drop_queue", 200),
    ("POST", re.compile(r"^/v1/verification_queue/([0-9]+)/stamp$"), "stamp", 200),
    ("GET", re.compile(r"^/v1/queue$"), "queue", 200),
    ("POST", re.compile(r"^/v1/subscriptions$"), "subscribe", 201),
    ("DELETE", re.compile(r"^/v1/subscriptions/([0-9]+)$"), "unsubscribe", 200),
    ("GET", re.compile(r"^/v1/board$"), "board", 200),
    ("GET", re.compile(r"^/v1/inbox$"), "inbox", 200),
    ("POST", re.compile(r"^/v1/inbox/ack$"), "ack_inbox", 200),
    ("POST", re.compile(r"^/v1/seal$"), "seal", 200),
    ("GET", re.compile(r"^/v1/seal$"), "seal_status", 200),
    ("GET", re.compile(r"^/v1/followups$"), "followups", 200),
    ("POST", re.compile(r"^/v1/followups/([0-9]+)/ack$"), "ack_followup", 200),
    ("POST", re.compile(r"^/v1/agents$"), "mint", 201),
    ("POST", re.compile(r"^/v1/agents/([A-Za-z0-9._-]+)/rotate$"), "rotate", 200),
    ("POST", re.compile(r"^/v1/agents/([A-Za-z0-9._-]+)/disable$"), "disable", 200),
]


class Api:
    def __init__(self, db_path, github_factory=None):
        self.db_path = db_path
        self.github_factory = github_factory or GhClient

    def handle(self, method: str, target: str, headers, body: bytes):
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path in ("/", "/board.html"):
            ledger = open_ledger(self.db_path)
            try:
                snap = ledger.snapshot(include_claims=True)
                page = render_html(snap).encode("utf-8")
            finally:
                ledger.close()
            return 200, page, "text/html; charset=utf-8"
        if method == "GET" and path == "/board.json":
            ledger = open_ledger(self.db_path)
            try:
                snap = ledger.snapshot(include_claims=False)
                for task in snap["tasks"]:
                    for item in task.get("evidence") or []:
                        item.pop("uri", None)
            finally:
                ledger.close()
            return 200, _dump(snap), "application/json"
        if method == "GET" and path == "/health":
            return 200, _dump({"ok": True}), "application/json"
        match = None
        groups = ()
        for allowed, pattern, name, status in _ROUTES:
            found = pattern.fullmatch(path)
            if allowed == method and found:
                match = (name, status)
                groups = found.groups()
                break
        if match is None:
            return 404, _dump({"error": "not_found", "message": "No such route."}), "application/json"
        if len(body) > _MAX_BODY:
            return 400, _dump({"error": "too_large", "message": "Body is too large."}), "application/json"
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, _dump({"error": "bad_json", "message": "JSON required."}), "application/json"
            if not isinstance(payload, dict):
                return 400, _dump({"error": "bad_json", "message": "JSON object required."}), "application/json"
            try:
                reject_forbidden_fields(payload)
            except LedgerError as exc:
                return exc.status, _dump(exc.to_dict()), "application/json"
        token = _bearer(headers)
        ledger = open_ledger(self.db_path)
        try:
            try:
                actor = ledger.authenticate(token)
                result = self._dispatch(ledger, actor, match[0], groups, payload, query)
            except LedgerError as exc:
                return exc.status, _dump(exc.to_dict()), "application/json"
        finally:
            ledger.close()
        status = match[1]
        if match[0] == "seal" and not result.get("ok", True):
            status = 422
        return status, _dump(result), "application/json"

    def _dispatch(self, ledger, actor, name, groups, payload, query):
        if name == "create_task":
            return ledger.add_task(
                actor,
                payload.get("title"),
                repo=payload.get("repo"),
                depends_on=payload.get("depends_on"),
                idempotency_key=payload.get("idempotency_key"),
                depends_on_pr=payload.get("depends_on_pr"),
            )
        if name == "depend_task":
            return ledger.depend(
                actor,
                groups[0],
                depends_on=payload.get("depends_on"),
                depends_on_pr=payload.get("depends_on_pr"),
                remove=bool(payload.get("remove")),
            )
        if name == "ready_tasks":
            return ledger.ready_tasks(actor)
        if name == "claim_task":
            return ledger.claim_task(actor, groups[0], assignee=payload.get("assignee"), model=payload.get("model"))
        if name == "block_task":
            return ledger.set_blocked(actor, groups[0], payload.get("reason"))
        if name == "unblock_task":
            return ledger.resolve_blocked(actor, groups[0])
        if name == "drop_task":
            return ledger.drop_task(actor, groups[0])
        if name == "done_task":
            return ledger.complete_task(
                actor,
                groups[0],
                merged_externally=bool(payload.get("merged_externally")),
                reason=payload.get("reason"),
            )
        if name == "post_update":
            return ledger.post_update(
                actor,
                groups[0],
                payload.get("kind"),
                payload.get("body"),
                on_behalf_of=payload.get("on_behalf_of"),
            )
        if name == "claims":
            limit = _query_int(query, "limit", 50)
            return ledger.read_quoted(actor, groups[0], limit=limit)
        if name == "overlap":
            return ledger.check_overlap(actor, groups[0], allow_cwd=False)
        if name == "link_pr":
            task_id, repo, number = _pr_body(payload, require_task=True)
            return ledger.link_pr(actor, task_id, repo, number, node_id=payload.get("github_node_id"))
        if name == "review":
            _task, repo, number = _pr_body(payload, require_task=False)
            return ledger.request_review(
                actor,
                repo,
                number,
                payload.get("tried_failed", None) if "tried_failed" in payload else None,
                on_behalf_of=payload.get("on_behalf_of"),
                commit_sha=payload.get("commit_sha"),
            )
        if name == "evidence":
            _task, repo, number = _pr_body(payload, require_task=False)
            return ledger.add_evidence(
                actor,
                repo,
                number,
                payload.get("kind"),
                payload.get("uri"),
                payload.get("commit_sha"),
                result=payload.get("result"),
                sha256=payload.get("sha256"),
                on_behalf_of=payload.get("on_behalf_of"),
            )
        if name == "enqueue":
            _task, repo, number = _pr_body(payload, require_task=False)
            return ledger.enqueue(
                actor,
                repo,
                number,
                payload.get("commit_sha"),
                on_behalf_of=payload.get("on_behalf_of"),
                require_run=bool(payload.get("require_run")),
                poll=bool(payload.get("poll")),
                github=self.github_factory() if payload.get("poll") else None,
            )
        if name == "claim_slot":
            timeout = payload.get("timeout")
            return ledger.claim_slot(actor, wait=bool(payload.get("wait")), timeout=30 if timeout is None else timeout)
        if name == "heartbeat":
            return ledger.heartbeat(actor, int(groups[0]))
        if name == "release":
            return ledger.release_slot(actor, int(groups[0]), payload.get("result"), note=payload.get("note"))
        if name in ("cancel_queue", "drop_queue"):
            return ledger.cancel_queue(actor, int(groups[0]), reason=payload.get("reason"))
        if name == "stamp":
            return ledger.stamp(actor, int(groups[0]), note=payload.get("note"))
        if name == "queue":
            return {"queue": ledger.queue(maintain=True)}
        if name == "subscribe":
            channel = payload.get("channel") or {}
            if not isinstance(channel, dict):
                raise LedgerError(400, "bad_channel", "Channel is not usable.")
            reject_forbidden_fields(channel)
            return ledger.notify_me(
                actor,
                payload.get("event_type"),
                channel.get("type"),
                channel_target=channel.get("url") or channel.get("target"),
                filt=payload.get("filter"),
                ttl_seconds=payload.get("ttl_seconds"),
            )
        if name == "unsubscribe":
            return ledger.unsubscribe(actor, int(groups[0]))
        if name == "board":
            since = _query_int(query, "since_cursor", 0)
            return ledger.list_board(actor, since_cursor=since)
        if name == "inbox":
            return ledger.my_inbox(actor, ack=False)
        if name == "ack_inbox":
            return ledger.ack_inbox(actor, payload.get("cursor"))
        if name == "seal":
            _task, repo, number = _pr_body(payload, require_task=False)
            return ledger.seal(actor, repo, number, self.github_factory())
        if name == "seal_status":
            repo = (query.get("repo") or [None])[0]
            number = (query.get("number") or [None])[0]
            if repo is None or number is None or not str(number).isdigit():
                raise LedgerError(400, "bad_pr", "PR must look like owner/repo#123.")
            return ledger.seal_status(repo, int(number))
        if name == "followups":
            return ledger.followups(actor)
        if name == "ack_followup":
            return ledger.ack_followup(actor, int(groups[0]))
        if name == "mint":
            token = ledger.mint_agent(
                actor,
                payload.get("id"),
                payload.get("kind"),
                payload.get("role"),
                model=payload.get("model"),
            )
            body = {"id": payload.get("id"), "token": token}
            if payload.get("model"):
                body["model"] = payload.get("model")
            return body
        if name == "rotate":
            token = ledger.rotate_token(actor, groups[0])
            return {"id": groups[0], "token": token}
        if name == "disable":
            return ledger.disable_agent(actor, groups[0])
        raise LedgerError(404, "not_found", "No such route.")


def serve(db_path, host: str = "127.0.0.1", port: int = 8741, github_factory=None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind outside localhost.")
    api = Api(db_path, github_factory=github_factory)
    handler = _handler(api)
    server = ThreadingHTTPServer((host, port), handler)
    return server


def _handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._respond()

        def do_POST(self):
            self._respond()

        def do_DELETE(self):
            self._respond()

        def _respond(self):
            port = self.server.server_address[1]
            if not host_allowed(self.headers.get("Host"), port):
                payload = _dump({"error": "bad_host", "message": "Host rejected."})
                self.send_response(421)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            length = _content_length(self.headers.get("Content-Length"))
            if length is None:
                payload = _dump({"error": "bad_length", "message": "Content-Length is not usable."})
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if length > _MAX_BODY:
                body = b""
                status, payload, content_type = 400, _dump({"error": "too_large", "message": "Body is too large."}), "application/json"
            else:
                body = self.rfile.read(length) if length else b""
                status, payload, content_type = api.handle(self.command, self.path, self.headers, body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    return Handler


def _content_length(value) -> int | None:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if not text.isdigit():
        return None
    return int(text)


def host_allowed(value, port: int) -> bool:
    """Local server only answers its own host. A foreign Host is a rebinding attempt."""
    if not isinstance(value, str) or not isinstance(port, int):
        return False
    host = value.strip().lower()
    if not host or any(ch in host for ch in " /\\@\t"):
        return False
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if port in (80, 443):
        allowed.update({"127.0.0.1", "localhost", "::1", "[::1]"})
    return host in allowed


def _bearer(headers) -> str:
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    if not isinstance(raw, str) or not raw.lower().startswith("bearer "):
        return ""
    return raw.split(" ", 1)[1].strip()


def _dump(payload) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _query_int(query, key, default):
    raw = (query.get(key) or [None])[0]
    if raw is None:
        return default
    if not str(raw).isdigit():
        raise LedgerError(400, "bad_cursor", "Event cursor is not usable.")
    return int(raw)


def _pr_body(payload, require_task: bool):
    pr = payload.get("pr")
    if not isinstance(pr, dict):
        raise LedgerError(400, "bad_pr", "PR must look like owner/repo#123.")
    reject_forbidden_fields(pr)
    repo = pr.get("repo")
    number = pr.get("number")
    task_id = payload.get("task_id")
    if require_task and not task_id:
        raise LedgerError(400, "bad_task", "Task id is not usable.")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    return task_id, repo, number
