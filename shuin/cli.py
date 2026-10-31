"""Shuin CLI. Human reads need no token. Agent writes use SHUIN_TOKEN."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from shuin import __version__
from shuin.db import default_db_path, token_required_message
from shuin.errors import LedgerError
from shuin.github import GhClient
from shuin.ledger import UPDATE_KINDS, open_ledger
from shuin.render import format_dependencies, write_board
from shuin.sanitize import TRIED_FAILED_MAX
from shuin.voice import format_left

_GLOBALS = {"--json", "--no-color"}
_PR_HELP = "owner/repo#N, or a bare number when the board has one repo."


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(version_text())
        return 0
    split = argv.index("--") if "--" in argv else len(argv)
    options, rest = _peel(argv[:split])
    rest.extend(argv[split:])
    rest, job_command = _split_job_command(rest)
    parser = argparse.ArgumentParser(
        prog="shuin",
        description="Local ledger. GitHub stays the source of truth.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create the ledger and the owner token.")
    p = sub.add_parser("demo", help="Seed a sample night so the board is not empty.")
    p.add_argument("--force", action="store_true")

    agent = sub.add_parser("agent", help="Mint, rotate, or disable a token.")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)
    p = agent_sub.add_parser("mint")
    p.add_argument("--id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--model", help="Model stored on this agent and copied onto each task run.")
    p = agent_sub.add_parser("rotate")
    p.add_argument("--id", required=True)
    p = agent_sub.add_parser("disable")
    p.add_argument("--id", required=True)

    task = sub.add_parser("task", help="Cards.")
    task_sub = task.add_subparsers(dest="task_cmd", required=True)
    p = task_sub.add_parser("add")
    p.add_argument("--title", required=True)
    p.add_argument("--repo")
    p.add_argument("--depends-on", action="append", help="Task id this card waits on. Repeat for more than one.")
    p.add_argument("--depends-on-pr", action="append", help=_PR_HELP + " The card waits until that PR merges.")
    p.add_argument("--idempotency-key")
    p = task_sub.add_parser(
        "depend",
        help="Wait on a task or a PR merge, or remove that wait.",
        description="Add a dependency. Claim stays refused while any dependency is open. A cycle is refused. --remove deletes one. Owner or lead.",
    )
    p.add_argument("task_id")
    p.add_argument("--depends-on", "--on", action="append", help="Task id this card waits on.")
    p.add_argument("--depends-on-pr", "--pr", action="append", help=_PR_HELP)
    p.add_argument(
        "--remove",
        action="store_true",
        help="Delete the named dependency. Owner or lead. The card is released when nothing open remains.",
    )
    p = task_sub.add_parser("ready", help="List tasks that can be claimed.")
    p = task_sub.add_parser("claim")
    p.add_argument("task_id")
    p.add_argument("--assignee")
    p.add_argument(
        "--model",
        help="Model for this task run. Defaults to the agent's model. Later block, review, and verification rows by this agent keep it.",
    )
    p = task_sub.add_parser("block")
    p.add_argument("task_id")
    p.add_argument("--reason", required=True)
    p = task_sub.add_parser("unblock")
    p.add_argument("task_id")
    p = task_sub.add_parser("drop")
    p.add_argument("task_id")
    p = task_sub.add_parser("done")
    p.add_argument("task_id")
    p.add_argument(
        "--merged-externally",
        action="store_true",
        help="Lead or owner. The linked PR is already merged. Close the card as merge and advance cards waiting on it.",
    )
    p.add_argument("--reason", help="Why this card is closing. Required with --merged-externally.")

    pr = sub.add_parser("pr", help="Bind a PR to a card.")
    pr_sub = pr.add_subparsers(dest="pr_cmd", required=True)
    p = pr_sub.add_parser("link")
    p.add_argument("task_id")
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p.add_argument("--node-id")

    p = sub.add_parser("update", help="Post an agent claim.")
    p.add_argument("task_id")
    p.add_argument("--kind", required=True, help=", ".join(UPDATE_KINDS))
    p.add_argument("--body", required=True)
    p.add_argument("--on-behalf-of")

    review = sub.add_parser(
        "review",
        help="Request review. A worker may only do this on a card they own.",
        description="Request review. A worker may only do this on a card they own.",
    )
    review_sub = review.add_subparsers(dest="review_cmd", required=True)
    p = review_sub.add_parser("request")
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p.add_argument(
        "--tried",
        required=False,
        default=None,
        help=f"Tried-and-failed line. One line, at most {TRIED_FAILED_MAX} characters. 'nothing' is allowed.",
    )
    p.add_argument("--commit-sha", help="Claimed head. Recorded as a pending head until poll confirms it.")
    p.add_argument("--on-behalf-of")

    p = sub.add_parser(
        "evidence",
        help="Attach evidence to a commit. A worker may only do this on a card they own.",
        description="Attach evidence to a commit. A worker may only do this on a card they own.",
    )
    ev = p.add_subparsers(dest="evidence_cmd", required=True)
    p = ev.add_parser("add")
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p.add_argument("--kind", required=True)
    p.add_argument("--uri", required=True)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--result")
    p.add_argument("--on-behalf-of")

    queue = sub.add_parser(
        "queue",
        help="Verification queue (FIFO). A tokenless read does not write.",
        description="Verification queue (FIFO). Without a token this only reads. With a token, rows for merged PRs are cancelled.",
    )
    queue_sub = queue.add_subparsers(dest="queue_cmd")
    p = queue_sub.add_parser("enqueue")
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p.add_argument("--commit-sha", required=True)
    p.add_argument(
        "--require-run",
        action="store_true",
        help="This row is a fresh run. A matching patch or tree does not carry the pass.",
    )
    p.add_argument(
        "--poll",
        action="store_true",
        help="Refresh this PR from GitHub before enqueueing.",
    )
    p = queue_sub.add_parser(
        "claim",
        help="Take the next slot. --wait blocks until a row is free.",
        description="Take the next slot whose commit is the current head. --wait blocks while the queue is empty, the slot is held, or the head is stale or unknown. Release, cancel, and a head change wake it. It does not poll.",
    )
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=30, help="Seconds to wait. At most 120.")
    p = queue_sub.add_parser("heartbeat")
    p.add_argument("--id", required=True, type=int)
    p = queue_sub.add_parser("release")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--result", required=True)
    p.add_argument("--note")
    p = queue_sub.add_parser(
        "cancel",
        help="Lead or owner. Cancel a queued or running row.",
        description="Lead or owner. Cancel a queued or running row.",
    )
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--reason", help="Why this row is leaving the queue.")
    p = queue_sub.add_parser(
        "drop",
        help="Lead or owner. Drop a queued or running row.",
        description="Lead or owner. Drop a queued or running row.",
    )
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--reason", help="Why this row is leaving the queue.")

    p = sub.add_parser("stamp", help="Pass the verifier slot.")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--note")

    p = sub.add_parser(
        "verify",
        help="Record a verifier pass or fail at a head.",
        description=(
            "Record a verifier pass or fail at a head. "
            "A pass needs a test log with a non-failing result for that commit. "
            "A fail needs a test log whose result is fail. "
            "shuin seal names this command when the local-seal fallback has no pass."
        ),
    )
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--result", required=True, help="pass or fail")

    p = sub.add_parser("seal", help="Run the fail-closed PR gate.")
    p.add_argument("--pr", required=True, help=_PR_HELP)
    p = sub.add_parser("seal-status", help="Read the seal.")
    p.add_argument("--pr", required=True, help=_PR_HELP)

    p = sub.add_parser("board", help="Print the board.")
    p.add_argument("--since", type=int, default=0)
    p = sub.add_parser("process-ran", help="Append a bridge step signal to the local ledger.")
    p.add_argument("--job", required=True)
    p.add_argument("--step", required=True)
    p.add_argument("--status", required=True)
    p = sub.add_parser("process-check", help="Check the local ledger for missing bridge steps.")
    p.add_argument("--since", required=True, help="1-365d or 1-8760h")
    p = sub.add_parser("render", help="Write board.html and board.json.")
    p.add_argument("--out", default=".")

    p = sub.add_parser("inbox", help="Events since your cursor.")
    p.add_argument("--peek", action="store_true")
    p = sub.add_parser("notify", help="Subscribe to an event.")
    p.add_argument("--event", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--target")
    p.add_argument("--task")
    p.add_argument("--ttl", type=int)
    p = sub.add_parser("unsubscribe")
    p.add_argument("--id", required=True, type=int)
    p = sub.add_parser("quoted", help="Lead only. Claims as quoted data.")
    p.add_argument("task_id")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser(
        "overlap",
        help="Shared files, hunks, and semantic checks across open PRs and branches.",
    )
    p.add_argument("task_id", nargs="?")
    p.add_argument("--branch", action="append", default=[], help="Branch to diff. Repeatable. Works before a PR exists.")
    p.add_argument("--sha", action="append", default=[], help="Commit to diff. Repeatable.")
    p.add_argument("--repo", help="Git checkout. Defaults to the current directory when it is one.")
    p.add_argument("--base", help="Base ref for the three-dot diff. Defaults to main or master.")
    p.add_argument("--migration-regex", help="Regex whose capture group is a migration id.")

    p = sub.add_parser("poll", help="Refresh linked PRs from GitHub.")
    p.add_argument("--loop", action="store_true")
    sub.add_parser("deliver", help="Send due webhooks.")
    p = sub.add_parser("followups", help="Lead outbox for follow-up relays.")
    follow = p.add_subparsers(dest="follow_cmd")
    follow.add_parser("list")
    p = follow.add_parser("ack")
    p.add_argument("--id", required=True, type=int)

    p = sub.add_parser("serve", help="Localhost HTTP API and board.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8741)
    sub.add_parser("mcp", help="MCP server on stdio.")

    job = sub.add_parser("job", help="Detach an agent command in a git worktree.")
    job_sub = job.add_subparsers(dest="job_cmd", required=True)
    run = job_sub.add_parser(
        "run",
        help="Mint or reuse a token, add a worktree, and detach a command.",
        description=(
            "Start a command in a new session so it survives the launching shell. "
            "On exit, timeout, or stop the process group is killed. "
            "SIGTERM waits --grace seconds (default 5) before SIGKILL. "
            "The sandbox must be able to write the worktree, the worktree base, "
            "the main repo's .git/worktrees/<name>, the objects directory, and the refs directory. "
            "A failed launch removes the worktree and any branch it created. "
            "After the job exits, the worktree stays until `shuin job rm`. "
            "Pass the command after --."
        ),
    )
    run.add_argument("--repo", default=".", help="Local git checkout. A linked worktree uses the main repo. Default: current directory.")
    run.add_argument("--branch", required=True, help="Branch to check out in the worktree.")
    run.add_argument("--base", help="Start point when the branch is new. Default: HEAD.")
    run.add_argument("--agent", help="Agent id to mint or reuse.")
    run.add_argument("--kind", default="codex", help="Agent kind when minting. Default: codex.")
    run.add_argument("--role", default="worker", help="Agent role when minting. Default: worker.")
    run.add_argument("--timeout", type=int, help="Seconds before the process group is killed.")
    run.add_argument("--grace", type=float, help="Seconds between SIGTERM and SIGKILL. Default: 5.")
    run.add_argument("--id", help="Job id. Default: a generated id.")
    job_sub.add_parser("status", help="Show one job, or every job.").add_argument("--id")
    stop = job_sub.add_parser("stop", help="Stop a starting job, or signal a running job that is still ours.")
    stop.add_argument("--id", required=True)
    stop.add_argument("--grace", type=float, help="Seconds between SIGTERM and SIGKILL. Default: 5.")
    rm = job_sub.add_parser(
        "rm",
        help="Remove a finished job's worktree. The branch is kept.",
        description=(
            "Remove the worktree and the job sidecar. The branch is kept. "
            "Worktrees stay on disk after the job exits until this command. "
            "Refuses a running or starting job, and a job whose command group still has a process."
        ),
    )
    rm.add_argument("--id", required=True)

    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or 0)
    if getattr(args, "cmd", None) == "job" and getattr(args, "job_cmd", None) == "run":
        args.command = job_command
    try:
        return _run(args, options)
    except LedgerError as exc:
        _emit(exc.to_dict() if options["json"] else exc.message, options, failed=True)
        return 1
    except KeyboardInterrupt:
        return 130


def _run(args, options) -> int:
    if args.cmd == "init":
        return _init(options)
    if args.cmd == "demo":
        from shuin.demo import seed

        path = _db(options)
        if path.exists() and not args.force:
            ledger = open_ledger(path)
            try:
                existing = ledger.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
            finally:
                ledger.close()
            if existing:
                _emit("Sample night already on disk.", options)
                return 0
        if args.force and path.exists():
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        ledger = open_ledger(path)
        try:
            tokens = seed(ledger)
        finally:
            ledger.close()
        secret_path = path.parent / "tokens.json"
        secret_path.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
        os.chmod(secret_path, 0o600)
        _emit({"db": str(path), "tokens": str(secret_path), "voice": "Sample night. Not a live sync."}, options)
        return 0
    if args.cmd == "render":
        ledger = open_ledger(_db(options))
        try:
            html_path, json_path = write_board(ledger, args.out)
            voice = ledger.snapshot(include_claims=False)["voice"]
        finally:
            ledger.close()
        _emit({"html": str(html_path), "json": str(json_path), "voice": voice} if options["json"] else f"{voice}\n{html_path}", options)
        return 0
    if args.cmd == "serve":
        from shuin.httpapi import serve

        server = serve(_db(options), host=args.host, port=args.port)
        host, port = server.server_address[:2]
        print(f"朱 Shuin  http://{host}:{port}/", file=sys.stderr)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    if args.cmd == "mcp":
        from shuin.mcp import serve_stdio

        serve_stdio(db_path=str(_db(options)), token=options["token"])
        return 0
    if args.cmd == "job":
        return _job(args, options)
    if args.cmd == "poll":
        return _poll(args, options)
    if args.cmd == "deliver":
        from shuin.ledger import post_webhook

        ledger = open_ledger(_db(options))
        try:
            allow_localhost = ledger.allows_localhost_webhooks()

            def send(url, body, headers):
                post_webhook(url, body, headers, allow_localhost=allow_localhost)

            result = ledger.deliver_due(send)
        finally:
            ledger.close()
        _emit(result, options)
        return 0

    ledger = open_ledger(_db(options))
    try:
        actor = _actor(ledger, options, required=_needs_token(args))
        payload, code = _dispatch(ledger, actor, args, options)
    finally:
        ledger.close()
    if payload is not None:
        _emit(payload, options, failed=code != 0)
    return code


def _dispatch(ledger, actor, args, options):
    if args.cmd == "agent":
        if args.agent_cmd == "mint":
            if actor is None:
                token = ledger.mint_agent(None, args.id, args.kind, args.role, model=args.model)
            else:
                token = ledger.mint_agent(actor, args.id, args.kind, args.role, model=args.model)
            body = {"id": args.id, "token": token}
            if args.model:
                body["model"] = args.model
            return body, 0
        if args.agent_cmd == "rotate":
            return {"id": args.id, "token": ledger.rotate_token(actor, args.id)}, 0
        return ledger.disable_agent(actor, args.id), 0
    if args.cmd == "task":
        if args.task_cmd == "add":
            return ledger.add_task(
                actor,
                args.title,
                repo=args.repo,
                depends_on=args.depends_on,
                idempotency_key=args.idempotency_key,
                depends_on_pr=args.depends_on_pr,
            ), 0
        if args.task_cmd == "depend":
            return ledger.depend(
                actor,
                args.task_id,
                depends_on=args.depends_on,
                depends_on_pr=args.depends_on_pr,
                remove=args.remove,
            ), 0
        if args.task_cmd == "ready":
            body = ledger.ready_tasks(actor)
            return body if options["json"] else _format_ready(body), 0
        if args.task_cmd == "claim":
            return ledger.claim_task(actor, args.task_id, assignee=args.assignee, model=args.model), 0
        if args.task_cmd == "block":
            return ledger.set_blocked(actor, args.task_id, args.reason), 0
        if args.task_cmd == "unblock":
            return ledger.resolve_blocked(actor, args.task_id), 0
        if args.task_cmd == "drop":
            return ledger.drop_task(actor, args.task_id), 0
        return ledger.complete_task(
            actor,
            args.task_id,
            merged_externally=args.merged_externally,
            reason=args.reason,
        ), 0
    if args.cmd == "pr":
        repo, number = ledger.resolve_pr_ref(args.pr)
        return ledger.link_pr(actor, args.task_id, repo, number, node_id=args.node_id), 0
    if args.cmd == "update":
        return ledger.post_update(actor, args.task_id, args.kind, args.body, on_behalf_of=args.on_behalf_of), 0
    if args.cmd == "review":
        repo, number = ledger.resolve_pr_ref(args.pr)
        tried = args.tried if args.tried is not None else None
        if tried is None:
            raise LedgerError(400, "tried_failed_required", "Tried-and-failed is required.")
        return ledger.request_review(actor, repo, number, tried, on_behalf_of=args.on_behalf_of, commit_sha=args.commit_sha), 0
    if args.cmd == "evidence":
        repo, number = ledger.resolve_pr_ref(args.pr)
        return ledger.add_evidence(actor, repo, number, args.kind, args.uri, args.commit_sha, result=args.result, on_behalf_of=args.on_behalf_of), 0
    if args.cmd == "queue":
        if args.queue_cmd in (None, "list"):
            rows = ledger.queue(maintain=actor is not None)
            return _format_queue(rows) if not options["json"] else {"queue": rows}, 0
        if args.queue_cmd == "enqueue":
            repo, number = ledger.resolve_pr_ref(args.pr)
            github = GhClient() if args.poll else None
            return ledger.enqueue(
                actor,
                repo,
                number,
                args.commit_sha,
                require_run=args.require_run,
                poll=args.poll,
                github=github,
            ), 0
        if args.queue_cmd == "claim":
            return ledger.claim_slot(actor, wait=args.wait, timeout=args.timeout), 0
        if args.queue_cmd == "heartbeat":
            return ledger.heartbeat(actor, args.id), 0
        if args.queue_cmd in ("cancel", "drop"):
            return ledger.cancel_queue(actor, args.id, reason=args.reason), 0
        return ledger.release_slot(actor, args.id, args.result, note=args.note), 0
    if args.cmd == "stamp":
        body = ledger.stamp(actor, args.id, note=args.note)
        return body["voice"] if not options["json"] else body, 0
    if args.cmd == "verify":
        repo, number = ledger.resolve_pr_ref(args.pr)
        body = ledger.verify(actor, repo, number, args.commit_sha, args.result)
        return body["voice"] if not options["json"] else body, 0
    if args.cmd == "seal":
        repo, number = ledger.resolve_pr_ref(args.pr)
        body = ledger.seal(actor, repo, number, GhClient())
        text = body["voice"] if not options["json"] else body
        return text, 0 if body["ok"] else 1
    if args.cmd == "seal-status":
        repo, number = ledger.resolve_pr_ref(args.pr)
        body = ledger.seal_status(repo, number)
        if options["json"]:
            return body, 0
        return body["voice"], 0
    if args.cmd == "process-ran":
        ledger.record_process_run(args.job, args.step, args.status)
        return None, 0
    if args.cmd == "process-check":
        match = re.fullmatch(r"([0-9]{1,4})([dh])", args.since)
        if match is None or not 1 <= int(match[1]) <= (365 if match[2] == "d" else 8760):
            raise LedgerError(400, "bad_since", "Since must be 1-365d or 1-8760h.")
        seconds = int(match[1]) * (86400 if match[2] == "d" else 3600)
        body = ledger.process_check(seconds)
        body["since"] = args.since
        ok = not (body["missing"] or body["absent"] or body["no_jobs"])
        if options["json"]:
            return body, 0 if ok else 1
        lines = [f"missing: {item['job']} {item['step']}" for item in body["missing"]]
        lines.extend(f"absent: {step}" for step in body["absent"])
        if body["no_jobs"]:
            lines.append(f"no jobs in the last {args.since}")
        lines.append(f"process-check: {len(body['jobs'])} jobs, {len(body['missing'])} missing, {len(body['absent'])} absent")
        _emit("\n".join(lines), options)
        return None, 0 if ok else 1
    if args.cmd == "board":
        if actor is None and options["json"]:
            raise LedgerError(401, "token_required", token_required_message(_db(options)))
        body = ledger.snapshot(since_cursor=args.since, include_claims=False) if actor is None else ledger.list_board(actor, since_cursor=args.since)
        if options["json"]:
            return body, 0
        return _format_board(body), 0
    if args.cmd == "inbox":
        return ledger.my_inbox(actor, ack=not args.peek), 0
    if args.cmd == "notify":
        filt = {"task_id": args.task} if args.task else None
        return ledger.notify_me(actor, args.event, args.channel, channel_target=args.target, filt=filt, ttl_seconds=args.ttl), 0
    if args.cmd == "unsubscribe":
        return ledger.unsubscribe(actor, args.id), 0
    if args.cmd == "quoted":
        return ledger.read_quoted(actor, args.task_id, limit=args.limit), 0
    if args.cmd == "overlap":
        if not args.task_id and not args.branch and not args.sha:
            raise LedgerError(400, "overlap_target_required", "Overlap needs a task, branch, or sha.")
        return ledger.check_overlap(
            actor,
            args.task_id,
            branches=args.branch,
            shas=args.sha,
            repo_path=args.repo,
            base=args.base,
            migration_regex=args.migration_regex,
        ), 0
    if args.cmd == "followups":
        if args.follow_cmd == "ack":
            return ledger.ack_followup(actor, args.id), 0
        return ledger.followups(actor), 0
    raise LedgerError(400, "bad_command", "Command is not usable.")


def _job(args, options) -> int:
    from shuin.jobs import job_status, remove_job, run_job, stop_job

    db = _db(options)
    if args.job_cmd == "run":
        payload = run_job(
            db=db,
            token=options["token"],
            repo=Path(args.repo),
            branch=args.branch,
            command=list(args.command or []),
            agent_id=args.agent,
            kind=args.kind,
            role=args.role,
            timeout=args.timeout,
            job_id=args.id,
            base=args.base,
            grace=args.grace,
        )
    elif args.job_cmd == "status":
        payload = job_status(db, job_id=args.id)
    elif args.job_cmd == "stop":
        payload = stop_job(db, args.id, grace=args.grace)
    elif args.job_cmd == "rm":
        payload = remove_job(db, args.id)
    else:
        raise LedgerError(400, "bad_command", "Command is not usable.")
    if options["json"]:
        _emit(payload, options)
    else:
        _emit(_format_job(payload), options)
    return 0


def version_text() -> str:
    """Package version, plus the git SHA of this package's source tree."""
    sha = _git_sha()
    if sha:
        return f"shuin {__version__} {sha}"
    return f"shuin {__version__}"


