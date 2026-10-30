"""Fail when a head, SHA, or epoch comparison appears outside seal.py."""

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "shuin"
HOME = "shuin/seal.py"

_SHA_REF = (
    r"(?:[A-Za-z_]\w*\.)?"
    r"(?:head_sha_reread|commit_sha|opened_sha|head_sha|tree_sha|pending_head|opened_epoch|head_epoch)"
)
_SHA_EXPR = (
    rf"(?:lower\s*\(\s*trim\s*\(\s*{_SHA_REF}\s*\)\s*\)"
    rf"|trim\s*\(\s*{_SHA_REF}\s*\)"
    rf"|lower\s*\(\s*{_SHA_REF}\s*\)"
    rf"|{_SHA_REF})"
)
_ATOM = r"(?:NULL|\?|'[^']*'|\d+)"
_OTHER = (
    rf"(?:{_SHA_EXPR}"
    rf"|lower\s*\(\s*trim\s*\(\s*{_ATOM}\s*\)\s*\)"
    rf"|lower\s*\(\s*{_ATOM}\s*\)"
    rf"|trim\s*\(\s*{_ATOM}\s*\)"
    rf"|{_ATOM}"
    rf"|\(\s*SELECT\b[^;]*?\))"
)
_OP = r"(?:<>|!=|==|=|IS\s+NOT|IS)"
_SQL_CMP = re.compile(
    rf"(?P<lhs>{_SHA_EXPR})\s*(?P<op>{_OP})\s*(?P<rhs>{_OTHER})"
    rf"|(?P<lhs2>{_OTHER})\s*(?P<op2>{_OP})\s*(?P<rhs2>{_SHA_EXPR})",
    re.IGNORECASE,
)
_CREATE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+INDEX|INDEX|TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_SHA_FIELDS = {
    "head_sha",
    "commit_sha",
    "opened_sha",
    "head_sha_reread",
    "tree_sha",
    "pending_head",
    "opened_epoch",
    "head_epoch",
}
_SHA_NAMES = {"sha1", "sha2", "head1", "head2", "head_sha", "old_head", "opened_sha"}
_ORDER = (ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)


@dataclass(frozen=True)
class Match:
    path: str
    function: str
    line: int
    text: str


def _norm(text: str) -> str:
    return " ".join(text.split())


def _is_sha_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id in _SHA_NAMES:
        return True
    if isinstance(node, ast.Attribute) and node.attr in _SHA_FIELDS:
        return True
    if isinstance(node, ast.Subscript):
        sliver = node.slice
        if isinstance(sliver, ast.Constant) and sliver.value in _SHA_FIELDS:
            return True
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "int" and _is_sha_expr(node.args[0]):
            return True
        if isinstance(func, ast.Attribute) and func.attr in ("lower", "strip") and _is_sha_expr(node.args[0]):
            return True
    if isinstance(node, (ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.IfExp)):
        children = []
        if isinstance(node, ast.BoolOp):
            children = list(node.values)
        elif isinstance(node, ast.BinOp):
            children = [node.left, node.right]
        elif isinstance(node, ast.UnaryOp):
            children = [node.operand]
        else:
            children = [node.test, node.body, node.orelse]
        return any(_is_sha_expr(item) for item in children)
    return False


