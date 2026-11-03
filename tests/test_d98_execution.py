"""C/P checkout and durable execution tests, using only temporary synthetic data."""

import json
import os
import socket
from pathlib import Path

import pytest
from d98_fixtures import dataset

from richletsgo.research.d98 import execution as ex
from richletsgo.research.d98.store import Store


@pytest.fixture
def registered(tmp_path):
    db = tmp_path / "synthetic.sqlite"
    pool, audit = dataset(db)
    repo = tmp_path / "repo"
    repo.mkdir()
    ex.git(repo, "init", "-b", "main")
    ex.git(repo, "config", "user.email", "synthetic@example.invalid")
    ex.git(repo, "config", "user.name", "Synthetic Test")
    ex.git(repo, "config", "core.autocrlf", "false")
    (repo / "source.py").write_text("# synthetic C\n")
    ex.git(repo, "add", ".")
    ex.git(repo, "commit", "-m", "C")
    c = ex.git(repo, "rev-parse", "HEAD")
    reg = ex.registration(c, pool, audit)
    docs = repo / "docs/reports"
    ex.publish_registration(docs, reg)
    ex.write_json(docs / "D98-pool.json", pool)
    ex.write_json(docs / "D98-collection-audit.json", audit)
    ex.git(repo, "add", ".")
    ex.git(repo, "commit", "-m", "P")
    ex.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    checkout = tmp_path / "C"
    ex.git(repo, "worktree", "add", "--detach", str(checkout), c)
    return (
        dict(
            root=checkout,
            registration_file=docs / "D98-study-preregistration.json",
            pool_file=docs / "D98-pool.json",
            audit_file=docs / "D98-collection-audit.json",
            db=db,
            output_root=tmp_path / "output",
            workers=1,
        ),
        reg,
        repo,
    )


def test_checkout_c_p_and_dirty(registered):
    args, reg, repo = registered
    pool, audit = ex.read_artifact(args["pool_file"]), ex.read_artifact(args["audit_file"])
    ex.validate_checkout(args["root"], reg, pool, audit)
    for name in ("source.py", "untracked.py"):
        file = args["root"] / name
        old = file.read_bytes() if file.exists() else None
        file.write_text("# dirty\n")
        with pytest.raises(ValueError, match="clean"):
            ex.run_registered(**args)
        if old is None:
            file.unlink()
        else:
            file.write_bytes(old)
    with pytest.raises(ValueError, match="registered C"):
        ex.run_registered(**{**args, "root": repo})
    modified = ex.envelope({**reg["body"], "policy": {}})
    ex.write_json(args["registration_file"], modified)
    with pytest.raises(ValueError, match="conditions"):
        ex.run_registered(**args)
    assert not args["output_root"].exists()


