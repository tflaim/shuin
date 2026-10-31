"""GitHub poller. Reads PR identity and check state. Does not store diffs or comment bodies.

Branch protection and ruleset lists are optional. A 404, a genuine 403, or an
empty ruleset list falls back to the local verifier pass and Gauntlet status.
Every path still reads the head's check runs and refuses if any failed. A 401, or
any other auth error, fails closed. A 403 on the pull request itself still
fails closed.
"""

import json
import subprocess
from collections.abc import Callable

from shuin.errors import GhProblem
from shuin.seal import Freshness, fresh
from shuin.types import CheckItem, ChecksRead, HeadRead, PrSnapshot

Runner = Callable[[list[str]], tuple[int, str, str]]
GAUNTLET_CONTEXT = "shuin/gauntlet"

_REVIEW = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}


def default_runner(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, out, err


def _kind_from_error(text: str) -> str:
    blob = text.lower()
    if "404" in blob or "not found" in blob:
        return "missing"
    if "403" in blob or "forbidden" in blob:
        return "forbidden"
    if any(token in blob for token in ("401", "auth", "login", "credential", "permission")):
        return "auth"
    return "infra"


class GhClient:
    def __init__(self, runner: Runner | None = None):
        self._run = runner or default_runner

    def api(self, path: str, *args: str):
        code, out, err = self._run(["gh", "api", path, *args])
        if code != 0:
            kind = _kind_from_error(err or out)
            if kind == "missing":
                raise GhProblem("infra")
            if kind in ("auth", "forbidden"):
                raise GhProblem("auth")
            raise GhProblem("infra")
        if not out.strip():
            raise GhProblem("infra")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise GhProblem("infra") from exc

    def post_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> dict:
        args = [
            "--method", "POST", "-f", f"state={state}",
            "-f", f"context={GAUNTLET_CONTEXT}", "-f", f"description={description}",
        ]
        status = {"sha": sha, "state": state, "context": GAUNTLET_CONTEXT, "description": description}
        if target_url is not None:
            args.extend(["-f", f"target_url={target_url}"])
            status["target_url"] = target_url
        self.api(f"repos/{repo}/statuses/{sha}", *args)
        return status

    def api_optional(self, path: str, *, ignore_auth: bool = False):
        """Return None on 404. ignore_auth also returns None on a genuine 403.

        401, login, credential, and permission errors still fail closed.
        """
        payload, _forbidden = self._read_optional(path, ignore_forbidden=ignore_auth)
        return payload

    def _read_optional(self, path: str, *, ignore_forbidden: bool = False):
        """Return (payload, forbidden). A 404 is (None, False)."""
        code, out, err = self._run(["gh", "api", path])
        if code != 0:
            kind = _kind_from_error(err or out)
            if kind == "missing":
                return None, False
            if ignore_forbidden and kind == "forbidden":
                return None, True
            if kind in ("auth", "forbidden"):
                raise GhProblem("auth")
            raise GhProblem("infra")
        if not out.strip():
            return None, False
        try:
            return json.loads(out), False
        except json.JSONDecodeError as exc:
            raise GhProblem("infra") from exc

    def api_collect(self, path: str) -> list:
        items: list = []
        page = 1
        while page <= 20:
            sep = "&" if "?" in path else "?"
            data = self.api(f"{path}{sep}per_page=100&page={page}")
            if isinstance(data, list):
                items.extend(data)
                if len(data) < 100:
                    return items
            elif isinstance(data, dict) and "check_runs" in data:
                batch = data.get("check_runs") or []
                items.extend(batch)
                total = int(data.get("total_count") or len(batch))
                if len(batch) < 100 or len(items) >= total:
                    return items
            else:
                raise GhProblem("infra")
            page += 1
        raise GhProblem("infra")

    def read_head(self, repo: str, number: int) -> HeadRead:
        try:
            pr = self.api(f"repos/{repo}/pulls/{number}")
        except GhProblem as exc:
            return HeadRead(error=exc.kind)
        try:
            return head_from_pr(pr)
        except GhProblem as exc:
            return HeadRead(error=exc.kind)

    def read_checks(self, repo: str, base_branch: str | None, head_sha: str | None) -> ChecksRead:
        if not head_sha or not base_branch:
            return ChecksRead(error="infra")
        try:
            contexts, fallback = self._required_contexts(repo, base_branch)
            if GAUNTLET_CONTEXT not in contexts:
                contexts.append(GAUNTLET_CONTEXT)
            runs = self.api_collect(f"repos/{repo}/commits/{head_sha}/check-runs")
            status = self.api(f"repos/{repo}/commits/{head_sha}/status")
            statuses = status.get("statuses") if isinstance(status, dict) else []
            checks = match_required(contexts, runs, statuses or [], head_sha)
            if fallback:
                checks = _include_failed_runs(checks, runs, head_sha)
            return ChecksRead(checks=tuple(checks), error=None)
        except GhProblem as exc:
            return ChecksRead(error=exc.kind)

    def fetch_pull(self, repo: str, number: int) -> PrSnapshot:
        pr = self.api(f"repos/{repo}/pulls/{number}")
        head = head_from_pr(pr)
        if head.error or not head.head_sha:
            raise GhProblem(head.error or "infra")
        reviews = self.api_collect(f"repos/{repo}/pulls/{number}/reviews")
        files_payload = self.api_collect(f"repos/{repo}/pulls/{number}/files")
        runs = self.api_collect(f"repos/{repo}/commits/{head.head_sha}/check-runs")
        status = self.api(f"repos/{repo}/commits/{head.head_sha}/status")
        statuses = status.get("statuses") if isinstance(status, dict) else []
        files = clean_files(item.get("filename") for item in files_payload if isinstance(item, dict))
        if files is None:
            raise GhProblem("infra")
        return PrSnapshot(
            repo=repo,
            number=number,
            node_id=head.node_id,
            branch=head.branch,
            base_branch=head.base_branch,
            head_sha=head.head_sha,
            state=head.state,
            review_state=aggregate_reviews(reviews),
            ci_state=aggregate_ci(runs, statuses or [], head.head_sha),
            files=tuple(files),
        )

    def _required_contexts(self, repo: str, base_branch: str) -> tuple[list[str], bool]:
        """Return required contexts and whether this was a fallback read.

        A fallback is protection 403 or 404, a ruleset list 403, or no rules.
        The caller still reads check runs and refuses a failure.
        """
        contexts: list[str] = []
        protection, protection_forbidden = self._read_optional(
            f"repos/{repo}/branches/{base_branch}/protection",
            ignore_forbidden=True,
        )
        if isinstance(protection, dict):
            contexts.extend(contexts_from_protection(protection))
        repo_contexts, repo_forbidden = self._contexts_from_rulesets(
            f"repos/{repo}/rulesets",
            base_branch,
            ignore_auth=True,
        )
        contexts.extend(repo_contexts)
        owner = repo.split("/", 1)[0]
        # 404 -> user account, no org rules. A genuine 403 is no ruleset list.
        org_list, org_forbidden = self._read_optional(
            f"orgs/{owner}/rulesets",
            ignore_forbidden=True,
        )
        if isinstance(org_list, list):
            contexts.extend(self._contexts_from_ruleset_list(f"orgs/{owner}/rulesets", org_list, base_branch))
        elif org_list is not None:
            raise GhProblem("infra")
        seen = []
        for ctx in contexts:
            if ctx not in seen:
                seen.append(ctx)
        fallback = (
            not seen
            or protection is None
            or protection_forbidden
            or repo_forbidden
            or org_forbidden
        )
        return seen, fallback

    def _failing_check_runs(self, repo: str, head_sha: str) -> list[CheckItem]:
        """Check runs on this head. A 403 fallback still refuses a failure."""
        runs = self._collect_check_runs(repo, head_sha, missing_ok=True)
        failing: list[CheckItem] = []
        for run in runs:
            if not fresh_check_run(run, head_sha):
                continue
            if _run_state(run) != "failure":
                continue
            failing.append(CheckItem(run["name"], run.get("id"), "failure", "check_run"))
        return failing

    def _collect_check_runs(self, repo: str, head_sha: str, *, missing_ok: bool) -> list:
        items: list = []
        page = 1
        path = f"repos/{repo}/commits/{head_sha}/check-runs"
        while page <= 20:
            code, out, err = self._run(["gh", "api", f"{path}?per_page=100&page={page}"])
            if code != 0:
                kind = _kind_from_error(err or out)
                if kind == "missing" and missing_ok and not items:
                    return []
                if kind in ("auth", "forbidden"):
                    raise GhProblem("auth")
                raise GhProblem("infra")
            if not out.strip():
                raise GhProblem("infra")
            try:
                data = json.loads(out)
            except json.JSONDecodeError as exc:
                raise GhProblem("infra") from exc
            if isinstance(data, dict) and "check_runs" in data:
                batch = data.get("check_runs") or []
                items.extend(batch)
                total = int(data.get("total_count") or len(batch))
                if len(batch) < 100 or len(items) >= total:
                    return items
            elif isinstance(data, list):
                items.extend(data)
                if len(data) < 100:
                    return items
            else:
                raise GhProblem("infra")
            page += 1
        raise GhProblem("infra")

    def _contexts_from_rulesets(self, list_path: str, base_branch: str, *, ignore_auth: bool = False) -> tuple[list[str], bool]:
        listing, forbidden = self._read_optional(list_path, ignore_forbidden=ignore_auth)
        if listing is None:
            return [], forbidden
        if not isinstance(listing, list):
            raise GhProblem("infra")
        return self._contexts_from_ruleset_list(list_path, listing, base_branch), forbidden

    def _contexts_from_ruleset_list(self, list_path: str, listing: list, base_branch: str) -> list[str]:
        found: list[str] = []
        for item in listing:
            if not isinstance(item, dict) or "id" not in item:
                continue
            detail = self.api(f"{list_path}/{item['id']}")
            if not isinstance(detail, dict):
                raise GhProblem("infra")
            if ruleset_applies(detail, base_branch):
                found.extend(contexts_from_ruleset(detail))
        return found


def _include_failed_runs(checks: list[CheckItem], runs: list, head_sha: str) -> list[CheckItem]:
    """A fallback that already has some required contexts still refuses any failure."""
    covered = {item.context for item in checks if item.state == "failure"}
    extra = list(checks)
    for run in runs:
        if not fresh_check_run(run, head_sha) or _run_state(run) != "failure":
            continue
        if run["name"] in covered:
            continue
        covered.add(run["name"])
        extra.append(CheckItem(run["name"], run.get("id"), "failure", "check_run"))
    return extra


def head_from_pr(pr: dict) -> HeadRead:
    if not isinstance(pr, dict):
        raise GhProblem("infra")
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    sha = head.get("sha")
    if sha is not None:
        sha = str(sha).lower()
        if not all(ch in "0123456789abcdef" for ch in sha) or not (7 <= len(sha) <= 64):
            raise GhProblem("infra")
    if pr.get("merged"):
        state = "merged"
    elif pr.get("state") == "open":
        state = "open"
    elif pr.get("state") == "closed":
        state = "closed"
    else:
        raise GhProblem("infra")
    return HeadRead(
        head_sha=sha,
        branch=head.get("ref"),
        base_branch=base.get("ref"),
        node_id=pr.get("node_id"),
        state=state,
        error=None,
    )


def contexts_from_protection(protection: dict) -> list[str]:
    block = protection.get("required_status_checks") or {}
    found: list[str] = []
    for ctx in block.get("contexts") or []:
        if isinstance(ctx, str) and ctx:
            found.append(ctx)
    for check in block.get("checks") or []:
        if isinstance(check, str) and check:
            found.append(check)
        elif isinstance(check, dict) and check.get("context"):
            found.append(check["context"])
    return found


def contexts_from_ruleset(ruleset: dict) -> list[str]:
    found: list[str] = []
    for rule in ruleset.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                found.append(check["context"])
    return found


def ruleset_applies(ruleset: dict, base_branch: str) -> bool:
    if ruleset.get("enforcement") not in (None, "active"):
        return False
    if ruleset.get("target") not in (None, "branch"):
        return False
    cond = (ruleset.get("conditions") or {}).get("ref_name") or {}
    include = cond.get("include") or []
    exclude = cond.get("exclude") or []
    ref = f"refs/heads/{base_branch}"
    if _match_any(exclude, ref, base_branch):
        return False
    if not include:
        return True
    return _match_any(include, ref, base_branch)


def _match_any(patterns, ref: str, branch: str) -> bool:
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        if pattern in (ref, branch, "~ALL", "**", "*", "refs/heads/*", "~DEFAULT_BRANCH"):
            return True
        if pattern.endswith("/**") and ref.startswith(pattern[:-3]):
            return True
        if pattern.endswith("*") and (ref.startswith(pattern[:-1]) or branch.startswith(pattern[:-1])):
            return True
    return False


def _run_state(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
        return "failure"
    if conclusion in ("skipped", "neutral", "stale"):
        return conclusion
    return "pending"


def _status_state(state: str | None) -> str:
    if state == "success":
        return "success"
    if state == "pending":
        return "pending"
    if state in ("failure", "error"):
        return "failure"
    return "pending"


_WORSE = {"failure": 5, "stale": 4, "skipped": 3, "neutral": 3, "pending": 2, "success": 1, "missing": 0}


def fresh_check_run(run: dict, head_sha: str | None) -> bool:
    """A success counts only for a full run on this exact SHA.

    A run whose head differs was carried over. A run flagged patch_identity
    or carried_over did not execute on this commit, even if the SHA matches.
    """
    if not isinstance(run, dict) or not run.get("name"):
        return False
    if run.get("carried_over") is True or run.get("patch_identity") is True:
        return False
    if head_sha is None:
        return True
    if not fresh(Freshness(head_sha, run.get("head_sha"))):
        return False
    suite = run.get("check_suite")
    if isinstance(suite, dict) and suite.get("head_sha"):
        if not fresh(Freshness(head_sha, suite.get("head_sha"))):
            return False
    return True


def fresh_status(status: dict, head_sha: str | None) -> bool:
    if not isinstance(status, dict) or not status.get("context"):
        return False
    if status.get("carried_over") is True or status.get("patch_identity") is True:
        return False
    if head_sha is None or status.get("sha") is None:
        return True
    return fresh(Freshness(head_sha, status.get("sha")))


def match_required(contexts: list[str], check_runs: list, statuses: list, head_sha: str | None = None) -> list[CheckItem]:
    if isinstance(head_sha, str):
        head_sha = head_sha.lower()
    latest_runs: dict[str, dict] = {}
    for run in check_runs:
        if not fresh_check_run(run, head_sha):
            continue
        prev = latest_runs.get(run["name"])
        if prev is None or int(run.get("id") or 0) >= int(prev.get("id") or 0):
            latest_runs[run["name"]] = run
    latest_status: dict[str, dict] = {}
    for status in statuses:
        if not fresh_status(status, head_sha):
            continue
        prev = latest_status.get(status["context"])
        if prev is None or int(status.get("id") or 0) >= int(prev.get("id") or 0):
            latest_status[status["context"]] = status
    matched: list[CheckItem] = []
    for context in contexts:
        run = latest_runs.get(context)
        status = latest_status.get(context)
        candidates: list[CheckItem] = []
        if run is not None:
            candidates.append(
                CheckItem(context, run.get("id"), _run_state(run), "check_run")
            )
        if status is not None:
            candidates.append(
                CheckItem(context, status.get("id"), _status_state(status.get("state")), "status")
            )
        if not candidates:
            matched.append(CheckItem(context, None, "missing", "missing"))
            continue
        worst = max(candidates, key=lambda item: _WORSE.get(item.state, 0))
        matched.append(worst)
    return matched


def aggregate_reviews(reviews: list) -> str | None:
    latest: dict = {}
    ordered = sorted(
        (rev for rev in reviews if isinstance(rev, dict)),
        key=lambda rev: rev.get("submitted_at") or "",
    )
    for rev in ordered:
        user = (rev.get("user") or {}).get("id")
        state = rev.get("state")
        if user is None or state in (None, "PENDING"):
            continue
        if state not in _REVIEW:
            continue
        latest[user] = _REVIEW[state]
    states = set(latest.values())
    if "changes_requested" in states:
        return "changes_requested"
    if "approved" in states:
        return "approved"
    if "commented" in states:
        return "commented"
    if "dismissed" in states:
        return "dismissed"
    return None


def aggregate_ci(check_runs: list, statuses: list, head_sha: str | None = None) -> str:
    if isinstance(head_sha, str):
        head_sha = head_sha.lower()
    states: list[str] = []
    for run in check_runs:
        if fresh_check_run(run, head_sha):
            states.append(_run_state(run))
    for status in statuses:
        if fresh_status(status, head_sha):
            states.append(_status_state(status.get("state")))
    if not states:
        return "none"
    if any(state == "failure" for state in states):
        return "failing"
    if any(state != "success" for state in states):
        return "pending"
    return "passing"


def clean_files(names) -> list[str] | None:
    cleaned = []
    for name in names:
        if not isinstance(name, str) or not name or len(name) > 500:
            continue
        parts = name.split("/")
        if name.startswith("/") or "\\" in name or ".." in parts:
            continue
        cleaned.append(name)
    unique = sorted(set(cleaned))
    if len(unique) > 2000:
        return None
    return unique
