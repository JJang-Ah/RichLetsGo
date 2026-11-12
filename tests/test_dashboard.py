"""M5-01a dashboard: screens on fake data, local-only access, sessions, CSRF and XSS."""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from richletsgo_dashboard import fmt
from richletsgo_dashboard.app import COOKIE, create_app
from richletsgo_dashboard.charts import Series, line_chart
from richletsgo_dashboard.control import Disconnected
from richletsgo_dashboard.security import (
    ABSOLUTE_SECONDS,
    CODE_RECHECK_SECONDS,
    IDLE_SECONDS,
    LOCKOUT_SECONDS,
    Auth,
    require_loopback,
)
from richletsgo_dashboard.source import Decision, FakeSource, LogEntry, Snapshot

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"origin": BASE}
CODE = "correct horse battery"
XSS = ["<script>alert(1)</script>", "<img src=x onerror=alert(2)>", '"><svg onload=alert(3)>']


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Connected:
    def connected(self) -> bool:
        return True


class XssSource(FakeSource):
    def snapshot(self) -> Snapshot:
        snap = super().snapshot()
        at = self.now
        snap.logs = [LogEntry(at, "alert", "system", text) for text in XSS]
        snap.decisions = [
            Decision(f"sig-{i}", at, "S1", "BTCUSDT", Decimal("0.7"), text, Decimal("0.01"))
            for i, text in enumerate(XSS)
        ]
        snap.overview = replace(snap.overview, pause_reasons=tuple(XSS))
        return snap


def make(source: FakeSource | None = None, trader: object | None = None, code: str | None = CODE):
    clock = Clock()
    codes = {"value": code}
    auth = Auth(code=lambda: codes["value"], clock=clock)
    app = create_app(source or FakeSource(), auth, trader or Disconnected(), port=PORT)  # type: ignore[arg-type]
    return TestClient(app, base_url=BASE), auth, clock, codes


def login(client: TestClient, code: str = CODE) -> str | None:
    r = client.post("/login", data={"code": code}, headers=ORIGIN, follow_redirects=False)
    return r.cookies.get(COOKIE)


def csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/control").text)
    assert match
    return match.group(1)


# --- screens ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/positions",
        "/equity",
        "/equity?range=1d",
        "/equity?range=all",
        "/orders",
        "/orders?status=unknown",
        "/logs",
        "/decisions",
        "/control",
    ],
)
def test_every_screen_renders_with_security_headers(path: str) -> None:
    client, *_ = make()
    r = client.get(path)
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert "style=" not in r.text  # inline styles would need 'unsafe-inline'


def test_overview_shows_decimal_strings_unknown_and_environment() -> None:
    client, *_ = make()
    text = client.get("/").text
    assert "DEMO" in text and "신규 진입 정지(시스템)" in text and "-42.15" in text
    orders = client.get("/orders?status=unknown").text
    assert "D-S1-E-a0c2" in orders and "row-unknown" in orders and "status-filled" not in orders
    positions = client.get("/positions").text
    assert "0.050" in positions and "96,120.5" in positions  # Decimal as stored, no float
    assert "0.50 / 0.80" in positions  # partial exchange stop is flagged


def test_live_environment_and_halted_are_marked() -> None:
    class Live(FakeSource):
        def snapshot(self) -> Snapshot:
            snap = super().snapshot()
            snap.overview = replace(snap.overview, pause_state="HALTED")
            return snap

    client, *_ = make(Live(environment="live"))
    text = client.get("/").text
    assert 'class="env-live"' in text and "LIVE" in text and "중단(HALTED)" in text


def test_empty_data_renders_empty_states() -> None:
    class Empty(FakeSource):
        def snapshot(self) -> Snapshot:
            return Snapshot(super().snapshot().overview)

    client, *_ = make(Empty())
    assert "보유 포지션이 없습니다" in client.get("/positions").text
    assert "데이터가 아직 없습니다" in client.get("/equity").text
    assert "해당하는 주문이 없습니다" in client.get("/orders").text


def test_formatting_never_uses_float() -> None:
    assert fmt.num(Decimal("0.10000000")) == "0.10000000"
    assert fmt.num(Decimal("1234567.5")) == "1,234,567.5"
    assert fmt.num(Decimal("1E-8")) == "0.00000001"
    assert fmt.money(Decimal("-42.155")) == "-42.16"
    assert fmt.pct(Decimal("0.0025")) == "0.25%"
    assert fmt.kst(datetime(2026, 11, 11, 15, 0, tzinfo=UTC)) == "2026-11-12 00:00"
    with pytest.raises(ValueError):
        fmt.kst(datetime(2026, 11, 11))


# --- local-only access ---------------------------------------------------------------------


def test_only_loopback_bind_and_host_header() -> None:
    for host in ("0.0.0.0", "192.168.0.2", "localhost", "::"):
        with pytest.raises(ValueError):
            require_loopback(host, PORT)
    require_loopback("127.0.0.1", PORT)
    client, *_ = make()
    assert client.get("/", headers={"host": "evil.example:8765"}).status_code == 400
    assert client.get("/", headers={"host": f"localhost:{PORT}"}).status_code == 200


