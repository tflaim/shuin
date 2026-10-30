"""A sample night. Labeled as such on the board. Not a live GitHub sync."""

from shuin.types import CheckItem, ChecksRead, HeadRead, PrSnapshot

OLD = "4f1a0c2e8b91d673aa10e5c44d2f8a9076bb11c0"
NEW = "9c2e71ab55d0438e1f6a90c27d4b18e0aa7712d4"
KEEP = "c31aa9076bb11c04f1a0c2e8b91d673aa10e5c44"
MERGED = "a90c27d4b18e0aa7712d49c2e71ab55d0438e1f6"
NIGHTLY = "7712d49c2e71ab55d0438e1f6a90c27d4b18e0aa"


class _Stable:
    def __init__(self, head: HeadRead, checks):
        self.head = head
        self.checks = checks

    def read_head(self, repo, number):
        return self.head

    def read_checks(self, repo, base, sha):
        return ChecksRead(checks=tuple(self.checks), error=None)


class _Moving:
    def __init__(self, first: HeadRead, second: HeadRead):
        self.reads = [first, second]
        self.n = 0

    def read_head(self, repo, number):
        head = self.reads[min(self.n, len(self.reads) - 1)]
        self.n += 1
        return head

    def read_checks(self, repo, base, sha):
        return ChecksRead(
            checks=(CheckItem("ci", 10, "success", "check_run"),),
            error=None,
        )


def seed(ledger) -> dict:
    tokens = {}
    tokens["admin"] = ledger.mint_agent(None, "admin", "human", "owner")
    owner = ledger.authenticate(tokens["admin"])

    def mint(agent_id, kind, role):
        tokens[agent_id] = ledger.mint_agent(owner, agent_id, kind, role)
        return ledger.authenticate(tokens[agent_id])

    lead = mint("eng-lead", "lead", "lead")
    cursor = mint("worker-1", "cursor", "worker")
    claude = mint("claude-1", "claude_code", "worker")
    codex = mint("codex-1", "codex", "worker")
    hermes = mint("hermes-1", "hermes", "worker")
    verifier = mint("verifier-1", "verifier", "verifier")

    parent = ledger.add_task(lead, "Cut the settings prompt down to one page", repo="acme/app")
    ledger.claim_task(lead, parent["id"], assignee="worker-1")
    ledger.link_pr(cursor, parent["id"], "acme/app", 120, node_id="PR_kwDOShuin120")
    ledger.apply_github_snapshot(
        PrSnapshot(
            "acme/app",
            120,
            "PR_kwDOShuin120",
            "settings-page",
            "main",
            MERGED,
            "open",
            "approved",
            "passing",
            ("ios/Login/Sheet.swift",),
        )
    )
    ledger.enqueue(cursor, "acme/app", 120, MERGED)
    slot = ledger.claim_slot(verifier)
    ledger.add_evidence(verifier, "acme/app", 120, "test_log", "file://bench/settings-page.log", MERGED, result="pass")
    ledger.add_evidence(verifier, "acme/app", 120, "screenshot", "file://bench/settings-page.png", MERGED, result="pass")
    ledger.stamp(verifier, slot["queue_id"])
    ledger.seal(
        lead,
        "acme/app",
        120,
        _Stable(
            HeadRead(MERGED, "settings-page", "main", "PR_kwDOShuin120", "open"),
            (CheckItem("ci", 1, "success", "check_run"),),
        ),
    )
    ledger.apply_github_snapshot(
        PrSnapshot(
            "acme/app",
            120,
            "PR_kwDOShuin120",
            "settings-page",
            "main",
            MERGED,
            "merged",
            "approved",
            "passing",
            ("ios/Login/Sheet.swift",),
        )
    )
    ledger.complete_task(lead, parent["id"])
    ledger.post_update(cursor, parent["id"], "progress", "Test device took the single prompt and stayed signed in.")

    child = ledger.add_task(
        lead,
        "Rebase the receipt check onto the login sheet",
        repo="acme/app",
        depends_on=parent["id"],
    )
    ledger.claim_task(lead, child["id"], assignee="claude-1")
    ledger.link_pr(claude, child["id"], "acme/app", 128, node_id="PR_kwDOShuin128")
    ledger.apply_github_snapshot(
        PrSnapshot(
            "acme/app",
            128,
            "PR_kwDOShuin128",
            "receipt-on-sheet",
            "settings-page",
            NEW,
            "open",
            "changes_requested",
            "failing",
            ("ios/Login/Sheet.swift", "ios/Ledger/Receipt.swift"),
        )
    )
    ledger.add_evidence(verifier, "acme/app", 128, "test_log", "file://bench/receipt-old.log", OLD, result="fail")
    ledger.request_review(
        claude,
        "acme/app",
        128,
        "Replayed the signing step twice. The profile is still expired.",
    )
    ledger.seal(
        lead,
        "acme/app",
        128,
        _Moving(
            HeadRead(OLD, "receipt-on-sheet", "settings-page", "PR_kwDOShuin128", "open"),
            HeadRead(NEW, "receipt-on-sheet", "settings-page", "PR_kwDOShuin128", "open"),
        ),
    )

    blocked = ledger.add_task(lead, "Match the test device ID in the spec", repo="acme/app")
    ledger.claim_task(lead, blocked["id"], assignee="codex-1")
    ledger.set_blocked(codex, blocked["id"], "needs_human")
    ledger.post_update(codex, blocked["id"], "question", "The spec device ID is the old test device. The one on the desk is different.")

    film = ledger.add_task(lead, "Film the login sheet on the test device", repo="acme/app")
    ledger.claim_task(lead, film["id"], assignee="hermes-1")
    ledger.link_pr(hermes, film["id"], "acme/app", 130, node_id="PR_kwDOShuin130")
    ledger.apply_github_snapshot(
        PrSnapshot(
            "acme/app",
            130,
            "PR_kwDOShuin130",
            "sheet-film",
            "main",
            KEEP,
            "open",
            None,
            "pending",
            ("ios/Login/Film.swift",),
        )
    )
    nightly = ledger.add_task(lead, "Stamp the nightly build of the login sheet", repo="acme/app")
    ledger.claim_task(lead, nightly["id"], assignee="worker-1")
    ledger.link_pr(cursor, nightly["id"], "acme/app", 131, node_id="PR_kwDOShuin131")
    ledger.apply_github_snapshot(
        PrSnapshot(
            "acme/app",
            131,
            "PR_kwDOShuin131",
            "nightly-sheet",
            "main",
            NIGHTLY,
            "open",
            None,
            "passing",
            ("ios/Login/Sheet.swift", "ios/Login/Nightly.swift"),
        )
    )
    ledger.enqueue(cursor, "acme/app", 131, NIGHTLY)
    ledger.claim_slot(verifier)
    ledger.enqueue(hermes, "acme/app", 130, KEEP)

    ledger.post_update(lead, child["id"], "note", "Head moved after the log. Treat the old film as stale.", on_behalf_of="claude-1")
    ledger.set_setting("sample", "1")
    return tokens
