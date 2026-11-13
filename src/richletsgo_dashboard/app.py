"""FastAPI application for the local dashboard (M5-01a).

Reads only through `DashboardSource`; never writes a database. Control requests need
a logged-in session, a matching CSRF token and a same-origin POST, and are forwarded
to the Trader (disabled until M5-01b).
"""

import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import fmt
from .charts import Series, line_chart, sparkline
from .control import COMMANDS, TraderClient
from .security import HEADERS, Auth, allowed_hosts, same_origin
from .source import DashboardSource, Snapshot, SourceUnavailable

HERE = Path(__file__).parent
COOKIE = "rlg_session"
RANGES = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
PAGES = (
    ("overview", "/", "개요"),
    ("positions", "/positions", "포지션"),
    ("equity", "/equity", "평가액"),
    ("orders", "/orders", "주문·체결"),
    ("logs", "/logs", "기록"),
    ("decisions", "/decisions", "결정·AI"),
    ("control", "/control", "제어"),
)
ORDER_FILTERS = ("all", "open", "unknown", "closed")
OPEN_STATES = {"CREATED", "SUBMITTED", "OPEN", "PARTIALLY_FILLED"}
PAUSE_LABELS = {
    "ACTIVE": "정상",
    "ENTRY_PAUSED_MANUAL": "신규 진입 정지(수동)",
    "ENTRY_PAUSED_SYSTEM": "신규 진입 정지(시스템)",
    "ENTRY_PAUSED_RISK": "신규 진입 정지(위험 한도)",
    "HALTED": "중단(HALTED)",
}
WARN = Decimal("0.8")


def tone(state: str) -> str:
    """Band colour for a pause state: green when trading, red when halted, amber otherwise."""
    return "ok" if state == "ACTIVE" else "halt" if state == "HALTED" else "warn"


def attention(snap: Snapshot, warn: Decimal) -> list[tuple[str, str]]:
    """Things the operator should look at first, as (text, link) pairs."""
    items = []
    unknown = sum(1 for r in snap.orders if r.status == "UNKNOWN")
    if unknown:
        items.append((f"UNKNOWN 주문 {unknown}건: 거래소에서 확인 필요", "/orders?status=unknown"))
    exposed = sum(1 for p in snap.positions if not p.fully_protected)
    if exposed:
        items.append((f"거래소 손절이 보유 수량과 다른 포지션 {exposed}개", "/positions"))
    hot = [limit.code for limit in snap.overview.limits if limit.used >= warn]
    if hot:
        items.append((f"한도 {int(warn * 100)}% 이상 사용: {', '.join(hot)}", "#limits"))
    return items


