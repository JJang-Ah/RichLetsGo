"""Run D98 only from a clean registered C checkout with published inputs."""

import argparse
from pathlib import Path

from richletsgo.backtest.codec import encode
from richletsgo.research.d98 import execution


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("preregistration", "pool", "audit", "db", "output-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--resume-crashed", action="store_true")
    args = parser.parse_args()
    root = Path(execution.__file__).resolve().parents[4]
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
