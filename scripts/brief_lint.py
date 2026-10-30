import argparse
import re
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
        commit = re.search(r"\bcommit(?:s|ting)?\b", line, re.IGNORECASE)
        for clause in re.split(r"[;.!?]\s+|\b(?:but|however)\b", line, flags=re.IGNORECASE):
            matches = list(ATTRIBUTION.finditer(clause))
            if not prompt or commit:
                matches.extend(VENDOR.finditer(clause))
            for match in matches:
                if not prompt or not PROHIBITION.search(clause[:match.start()]):
                    return True
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
        if "/" in path and not re.search(r"[*?]", path.split("/", 1)[0]):
            directories.add(path.split("/", 1)[0])

    title = spec.get("TITLE", "")
    if len(title) > 72 or title.endswith("."):
        report("title", "TITLE must be at most 72 characters and must not end with a period")

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
