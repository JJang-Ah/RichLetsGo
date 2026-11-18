"""Aggregate D103 summary.json into the report tables (rules fixed in D103-structure.md).

Read-only: reads only summary.json. The aggregation lives in
``richletsgo.research.d103.tables.build`` so it is typed and tested.
"""

import argparse
import json
from pathlib import Path

from richletsgo.research.d103.tables import build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.summary.read_text(encoding="utf-8"))
    args.output.write_text(
        json.dumps(build(document), ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