def create_app(
    source: DashboardSource,
    auth: Auth,
    trader: TraderClient,
    *,
    port: int,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    previous_revoke = auth.on_revoke

    def revoke(session_id: str) -> None:
        previous_revoke(session_id)
        trader.revoke(session_id)

    auth.on_revoke = revoke
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals["new_request_id"] = lambda: secrets.token_urlsafe(24)
    env.filters.update(kst=fmt.kst, num=fmt.num, money=fmt.money, signed=fmt.signed, pct=fmt.pct)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    hosts = allowed_hosts(port)

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host") not in hosts:
            response: Response = Response("invalid host", status_code=400)
        elif request.method not in ("GET", "HEAD") and not same_origin(
            request.headers.get("origin"), port
        ):
            response = Response("cross-origin request refused", status_code=403)
        else:
            response = await call_next(request)
        for key, value in HEADERS.items():
            response.headers[key] = value
        return response

    @app.exception_handler(SourceUnavailable)
    async def unavailable(request: Request, error: Exception) -> Response:
        return Response(
            f"대시보드가 DB를 읽지 못했습니다: {error}\n"
            "--db 경로와 --environment가 Trader 설정과 같은지 확인하세요.",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )

    def page(request: Request, name: str, snap: Snapshot, **extra: Any) -> HTMLResponse:
        session = auth.session(request.cookies.get(COOKIE))
        context = {
            "page": name,
            "pages": PAGES,
            "page_label": (
                "명령 결과"
                if name == "command_result"
                else next(label for key, _, label in PAGES if key == name)
            ),
            "o": snap.overview,
            "pause_label": PAUSE_LABELS.get(snap.overview.pause_state, snap.overview.pause_state),
            "trader": trader.connected() and not snap.overview.sample,
            "session": session,
            **extra,
        }
        return templates.TemplateResponse(request, f"{name}.html", context)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        snap = source.snapshot()
        pts = snap.equity
        if pts:
            pts = [p for p in pts if p.at >= pts[-1].at - timedelta(days=7)]
        return page(
            request,
            "overview",
            snap,
            warn=WARN,
            attention=attention(snap, WARN),
            spark=sparkline([(p.at, p.equity) for p in pts], "최근 7일 평가액 추이"),
            tone=tone(snap.overview.pause_state),
        )

    @app.get("/positions", response_class=HTMLResponse)
    def positions(request: Request) -> HTMLResponse:
        snap = source.snapshot()
        return page(request, "positions", snap, positions=snap.positions)

    @app.get("/equity", response_class=HTMLResponse)
    def equity(request: Request, range: str = "7d") -> HTMLResponse:  # noqa: A002
        snap = source.snapshot()
        span = RANGES.get(range, RANGES["7d"])
        pts = snap.equity
        if span is not None and pts:
            pts = [p for p in pts if p.at >= pts[-1].at - span]
        charts = (
            line_chart(Series("평가액 (USDT)", [(p.at, p.equity) for p in pts], "money")),
            line_chart(
                Series("위험 기준 최고점 대비 낙폭", [(p.at, -p.drawdown) for p in pts], "pct")
            ),
        )
        return page(
            request,
            "equity",
            snap,
            charts=charts,
            ranges=RANGES,
            current=range if range in RANGES else "7d",
            points=pts[-48:][::-1],
        )

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request, status: str = "all") -> HTMLResponse:
        snap = source.snapshot()
        status = status if status in ORDER_FILTERS else "all"
        rows = snap.orders
        if status == "open":
            rows = [r for r in rows if r.status in OPEN_STATES]
        elif status == "unknown":
            rows = [r for r in rows if r.status == "UNKNOWN"]
        elif status == "closed":
            rows = [r for r in rows if r.status not in OPEN_STATES | {"UNKNOWN"}]
        return page(
            request,
            "orders",
            snap,
            orders=rows,
            fills=snap.fills,
            filters=ORDER_FILTERS,
            current=status,
        )

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request) -> HTMLResponse:
        snap = source.snapshot()
        return page(request, "logs", snap, logs=snap.logs)

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(request: Request) -> HTMLResponse:
        snap = source.snapshot()
        return page(request, "decisions", snap, decisions=snap.decisions)

    @app.get("/control", response_class=HTMLResponse)
    def control(request: Request) -> HTMLResponse:
        snap = source.snapshot()
        unknown = [r for r in snap.orders if r.status == "UNKNOWN"]
        return page(request, "control", snap, unknown=unknown, locked=auth.locked())

    @app.get("/api/overview")
    def api_overview() -> JSONResponse:
        snap = source.snapshot()
        o = snap.overview
        return JSONResponse(
            {
                "attention": [{"text": text, "href": href} for text, href in attention(snap, WARN)],
                "pause": PAUSE_LABELS.get(o.pause_state, o.pause_state),
                "pause_state": o.pause_state,
                "tone": tone(o.pause_state),
                "pause_reasons": list(o.pause_reasons),
                "equity": fmt.money(o.equity),
                "today_pnl": fmt.signed(o.today_pnl),
                "drawdown": fmt.pct(o.drawdown),
                "last_closed_bar": fmt.kst(o.last_closed_bar),
                "trader": "연결됨" if trader.connected() else "미연결",
            }
        )

    @app.post("/login")
    def login(request: Request, code: str = Form(...)) -> Response:
        session = auth.login(code, previous=request.cookies.get(COOKIE))
        if session is None:
            return RedirectResponse("/control?login=failed", status_code=303)
        response = RedirectResponse("/control", status_code=303)
        response.set_cookie(COOKIE, session.sid, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")) -> Response:
        session = auth.session(request.cookies.get(COOKIE))
        if session is not None and auth.csrf_ok(session, csrf):
            auth.revoke(session.sid)
        response = RedirectResponse("/control", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/control/{command}")
    def run(
        request: Request,
        command: str,
        csrf: str = Form(""),
        request_id: str = Form(""),
        order_link_id: str = Form(""),
        confirmation_id: str = Form(""),
        manually_checked: bool = Form(False),
    ) -> Response:
        if command not in COMMANDS:
            return Response("unknown command", status_code=404)
        session = auth.session(request.cookies.get(COOKIE))
        if session is None:
            return Response("login required", status_code=401)
        if not auth.csrf_ok(session, csrf):
            return Response("invalid csrf token", status_code=403)
        if not trader.connected():
            return Response(
                "Trader 미연결: 제어 API(M5-01b) 이후 사용할 수 있습니다", status_code=503
            )
        if not request_id or len(request_id) > 256:
            return Response("request ID required", status_code=400)
        snap = source.snapshot()
        if snap.overview.sample:
            return Response("sample data cannot control a Trader", status_code=403)
        try:
            result = trader.command(
                command,
                session_id=session.sid,
                environment=snap.overview.environment,
                account_id=snap.overview.account_id,
                request_id=request_id,
                target=order_link_id or None,
                confirmation_id=confirmation_id or None,
                manually_checked=manually_checked,
            )
        except OSError:
            result = {
                "state": "response_unknown",
                "message": "Trader 응답 확인 필요: 같은 요청 ID로 다시 확인하세요",
            }
        result = {
            "command": command,
            "request_id": request_id,
            "target": order_link_id or None,
            "confirmation_id": confirmation_id or None,
            "manually_checked": manually_checked,
            **result,
        }
        return page(request, "command_result", snap, result=result)

    return app


def monotonic() -> float:
    return time.monotonic()
