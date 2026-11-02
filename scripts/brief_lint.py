import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import TypedDict


class Finding(TypedDict):
    check: str
    message: str


REQUIRED = (
    "SLUG", "REPO_URL", "BASE", "MODEL", "EFFORT", "TIMEOUT_MIN",
    "ALLOWED_PATHS", "TEST_CMD", "PROMPT_FILE", "TITLE",
)
VENDORS = (
    "Clau" + "de", "Cur" + "sor", "Co" + "dex", "Open" + "AI",
    "Chat" + "GPT", "G" + "PT", "Co" + "pilot", "GitHub" + "Co" + "pilot",
    "Gem" + "ini", "Anth" + "ropic", "Wind" + "surf", "Gro" + "k",
)
ATTRIBUTION = re.compile(
    "|".join(re.escape(phrase) for phrase in (
        "Co-authored" + "-by", "Co-authored" + " by",
        "Generated" + " with", "Generated" + " by", "Generated" + "-by",
        "Assisted" + "-by", "Authored" + "-by", "Written" + "-by",
    )) + r"|\b[\w.+-]*noreply[\w.+-]*@[\w.-]+|\b[\w.+-]+@[\w.-]*noreply[\w.-]*",
    re.IGNORECASE,
)
VENDOR = re.compile(r"\b(?:" + "|".join(map(re.escape, VENDORS)) + r")\b", re.IGNORECASE)
BARE_PYTHON = re.compile(
    r"(?:^|&&|\|\||[;|(`])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:exec\s+|env\s+)?"
    r"(python3?(?:\.\d+)?|pip3?(?:\.\d+)?)(?![\w./-])"
)
PIP_INSTALL = re.compile(r"(?:^|[\s/])(?:pip3?(?:\.\d+)?|-m\s+pip)\s+install\b(?P<args>.*)")
PIP_OPTION_WITH_VALUE = {"-r", "--requirement", "-c", "--constraint", "-t", "--target", "--prefix", "-i", "--index-url"}
TREE_CLEANUP = re.compile(r"\$\?.*rm\s+-rf\b(?=[^;&|]*\bbuild\b)(?=[^;&|]*egg-info)[^;&|]*.*\bexit\s+\"?\$", re.DOTALL)


def _pip_installs_tree(test_cmd: str) -> bool:
    """pip install of the worktree itself writes build/ and *.egg-info into it."""
    for segment in re.split(r"&&|\|\||[;|]", test_cmd):
        match = PIP_INSTALL.search(segment)
        if not match:
            continue
        args = match.group("args").split()
        skip = False
        for arg in args:
            if skip:
                skip = False
            elif arg in PIP_OPTION_WITH_VALUE:
                skip = True
            elif re.fullmatch(r"\.(?:/)?(?:\[[\w,-]+\])?", arg):
                return not TREE_CLEANUP.search(test_cmd)
    return False


COMMIT = re.compile(
    r"^\s*(?:[-*]\s*|\d+[.)]\s*)?(?:(?:then|and|also|please|finally|now|next|first)\s+)*"
    r"(?:you\s+(?:should|must|need\s+to|can)\s+)?"
    r"(?:(?:run\s+)?git\s+(?:commit|push)\b|(?:commit|push)\s+(?:it|this|them|the|your|all|each|everything|with|as|to)\b"
    r"|(?:make|create)\s+(?:one|a|single|new|exactly\s+one)\s+commit\b|(?:one|single|exactly\s+one)\s+commit\b)"
    r"|\brun\s+git\s+(?:commit|push)\b",
    re.IGNORECASE,
)
GET_ONLY_ENVIRONMENT = (
    "colorSchemeContrast", "accessibilityReduceTransparency", "accessibilityReduceMotion",
    "accessibilityDifferentiateWithoutColor", "accessibilityInvertColors",
)
PROHIBITION = re.compile(r"\b(?:do\s+not|don't|never|must\s+not|no)\b", re.IGNORECASE)
EXTENSIONS = {
    ".py", ".sh", ".md", ".swift", ".ts", ".json", ".txt", ".yaml", ".yml",
    ".tsx", ".jsx", ".js", ".css", ".html", ".toml", ".sql", ".go", ".rs",
    ".c", ".h", ".cpp", ".java", ".kt", ".rb", ".xml", ".csv", ".mdx",
    ".svg", ".png", ".jpg", ".webp", ".ini", ".env", ".lock", ".zsh",
}
REPO_DIRS = {
    "src", "scripts", "tests", "shuin", "docs", "lib", "app", "apps", "packages",
    "tools", "include", "assets", "public", "config", "components", "pages",
    "server", "client", "ios", "android", "migrations", "resources", "fixtures",
    "skills", ".github", "githooks",
}


