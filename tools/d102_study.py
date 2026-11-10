"""Run D102 once from its clean, registered C checkout."""

import argparse
from pathlib import Path

import richletsgo
from richletsgo.backtest.codec import encode
from richletsgo.research.d102 import execution


def checked_worktree_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    package_root = root / "src" / "richletsgo"
    imported = Path(richletsgo.__file__).resolve().parent if richletsgo.__file__ else None
    execution_file = Path(execution.__file__).resolve()
    if (
        imported != package_root.resolve()
        or execution_file != (package_root / "research" / "d102" / "execution.py").resolve()
    ):
        raise RuntimeError(
            f"D102 worktree/package mismatch: CLI worktree={root}; imported package={imported}; "
            f"execution module={execution_file}. Create a dedicated venv in this C worktree, "
            "run its Python with '-m pip install -e .' from this worktree, then use that Python."
        )
    return root


def main() -> None:
    root = checked_worktree_root()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("preregistration", "pool", "audit", "db", "output-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--resume-crashed", action="store_true")
    args = parser.parse_args()
    result = execution.run_registered(
        root,
        args.preregistration,
        args.pool,
        args.audit,
        args.db,
        args.output_root,
        workers=args.workers,
        resume_crashed=args.resume_crashed,
    )
    print(encode(result))


if __name__ == "__main__":
    main()
