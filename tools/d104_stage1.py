"""Run registered D104 stage 1 only. Use a new campaign directory for collection."""

import argparse
import json
import subprocess
from pathlib import Path

from richletsgo.research.d104 import stage1
from richletsgo.research.d104.inputs import number
from richletsgo.research.d104.stage1_report import report


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("registration", "collect", "freeze", "run"))
    parser.add_argument("--registration", required=True, type=Path)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    if (
        root != Path(git("rev-parse", "--show-toplevel")).resolve()
        or not Path(stage1.__file__).resolve().is_relative_to(root / "src")
        or git("status", "--porcelain")
    ):
        parser.error("clean code checkout with checkout-local installation required")
    reports = root / "docs" / "reports"
    reg = stage1.packed(stage1.registration(git("rev-parse", "HEAD"), reports))
    if args.action == "registration":
        with args.registration.open("xb") as out:
            out.write(reg)
        return
    if args.registration.read_bytes() != reg or args.folder is None:
        parser.error("registration or folder mismatch")
    folder = args.folder.resolve()
    if folder.is_relative_to(root):
        parser.error("campaign folder must be outside the code checkout")
    if args.action == "collect":
        stage1.collect(folder, reports)
        return
    path = folder / "manifest.json"
    if args.action == "run" and (
        not args.manifest_sha256 or stage1.sha(path.read_bytes()) != args.manifest_sha256
    ):
        parser.error("published manifest hash required")
    inputs, manifest = stage1.fixtures(folder, reports)
    manifest["registration_sha256"] = stage1.sha(reg)
    blob = stage1.packed(manifest)
    if args.action == "freeze":
        with path.open("xb") as out:
            out.write(blob)
        print(stage1.sha(blob))
        return
    if path.read_bytes() != blob:
        parser.error("frozen input mismatch")
    with (folder / "run-started").open("xb") as marker:
        marker.write(stage1.sha(blob).encode())
    try:
        result = report(inputs)
    except ValueError as exc:
        result = dict(verdict="INVALID_INPUT", reason=str(exc))
    with (folder / "result.json").open("xb") as out:
        out.write((json.dumps(result, default=number, sort_keys=True, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
