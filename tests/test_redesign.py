"""D95/D96 synthetic-only acceptance checks; never opens historical market datasets."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_backtest import populate, run_config
from test_risk import entry_order
from test_simulated_exchange import F, Fault, H, T, entry, fills, orders, position, setup, step
from test_strategy import BTC, START, history, state
from test_walkforward import NOW, fixture_config

from richletsgo.backtest.walkforward import run_campaign
from richletsgo.backtest.wf_config import load_walk_config
from richletsgo.domain import OrderState, Price
from richletsgo.exchange.contracts import OrderRequest
from richletsgo.marketdata.models import Interval
from richletsgo.marketdata.store import MarketDataStore
from richletsgo.oms.transmission import decode_order, encode_order
from richletsgo.risk.records import Direction
from richletsgo.strategy.candidates import required_bars, s2_p
from richletsgo.strategy.history import four_hour_bars
from richletsgo.strategy.models import S2PEpisode, S2PParams, S2PState, S2Result, Trend

P = S2PParams(2, D(20), 1, D("1.5"), D(2), 30)


def test_aggregate_ohlcv_gaps_and_future():
    h = history([100, 101, 98, 102, 103, 104, 105, 106])
    bars = four_hour_bars(h.bars, as_of=h.as_of)
    assert (
        bars[0].open,
        bars[0].high,
        bars[0].low,
        bars[0].close,
        bars[0].volume,
        bars[0].turnover,
    ) == (100, 103, 97, 102, 4, 401)
    assert four_hour_bars(h.bars[:4], as_of=h.bars[3].end) == bars[:1]
    assert four_hour_bars(h.bars[:2] + h.bars[3:], as_of=h.as_of) == bars[1:]
    assert four_hour_bars(h.bars[:3], as_of=h.bars[2].end) == ()
    with pytest.raises(ValueError):
        four_hour_bars(h.bars, as_of=h.bars[-1].start)


@pytest.mark.parametrize(
    "direction,stop,close", [(Direction.LONG, 90, 120), (Direction.SHORT, 110, 80)]
)
def test_vwap_target_and_json(direction, stop, close):
    h = history([close], Interval.FOUR_HOUR)
    ep = S2PEpisode(BTC, direction, S2PState.OPEN, Price(D(100)), Price(D(stop)), START)
    assert S2PEpisode.from_dict(json.loads(json.dumps(ep.to_dict()))) == ep
    result = s2_p(h, state(Trend(direction.value)), ep, P)
    assert result.signal.kind == "EXIT"
    assert S2Result.from_dict(json.loads(json.dumps(result.to_dict()))) == result
    # Shift VWAP away from the target, leaving the attached stop unchanged.
    moved = replace(ep, average_price=Price(D(101 if direction is Direction.LONG else 99)))
    assert s2_p(h, state(Trend(direction.value)), moved, P).signal is None


@pytest.mark.parametrize("trend", [Trend.FLAT, Trend.SHORT])
def test_pending_exit_on_trend_change(trend):
    ep = S2PEpisode(BTC, Direction.LONG, S2PState.PENDING, None, Price(D(90)), None)
    assert S2PEpisode.from_dict(ep.to_dict()) == ep
    assert s2_p(history([100], Interval.FOUR_HOUR), state(trend), ep, P).signal.kind == "EXIT"
    assert s2_p(history([100], Interval.FOUR_HOUR), state(Trend.LONG), ep, P).signal is None


def test_holding_bars_and_future_episode():
    ep = S2PEpisode(BTC, Direction.LONG, S2PState.OPEN, Price(D(100)), Price(D(90)), START)
    assert s2_p(history([100] * 30, Interval.FOUR_HOUR), state(Trend.LONG), ep, P).signal is None
    assert (
        s2_p(history([100] * 31, Interval.FOUR_HOUR), state(Trend.LONG), ep, P).signal.kind
        == "EXIT"
    )
    with pytest.raises(ValueError):
        s2_p(
            history([100], Interval.FOUR_HOUR),
            state(Trend.LONG),
            replace(ep, entry_bar_start=START + 4 * H),
            P,
        )


@pytest.mark.parametrize("short", [False, True])
def test_entry_crossing_expiration_and_prefix(short):
    values = list(range(120, 99, -1)) + [105]
    if short:
        values = [220 - v for v in values]
    h = history(values, Interval.FOUR_HOUR)
    trend = Trend.SHORT if short else Trend.LONG
    result = s2_p(h, state(trend), None, P)
    assert result.signal is not None and result.signal.kind == "ENTRY"
    assert result.signal.limit_price == result.signal.entry_price
    assert result.signal.post_only and result.signal.expires_at == h.bars[-1].end + 4 * H
    assert S2Result.from_dict(result.to_dict()) == result
    future = history(values + [170, 180], Interval.FOUR_HOUR)
    assert s2_p(future.through(h.bars[-1].start), state(trend), None, P) == result


@pytest.mark.parametrize(
    "key,value",
    [
        ("lower", D(0)),
        ("lower", D(50)),
        ("stop_atr", D(0)),
        ("target_r", D(-1)),
        ("rsi_period", 0),
        ("max_hold_bars", True),
    ],
)
def test_parameter_validation(key, value):
    with pytest.raises((ValueError, TypeError)):
        replace(P, **{key: value})


def test_persisted_order_policy_and_legacy():
    order = replace(entry_order(), limit_price=Price(D(100)), expires_at=T + 4 * H, post_only=True)
    restored = decode_order(json.loads(json.dumps(encode_order(order))))
    assert restored.expires_at == order.expires_at and restored.post_only
    request = OrderRequest.from_record(restored)
    assert request.expires_at == order.expires_at and request.post_only
    legacy = encode_order(order)
    legacy.pop("expires_at")
    legacy.pop("post_only")
    assert not decode_order(legacy).post_only
    assert decode_order(legacy).expires_at is None


@pytest.mark.parametrize("partial", [False, True])
def test_four_hour_expiry_fills_last_bar_first(partial):
    ex, clock = setup(*([Fault("fill", 1, F.PARTIAL_FILL, "entry", D(".5"))] if partial else []))
    asyncio.run(ex.place_order(replace(entry(limit="100", expires=T + 4 * H), post_only=True)))
    for i in range(3):
        step(ex, clock, i, op="101", low="100", high="102", close="101")
        assert next(o for o in orders(ex) if o.order_link_id == "entry").state == OrderState.OPEN
    step(ex, clock, 3, op="101", low="99", high="102", close="101")
    assert position(ex) == D(".5" if partial else "1")
    assert fills(ex)[0].price.value == 100
    assert next(o for o in orders(ex) if o.order_link_id == "entry").state.is_terminal
    assert any(o.parent_order_link_id == "entry" for o in orders(ex))


def test_d96_configuration_calendar_only():
    cfg = load_walk_config(Path(__file__).resolve().parents[1] / "config.holdout.example.toml")
    assert len(cfg.windows) == 5 and len(cfg.grids["S1B-S2P"]) == 16
    assert cfg.warmup_start.isoformat().startswith("2021-04-06")
    assert cfg.minimum_trades == {"S1": 6, "S2": 20}
    assert cfg.research.selection["require_positive_score"]
    assert required_bars(cfg.grids["S1B-S2P"][0][1]) == 140


def test_synthetic_runner_and_four_hour_boundaries(tmp_path):
    cfg = run_config(48, s2=replace(P, rsi_period=1))
    with MarketDataStore(tmp_path / "source") as store:
        populate(store, 48)
        result = asyncio.run(
            cfg.execute(store, results_dir=tmp_path / "results", journal_dir=tmp_path / "journal")
        )
    assert result.status == "COMPLETED"


def test_synthetic_campaign_resume_and_positive_filter(tmp_path):
    path, cfg = fixture_config(tmp_path)
    text = (
        path.read_text().split("[backtest.grid]")[0]
        + """[backtest.grid]
