"""Backtest data ingestion (M2-01). The only module in M2 that touches the network."""

import argparse
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

from richletsgo.domain import Symbol
from richletsgo.domain.clock import require_utc

from .bybit_public import (
    BybitApiError,
    BybitPublicClient,
    RetryPolicy,
    Transport,
    UrllibTransport,
)
from .models import Bar, FundingRate, Interval
from .store import MarketDataStore
from .validate import funding_gaps, missing_bar_ranges, parse_funding, parse_klines

DEFAULT_HISTORY = timedelta(days=3 * 365)  # D70: three years
MAX_REJECTED_DETAIL = 200  # raw rows kept per series in the run record; the count is exact


@dataclass(slots=True)
class SeriesReport:
    name: str
    requested_ranges: list[tuple[str, str]] = field(default_factory=list)
    received: int = 0
    stored: int = 0
    rejected: int = 0
    rejected_rows: list[dict[str, object]] = field(default_factory=list)
    in_progress_dropped: int = 0
    gaps: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.conflicts


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def ingest_bars(
    client: BybitPublicClient,
    store: MarketDataStore,
    symbol: Symbol,
    interval: Interval,
    first: datetime,
    now: datetime,
) -> SeriesReport:
    """Fetch only missing closed bars in [first, last closed bar] and report remaining gaps.

    One series is one transaction (I10): every response is fetched and checked first,
    and nothing is stored if any response conflicts or any request fails.
    """
    report = SeriesReport(f"bars {symbol.value} {interval.value}")
    last = interval.floor(now) - interval.duration  # last bar that is fully closed
    if last < first:
        return report
    try:
        missing = missing_bar_ranges(
            interval, store.bar_starts(symbol, interval, first, last), first, last
        )
        fetched: list[Bar] = []
        for lo, hi in missing:
            report.requested_ranges.append((_iso(lo), _iso(hi)))
            rows = client.klines(symbol, interval, lo, hi)
            batch = parse_klines(rows, symbol, interval, now)
            report.received += len(rows)
            report.rejected += len(batch.rejected)
            for row in batch.rejected[: MAX_REJECTED_DETAIL - len(report.rejected_rows)]:
                report.rejected_rows.append(
                    {"range": [_iso(lo), _iso(hi)], "reason": row.reason, "raw": list(row.raw)}
                )
            report.in_progress_dropped += batch.in_progress_dropped
            report.conflicts += [f"response {_iso(t)}" for t in batch.conflicts]
            fetched += [b for b in batch.bars if lo <= b.start <= hi]
        if not report.conflicts:
            result = store.put_bars(fetched)
            report.stored += result.inserted
            report.conflicts += list(result.conflicts)
    except BybitApiError as exc:
        report.error = str(exc)
    remaining = missing_bar_ranges(
        interval, store.bar_starts(symbol, interval, first, last), first, last
    )
    report.gaps = [(_iso(a), _iso(b)) for a, b in remaining]
    return report


