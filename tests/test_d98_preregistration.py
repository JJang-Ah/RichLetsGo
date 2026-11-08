"""Preregistration policy identity and CLI worktree provenance, synthetic only."""

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from d98_fixtures import dataset

from richletsgo.backtest.codec import digest
from richletsgo.research.d98.execution import registration, validate_checkout
from richletsgo.research.d98.models import POLICY

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "d98_study.py"


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("d98_study_cli_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matching_worktree_from_unrelated_cwd(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.checked_worktree_root() == ROOT
    called = []
    monkeypatch.setattr(
        cli.execution, "run_registered", lambda root, *args, **kw: called.append(root)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--preregistration",
            "synthetic-reg",
            "--pool",
            "synthetic-pool",
            "--audit",
            "synthetic-audit",
            "--db",
            "synthetic-db",
            "--output-root",
            str(tmp_path / "out"),
            "--workers",
            "1",
        ],
    )
    cli.main()
    assert called == [ROOT]
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("foreign", ["package", "execution", "cli"])
def test_wrong_worktree_fails_before_parsing_or_execution(cli, tmp_path, monkeypatch, foreign):
    if foreign == "package":
        monkeypatch.setattr(
            cli.richletsgo, "__file__", str(tmp_path / "src/richletsgo/__init__.py")
        )
    elif foreign == "execution":
        monkeypatch.setattr(
            cli.execution, "__file__", str(tmp_path / "src/richletsgo/research/d98/execution.py")
        )
    else:
        monkeypatch.setattr(cli, "__file__", str(tmp_path / "tools/d98_study.py"))
    monkeypatch.setattr(
        cli.argparse.ArgumentParser, "parse_args", lambda *a: pytest.fail("parsed before guard")
    )
    monkeypatch.setattr(
        cli.execution, "run_registered", lambda *a, **kw: pytest.fail("executed before guard")
    )
    with pytest.raises(RuntimeError, match="worktree/package mismatch") as error:
        cli.main()
    assert "dedicated venv" in str(error.value) and "pip install -e ." in str(error.value)


def test_copied_cli_with_original_editable_install_rejected(tmp_path):
    # Simulates invoking a C worktree script with the parent checkout's venv.
    script = tmp_path / "C" / "tools" / "d98_study.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(SCRIPT.read_bytes())
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode != 0
    assert "worktree/package mismatch" in result.stderr
    assert "pip install -e ." in result.stderr
    assert "usage:" not in result.stdout


def test_rules_are_in_registration_digest_and_required(tmp_path):
    pool, audit = dataset(tmp_path / "synthetic.sqlite")
    reg = registration("synthetic-C", pool, audit)
    rules = reg["body"]["policy"]["rules"]
    assert rules == POLICY["rules"]
    assert (
        rules["direction"]
        == "BTC trend state: LONG -> long only; SHORT -> short only; FLAT -> no signal."
    )
    assert rules["overheat"] == "abs(close(B_0) - EMA20) <= e * ATR14 (boundary included)."
    assert rules["exit_time"] == (
        "Start timestamp of the exit 1h bar, not the actual intrabar stop fill time; "
        "delisting liquidation uses E_a."
    )
    for field in (None, "direction", "overheat", "exit_time"):
        changed = deepcopy(reg)
        if field is None:
            del changed["body"]["policy"]["rules"]
        else:
            changed["body"]["policy"]["rules"][field] = "changed"
        changed["sha256"] = digest(changed["body"])
        assert changed["sha256"] != reg["sha256"]
        # Rejection precedes even git access, no real checkout/database required.
        with pytest.raises(ValueError, match="conditions/identity mismatch"):
            validate_checkout(tmp_path / "nonexistent", changed, pool, audit)
