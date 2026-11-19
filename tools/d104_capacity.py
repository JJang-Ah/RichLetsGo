"""Publish one D104 capacity summary after review of the effective scope."""

import argparse
import json
from pathlib import Path

from richletsgo.research.d104.capacity_guard import collect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    result = collect(args.reports)
    with args.output.open("xb") as handle:
        handle.write((json.dumps(result, sort_keys=True, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
