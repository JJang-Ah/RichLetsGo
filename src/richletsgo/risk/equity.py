"""Account equity, daily PnL (D72, D81) and the two peaks (D48)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from richletsgo.domain.clock import require_utc, trading_day_start
from richletsgo.domain.values import finite_decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Exchange-reported wallet balance and unrealized PnL, kept separate (D72)."""

    at: datetime
    wallet_balance: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        require_utc(self.at)
        finite_decimal(self.wallet_balance, "wallet_balance")
        finite_decimal(self.unrealized_pnl, "unrealized_pnl")

    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl


@dataclass(frozen=True, slots=True)
class CashFlow:
    """Deposit (+) or withdrawal (-) read from exchange records (D71)."""

    flow_id: str
    at: datetime
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.flow_id:
            raise ValueError("flow_id is required")
        require_utc(self.at)
        if finite_decimal(self.amount, "amount") == 0:
            raise ValueError("cash flow amount must be nonzero")


@dataclass(frozen=True, slots=True)
class PeakReset:
    """Audit record for a confirmed HALTED resume (D48)."""

    at: datetime
    actor: str
    previous_risk_peak: Decimal
    new_risk_peak: Decimal
    record_peak: Decimal


@dataclass(frozen=True, slots=True)
class TrackerState:
    day_start: datetime
    baseline: Decimal
    net_deposit: Decimal
    latest: AccountSnapshot
    record_peak: Decimal
    risk_peak: Decimal
    seen_flows: frozenset[str]


class EquityTracker:
    """Caller drives boundaries: start_day at UTC 00:00 after funding (D76), observe otherwise."""

    def __init__(self, state: TrackerState) -> None:
        self._s = state

    @classmethod
    def open(cls, snapshot: AccountSnapshot) -> "EquityTracker":
        return cls(
            TrackerState(
                trading_day_start(snapshot.at),
                snapshot.wallet_balance,
                Decimal(0),
                snapshot,
                snapshot.equity,
                snapshot.equity,
                frozenset(),
            )
        )

    @property
    def state(self) -> TrackerState:
        return self._s

    def start_day(self, snapshot: AccountSnapshot) -> None:
        """New trading day: baseline = wallet balance excluding unrealized PnL (D72)."""
        day = trading_day_start(snapshot.at)
        if day <= self._s.day_start:
            raise ValueError("start_day requires a later trading day")
        self._s = TrackerState(
            day,
            snapshot.wallet_balance,
            Decimal(0),
            snapshot,
            max(self._s.record_peak, snapshot.equity),
            max(self._s.risk_peak, snapshot.equity),
            self._s.seen_flows,
        )

    def observe(self, snapshot: AccountSnapshot, cash_flows: Sequence[CashFlow] = ()) -> None:
        """The snapshot must already include the given flows; apply both together.

        Peaks shift by each flow so deposits/withdrawals are not drawdown (02 1장).
        """
        if trading_day_start(snapshot.at) != self._s.day_start:
            raise ValueError("snapshot is outside the current trading day; call start_day")
        if snapshot.at < self._s.latest.at:
            raise ValueError("snapshot is older than the latest observation")
        net = self._s.net_deposit
        record, risk = self._s.record_peak, self._s.risk_peak
        seen = set(self._s.seen_flows)
        for flow in cash_flows:
            if flow.flow_id in seen:
                continue
            if trading_day_start(flow.at) != self._s.day_start or flow.at > snapshot.at:
                raise ValueError("cash flow must belong to the current day and the snapshot")
            seen.add(flow.flow_id)
            net += flow.amount
            record += flow.amount
            risk += flow.amount
        self._s = TrackerState(
            self._s.day_start,
            self._s.baseline,
            net,
            snapshot,
            max(record, snapshot.equity),
            max(risk, snapshot.equity),
            frozenset(seen),
        )

    @property
    def equity(self) -> Decimal:
        return self._s.latest.equity

    @property
    def day_start(self) -> datetime:
        return self._s.day_start

    @property
    def daily_pnl(self) -> Decimal:
        """D81: (current equity - net deposits) - baseline."""
        return (self.equity - self._s.net_deposit) - self._s.baseline

    @property
    def daily_loss(self) -> Decimal:
        return max(Decimal(0), -self.daily_pnl)

    @property
    def record_peak(self) -> Decimal:
        return self._s.record_peak

    @property
    def risk_peak(self) -> Decimal:
        return self._s.risk_peak

    @property
    def drawdown_ratio(self) -> Decimal:
        """Drawdown from the risk-baseline peak (D24, D48)."""
        if self._s.risk_peak <= 0:
            return Decimal(1)
        return max(Decimal(0), (self._s.risk_peak - self.equity) / self._s.risk_peak)

    def reset_risk_peak(self, value: Decimal, at: datetime, actor: str) -> PeakReset:
        """Only for a confirmed HALTED resume. The record peak is never lowered."""
        require_utc(at)
        if finite_decimal(value, "value") <= 0:
            raise ValueError("risk peak must be positive")
        previous = self._s.risk_peak
        new = max(value, self.equity)
        self._s = TrackerState(
            self._s.day_start,
            self._s.baseline,
            self._s.net_deposit,
            self._s.latest,
            self._s.record_peak,
            new,
            self._s.seen_flows,
        )
        return PeakReset(at, actor, previous, new, self._s.record_peak)
