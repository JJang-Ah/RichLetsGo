"""Four pure candidates: no sizing, storage, clock reads or exchange I/O."""

from decimal import Decimal, localcontext

from richletsgo.domain import Price, StrategyId
from richletsgo.marketdata.models import Interval
from richletsgo.portfolio.signals import MarketSignal
from richletsgo.risk.records import Direction

from .history import ClosedBars
from .indicators import CALC, atr, bollinger, channel, rsi, sma, wilder_window
from .models import (
    S1AParams,
    S1BParams,
    S1Result,
    S1State,
    S2AParams,
    S2BParams,
    S2Episode,
    S2PEpisode,
    S2PParams,
    S2PState,
    S2Result,
    Trend,
)


def _s1_input(history: ClosedBars, previous: S1State) -> None:
    if history.interval is not Interval.DAY or history.symbol is not previous.symbol:
        raise ValueError("S1 requires matching daily history/state")
    if history.bars and previous.bar_start is not None:
        if previous.bar_start >= history.bars[-1].start:
            raise ValueError("previous S1 state must precede the evaluated bar")


def _s1_result(history: ClosedBars, previous: S1State, trend: Trend, n: int) -> S1Result:
    stops = channel(history, n, include_current=False)
    start = history.bars[-1].start if history.bars else None
    if stops is None or trend is Trend.FLAT:
        return S1Result(S1State(history.symbol, Trend.FLAT, None, start), None)
    desired = stops.low if trend is Trend.LONG else stops.high
    if previous.trend is trend and previous.stop_price is not None:
        desired = (max if trend is Trend.LONG else min)(desired, previous.stop_price.value)
    bar = history.bars[-1]
    breached = desired >= bar.close if trend is Trend.LONG else desired <= bar.close
    if breached:
        state = S1State(history.symbol, trend, previous.stop_price, start)
        return S1Result(
            state,
            MarketSignal(
                StrategyId.S1,
                bar.symbol,
                bar.start,
                "EXIT",
                Direction(trend.value),
                Price(bar.close),
                previous.stop_price or Price(desired),
            ),
        )
    state = S1State(history.symbol, trend, Price(desired), start)
    signal = MarketSignal(
        StrategyId.S1,
        bar.symbol,
        bar.start,
        "ENTRY",
        Direction(trend.value),
        Price(bar.close),
        Price(desired),
    )
    return S1Result(state, signal)


def s1_a(history: ClosedBars, previous: S1State, params: S1AParams) -> S1Result:
    """Daily SMA direction; equality retains trend, warm-up is FLAT."""
    _s1_input(history, previous)
    fast, slow = sma(history, params.fast), sma(history, params.slow)
    trend = Trend.FLAT
    if fast is not None and slow is not None:
        trend = Trend.LONG if fast > slow else Trend.SHORT if fast < slow else previous.trend
    return _s1_result(history, previous, trend, params.exit_channel)


def s1_b(history: ClosedBars, previous: S1State, params: S1BParams) -> S1Result:
    """Strict close breakout of the prior N-bar channel; otherwise retain trend."""
    _s1_input(history, previous)
    bounds = channel(history, params.entry_channel, include_current=False)
    trend = Trend.FLAT
    if bounds is not None:
        close = history.bars[-1].close
        trend = (
            Trend.LONG
            if close > bounds.high
            else Trend.SHORT
            if close < bounds.low
            else previous.trend
        )
    return _s1_result(history, previous, trend, params.exit_channel)


def _s2_input(history: ClosedBars, s1: S1State, episode: S2Episode | None) -> None:
    if history.interval is not Interval.HOUR or history.symbol is not s1.symbol:
        raise ValueError("S2 requires matching hourly history/S1 state")
    if episode is not None and episode.symbol is not history.symbol:
        raise ValueError("episode symbol mismatch")
    if not history.bars:
        return
    bar = history.bars[-1]
    if s1.bar_start is not None and s1.bar_start + Interval.DAY.duration > bar.end:
        raise ValueError("S1 state is from a future daily close")
    if episode is not None and episode.entry_bar_start > bar.start:
        raise ValueError("episode starts after evaluated bar")


def _forced_exit(history: ClosedBars, s1: S1State, episode: S2Episode, maximum: int) -> bool:
    # Count fully closed holding bars including the entry bar. Exactly maximum
    # is allowed; exit only when the count exceeds it (not >=).
    held = (history.bars[-1].end - episode.entry_bar_start) // Interval.HOUR.duration
    return s1.trend.value != episode.direction.value or held > maximum


def _exit(history: ClosedBars, episode: S2Episode) -> S2Result:
    bar = history.bars[-1]
    return S2Result(
        MarketSignal(
            StrategyId.S2,
            bar.symbol,
            bar.start,
            "EXIT",
            episode.direction,
            Price(bar.close),
            episode.stop_price,
        )
    )


def _entry(history: ClosedBars, trend: Trend, n: int, multiple: Decimal) -> S2Result:
    volatility = atr(history, n)
    if volatility is None or volatility <= 0:
        return S2Result(None)
    bar = history.bars[-1]
    direction = Direction(trend.value)
    with localcontext(CALC):
        stop = bar.close - direction.sign * multiple * volatility
    # An impossible nonpositive stop cannot be expressed as a domain Price.
    if stop <= 0 or stop == bar.close:
        return S2Result(None)
    return S2Result(
        MarketSignal(
            StrategyId.S2, bar.symbol, bar.start, "ENTRY", direction, Price(bar.close), Price(stop)
        )
    )


