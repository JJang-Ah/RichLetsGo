"""D102 registration publication uses clean C and later main input bytes."""

import json
import subprocess
from pathlib import Path

import pytest

from richletsgo.backtest.codec import encode
from richletsgo.research.d102 import execution


def test_checkout_c_uses_later_published_registration_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    checkout_c = tmp_path / "checkout-c"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Synthetic Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "synthetic@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)

    reports = repo / "docs" / "reports"
    reports.mkdir(parents=True)
    freeze = execution.envelope({"synthetic": "freeze"})
    monkeypatch.setattr(execution, "D98_FREEZE_DIGEST", freeze["sha256"])
    freeze_path = reports / "D98-study-preregistration.json"
    freeze_path.write_text(encode(freeze) + "\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text("synthetic C\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "C"], check=True, capture_output=True)
    code_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", code_commit],
        check=True,
    )

    pool_body = {"synthetic": "pool"}
    pool = execution.envelope(pool_body)
    audit = execution.envelope(
        {
            "pool_sha256": pool["sha256"],
            "data_hash": "synthetic-data-hash",
            "status": "READY",
            "contracts": [],
        }
    )
    monkeypatch.setattr(execution, "D98_POOL_DIGEST", pool["sha256"])
    monkeypatch.setattr(execution, "D98_AUDIT_DIGEST", audit["sha256"])
    monkeypatch.setattr(execution, "D98_DATA_HASH", audit["body"]["data_hash"])
    reg = execution.registration(code_commit, pool, audit)
    reg = json.loads(encode(reg))
    input_values = {
        execution.REGISTRATION_PATH: reg,
        execution.POOL_PATH: pool,
        execution.AUDIT_PATH: audit,
    }
    input_files: dict[str, Path] = {}
    for relative, value in input_values.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encode(value) + "\n", encoding="utf-8", newline="\n")
        input_files[relative] = path
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "publish synthetic registration"],
        check=True,
        capture_output=True,
    )
    published_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", published_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout_c), code_commit],
        check=True,
        capture_output=True,
    )

    execution.validate_checkout(
        checkout_c,
        reg,
        pool,
        audit,
        registration_file=input_files[execution.REGISTRATION_PATH],
        pool_file=input_files[execution.POOL_PATH],
        audit_file=input_files[execution.AUDIT_PATH],
    )
    input_files[execution.REGISTRATION_PATH].write_bytes(
        input_files[execution.REGISTRATION_PATH].read_bytes() + b"\n"
    )
    with pytest.raises(ValueError, match="published input bytes"):
        execution.validate_checkout(
            checkout_c,
            reg,
            pool,
            audit,
            registration_file=input_files[execution.REGISTRATION_PATH],
            pool_file=input_files[execution.POOL_PATH],
            audit_file=input_files[execution.AUDIT_PATH],
        )


def test_registered_run_verifies_before_study_and_completed_recall(tmp_path, monkeypatch):
    from test_d102_study import dataset

    db = tmp_path / "synthetic.sqlite"
    pool, audit = dataset(db)
    reg = execution.envelope({"code_commit": "synthetic"})
    inputs = []
    for name, value in [("registration", reg), ("pool", pool), ("audit", audit)]:
        path = tmp_path / (name + ".json")
        path.write_text(encode(value), encoding="utf-8")
        inputs.append(path)
    checks = []
    monkeypatch.setattr(
        execution, "validate_checkout", lambda *args, **kw: checks.append("checkout")
    )
    monkeypatch.setattr(
        execution, "verify_frozen_dataset", lambda *args, **kw: checks.append("freeze")
    )
    result = execution.run_registered(tmp_path, *inputs, db, tmp_path / "output", workers=1)
    assert checks == ["checkout", "freeze"]
    monkeypatch.setattr(execution, "study", lambda *args, **kw: pytest.fail("repeat research"))
    again = execution.run_registered(tmp_path, *inputs, db, tmp_path / "output", workers=1)
    assert result["sha256"] == again["sha256"]
    assert checks == ["checkout", "freeze", "checkout", "freeze"]

    def reject(*args, **kwargs):
        raise ValueError("freeze mismatch")

    monkeypatch.setattr(execution, "verify_frozen_dataset", reject)
    with pytest.raises(ValueError, match="freeze mismatch"):
        execution.run_registered(tmp_path, *inputs, db, tmp_path / "output", workers=1)


def test_crash_requires_explicit_resume_and_preserves_attempt_history(tmp_path, monkeypatch):
    from test_d102_study import dataset

    db = tmp_path / "synthetic.sqlite"
    pool, audit = dataset(db)
    reg = execution.envelope({"code_commit": "synthetic"})
    paths = []
    for name, value in [("reg", reg), ("pool", pool), ("audit", audit)]:
        path = tmp_path / (name + ".json")
        path.write_text(encode(value), encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(execution, "validate_checkout", lambda *args, **kw: None)
    monkeypatch.setattr(execution, "verify_frozen_dataset", lambda *args, **kw: None)
    original = execution.study

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(execution, "study", fail)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        execution.run_registered(tmp_path, *paths, db, tmp_path / "results", workers=1)
    with pytest.raises(ValueError, match="resume-crashed"):
        execution.run_registered(tmp_path, *paths, db, tmp_path / "results", workers=1)
    monkeypatch.setattr(execution, "study", original)
    execution.run_registered(
        tmp_path, *paths, db, tmp_path / "results", workers=1, resume_crashed=True
    )
    state = json.loads((tmp_path / "results" / reg["sha256"] / "state.json").read_text())
    assert state["status"] == "COMPLETED" and len(state["attempts"]) == 2


def test_cli_rejects_other_worktree_import(monkeypatch):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "d102_study.py"
    spec = importlib.util.spec_from_file_location("d102_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.checked_worktree_root() == path.parents[1]
    monkeypatch.setattr(module.execution, "__file__", str(path.parent / "wrong.py"))
    with pytest.raises(RuntimeError, match="worktree/package mismatch"):
        module.checked_worktree_root()
