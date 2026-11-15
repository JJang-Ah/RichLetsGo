"""Bybit transport and strategy inputs for the transport-independent boundary runner."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from richletsgo.backtest.results import TradeLedger
from richletsgo.config import AppConfig
from richletsgo.domain import Price, StrategyId, Symbol
from richletsgo.exchange.bybit.gateway import BybitGateway
from richletsgo.exchange.contracts import ExchangeError
from richletsgo.marketdata.models import Bar, Interval, from_ms, to_ms
from richletsgo.portfolio.funding import snapshot_key
from richletsgo.portfolio.signals import MarketSignal, SignalRouter
from richletsgo.risk.checks import StrategyRiskInput
from richletsgo.risk.equity import AccountSnapshot, CashFlow
from richletsgo.risk.gate import Admission, MarketInputs
from richletsgo.risk.guards import DATA_PAUSE_REASON
from richletsgo.risk.pause import Pause, PauseKind
from richletsgo.risk.records import Direction
from richletsgo.risk.sizing import liquidation_buffer_ok
from richletsgo.storage.codec import decode, encode
from richletsgo.storage.contracts import UnitOfWork
from richletsgo.strategy import candidates
from richletsgo.strategy.boundary import boundary_signals
from richletsgo.strategy.history import ClosedBars, four_hour_bars
from richletsgo.strategy.models import (
    S1AParams,
    S1BParams,
    S1State,
    S2AParams,
    S2BParams,
    S2Episode,
    S2PEpisode,
    S2PParams,
    S2PState,
    Trend,
)

from .trader import FundingPayment


def checked(value: Any) -> Any:
    if isinstance(value, ExchangeError):
        raise OSError(value.message)
    return value


class BybitInputs:
    def __init__(self, config: AppConfig, gateway: BybitGateway, router: SignalRouter) -> None:
        self.config, self.gateway, self.router = config, gateway, router
        self.history: dict[tuple[Symbol, Interval], dict[datetime, Bar]] = {}
        self.marks: dict[Symbol, Price] = {}
        self.fee = Decimal(0)

    def remember(self, bars: tuple[Bar, ...]) -> None:
        for bar in bars:
            series = self.history.setdefault((bar.symbol, bar.interval), {})
            if bar.start in series and not series[bar.start].same_values(bar):
                raise ValueError("conflicting closed strategy history")
            series[bar.start] = bar

    async def fetch(
        self, symbol: Symbol, interval: Interval, first: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        result = []
        cursor = first
        while cursor < end:
            last = min(end, cursor + 1000 * interval.duration)
            body = checked(
                await self.gateway.rest.call(
                    "GET",
                    "/v5/market/kline",
                    {
                        "category": "linear",
                        "symbol": symbol.value,
                        "interval": interval.value,
                        "start": to_ms(cursor),
                        "end": to_ms(last) - 1,
                        "limit": 1000,
                    },
                    public=True,
                )
            )
            for row in body["result"]["list"]:
                bar = Bar(symbol, interval, from_ms(int(row[0])), *(Decimal(x) for x in row[1:7]))
                if not cursor <= bar.start < last or bar.end > end:
                    raise ValueError("warmup candle outside requested closed range")
                result.append(bar)
            cursor = last
        ordered = tuple(sorted(result, key=lambda b: b.start))
        self.remember(ordered)
        return ordered

    async def warmup(self) -> tuple[Bar, ...]:
        result: list[Bar] = []
        now = self.gateway.clock.now()
        for symbol in self.config.bybit.symbols:
            for interval in (Interval.DAY, Interval.HOUR):
                count = max(
                    candidates.required_bars(s.params)
                    * (4 if isinstance(s.params, S2PParams) and interval is Interval.HOUR else 1)
                    for s in self.config.strategies.values()
                )
                end = interval.floor(now)
                bars = await self.fetch(symbol, interval, end - count * interval.duration, end)
                ClosedBars(symbol, interval, bars, end).through(end, lookback=count)
                if len(bars) != count or bars[-1].end != end:
                    raise ValueError("incomplete warmup history")
                result.extend(bars)
        await self.maintain()
        for symbol in self.config.bybit.symbols:
            self.gateway.pauses.clear(
                PauseKind.ENTRY_PAUSED_SYSTEM, reason=DATA_PAUSE_REASON, symbol=symbol
            )
        return tuple(result)

    async def bar(self, symbol: Symbol, interval: Interval, at: datetime) -> Bar | None:
        bars = await self.fetch(symbol, interval, at - interval.duration, at)
        return bars[0] if len(bars) == 1 else None

    async def funding(self, since: datetime) -> tuple[FundingPayment, ...]:
        bounds = checked(self.gateway._range(since))
        rows = checked(
            await self.gateway.rest.pages(
                "/v5/account/transaction-log",
                {
                    "accountType": "UNIFIED",
                    "category": "linear",
                    "currency": "USDT",
                    "type": "SETTLEMENT",
                    **bounds,
                },
            )
        )
        result = []
        for row in rows:
            if row["currency"] != "USDT" or row["type"] != "SETTLEMENT":
                raise ValueError("unexpected funding record")
            symbol = Symbol(row["symbol"])
            if symbol not in self.config.bybit.symbols:
                raise ValueError("funding for unconfigured symbol")
            result.append(
                FundingPayment(
                    row["id"],
                    symbol,
                    from_ms(int(row["transactionTime"])),
                    -Decimal(row["funding"]),
                )
            )
        return tuple(result)

    async def account(self) -> AccountSnapshot:
        value: AccountSnapshot = checked(await self.gateway.get_account())
        return value

    async def cash_flows(self, since: datetime) -> tuple[CashFlow, ...]:
        value: tuple[CashFlow, ...] = checked(await self.gateway.get_cash_flows(since))
        return value

    async def maintain(self) -> None:
        if not await self.gateway.refresh_instruments():
            raise OSError("instrument refresh failed")
        rates: list[Decimal] = []
        for symbol in self.config.bybit.symbols:
            body = checked(
                await self.gateway.rest.call(
                    "GET", "/v5/account/fee-rate", {"category": "linear", "symbol": symbol.value}
                )
            )
            rates.extend(Decimal(row["takerFeeRate"]) for row in body["result"]["list"])
        self.fee = max(rates)
        if self.fee < 0:
            raise ValueError("invalid taker fee")

    async def check_liquidation(self) -> None:
        positions = checked(await self.gateway.get_positions())
        missing = False
        async with self.router.gate.account_lock:
            async with self.router.gate._uow() as u:
                for position in positions:
                    if not position.qty.value:
                        continue
                    if position.liquidation_price is None:
                        missing = True
                        continue
                    for book in u.portfolio.books():
                        if (
                            book.symbol is not position.symbol
                            or not book.qty
                            or book.stop_price is None
                            or book.avg_price is None
                        ):
                            continue
                        direction = Direction.LONG if book.qty > 0 else Direction.SHORT
                        if not liquidation_buffer_ok(
                            book.avg_price,
                            book.stop_price.value,
                            position.liquidation_price.value,
                            self.config.risk.liquidation_buffer_ratio,
                            direction,
                        ):
                            episode = u.episodes.active_for(book.strategy, book.symbol)
                            if episode:
                                self.router.portfolio.request_close(u, episode)
                            self.gateway.pause("actual_liquidation_buffer")
                await u.commit()
            if missing:
                self.gateway.pause("liquidation_price_unavailable")
            else:
                self.gateway.pauses.clear(
                    PauseKind.ENTRY_PAUSED_SYSTEM, reason="liquidation_price_unavailable"
                )

    async def market(self, symbol: Symbol) -> MarketInputs:
        timestamps = []
        for item in self.config.bybit.symbols:
            body = checked(
                await self.gateway.rest.call(
                    "GET",
                    "/v5/market/tickers",
                    {"category": "linear", "symbol": item.value},
                    public=True,
                )
            )
            if (
                abs(to_ms(self.gateway.clock.now()) - int(body["time"]))
                > self.config.bybit.timeout_seconds * 1000
            ):
                raise OSError("stale mark snapshot")
            self.marks[item] = Price(Decimal(body["result"]["list"][0]["markPrice"]))
            timestamps.append(from_ms(int(body["time"])))
        observed = min(timestamps)
        valid_until = observed + timedelta(seconds=self.config.bybit.timeout_seconds)
        if self.gateway.clock.now() > valid_until:
            raise OSError("stale combined mark snapshot")
        instrument = checked(await self.gateway.get_instrument(symbol))
        tier = self.gateway.tier_one[symbol]
        return MarketInputs(
            self.marks,
            instrument,
            self.config.bybit.leverage,
            tier.mmr,
            self.fee,
            self.config.risk.slippage_buffer,
            tier.limit_notional,
            observed,
            valid_until,
        )

    async def route(
        self,
        signal: MarketSignal,
        market: MarketInputs,
        figures: dict[StrategyId, StrategyRiskInput],
    ) -> Admission:
        return await self.router.handle(signal, market, figures)

    async def figures(
        self, uow: UnitOfWork, baseline: dict[str, str]
    ) -> dict[StrategyId, StrategyRiskInput]:
        ledger = TradeLedger()
        funded: frozenset[str] = frozenset()
        raw = uow.recovery.get("live_trade_ledger")
        if raw:
            trades, seen, reasons, funded = decode(raw)
            ledger.trades, ledger.seen, ledger.exit_reasons = list(trades), set(seen), reasons
            ledger.active = {
                (t.strategy, t.symbol): t for t in ledger.trades if t.closed_at is None
            }
        ledger.ingest(uow)
        paid = set(funded)
        for _, content in uow.recovery.active("funding_record:"):
            record = json.loads(content)
            if record["id"] in paid:
                continue
            at = datetime.fromisoformat(record["settled_at"])
            raw_positions = uow.recovery.get(snapshot_key(at, Symbol(record["symbol"])))
            if raw_positions:
                positions = {
                    StrategyId(s): abs(Decimal(q)) for s, q in json.loads(raw_positions).items()
                }
                total = sum(positions.values(), Decimal(0))
                for strategy, qty in positions.items():
                    if not qty:
                        continue
                    trades = [
                        t
                        for t in ledger.trades
                        if t.strategy is strategy
                        and t.symbol.value == record["symbol"]
                        and t.opened_at < at
                        and (t.closed_at is None or at <= t.closed_at)
                    ]
                    if len(trades) != 1:
                        raise ValueError("funding trade attribution unavailable")
                    trades[0].funding += Decimal(record["amount"]) * qty / total
            paid.add(record["id"])
        uow.recovery.put(
            "live_trade_ledger",
            encode(
                (tuple(ledger.trades), frozenset(ledger.seen), ledger.exit_reasons, frozenset(paid))
            ),
        )
        result = {}
        for strategy in StrategyId:
            pnl = sum(
                (
                    b.net_realized
                    + b.qty
                    * (
                        (self.marks.get(b.symbol, Price(b.avg_price or Decimal(1)))).value
                        - (b.avg_price or Decimal(0))
                    )
                    for b in uow.portfolio.books()
                    if b.strategy is strategy
                ),
                Decimal(0),
            ) - Decimal(baseline.get(strategy.value, "0"))
            streak, latest = 0, None
            for trade in sorted(
                (t for t in ledger.trades if t.strategy is strategy and t.closed_at),
                key=lambda t: (t.closed_at, t.episode_id),
                reverse=True,
            ):
                if trade.net_pnl >= 0:
                    break
                streak += 1
                latest = latest or trade.closed_at
            result[strategy] = StrategyRiskInput(pnl, streak, latest)
        return result

    def closed(self, symbol: Symbol, interval: Interval, at: datetime, count: int) -> ClosedBars:
        values = self.history.get((symbol, interval), {})
        bars = tuple(sorted((b for b in values.values() if b.end <= at), key=lambda b: b.start))[
            -count:
        ]
        return ClosedBars(symbol, interval, bars, at)

    async def signals(
        self, uow: UnitOfWork, at: datetime, bars: tuple[Bar, ...], available: frozenset[Symbol]
    ) -> tuple[MarketSignal, ...]:
        self.remember(bars)
        result = []
        s1 = self.config.strategies[StrategyId.S1].params
        assert isinstance(s1, (S1AParams, S1BParams))
        s2_config = self.config.strategies.get(StrategyId.S2)
        s2 = s2_config.params if s2_config else None
        assert s2 is None or isinstance(s2, (S2AParams, S2BParams, S2PParams))
        states = {}
        for symbol in self.config.bybit.symbols:
            raw = uow.recovery.get("strategy_state:" + symbol.value)
            states[symbol] = decode(raw) if raw else S1State(symbol, Trend.FLAT, None, None)
        valid = set(available)
        for symbol in available:
            try:
                day = self.closed(symbol, Interval.DAY, at, candidates.required_bars(s1))
                if len(day.bars) < candidates.required_bars(s1):
                    raise ValueError("short daily history")
                if s2 is not None:
                    n = candidates.required_bars(s2) * (4 if isinstance(s2, S2PParams) else 1)
                    hour = self.closed(symbol, Interval.HOUR, at, n)
                    if len(hour.bars) < n:
                        raise ValueError("short hourly history")
            except ValueError:
                valid.discard(symbol)
                self.gateway.pauses.add(
                    Pause(
                        PauseKind.ENTRY_PAUSED_SYSTEM,
                        DATA_PAUSE_REASON,
                        self.gateway.clock.now(),
                        symbol=symbol,
                    )
                )
                self.gateway.alert("missing strategy history: " + symbol.value)
        available = frozenset(valid)

        async def first(symbol: Symbol) -> None:
            if symbol not in available:
                return
            history = self.closed(symbol, Interval.DAY, at, candidates.required_bars(s1))
            out = (
                candidates.s1_a(history, states[symbol], s1)
                if isinstance(s1, S1AParams)
                else candidates.s1_b(history, states[symbol], s1)
            )
            states[symbol] = out.state
            uow.recovery.put("strategy_state:" + symbol.value, encode(out.state))
            if out.signal:
                result.append(out.signal)
                if (
                    out.signal.kind != "EXIT"
                    and uow.portfolio.book(StrategyId.S1, symbol).qty * out.signal.direction.sign
                    > 0
                ):
                    spec = self.gateway._instruments[symbol]
                    self.router.portfolio.trail_stop(
                        uow, StrategyId.S1, symbol, spec, out.signal.stop_price
                    )

        async def second(symbol: Symbol) -> None:
            if symbol not in available or s2 is None:
                return
            active = uow.episodes.active_for(StrategyId.S2, symbol)
            book = uow.portfolio.book(StrategyId.S2, symbol)
            pending = None
            episode = None
            if active:
                order = uow.orders.get(active.entry_order_keys[0])
                if order is None or order.attached_stop is None:
                    raise ValueError("missing S2 entry protection")
                times = [
                    f.event.exec_time
                    for f in uow.fills.all()
                    if f.order_key in active.entry_order_keys
                ]
                opened = min(times) if times else active.opened_at
                if isinstance(s2, S2PParams):
                    pending = S2PEpisode(
                        symbol,
                        active.direction,
                        S2PState.OPEN if book.qty else S2PState.PENDING,
                        Price(book.avg_price) if book.avg_price is not None else None,
                        order.attached_stop.stop_loss,
                        Interval.FOUR_HOUR.floor(opened) if book.qty else None,
                    )
                else:
                    episode = S2Episode(
                        symbol,
                        active.direction,
                        Interval.HOUR.floor(opened),
                        order.attached_stop.stop_loss,
                    )
            n = candidates.required_bars(s2)
            if isinstance(s2, S2PParams):
                hourly = self.closed(symbol, Interval.HOUR, at, n * 4)
                out = candidates.s2_p(
                    ClosedBars(
                        symbol, Interval.FOUR_HOUR, four_hour_bars(hourly.bars, as_of=at), at
                    ),
                    states[symbol],
                    pending,
                    s2,
                )
            else:
                history = self.closed(symbol, Interval.HOUR, at, n)
                out = (
                    candidates.s2_a(history, states[symbol], episode, s2)
                    if isinstance(s2, S2AParams)
                    else candidates.s2_b(history, states[symbol], episode, s2)
                )
            if out.signal:
                result.append(out.signal)

        async def settle() -> None:
            self.router.portfolio.reconcile(uow)

        await boundary_signals(at, self.config.bybit.symbols, s2, first, second, settle)
        return tuple(result)
