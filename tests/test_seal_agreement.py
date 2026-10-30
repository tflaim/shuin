"""The Board and merge closing must give the same answer about a Seal."""

import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card, run_log
from tests.test_gate import FakeGH, _check, _head


def _refused_seal_with_verifier_pass(env):
    task = open_card(env, head=SHA_A)
    run_log(env, SHA_A, 128)
    env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("failure"),)),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"
    return task


def test_board_does_not_stamp_a_card_whose_seal_was_refused_at_this_head(env):
    """The Board said Stamped while merge refused the same Seal."""
    task = _refused_seal_with_verifier_pass(env)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")

    with pytest.raises(LedgerError) as refused:
        env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert refused.value.code == "seal_refused"

    card = env.ledger.seal_status("acme/app", 128)
    assert card["seal"] != "stamped"
    assert card["voice"] != "Stamped."


def test_open_card_with_a_refused_receipt_names_the_next_command(env):
    _refused_seal_with_verifier_pass(env)
    card = env.ledger.seal_status("acme/app", 128)
    assert card["seal"] != "stamped"
    assert card["voice"] == "Seal is missing. shuin seal --pr acme/app#128"


def _pr(state="open", head=SHA_A, synced=1):
    return {"repo": "acme/app", "number": 128, "state": state, "head_sha": head, "last_synced_at": synced}


def _receipt(classification="success", head=SHA_A, reread=SHA_A):
    return {"classification": classification, "head_sha": head, "head_sha_reread": reread}


def test_verdict_is_one_pure_answer():
    from shuin.seal import verdict
    from tests.conftest import SHA_B

    assert verdict(_pr(), _receipt(), "pass").holds
    assert verdict(_pr(state="merged"), _receipt(), "pass").holds
    assert verdict(_pr(), _receipt(), "missing").reason == "missing"
    assert verdict(_pr(), _receipt("failure"), "pass").reason == "missing"
    assert verdict(_pr(state="closed"), _receipt(), "pass").reason == "closed"
    assert verdict(_pr(state="merged"), _receipt("failure"), "pass").reason == "merged"
    moved = verdict(_pr(head=SHA_B), _receipt(), "pass")
    assert (moved.reason, moved.code) == ("head_moved", "stale_head")
    assert verdict(_pr(synced=None), None, "pass").reason == "unsynced"


def test_gate_and_seal_do_not_import_the_ledger():
    import ast
    import importlib.util
    from pathlib import Path

    for name in ("shuin.gate", "shuin.seal"):
        source = Path(importlib.util.find_spec(name).origin).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        assert not any(mod == "shuin.ledger" or mod.startswith("shuin.ledger.") for mod in imported), name


def test_board_does_not_stamp_a_verifier_pass_with_no_seal_receipt(env):
    """No receipt: task done on the merged PR refuses, so the Board must not stamp."""
    task = open_card(env, head=SHA_A)
    run_log(env, SHA_A, 128)
    env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")

    with pytest.raises(LedgerError) as refused:
        env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert refused.value.code == "seal_refused"

    card = env.ledger.seal_status("acme/app", 128)
    assert card["seal"] != "stamped"
    assert card["voice"] == f"PR is already merged. shuin task done {task['id']} --merged-externally --reason landed"


def test_board_does_not_stamp_a_merged_pr_sealed_at_an_older_head(env):
    """A success receipt for an older head does not hold once the PR merged at a new one."""
    from tests.conftest import SHA_B

    task = open_card(env, head=SHA_B)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, ?, ?, 'success', '[]', 'pass', '{}', 1)""",
        (pr["id"], task["id"], SHA_A, SHA_A),
    )
    run_log(env, SHA_B, 128)
    env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_B, "pass")
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")

    with pytest.raises(LedgerError) as refused:
        env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert refused.value.code == "seal_refused"

    card = env.ledger.seal_status("acme/app", 128)
    assert card["seal"] != "stamped"
    assert card["voice"] == f"PR is already merged. shuin task done {task['id']} --merged-externally --reason landed"


def _stamp_at_head(env):
    task = open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    return task, queued


def _success_receipt(env, task):
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, ?, ?, 'success', '[]', 'pass', '{}', 1)""",
        (pr["id"], task["id"], SHA_A, SHA_A),
    )