def test_run_freeze_completed_and_lf(registered, monkeypatch):
    args, reg, repo = registered
    original = ex.study

    def checked(*a, **kw):
        store = Store(args["db"])
        assert (
            store.db.execute("SELECT value FROM identity WHERE key='frozen'").fetchone()[0]
            == reg["sha256"]
        )
        store.freeze(reg["sha256"])
        with pytest.raises(ValueError, match="another registration"):
            store.freeze("different")
        with pytest.raises(ValueError, match="frozen"):
            store.put("funding", "SYNUSDT", [[0, "0"]])
        store.close()
        return original(*a, **kw)

    monkeypatch.setattr(ex, "study", checked)
    result = ex.run_registered(**args)
    directory = Path(result["result"]).parent
    for p in [
        *directory.glob("*.json*"),
        *directory.glob("*.md"),
        *args["registration_file"].parent.glob("*"),
    ]:
        assert b"\r" not in p.read_bytes()
    # Git's stored blob must have precisely the published LF bytes, too.
    for name in ("D98-study-preregistration.json", "D98-study-preregistration.md"):
        import subprocess

        blob = subprocess.check_output(
            ["git", "-C", str(repo), "show", "HEAD:docs/reports/" + name]
        )
        assert blob == (args["registration_file"].parent / name).read_bytes()
    monkeypatch.setattr(ex, "study", lambda *a, **kw: pytest.fail("must not recalculate"))
    monkeypatch.setattr(ex, "_audit", lambda *a, **kw: pytest.fail("must not reopen DB"))
    assert ex.run_registered(**args) == result
    with (directory / "D98-study-trades.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        ex.run_registered(**args)


def test_data_mismatch_before_freeze(registered, monkeypatch):
    args, _, _ = registered
    store = Store(args["db"])
    with store.db:
        store.db.execute(
            "UPDATE funding SET rate='0.000001' WHERE time=(SELECT MIN(time) FROM funding)"
        )
    store.close()
    monkeypatch.setattr(ex, "study", lambda *a, **kw: pytest.fail("must not calculate"))
    with pytest.raises(ValueError, match="data_hash"):
        ex.run_registered(**args)
    store = Store(args["db"])
    assert not store.db.execute("SELECT 1 FROM identity WHERE key='frozen'").fetchone()
    store.close()


def test_blocked_audit_and_bad_digest(registered, monkeypatch):
    args, reg, _ = registered
    monkeypatch.setattr(ex, "validate_checkout", lambda *a: None)
    monkeypatch.setattr(ex, "_audit", lambda *a, **kw: pytest.fail("must not open DB"))
    for status in ("BTC_INCOMPLETE", "STUDY_UNDECIDABLE"):
        audit = ex.read_artifact(args["audit_file"])
        audit["body"]["status"] = status
        ex.write_json(args["audit_file"], ex.envelope(audit["body"]))
        assert ex.run_registered(**args) == {"status": status}
    reg["sha256"] = "wrong"
    ex.write_json(args["registration_file"], reg)
    with pytest.raises(ValueError, match="digest"):
        ex.run_registered(**args)


def test_live_owner_kernel_gate_and_explicit_restart(registered, monkeypatch):
    args, reg, _ = registered
    directory = args["output_root"] / reg["sha256"]
    directory.mkdir(parents=True)
    owner = dict(pid=os.getpid(), host=socket.gethostname(), started="synthetic")
    ex.write_json(directory / "run.lock", owner)
    ex.write_json(directory / "state.json", dict(status="RUNNING", attempts=[owner]))
    with pytest.raises(ValueError, match="live"):
        ex.run_registered(**args, resume_crashed=True)
    with ex.execution_gate(directory / "gate.lock"), pytest.raises(OSError):
        ex.run_registered(**args, resume_crashed=True, alive=lambda _: False)
    with pytest.raises(ValueError, match="resume-crashed"):
        ex.run_registered(**args, alive=lambda _: False)
    result = ex.run_registered(**args, resume_crashed=True, alive=lambda _: False)
    restarted = ex.read_artifact(Path(result["result"]))
    assert restarted["execution"]["restart_count"] == 1
    clean = ex.run_registered(**{**args, "output_root": args["output_root"] / "clean"})
    assert clean["sha256"] == result["sha256"]
    assert ex.read_artifact(Path(clean["result"]))["execution"]["restart_count"] == 0


def test_rename_before_completed_recovers_without_calculation(registered, monkeypatch):
    args, _, _ = registered

    def crash():
        raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError, match="synthetic crash"):
        ex.run_registered(**args, after_result=crash)
    monkeypatch.setattr(ex, "study", lambda *a, **kw: pytest.fail("recalculated"))
    monkeypatch.setattr(ex, "_audit", lambda *a, **kw: pytest.fail("reaudited"))
    result = ex.run_registered(**args, alive=lambda _: False)
    directory = Path(result["result"]).parent
    assert json.loads((directory / "state.json").read_text())["status"] == "COMPLETED"
    assert not (directory / "run.lock").exists()


def test_hard_crash_dataset_marker_recovered(registered):
    import subprocess
    import sys

    args, reg, _ = registered
    directory = args["output_root"] / reg["sha256"]
    directory.mkdir(parents=True)
    # Real child death: no Python finally executes; OS gate alone is released.
    script = """
import os, socket, sys
from pathlib import Path
from richletsgo.research.d98.execution import write_json
from richletsgo.research.d98.store import dataset_lock
owner = dict(pid=os.getpid(),host=socket.gethostname(),started='hard-crash')
write_json(Path(sys.argv[2])/'run.lock',owner)
write_json(Path(sys.argv[2])/'state.json',dict(status='RUNNING',attempts=[owner]))
with dataset_lock(Path(sys.argv[1]),owner=owner):
    os._exit(7)
"""
    child = subprocess.run([sys.executable, "-c", script, str(args["db"]), str(directory)])
    assert child.returncode == 7
    marker = args["db"].with_suffix(".sqlite.lock")
    assert marker.exists()
    with pytest.raises(ValueError, match="resume-crashed"):
        ex.run_registered(**args)
    result = ex.run_registered(**args, resume_crashed=True)
    assert result["status"] == "COMPLETED"
    assert not marker.exists()


def test_different_frozen_registration_blocks_calculation(registered, monkeypatch):
    args, _, _ = registered
    store = Store(args["db"])
    store.freeze("different")
    store.close()
    monkeypatch.setattr(ex, "study", lambda *a, **kw: pytest.fail("must not calculate"))
    with pytest.raises(ValueError, match="another registration"):
        ex.run_registered(**args)


def test_real_death_after_result_rename_recovers_dataset_lock(registered):
    import subprocess
    import sys

    args, _, _ = registered
    script = """
import os, sys, json
from pathlib import Path
from richletsgo.research.d98.execution import run_registered
args = json.loads(sys.argv[1])
args = {k: (v if k == 'workers' else Path(v)) for k,v in args.items()}
run_registered(**args,after_result=lambda: os._exit(9))
"""
    encoded = json.dumps({k: v if k == "workers" else str(v) for k, v in args.items()})
    child = subprocess.run([sys.executable, "-c", script, encoded])
    assert child.returncode == 9
    marker = args["db"].with_suffix(".sqlite.lock")
    assert marker.exists()
    result = ex.run_registered(**args)
    assert result["status"] == "COMPLETED"
    assert not marker.exists()
