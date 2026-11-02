import hashlib
import json
import math
import re
import statistics
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Kpi:
    id: str
    unit: str
    better: str
    command: str
    samples: int
    tolerance_pct: float
    gate: str
    tolerance_abs: float = 0.0
    budget: float | None = None
    timeout_s: float = 900
    baseline: float | None = None


@dataclass(frozen=True)
class Scorecard:
    schema: int
    id: str
    repo: str
    baseline: dict | None
    kpis: tuple[Kpi, ...]
    scorecard_sha: str


@dataclass(frozen=True)
class Result:
    scorecard_id: str
    scorecard_sha: str
    baseline_sha: str | None
    machine: str
    overall: str
    kpis: tuple[dict, ...]
    run_sha: str = ""
    reason: str | None = None


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_.]+", value):
        raise ValueError(f"malformed id: {value}")
    return value


def _parse(text, digest):
    data = tomllib.loads(text)
    if type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("schema must be 1")
    card_id = _id(data.get("id"))
    if not isinstance(data.get("repo"), str) or not data["repo"]:
        raise ValueError("repo must be a nonempty string")
    baseline = data.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(baseline.get("sha", ""))):
            raise ValueError("baseline sha must be a 40-character commit")
        for field in ("machine", "measured_at"):
            if not isinstance(baseline.get(field), str) or not baseline[field]:
                raise ValueError(f"baseline {field} must be a nonempty string")
    if not isinstance(data.get("kpi"), list) or not data["kpi"]:
        raise ValueError("at least one kpi is required")
    kpis = []
    seen = set()
    for row in data["kpi"]:
        if not isinstance(row, dict):
            raise ValueError("kpi must be a table")
        name = _id(row.get("id"))
        if name in seen:
            raise ValueError(f"duplicate kpi id: {name}")
        seen.add(name)
        if row.get("better") not in ("lower", "higher"):
            raise ValueError(f"kpi {name}: better must be lower or higher")
        if row.get("gate") not in ("fail", "report"):
            raise ValueError(f"kpi {name}: gate must be fail or report")
        if type(row.get("samples")) is not int or row["samples"] < 1:
            raise ValueError(f"kpi {name}: samples must be an integer >= 1")
        for field in ("unit", "command"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"kpi {name}: {field} must be a nonempty string")
        for field, default in (("tolerance_pct", None), ("tolerance_abs", 0.0)):
            value = row.get(field, default)
            if not _finite(value) or value < 0:
                raise ValueError(f"kpi {name}: {field} must be a finite nonnegative number")
        timeout = row.get("timeout_s", 900)
        if not _finite(timeout) or timeout <= 0:
            raise ValueError(f"kpi {name}: timeout_s must be a finite positive number")
        if "baseline" in row and baseline is None:
            raise ValueError(f"kpi {name}: baseline requires [baseline]")
        if baseline is not None and "baseline" not in row:
            raise ValueError(f"kpi {name}: baseline is required with [baseline]")
        for field in ("budget", "baseline"):
            if field in row and not _finite(row[field]):
                raise ValueError(f"kpi {name}: {field} must be a finite number")
        kpis.append(Kpi(**{field: row[field] for field in Kpi.__dataclass_fields__ if field in row}))
    return Scorecard(1, card_id, data["repo"], baseline, tuple(kpis), digest)


def load(path: str | Path) -> Scorecard:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise ValueError("missing file") from None
    return _parse(raw.decode("utf-8"), hashlib.sha256(raw).hexdigest())


def run_commands(card: Scorecard, cwd: str | Path, runner=subprocess.run) -> dict[str, list[float]]:
    values = {kpi.id: [] for kpi in card.kpis}
    commands = dict.fromkeys(kpi.command for kpi in card.kpis if kpi.command != "@counts")
    for command in commands:
        kpis = [kpi for kpi in card.kpis if kpi.command == command]
        failed = set()
        for index in range(max(kpi.samples for kpi in kpis)):
            active = [kpi for kpi in kpis if index < kpi.samples]
            try:
                output = runner(["sh", "-c", command], cwd=cwd, capture_output=True, text=True,
                                timeout=min(kpi.timeout_s for kpi in active))
            except (subprocess.TimeoutExpired, OSError):
                failed.update(kpi.id for kpi in active)
                continue
            if output.returncode != 0:
                failed.update(kpi.id for kpi in active)
                continue
            parsed = {}
            duplicate = set()
            for line in output.stdout.splitlines():
                match = re.fullmatch(r"kpi ([a-z0-9_.]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", line)
                if match:
                    name, number = match.groups()
                    if name in parsed:
                        duplicate.add(name)
                    parsed[name] = float(number)
            for kpi in active:
                value = parsed.get(kpi.id)
                if kpi.id in duplicate or not _finite(value):
                    failed.add(kpi.id)
                else:
                    values[kpi.id].append(value)
        for name in failed:
            values[name] = []
    return values


