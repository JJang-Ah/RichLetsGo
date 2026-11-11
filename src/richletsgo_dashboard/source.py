"""Read model for the dashboard (M5-01a). Screens read only through `DashboardSource`.

Money and quantities stay Decimal end to end; times are UTC and only formatted to KST
at render time. The SQLite reader arrives after M3-02; until then `FakeSource` serves
deterministic sample data.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Limit:
    code: str  # L1..L7
    name: str
    used: Decimal  # fraction of the limit in use, 0..1+
    detail: str


@dataclass(frozen=True)
class Overview:
    environment: str  # "demo" | "live"
    account_id: str
    pause_state: str  # e.g. ACTIVE, ENTRY_PAUSED_MANUAL, HALTED
    pause_reasons: tuple[str, ...]
    equity: Decimal
    today_pnl: Decimal
    drawdown: Decimal  # fraction below the risk peak, >= 0
    limits: tuple[Limit, ...]
    last_closed_bar: datetime | None
    last_fill: datetime | None
    key_expires_in_days: int | None
    ai_mode: str
    sample: bool = False  # True for FakeSource: shown as a banner on every screen


@dataclass(frozen=True)
class Position:
    strategy: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    stop_price: Decimal | None
    exchange_stop_quantity: Decimal

    @property
    def stop_distance(self) -> Decimal | None:
        if self.stop_price is None or self.mark_price <= 0:
            return None
        return abs(self.mark_price - self.stop_price) / self.mark_price

    @property
    def fully_protected(self) -> bool:
        return self.stop_price is not None and self.exchange_stop_quantity == self.quantity


@dataclass(frozen=True)
class EquityPoint:
    at: datetime
    equity: Decimal
    drawdown: Decimal


@dataclass(frozen=True)
class Order:
    order_link_id: str
    strategy: str
    symbol: str
    purpose: str
    side: str
    quantity: Decimal
    price: Decimal | None
    status: str
    filled: Decimal
    created_at: datetime


@dataclass(frozen=True)
class Fill:
    exec_id: str
    order_link_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    at: datetime


@dataclass(frozen=True)
class LogEntry:
    at: datetime
    kind: str  # alert | command | pause
    source: str  # telegram | web | system
    text: str  # untrusted: always rendered as text


@dataclass(frozen=True)
class Decision:
    signal_id: str
    at: datetime
    strategy: str
    symbol: str
    ai_score: Decimal | None
    reason: str  # untrusted
    cost_usd: Decimal


@dataclass
class Snapshot:
    overview: Overview
    positions: list[Position] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


class DashboardSource(Protocol):
    def snapshot(self) -> Snapshot: ...


D = Decimal


class FakeSource:
    """Deterministic sample data until the SQLite reader exists (M3-02)."""

    def __init__(self, *, now: datetime | None = None, environment: str = "demo") -> None:
        self.now = now or datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
        self.environment = environment

    def snapshot(self) -> Snapshot:
        now = self.now
        equity, peak, points = D("10000"), D("10000"), []
        for i in range(24 * 30):
            step = D((i * 37) % 23 - 11) * D("1.7")
            equity += step
            peak = max(peak, equity)
            points.append(
                EquityPoint(now - timedelta(hours=24 * 30 - i), equity, (peak - equity) / peak)
            )
        last = points[-1]
        overview = Overview(
            environment=self.environment,
            account_id="demo-sub-1" if self.environment == "demo" else "live-sub-1",
            pause_state="ENTRY_PAUSED_SYSTEM",
            pause_reasons=("unknown_order:BTCUSDT",),
            equity=last.equity,
            today_pnl=D("-42.15"),
            drawdown=last.drawdown,
            limits=(
                Limit("L1", "거래당 위험", D("0.40"), "0.40% / 1.00%"),
                Limit("L2", "일일 손실", D("0.21"), "-42.15 / -200.00 USDT"),
                Limit("L3", "총 노출", D("0.63"), "6,300 / 10,000 USDT"),
                Limit("L4", "계약별 노출", D("0.48"), "BTCUSDT 4,800 / 10,000"),
                Limit("L5", "동시 포지션", D("0.50"), "2 / 4"),
                Limit("L6", "낙폭", D(str(round(last.drawdown / D("0.15"), 4))), "기준 15%"),
                Limit("L7", "연속 손실", D("0.33"), "1 / 3"),
            ),
            last_closed_bar=now - timedelta(minutes=12),
            last_fill=now - timedelta(hours=3, minutes=5),
            key_expires_in_days=41,
            ai_mode="disabled",
            sample=True,
        )
        positions = [
            Position(
                "S1",
                "BTCUSDT",
                "Buy",
                D("0.050"),
                D("96120.5"),
                D("96880.0"),
                D("94200.0"),
                D("0.050"),
            ),
            Position(
                "S2",
                "ETHUSDT",
                "Sell",
                D("0.80"),
                D("3412.40"),
                D("3398.10"),
                D("3490.00"),
                D("0.50"),
            ),
        ]
        orders = [
            Order(
                "D-S1-E-7f3a",
                "S1",
                "BTCUSDT",
                "진입",
                "Buy",
                D("0.050"),
                D("96120.5"),
                "FILLED",
                D("0.050"),
                now - timedelta(hours=3, minutes=6),
            ),
            Order(
                "D-S2-P-19bc",
                "S2",
                "ETHUSDT",
                "보호",
                "Buy",
                D("0.50"),
                None,
                "OPEN",
                D("0"),
                now - timedelta(hours=1),
            ),
            Order(
                "D-S1-E-a0c2",
                "S1",
                "BTCUSDT",
                "진입",
                "Buy",
                D("0.020"),
                D("95500.0"),
                "UNKNOWN",
                D("0"),
                now - timedelta(minutes=18),
            ),
            Order(
                "D-S2-E-5e11",
                "S2",
                "ETHUSDT",
                "진입",
                "Sell",
                D("0.30"),
                D("3440.00"),
                "OPEN",
                D("0"),
                now - timedelta(minutes=40),
            ),
        ]
        fills = [
            Fill(
                "e-001",
                "D-S1-E-7f3a",
                "BTCUSDT",
                "Buy",
                D("96120.5"),
                D("0.050"),
                D("2.643814"),
                now - timedelta(hours=3, minutes=5),
            ),
            Fill(
                "e-002",
                "D-S2-E-4410",
                "ETHUSDT",
                "Sell",
                D("3412.40"),
                D("0.80"),
                D("1.501456"),
                now - timedelta(hours=7),
            ),
        ]
        logs = [
            LogEntry(
                now - timedelta(minutes=18),
                "alert",
                "system",
                "UNKNOWN 주문 D-S1-E-a0c2: 응답 없음, 조회 중",
            ),
            LogEntry(now - timedelta(hours=2), "command", "telegram", "/pause → 완료"),
            LogEntry(now - timedelta(hours=1, minutes=55), "command", "web", "/resume 재확인 요청"),
            LogEntry(now - timedelta(days=1), "pause", "system", "ENTRY_PAUSED_SYSTEM(sync) 해제"),
        ]
        return Snapshot(overview, positions, points, orders, fills, logs, [])
