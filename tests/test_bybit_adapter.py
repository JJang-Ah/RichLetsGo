"""Offline V5 examples, not recordings from a demo account. No credentials/network."""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from richletsgo.config import load_config
from richletsgo.domain import OrderPurpose, OrderState, Price, Qty, StopKind, Symbol
from richletsgo.domain.clock import ManualClock
from richletsgo.exchange.bybit import mapping
from richletsgo.exchange.bybit.gateway import BybitGateway
from richletsgo.exchange.bybit.rest import AiohttpTransport, RestClient, signature
from richletsgo.exchange.bybit.streams import StreamRunner
from richletsgo.exchange.contracts import Accepted, ErrorCode, OrderRequest
from richletsgo.keystore import ApiCredential, Secret
from richletsgo.marketdata.models import to_ms
from richletsgo.portfolio.models import StopParameters
from richletsgo.risk.pause import PauseKind, PauseState

NOW = datetime(2026, 10, 26, tzinfo=UTC)
BTC = Symbol.BTCUSDT


def order_row(**changes):
    return (
        dict(
            orderId="exchange-1",
            orderLinkId="entry-1",
            parentOrderLinkId="",
            symbol="BTCUSDT",
            side="Buy",
            qty="0.020",
            price="0",
            cumExecQty="0.005",
            updatedTime=str(to_ms(NOW)),
            orderType="Market",
            timeInForce="IOC",
            orderStatus="PartiallyFilled",
            stopOrderType="",
            triggerPrice="",
            reduceOnly=False,
            **{},
        )
        | changes
    )


def execution_row(**changes):
    return (
        dict(
            execId="exec-1",
            orderId="exchange-1",
            orderLinkId="entry-1",
            symbol="BTCUSDT",
            side="Buy",
            execQty="0.005",
            execPrice="60000",
            execFee="0.165",
            execTime=str(to_ms(NOW)),
            isMaker=False,
            execType="Trade",
        )
        | changes
    )


def position_row(symbol="BTCUSDT", **changes):
    return (
        dict(symbol=symbol, size="0", side="", avgPrice="", positionIdx=0, leverage="3") | changes
    )


def wallet_row(**changes):
    return (
        dict(
            accountType="UNIFIED",
            totalWalletBalance="",
            totalPerpUPL="",
            coin=[
                dict(
                    coin="USDT",
                    walletBalance="1000",
                    unrealisedPnl="-10",
                    spotBorrow="0",
                    borrowAmount="0",
                )
            ],
        )
        | changes
    )


def response(result=None, *, code=0):
    return 200, json.dumps(
        dict(
            retCode=code,
            retMsg="never echo secret",
            time=to_ms(NOW),
            result={} if result is None else result,
        )
    ).encode()


class FakeTransport:
    def __init__(self, responses=(), handler=None):
        self.responses, self.handler, self.calls = list(responses), handler, []

    async def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if self.handler:
            item = self.handler(method, url, body)
        else:
            item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def setup(tmp_path):
    config = load_config(Path(__file__).parents[1] / "config.example.toml").bybit
    clock = ManualClock(NOW)
    credential = ApiCredential(Secret("unit-api-key"), Secret("unit-api-secret"), NOW)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    def build(responses=(), handler=None):
        transport = FakeTransport(responses, handler)
        rest = RestClient(config, credential, clock, transport, sleep)
        alerts = []
        gateway = BybitGateway(rest, PauseState(), alerts.append)
        return gateway, transport, sleeps, alerts

    return build


def request(purpose=OrderPurpose.ENTRY, qty="0.02", **changes):
    values = dict(
        order_link_id="entry-1",
        symbol=BTC,
        qty=Qty(D(qty)),
        purpose=purpose,
        reference_price=Price(D("60000")),
    )
    if purpose is OrderPurpose.ENTRY:
        values["stop"] = StopParameters(Price(D("59000" if D(qty) > 0 else "61000")), "LastPrice")
    values.update(changes)
    return OrderRequest(**values)


def ready(gateway):
    gateway._settings_ok = gateway._clock_ok = True
    gateway._products_ok = True
    gateway._refreshed = NOW
    gateway._clock_checked = NOW
    gateway.stream_state(True)
    gateway.synchronized(True)