def _git_sha() -> str | None:
    """HEAD of the repo that contains this shuin package, or None."""
    package = Path(__file__).resolve().parent
    if package.name != "shuin" or not (package / "__init__.py").is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(package), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if len(lines) != 2:
        return None
    toplevel, sha = lines[0].strip(), lines[1].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return None
    repo_shuin = Path(toplevel) / "shuin"
    if not (repo_shuin / "__init__.py").is_file():
        return None
    try:
        if not package.samefile(repo_shuin):
            return None
    except OSError:
        return None
    return sha


def _poll(args, options) -> int:
    ledger = open_ledger(_db(options))
    try:
        while True:
            result = ledger.poll_once(GhClient())
            _emit(result, options)
            if not args.loop:
                return 0
            time.sleep(60)
    finally:
        ledger.close()


def _init(options) -> int:
    path = _db(options)
    ledger = open_ledger(path)
    try:
        count = ledger.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
        if count:
            _emit({"db": str(path), "voice": "Ledger already exists."}, options)
            return 0
        token = ledger.mint_agent(None, "admin", "human", "owner")
    finally:
        ledger.close()
    secret = path.parent / "owner.token"
    secret.write_text(token + "\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    _emit({"db": str(path), "token_file": str(secret), "id": "admin"}, options)
    return 0


def _needs_token(args) -> bool:
    if args.cmd in ("board", "process-ran", "process-check", "seal-status", "queue") and getattr(args, "queue_cmd", "list") in (None, "list"):
        return False
    if args.cmd == "agent" and args.agent_cmd == "mint":
        return False
    if args.cmd == "queue" and args.queue_cmd in (None, "list"):
        return False
    return True


def _actor(ledger, options, required: bool):
    token = options["token"]
    if not token:
        if required:
            raise LedgerError(401, "token_required", token_required_message(_db(options)))
        return None
    return ledger.authenticate(token)


def _db(options) -> Path:
    return Path(options["db"] or default_db_path())


def _split_job_command(rest):
    """Take the argv after -- for `job run`, so the command is not parsed as flags."""
    if not rest or rest[0] != "job" or "--" not in rest:
        return rest, None
    split = rest.index("--")
    if "run" not in rest[:split]:
        return rest, None
    return rest[:split], rest[split + 1 :]


def _format_job(payload) -> str:
    if isinstance(payload, dict) and payload.get("removed"):
        return f"{payload['id']}  removed\n  worktree  {payload.get('worktree')}"
    rows = payload["jobs"] if isinstance(payload, dict) and "jobs" in payload else [payload]
    if not rows:
        return "No jobs."
    lines = []
    for row in rows:
        pid = row.get("pid") or "—"
        pgid = row.get("pgid") or "—"
        lines.append(f"{row['id']}  {row['status']:<8} pid {pid}  pgid {pgid}")
        lines.append(f"  worktree  {row.get('worktree')}")
        lines.append(f"  log       {row.get('log')}")
    return "\n".join(lines)


def _peel(argv):
    options = {
        "json": False,
        "no_color": bool(os.environ.get("NO_COLOR")),
        "db": os.environ.get("SHUIN_DB"),
        "token": os.environ.get("SHUIN_TOKEN") or os.environ.get("LEDGER_TOKEN"),
    }
    rest = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--json":
            options["json"] = True
        elif item == "--no-color":
            options["no_color"] = True
        elif item == "--db" and index + 1 < len(argv):
            options["db"] = argv[index + 1]
            index += 1
        elif item.startswith("--db="):
            options["db"] = item.split("=", 1)[1]
        elif item == "--token" and index + 1 < len(argv):
            options["token"] = argv[index + 1]
            index += 1
        elif item.startswith("--token="):
            options["token"] = item.split("=", 1)[1]
        else:
            rest.append(item)
        index += 1
    return options, rest


def _emit(payload, options, failed: bool = False) -> None:
    if options["json"] or isinstance(payload, (dict, list)):
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    else:
        text = str(payload)
    print(text, file=sys.stderr if failed and not options["json"] else sys.stdout)


def _format_ready(body) -> str:
    tasks = body.get("tasks") or []
    if not tasks:
        return "No tasks ready."
    lines = ["朱  READY"]
    for task in tasks:
        deps = format_dependencies(task.get("dependencies"))
        suffix = f"  {deps}" if deps else ""
        lines.append(f"{task['id']}  {task['status']:<8} {task['title']}{suffix}")
    return "\n".join(lines)


def _format_queue(rows) -> str:
    if not rows:
        return "Queue empty."
    lines = ["朱  QUEUE"]
    for item in rows:
        left = format_left(item["lease_left_seconds"]) if item.get("lease_left_seconds") is not None else ""
        lines.append(
            f"{item['position']:02d}  {item['state']:<14} {item['repo']}#{item['pr']}  {item['commit_sha'][:7]}  {item.get('claimed_by') or '—'}  {left}"
        )
    return "\n".join(lines)


def _format_board(body) -> str:
    lines = [f"朱  {body['voice']}"]
    for task in body["tasks"]:
        if task["status"] == "dropped":
            continue
        pr = task.get("pr") or {}
        pr_bit = f"{pr.get('repo')}#{pr.get('number')}" if pr else "—"
        pad = "  " * int(task.get("depth") or 0)
        closed = ""
        if task["status"] == "done" and task.get("closed_by"):
            closed = f"  {task['closed_by']}"
            if task.get("closed_reason"):
                closed += f"  {task['closed_reason']}"
        lines.append(f"{pad}{task['id']}  {task.get('owner') or '—':<12} {task['status']:<12} {pr_bit}  {task['voice']}{closed}")
        dep_line = format_dependencies(task.get("dependencies"))
        if dep_line:
            lines.append(f"{pad}  {dep_line}")
        for run in task.get("runs") or []:
            if not run.get("model"):
                continue
            lines.append(f"{pad}  run {run['attempt']}  {run.get('agent_id') or '—'}  {run['model']}")
    if not any(task["status"] != "dropped" for task in body["tasks"]):
        lines.append("No cards. The night is quiet.")
    return "\n".join(lines)