def funding_request_spans(
    times: Sequence[datetime], interval: timedelta, first: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Ranges to (re)query: both edges always (a record may sit exactly one interval
    away, and D87 trusts recorded times, not the current interval), plus every inner
    stretch longer than the captured interval so a later run can fill it."""
    if not times:
        return [(first, now)]
    spans: list[tuple[datetime, datetime]] = []
    if times[0] > first:
        spans.append((first, times[0]))
    spans += [(a, b) for a, b in zip(times, times[1:], strict=False) if b - a > interval]
    if now > times[-1]:
        spans.append((times[-1], now))
    return spans


def ingest_funding(
    client: BybitPublicClient,
    store: MarketDataStore,
    symbol: Symbol,
    first: datetime,
    now: datetime,
    funding_interval: timedelta,
) -> SeriesReport:
    """Fetch edges and inner gaps of the stored span; gaps follow D87 (recorded times,
    captured interval). Like bars, the whole series is stored in one transaction."""
    report = SeriesReport(f"funding {symbol.value}")
    try:
        times = sorted(t for t in store.funding_times(symbol) if first <= t <= now)
        fetched: dict[datetime, FundingRate] = {}
        for lo, hi in funding_request_spans(times, funding_interval, first, now):
            report.requested_ranges.append((_iso(lo), _iso(hi)))
            rows = client.funding_history(symbol, lo, hi)
            report.received += len(rows)
            for rate in parse_funding(rows, symbol):
                previous = fetched.get(rate.funding_time)
                if previous is not None and previous.rate != rate.rate:
                    raise ValueError(
                        f"conflicting funding rates at {rate.funding_time.isoformat()}"
                    )
                fetched[rate.funding_time] = rate
        result = store.put_funding([fetched[k] for k in sorted(fetched)])
        report.stored += result.inserted
        report.conflicts += list(result.conflicts)
    except (BybitApiError, ValueError) as exc:
        report.error = str(exc)
    stored_times = [t for t in store.funding_times(symbol) if first <= t <= now]
    report.gaps = [
        (_iso(a), _iso(b)) for a, b in funding_gaps(stored_times, funding_interval, first, now)
    ]
    return report


@dataclass(slots=True)
class IngestResult:
    reports: list[SeriesReport]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.reports)


def run_ingest(
    client: BybitPublicClient,
    store: MarketDataStore,
    *,
    symbols: Sequence[Symbol],
    intervals: Sequence[Interval],
    since: datetime,
    now: datetime,
    minute_range: tuple[datetime, datetime] | None,
    with_funding: bool,
    end: datetime | None = None,
) -> IngestResult:
    require_utc(since)
    require_utc(now)
    end = now if end is None else min(end, now)
    reports: list[SeriesReport] = []
    for symbol in symbols:
        snapshot_report = SeriesReport(f"snapshots {symbol.value}")
        try:
            snapshot = client.instrument(symbol, now)
            tiers = client.risk_limit_tiers(symbol, now)
            for result in (store.put_instrument(snapshot), store.put_risk_tiers(tiers)):
                snapshot_report.stored += result.inserted
                snapshot_report.conflicts += list(result.conflicts)
        except (BybitApiError, ValueError, KeyError) as exc:
            snapshot_report.error = str(exc)
            snapshot = None
        reports.append(snapshot_report)
        for interval in intervals:
            if interval is Interval.MINUTE:
                if minute_range is None:
                    raise ValueError("1-minute bars require an explicit --from/--to range")
                lo, hi = minute_range
                reports.append(ingest_bars(client, store, symbol, interval, lo, min(hi, end)))
            else:
                reports.append(ingest_bars(client, store, symbol, interval, since, end))
        if with_funding:
            if snapshot is None:
                reports.append(SeriesReport(f"funding {symbol.value}", error="no instrument"))
            else:
                reports.append(
                    ingest_funding(client, store, symbol, since, end, snapshot.funding_interval)
                )
    return IngestResult(reports)


# ---------------------------------------------------------------------- CLI


def _utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return require_utc(value)


def load_settings(config: Path) -> tuple[Path, str]:
    """Read only the two keys M2-01 needs; the full loader is M3 (PR #12 I3)."""
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    try:
        raw_dir = data["paths"]["backtest"]["market_data_dir"]
        base_url = data["bybit_public"]["base_url"]
    except KeyError as exc:
        raise SystemExit(f"missing config key: {exc}") from exc
    directory = Path(raw_dir)
    if not directory.is_absolute():
        directory = config.resolve().parent / directory  # relative to the config file (03 3.1)
    return directory, str(base_url)


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    transport_factory: Callable[[], Transport] = UrllibTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m richletsgo.marketdata.ingest")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", choices=[s.value for s in Symbol], required=True)
    parser.add_argument(
        "--intervals", nargs="+", choices=[i.value for i in Interval], required=True
    )
    parser.add_argument("--since", type=_utc, help="UTC start; default now - 3 years (D70)")
    parser.add_argument("--from", dest="minute_from", type=_utc)
    parser.add_argument("--to", dest="minute_to", type=_utc, help="exclusive UTC end for 1m bars")
    parser.add_argument(
        "--until", type=_utc, help="exclusive UTC data end; snapshots retain actual capture time"
    )
    parser.add_argument("--preregistration", type=Path)
    parser.add_argument("--skip-funding", action="store_true")
    parser.add_argument("--pause", type=float, default=0.2)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--backoff", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--commit", default="unknown")
    args = parser.parse_args(argv)

    started = now()
    since = args.since or started - DEFAULT_HISTORY
    minute_range = None
    if Interval.MINUTE.value in args.intervals:
        if args.minute_from is None or args.minute_to is None:
            parser.error("1-minute bars require --from and --to")
        minute_range = (args.minute_from, args.minute_to)
    from richletsgo.backtest.registration import check_collection

    end = min(args.until, started) if args.until is not None else started
    check_collection(args.preregistration, tuple(Symbol(s) for s in args.symbols), since, end)
    directory, base_url = load_settings(args.config)
    client = BybitPublicClient(
        base_url,
        transport=transport_factory(),
        policy=RetryPolicy(args.pause, args.max_attempts, args.backoff, args.timeout),
        sleep=sleep,
    )
    with MarketDataStore(directory) as store:
        result = run_ingest(
            client,
            store,
            symbols=[Symbol(s) for s in args.symbols],
            intervals=[Interval(i) for i in args.intervals],
            since=since,
            now=started,
            minute_range=minute_range,
            with_funding=not args.skip_funding,
            end=end,
        )
        summary: dict[str, object] = {
            "request": {
                "symbols": args.symbols,
                "intervals": args.intervals,
                "since": _iso(since),
                "now": _iso(started),
                **({"end": _iso(end)} if args.until is not None else {}),
                "minute_range": [_iso(t) for t in minute_range] if minute_range else None,
                "with_funding": not args.skip_funding,
            },
            "series": {r.name: _as_dict(r) for r in result.reports},
        }
        if args.preregistration is not None:
            summary["request_attempts"] = client.attempts
        store.record_run(
            started=started,
            finished=now(),
            summary=summary,
            ok=result.ok,
            package_version=version("richletsgo"),
            code_commit=args.commit,
        )
    for report in result.reports:
        status = "OK" if report.ok else "FAIL"
        print(
            f"[{status}] {report.name}: received={report.received} stored={report.stored} "
            f"rejected={report.rejected} in_progress_dropped={report.in_progress_dropped} "
            f"gaps={len(report.gaps)} conflicts={len(report.conflicts)}"
            + (f" error={report.error}" if report.error else "")
        )
        for lo, hi in report.gaps[:20]:
            print(f"    gap {lo} .. {hi}")
    return 0 if result.ok else 1


def _as_dict(report: SeriesReport) -> dict[str, object]:
    return {
        "requested_ranges": report.requested_ranges,
        "received": report.received,
        "stored": report.stored,
        "rejected": report.rejected,
        "rejected_rows": report.rejected_rows,
        "in_progress_dropped": report.in_progress_dropped,
        "gaps": report.gaps,
        "conflicts": report.conflicts,
        "error": report.error,
    }


if __name__ == "__main__":
    sys.exit(main())