def _glob(pattern: str) -> re.Pattern:
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern.endswith("/"):
        pattern += "**"
    parts = []
    i = 0
    while i < len(pattern):
        if pattern[i:i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + "(?:/.*)?")


def _has_attribution(text: str, prompt: bool) -> bool:
    for line in text.splitlines():
        words = re.sub(r"\S*/\S*", " ", line)
        commit = re.search(r"\bcommit(?:s|ting)?\b", words, re.IGNORECASE)
        for clause in re.split(r"[;.!?]\s+|\b(?:but|however)\b", line, flags=re.IGNORECASE):
            matches = list(ATTRIBUTION.finditer(clause))
            if not prompt or commit:
                matches.extend(VENDOR.finditer(clause))
            for match in matches:
                if not prompt or not PROHIBITION.search(clause[:match.start()]):
                    return True
    return False


BRIDGE_LIB = Path(os.environ.get(
    "BRIEF_LINT_BRIDGE_LIB", "/workspace/spike-setup/bridge-wiring-fix/taco-live/bridge-lib.sh"))
BRIDGE_KEYS = ("SLUG", "REPO_URL", "BASE", "MODEL", "EFFORT", "TIMEOUT_MIN",
               "ALLOWED_PATHS", "TEST_CMD", "PROMPT_FILE", "TITLE", "BODY_FILE")
PARSE_SCRIPT = (
    'eval "$1"; shift; BL_ERRS=0; '
    'bl_verr() { BL_ERRS=$((BL_ERRS + 1)); printf "E%s\\0" "$*"; }; '
    'bl_parse_spec spec.env; rc=$?; '
    'for k in "$@"; do v="S_$k"; printf "V%s=%s\\0" "$k" "${!v}"; done; exit $rc'
)


def _bridge_functions(text: str) -> str:
    """bl_trim and bl_parse_spec, cut from bridge-lib.sh by their definition lines."""
    found = [re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
             for name in ("bl_trim", "bl_parse_spec")]
    return "\n".join(match.group(0) for match in found if match) if all(found) else ""


def _spec_parse(folder: Path) -> list[str]:
    """Parse spec.env with the bridge's own bl_parse_spec. It expands nothing."""
    if not BRIDGE_LIB.is_file():
        return [f"bridge-lib.sh not found at {BRIDGE_LIB}; set BRIEF_LINT_BRIDGE_LIB"]
    functions = _bridge_functions(BRIDGE_LIB.read_text(encoding="utf-8"))
    if not functions:
        return [f"bl_trim or bl_parse_spec missing from {BRIDGE_LIB}"]
    try:
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", PARSE_SCRIPT, "_", functions, *BRIDGE_KEYS],
            cwd=folder, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ["bl_parse_spec did not finish in 10 seconds"]
    records = [record for record in result.stdout.split("\0") if record]
    problems = [record[1:] for record in records if record[0] == "E"]
    if result.returncode and not problems:
        problems.append(f"bl_parse_spec exited {result.returncode}")
    values = dict(record[1:].split("=", 1) for record in records if record[0] == "V")
    present = {key.strip() for key in re.findall(r"^\s*([^#=\n][^=\n]*)=", (folder / "spec.env").read_text(encoding="utf-8"), re.MULTILINE)}
    for key in BRIDGE_KEYS:
        value = values.get(key, "")
        if key in present and not value:
            problems.append(f"{key} is empty after bl_parse_spec")
        if "'\\''" in value:
            problems.append(f"{key} keeps a literal '\\'' sequence; bl_parse_spec does not unescape it")
        for quote in "'\"":
            if (value.startswith(quote) or value.endswith(quote)) and value.count(quote) % 2:
                problems.append(f"{key} has an unmatched {quote} after bl_parse_spec")
    return problems


