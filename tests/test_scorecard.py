import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from shuin.cli import main


BASE = "a" * 40


def card_text(kpis=None, baseline=True):
    if kpis is None:
        kpis = [{"id": "bench.x", "baseline": 4, "budget": 8}]
    text = 'schema = 1\nid = "demo"\nrepo = "owner/demo"\n'
    if baseline:
        text += f'\n[baseline]\nsha = "{BASE}"\nmachine = "taco"\nmeasured_at = "2026-11-01T17:00:00Z"\n'
    for overrides in kpis:
        row = dict(id="bench.x", unit="ms", better="lower", command="measure", samples=1,
                   tolerance_pct=15, gate="fail")
        row.update(overrides)
        if not baseline:
            row.pop("baseline", None)
        text += "\n[[kpi]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in row.items())
    return text


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-c", "user.name=Tester", "-c", "user.email=test@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Initial"],
                   cwd=tmp_path, check=True)
    (tmp_path / "scorecard").mkdir()
    monkeypatch.setenv("SHUIN_DB", str(tmp_path / "must-not-exist.db"))
    monkeypatch.delenv("SHUIN_TOKEN", raising=False)
    monkeypatch.delenv("LEDGER_TOKEN", raising=False)
    return tmp_path


def write_card(repo, text):
    path = repo / "scorecard/scorecard.toml"
    path.write_text(text)
    return path