def _same_sha_names(tree: ast.AST) -> set[str]:
    """same_sha, plus a local name bound to it with `import ... as` or an assignment."""
    names = {"same_sha"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "same_sha":
                    names.add(alias.asname or alias.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not _same_sha_value(node.value, names):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _same_sha_value(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr == "same_sha"
    return False


def _same_sha(node: ast.AST, names: set[str]) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id in names
    if isinstance(func, ast.Attribute):
        return func.attr == "same_sha"
    return False


def _function_at(spans: list[tuple[int, int, str]], line: int) -> str:
    name = "<module>"
    start_at = -1
    for start, end, fname in spans:
        if start <= line <= end and start >= start_at:
            start_at = start
            name = fname
    return name


def _sql_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _SQL_CMP.finditer(text):
        if spans and match.start() < spans[-1][1]:
            continue
        spans.append((match.start(), match.end()))
    return spans


def _scan_python(path: Path, rel: str):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = _same_sha_names(tree)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _same_sha(node, names):
            segment = ast.get_source_segment(source, node)
            if segment:
                yield Match(rel, _function_at(spans, node.lineno), node.lineno, _norm(segment))
        elif isinstance(node, ast.Compare) and any(isinstance(op, _ORDER) for op in node.ops):
            operands = [node.left, *node.comparators]
            if any(_is_sha_expr(item) for item in operands):
                segment = ast.get_source_segment(source, node)
                if segment:
                    yield Match(rel, _function_at(spans, node.lineno), node.lineno, _norm(segment))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for _start, _end in _sql_spans(node.value):
                yield Match(rel, _function_at(spans, node.lineno), node.lineno, _norm(node.value))


def _scan_sql(path: Path, rel: str):
    current = "<module>"
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        code = line.split("--", 1)[0]
        created = _CREATE.search(code)
        if created:
            current = created.group(1)
        for _start, _end in _sql_spans(code):
            yield Match(rel, current, lineno, _norm(code))


def scan() -> list[Match]:
    found: list[Match] = []
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in {".py", ".sql"} or not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == HOME:
            continue
        if path.suffix == ".py":
            found.extend(_scan_python(path, rel))
        else:
            found.extend(_scan_sql(path, rel))
    return found


# path, function, count, reason. A copied comparison in that function exceeds the count.
ALLOW = (
    (
        "shuin/db.py",
        "<module>",
        4,
        "The evidence view SQL is the same projection migration 10 installs.",
    ),
    (
        "shuin/db.py",
        "_migrate_fresh_run",
        8,
        "Version 5 backfill copies the head onto runs opened at that head and fills an empty epoch.",
    ),
    (
        "shuin/ledger.py",
        "sync_seal_cache",
        4,
        "The seal cache records that GitHub replaced the stored head.",
    ),
    (
        "shuin/ledger.py",
        "apply_github_snapshot",
        9,
        "A poll records that the cached head changed so the epoch can move, confirms a pending commit, and writes the new head.",
    ),
    (
        "shuin/ledger.py",
        "_claim_once",
        2,
        "Claim writes the opened sha and epoch. It does not compare them to the head.",
    ),
    (
        "shuin/ledger.py",
        "_pass_kind_for",
        3,
        "Finds a prior full pass of the same patch or tree on another commit.",
    ),
    (
        "shuin/ledger.py",
        "_logged_run",
        2,
        "Loads the test log for one commit. It does not decide whether that commit is the head.",
    ),
    (
        "shuin/ledger.py",
        "_run_proof",
        1,
        "Loads the test log written for the claimed commit.",
    ),
    (
        "shuin/ledger.py",
        "_evidence_cite",
        1,
        "Loads the latest evidence row for one commit.",
    ),
    (
        "shuin/ledger.py",
        "_has_matching_evidence",
        1,
        "A queued row that already has evidence for its commit is shown as evidence_ready.",
    ),
    (
        "shuin/ledger.py",
        "_backfill_missing_heads",
        8,
        "Fills an empty PR head from a receipt. It does not decide whether a seal is fresh.",
    ),
    (
        "shuin/ledger.py",
        "_note_pending_head",
        2,
        "A pending commit is stored only while the head is still empty.",
    ),
    (
        "shuin/ledger.py",
        "_receipt_head",
        1,
        "Backfill uses a receipt whose two reads name one commit.",
    ),
    (
        "shuin/schema.sql",
        "evidence_v",
        4,
        "evidence_v projects staleness with lower(trim()). The board asks seal.fresh.",
    ),
)


def test_head_freshness_checks_live_in_seal():
    matches = scan()
    got = Counter((item.path, item.function) for item in matches)
    want: Counter = Counter()
    for path, function, count, reason in ALLOW:
        assert reason.strip()
        want[(path, function)] += count
    problems = []
    for key in sorted(set(got) | set(want)):
        if got[key] != want[key]:
            lines = [f"{item.line}: {item.text}" for item in matches if (item.path, item.function) == key]
            problems.append(f"{key[0]} {key[1]}: got {got[key]} want {want[key]} :: " + " || ".join(lines))
    assert problems == []


def test_aliased_same_sha_is_a_freshness_check(tmp_path):
    path = tmp_path / "alias_check.py"
    path.write_text(
        "from shuin.sanitize import same_sha as commits_match\n"
        "\n"
        "also = commits_match\n"
        "\n"
        "def gate(row):\n"
        "    return commits_match(row.head_sha, row.opened_sha) and also(row.head_sha, row.opened_sha)\n",
        encoding="utf-8",
    )
    matches = list(_scan_python(path, "alias_check.py"))
    assert {item.text for item in matches} == {
        "commits_match(row.head_sha, row.opened_sha)",
        "also(row.head_sha, row.opened_sha)",
    }
