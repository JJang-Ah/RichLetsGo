"""D101-owned registration checks and one-shot read-only execution boundary."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from richletsgo.backtest.codec import digest, encode
from richletsgo.research.d98.audit import _audit
from richletsgo.research.d98.locks import execution_gate
from richletsgo.research.d98.store import Store

from .models import (
    D98_AUDIT_DIGEST,
    D98_DATA_HASH,
    D98_FREEZE_DIGEST,
    D98_POOL_DIGEST,
    POLICY,
    POLICY_VERSION,
)
from .study import markdown, study

REGISTRATION_PATH = "docs/reports/D101-study-preregistration.json"
POOL_PATH = "docs/reports/D98-pool.json"
AUDIT_PATH = "docs/reports/D98-collection-audit.json"
D98_REGISTRATION_PATH = "docs/reports/D98-study-preregistration.json"


def envelope(body: dict[str, Any]) -> dict[str, Any]:
    return {"body": body, "sha256": digest(body)}


def read_artifact(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("body"), dict):
        raise ValueError("artifact envelope required")
    if digest(value["body"]) != value.get("sha256"):
        raise ValueError("artifact digest mismatch")
    return value


def registration(code_commit: str, pool: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    for artifact in (pool, audit):
        if digest(artifact["body"]) != artifact["sha256"]:
            raise ValueError("artifact digest mismatch")
    if (
        pool["sha256"] != D98_POOL_DIGEST
        or audit["sha256"] != D98_AUDIT_DIGEST
        or audit["body"].get("pool_sha256") != pool["sha256"]
        or audit["body"].get("data_hash") != D98_DATA_HASH
        or audit["body"].get("status") != "READY"
    ):
        raise ValueError("D101 requires the registered D98 DESIGN pool and READY audit")
    return envelope(
        {
            "schema": POLICY_VERSION,
            "code_commit": code_commit,
            "pool_sha256": pool["sha256"],
            "audit_sha256": audit["sha256"],
            "data_hash": audit["body"]["data_hash"],
            "d98_freeze_digest": D98_FREEZE_DIGEST,
            "policy": POLICY,
        }
    )


def git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    output = subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=not binary,
        encoding=None if binary else "utf-8",
    )
    return output if binary else output.strip()


def validate_checkout(
    root: Path,
    reg: dict[str, Any],
    pool: dict[str, Any],
    audit: dict[str, Any],
    *,
    registration_file: Path,
    pool_file: Path,
    audit_file: Path,
) -> None:
    expected = registration(reg["body"]["code_commit"], pool, audit)
    if reg != expected:
        raise ValueError("D101 registration conditions or identity mismatch")
    code_commit = reg["body"]["code_commit"]
    if git(root, "rev-parse", "HEAD") != code_commit:
        raise ValueError("checkout must be the registered C commit")
    if git(root, "status", "--porcelain"):
        raise ValueError("checkout must be clean")
    subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", code_commit, "origin/main"],
        check=True,
        capture_output=True,
    )
    published_inputs = (
        (REGISTRATION_PATH, registration_file),
        (POOL_PATH, pool_file),
        (AUDIT_PATH, audit_file),
        (D98_REGISTRATION_PATH, root / D98_REGISTRATION_PATH),
    )
    for relative, local_file in published_inputs:
        local = local_file.read_bytes()
        published = git(root, "show", f"origin/main:{relative}", binary=True)
        if local != published:
            raise ValueError(f"published input bytes differ: {relative}")
    d98_reg = read_artifact(root / D98_REGISTRATION_PATH)
    if d98_reg["sha256"] != D98_FREEZE_DIGEST:
        raise ValueError("published D98 freeze registration digest mismatch")


def verify_frozen_dataset(
    pool: dict[str, Any],
    published_audit: dict[str, Any],
    db: Path,
    *,
    workers: int = 1,
    expected_freeze: str = D98_FREEZE_DIGEST,
    expected_pool_digest: str = D98_POOL_DIGEST,
    expected_data_hash: str = D98_DATA_HASH,
) -> dict[str, Any]:
    """Check freeze and recompute D98 audit in read-only mode before D101 calculation."""
    if workers < 1:
        raise ValueError("positive worker count required")
    if digest(pool["body"]) != pool["sha256"]:
        raise ValueError("pool digest mismatch")
    if digest(published_audit["body"]) != published_audit["sha256"]:
        raise ValueError("published audit digest mismatch")
    if published_audit["body"].get("pool_sha256") != pool["sha256"]:
        raise ValueError("published audit pool mismatch")
    if pool["sha256"] != expected_pool_digest:
        raise ValueError("unregistered D98 source dataset")

    store = Store(db, readonly=True)
    try:
        row = store.db.execute("SELECT value FROM identity WHERE key='frozen'").fetchone()
        if row is None or row[0] != expected_freeze:
            raise ValueError("D98 dataset freeze registration mismatch")
        pool_row = store.db.execute("SELECT value FROM identity WHERE key='pool'").fetchone()
        if pool_row is None or pool_row[0] != pool["sha256"]:
            raise ValueError("D98 dataset pool identity mismatch")
    finally:
        store.close()

    recomputed = _audit(pool, db, workers=workers)
    if recomputed["body"]["data_hash"] != published_audit["body"].get("data_hash"):
        raise ValueError("D98 data_hash differs from published audit")
    if recomputed["body"]["data_hash"] != expected_data_hash:
        raise ValueError("D98 data_hash differs from D101 preregistration")
    return recomputed


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(encode(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _completed(directory: Path, registration_digest: str) -> dict[str, Any]:
    result = read_artifact(directory / "D101-study.json")
    # Study JSON includes execution metadata; its sha256 intentionally covers body only.
    if result["body"].get("registration_digest") != registration_digest:
        raise ValueError("completed result registration mismatch")
    trades = directory / "D101-study-trades.jsonl"
    import hashlib

    hasher = hashlib.sha256(trades.read_bytes())
    if hasher.hexdigest() != result["body"]["trades"]["sha256"]:
        raise ValueError("completed trade stream digest mismatch")
    if (directory / "D101-study.md").read_bytes() != markdown(result).encode("utf-8"):
        raise ValueError("completed Markdown bytes mismatch")
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
) -> dict[str, Any]:
    """Validate registered bytes, frozen D98 data, then run or return the single result."""
    reg = read_artifact(registration_file)
    pool = read_artifact(pool_file)
    published_audit = read_artifact(audit_file)
    validate_checkout(
        root,
        reg,
        pool,
        published_audit,
        registration_file=registration_file,
        pool_file=pool_file,
        audit_file=audit_file,
    )
    # Verification is intentionally before creating output state or calculating signals.
    verify_frozen_dataset(pool, published_audit, db, workers=workers)
    if published_audit["body"]["status"] != "READY":
        return {"status": published_audit["body"]["status"]}

    directory = output_root / reg["sha256"]
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / "state.json"
    with execution_gate(directory / "execution.gate"):
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else None
        if state and state.get("status") == "COMPLETED":
            result = _completed(directory, reg["sha256"])
            if result["sha256"] != state.get("result_sha256"):
                raise ValueError("completed result state digest mismatch")
            return result
        if state and state.get("status") == "RUNNING" and not resume_crashed:
            raise ValueError("D101 execution interrupted; explicit --resume-crashed required")
        for name in ("D101-study.json", "D101-study.md", "D101-study-trades.jsonl"):
            (directory / name).unlink(missing_ok=True)
        attempts = list(state.get("attempts", [])) if state else []
        attempts.append(datetime.now(UTC).isoformat())
        running = {"status": "RUNNING", "attempts": attempts}
        _atomic_json(state_file, running)
        try:
            with TemporaryDirectory(prefix="d101-attempt-", dir=directory) as temp:
                stage = Path(temp)
                result = study(
                    pool,
                    published_audit,
                    db,
                    stage,
                    workers=workers,
                    registration_digest=reg["sha256"],
                )
                for name in ("D101-study.json", "D101-study.md", "D101-study-trades.jsonl"):
                    os.replace(stage / name, directory / name)
            _atomic_json(
                state_file,
                {
                    "status": "COMPLETED",
                    "attempts": attempts,
                    "result_sha256": result["sha256"],
                },
            )
            return result
        except Exception:
            # Preserve RUNNING and partial state for explicit recovery/audit.
            raise
