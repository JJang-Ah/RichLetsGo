"""Demo fee-rate 10001 regression; synthetic transport and temporary configuration only."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_bybit_adapter import NOW, FakeTransport, response
from test_m304_trader import runtime as trader_runtime
from test_service_config import config_file

from richletsgo.config import ConfigError, load_config
from richletsgo.domain import Environment, ManualClock
from richletsgo.exchange.bybit.rest import RestClient
from richletsgo.exchange.contracts import ExchangeError
from richletsgo.keystore import ApiCredential, Secret
from richletsgo.live.inputs import BybitInputs
from richletsgo.storage.codec import decode, encode


def demo_config(tmp_path):
    return config_file(tmp_path, 'environment = "backtest"', 'environment = "demo"')


def gateway(config, *, code=10001, rates=None):
    def handler(method, url, body):
        assert method == "GET" and "/v5/account/fee-rate?" in url
        return response({"list": rates or []}, code=code)

    transport = FakeTransport(handler=handler)

    async def sleep(_):
        pass

    rest = RestClient(
        config.bybit,
        ApiCredential(Secret("dummy-key"), Secret("dummy-secret"), NOW),
        ManualClock(NOW),
        transport,
        sleep,
    )
    refreshed = []

    async def refresh():
        refreshed.append(True)
        return True

    return SimpleNamespace(rest=rest, refresh_instruments=refresh), transport, refreshed


def test_demo_10001_does_not_block_warmup_fee_maintenance(tmp_path):
    async def run():
        config = load_config(demo_config(tmp_path))
        gw, transport, refreshed = gateway(config)
        observed = await gw.rest.call(
            "GET", "/v5/account/fee-rate", {"category": "linear", "symbol": "BTCUSDT"}
        )
        assert isinstance(observed, ExchangeError) and "10001" in observed.message
        transport.calls.clear()
        inputs = BybitInputs(config, gw, None)
        await inputs.maintain()
        assert inputs.fee == D("0.00055") and config.bybit.demo_fee_rates.maker_fee_rate == D(
            "0.0002"
        )
        assert refreshed == [True] and not transport.calls
        await inputs.maintain()
        assert len(refreshed) == 2 and not transport.calls

    asyncio.run(run())


@pytest.mark.parametrize("field", ["maker_fee_rate", "taker_fee_rate"])
def test_demo_missing_rate_refuses_start_without_default(tmp_path, field):
    path = demo_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    # Remove only the operational demo field, not the separate research setting.
    value = '"0.0002"' if field == "maker_fee_rate" else '"0.00055"'
    path.write_text(text.replace(f"{field} = {value}", "", 1), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("maker_fee_rate", '"NaN"'),
        ("taker_fee_rate", '"Infinity"'),
        ("taker_fee_rate", '"-0.001"'),
        ("maker_fee_rate", '"1"'),
        ("taker_fee_rate", '"1"'),
        ("taker_fee_rate", "0.00055"),
    ],
)
def test_invalid_demo_rates_rejected_by_loader(tmp_path, field, value):
    path = demo_config(tmp_path)
    old = '"0.0002"' if field == "maker_fee_rate" else '"0.00055"'
    path.write_text(
        path.read_text(encoding="utf-8").replace(f"{field} = {old}", f"{field} = {value}", 1),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_programmatic_demo_without_rates_cannot_call_api_as_fallback(tmp_path):
    async def run():
        config = load_config(demo_config(tmp_path))
        config = replace(config, bybit=replace(config.bybit, demo_fee_rates=None))
        gw, transport, refreshed = gateway(config)
        with pytest.raises(ValueError, match="explicit demo"):
            await BybitInputs(config, gw, None).maintain()
        assert not refreshed and not transport.calls

    asyncio.run(run())


@pytest.mark.parametrize("code", [10001, 0])
def test_live_uses_only_api_even_when_demo_rates_are_present(tmp_path, code):
    async def run():
        config = load_config(demo_config(tmp_path))
        # Loader, RestClient and entrypoint still prohibit real live startup.
        # Change only the input policy label; transport remains synthetic/demo.
        config = replace(config, environment=Environment.LIVE)
        gw, transport, refreshed = gateway(config, code=code, rates=[{"takerFeeRate": "0.0007"}])
        inputs = BybitInputs(config, gw, None)
        if code:
            with pytest.raises(OSError, match="10001"):
                await inputs.maintain()
        else:
            await inputs.maintain()
            assert (
                inputs.fee == D("0.0007")
                and inputs.fee != config.bybit.demo_fee_rates.taker_fee_rate
            )
        assert transport.calls and all("/v5/account/fee-rate?" in c[1] for c in transport.calls)

    asyncio.run(run())


def test_demo_rates_source_and_config_version_are_recorded(tmp_path):
    async def run():
        path = demo_config(tmp_path)
        config = load_config(path)
        data = config.public_dict()
        rates = data["bybit"]["demo_fee_rates"]
        assert rates["maker_fee_rate"] == "0.0002" and rates["taker_fee_rate"] == "0.00055"
        assert "Trading-Fee-Structure" in rates["source"]
        db, clock, trader, inputs, sync, alerts = await trader_runtime(tmp_path)
        trader.config_record = encode({"hash": config.config_hash, "config": data})
        assert await trader.boot()
        async with db.unit_of_work() as u:
            saved = u.recovery.active("config_version:")
            assert (
                len(saved) == 1
                and decode(saved[0][1])["config"]["bybit"]["demo_fee_rates"] == rates
            )
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'maker_fee_rate = "0.0002"', 'maker_fee_rate = "0.0003"', 1
            ),
            encoding="utf-8",
        )
        assert load_config(path).config_hash != config.config_hash
        await trader.close()

    asyncio.run(run())


def test_source_can_fall_back_to_configuration_location_not_a_numeric_default(tmp_path):
    path = demo_config(tmp_path)
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("fee_rate_source =")
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    config = load_config(path)
    assert config.bybit.demo_fee_rates.source == "configuration:[bybit.demo]"
    assert config.bybit.demo_fee_rates.taker_fee_rate == D("0.00055")
