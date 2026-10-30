"""Issues 16, 20, 21, and 22."""

import json
import subprocess
from pathlib import Path

import pytest

from shuin import __version__
from shuin.cli import _format_board, main
from shuin.errors import LedgerError
from shuin.render import render_html, write_board
from shuin.types import CheckItem, ChecksRead, HeadRead
from tests.conftest import SHA_A, SHA_B, open_card, run_log, snap


class _Pull:
    def __init__(self, head, node=None):
        self.head = head
        self.node = node

    def fetch_pull(self, repo, number):
        node = self.node or f"PR_node_{number}"
        return snap(number=number, node=node, head=self.head)


class _Gate:
    def __init__(self, heads, checks=()):
        self.heads = list(heads)
        self.checks = checks
        self.n = 0

    def read_head(self, repo, number):
        head = self.heads[min(self.n, len(self.heads) - 1)]
        self.n += 1
        return head

    def read_checks(self, repo, base, sha):
        return ChecksRead(checks=tuple(self.checks))


def _head(sha, state="open"):
    return HeadRead(sha, "feat/sheet", "main", "PR_node_128", state)


def _check(state="success"):
    return CheckItem("ci", 1, state, "check_run")


def test_enqueue_records_a_pending_head_and_poll_confirms_it(env, monkeypatch, capsys):
    open_card(env)
    with pytest.raises(LedgerError) as raised:
        env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert raised.value.code == "unknown_head"
    assert "head moved" not in raised.value.message
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] is None
    assert pr["pending_head"] == SHA_A

    confirmed = env.ledger.poll_pr(_Pull(SHA_A), "acme/app", 128)
    assert confirmed["ok"] is True
    assert confirmed["pending_confirmed"] is True
    assert confirmed["head_moved"] is False
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] == SHA_A
    assert pr["pending_head"] is None
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert queued["state"] == "queued"
    status = env.ledger.seal_status("acme/app", 128)
    assert "head moved" not in status["voice"]

    fresh = open_card(env, number=129, node="PR_node_129")
    monkeypatch.setattr("shuin.cli.GhClient", lambda: _Pull(SHA_A))
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "queue",
            "enqueue",
            "--pr",
            "acme/app#129",
            "--commit-sha",
            SHA_A,
            "--poll",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["state"] == "queued"
    assert body["pr"] == 129
    pr = env.ledger.pr_by_repo_number("acme/app", 129)
    assert pr["head_sha"] == SHA_A
    assert pr["pending_head"] is None
    assert pr["task_id"] == fresh["id"]


def test_review_and_evidence_record_a_pending_head(env):
    open_card(env)
    env.ledger.request_review(
        env.actors["worker-1"],
        "acme/app",
        128,
        "nothing",
        commit_sha=SHA_A,
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] is None
    assert pr["pending_head"] == SHA_A
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "build_log",
        "file://bench/pending.log",
        SHA_B,
        result="pass",
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["pending_head"] == SHA_B
    assert pr["head_sha"] is None

    known = open_card(env, number=130, node="PR_node_130", head=SHA_A)
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        130,
        "build_log",
        "file://bench/known.log",
        SHA_B,
        result="info",
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 130)
    assert pr["head_sha"] == SHA_A
    assert pr["pending_head"] is None
    assert pr["task_id"] == known["id"]


def test_poll_that_disagrees_is_a_real_move(env):
    open_card(env)
    with pytest.raises(LedgerError):
        env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    with pytest.raises(LedgerError) as raised:
        env.ledger.enqueue(
            env.actors["worker-1"],
            "acme/app",
            128,
            SHA_A,
            poll=True,
            github=_Pull(SHA_B),
        )
    assert raised.value.code == "stale_head"
    assert raised.value.message == "Seal broken: head moved."
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] == SHA_B
    assert pr["pending_head"] is None