def test_sign_exact_wire_query_and_json(setup):
    async def run():
        g, t, _, _ = setup([response({"list": []}), response({"orderId": "id"})])
        await g.rest.call("GET", "/v5/order/realtime", {"symbol": "BTCUSDT", "cursor": "a+/ b"})
        await g.rest.call("POST", "/v5/order/create", {"qty": "0.020", "symbol": "BTCUSDT"})
        for method, url, headers, body, _ in t.calls:
            wire = url.split("?", 1)[1] if method == "GET" else body
            value = headers["X-BAPI-TIMESTAMP"] + "unit-api-key" + "5000" + wire
            expected = hmac.new(b"unit-api-secret", value.encode(), hashlib.sha256).hexdigest()
            assert headers["X-BAPI-SIGN"] == expected
            assert "unit-api-key" not in url and "unit-api-key" not in (body or "")
        assert "%2B%2F+b" in t.calls[0][1]
        # Fixed public HMAC SHA256 vector, in addition to the V5 canonicalization checks.
        assert signature("key", "The quick brown fox jumps over the lazy dog") == (
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("unit-api-secret"),
        ConnectionError(),
        (503, b""),
        (200, b"broken"),
        response(code=10006),
        response(code=110072),
        response({}),
    ],
)
def test_create_ambiguous_never_retries(setup, failure):
    async def run():
        g, t, sleeps, _ = setup([failure])
        ready(g)
        result = await g.place_order(request())
        assert result.code is ErrorCode.TIMEOUT
        assert len(t.calls) == 1 and not sleeps
        assert "secret" not in result.message

    asyncio.run(run())


def test_query_retry_and_pagination(setup):
    async def run():
        g, t, sleeps, _ = setup(
            [
                ConnectionError(),
                (500, b""),
                response({"list": [order_row()], "nextPageCursor": "next+/"}),
                response({"list": [order_row(orderId="second")]}),
                response({"list": []}),
            ]
        )
        rows = await g.get_open_orders(BTC)
        assert len(rows) == 2 and sleeps == [1, 2]
        assert "cursor=next%2B%2F" in t.calls[3][1]
        assert "orderFilter=StopOrder" in t.calls[4][1]

    asyncio.run(run())


@pytest.mark.parametrize(
    "code, expected",
    [
        (110001, ErrorCode.NOT_FOUND),
        (110008, ErrorCode.TOO_LATE),
        (110010, ErrorCode.TOO_LATE),
        (110004, ErrorCode.REJECTED),
        (10001, ErrorCode.REJECTED),
        (10014, ErrorCode.TIMEOUT),
    ],
)
def test_command_error_mapping_no_retries(setup, code, expected):
    async def run():
        g, t, _, _ = setup([response(code=code)])
        g._requests["entry-1"] = request()
        result = await g.cancel_order("entry-1")
        assert result.code is expected and len(t.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("qty", ["0.02", "-0.02"])
def test_entry_partial_stop_and_close_flags(setup, qty):
    async def run():
        g, t, _, _ = setup([response({"orderId": "a"})] * 3)
        ready(g)
        assert isinstance(await g.place_order(request(qty=qty)), Accepted)
        entry = json.loads(t.calls[-1][3])
        assert entry["qty"] == "0.02" and entry["positionIdx"] == 0
        assert entry["tpslMode"] == "Partial" and entry["slOrderType"] == "Market"
        assert entry["slTriggerBy"] == "LastPrice" and "reduceOnly" not in entry
        assert entry["side"] == ("Buy" if D(qty) > 0 else "Sell")
        # Unready adapter still submits reduce-only close and replacement SL.
        g.stream_state(False)
        await g.place_order(request(OrderPurpose.CLOSE, qty=qty))
        assert json.loads(t.calls[-1][3])["reduceOnly"] is True
        await g.place_order(request(OrderPurpose.PROTECT, qty=qty, trigger_price=Price(D("59000"))))
        protective = json.loads(t.calls[-1][3])
        assert protective["reduceOnly"] and "stopLoss" not in protective
        assert protective["triggerDirection"] == (1 if D(qty) > 0 else 2)

    asyncio.run(run())


def test_no_entry_without_stop_or_readiness(setup):
    async def run():
        g, t, _, _ = setup([])
        ready(g)
        assert (await g.place_order(request(stop=None))).code is ErrorCode.REJECTED
        assert (
            await g.place_order(request(stop=StopParameters(Price(D("61000")), "LastPrice")))
        ).code is ErrorCode.REJECTED
        g.synchronized(False)
        assert (await g.place_order(request())).code is ErrorCode.REJECTED
        assert not t.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "margin,idx,leverage",
    [("REGULAR_MARGIN", 0, "3"), ("ISOLATED_MARGIN", 1, "3"), ("ISOLATED_MARGIN", 0, "2")],
)
def test_settings_fail_closed_read_only(setup, margin, idx, leverage):
    async def run():
        g, t, _, alerts = setup(
            [
                response({"marginMode": margin}),
                response({"list": [position_row(positionIdx=idx, leverage=leverage)]}),
                response({"list": [position_row("ETHUSDT", positionIdx=idx, leverage=leverage)]}),
            ]
        )
        assert not await g.check_settings()
        assert "bybit_account_settings" in alerts
        assert all(call[0] == "GET" for call in t.calls)

    asyncio.run(run())


