"""Registered execution gates, durable one-shot lifecycle, and LF-only publication."""

import hashlib
import json
import os
import socket
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest, encode

from .audit import _audit
from .locks import execution_gate
from .models import POLICY
from .store import Store, dataset_lock
from .study import study

REGISTRATION_PATH = "docs/reports/D98-study-preregistration.json"


def envelope(body: dict[str, Any]) -> dict[str, Any]:
    return {"body": body, "sha256": digest(body)}


def read_artifact(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if digest(value["body"]) != value["sha256"]:
        raise ValueError("artifact digest mismatch")
    return dict(value)


def registration(code_commit: str, pool: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    for value in (pool, audit):
        if digest(value["body"]) != value["sha256"]:
            raise ValueError("artifact digest mismatch")
    if audit["body"]["pool_sha256"] != pool["sha256"]:
        raise ValueError("audit pool mismatch")
    return envelope(
        {
            "code_commit": code_commit,
            "pool_sha256": pool["sha256"],
            "audit_sha256": audit["sha256"],
            "data_hash": audit["body"]["data_hash"],
            "policy": POLICY,
        }
    )


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_text(path, encode(value) + "\n")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, encoding="utf-8"
    ).strip()


def validate_checkout(
    root: Path, reg: dict[str, Any], pool: dict[str, Any], audit: dict[str, Any]
) -> None:
    expected = registration(reg["body"]["code_commit"], pool, audit)
    if reg != expected:
        raise ValueError("registration conditions/identity mismatch")
    if git(root, "rev-parse", "HEAD") != reg["body"]["code_commit"]:
        raise ValueError("checkout must be registered C")
    if git(root, "status", "--porcelain"):
        raise ValueError("checkout must be clean")
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            reg["body"]["code_commit"],
            "origin/main",
        ],
        check=True,
        capture_output=True,
    )
    for relative, value in (
        (REGISTRATION_PATH, reg),
        ("docs/reports/D98-pool.json", pool),
        ("docs/reports/D98-collection-audit.json", audit),
    ):
        published = json.loads(git(root, "show", f"origin/main:{relative}"))
        if published != value:
            raise ValueError("input not published on origin/main")


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # access denied/unknown: fail closed
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def markdown(result: dict[str, Any]) -> str:
    b = result["body"]
    lines = [
        "# D98 signal study",
        "",
        f"Status: **{b['status']}**",
        "",
        f"Registration: `{b['registration_digest']}`",
        f"Data hash: `{b['data_hash']}`",
        f"Result digest: `{result['sha256']}`",
        "",
        "| Combination | Valid | Mean net R | Trimmed mean R | Undecidable |",
        "|---|---:|---:|---:|---|",
    ]
    for key, r in sorted(b["combinations"].items()):
        lines.append(
            f"| {key} | {r['valid']} | {r['mean_R']} | {r['trimmed_mean_R']} | {r['undecidable']} |"
        )
    lines += [
        "",
        f"Trades SHA-256: `{b['trades']['sha256']}`",
        "",
        "All stages, distributions, yearly criteria and policy are in the JSON. Design reference only; not holdout validation.",
    ]
    return "\n".join(lines) + "\n"


def completed_result(directory: Path, reg: dict[str, Any]) -> dict[str, Any]:
    result = read_artifact(directory / "D98-study.json")
    if (
        result["body"]["registration_digest"] != reg["sha256"]
        or result["body"]["data_hash"] != reg["body"]["data_hash"]
    ):
        raise ValueError("result registration mismatch")
    if file_hash(directory / "D98-study-trades.jsonl") != result["body"]["trades"]["sha256"]:
        raise ValueError("trade file digest mismatch")
    if (directory / "D98-study.md").read_bytes() != markdown(result).encode("utf-8"):
        raise ValueError("Markdown bytes mismatch")
    return result