def fake_measurements(monkeypatch, outputs):
    from shuin import scorecard

    original = scorecard.run_commands
    responses = iter(outputs)

    def runner(command, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        code, stdout = response if isinstance(response, tuple) else (0, response)
        return subprocess.CompletedProcess(command, code, stdout, "")

    monkeypatch.setattr(scorecard, "run_commands", lambda card, cwd: original(card, cwd, runner=runner))


def run_card(repo, capsys, *extra):
    out = repo / "result.json"
    code = main(["scorecard", "run", "--dir", str(repo), "--machine", "taco", "--out", str(out), *extra])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert not (repo / "must-not-exist.db").exists()
    result = json.loads(out.read_text())
    assert captured.out == "\n".join(result["lines"]) + "\n"
    assert not Path(str(out) + ".tmp").exists()
    assert result["run_sha"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert result["scorecard_sha"] == hashlib.sha256((repo / "scorecard/scorecard.toml").read_bytes()).hexdigest()
    return code, result


@pytest.mark.parametrize("base,value,better,tolerance,budget,gate,status,overall,code,line", [
    (4, 4.2, "lower", 15, 8, "fail", "same", "PASS", 0,
     "kpi bench.x ms lower base=4.000 run=4.200 delta=+5.0% band=15% budget=8 same"),
    (1.3, 2.1, "lower", 15, 20, "fail", "regressed", "FAIL", 1,
     "kpi bench.x ms lower base=1.300 run=2.100 delta=+61.5% band=15% budget=20 regressed (fail)"),
    (1.3, 2.1, "lower", 15, 20, "report", "regressed", "PASS", 0,
     "kpi bench.x ms lower base=1.300 run=2.100 delta=+61.5% band=15% budget=20 regressed (report)"),
    (100, 120, "higher", 10, 50, "fail", "improved", "PASS", 0,
     "kpi bench.x ms higher base=100.000 run=120.000 delta=-20.0% band=10% budget=50 improved"),
    (100, 80, "higher", 10, 50, "fail", "regressed", "FAIL", 1,
     "kpi bench.x ms higher base=100.000 run=80.000 delta=+20.0% band=10% budget=50 regressed (fail)"),
    (8.5, 9, "lower", 15, 8, "report", "over_budget", "FAIL", 1,
     "kpi bench.x ms lower base=8.500 run=9.000 delta=+5.9% band=15% budget=8 over_budget"),
    (100, 40, "higher", 10, 50, "report", "over_budget", "FAIL", 1,
     "kpi bench.x ms higher base=100.000 run=40.000 delta=+60.0% band=10% budget=50 over_budget"),
])
def test_run_diffs(repo, monkeypatch, capsys, base, value, better, tolerance, budget, gate, status, overall, code, line):
    write_card(repo, card_text([dict(id="bench.x", baseline=base, better=better,
                                   tolerance_pct=tolerance, budget=budget, gate=gate)]))
    fake_measurements(monkeypatch, [f"ignored output\nkpi bench.x={value}\n"])
    actual, result = run_card(repo, capsys)
    assert actual == code
    assert result["overall"] == overall
    assert result["kpis"][0]["status"] == status
    assert result["lines"][0] == line
    counts = {name: int(name == status) for name in ("improved", "same", "regressed", "missing", "over_budget")}
    assert result["lines"][1] == (
        f"scorecard demo sha={result['run_sha'][:12]} baseline={BASE[:12]} kpis=1 "
        + " ".join(f"{name}={count}" for name, count in counts.items()) + f" -> {overall}"
    )
    assert set(result) == {"scorecard_id", "scorecard_sha", "run_sha", "baseline_sha", "machine", "overall", "kpis", "lines"}
    assert set(result["kpis"][0]) == {"id", "unit", "better", "gate", "samples", "value", "baseline",
                                      "delta_pct", "band_pct", "budget", "status"}


@pytest.mark.parametrize("outputs,statuses", [
    ([(1, "kpi a=1\nkpi b=2\n")], ["missing", "missing"]),
    (["kpi a=1\n"], ["same", "missing"]),
    ([subprocess.TimeoutExpired("measure", 1)], ["missing", "missing"]),
    (["kpi a=nan\nkpi b=inf\n"], ["missing", "missing"]),
    (["kpi a=1\nkpi a=2\nkpi b=2\n"], ["missing", "same"]),
])
def test_shared_command_failures(repo, monkeypatch, capsys, outputs, statuses):
    write_card(repo, card_text([dict(id="a", baseline=1), dict(id="b", baseline=2)]))
    fake_measurements(monkeypatch, outputs)
    code, result = run_card(repo, capsys)
    assert code == 1
    assert result["overall"] == "FAIL"
    assert [row["status"] for row in result["kpis"]] == statuses


def test_shared_samples_median_and_first_requested_samples(repo, monkeypatch, capsys):
    write_card(repo, card_text([dict(id="a", baseline=3, samples=3), dict(id="b", baseline=10, samples=1)]))
    fake_measurements(monkeypatch, ["kpi a=5\nkpi b=10\n", "kpi a=1\n", "kpi a=3\n"])
    code, result = run_card(repo, capsys)
    assert code == 0
    assert [(row["samples"], row["value"]) for row in result["kpis"]] == [([5, 1, 3], 3), ([10], 10)]


def test_partial_samples_are_missing(repo, monkeypatch, capsys):
    write_card(repo, card_text([dict(baseline=3, samples=3)]))
    fake_measurements(monkeypatch, ["kpi bench.x=3\n", (1, ""), "kpi bench.x=3\n"])
    code, result = run_card(repo, capsys)
    assert code == 1
    assert result["kpis"][0]["status"] == "missing"
    assert result["kpis"][0]["value"] is None


@pytest.mark.parametrize("base,value,absolute,status", [(1, 1.4, .5, "same"), (0, .1, 0, "regressed"), (0, 0, 0, "same")])
def test_absolute_band_and_zero_baseline(repo, monkeypatch, capsys, base, value, absolute, status):
    write_card(repo, card_text([dict(baseline=base, tolerance_pct=10, tolerance_abs=absolute)]))
    fake_measurements(monkeypatch, [f"kpi bench.x={value}\n"])
    code, result = run_card(repo, capsys)
    assert code == (1 if status == "regressed" else 0)
    assert result["kpis"][0]["status"] == status
    if base == 0:
        assert result["kpis"][0]["delta_pct"] is None


@pytest.mark.parametrize("output,budget,overall,code,status", [
    ("kpi bench.x=4\n", 8, "NO_BASELINE", 3, "measured"),
    ("", 8, "FAIL", 1, "missing"),
    ("kpi bench.x=9\n", 8, "FAIL", 1, "over_budget"),
])
def test_bootstrap_never_passes(repo, monkeypatch, capsys, output, budget, overall, code, status):
    write_card(repo, card_text([dict(budget=budget, gate="report")], baseline=False))
    fake_measurements(monkeypatch, [output])
    actual, result = run_card(repo, capsys)
    assert actual == code
    assert result["overall"] == overall
    assert result["baseline_sha"] is None
    assert result["kpis"][0]["status"] == status
    assert result["lines"][-1].endswith(f"-> {overall}")


def test_machine_mismatch(repo, monkeypatch, capsys):
    write_card(repo, card_text())
    fake_measurements(monkeypatch, ["kpi bench.x=4\n"])
    code, result = run_card(repo, capsys, "--machine", "other")
    assert code == 1
    assert result["overall"] == "FAIL"
    assert result["lines"][-1].endswith("-> FAIL (machine mismatch: taco != other)")


@pytest.mark.parametrize("counts,code,values", [
    (["--counts", "collected=10,passed=9,failed=1"], 0, [10, 9, 1]),
    ([], 1, [None, None, None]),
])
def test_counts(repo, capsys, counts, code, values):
    write_card(repo, card_text([dict(id=f"counts.{name}", command="@counts", baseline=value)
                               for name, value in zip(("collected", "passed", "failed"), (10, 9, 1))]))
    actual, result = run_card(repo, capsys, *counts)
    assert actual == code
    assert [row["value"] for row in result["kpis"]] == values


@pytest.mark.parametrize("counts", ["passed=-1", "passed=one", "passed=1,passed=2", "unknown=2", "passed=1,", ""])
def test_bad_counts(repo, capsys, counts):
    write_card(repo, card_text())
    assert main(["scorecard", "run", "--dir", str(repo), "--machine", "taco", "--counts", counts,
                 "--out", str(repo / "out.json")]) == 2
    assert capsys.readouterr() == ("", "scorecard invalid: counts must be unique collected,passed,failed nonnegative integers\n")
    assert not (repo / "out.json").exists()


@pytest.mark.parametrize("change,reason", [
    (lambda text: None, "missing file"),
    (lambda text: text.replace("schema = 1", "schema = 2"), "schema must be 1"),
    (lambda text: text.replace('id = "demo"', 'id = "Bad-id"'), "malformed id: Bad-id"),
    (lambda text: text.replace('id = "bench.x"', 'id = "Bad-id"'), "malformed id: Bad-id"),
    (lambda text: text + text[text.index("[[kpi]]"):], "duplicate kpi id: bench.x"),
    (lambda text: text.replace('better = "lower"', 'better = "sideways"'), "kpi bench.x: better must be lower or higher"),
    (lambda text: text.replace('gate = "fail"', 'gate = "skip"'), "kpi bench.x: gate must be fail or report"),
    (lambda text: text.replace("samples = 1", "samples = 0"), "kpi bench.x: samples must be an integer >= 1"),
    (lambda text: text.replace("samples = 1", "samples = true"), "kpi bench.x: samples must be an integer >= 1"),
    (lambda text: text.replace("tolerance_pct = 15", "tolerance_pct = -1"), "kpi bench.x: tolerance_pct must be a finite nonnegative number"),
    (lambda text: text + "tolerance_abs = -1\n", "kpi bench.x: tolerance_abs must be a finite nonnegative number"),
    (lambda text: text.replace("[baseline]", "[removed]"), "kpi bench.x: baseline requires [baseline]"),
    (lambda text: text.replace("baseline = 4\n", ""), "kpi bench.x: baseline is required with [baseline]"),
    (lambda text: text + "timeout_s = 0\n", "kpi bench.x: timeout_s must be a finite positive number"),
    (lambda text: text.replace('sha = "' + BASE + '"', 'sha = "bad"'), "baseline sha must be a 40-character commit"),
    (lambda text: text.replace("baseline = 4", "baseline = nan"), "kpi bench.x: baseline must be a finite number"),
    (lambda text: text[:text.index("[[kpi]]")], "at least one kpi is required"),
])
def test_check_invalid(repo, capsys, change, reason):
    text = change(card_text())
    if text is not None:
        write_card(repo, text)
    assert main(["scorecard", "check", "--dir", str(repo)]) == 2
    assert capsys.readouterr() == ("", f"scorecard invalid: {reason}\n")


@pytest.mark.parametrize("baseline,expected", [(True, BASE[:12]), (False, "none")])
def test_check_and_alternate_file(repo, capsys, baseline, expected):
    (repo / "nightly.toml").write_text(card_text(baseline=baseline))
    assert main(["scorecard", "check", "--dir", str(repo), "--file", "nightly.toml"]) == 0
    assert capsys.readouterr() == (f"scorecard ok demo kpis=1 baseline={expected}\n", "")
    assert not (repo / "must-not-exist.db").exists()


def result_input(text, value=4):
    return dict(scorecard_id="demo", scorecard_sha=hashlib.sha256(text.encode()).hexdigest(),
                run_sha=BASE, machine="taco", kpis=[dict(id="bench.x", value=value, status="measured")])


def test_rebaseline_preserves_fields_and_order(repo, capsys):
    from shuin.scorecard import load

    text = card_text([dict(id="bench.x", budget=8), dict(id="second", samples=3, tolerance_abs=.5)], baseline=False)
    path = write_card(repo, text)
    args = ["scorecard", "rebaseline", "--dir", str(repo), "--file", "scorecard/scorecard.toml"]
    inputs = []
    for index, value in enumerate((4, 5, 6)):
        data = result_input(text, value)
        data["kpis"].append(dict(id="second", value=value * 2, status="measured"))
        inputs.append(data)
        run_path = repo / f"run{index}.json"
        run_path.write_text(json.dumps(data))
        args += ["--from", str(run_path)]
    assert main(args) == 0
    assert capsys.readouterr().err == ""
    written = tomllib.loads(path.read_text())
    before = tomllib.loads(text)
    assert written["baseline"]["sha"] == BASE
    assert written["baseline"]["machine"] == "taco"
    assert written["baseline"]["measured_at"].endswith("Z")
    assert [row.pop("baseline") for row in written["kpi"]] == [5, 10]
    written.pop("baseline")
    assert written == before
    first = load(path)
    copy = repo / "copy.toml"
    copy.write_text(path.read_text())
    assert load(copy) == first
    assert first.kpis[0].baseline == 5


@pytest.mark.parametrize("alter,reason", [
    (lambda r: r.update(run_sha="b" * 40), "input run_sha values disagree"),
    (lambda r: r.update(machine="other"), "input machine values disagree"),
    (lambda r: r.update(scorecard_sha="b" * 64), "input scorecard_sha values disagree"),
    (lambda r: r.update(kpis=[]), "input is missing kpi: bench.x"),
    (lambda r: r["kpis"][0].update(value=None), "input is missing kpi: bench.x"),
])
def test_rebaseline_refuses_bad_inputs(repo, capsys, alter, reason):
    text = card_text(baseline=False)
    path = write_card(repo, text)
    first, second = result_input(text), result_input(text)
    alter(second)
    args = ["scorecard", "rebaseline", "--dir", str(repo)]
    for index, result in enumerate((first, second)):
        target = repo / f"input{index}.json"
        target.write_text(json.dumps(result))
        args += ["--from", str(target)]
    assert main(args) == 2
    assert capsys.readouterr() == ("", f"scorecard invalid: {reason}\n")
    assert path.read_text() == text


def test_real_shell_command_and_alternate_run(repo, capsys):
    text = card_text([dict(command="test -f marker && printf 'kpi bench.x=4\\n'", baseline=4, timeout_s=2)])
    path = repo / "nightly.toml"
    path.write_text(text)
    (repo / "marker").touch()
    out = repo / "out.json"
    assert main(["scorecard", "run", "--dir", str(repo), "--file", "nightly.toml",
                 "--machine", "taco", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["kpis"][0]["value"] == 4
    assert capsys.readouterr().err == ""


def test_real_cli_process(repo):
    write_card(repo, card_text([dict(command="printf 'kpi bench.x=4\\n'", budget=8)], baseline=False))
    proc = subprocess.run([sys.executable, "-m", "shuin", "scorecard", "run", "--dir", str(repo),
                           "--machine", "taco", "--out", str(repo / "out.json")], capture_output=True, text=True)
    assert proc.returncode == 3
    assert proc.stderr == ""
    assert proc.stdout.endswith("-> NO_BASELINE\n")


@pytest.mark.parametrize("value,status", [(1.1, "same"), (.9, "same"), (1.1001, "regressed"), (.8999, "improved")])
def test_pure_diff_includes_band_boundaries(repo, value, status):
    from shuin.scorecard import diff, format_lines, load

    card = load(write_card(repo, card_text([dict(baseline=1, tolerance_pct=10)])))
    result = diff(card, {"bench.x": value}, "taco")
    assert result == diff(card, {"bench.x": value}, "taco")
    assert result.kpis[0]["status"] == status
    assert format_lines(result)[0].endswith(status + (" (fail)" if status == "regressed" else ""))


def test_rebaseline_accepts_results_and_refuses_a_different_card(repo):
    from dataclasses import replace

    from shuin.scorecard import diff, load, rebaseline

    text = card_text(baseline=False)
    card = load(write_card(repo, text))
    results = [replace(diff(card, {"bench.x": value}, "taco"), run_sha=BASE) for value in (4, 5, 6)]
    updated = rebaseline(text, results)
    assert tomllib.loads(updated)["kpi"][0]["baseline"] == 5
    with pytest.raises(ValueError, match="^input scorecard_sha does not match scorecard$"):
        rebaseline(text + "\n", results)


def test_real_timeout(repo, capsys):
    write_card(repo, card_text([dict(command="sleep 0.2; printf 'kpi bench.x=4\\n'", baseline=4, timeout_s=.01)]))
    code, result = run_card(repo, capsys)
    assert code == 1
    assert result["kpis"][0]["status"] == "missing"


def test_negative_baseline_uses_the_signed_percentage_formula(repo):
    from shuin.scorecard import diff, load

    card = load(write_card(repo, card_text([dict(baseline=-100, tolerance_pct=10)])))
    result = diff(card, {"bench.x": -80}, "taco")
    assert result.kpis[0]["delta_pct"] == -20
    assert result.kpis[0]["status"] == "regressed"


def test_large_finite_values_cannot_overflow_the_noise_band(repo):
    from shuin.scorecard import diff, load

    card = load(write_card(repo, card_text([dict(baseline=1e308, tolerance_pct=15)])))
    result = diff(card, {"bench.x": 1.3e308}, "taco")
    assert result.kpis[0]["status"] == "regressed"
    assert result.overall == "FAIL"
