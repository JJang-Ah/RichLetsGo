"""Portable single-file report; deterministic research body and separate run metadata."""

import json
from typing import Any

from .codec import digest


def block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n```"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    def line(row: list[Any]) -> str:
        return (
            "| "
            + " | ".join(
                str(v).replace("|", "\\|").replace("\n", " ") if v is not None else "N/A"
                for v in row
            )
            + " |"
        )

    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def render(body: dict[str, Any], manifest: dict[str, Any]) -> str:
    identity = body["identity"]
    sections = [
        "# M2-04 walk-forward research report",
        "No guarantee of profit. Research only; the user chooses the final strategy.",
        "## Reproducibility",
        f"Research body SHA-256: `{digest(body)}`",
        block(identity),
        "Score = net P&L / max(hourly maximum drawdown in USDT, configured floor). "
        "Joint S1/S2 candidates must meet both minimum trade counts. Ties: more trades, then numeric S1 tuple followed by S2 tuple in dataclass field order.",
        "## Collection and validation",
        block(
            {
                "ingest_runs": body["ingest_runs"],
                "validation": body["validation"],
                "conflicts": body["conflicts"],
            }
        ),
        "## Four-combination comparison",
        table(
            [
                "Combination",
                "V1",
                "Net USDT",
                "Max DD USDT",
                "Max DD ratio",
                "Trades",
                "Win rate",
                "Average win/loss",
                "Average R",
                "Max losing streak",
                "Invalid/no candidate",
            ],
            [
                [
                    name,
                    a["verdict"],
                    a["net_pnl"],
                    a["max_drawdown"],
                    a["max_drawdown_ratio"],
                    a["trades"],
                    a["win_rate"],
                    a["average_win_loss_ratio"],
                    a["average_r"],
                    a["max_consecutive_losses"],
                    a["invalid_windows"],
                ]
                for name, a in sorted(body["aggregates"].items())
            ],
        ),
    ]
    for name, result in sorted(body["aggregates"].items()):
        windows = body["windows"][name]
        sections += [
            "## " + name,
            "### Selected parameters and independent windows",
            table(
                [
                    "Test start",
                    "Test end",
                    "Status",
                    "S1 / S2 parameters",
                    "Training score",
                    "Net USDT",
                    "Pauses",
                    "Forced closes",
                ],
                [
                    [
                        w["start"],
                        w["end"],
                        w["status"],
                        json.dumps(w["selected"], sort_keys=True),
                        w.get("selection_score"),
                        w.get("summary", {}).get("net_pnl", "0"),
                        json.dumps(w.get("summary", {}).get("pause_counts", {}), sort_keys=True),
                        w.get("summary", {}).get("forced_closes", 0),
                    ]
                    for w in windows
                ],
            ),
        ]
        sections += [
            "### Training candidate diagnostics",
            table(
                [
                    "Test start",
                    "Parameters",
                    "Status",
                    "S1 trades",
                    "S2 trades",
                    "Net USDT",
                    "Max DD USDT",
                    "Pauses",
                    "Forced closes",
                ],
                [
                    [
                        w["start"],
                        json.dumps(t["parameters"], sort_keys=True),
                        t["summary"]["status"],
                        t["summary"]["trade_counts"]["S1"],
                        t["summary"]["trade_counts"]["S2"],
                        t["summary"]["net_pnl"],
                        t["summary"]["max_drawdown"],
                        json.dumps(t["summary"]["pause_counts"], sort_keys=True),
                        t["summary"]["forced_closes"],
                    ]
                    for w in windows
                    for t in w["training"]
                ],
            ),
        ]
        monthly_curve = {}
        for row in result["equity"]:
            monthly_curve[row["at"][:7]] = row
        sections += [
            "### Additive equity (month-end samples; drawdown uses every hour)",
            table(
                ["At UTC", "Equity USDT"], [[r["at"], r["equity"]] for r in monthly_curve.values()]
            ),
            "### Monthly P&L",
            table(
                ["Month", "Net USDT"], [[m, v] for m, v in sorted(result["monthly_pnl"].items())]
            ),
            "### Strategy and symbol breakdown",
            block({"strategy": result["by_strategy"], "symbol": result["by_symbol"]}),
            "### V2 expected-range baseline",
            block(
                {
                    "window_max_drawdowns": [
                        {
                            "start": w["start"],
                            "status": w["status"],
                            "drawdown_usdt": w.get("summary", {}).get("max_drawdown"),
                            "drawdown_ratio": w.get("summary", {}).get("max_drawdown_ratio"),
                        }
                        for w in windows
                    ],
                    "trade_net_pnl_quantiles": result["pnl_distribution"],
                }
            ),
            "### Closed trade summary",
            table(
                [
                    "Opened UTC",
                    "Closed UTC",
                    "Strategy",
                    "Symbol",
                    "Direction",
                    "Entry",
                    "Exit",
                    "Net USDT",
                    "R",
                    "Exit reasons",
                ],
                [
                    [
                        t[k]
                        for k in (
                            "opened_at",
                            "closed_at",
                            "strategy",
                            "symbol",
                            "direction",
                            "entry_price",
                            "exit_price",
                            "net_pnl",
                            "r_multiple",
                            "reasons",
                        )
                    ]
                    for t in result["trade_records"]
                ],
            ),
        ]
    sections += [
        "## Assumptions and limitations",
        "- Each window starts with the same initial equity, an empty account and no pause state. Out-of-sample P&L is added, not compounded. Window resets can hide persistent risk halts.\n"
        "- All runs close within their final hour, including fees, slippage and funding. This truncates long S1 trends. Signals fill at the next bar open; ambiguous intrabar events prioritize the stop.\n"
        "- Stop-query confirmation may lag by one hourly boundary and block new entries during that boundary (M2-03 P3).\n"
        "- Instrument rules and risk tiers are pinned collection-time snapshots, not historical snapshots.\n"
        "- Optional one-minute ambiguity revalidation was not performed. Hourly OHLC cannot reconstruct intrabar paths.\n"
        "- INVALID_DATA and NO_CANDIDATE windows are flat and count toward the invalid-window limit. UNSET means pass criteria were omitted. PASS requires strictly positive configured excess P&L and drawdown strictly below the configured limit.\n"
        "- This is one explicitly configured small grid; no tuning was performed using test results. Fees/slippage and all risk limits are recorded above.",
        "## Execution metadata (excluded from research body hash)",
        block(
            {
                "created_at": manifest["created_at"],
                "elapsed_seconds": manifest.get("last_elapsed_seconds"),
                "operations": manifest["operations"],
                "published_artifacts": len(manifest["artifacts"]),
            }
        ),
    ]
    return "\n\n".join(sections) + "\n"