def _asks_to_commit(prompt: str) -> bool:
    for line in prompt.splitlines():
        for sentence in re.split(r"[;.!?]\s+|\b(?:but|however)\b", line, flags=re.IGNORECASE):
            start = 0
            for piece in re.split(r"([,:]\s+)", sentence):
                match = COMMIT.search(piece)
                if match and not PROHIBITION.search(sentence[:start + match.start()]):
                    return True
                start += len(piece)
    return False


def lint(folder: Path) -> list[Finding]:
    findings: list[Finding] = []

    def report(check: str, message: str) -> None:
        findings.append({"check": check, "message": message})

    try:
        spec_text = (folder / "spec.env").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [{"check": "missing-key", "message": "spec.env is missing or unreadable"}]
    spec = {}
    for number, line in enumerate(spec_text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            report("missing-key", f"spec.env line {number}: expected KEY=VALUE")
            continue
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                report("missing-key", f"spec.env line {number}: unmatched quote")
                continue
            value = value[1:-1]
        spec[key] = value
    if (folder / "spec.env").is_file():
        for problem in _spec_parse(folder):
            report("spec-parse", problem)
    for key in REQUIRED:
        if not spec.get(key, "").strip():
            report("missing-key", f"{key} is required and must be non-empty")

    slug = spec.get("SLUG", "")
    if slug.strip() and not re.fullmatch(r"tflaim/[A-Za-z0-9._-]+", slug):
        report("slug-prefix", "SLUG must match tflaim/<letters, digits, dot, dash, underscore>")
    repo = spec.get("REPO_URL", "")
    if repo.strip() and not re.fullmatch(r"https://github\.com/tflaim/[A-Za-z0-9._-]+\.git", repo):
        report("repo-url", "REPO_URL must match https://github.com/tflaim/<name>.git")
    base = spec.get("BASE", "")
    if base.strip() and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", base)
        or ".." in base or "//" in base or "@{" in base
        or any(part.startswith(".") or part.endswith((".lock", ".")) for part in base.split("/"))
        or base.endswith("/") or base == slug
    ):
        report("missing-key", "BASE must be a valid branch distinct from SLUG")
    model = spec.get("MODEL", "")
    if model.strip() and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
        report("missing-key", "MODEL must be a non-empty model name")
    effort = spec.get("EFFORT", "")
    if effort.strip() and effort not in {"low", "medium", "high", "xhigh"}:
        report("missing-key", "EFFORT must be low, medium, high, or xhigh")
    timeout = spec.get("TIMEOUT_MIN", "")
    digits = timeout.lstrip("0") or "0"
    if timeout.strip() and (
        not re.fullmatch(r"[0-9]+", timeout) or len(digits) > 3 or not 1 <= int(digits) <= 240
    ):
        report("missing-key", "TIMEOUT_MIN must be a positive integer at most 240")

    allowed = []
    entries = []
    directories = set(REPO_DIRS)
    for entry in spec.get("ALLOWED_PATHS", "").split():
        path = entry[2:] if entry.startswith("./") else entry
        if (
            path in {".", "", "*", "**", "**/*"}
            or path.startswith("/") or ".." in path
            or any(part == "." for part in path.rstrip("/").split("/"))
            or path.lower().split("/", 1)[0] == "githooks"
            or not re.fullmatch(r"[A-Za-z0-9._/*?@+-]+", path)
        ):
            report("broad-paths", f"ALLOWED_PATHS entry is forbidden or too broad: {entry}")
            continue
        allowed.append(_glob(path))
        entries.append(entry)
        if "/" in path and not re.search(r"[*?]", path.split("/", 1)[0]):
            directories.add(path.split("/", 1)[0])

    title = spec.get("TITLE", "")
    if len(title) > 72 or title.endswith("."):
        report("title", "TITLE must be at most 72 characters and must not end with a period")

    if "BRANCH" in spec:
        report("branch-key", "spec.env must not set BRANCH; the bridge derives the branch from SLUG")
    match = BARE_PYTHON.search(spec.get("TEST_CMD", ""))
    if match:
        report(
            "bare-python",
            f"TEST_CMD runs bare {match.group(1)}; use /opt/homebrew/bin/python3.14 or a venv made from it",
        )

    if _pip_installs_tree(spec.get("TEST_CMD", "")):
        report(
            "pip-install-tree",
            "TEST_CMD runs pip install . in the worktree, which writes build/ and *.egg-info there;"
            " install from a temp copy",
        )

    texts = {"TITLE": title}
    for key in ("PROMPT_FILE", "BODY_FILE"):
        if key not in spec or (key == "PROMPT_FILE" and not spec[key].strip()):
            continue
        name = spec[key]
        try:
            path = folder / name
            if not name.strip() or Path(name).is_absolute() or not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError
            texts[key] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError, RuntimeError):
            report("missing-key", f"{key} must name a readable file inside the job folder")

    prompt = texts.get("PROMPT_FILE")
    if prompt is not None:
        seen = set()
        for line in prompt.splitlines():
            line = re.split(r"\b(?:do not edit|out of scope)\b", line, maxsplit=1, flags=re.IGNORECASE)[0]
            for token in re.findall(r"[^\s`\"'<>()[\]{},;]+", line):
                if re.match(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|www\.)", token):
                    continue
                token = re.sub(r":\d+(?:[-:]\d+)?$", "", token.rstrip(".!?:"))
                path = token[2:] if token.startswith("./") else token
                if Path(path).suffix.lower() not in EXTENSIONS and not (
                    "/" in path and path.split("/", 1)[0] in directories
                ):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                if spec.get("ALLOWED_PATHS", "").strip() and (
                    path.startswith("/") or ".." in path.split("/")
                    or not any(pattern.fullmatch(path) for pattern in allowed)
                ):
                    report("prompt-outside-paths", f"{path}: the diff gate would reject this file")
        for label, pattern in (
            ("Goal or Bug", r"^(?:Goal|Bug)[.:]"),
            ("Data shape", r"^Data shape[.:]"),
            ("Change", r"^Change[.:]"),
            ("Tests", r"^Tests[.:]"),
            ("exact counts", r"\bexact counts\b"),
        ):
            if not re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE):
                report("missing-section", f"Prompt must contain {label}")
    if prompt is not None:
        if _asks_to_commit(prompt):
            report("commit-instruction", "Prompt tells the job to commit or push; the bridge commits")
        listed = set(re.findall(r"[^\s`\"'<>()[\]{},;]+", prompt))
        listed |= {token.rstrip(".:") for token in listed}
        missing = [entry for entry in entries if entry not in listed]
        if missing:
            report("paths-not-inline", "Prompt must list every ALLOWED_PATHS entry: " + " ".join(missing))
        everything = prompt + " " + spec.get("ALLOWED_PATHS", "")
        if "web/vendor/" in everything and not (
            "web/vendor/manifest.json" in prompt and re.search(r"\bsha-?256\b", prompt, re.IGNORECASE)
            and re.search(r"\btests?\b", prompt, re.IGNORECASE)
        ):
            report(
                "vendor-manifest",
                "Brief touches web/vendor/ but does not require a test that every web/vendor/manifest.json"
                " entry exists with a matching sha256",
            )
        if ".swift" in everything:
            for key in GET_ONLY_ENVIRONMENT:
                if re.search(r"\.environment\(\s*\\\." + key + r"\b", prompt) and not re.search(
                    r"\bget-only\b", prompt, re.IGNORECASE
                ):
                    report("swift-readonly-env", f"Prompt writes get-only EnvironmentValues.{key}; inject it instead")
                elif key in prompt and not re.search(r"\binject", prompt, re.IGNORECASE):
                    report(
                        "swift-readonly-env",
                        f"Prompt names get-only EnvironmentValues.{key} but not the injected init or style input tests use",
                    )
    for source, text in texts.items():
        if _has_attribution(text, prompt=source == "PROMPT_FILE"):
            report("attribution", f"{source} requests or contains forbidden attribution")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a bridge job brief before queueing.")
    parser.add_argument("folder", type=Path)
    findings = lint(parser.parse_args().folder)
    for finding in findings:
        print(f"{finding['check']}: {finding['message']}")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