def test_cross_origin_post_is_refused_before_login() -> None:
    client, *_ = make()
    r = client.post("/login", data={"code": CODE}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.post("/login", data={"code": CODE}).status_code == 403  # no Origin


# --- sessions and control ------------------------------------------------------------------


def test_control_needs_login_csrf_and_trader() -> None:
    client, *_ = make()
    assert client.post("/control/pause", data={"csrf": "x"}, headers=ORIGIN).status_code == 401
    sid = login(client)
    assert sid
    client.cookies.set(COOKIE, sid)
    assert client.post("/control/pause", data={"csrf": "bad"}, headers=ORIGIN).status_code == 403
    token = csrf(client)
    r = client.post("/control/pause", data={"csrf": token}, headers=ORIGIN)
    assert r.status_code == 503 and "Trader 미연결" in r.text
    assert client.post("/control/order", data={"csrf": token}, headers=ORIGIN).status_code == 404
    page = client.get("/control").text
    assert page.count("disabled") >= 3 and "로그아웃" in page


def test_connected_trader_enables_buttons_but_forwarding_waits_for_m5_01b() -> None:
    client, *_ = make(trader=Connected())
    client.cookies.set(COOKIE, login(client) or "")
    page = client.get("/control").text
    assert "Trader 미연결" not in page and " disabled" not in page
    r = client.post("/control/pause", data={"csrf": csrf(client)}, headers=ORIGIN)
    assert r.status_code == 501


def test_login_rotates_session_and_locks_after_failures() -> None:
    client, auth, clock, _ = make()
    first, second = login(client), login(client)
    assert first and second and first != second
    for _ in range(5):
        assert login(client, "wrong") is None
    assert auth.locked() and login(client) is None
    client.cookies.clear()
    assert "10분 동안 잠겼습니다" in client.get("/control").text
    clock.now += LOCKOUT_SECONDS + 1
    assert login(client)


def test_sessions_expire_and_are_revoked() -> None:
    client, auth, clock, codes = make()
    sid = login(client)
    assert auth.session(sid)
    clock.now += IDLE_SECONDS + 1
    assert auth.session(sid) is None  # idle

    sid = login(client)
    for _ in range(ABSOLUTE_SECONDS // (IDLE_SECONDS - 60) + 1):
        clock.now += IDLE_SECONDS - 60
        auth.session(sid)
    assert auth.session(sid) is None  # absolute

    sid = login(client)
    client.cookies.set(COOKIE, sid or "")
    client.post("/logout", data={"csrf": csrf(client)}, headers=ORIGIN)
    assert auth.session(sid) is None  # logout

    sid = login(client)
    codes["value"] = "a new code"
    clock.now += CODE_RECHECK_SECONDS
    assert auth.session(sid) is None  # access code changed
    assert login(client, CODE) is None and login(client, "a new code")

    restarted = Auth(code=lambda: "a new code", clock=clock)
    assert restarted.session(sid) is None  # restart: in-memory sessions are gone


def test_no_access_code_disables_login() -> None:
    client, *_ = make(code=None)
    assert login(client, "") is None and login(client, "anything") is None


def test_secrets_never_reach_html() -> None:
    client, *_ = make()
    client.cookies.set(COOKIE, login(client) or "")
    for path in ("/", "/control", "/logs", "/api/overview"):
        assert CODE not in client.get(path).text
    assert CODE not in client.post("/login", data={"code": "x"}, headers=ORIGIN).text


# --- XSS -------------------------------------------------------------------------------


def test_untrusted_text_is_escaped_everywhere() -> None:
    client, *_ = make(XssSource())
    for path in ("/", "/logs", "/decisions"):
        text = client.get(path).text
        assert (
            "<script>alert" not in text and "<img src=x" not in text and "<svg onload" not in text
        )
        assert "&lt;script&gt;" in text or "&lt;img" in text
    chart = str(
        line_chart(
            Series(
                XSS[0],
                [
                    (datetime.now(UTC), Decimal(1)),
                    (datetime.now(UTC) + timedelta(hours=1), Decimal(2)),
                ],
                "money",
            )
        )
    )
    assert "<script>" not in chart and "&lt;script&gt;" in chart


# --- PR #64 review cases -------------------------------------------------------------------


def test_old_code_login_racing_a_code_change_cannot_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from richletsgo_dashboard import security

    clock = Clock()
    codes = {"value": "old code"}
    auth = Auth(code=lambda: codes["value"], clock=clock)
    assert auth.session(None) is None  # load the old digest
    real = security._digest
    verifying = threading.Event()

    def slow_digest(text: str) -> bytes:
        if text == "old code" and threading.current_thread().name == "login":
            verifying.set()
            time.sleep(0.2)  # login is mid-verification with the old code
        return real(text)

    monkeypatch.setattr(security, "_digest", slow_digest)
    issued: list[object] = []
    worker = threading.Thread(target=lambda: issued.append(auth.login("old code")), name="login")
    worker.start()
    assert verifying.wait(2)
    codes["value"] = "new code"
    clock.now += CODE_RECHECK_SECONDS
    auth.session(None)  # another request notices the change and revokes
    worker.join()
    session = issued[0]
    # Either the login finished first and was revoked, or it was refused; never alive.
    assert session is None or auth.session(session.sid) is None  # type: ignore[attr-defined]
    assert auth.sessions == {}


def test_relogin_revokes_previous_session_but_failure_keeps_it() -> None:
    client, auth, *_ = make()
    first = login(client)
    assert first
    client.cookies.set(COOKIE, first)
    old_csrf = csrf(client)
    assert login(client, "wrong") is None
    assert auth.session(first)  # failed attempt leaves the session alone
    second = login(client)
    assert second and second != first
    assert auth.session(first) is None
    client.cookies.set(COOKIE, first)
    assert client.post("/control/pause", data={"csrf": old_csrf}, headers=ORIGIN).status_code == 401
    client.cookies.set(COOKIE, second)
    assert (
        client.post("/control/pause", data={"csrf": csrf(client)}, headers=ORIGIN).status_code
        == 503
    )
    client.post("/logout", data={"csrf": csrf(client)}, headers=ORIGIN)
    assert auth.session(second) is None


def test_sample_banner_marks_fake_data() -> None:
    client, *_ = make(FakeSource(environment="live"))
    for path in ("/", "/positions", "/control"):
        assert "샘플 데이터입니다" in client.get(path).text


def test_overview_lists_what_to_check_first() -> None:
    client, *_ = make()
    text = client.get("/").text
    assert "먼저 확인할 것" in text
    assert "UNKNOWN 주문 1건" in text and 'href="/orders?status=unknown"' in text
    assert "거래소 손절이 보유 수량과 다른 포지션 1개" in text
    assert '<h1 class="sr-only">개요</h1>' in text and 'href="#main"' in text


def test_overview_all_clear_when_nothing_to_check() -> None:
    class Calm(FakeSource):
        def snapshot(self) -> Snapshot:
            snap = super().snapshot()
            snap.orders = [r for r in snap.orders if r.status != "UNKNOWN"]
            snap.positions = [p for p in snap.positions if p.fully_protected]
            snap.overview = replace(
                snap.overview,
                limits=[replace(limit, used=Decimal("0.1")) for limit in snap.overview.limits],
            )
            return snap

    client, *_ = make(Calm())
    assert "확인할 항목이 없습니다." in client.get("/").text


def test_refresh_api_carries_current_attention_items() -> None:
    calm = True

    class Switching(FakeSource):
        def snapshot(self) -> Snapshot:
            snap = super().snapshot()
            if calm:
                snap.orders = [r for r in snap.orders if r.status != "UNKNOWN"]
                snap.positions = [p for p in snap.positions if p.fully_protected]
                snap.overview = replace(
                    snap.overview,
                    limits=[replace(x, used=Decimal("0.1")) for x in snap.overview.limits],
                )
            return snap

    client, *_ = make(Switching())
    assert client.get("/api/overview").json()["attention"] == []
    calm = False
    items = client.get("/api/overview").json()["attention"]
    assert {
        "text": "UNKNOWN 주문 1건: 거래소에서 확인 필요",
        "href": "/orders?status=unknown",
    } in items
    assert any(i["href"] == "/positions" for i in items)


def test_overview_band_shows_state_tone_and_equity_trace() -> None:
    client, *_ = make()
    text = client.get("/").text
    assert 'class="band tone-warn"' in text  # sample is ENTRY_PAUSED_SYSTEM
    assert 'class="spark"' in text and 'href="/equity?range=7d"' in text


def test_refresh_api_carries_band_tone_and_reasons() -> None:
    state: list[tuple[str, tuple[str, ...]]] = [("ACTIVE", ())]

    class Switching(FakeSource):
        def snapshot(self) -> Snapshot:
            snap = super().snapshot()
            code, reasons = state[0]
            snap.overview = replace(snap.overview, pause_state=code, pause_reasons=reasons)
            return snap

    client, *_ = make(Switching())
    page = client.get("/").text
    assert 'class="band tone-ok" id="band"' in page
    assert '<p class="state-why" id="state-why" hidden></p>' in page  # refresh target exists
    seen = []
    for code, reasons in (("ACTIVE", ()), ("HALTED", ("daily_loss",)), ("ACTIVE", ())):
        state[0] = (code, reasons)
        data = client.get("/api/overview").json()
        seen.append((data["pause_state"], data["tone"], data["pause_reasons"]))
    assert seen == [
        ("ACTIVE", "ok", []),
        ("HALTED", "halt", ["daily_loss"]),
        ("ACTIVE", "ok", []),
    ]
