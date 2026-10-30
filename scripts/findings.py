import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, message + "\n")


def validate(row, number):
    if not isinstance(row, dict) or set(row) != {
        "id", "date", "repo", "pr", "class", "finding", "found_by", "enforcer",
    }:
        raise ValueError("row must contain id, date, repo, pr, class, finding, found_by and enforcer")
    if number > 9999:
        raise ValueError("log cannot exceed f-9999")
    if row["id"] != f"f-{number:04d}":
        raise ValueError(f"id must be f-{number:04d}")
    if not isinstance(row["date"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row["date"]):
        raise ValueError("date must be a valid YYYY-MM-DD date")
    try:
        date.fromisoformat(row["date"])
    except ValueError:
        raise ValueError("date must be a valid YYYY-MM-DD date") from None
    if not isinstance(row["repo"], str) or not row["repo"].strip():
        raise ValueError("repo must be a nonempty string")
    if type(row["pr"]) is not int or row["pr"] <= 0:
        raise ValueError("pr must be a positive integer")
    if not isinstance(row["class"], str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", row["class"]):
        raise ValueError("class must be a kebab-case slug")
    if not isinstance(row["finding"], str) or not row["finding"].strip() or len(row["finding"]) > 200:
        raise ValueError("finding must contain 1-200 characters")
    if row["found_by"] not in ("forge", "codex", "gauntlet", "human"):
        raise ValueError("found_by must be forge, codex, gauntlet or human")
    enforcer = row["enforcer"]
    if enforcer is not None:
        if not isinstance(enforcer, dict) or set(enforcer) != {"kind", "path", "status", "pr"}:
            raise ValueError("enforcer must contain kind, path, status and pr")
        if enforcer["kind"] not in ("lint", "test", "hook"):
            raise ValueError("enforcer.kind must be lint, test or hook")
        if not isinstance(enforcer["path"], str) or not enforcer["path"].strip():
            raise ValueError("enforcer.path must be a nonempty string")
        if enforcer["status"] not in ("proposed", "shipped"):
            raise ValueError("enforcer.status must be proposed or shipped")
        if enforcer["pr"] is not None and (type(enforcer["pr"]) is not int or enforcer["pr"] <= 0):
            raise ValueError("enforcer.pr must be null or a positive integer")


def read_log(path):
    rows = []
    separator = ""
    with path.open("rb") as log:
        for number, line in enumerate(log, 1):
            try:
                try:
                    row = json.loads(line)
                except ValueError:
                    raise ValueError("invalid JSON") from None
                validate(row, number)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"line {number}: {exc}") from None
            rows.append(row)
            separator = "" if line.endswith(b"\n") else "\n"
    return rows, separator


def main():
    parser = Parser()
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    check = commands.add_parser("check")
    for command in (add, check):
        command.add_argument("--log", type=Path, default=Path(__file__).resolve().parents[1] / "review-findings/log.jsonl")
    for field in ("repo", "pr", "class", "finding", "found-by"):
        add.add_argument(f"--{field}", required=True)
    add.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    args = parser.parse_args()
    try:
        try:
            rows, separator = read_log(args.log)
        except FileNotFoundError:
            if args.command == "check":
                raise
            rows = []
            separator = ""
        if args.command == "check":
            classes = {}
            for row in rows:
                classes.setdefault(row["class"], []).append(row)
            failed = False
            for name, findings in classes.items():
                if len(findings) >= 2 and not any(
                    row["enforcer"] and row["enforcer"]["status"] == "shipped" for row in findings
                ):
                    ids = ", ".join(row["id"] for row in findings)
                    print(f"class {name} has {len(findings)} findings and no shipped check: "
                          f"add a lint, test or hook, then set its enforcer ({ids})", file=sys.stderr)
                    failed = True
            return int(failed)
        try:
            pr = int(args.pr)
        except ValueError:
            raise ValueError("pr must be a positive integer") from None
        row = {
            "id": f"f-{len(rows) + 1:04d}", "date": args.date, "repo": args.repo, "pr": pr,
            "class": getattr(args, "class"), "finding": args.finding,
            "found_by": args.found_by, "enforcer": None,
        }
        validate(row, len(rows) + 1)
        with args.log.open("a", encoding="utf-8") as log:
            log.write(separator + json.dumps(row) + "\n")
        print(row["id"])
    except (ValueError, OSError) as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 2 if args.command == "add" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
