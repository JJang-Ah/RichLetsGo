"""D98 metadata review; formal split only after reviewed tables reach main."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from richletsgo.backtest.codec import encode
from richletsgo.marketdata.bybit_public import BybitPublicClient, RetryPolicy, UrllibTransport
from richletsgo.research.d98 import tables
from richletsgo.research.d98.pool import MetadataTransport, build, collect, markdown, review

TABLE_PATH = "src/richletsgo/research/d98/tables.py"


def git(*args: str) -> str:
    # Resolve the checkout from the running module, not the caller's cwd.
    return subprocess.check_output(
        ["git", "-C", str(Path(tables.__file__).resolve().parent), *args],
        text=True,
        encoding="utf-8",
    ).strip()


def formal_guard() -> None:
    if not tables.TABLES_FROZEN:
        raise ValueError("tables await metadata/Claude review; use --review")
    if git("status", "--porcelain"):
        raise ValueError("formal split requires a clean checkout")
    local = git("rev-parse", f"HEAD:{TABLE_PATH}")
    published = git("rev-parse", f"origin/main:{TABLE_PATH}")
    if local != published:
        raise ValueError("table version is not on origin/main; fetch after merge")


def write_new(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def run(
    output: Path,
    *,
    review_mode: bool,
    client: BybitPublicClient,
    captured_at: datetime,
    commit: str,
) -> dict[str, Any]:
    if not review_mode:
        formal_guard()
    output = output.resolve()
    summary = output.with_suffix(".md")
    if output == summary or output.exists() or (not review_mode and summary.exists()):
        raise ValueError("output already exists or conflicts with summary")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Retain a failed-attempt marker: never silently recollect a formal split.
    marker = output.with_suffix(".started.json")
    write_new(
        marker,
        encode(
            {
                "captured_at": captured_at,
                "code_commit": commit,
                "mode": "review" if review_mode else "formal",
            }
        ),
    )
    try:
        rows = collect(client)
        result = (
            review(rows, captured_at, commit) if review_mode else build(rows, captured_at, commit)
        )
        write_new(output, encode(result) + "\n")
        if not review_mode:
            write_new(summary, markdown(result))
        return result
    finally:
        write_new(output.with_suffix(".requests.json"), encode(client.attempts) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = BybitPublicClient(
        "https://api.bybit.com",
        transport=MetadataTransport(UrllibTransport()),
        policy=RetryPolicy(0.2, 5, 1, 10),
        sleep=time.sleep,
    )
    result = run(
        args.output,
        review_mode=args.review,
        client=client,
        captured_at=datetime.now(UTC),
        commit=git("rev-parse", "HEAD"),
    )
    # Do not print symbol assignments to the console.
    print(
        json.dumps(
            {
                "output": str(args.output),
                "mode": "review" if args.review else "formal",
                "contracts": len(
                    result["instruments"] if args.review else result["body"]["contracts"]
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
