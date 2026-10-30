"""Overlap beyond the file list.

Open PRs still report shared paths the way they always have. When a diff is
available, files touched by more than one open PR or branch also report the
hunks: the line range and a short snippet. Semantic checks sit behind a small
registry so another check can be registered without changing the command.

Two checks ship with Shuin.

Duplicate migration ids. A configurable regex, defaulting to GRDB
``registerMigration("vN")`` and common SQL migration filenames (Flyway
``V7__name.sql``, ``v7_name.sql``, ``V7.sql``, and ``0007_name.sql``). The
id is casefolded, so ``V7__name.sql`` and ``registerMigration("v7")`` are the
same id (``v7``). ``0007_name.sql`` stays ``0007`` and is not ``v7``. The
same id introduced by two open PRs or branches in one repo is reported as
``duplicate migration id``. A comment, or a mention in a doc, is not an id.
Two repos can both add ``v7``.

Added Swift enum cases. A ``case name`` line added inside an enum is a
warning when another open PR or branch changes a file that switches on that
type and does not already handle the case. The type match is a heuristic:
the switched expression names the type, or a name in it is declared with that
type. A ``default`` or ``@unknown default`` counts as handled, and so does a
``case`` pattern that names the new case, including ``case (.color, _)``.
A ``case`` line inside a method, initializer, or subscript is not a new enum
case.

``--branch`` and ``--sha`` diff a checkout (three-dot against ``--base``, or
``main`` / ``master``) so the check works before a PR exists. Patches are not
stored on the PR row. A local branch or an injected diff supplies them.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shuin.errors import LedgerError
from shuin.sanitize import clean_sha

# The id is the first capturing group that matched, then casefolded.
# V7__name.sql and registerMigration("v7") are both "v7".
# 0007_name.sql is "0007", which is not "v7".
# 1. GRDB registerMigration("v7") or registerMigration('v7')
# 2. V7__name.sql / v7_name.sql
# 3. V7.sql
# 4. 0007_name.sql / 0007_name.up.sql
DEFAULT_MIGRATION_REGEX = (
    r"""registerMigration\(\s*["']([^"']+)["']"""
    r"""|(?:^|/)([Vv]\d+(?:[._]\d+)*)(?:__|_)[^/\s]*(?i:\.sql)"""
    r"""|(?:^|/)([Vv]\d+(?:[._]\d+)*)(?i:\.sql)"""
    r"""|(?:^|/)(\d{3,})_[^/\s]*\.(?i:(?:up\.)?sql)"""
)

_SNIPPET = 160
_DIFF_CAP = 5_000_000
_TEXT_CAP = 200_000
_TEXT_FILES = 40
_HUNKS_CAP = 30

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_ENUM = re.compile(r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_CASE = re.compile(
    r"^\s*(?:(?:public|private|internal|fileprivate|open|indirect|final)\s+)*case\s+(.+)$"
)
_CASE_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\b")
_SWITCH = re.compile(r"\bswitch\s+([^{\n]+?)\s*\{")
_BIND = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"(?:inout\s+|consuming\s+|borrowing\s+)?(?:some\s+|any\s+)?"
    r"([A-Z][A-Za-z0-9_]*)\b"
)
_SKIP_BIND = frozenset(
    {
        "case",
        "return",
        "if",
        "switch",
        "guard",
        "import",
        "func",
        "var",
        "let",
        "class",
        "struct",
        "enum",
        "protocol",
        "extension",
    }
)
_CASE_KEYWORDS = frozenset({"let", "var", "where", "if", "is", "as", "true", "false", "nil"})
_MEMBER = re.compile(r"\b(?:func|init|subscript)\b")
# Userinfo is optional. A credential helper may rewrite origin to
# https://x-access-token:<secret>@github.com/owner/name.git. The secret
# stays out of the result.
_ORIGIN = re.compile(
    r"^(?:git@github\.com:|ssh://(?:[^@\s]+@)?git@github\.com/|https://(?:[^@\s]+@)?github\.com/)"
    r"([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    intro: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FileDiff:
    path: str
    added: bool
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True)
class Subject:
    """One open PR, or a branch or commit that has no PR yet."""

    key: str
    kind: str
    task_id: str | None = None
    repo: str | None = None
    pr: int | None = None
    branch: str | None = None
    sha: str | None = None
    files: tuple[str, ...] = ()
    diff: str = ""
    texts: tuple[tuple[str, str], ...] = ()


class Check:
    """A semantic check. ``run`` returns finding dicts.

    A finding may include ``_keys``, a list of subject keys it involves.
    ``analyze`` uses that to keep findings for the requested task or branch,
    then drops the key before returning.
    """

    name = "check"

    def run(self, subjects: list[Subject]) -> list[dict]:
        return []


class Registry:
    """Ordered list of semantic checks. ``register`` appends one."""

    def __init__(self, checks: list | None = None):
        self._checks: list = []
        for check in checks or []:
            self.register(check)

    def register(self, check) -> None:
        name = getattr(check, "name", None)
        run = getattr(check, "run", None)
        if not isinstance(name, str) or not name or not callable(run):
            raise LedgerError(400, "bad_check", "A semantic check needs a name and run.")
        self._checks.append(check)

    def __iter__(self):
        return iter(self._checks)

    def run(self, subjects: list[Subject]) -> list[dict]:
        found: list[dict] = []
        for check in self._checks:
            for item in check.run(subjects):
                if isinstance(item, dict):
                    found.append(item)
        return found


class DuplicateMigrationCheck(Check):
    """Same migration id introduced by more than one subject."""

    name = "duplicate_migration_id"

    def __init__(self, pattern: str | None = None):
        self._regex = compile_migration(pattern)

    def run(self, subjects: list[Subject]) -> list[dict]:
        # The same id in two repos is not a duplicate. A missing repo only
        # collides with another missing repo, so two local branches still do.
        by_id: dict[tuple, list] = {}
        for subject in subjects:
            seen_local: set[tuple] = set()
            for hit in _migration_hits(subject, self._regex):
                ident, path, line, snippet = hit
                marker = (path, line, snippet)
                if marker in seen_local:
                    continue
                seen_local.add(marker)
                by_id.setdefault((subject.repo, ident), []).append((subject, path, line, snippet))
        findings = []
        for _repo, ident in sorted(by_id, key=lambda item: (item[1], item[0] or "")):
            locs = by_id[(_repo, ident)]
            keys = {item[0].key for item in locs}
            if len(keys) < 2:
                continue
            findings.append(
                {
                    "check": self.name,
                    "severity": "error",
                    "message": "duplicate migration id",
                    "id": ident,
                    "locations": [_location(sub, path, line, snippet) for sub, path, line, snippet in locs],
                    "_keys": sorted(keys),
                }
            )
        return findings


class EnumSwitchCheck(Check):
    """Added enum case versus a switch over that type in another diff."""

    name = "enum_switch"

    def run(self, subjects: list[Subject]) -> list[dict]:
        added: dict[tuple[str, str], list] = {}
        parsed = {subject.key: parse_diff(subject.diff) for subject in subjects}
        for subject in subjects:
            for file_diff in parsed[subject.key]:
                for enum_name, case_name, line, snippet in _added_cases(file_diff):
                    added.setdefault((enum_name, case_name), []).append((subject, file_diff.path, line, snippet))
        findings = []
        for enum_name, case_name in sorted(added):
            adders = added[(enum_name, case_name)]
            adder_keys = {item[0].key for item in adders}
            switches = []
            switch_keys: list[str] = []
            for subject in subjects:
                if subject.key in adder_keys:
                    continue
                for path, line, expr, body in _switch_sites(subject, parsed[subject.key]):
                    if not _switch_over(expr, enum_name, _bindings_for(subject, parsed[subject.key], path)):
                        continue
                    if _handles(body, case_name):
                        continue
                    switches.append(_location(subject, path, line, _short(f"switch {expr}")))
                    switch_keys.append(subject.key)
            if not switches:
                continue
            findings.append(
                {
                    "check": self.name,
                    "severity": "warning",
                    "message": f"added enum case .{case_name} may break an exhaustive switch",
                    "type": enum_name,
                    "case": case_name,
                    "locations": [_location(sub, path, line, snippet) for sub, path, line, snippet in adders],
                    "switches": switches,
                    "_keys": sorted(adder_keys | set(switch_keys)),
                }
            )
        return findings


def default_registry(migration_regex: str | None = None) -> Registry:
    return Registry([DuplicateMigrationCheck(migration_regex), EnumSwitchCheck()])


def compile_migration(pattern: str | None):
    raw = pattern or DEFAULT_MIGRATION_REGEX
    try:
        return re.compile(raw)
    except re.error as exc:
        raise LedgerError(400, "bad_regex", "Migration regex is not usable.") from exc


def analyze(
    subjects: list[Subject],
    *,
    registry: Registry | None = None,
    migration_regex: str | None = None,
    focus: set[str] | None = None,
) -> dict:
    """Shared paths, hunks on those paths, and semantic findings.

    ``focus`` keeps results that involve one of those subject keys. ``None``
    keeps every result, which is what a fixture calls.
    """
    if registry is None:
        registry = default_registry(migration_regex)
    parsed = [(subject, parse_diff(subject.diff)) for subject in subjects]
    overlaps = _file_overlaps(parsed)
    hunks = _hunk_report(parsed, {item["path"] for item in overlaps})
    findings = registry.run(subjects)
    if focus is not None:
        overlaps = [item for item in overlaps if focus.intersection(item.get("_keys") or ())]
        hunks = [item for item in hunks if focus.intersection(item.get("_keys") or ())]
        kept = []
        for item in findings:
            keys = item.get("_keys")
            if not keys or focus.intersection(keys):
                kept.append(item)
        findings = kept
    for item in overlaps:
        item.pop("_keys", None)
    for item in hunks:
        item.pop("_keys", None)
    for item in findings:
        item.pop("_keys", None)
    findings.sort(
        key=lambda item: (
            item.get("check") or "",
            item.get("id") or "",
            item.get("type") or "",
            item.get("case") or "",
            item.get("message") or "",
        )
    )
    return {"overlaps": overlaps, "hunks": hunks, "findings": findings}


def cap_diff(diff: str) -> tuple[str, bool]:
    """Keep the first 5 MB of a diff. The caller says that it was truncated."""
    if diff and len(diff) > _DIFF_CAP:
        return diff[:_DIFF_CAP], True
    return diff or "", False


def parse_diff(diff: str) -> list[FileDiff]:
    if not diff or not diff.strip() or "diff --git " not in diff:
        return []
    diff, _truncated = cap_diff(diff)
    files = []
    for part in re.split(r"(?m)^(?=diff --git )", diff):
        if not part.startswith("diff --git "):
            continue
        parsed = _parse_file(part)
        if parsed is not None:
            files.append(parsed)
    return files


def is_checkout(path: Path) -> bool:
    if not path.is_dir():
        return False
    code, out, _ = _git(path, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def resolve_checkout(path: str | Path | None, *, allow_cwd: bool = True) -> Path | None:
    """A path must be a checkout.

    ``None`` uses the working directory when it is one and ``allow_cwd`` is
    set. HTTP and MCP pass ``allow_cwd=False`` so a server process does not
    read whatever directory it was started in.
    """
    if path:
        repo = Path(path)
        if not is_checkout(repo):
            raise LedgerError(400, "not_a_repo", "Overlap needs a git checkout.")
        return repo
    if not allow_cwd:
        return None
    cwd = Path.cwd()
    if is_checkout(cwd):
        return cwd
    return None


def checkout_remote(repo: Path) -> str | None:
    """``owner/name`` from ``origin``, or ``None`` when it is not a GitHub URL."""
    code, out, _ = _git(repo, ["remote", "get-url", "origin"])
    if code != 0:
        return None
    match = _ORIGIN.match(out.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def prefer_ref(repo: Path, sha: str | None, branch: str | None) -> str | None:
    """A local ``head_sha`` wins. The branch name is only a fallback."""
    if sha and ref_exists(repo, sha):
        return sha
    if branch and ref_exists(repo, branch):
        return branch
    return None


def clean_ref(value: str, *, what: str) -> str:
    if (
        not isinstance(value, str)
        or not _REF.fullmatch(value)
        or ".." in value.split("/")
        or "@{" in value
        or value.endswith("/")
        or value.endswith(".")
    ):
        raise LedgerError(400, "bad_ref", f"{what} is not usable.")
    return value


def default_base(repo: Path) -> str:
    for name in ("main", "master"):
        if ref_exists(repo, name):
            return name
    return "HEAD"


def ref_exists(repo: Path, ref: str) -> bool:
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref.split("/"):
        return False
    code, _, _ = _git(repo, ["rev-parse", "--verify", "--end-of-options", ref])
    return code == 0


def diff_ref(repo: Path, ref: str, base: str) -> tuple[str, bool]:
    """Three-dot diff text, and whether it was cut at 5 MB."""
    code, out, _ = _git(repo, ["diff", "--no-ext-diff", "--unified=3", f"{base}...{ref}"])
    if code != 0:
        raise LedgerError(400, "bad_ref", "That ref cannot be diffed.")
    return cap_diff(out)


def try_diff_ref(repo: Path, ref: str, base: str) -> tuple[str, bool] | None:
    """``None`` when the ref is not in the checkout. An empty diff is a real result."""
    if not ref_exists(repo, ref) or not ref_exists(repo, base):
        return None
    try:
        return diff_ref(repo, ref, base)
    except LedgerError:
        return None


def file_texts(repo: Path, ref: str, paths: list[str]) -> tuple[tuple[str, str], ...]:
    """Full text of changed Swift files, so a switch outside the hunk can still be seen."""
    if not ref_exists(repo, ref):
        return ()
    found = []
    for path in paths:
        if not path.endswith(".swift") or not _safe_path(path):
            continue
        if len(found) >= _TEXT_FILES:
            break
        code, out, _ = _git(repo, ["show", "--no-ext-diff", f"{ref}:{path}"])
        if code != 0 or "\0" in out or len(out) > _TEXT_CAP:
            continue
        found.append((path, out))
    return tuple(found)


def load_ref_subject(repo: Path, *, kind: str, ref: str, base: str | None) -> tuple[Subject, bool]:
    """Diff one branch or commit the user named. A missing ref is an error.

    The subject repo is the checkout's ``origin`` when that remote is a
    GitHub ``owner/name``. The bool is true when the diff was truncated.
    """
    if kind == "sha":
        ref = clean_sha(ref)
        label = "Commit SHA"
    else:
        ref = clean_ref(ref, what="Branch")
        label = "Branch"
    base_ref = clean_ref(base, what="Base") if base else default_base(repo)
    if not ref_exists(repo, ref):
        raise LedgerError(400, "bad_ref", f"{label} is not in the checkout.")
    if not ref_exists(repo, base_ref):
        raise LedgerError(400, "bad_ref", "That base is not in the checkout.")
    diff, truncated = diff_ref(repo, ref, base_ref)
    paths = [item.path for item in parse_diff(diff)]
    texts = file_texts(repo, ref, paths)
    remote = checkout_remote(repo)
    if kind == "sha":
        subject = Subject(
            key=f"sha:{ref}", kind="sha", repo=remote, sha=ref, files=tuple(paths), diff=diff, texts=texts
        )
    else:
        subject = Subject(
            key=f"branch:{ref}",
            kind="branch",
            repo=remote,
            branch=ref,
            files=tuple(paths),
            diff=diff,
            texts=texts,
        )
    return subject, truncated


def _git(repo: Path, args: list[str]) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", "git failed"
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _parse_file(part: str) -> FileDiff | None:
    lines = part.splitlines()
    path = _path_from_headers(lines)
    if not path or not _safe_path(path):
        return None
    preamble: list[str] = []
    hunks: list[Hunk] = []
    current: dict | None = None
    body: list[str] = []
    for line in lines:
        match = _HUNK.match(line)
        if match:
            if current is not None:
                hunks.append(_hunk_from(current, body))
            current = {
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2)) if match.group(2) is not None else 1,
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4)) if match.group(4) is not None else 1,
                "intro": (match.group(5) or "").strip(),
            }
            body = []
            continue
        if current is None:
            preamble.append(line)
            continue
        if line.startswith("\\"):
            continue
        body.append(line)
    if current is not None:
        hunks.append(_hunk_from(current, body))
    header = "\n".join(preamble)
    added = "new file mode " in header or "\n--- /dev/null" in f"\n{header}" or header.startswith("--- /dev/null")
    if not added and hunks and all(hunk.old_count == 0 and hunk.old_start == 0 for hunk in hunks):
        added = True
    return FileDiff(path=path, added=added, hunks=tuple(hunks))


def _hunk_from(header: dict, body: list[str]) -> Hunk:
    return Hunk(
        old_start=header["old_start"],
        old_count=header["old_count"],
        new_start=header["new_start"],
        new_count=header["new_count"],
        intro=header["intro"],
        lines=tuple(body),
    )


def _path_from_headers(lines: list[str]) -> str | None:
    new_path = None
    old_path = None
    for line in lines:
        if line.startswith("diff --git "):
            pair = _split_ab(line)
            if pair:
                old_path, new_path = pair
        elif line.startswith("+++ "):
            raw = _unquote(line[4:].strip())
            if raw != "/dev/null":
                new_path = raw[2:] if raw.startswith("b/") else raw
        elif line.startswith("--- "):
            raw = _unquote(line[4:].strip())
            if raw != "/dev/null":
                old_path = raw[2:] if raw.startswith("a/") else raw
        elif line.startswith("@@"):
            break
    return new_path or old_path


def _split_ab(line: str) -> tuple[str, str] | None:
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        try:
            left, right = _split_quoted(rest)
        except ValueError:
            return None
    else:
        idx = rest.find(" b/")
        if idx < 0:
            return None
        left, right = rest[:idx], rest[idx + 1 :]
    if left.startswith("a/"):
        left = left[2:]
    if right.startswith("b/"):
        right = right[2:]
    return _unquote(left), _unquote(right)


def _split_quoted(text: str) -> tuple[str, str]:
    parts = []
    buf = []
    quoted = False
    escape = False
    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and quoted:
            escape = True
            buf.append(ch)
            continue
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
            if not quoted:
                parts.append(_unquote("".join(buf)))
                buf = []
            continue
        if ch == " " and not quoted:
            continue
        buf.append(ch)
    if len(parts) != 2:
        raise ValueError(text)
    return parts


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        body = path[1:-1]
        try:
            return bytes(body, "utf-8").decode("unicode_escape")
        except UnicodeError:
            return body
    return path


def _safe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/")


def _end(start: int, count: int) -> int:
    if count <= 0:
        return start
    return start + count - 1


def _short(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) > _SNIPPET:
        return flat[: _SNIPPET - 3] + "..."
    return flat


def _snippet(hunk: Hunk) -> str:
    added = [line[1:] for line in hunk.lines if line.startswith("+")]
    chosen = added or [line[1:] for line in hunk.lines if line.startswith("-")]
    if not chosen:
        chosen = [line[1:] if line[:1] in "+- " else line for line in hunk.lines]
    return _short(" ".join(part.strip() for part in chosen if part.strip()))


def _party(subject: Subject) -> dict:
    if subject.kind == "pr":
        return {"task_id": subject.task_id, "repo": subject.repo, "pr": subject.pr}
    if subject.kind == "branch":
        return {"branch": subject.branch}
    return {"sha": subject.sha}


def _location(subject: Subject, path: str, line: int | None, snippet: str) -> dict:
    body = _party(subject)
    body["path"] = path
    if line is not None:
        body["line"] = line
    if snippet:
        body["snippet"] = snippet
    return body


def _touched(subject: Subject, files: list[FileDiff]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for path in subject.files:
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            paths.append(path)
    for file_diff in files:
        if file_diff.path not in seen:
            seen.add(file_diff.path)
            paths.append(file_diff.path)
    return paths


def _file_overlaps(parsed: list[tuple[Subject, list[FileDiff]]]) -> list[dict]:
    order: list[str] = []
    parties: dict[str, list[tuple[Subject, dict]]] = {}
    for subject, files in parsed:
        for path in _touched(subject, files):
            if path not in parties:
                parties[path] = []
                order.append(path)
            party = _party(subject)
            if party in [item[1] for item in parties[path]]:
                continue
            parties[path].append((subject, party))
    overlaps = []
    for path in sorted(order):
        group = parties[path]
        if len(group) < 2:
            continue
        item: dict = {
            "path": path,
            "prs": [party for subject, party in group if subject.kind == "pr"],
            "_keys": [subject.key for subject, _party in group],
        }
        branches = [subject.branch for subject, _party in group if subject.kind == "branch" and subject.branch]
        shas = [subject.sha for subject, _party in group if subject.kind == "sha" and subject.sha]
        if branches:
            item["branches"] = branches
        if shas:
            item["shas"] = shas
        overlaps.append(item)
    return overlaps


def _hunk_report(parsed: list[tuple[Subject, list[FileDiff]]], paths: set[str]) -> list[dict]:
    by_path: dict[str, list[tuple[Subject, Hunk]]] = {path: [] for path in paths}
    touch_keys: dict[str, list[str]] = {path: [] for path in paths}
    for subject, files in parsed:
        touched = set(_touched(subject, files))
        for path in paths:
            if path in touched and subject.key not in touch_keys[path]:
                touch_keys[path].append(subject.key)
        for file_diff in files:
            if file_diff.path not in by_path:
                continue
            for hunk in file_diff.hunks:
                if len(by_path[file_diff.path]) >= _HUNKS_CAP:
                    break
                by_path[file_diff.path].append((subject, hunk))
    report = []
    for path in sorted(by_path):
        rows = by_path[path]
        if not rows:
            continue
        hunks = []
        for subject, hunk in rows:
            body = _party(subject)
            body.update(
                {
                    "start": hunk.new_start,
                    "end": _end(hunk.new_start, hunk.new_count),
                    "old_start": hunk.old_start,
                    "old_end": _end(hunk.old_start, hunk.old_count),
                    "snippet": _snippet(hunk),
                }
            )
            hunks.append(body)
        report.append({"path": path, "hunks": hunks, "_keys": touch_keys.get(path) or []})
    return report


def _migration_hits(subject: Subject, regex) -> list[tuple[str, str, int | None, str]]:
    hits = []
    for file_diff in parse_diff(subject.diff):
        if file_diff.added:
            ident = _path_id(file_diff.path, regex)
            if ident:
                hits.append((ident, file_diff.path, None, file_diff.path))
        if not _migration_source(file_diff.path, regex):
            continue
        sql = file_diff.path.lower().endswith(".sql")
        in_block = False
        for line_no, added, text in _new_lines(file_diff):
            visible, in_block = _strip_comments(text, in_block, sql=sql)
            if not added or not visible.strip():
                continue
            ident = _id_in(visible, regex)
            if ident:
                hits.append((ident, file_diff.path, line_no, _short(visible)))
    return hits


def _migration_source(path: str, regex) -> bool:
    """Swift, SQL, or a path the migration regex matches through the end."""
    lower = path.lower()
    if lower.endswith(".swift") or lower.endswith(".sql"):
        return True
    return _path_id(path, regex) is not None


def _path_id(path: str, regex) -> str | None:
    """An id from a path only when the match consumes the whole path.

    ``docs/V7__note.sql.md`` matches a ``.sql`` pattern in the middle and is
    not a migration file.
    """
    match = regex.search(path)
    if not match or match.end() != len(path):
        return None
    return _ident(match)


def _strip_comments(text: str, in_block: bool, *, sql: bool) -> tuple[str, bool]:
    """Drop ``//`` and ``/* */``. SQL also drops ``--`` and ``#``."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if in_block:
            end = text.find("*/", index)
            if end < 0:
                return "".join(out), True
            index = end + 2
            in_block = False
            continue
        if text.startswith("/*", index):
            in_block = True
            index += 2
            continue
        if text.startswith("//", index):
            break
        if sql and (text.startswith("--", index) or text[index] == "#"):
            break
        out.append(text[index])
        index += 1
    return "".join(out), in_block


def _id_in(text: str, regex) -> str | None:
    match = regex.search(text)
    if not match:
        return None
    return _ident(match)


def _ident(match) -> str | None:
    raw = _first_group(match)
    if not raw:
        return None
    ident = raw.strip().casefold()
    if not ident or len(ident) > 80:
        return None
    return ident


def _first_group(match) -> str | None:
    if match.lastindex:
        for index in range(1, match.lastindex + 1):
            piece = match.group(index)
            if piece:
                return piece
        return None
    return match.group(0)


def _new_lines(file_diff: FileDiff):
    for hunk in file_diff.hunks:
        line_no = hunk.new_start
        for line in hunk.lines:
            if line.startswith("-"):
                continue
            if line.startswith("\\"):
                continue
            added = line.startswith("+")
            text = line[1:] if line[:1] in "+ " else line
            yield line_no, added, text
            line_no += 1


def _added_cases(file_diff: FileDiff) -> list[tuple[str, str, int, str]]:
    rows = []
    for hunk in file_diff.hunks:
        line_no = hunk.new_start
        for line in hunk.lines:
            if line.startswith("-") or line.startswith("\\"):
                continue
            added = line.startswith("+")
            text = line[1:] if line[:1] in "+ " else line
            rows.append((line_no, added, text, hunk.intro))
            line_no += 1
    found = []
    seen = set()
    for index, (line_no, added, text, intro) in enumerate(rows):
        if not added:
            continue
        match = _CASE.match(text)
        if not match:
            continue
        enum_name = _enum_on_same_line(text) or _enclosing_enum(rows, index, intro)
        if not enum_name:
            continue
        for name in _case_names(match.group(1)):
            marker = (enum_name, name, line_no)
            if marker in seen:
                continue
            seen.add(marker)
            found.append((enum_name, name, line_no, _short(text)))
    return found


def _case_names(rest: str) -> list[str]:
    rest = rest.split("//", 1)[0]
    names = []
    for part in rest.split(","):
        piece = part.strip()
        if not piece or piece.startswith("."):
            continue
        match = _CASE_WORD.match(piece)
        if not match:
            continue
        name = match.group(1)
        if name in _CASE_KEYWORDS:
            continue
        names.append(name)
    return names


def _enum_on_same_line(text: str) -> str | None:
    match = re.search(r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)\b[^{]*\{[^}]*\bcase\b", text)
    return match.group(1) if match else None


def _enum_on_line(text: str) -> str | None:
    match = _ENUM.search(text)
    return match.group(1) if match else None


def _bare_brace(text: str) -> bool:
    return text.strip() == "{"


def _enclosing_enum(rows, index: int, intro: str) -> str | None:
    depth = 0
    for cursor in range(index - 1, -1, -1):
        text = rows[cursor][2]
        for ch in reversed(text):
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth == 0:
                    found = _enum_on_line(text)
                    if found:
                        return found
                    # `func title() -> String {` is a method, not the enum
                    # on the previous line. A brace that is the whole line
                    # still belongs to the declaration above it.
                    if _bare_brace(text) and cursor and not _MEMBER.search(rows[cursor - 1][2]):
                        return _enum_on_line(rows[cursor - 1][2])
                    return None
                depth -= 1
    if not intro or _MEMBER.search(intro):
        return None
    return _enum_on_line(intro)


def _bindings_for(subject: Subject, files: list[FileDiff], path: str) -> dict[str, str]:
    texts = dict(subject.texts)
    if path in texts:
        return _bindings(texts[path])
    for file_diff in files:
        if file_diff.path == path:
            return _bindings(_visible_text(file_diff))
    return {}


def _visible_text(file_diff: FileDiff) -> str:
    lines = []
    for hunk in file_diff.hunks:
        for line in hunk.lines:
            if line.startswith("-") or line.startswith("\\"):
                continue
            lines.append(line[1:] if line[:1] in "+ " else line)
    return "\n".join(lines)


def _switch_sites(subject: Subject, files: list[FileDiff]):
    texts = dict(subject.texts)
    seen: set[str] = set()
    by_path = {file_diff.path: file_diff for file_diff in files}
    for path in _touched(subject, files):
        if path in seen:
            continue
        seen.add(path)
        if path in texts:
            for line, expr, body in _switches_in(texts[path]):
                yield path, line, expr, body
            continue
        file_diff = by_path.get(path)
        if file_diff is None:
            continue
        for line, expr, body in _switches_in_hunks(file_diff):
            yield path, line, expr, body


def _switches_in_hunks(file_diff: FileDiff):
    for hunk in file_diff.hunks:
        lines = []
        numbers = []
        line_no = hunk.new_start
        for line in hunk.lines:
            if line.startswith("-") or line.startswith("\\"):
                continue
            text = line[1:] if line[:1] in "+ " else line
            lines.append(text)
            numbers.append(line_no)
            line_no += 1
        blob = "\n".join(lines)
        for match in _SWITCH.finditer(blob):
            prefix = blob.count("\n", 0, match.start())
            file_line = numbers[prefix] if prefix < len(numbers) else hunk.new_start
            body = _brace_body(blob, match.end() - 1)
            yield file_line, match.group(1).strip(), body


def _switches_in(text: str):
    for match in _SWITCH.finditer(text):
        brace = match.end() - 1
        body = _brace_body(text, brace)
        line = text.count("\n", 0, match.start()) + 1
        yield line, match.group(1).strip(), body


def _brace_body(text: str, open_index: int) -> str:
    depth = 0
    for index in range(open_index, len(text)):
        ch = text[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return text[open_index + 1 :]


def _handles(body: str, case_name: str) -> bool:
    if re.search(r"@unknown\s+default\b|\bdefault\s*:", body):
        return True
    # `case .color`, `case (.color, _)`, and `case let (.color, x)` all handle it.
    needle = re.compile(rf"\.{re.escape(case_name)}\b")
    for line in body.splitlines():
        if re.match(r"\s*case\b", line) and needle.search(line):
            return True
    return False


def _bindings(text: str) -> dict[str, str]:
    found = {}
    for match in _BIND.finditer(text):
        name, typ = match.group(1), match.group(2)
        if name in _SKIP_BIND:
            continue
        found[name] = typ
    return found


def _switch_over(expr: str, type_name: str, bindings: dict[str, str]) -> bool:
    if re.search(rf"\b{re.escape(type_name)}\b", expr):
        return True
    for ident in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr):
        if bindings.get(ident) == type_name:
            return True
    return False