def test_equal_heads_do_not_say_the_head_moved(env):
    task = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET head_sha=? WHERE number=128", (SHA_A.upper(),))
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    stamped = env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    assert stamped["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _Gate([_head(SHA_A), _head(SHA_A.upper())], checks=(_check(),)),
    )
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert "head moved" not in body["voice"]
    status = env.ledger.seal_status("acme/app", 128)
    assert status["stale"] is False
    assert "head moved" not in status["voice"]
    assert status["id"] == task["id"]

    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, ?, ?, 'stale', '[]', 'missing', '{}', 1)""",
        (pr["id"], task["id"], SHA_A.upper(), SHA_A),
    )
    quiet = env.ledger.seal_status("acme/app", 128)
    assert quiet["stale"] is False
    assert "head moved" not in quiet["voice"]


def test_token_and_role_errors_name_what_is_missing(env, capsys):
    token_dir = env.db.parent
    (token_dir / "owner.token").write_text("secret\n", encoding="utf-8")
    (token_dir / "worker.token").write_text("secret\n", encoding="utf-8")
    code = main(["--db", str(env.db), "--json", "task", "add", "--title", "Needs a token"])
    assert code == 1
    message = json.loads(capsys.readouterr().out)["message"]
    assert "SHUIN_TOKEN" in message
    assert "--token" in message
    assert "owner.token" in message
    assert "worker.token" in message
    assert "SHUIN_DB" in message

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "agent",
            "mint",
            "--id",
            "nope",
            "--kind",
            "cursor",
            "--role",
            "worker",
        ]
    )
    assert code == 1
    role = json.loads(capsys.readouterr().out)["message"]
    assert "agent mint" in role
    assert "owner" in role


def test_model_is_stored_per_task_run_and_shown(env, tmp_path):
    token = env.ledger.mint_agent(env.actors["admin"], "codex-2", "codex", "worker", model="gpt-5.4")
    worker = env.ledger.authenticate(token)
    task = env.ledger.add_task(worker, "Show the model", repo="acme/app")
    claimed = env.ledger.claim_task(worker, task["id"], model="composer-2.5")
    assert claimed["model"] == "composer-2.5"
    again = env.ledger.claim_task(worker, task["id"])
    assert again["model"] == "gpt-5.4"
    board = env.ledger.list_board(env.actors["eng-lead"])
    card = next(item for item in board["tasks"] if item["id"] == task["id"])
    assert [run["model"] for run in card["runs"]] == ["composer-2.5", "gpt-5.4"]
    text = _format_board(board)
    assert "composer-2.5" in text
    assert "gpt-5.4" in text
    assert "run 1" in text
    assert "run 2" in text
    page = render_html(board)
    assert "composer-2.5" in page
    assert "gpt-5.4" in page
    _html_path, json_path = write_board(env.ledger, tmp_path)
    public = json.loads(json_path.read_text(encoding="utf-8"))
    stored = next(item for item in public["tasks"] if item["id"] == task["id"])
    assert [run["model"] for run in stored["runs"]] == ["composer-2.5", "gpt-5.4"]


def _version_checkout(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    package = path / "shuin"
    package.mkdir()
    (package / "__init__.py").write_text(path.name + "\n", encoding="utf-8")
    (package / "cli.py").write_text("# stand-in\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(path)])
    subprocess.check_call(["git", "-C", str(path), "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "-C", str(path), "config", "user.name", "Test"])
    subprocess.check_call(["git", "-C", str(path), "config", "commit.gpgsign", "false"])
    subprocess.check_call(["git", "-C", str(path), "add", "shuin"])
    subprocess.check_call(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", "commit", "-q", "-m", path.name]
    )
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_version_prints_the_sha_and_survives_without_git(capsys, monkeypatch, tmp_path):
    package_sha = _version_checkout(tmp_path / "checkout")
    other_sha = _version_checkout(tmp_path / "cwd-repo")
    monkeypatch.chdir(tmp_path / "cwd-repo")
    monkeypatch.setattr("shuin.cli.__file__", str(tmp_path / "checkout" / "shuin" / "cli.py"))

    code = main(["--version"])
    assert code == 0
    text = capsys.readouterr().out.strip()
    assert text == f"shuin {__version__} {package_sha}"
    assert other_sha not in text

    nested = tmp_path / "nested" / "vendor" / "shuin"
    nested.mkdir(parents=True)
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "cli.py").write_text("", encoding="utf-8")
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(tmp_path / "nested")])
    subprocess.check_call(["git", "-C", str(tmp_path / "nested"), "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "-C", str(tmp_path / "nested"), "config", "user.name", "Test"])
    subprocess.check_call(["git", "-C", str(tmp_path / "nested"), "config", "commit.gpgsign", "false"])
    subprocess.check_call(["git", "-C", str(tmp_path / "nested"), "add", "vendor"])
    subprocess.check_call(
        ["git", "-C", str(tmp_path / "nested"), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "nested"]
    )
    monkeypatch.setattr("shuin.cli.__file__", str(nested / "cli.py"))
    code = main(["--version"])
    assert code == 0
    assert capsys.readouterr().out.strip() == f"shuin {__version__}"

    loose = tmp_path / "loose" / "shuin"
    loose.mkdir(parents=True)
    (loose / "__init__.py").write_text("", encoding="utf-8")
    (loose / "cli.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("shuin.cli.__file__", str(loose / "cli.py"))
    code = main(["--version"])
    assert code == 0
    assert capsys.readouterr().out.strip() == f"shuin {__version__}"

    def boom(*_args, **_kwargs):
        raise OSError("git missing")

    monkeypatch.setattr("shuin.cli.__file__", str(tmp_path / "checkout" / "shuin" / "cli.py"))
    monkeypatch.setattr("shuin.cli.subprocess.run", boom)
    code = main(["--version"])
    assert code == 0
    missing = capsys.readouterr().out
    assert missing.strip() == f"shuin {__version__}"
    assert package_sha not in missing
    assert "Traceback" not in missing
