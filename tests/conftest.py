from types import SimpleNamespace

import pytest

from shuin.ledger import open_ledger
from shuin.types import PrSnapshot

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def clock():
    return {"t": 1_700_000_000}


@pytest.fixture
def env(tmp_path, clock):
    ledger = open_ledger(tmp_path / "ledger.db", clock=lambda: clock["t"])
    tokens = {"admin": ledger.mint_agent(None, "admin", "human", "owner")}
    owner = ledger.authenticate(tokens["admin"])
    for agent_id, kind, role in (
        ("eng-lead", "lead", "lead"),
        ("worker-1", "cursor", "worker"),
        ("verifier-1", "verifier", "verifier"),
    ):
        tokens[agent_id] = ledger.mint_agent(owner, agent_id, kind, role)
    actors = {name: ledger.authenticate(token) for name, token in tokens.items()}
    box = SimpleNamespace(ledger=ledger, tokens=tokens, actors=actors, clock=clock, db=tmp_path / "ledger.db")
    yield box
    ledger.close()


def snap(number=128, node="PR_node_128", branch="feat/sheet", head=SHA_A, state="open", review=None, ci="pending", files=(), patch_id=None, tree_sha=None):
    return PrSnapshot(
        repo="acme/app",
        number=number,
        node_id=node,
        branch=branch,
        base_branch="main",
        head_sha=head,
        state=state,
        review_state=review,
        ci_state=ci,
        files=tuple(files),
        patch_id=patch_id,
        tree_sha=tree_sha,
    )


def run_log(env, sha=SHA_A, number=128):
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        number,
        "test_log",
        f"file://bench/{number}.log",
        sha,
        result="pass",
    )


def open_card(env, number=128, node="PR_node_128", head=None, title="Fix the login sheet"):
    task = env.ledger.add_task(env.actors["eng-lead"], title, repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], task["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], task["id"], "acme/app", number, node_id=node)
    if head:
        env.ledger.apply_github_snapshot(snap(number=number, node=node, head=head))
    return task
