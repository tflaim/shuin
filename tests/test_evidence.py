import hashlib

import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card


def test_evidence_paths_stay_in_the_evidence_directory(env):
    open_card(env, head=SHA_A)
    root = env.db.parent
    secret = root / "secret.txt"
    secret.write_bytes(b"topsecret")
    digest = hashlib.sha256(b"topsecret").hexdigest()
    with pytest.raises(LedgerError) as escaped:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            128,
            "build_log",
            str(secret),
            SHA_A,
            sha256=digest,
        )
    assert escaped.value.code == "bad_uri"
    assert escaped.value.message == "URI is not usable."
    assert "match" not in escaped.value.message.lower()
    assert "exist" not in escaped.value.message.lower()

    evidence = root / "evidence"
    evidence.mkdir()
    present = evidence / "present.log"
    present.write_bytes(b"topsecret")
    with pytest.raises(LedgerError) as gone:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            128,
            "build_log",
            "missing.log",
            SHA_A,
            sha256=digest,
        )
    with pytest.raises(LedgerError) as mismatched:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            128,
            "build_log",
            "present.log",
            SHA_A,
            sha256="0" * 64,
        )
    assert gone.value.code == mismatched.value.code == "bad_uri"
    assert gone.value.message == mismatched.value.message == "URI is not usable."

    body = env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "build_log",
        "present.log",
        SHA_A,
        sha256=digest,
    )
    assert body["evidence_id"]

    link = evidence / "escape.log"
    link.symlink_to(secret)
    with pytest.raises(LedgerError) as linked:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            128,
            "build_log",
            "escape.log",
            SHA_A,
            sha256=digest,
        )
    assert linked.value.message == "URI is not usable."
    stored = env.ledger.conn.execute("SELECT uri FROM evidence").fetchall()
    assert all("secret.txt" not in row["uri"] for row in stored)
