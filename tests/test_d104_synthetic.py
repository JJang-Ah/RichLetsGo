"""Synthetic D104 fixtures only: no exchange, key, database, or campaign reads."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from richletsgo.research.d104 import execution as x
from richletsgo.research.d104 import inputs as i
from richletsgo.research.d104 import verdict as v


def ms(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    "opening,minutes",
    [
        ("2026-03-06T14:30", 390),
        ("2026-03-09T13:30", 390),
        ("2026-07-02T13:30", 210),
        ("2026-06-22T00:00", 390),
    ],
)
def test_confirmed_regular_rows_use_utc_bounds_and_actual_close(opening, minutes):
    at = ms(opening)
    kr = opening.endswith("00:00")
    s = i.Session(
        "SKHYNIXUSDT" if kr else "MUUSDT", "XKRX" if kr else "XNYS", at, at + minutes * 60_000
    )
    bars = [
        i.Candle(s.symbol, t, D(100), D(101), D(99), D(100)) for t in range(s.start, s.end, i.BAR)
    ]
    for index, allowed in [(6, True), (-4, True), (-3, False)]:
        rows = bars.copy()
        b = rows[index]
        rows[index] = i.Candle(s.symbol, b.at, D(100), D(103), D(99), D(102))
        signal = x.breakout(s, reversed(rows))
        assert bool(signal) == allowed
        if signal:
            assert (signal.at, signal.stop, signal.side) == (b.at + i.BAR, D(99), 1)
    with pytest.raises(ValueError, match="missing"):
        x.breakout(s, bars[1:])
    assert i.digest(bars) == i.digest([*reversed(bars), bars[0]])
    edited = i.Candle(s.symbol, at, D(100), D(102), D(99), D(100))
    assert i.digest(bars) != i.digest([edited, *bars[1:]])
    with pytest.raises(ValueError):
        i.digest([*bars, edited])


def test_refuse_unregistered_access_and_outside_universe():
    for symbol, cal in [
        ("CLUSDT", "CMES"),
        ("XAUUSDT", "XNYS"),
        ("SKHYUSDT", "XNYS"),
        ("MUUSDT", "CMES"),
    ]:
        with pytest.raises(ValueError):
            i.Session(symbol, cal, 0, i.DAY)
    with pytest.raises(ValueError):
        i.Session("MUUSDT", "XNYS", i.B, i.B + i.DAY)
    with pytest.raises(PermissionError):
        i.fetch("public", symbol="MUUSDT", end=i.B - 1)
    assert i.B == ms("2026-09-01")


def test_midnight_window_and_thresholds():
    days = {k * i.DAY: D(20_000_000) for k in range(31)}
    days[30 * i.DAY] = D(50_000_000)
    assert i.turnover(30 * i.DAY - 1, 0, days) is None
    assert i.turnover(30 * i.DAY, 0, days) == 20_000_000
    assert i.turnover(31 * i.DAY, 0, days) == 21_000_000
    del days[i.DAY]
    with pytest.raises(ValueError):
        i.turnover(30 * i.DAY, 0, days)
    assert [i.slip(D(n)) for n in (19_999_999, 20_000_000, 50_000_000, 200_000_000)] == [
        None,
        D(".0015"),
        D(".001"),
        D(".0005"),
    ]


@pytest.mark.parametrize("side", [1, -1])
def test_reservation_funding_gap_and_intrabar_stop(side):
    terms = x.Terms(D(".001"), D(".01"), D(".01"), D(".01"), D(1))
    signals = [
        (x.Signal(symbol, 7 * i.BAR, side, D(100), D(100 - 5 * side)), terms)
        for symbol in reversed(list(i.CALENDAR))
    ]
    orders = x.reserve(signals, D(10000), {})
    assert [r.signal.symbol for r in orders] == sorted(i.CALENDAR)[:3]
    r = orders[0]
    sym = r.signal.symbol
    sess = i.Session(sym, i.CALENDAR[sym], 0, 12 * i.BAR)

    def bar(k, o, h, low):
        return i.Candle(sym, k * i.BAR, D(o), D(h), D(low), D(o))

    def entry(b):
        return x.step(b, sess, None, r, (D(".01"), D(100)))

    entered = entry(bar(7, 100, 101, 99))
    pos = entered.position
    assert pos and entered.cash_change == -pos.entry * r.qty * x.FEE
    assert not x.step(bar(7, 100, 101, 99), sess, None, r, free_notional=D(1)).position
    assert not entry(bar(7, 100 - 6 * side, 110, 90)).position
    stopped = entry(bar(7, 100, 106, 94))
    assert stopped.position is None and stopped.exit_price == terms.price(r.signal.stop, -side)
    for k, o in [(8, 100 - 6 * side), (11, 100)]:
        result = x.step(bar(k, o, 110, 90), sess, pos, funding=(D(".01"), D(100)))
        exit = terms.price(D(o), -side)
        assert result.exit_price == exit and result.position is None
        assert result.cash_change == r.qty * ((exit - pos.entry) * side - exit * x.FEE - side)


def test_verdict_precedence_and_day_grouping():
    first = date(2026, 6, 1)
    until = first + timedelta(days=60)
    trades = [
        v.Trade(first + timedelta(days=k // 2), k, list(i.CALENDAR)[k % 5], D(2), D(10))
        for k in range(100)
    ]

    def design(ts=trades, e=150, valid=True, ruined=False):
        return v.design(ts, e, first, until, valid=valid, ruined=ruined)

    assert [v.minimum(k) for k in (150, 250, 251, 256)] == [100, 100, 101, 103]
    with pytest.raises(ValueError, match="INVALID_INPUT"):
        design([v.Trade(until, 0, "MUUSDT", D(1), D(1))], e=0)
    assert design() == "GO" and design(e=251) == "NO_GO"
    assert design(e=149) == "INDETERMINATE" and design(e=149, valid=False) == "INVALID_INPUT"
    assert design([v.Trade(first, k, "MUUSDT", D(1), D(1)) for k in range(100)]) == "NO_GO"

    def hold(ts=trades, dd=D(".20"), valid=True):
        return v.holdout(ts, D(10500), dd, valid=valid, ruined=False)

    assert hold() == "PASS" and hold([]) == "INDETERMINATE"
    assert hold([], D(".21")) == "FAIL" and hold([], D(".21"), False) == "INVALID_INPUT"


def test_shared_cash_replay_hash_and_stressed_drawdown():
    from dataclasses import replace

    from richletsgo.research.d104.replay import Fixture, FundingPeriod, replay

    fixtures = []
    for sym in list(i.CALENDAR)[:4]:
        at = ms("2026-06-01T13:30")
        s = i.Session(sym, "XNYS", at, at + 12 * i.BAR)
        bars = tuple(
            i.Candle(
                sym,
                at + k * i.BAR,
                D(100 if k <= 6 else 102),
                D(101 if k < 6 else 103),
                D(99 if k < 7 else 101),
                D(100 if k < 6 else 102),
            )
            for k in range(12)
        )
        days = {at // i.DAY * i.DAY - j * i.DAY: D(20_000_000) for j in range(1, 31)}
        fixtures.append(
            Fixture(
                s,
                bars,
                bars,
                0,
                days,
                x.Terms(D(".0015"), D(".01"), D(".01"), D(0), D(0)),
                {at + 8 * i.BAR: (D(".001"), D(102))},
                (FundingPeriod(at, s.end, i.DAY, at + 8 * i.BAR),),
            )
        )
    result = replay(fixtures)
    assert [sym for _, sym in result.entries] == sorted(list(i.CALENDAR)[:4])[:3]
    assert sum(t.pnl for t in result.trades) == result.equity - 10000 and len(result.trades) == 3
    assert result.equity < 10000 and result.mdd > 0
    assert result == replay(
        replace(f, trade=f.trade[::-1], mark=f.mark[::-1]) for f in reversed(fixtures)
    )
    assert result.minimum_equity <= result.equity
    with pytest.raises(ValueError, match="settlement"):
        replay([replace(fixtures[0], settlements={})])
    bad = replace(fixtures[0], settlements={at + 8 * i.BAR: None})
    with pytest.raises(ValueError, match="settlement"):
        replay([bad])
    with pytest.raises(ValueError, match="missing candle"):
        replay([replace(bad, mark=bad.mark[:-1])])


def test_capacity_is_price_blind_and_deduplicates_calendar_rows():
    day = 30 * i.DAY
    s = i.Session("MUUSDT", "XNYS", day, day + 12 * i.BAR)
    previous = i.Session("MUUSDT", "XNYS", s.start - i.DAY, s.end - i.DAY)
    days = {j * i.DAY: D(20_000_000) for j in range(30)}
    result = i.capacity([s, previous, s], {"MUUSDT": 0}, {"MUUSDT": days})
    assert sum(result.values()) == result["MUUSDT"] == 1
    del days[0]
    with pytest.raises(ValueError, match="daily turnover"):
        i.capacity([s], {"MUUSDT": 0}, {"MUUSDT": days})