def test_clock_pause_protection_and_recovery(setup):
    async def run():
        g, t, _, alerts = setup(
            [
                response({"timeNano": str(to_ms(NOW + timedelta(seconds=2)) * 1_000_000)}),
                response({"orderId": "close"}),
                response({"timeNano": str(to_ms(NOW) * 1_000_000)}),
            ]
        )
        ready(g)
        assert not await g.check_clock()
        assert g.clock_offset_ms == 2000 and "clock" in alerts
        assert (await g.place_order(request())).code is ErrorCode.REJECTED
        assert isinstance(await g.place_order(request(OrderPurpose.CLOSE)), Accepted)
        assert await g.check_clock()
        assert not any(p.reason == "clock" for p in g.pauses.active())
        assert "X-BAPI-SIGN" not in t.calls[0][2]

    asyncio.run(run())


@pytest.mark.parametrize("parent", ["entry-parent", ""])
def test_attached_stop_mapping_no_invented_parent(parent):
    event = mapping.order(
        order_row(
            side="Sell",
            stopOrderType="PartialStopLoss",
            parentOrderLinkId=parent,
            triggerPrice="59000",
            reduceOnly=True,
            orderLinkId="",
        )
    )
    assert event.order_qty.value == D("-0.020")
    assert event.parent_order_link_id == (parent or None)
    assert event.stop_kind is StopKind.PARTIAL_STOP_LOSS
    assert event.order_link_id is None and event.state is OrderState.PARTIALLY_FILLED
    assert event.raw["parentOrderLinkId"] == parent


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("StopLoss", StopKind.STOP_LOSS),
        ("Stop", StopKind.CONDITIONAL),
        ("TakeProfit", StopKind.CONDITIONAL),
    ],
)
def test_stop_mapping(kind, expected):
    assert (
        mapping.order(
            order_row(stopOrderType=kind, triggerPrice="59000", reduceOnly=True)
        ).stop_kind
        is expected
    )


def test_execution_position_wallet_units():
    event = mapping.execution(execution_row(side="Sell", execFee="-0.01", isMaker=True))
    assert event.qty.value == D("-0.005") and event.fee == D("-0.01")
    assert mapping.position(
        position_row(size="0.02", side="Sell", entryPrice="60000", avgPrice="60000"), NOW
    ).qty.value == D("-0.02")
    assert mapping.account(wallet_row(), NOW).equity == D("990")
    with pytest.raises(ValueError):
        mapping.execution(execution_row(execQty=0.1))


def test_d90_signed_total_amend_and_unsupported(setup):
    async def run():
        g, t, _, _ = setup([response({"orderId": "exchange-1"}), response(code=110060)])
        g._remember(
            mapping.order(
                order_row(
                    side="Sell",
                    stopOrderType="PartialStopLoss",
                    triggerPrice="59000",
                    reduceOnly=True,
                    parentOrderLinkId="p",
                )
            )
        )
        assert isinstance(await g.amend_stop("exchange-1", qty=Qty(D("-0.012"))), Accepted)
        body = json.loads(t.calls[0][3])
        assert body["qty"] == "0.012" and "triggerPrice" not in body
        assert body["orderId"] == "exchange-1" and "orderLinkId" not in body
        assert (await g.amend_stop("entry-1", Price(D("59500")))).code is ErrorCode.UNSUPPORTED
        assert "qty" not in json.loads(t.calls[1][3])
        assert (await g.amend_stop("entry-1", qty=Qty(D("0.01")))).code is ErrorCode.REJECTED
        assert len(t.calls) == 2

    asyncio.run(run())