def test_stamp_reply_agrees_with_seal_status_before_a_seal(env):
    _, queued = _stamp_at_head(env)
    stamped = env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    card = env.ledger.seal_status("acme/app", 128)
    assert stamped["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    assert card["voice"] == "Seal is missing. shuin seal --pr acme/app#128"
    assert card["voice"].endswith(stamped["voice"].split("Not sealed yet: ", 1)[1])


def test_stamp_reply_agrees_with_seal_status_when_the_seal_holds(env):
    task, queued = _stamp_at_head(env)
    _success_receipt(env, task)
    stamped = env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    card = env.ledger.seal_status("acme/app", 128)
    assert stamped["voice"] == "Stamped."
    assert card["seal"] == "stamped"
    assert card["voice"] == "Stamped."


def test_mcp_and_http_stamp_keep_their_shape_and_use_the_new_reply(env):
    import http.client
    import json
    import threading

    from shuin.httpapi import serve
    from shuin.mcp import call_tool

    _, first = _stamp_at_head(env)
    # MCP and HTTP open their own ledger on the wall clock; the fixture clock is in 2023.
    env.ledger.conn.execute(
        "UPDATE verification_queue SET lease_expires_at=? WHERE id=?",
        (2_000_000_000, first["queue_id"]),
    )
    text, is_error = call_tool("stamp", {"queue_id": first["queue_id"]}, db_path=env.db, token=env.tokens["verifier-1"])
    assert is_error is False, text
    body = json.loads(text)
    assert set(body) >= {"queue_id", "result", "voice", "run_id", "output_sha256", "run_at"}
    assert body["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"

    second = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.conn.execute(
        "UPDATE verification_queue SET lease_expires_at=? WHERE id=?",
        (2_000_000_000, second["queue_id"]),
    )
    server = serve(env.db, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            f"/v1/verification_queue/{second['queue_id']}/stamp",
            body="{}",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Authorization": f"Bearer {env.tokens['verifier-1']}",
                "Content-Type": "application/json",
            },
        )
        reply = conn.getresponse()
        payload = json.loads(reply.read())
        assert reply.status == 200, payload
        assert set(payload) >= {"queue_id", "result", "voice"}
        assert payload["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    finally:
        server.shutdown()
        server.server_close()


def test_release_pass_reply_agrees_with_seal_status_before_a_seal(env):
    _, queued = _stamp_at_head(env)
    released = env.ledger.release_slot(env.actors["verifier-1"], queued["queue_id"], "pass")
    card = env.ledger.seal_status("acme/app", 128)
    assert released["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    assert card["voice"] == "Seal is missing. shuin seal --pr acme/app#128"


def test_release_pass_reply_agrees_with_seal_status_when_the_seal_holds(env):
    task, queued = _stamp_at_head(env)
    _success_receipt(env, task)
    released = env.ledger.release_slot(env.actors["verifier-1"], queued["queue_id"], "pass")
    card = env.ledger.seal_status("acme/app", 128)
    assert released["voice"] == "Stamped."
    assert card["seal"] == "stamped"
    assert card["voice"] == "Stamped."


def test_release_fail_still_frees_the_slot(env):
    _, queued = _stamp_at_head(env)
    released = env.ledger.release_slot(env.actors["verifier-1"], queued["queue_id"], "fail")
    assert released["voice"] == "Slot free."


def test_page_voice_does_not_stamp_an_unsealed_board(env):
    _, queued = _stamp_at_head(env)
    env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert env.ledger.snapshot()["voice"] != "Stamped."


def test_seal_readout_cannot_stamp_without_asking_the_seal():
    from shuin.voice import seal_readout

    with pytest.raises(TypeError):
        seal_readout(blocked=False, seal_broken=False, stale_verification=False, verification_state="passed")