def run_registered(
    root: Path,
    registration_file: Path,
    pool_file: Path,
    audit_file: Path,
    db: Path,
    output_root: Path,
    *,
    workers: int,
    resume_crashed: bool = False,
    alive: Callable[[int], bool] = process_alive,
    after_result: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("positive worker count required")
    reg = read_artifact(registration_file)
    pool = read_artifact(pool_file)
    published_audit = read_artifact(audit_file)
    validate_checkout(root, reg, pool, published_audit)
    if published_audit["body"]["status"] != "READY":
        return {"status": published_audit["body"]["status"]}
    directory = output_root / reg["sha256"]
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "run.lock"
    state_file = directory / "state.json"
    result_file = directory / "D98-study.json"
    with execution_gate(directory / "gate.lock"):
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else None
        if lock.exists():
            owner = json.loads(lock.read_text(encoding="utf-8"))
            if owner["host"] != socket.gethostname() or alive(owner["pid"]):
                raise ValueError("live or unverifiable execution owner")
        if state and state["status"] == "COMPLETED":
            result = completed_result(directory, reg)
            if result["sha256"] != state["result_sha256"]:
                raise ValueError("completed state digest mismatch")
            clean_published_locks(db, lock, state["attempts"], alive)
            return {"status": "COMPLETED", "result": str(result_file), "sha256": result["sha256"]}
        if result_file.exists():
            result = completed_result(directory, reg)
            write_json(
                state_file,
                {
                    "status": "COMPLETED",
                    "result_sha256": result["sha256"],
                    "attempts": state["attempts"] if state else [],
                },
            )
            clean_published_locks(db, lock, state["attempts"] if state else [], alive)
            return {"status": "COMPLETED", "result": str(result_file), "sha256": result["sha256"]}
        if (lock.exists() or state) and not resume_crashed:
            raise ValueError("crashed execution requires --resume-crashed")
        if lock.exists():
            lock.unlink()
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": datetime.now(UTC).isoformat(),
        }
        with lock.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encode(owner) + "\n")
        attempts = list(state["attempts"]) if state else []
        attempts.append(owner)
        write_json(state_file, {"status": "RUNNING", "attempts": attempts})
        with dataset_lock(
            db, owner=owner, recover_owners=attempts[:-1] if resume_crashed else None
        ):
            # Hold the same lock through rehash, freeze, calculation and publication.
            current = _audit(pool, db, workers=workers)
            if current["body"]["data_hash"] != reg["body"]["data_hash"]:
                raise ValueError("data_hash mismatch")
            for key in ("status", "contracts", "metadata_eligible", "input_insufficient"):
                if current["body"][key] != published_audit["body"][key]:
                    raise ValueError("audit content mismatch")
            store = Store(db)
            try:
                store.freeze(reg["sha256"])
            finally:
                store.close()
            with TemporaryDirectory(prefix="attempt-", dir=directory) as temporary:
                stage = Path(temporary)
                result = study(
                    pool,
                    published_audit,
                    db,
                    stage,
                    workers=workers,
                    registration_digest=reg["sha256"],
                )
                result["execution"] = {"attempts": attempts, "restart_count": len(attempts) - 1}
                (stage / "D98-study-trades.jsonl").replace(directory / "D98-study-trades.jsonl")
                atomic_text(directory / "D98-study.md", markdown(result))
                write_json(result_file, result)
            if after_result:
                after_result()
            write_json(
                state_file,
                {"status": "COMPLETED", "result_sha256": result["sha256"], "attempts": attempts},
            )
            lock.unlink()
        return {"status": "COMPLETED", "result": str(result_file), "sha256": result["sha256"]}


def publish_registration(directory: Path, value: dict[str, Any]) -> None:
    """Publish proposal only; execution still requires its merge on origin/main."""
    if digest(value["body"]) != value["sha256"]:
        raise ValueError("artifact digest mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "D98-study-preregistration.json", value)
    atomic_text(
        directory / "D98-study-preregistration.md",
        "# D98 study preregistration\n\n"
        + f"Digest: `{value['sha256']}`\n\n"
        + "```json\n"
        + encode(value["body"])
        + "\n```\n",
    )


def clean_published_locks(
    db: Path,
    run_lock: Path,
    attempts: list[dict[str, Any]],
    alive: Callable[[int], bool],
) -> None:
    """Finish only this registration's dead markers after durable result publication."""
    marker = db.with_suffix(db.suffix + ".lock")
    if marker.exists():
        owner = json.loads(marker.read_text(encoding="utf-8"))
        if owner not in attempts or owner["host"] != socket.gethostname() or alive(owner["pid"]):
            raise ValueError("live or unrelated dataset owner")
        with dataset_lock(db, owner=owner, recover_owners=attempts):
            pass
    if run_lock.exists():
        run_lock.unlink()