def test_history_bounds_and_repeated_cursor(setup):
    async def run():
        g, t, _, alerts = setup([response({"list": [], "nextPageCursor": "same"})] * 2)
        result = await g.get_order_history(BTC, NOW - timedelta(days=8))
        assert result.code is ErrorCode.QUERY_FAILED and not t.calls
        assert "bybit_history_window" in alerts
        result = await g.get_executions(BTC, NOW - timedelta(days=7))
        assert result.code is ErrorCode.QUERY_FAILED and len(t.calls) == 2

    asyncio.run(run())


def test_all_queries_and_cashflows(setup):
    async def run():
        g, t, _, _ = setup(
            [
                response({"list": [order_row()]}),
                response({"list": [execution_row(), execution_row(execType="Funding")]}),
                response({"list": [position_row()]}),
                response({"list": [position_row("ETHUSDT")]}),
                response({"list": [wallet_row()]}),
                response(
                    {
                        "list": [
                            dict(currency="USDT", type="TRADE"),
                            dict(
                                id="in",
                                currency="USDT",
                                type="TRANSFER_IN",
                                change="100",
                                transactionTime=str(to_ms(NOW)),
                            ),
                            dict(
                                id="out",
                                currency="USDT",
                                type="TRANSFER_OUT",
                                change="-25",
                                transactionTime=str(to_ms(NOW)),
                            ),
                        ]
                    }
                ),
            ]
        )
        assert len(await g.get_order_history(BTC, NOW - timedelta(days=1))) == 1
        assert len(await g.get_executions(BTC, NOW - timedelta(days=1))) == 1
        assert len(await g.get_positions()) == 2
        assert (await g.get_account()).equity == D("990")
        assert sum(f.amount for f in await g.get_cash_flows(NOW - timedelta(days=1))) == D("75")
        assert "category=" not in t.calls[-1][1]

    asyncio.run(run())


def test_daily_instruments_tier_refresh(setup):
    def handler(method, url, body):
        query = parse_qs(urlparse(url).query)
        symbol = query["symbol"][0]
        if "instruments-info" in url:
            return response(
                {
                    "list": [
                        dict(
                            symbol=symbol,
                            lotSizeFilter=dict(qtyStep="0.001", minOrderQty="0.001"),
                            priceFilter=dict(tickSize="0.1"),
                            leverageFilter=dict(maxLeverage="100"),
                        )
                    ]
                }
            )
        return response(
            {
                "list": [
                    dict(
                        symbol=symbol,
                        riskLimitValue="200000",
                        maintenanceMargin="0.005",
                        initialMargin="0.01",
                        maxLeverage="100",
                    )
                ]
            }
        )

    async def run():
        g, t, _, _ = setup(handler=handler)
        assert (await g.get_instrument(BTC)).qty_step == D("0.001")
        assert g.tier_one[BTC].mmr == D("0.005") and len(t.calls) == 4
        await g.get_instrument(BTC)
        assert len(t.calls) == 4
        g.clock.advance(timedelta(days=1))
        await g.get_instrument(BTC)
        assert len(t.calls) == 8
        assert all("X-BAPI-SIGN" not in c[2] for c in t.calls)

    asyncio.run(run())


def kline(confirm):
    return dict(
        topic="kline.60.BTCUSDT",
        data=[
            dict(
                interval="60",
                start=to_ms(NOW),
                confirm=confirm,
                open="60000",
                high="61000",
                low="59000",
                close="60500",
                volume="2",
                turnover="120000",
            )
        ],
    )


def test_public_only_confirmed_bars():
    assert mapping.bars(kline(False)) == ()
    assert mapping.bars(kline(True))[0].close == D("60500")


def test_private_keeps_duplicate_out_of_order_messages(setup):
    async def run():
        g, _, _, _ = setup()
        for state in ("Filled", "New", "Filled"):
            await g.on_private_message(
                dict(topic="order", creationTime=to_ms(NOW), data=[order_row(orderStatus=state)])
            )
        stream = g.private_events()
        assert [(await anext(stream)).state for _ in range(3)] == [
            OrderState.FILLED,
            OrderState.OPEN,
            OrderState.FILLED,
        ]
        await stream.aclose()
        await g.on_private_message(
            dict(topic="wallet", creationTime=to_ms(NOW), data=[wallet_row()])
        )
        assert (await g.wallet_events.get()).equity == D("990")

    asyncio.run(run())


class FakeSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []

    async def send(self, data):
        self.sent.append(data)
        if data["op"] in ("auth", "subscribe"):
            self.queue.put_nowait(dict(op=data["op"], success=True))
        if data["op"] == "ping":
            self.queue.put_nowait(dict(op="pong"))

    async def receive(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


def test_ws_reconnect_reader_runs_during_sync_and_cancel_pauses(setup):
    async def run():
        g, _, _, alerts = setup()
        sockets, delays, calls = [], [], []
        done = asyncio.Event()

        class Factory:
            @asynccontextmanager
            async def connect(self, url):
                assert url == g.config.private_ws_url
                ws = FakeSocket()
                sockets.append(ws)
                yield ws

        async def checked():
            return True

        g.check_settings = g.check_clock = checked

        async def synchronize():
            calls.append("sync")
            assert g.connected and not g._synchronized
            ws = sockets[-1]
            ws.queue.put_nowait(
                dict(topic="execution", creationTime=to_ms(NOW), data=[execution_row()])
            )
            # Synchronizer must be able to collect while its REST reconciliation is in flight.
            stream = g.private_events()
            assert (await asyncio.wait_for(anext(stream), 1)).exec_id == "exec-1"
            await stream.aclose()
            if len(calls) == 1:
                ws.queue.put_nowait(ConnectionError("unit-api-secret"))
            else:
                done.set()
            return True

        async def sleep(delay):
            if delay >= 20:
                await asyncio.Event().wait()
            delays.append(delay)
            await asyncio.sleep(0)

        runner = StreamRunner(g.config, g.clock, Factory(), sleep, alerts.append)
        task = asyncio.create_task(runner.private(g, synchronize))
        await asyncio.wait_for(done.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 2 and delays == [1]
        assert not g.connected and not g._synchronized
        auth = sockets[0].sent[0]
        assert auth["args"][2] == signature("unit-api-secret", f"GET/realtime{to_ms(NOW) + 5000}")
        assert sockets[0].sent[1]["args"] == ["order", "execution", "position", "wallet"]
        assert "secret" not in str(alerts)

    asyncio.run(run())


def test_recovery_discovers_generated_sl_without_guessing_symbol(setup):
    async def run():
        stop = order_row(
            orderId="generated-stop",
            orderLinkId="",
            side="Sell",
            reduceOnly=True,
            stopOrderType="PartialStopLoss",
            triggerPrice="59000",
        )
        g, t, _, _ = setup(
            [
                response({"list": []}),
                response({"list": [stop]}),
                response({"orderId": "generated-stop"}),
            ]
        )
        assert isinstance(await g.amend_stop("generated-stop", qty=Qty(D("-0.01"))), Accepted)
        assert all("orderId=" not in call[1] for call in t.calls[:2])
        assert json.loads(t.calls[-1][3])["orderId"] == "generated-stop"

    asyncio.run(run())


def test_cancel_retries_identical_identity_after_timeout(setup):
    async def run():
        g, t, sleeps, _ = setup(
            [TimeoutError(), response({"orderId": "id", "orderLinkId": "entry-1"})]
        )
        g._requests["entry-1"] = request()
        assert isinstance(await g.cancel_order("entry-1"), Accepted)
        assert t.calls[0][3] == t.calls[1][3] and sleeps == [1]

    asyncio.run(run())


def test_malformed_query_and_unknown_wallet_movement_stop_recovery(setup):
    async def run():
        g, _, _, alerts = setup(
            [
                response({"list": [order_row(orderStatus="FutureStatus")]}),
                response({"list": [dict(currency="USDT", type="UNKNOWN", change="10")]}),
            ]
        )
        assert (await g.get_order_history(BTC, NOW)).code is ErrorCode.QUERY_FAILED
        assert (await g.get_cash_flows(NOW)).code is ErrorCode.QUERY_FAILED
        assert "bybit_cashflow_mapping" in alerts

    asyncio.run(run())


def test_wrong_ack_and_stale_products_do_not_admit_entry(setup):
    async def run():
        g, t, _, _ = setup([response({"orderId": "id", "orderLinkId": "other"})])
        ready(g)
        assert (await g.place_order(request())).code is ErrorCode.TIMEOUT
        g._products_ok = False
        assert (await g.place_order(request())).code is ErrorCode.REJECTED
        assert len(t.calls) == 1

    asyncio.run(run())


def test_transport_no_redirects_and_sanitized_errors():
    class Session:
        def request(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["data"] == b'{"qty":"1"}'
            raise TimeoutError("secret in lower-level error")

    async def run():
        with pytest.raises(ConnectionError, match="^Bybit transport unavailable$"):
            await AiohttpTransport(Session()).request(
                "POST", "https://api-demo.bybit.com", {}, '{"qty":"1"}', 1
            )

    asyncio.run(run())


def test_live_and_crossed_endpoints_refused(setup):
    g, _, _, _ = setup()
    with pytest.raises(ValueError):
        RestClient(
            replace(g.config, rest_url="https://api.bybit.com"),
            g.rest.credential,
            g.clock,
            g.rest.transport,
            g.rest.sleep,
        )
    with pytest.raises(ValueError):
        StreamRunner(
            replace(g.config, private_ws_url="wss://stream.bybit.com/v5/private"),
            g.clock,
            None,
            g.rest.sleep,
            lambda _: None,
        )


def test_ws_handshake_failure_and_missing_pong_force_reconnect(setup):
    async def run():
        g, _, _, _ = setup()

        async def ready_callback():
            pass

        async def on_message(message):
            pass

        async def no_delay(seconds):
            await asyncio.sleep(0)

        class RejectSocket(FakeSocket):
            async def send(self, data):
                self.queue.put_nowait(dict(op=data["op"], success=False, ret_msg="unit-api-secret"))

        runner = StreamRunner(g.config, g.clock, None, no_delay, lambda _: None)
        with pytest.raises(ConnectionError, match="handshake rejected"):
            await runner._session(
                RejectSocket(), ["order"], on_message, ready_callback, g.rest.credential
            )

        class NoPong(FakeSocket):
            async def send(self, data):
                if data["op"] != "ping":
                    await super().send(data)

        # Shorten only the injected unit-test heartbeat after validating production config.
        runner.config = replace(g.config, ws_ping_seconds=0.01)
        with pytest.raises(ExceptionGroup) as failure:
            await runner._session(NoPong(), ["order"], on_message, ready_callback, None)
        assert any(isinstance(e, TimeoutError) for e in failure.value.exceptions)

    asyncio.run(run())


def test_private_invalid_event_is_not_silently_dropped(setup):
    async def run():
        g, _, _, alerts = setup()
        with pytest.raises(ValueError, match="private event invalid"):
            await g.on_private_message(
                dict(topic="order", creationTime=to_ms(NOW), data=[order_row(symbol="DOGEUSDT")])
            )
        assert "bybit_private_mapping" in alerts
        assert g.pauses.has(PauseKind.ENTRY_PAUSED_SYSTEM)

    asyncio.run(run())


def test_actual_m106_synchronizer_accepts_flat_bybit_snapshots(setup, tmp_path):
    from test_portfolio import setup as portfolio_setup
    from test_risk import counter

    from richletsgo.oms.journal import ProtectiveJournal
    from richletsgo.oms.synchronizer import Synchronizer, SyncPolicy

    async def run():
        db, clock, portfolio, router = portfolio_setup()
        clock.set(NOW)

        def handler(method, url, body):
            if "wallet-balance" in url:
                return response({"list": [wallet_row()]})
            if "position/list" in url:
                symbol = parse_qs(urlparse(url).query)["symbol"][0]
                return response({"list": [position_row(symbol)]})
            return response({"list": []})

        gateway, _, _, _ = setup(handler=handler)
        gateway.clock = gateway.rest.clock = clock
        gateway.pauses = router.gate.pauses
        gateway.stream_state(True)
        sync = Synchronizer(
            db.unit_of_work,
            gateway,
            portfolio,
            ProtectiveJournal(tmp_path),
            clock,
            gateway.pauses,
            counter(),
            SyncPolicy(
                timedelta(days=6),
                timedelta(minutes=5),
                timedelta(seconds=20),
                timedelta(seconds=5),
                3,
            ),
            (BTC,),
        )
        assert await sync.synchronize()
        gateway.synchronized(True)
        assert gateway._synchronized
        assert sync.account.wallet_balance == D("1000")
        assert sync.account.unrealized_pnl == D("-10")
        await sync.close()

    asyncio.run(run())
