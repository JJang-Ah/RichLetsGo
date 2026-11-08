"""Run D98 only from a clean registered C checkout with published inputs."""

import argparse
from pathlib import Path

import richletsgo
from richletsgo.backtest.codec import encode
from richletsgo.research.d98 import execution


def checked_worktree_root() -> Path:
    """Bind the CLI location to its imported package before reading any inputs."""
    root = Path(__file__).resolve().parents[1]
    expected = root / "src" / "richletsgo"
    imported = Path(richletsgo.__file__).resolve().parent if richletsgo.__file__ else None
    execution_file = Path(execution.__file__).resolve()
    if (
        imported != expected.resolve()
        or execution_file != (expected / "research" / "d98" / "execution.py").resolve()
    ):
        raise RuntimeError(
            f"D98 worktree/package mismatch: CLI worktree={root}; "
            f"imported package={imported}; execution module={execution_file}. "
            "Create a dedicated venv in this C worktree, run its Python with "
            "'-m pip install -e .' from this worktree, then use that Python to run this CLI."
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
    print(
        encode(
            execution.run_registered(
                root,
                args.preregistration,
                args.pool,
                args.audit,
                args.db,
                args.output_root,
                workers=args.workers,
                resume_crashed=args.resume_crashed,
            )
        )
    )


if __name__ == "__main__":
    main()