S1B = [{ entry_channel = 1, exit_channel = 1 }]
S2P = [{ rsi_period = 1, lower = "20", atr_period = 1, stop_atr = "1.5", target_r = "2", max_hold_bars = 30 }]
"""
    )
    text = text.replace(
        "[backtest.selection]", "[backtest.selection]\nrequire_positive_score = true"
    )
    path.write_text(text)
    root = run_campaign(path, "synthetic", NOW)
    first = (root / "report-data.json").read_bytes()
    path.write_text(path.read_text().replace("workers = 1", "workers = 2"))
    assert run_campaign(path, "synthetic", NOW) == root
    assert (root / "report-data.json").read_bytes() == first
    body = json.loads(first)
    for w in body["windows"]["S1B-S2P"]:
        if w["selected"] is not None:
            assert D(w["selection_score"]) > 0


@pytest.mark.parametrize("fill_fraction", [D(0), D(".5"), D(1)])
def test_signal_gate_send_expire_book_and_no_duplicate(tmp_path, fill_fraction):
    from test_portfolio import RISK_INPUT, signal
    from test_risk import market
    from v0.test_recovery_integration import bar as advance
    from v0.test_recovery_integration import setup as integrated

    from richletsgo.risk.records import EpisodeState

    async def scenario():
        faults = [Fault("fill", 1, F.PARTIAL_FILL, value=fill_fraction)] if fill_fraction else []
        db, clock, svc, router, ex, sync = integrated(tmp_path, faults)
        assert await sync.synchronize()
        start = clock.now()
        sig = replace(signal(), limit_price=Price(D(100)), expires_at=start + 4 * H, post_only=True)
        admission = await router.handle(sig, market(), RISK_INPUT)
        assert admission.order.expires_at == sig.expires_at and admission.order.post_only
        await sync.submit(admission.order)
        for i in range(4):
            advance(clock, ex, low="99" if i == 3 and fill_fraction else "100")
            await sync.tick()
            if i == 0:
                async with db.unit_of_work() as u:
                    saved = u.orders.get(admission.order.key)
                    assert (
                        saved.state == OrderState.OPEN
                        and saved.expires_at == sig.expires_at
                        and saved.post_only
                    )
        async with db.unit_of_work() as u:
            saved = u.orders.get(admission.order.key)
            book = u.portfolio.book(sig.strategy, BTC)
            assert saved.state.is_terminal
            assert book.qty == admission.order.qty.value * fill_fraction
            episode = u.episodes.get(admission.episode.episode_id)
            assert (episode.state is EpisodeState.CLOSED) == (fill_fraction == 0)
            if fill_fraction:
                assert sum(s.qty for s in book.stops) == book.qty
        repeated = await router.handle(sig, market(), RISK_INPUT)
        assert repeated.reused
        assert (
            len(
                [
                    o
                    for o in await ex.get_order_history(BTC, start)
                    if o.order_link_id == admission.order.order_link_id
                ]
            )
            == 1
        )
        await sync.close()

    asyncio.run(scenario())


def test_holdout_draft_refused_before_storage_access(tmp_path, monkeypatch):
    from richletsgo.backtest import walkforward

    def forbidden(*args, **kwargs):
        pytest.fail("must not access data before preregistration")

    monkeypatch.setattr(walkforward, "prepare", forbidden)
    source = Path(__file__).resolve().parents[1] / "config.holdout.example.toml"
    with pytest.raises(ValueError, match="preregistration"):
        run_campaign(source, "synthetic", NOW, prepare_only=True)
