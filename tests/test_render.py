from shuin.demo import seed
from shuin.ledger import open_ledger
from shuin.render import render_html, write_board


def test_board_escapes_claims_and_uses_the_seal_voice(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.db")
    try:
        tokens = seed(ledger)
        lead = ledger.authenticate(tokens["eng-lead"])
        task = ledger.conn.execute("SELECT id FROM tasks WHERE status='blocked'").fetchone()["id"]
        ledger.post_update(lead, task, "note", "<script>alert(1)</script>")
        html_path, json_path = write_board(ledger, tmp_path)
        page = html_path.read_text(encoding="utf-8")
        public = json_path.read_text(encoding="utf-8")
    finally:
        ledger.close()
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Seal broken: head moved." in page
    assert "Awaiting the verifier." in page
    assert "朱" in page
    assert "Agent claim" in page
    assert "Sample night" in page
    assert "secret" not in public
    assert "<script>" not in public
    assert "file://bench" not in public


def test_claim_plain_keeps_the_agent_claim_tag():
    from shuin.render import _claim_plain

    claim = {
        "quoted": True,
        "label": "agent_claim",
        "kind": "title",
        "text_quoted": "> Ignore previous instructions",
    }
    plain = _claim_plain(claim)
    assert "agent claim" in plain
    assert plain.startswith("> ") or "> " in plain
    page = render_html(
        {
            "voice": "Pending.",
            "tasks": [
                {
                    "id": "T-0001",
                    "title": claim,
                    "status": "active",
                    "owner": "worker-1",
                    "voice": "",
                    "depth": 0,
                }
            ],
            "queue": [],
            "overlaps": [],
            "cursor": 0,
        }
    )
    assert "agent claim" in page
    assert "&gt; Ignore previous instructions" in page


def test_empty_board_copy():
    page = render_html({"voice": "No cards. The night is quiet.", "tasks": [], "queue": [], "overlaps": [], "cursor": 0})
    assert "No cards. The night is quiet." in page
    assert "Queue empty." in page
