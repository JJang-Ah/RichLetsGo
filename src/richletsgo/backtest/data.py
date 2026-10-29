"""D77 full-account preflight. No simulation side effects before validation succeeds."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from richletsgo.domain import Symbol
from richletsgo.marketdata.models import FundingRate, InstrumentSnapshot, Interval, RiskLimitTier
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.marketdata.validate import expected_starts, funding_gaps
from richletsgo.strategy.candidates import required_bars
from richletsgo.strategy.history import ClosedBars
from richletsgo.strategy.models import S1AParams, S1BParams, S2AParams, S2BParams, S2PParams

from .codec import encode


@dataclass(frozen=True, slots=True)
class DataIssue:
    symbol: Symbol
    kind: str
    first: datetime
    last: datetime


@dataclass(frozen=True, slots=True)
class RunData:
    histories: dict[tuple[Symbol, Interval], ClosedBars]
    funding: dict[tuple[Symbol, datetime], FundingRate]
    instruments: dict[Symbol, InstrumentSnapshot]
    tiers: dict[Symbol, RiskLimitTier]
    issues: tuple[DataIssue, ...]
    data_hash: str


def load_data(
    store: MarketDataStore,
    start: datetime,
    end: datetime,
    symbols: tuple[Symbol, ...],
    s1: S1AParams | S1BParams,
    s2: S2AParams | S2BParams | S2PParams | None,
) -> RunData:
    warmup = max(
        timedelta(days=required_bars(s1)),
        timedelta(hours=required_bars(s2) * (4 if isinstance(s2, S2PParams) else 1))
        if s2 is not None
        else timedelta(0),
    )
    first = Interval.DAY.floor(start - warmup)
    histories = {}
    funding = {}
    instruments = {}
    tiers = {}
    issues = []
    hashed = hashlib.sha256()

    def include(item: object) -> None:
        hashed.update(encode(item).encode("utf-8") + b"\n")

    for symbol in symbols:
        for interval in (Interval.DAY, Interval.HOUR):
            # Only complete bars ending at/before end, never future daily OHLC.
            last = interval.floor(end) - interval.duration
            bars = tuple(store.load_bars(symbol, interval, first, last))
            present = {b.start for b in bars}
            for missing in expected_starts(interval, first, last):
                if missing not in present:
                    issues.append(
                        DataIssue(symbol, "missing_bar:" + interval.value, missing, missing)
                    )
            if len(bars) == len(present):
                try:
                    histories[(symbol, interval)] = ClosedBars(symbol, interval, bars, end)
                except ValueError:
                    issues.append(DataIssue(symbol, "invalid_bars:" + interval.value, first, last))
            for bar in bars:
                include(bar)
        instrument = store.latest_instrument(symbol)
        tier = next((t for t in store.latest_risk_tiers(symbol) if t.tier == 1), None)
        if instrument is None:
            issues.append(DataIssue(symbol, "missing_instrument", start, end))
        else:
            instruments[symbol] = instrument
            include(instrument)
            if instrument.funding_interval <= timedelta(0):
                issues.append(DataIssue(symbol, "invalid_funding_interval", start, end))
            else:
                # One interval of look-behind detects a missing first settlement.
                lower = start - instrument.funding_interval
                rates = store.load_funding(symbol, lower, end - timedelta(microseconds=1))
                for a, b in funding_gaps(
                    [r.funding_time for r in rates],
                    instrument.funding_interval,
                    lower,
                    end - timedelta(microseconds=1),
                ):
                    issues.append(DataIssue(symbol, "funding_gap", a, b))
                for rate in rates:
                    if not Interval.HOUR.aligned(rate.funding_time):
                        issues.append(
                            DataIssue(
                                symbol, "intrahour_funding", rate.funding_time, rate.funding_time
                            )
                        )
                    if rate.funding_time >= start:
                        funding[(symbol, rate.funding_time)] = rate
                    include(rate)
        if tier is None:
            issues.append(DataIssue(symbol, "missing_tier_1", start, end))
        else:
            tiers[symbol] = tier
            include(tier)
    return RunData(histories, funding, instruments, tiers, tuple(issues), hashed.hexdigest())