def s2_a(
    history: ClosedBars, s1: S1State, episode: S2Episode | None, params: S2AParams
) -> S2Result:
    """Band re-entry uses each bar's own bands; exits do not depend on ATR warm-up."""
    _s2_input(history, s1, episode)
    if not history.bars:
        return S2Result(None)
    if episode is not None and _forced_exit(history, s1, episode, params.max_hold_bars):
        return _exit(history, episode)
    current = bollinger(history, params.period, params.k)
    if current is None:
        return S2Result(None)
    close = history.bars[-1].close
    if episode is not None:
        hit = close >= current.mid if episode.direction is Direction.LONG else close <= current.mid
        return _exit(history, episode) if hit else S2Result(None)
    if s1.trend is Trend.FLAT:
        return S2Result(None)
    previous = bollinger(history.before_last(lookback=params.period), params.period, params.k)
    if previous is None:
        return S2Result(None)
    old_close = history.bars[-2].close
    hit = (
        old_close < previous.lower and close >= current.lower
        if s1.trend is Trend.LONG
        else old_close > previous.upper and close <= current.upper
    )
    return (
        _entry(history, s1.trend, params.atr_period, params.atr_multiple) if hit else S2Result(None)
    )


def s2_b(
    history: ClosedBars, s1: S1State, episode: S2Episode | None, params: S2BParams
) -> S2Result:
    """Wilder RSI threshold return, with same-direction entry and fixed ATR stop."""
    _s2_input(history, s1, episode)
    if not history.bars:
        return S2Result(None)
    if episode is not None and _forced_exit(history, s1, episode, params.max_hold_bars):
        return _exit(history, episode)
    current = rsi(history, params.period)
    if current is None:
        return S2Result(None)
    if episode is not None:
        hit = (
            current >= params.neutral
            if episode.direction is Direction.LONG
            else current <= params.neutral
        )
        return _exit(history, episode) if hit else S2Result(None)
    if s1.trend is Trend.FLAT:
        return S2Result(None)
    previous = rsi(history.before_last(lookback=wilder_window(params.period)), params.period)
    if previous is None:
        return S2Result(None)
    hit = (
        previous < params.low and current >= params.low
        if s1.trend is Trend.LONG
        else previous > params.high and current <= params.high
    )
    return (
        _entry(history, s1.trend, params.atr_period, params.atr_multiple) if hit else S2Result(None)
    )


def required_bars(params: S1AParams | S1BParams | S2AParams | S2BParams | S2PParams) -> int:
    """Minimum steady-state input window, including the previous-bar indicator."""
    if isinstance(params, S1AParams):
        return max(params.slow, params.exit_channel + 1)
    if isinstance(params, S1BParams):
        return max(params.entry_channel, params.exit_channel) + 1
    if isinstance(params, S2PParams):
        return max(wilder_window(params.rsi_period) + 1, wilder_window(params.atr_period))
    if isinstance(params, S2AParams):
        return max(params.period + 1, wilder_window(params.atr_period))
    return max(wilder_window(params.period) + 1, wilder_window(params.atr_period))


def s2_p(
    history: ClosedBars, s1: S1State, episode: S2PEpisode | None, params: S2PParams
) -> S2Result:
    """D95: four-hour RSI return, fixed attached protection and VWAP price target."""
    if history.interval is not Interval.FOUR_HOUR or history.symbol is not s1.symbol:
        raise ValueError("S2-P requires matching four-hour history")
    if episode is not None and episode.symbol is not history.symbol:
        raise ValueError("episode symbol mismatch")
    if not history.bars:
        return S2Result(None)
    bar = history.bars[-1]
    if s1.bar_start is not None and s1.bar_start + Interval.DAY.duration > bar.end:
        raise ValueError("future S1 state")
    if episode is not None:
        if episode.entry_bar_start is not None and episode.entry_bar_start > bar.start:
            raise ValueError("future episode")
        hit = s1.trend.value != episode.direction.value
        if episode.state is S2PState.OPEN:
            assert episode.average_price is not None and episode.entry_bar_start is not None
            with localcontext(CALC):
                price = episode.average_price.value
                distance = abs(price - episode.stop_price.value)
                target = price + episode.direction.sign * params.target_r * distance
            hit |= (
                bar.close >= target if episode.direction is Direction.LONG else bar.close <= target
            )
            hit |= (
                bar.end - episode.entry_bar_start
            ) // Interval.FOUR_HOUR.duration > params.max_hold_bars
        return (
            S2Result(
                MarketSignal(
                    StrategyId.S2,
                    bar.symbol,
                    bar.start,
                    "EXIT",
                    episode.direction,
                    Price(bar.close),
                    episode.stop_price,
                )
            )
            if hit
            else S2Result(None)
        )
    if s1.trend is Trend.FLAT:
        return S2Result(None)
    current = rsi(history, params.rsi_period)
    previous = rsi(
        history.before_last(lookback=wilder_window(params.rsi_period)), params.rsi_period
    )
    if current is None or previous is None:
        return S2Result(None)
    upper = Decimal(100) - params.lower
    hit = (
        previous < params.lower <= current
        if s1.trend is Trend.LONG
        else previous > upper >= current
    )
    if not hit:
        return S2Result(None)
    result = _entry(history, s1.trend, params.atr_period, params.stop_atr)
    if result.signal is None:
        return result
    from dataclasses import replace

    return S2Result(
        replace(
            result.signal,
            limit_price=Price(bar.close),
            expires_at=bar.end + Interval.FOUR_HOUR.duration,
            post_only=True,
        )
    )
