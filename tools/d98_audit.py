"""Audit D98 coverage and stream data hashes without computing signals."""

import argparse
import subprocess
import tomllib
from pathlib import Path

from richletsgo.backtest.codec import digest, encode
from richletsgo.marketdata.ingest import load_settings
from richletsgo.research.d98.audit import audit
from richletsgo.research.d98.boundaries import load_pool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--export-log",
        type=Path,
        help="Optional full .jsonl.gz log; hash covers decompressed bytes",
    )
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(".md").exists():
        raise ValueError("output exists; preserve prior audits")
    if args.export_log is not None:
        if args.export_log.resolve() in {
            args.output.resolve(),
            args.output.with_suffix(".md").resolve(),
        }:
            raise ValueError("log export conflicts with audit output")
        if args.export_log.exists():
            raise ValueError("log export exists; preserve prior logs")
        args.export_log.parent.mkdir(parents=True, exist_ok=True)
    directory, _ = load_settings(args.config)
    settings = tomllib.loads(args.config.read_text(encoding="utf-8"))["d98"]["collection"]
    result = audit(
        load_pool(args.pool),
        args.db or directory / "d98-design.sqlite",
        workers=settings["workers"],
        export_log=args.export_log,
    )
    result["body"]["code_commit"] = subprocess.check_output(
        ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    result["sha256"] = digest(result["body"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        f.write(encode(result) + "\n")
    b = result["body"]
    with args.output.with_suffix(".md").open("x", encoding="utf-8") as f:
        f.write(
            f"# D98 collection audit\n\nStatus: {b['status']}\n\nDigest: `{result['sha256']}`\n\nData hash: `{b['data_hash']}`\n\nMetadata eligible: {b['metadata_eligible']}; missing input: {b['input_insufficient']}\n\nContracts: {len(b['contracts'])}. Details and request history are in the JSON. No signals or returns computed.\n"
        )
    print(b["status"], b["data_hash"])


if __name__ == "__main__":
    main()