def diff(card: Scorecard, values: dict[str, list[float] | float], machine: str) -> Result:
    rows = []
    failed = False
    for kpi in card.kpis:
        raw = values.get(kpi.id, [])
        samples = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        value = statistics.median(samples) if samples and all(_finite(n) for n in samples) else None
        if value is not None and not _finite(value):
            value = None
        base = kpi.baseline
        delta = None
        change = None if value is None or base is None else (value - base) * (1 if kpi.better == "lower" else -1)
        if change is not None and base != 0:
            percent = change / base * 100
            delta = percent if math.isfinite(percent) else None
        if value is None:
            status = "missing"
        elif kpi.budget is not None and (value > kpi.budget if kpi.better == "lower" else value < kpi.budget):
            status = "over_budget"
        elif card.baseline is None:
            status = "measured"
        else:
            band = max(abs(base) * (kpi.tolerance_pct / 100), kpi.tolerance_abs) if base else 0
            direction = 1 if kpi.better == "lower" else -1
            worse = base + direction * band
            better = base - direction * band
            status = "regressed" if (value - worse) * direction > 0 else "improved" if (value - better) * direction < 0 else "same"
        failed |= status in ("missing", "over_budget") or (status == "regressed" and kpi.gate == "fail")
        rows.append(dict(id=kpi.id, unit=kpi.unit, better=kpi.better, gate=kpi.gate, samples=samples,
                         value=value, baseline=base, delta_pct=delta, band_pct=kpi.tolerance_pct,
                         budget=kpi.budget, status=status))
    reason = None
    if card.baseline is not None and card.baseline["machine"] != machine:
        reason = f"machine mismatch: {card.baseline['machine']} != {machine}"
        failed = True
    overall = "FAIL" if failed else "NO_BASELINE" if card.baseline is None else "PASS"
    return Result(card.id, card.scorecard_sha, card.baseline["sha"] if card.baseline else None,
                  machine, overall, tuple(rows), reason=reason)


def format_lines(result: Result) -> list[str]:
    lines = []
    for row in result.kpis:
        base = "none" if row["baseline"] is None else f"{row['baseline']:.3f}"
        value = "missing" if row["value"] is None else f"{row['value']:.3f}"
        delta = "none" if row["delta_pct"] is None else f"{row['delta_pct']:+.1f}%"
        budget = "none" if row["budget"] is None else f"{row['budget']:g}"
        status = row["status"]
        if status == "regressed":
            status += f" ({row['gate']})"
        lines.append(f"kpi {row['id']} {row['unit']} {row['better']} base={base} run={value} "
                     f"delta={delta} band={row['band_pct']:g}% budget={budget} {status}")
    counts = " ".join(f"{status}={sum(row['status'] == status for row in result.kpis)}"
                      for status in ("improved", "same", "regressed", "missing", "over_budget"))
    baseline = result.baseline_sha[:12] if result.baseline_sha else "none"
    suffix = f" ({result.reason})" if result.reason else ""
    lines.append(f"scorecard {result.scorecard_id} sha={result.run_sha[:12]} baseline={baseline} "
                 f"kpis={len(result.kpis)} {counts} -> {result.overall}{suffix}")
    return lines


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if _finite(value):
        return repr(value)
    raise ValueError("unsupported TOML field value")


def rebaseline(text: str, results: list[dict | Result]) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    card = _parse(text, digest)
    results = [asdict(result) if isinstance(result, Result) else result for result in results]
    if not results or any(not isinstance(result, dict) for result in results):
        raise ValueError("at least one run result is required")
    for field in ("run_sha", "machine", "scorecard_sha"):
        first = results[0].get(field)
        if not isinstance(first, str) or not first:
            raise ValueError(f"input {field} is required")
        if any(result.get(field) != first for result in results):
            raise ValueError(f"input {field} values disagree")
    if not re.fullmatch(r"[0-9a-f]{40}", results[0]["run_sha"]):
        raise ValueError("input run_sha must be a 40-character commit")
    if results[0]["scorecard_sha"] != digest:
        raise ValueError("input scorecard_sha does not match scorecard")
    samples = {kpi.id: [] for kpi in card.kpis}
    for result in results:
        if result.get("scorecard_id") != card.id:
            raise ValueError("input scorecard_id does not match scorecard")
        rows = result.get("kpis")
        if not isinstance(rows, (list, tuple)) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("input kpis must be an array of tables")
        for kpi in card.kpis:
            matching = [row for row in rows if row.get("id") == kpi.id]
            if len(matching) != 1 or not _finite(matching[0].get("value")) or matching[0].get("status") == "missing":
                raise ValueError(f"input is missing kpi: {kpi.id}")
            samples[kpi.id].append(matching[0]["value"])
    data = tomllib.loads(text)
    data["baseline"] = dict(sha=results[0]["run_sha"], machine=results[0]["machine"],
                            measured_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    for row in data["kpi"]:
        row["baseline"] = statistics.median(samples[row["id"]])
    lines = [f"{key} = {_toml_value(value)}" for key, value in data.items() if key not in ("baseline", "kpi")]
    lines += ["", "[baseline]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in data["baseline"].items()]
    for row in data["kpi"]:
        lines += ["", "[[kpi]]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in row.items()]
    return "\n".join(lines) + "\n"
